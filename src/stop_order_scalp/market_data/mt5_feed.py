"""The live market-data feed: connection, symbol resolution, candles, ticks.

One of only two modules permitted to name ``MetaTrader5`` (the other is
:mod:`stop_order_scalp.execution.mt5_broker`), and even here the import is indirect --
through :class:`~stop_order_scalp.market_data.mt5_module.MT5Module`, which owns it. The
architecture check allows both modules; keeping the ``import`` statement in one file is
what makes the boundary reviewable.

Three things are worth stating plainly, because each is a way this could quietly lie.

**Which clock decides that a bar has closed.** Broker server time, from the terminal's own
``time_current()``, never the local machine clock. See :class:`ServerClock` and
``docs/mt5/SETUP.md`` for the full argument; the short version is that the terminal
anchors daily bars and session boundaries to server time, so comparing a server-timestamped
bar against a local clock can be off by hours and would shift every boundary.

**What a candle method returns.** :meth:`MT5Feed.candles` returns **only closed bars**, and
it re-freezes on every call rather than trusting a cached result, because a bar that was
forming a second ago may be closed now and vice versa after a reconnect. The forming bar
is reachable only through :meth:`MT5Feed.forming_candle`, by name.

**What happens when the terminal is absent.** Every entry point raises
:class:`~stop_order_scalp.domain.exceptions.BrokerNotConnectedError` or returns a report
saying so. Nothing silently degrades to empty data, because "no candles" and "no terminal"
lead to opposite actions.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from stop_order_scalp.domain.exceptions import (
    BrokerNotConnectedError,
    MarketDataError,
    RetryableError,
    SymbolNotFoundError,
)
from stop_order_scalp.domain.interfaces import Clock
from stop_order_scalp.domain.models import (
    AccountSnapshot,
    Candle,
    EnvironmentSettings,
    Tick,
)
from stop_order_scalp.domain.value_objects import SymbolSpecification
from stop_order_scalp.infrastructure.clock import SystemClock
from stop_order_scalp.infrastructure.config import AppConfig
from stop_order_scalp.market_data.candles import FreezeReport, freeze_closed_bars
from stop_order_scalp.market_data.mt5_module import (
    MT5Api,
    MT5Module,
    account_info_to_snapshot,
    as_sequence,
    epoch_to_datetime,
    row_to_candle,
    symbol_info_to_specification,
    tick_info_to_tick,
)
from stop_order_scalp.market_data.timeframes import canonical_name, period_seconds, to_mt5

__all__ = ["MT5Feed", "ServerClock", "probe_connection"]

#: ``copy_rates_from_pos`` positions are counted from the current bar, so 0 is the bar in
#: progress. Always request one more than needed and let the freeze drop the last one, so
#: the forming bar can never reach a decision.
_EXTRA_BAR: Final[int] = 1


class ServerClock:
    """Broker server time, read from the terminal.

    Implements :class:`~stop_order_scalp.domain.interfaces.Clock`, so it can be injected
    anywhere a clock is expected.

    Why not the system clock: MetaTrader 5 anchors daily bars, session boundaries and
    symbol trading hours to **server** time, which for many brokers is UTC+2 or UTC+3. A
    bar stamped 00:00 server time closes at 00:00 server time; on a machine running UTC,
    local time reaches that instant two or three hours late. Every "is this bar closed"
    answer would then be wrong by that offset, and on an hourly or daily timeframe it would
    be wrong for hours at a stretch.

    Falls back to the local clock only if the terminal cannot be reached, and says so via
    :attr:`degraded`. A degraded clock is visible rather than silent, because it is exactly
    the kind of substitution this project refuses to make invisibly.
    """

    __slots__ = ("_degraded", "_fallback", "_module")

    def __init__(self, module: MT5Module, fallback: Clock) -> None:
        self._module = module
        self._fallback = fallback
        self._degraded = False

    @property
    def degraded(self) -> bool:
        """Whether the last read fell back to local time."""
        return self._degraded

    def now(self) -> datetime:
        self._degraded = False
        try:
            seconds = self._module.api().time_current()
        except (BrokerNotConnectedError, AttributeError, TypeError):
            self._degraded = True
            return self._fallback.now()
        if not seconds:
            # The terminal is reachable but has no server clock yet, which happens
            # immediately after a connect and before the first tick.
            self._degraded = True
            return self._fallback.now()
        return epoch_to_datetime(seconds)

    def __repr__(self) -> str:
        return f"ServerClock(degraded={self._degraded})"


class MT5Feed:
    """Live candles, ticks and account state from a running MetaTrader 5 terminal.

    Implements :class:`~stop_order_scalp.domain.interfaces.MarketDataProvider`, plus the
    read-only half of
    :class:`~stop_order_scalp.domain.interfaces.AccountReader`. It never writes to the
    broker: there is no order method on this class, and that is deliberate. Anything that
    can place an order belongs to
    :class:`~stop_order_scalp.domain.interfaces.Broker`, in Phase 5.
    """

    __slots__ = ("_api", "_clock", "_connected", "_module", "_specifications", "_symbols")

    def __init__(self, module: MT5Module | None = None, *, clock: Clock | None = None) -> None:
        self._module = module if module is not None else MT5Module()
        self._clock = clock if clock is not None else SystemClock()
        self._connected = False
        self._specifications: dict[str, SymbolSpecification] = {}
        self._symbols: dict[str, str] = {}

    # --- connection ------------------------------------------------------

    @property
    def server_clock(self) -> ServerClock:
        return ServerClock(self._module, self._fallback_clock)

    @property
    def _fallback_clock(self) -> Clock:
        return self._clock

    def connect(self, settings: EnvironmentSettings) -> None:
        """Start or attach to the terminal. Idempotent.

        Credentials are optional, and the docstring above used to say so without the code
        agreeing: it passed ``login``/``password``/``server`` unconditionally, so a terminal
        that was already signed in still got a login attempt with an empty password -- and
        MetaTrader 5 answers that with error ``-2, Invalid "password" argument`` rather than
        falling back to the saved session. The result was that a working, authenticated,
        already-running terminal reported as unreachable.

        So credentials are now passed **only when a password is actually configured**. With
        none, ``initialize(path=...)`` attaches to the terminal's own session, which is both
        the normal case and the safer one: reading a symbol specification should not require
        storing a broker password anywhere on disk.

        :raises BrokerNotConnectedError: if the terminal refuses, with the terminal's own
            error code and message attached.
        """
        api = self._module.api()
        password = _password()
        if password:
            ok = api.initialize(
                path=settings.mt5_path,
                login=settings.mt5_login,
                password=password,
                server=settings.mt5_server,
                timeout=settings.mt5_timeout_ms,
                portable=False,
            )
        else:
            # No password stored: attach to whatever session the terminal already has. An
            # empty password is not the same as no password to MetaTrader 5, and it is
            # rejected as invalid rather than ignored.
            ok = api.initialize(
                path=settings.mt5_path,
                timeout=settings.mt5_timeout_ms,
                portable=False,
            )
        if not ok:
            code, message = self._module.describe_last_error()
            raise BrokerNotConnectedError(
                f"MetaTrader 5 refused to initialize (code {code}): {message}. "
                "Check SOS_MT5_PATH, that the terminal is not already running under "
                "another user, and that the login is permitted."
            )
        self._connected = True

    def shutdown(self) -> None:
        """Detach. Idempotent, and safe when never connected."""
        if self._connected:
            self._module.api().shutdown()
            self._connected = False
        self._specifications.clear()
        self._symbols.clear()

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _require_connection(self) -> MT5Api:
        if not self._connected:
            raise BrokerNotConnectedError("not connected to MetaTrader 5; call connect() first")
        return self._module.api()

    # --- symbols ---------------------------------------------------------

    def resolve_symbol(self, configured: str, aliases: Sequence[str] = ()) -> str:
        """Map a configured logical instrument to a name this broker actually offers.

        Matching is exact and case-insensitive, over the configured name followed by any
        explicit aliases. There is no substring or fuzzy match, so ``US30`` can never
        select ``US30mini`` or ``EURUSD30`` by accident.
        """
        api = self._require_connection()
        for candidate in (configured, *aliases):
            name = candidate.strip()
            if not name:
                continue
            if api.symbol_select(name, True):
                self._symbols[configured] = name
                return name
        raise SymbolNotFoundError(
            f"none of {list(dict.fromkeys([configured, *aliases]))} is available on this "
            "account; check config/default.yaml symbol_aliases against the broker's "
            "Market Watch list"
        )

    def specification(self, symbol: str) -> SymbolSpecification:
        """The instrument's contract details, resolved once and cached."""
        cached = self._specifications.get(symbol)
        if cached is not None:
            return cached
        api = self._require_connection()
        info = api.symbol_info(symbol)
        if info is None:
            raise SymbolNotFoundError(f"the terminal does not know a symbol called {symbol!r}")
        specification = symbol_info_to_specification(info)
        self._specifications[symbol] = specification
        return specification

    def digits_for(self, symbol: str) -> int:
        return self.specification(symbol).digits

    # --- candles ---------------------------------------------------------

    def candles(
        self,
        symbol: str,
        timeframe: str,
        count: int,
        *,
        before: datetime | None = None,
    ) -> Sequence[Candle]:
        """Closed candles for ``timeframe``, **oldest first**.

        :param before: judge closure as of this moment instead of the server clock. This is
            the backtest and replay seam: supplying it makes the answer independent of when
            the call happened, which is what a historical replay needs.
        :raises MarketDataError: if ``count`` is not positive.
        """
        closed, _ = self.freeze(symbol, timeframe, count, before=before)
        return closed

    def freeze(
        self,
        symbol: str,
        timeframe: str,
        count: int,
        *,
        before: datetime | None = None,
    ) -> tuple[tuple[Candle, ...], FreezeReport]:
        """Closed candles plus the report of what was withheld.

        The form every consumer should use. :meth:`candles` discards the report; nothing
        that feeds a decision should discard it.
        """
        if count <= 0:
            raise MarketDataError(f"count must be positive, got {count}")
        api = self._require_connection()
        name = canonical_name(timeframe)
        digits = self.digits_for(symbol)

        # One extra bar, because the newest element of a rates table is the one in
        # progress. Requesting the exact count and trusting the feed to exclude it would
        # make the freeze depend on a terminal behaviour we do not control.
        table = api.copy_rates_from_pos(symbol, to_mt5(name), 0, count + _EXTRA_BAR)
        if table is None:
            code, message = self._module.describe_last_error()
            raise RetryableError(f"copy_rates_from_pos({symbol}, {name}) failed: {message} ({code})")

        rows = as_sequence(table)
        # The table is newest-first; row_to_candle is order-agnostic and the freeze sorts,
        # so the ordering is handled in exactly one place.
        candles = [row_to_candle(row, timeframe=name, digits=digits) for row in rows]
        reference = before if before is not None else self.server_clock.now()
        closed, report = freeze_closed_bars(candles, reference=reference, timeframe=name)
        return closed[-count:], report

    def forming_candle(self, symbol: str, timeframe: str) -> Candle | None:
        """The bar currently in progress, or ``None``.

        Present for research and for the ``current_forming`` selection mode. The baseline
        strategy never calls it, and naming it explicitly is what keeps the default safe.
        """
        api = self._require_connection()
        name = canonical_name(timeframe)
        digits = self.digits_for(symbol)
        table = api.copy_rates_from_pos(symbol, to_mt5(name), 0, 1)
        rows = as_sequence(table)
        if not rows:
            return None
        candle = row_to_candle(rows[0], timeframe=name, digits=digits, is_confirmed=False)
        if candle.is_closed_at(self.server_clock.now()):
            return None
        return candle

    # --- ticks and account -----------------------------------------------

    def tick(self, symbol: str) -> Tick | None:
        """The latest bid/ask, or ``None`` when the symbol is quiet."""
        api = self._require_connection()
        info = api.symbol_info_tick(symbol)
        if info is None:
            return None
        return tick_info_to_tick(info, digits=self.digits_for(symbol))

    def stream_ticks(self, symbol: str) -> Iterator[Tick]:
        """Yield ticks as they arrive.

        A blocking generator. The caller owns the deadline and the cancellation; this
        yields what the terminal gives it and never invents an interval.
        """
        api = self._require_connection()
        digits = self.digits_for(symbol)
        previous = 0
        while True:
            info = api.symbol_info_tick(symbol)
            if info is None:
                continue
            moment = int(info["time"] if isinstance(info, dict) else info.time)
            if moment > previous:
                previous = moment
                yield tick_info_to_tick(info, digits=digits)

    def account(self) -> AccountSnapshot:
        api = self._require_connection()
        info = api.account_info()
        if info is None:
            raise BrokerNotConnectedError("the terminal returned no account information")
        return account_info_to_snapshot(info)

    def server_time(self) -> datetime:
        return self.server_clock.now()

    def symbol_available(self, symbol: str) -> bool:
        try:
            api = self._require_connection()
        except BrokerNotConnectedError:
            return False
        return bool(api.symbol_info(symbol))

    def leverage_for(self, symbol: str) -> Decimal:
        api = self._require_connection()
        info = api.symbol_info(symbol)
        value = None if info is None else getattr(info, "leverage", None)
        if value is None and info is not None:
            value = info.get("leverage") if isinstance(info, dict) else None
        return Decimal(str(value or 100))

    def __repr__(self) -> str:
        return f"MT5Feed(connected={self._connected})"


def _password() -> str | None:
    """The terminal password, read once and never stored.

    ``EnvironmentSettings`` deliberately keeps only a presence flag, so the value has to be
    fetched from the environment here and handed straight to the terminal. Nothing here
    records it, and it is not an argument to anything that logs.
    """
    return os.environ.get("SOS_MT5_PASSWORD") or None


def probe_connection(config: AppConfig) -> dict[str, Any]:
    """Read-only reachability report for ``test-connection``.

    Never raises and never writes to the account. Every failure mode becomes a field in the
    returned report, because "the command crashed" is a far worse diagnostic than "the
    package is not installed".

    Note that credentials are not required: the terminal attaches to its own saved session
    if one exists, so a successful probe proves reachability without proving that this
    project's credentials are right. Check ``credentials_configured`` for that.
    """
    report: dict[str, Any] = {
        "connected": False,
        "terminal_path_configured": bool(config.environment.mt5_path),
        "credentials_configured": config.environment.has_credentials,
        "package_installed": False,
        "terminal_running": False,
        "account_login": None,
        "account_server": None,
        "is_demo": None,
        "error": None,
    }
    module = MT5Module()
    try:
        api = module.api()
    except BrokerNotConnectedError as exc:
        report["error"] = str(exc)
        return report
    report["package_installed"] = True

    try:
        report["version"] = tuple(int(part) for part in api.version())
    except (AttributeError, TypeError, ValueError):
        report["version"] = None

    try:
        feed = MT5Feed(module)
        feed.connect(config.environment)
    except Exception as exc:
        # A probe reports, it does not propagate: "the command crashed" is a far worse
        # diagnostic than "the terminal refused with code -1".
        code, message = module.describe_last_error()
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["last_error"] = {"code": code, "message": message}
        return report

    report["connected"] = True
    report["terminal_running"] = True
    try:
        terminal = api.terminal_info()
        report["terminal_build"] = None if terminal is None else int(
            getattr(terminal, "build", 0) or 0
        )
    except (AttributeError, TypeError, ValueError):
        report["terminal_build"] = None
    try:
        snapshot = feed.account()
        report["account_login"] = snapshot.login
        report["account_server"] = snapshot.server
        report["is_demo"] = snapshot.is_demo
        report["currency"] = snapshot.currency
    except (BrokerNotConnectedError, MarketDataError):
        pass
    finally:
        feed.shutdown()
    return report


def period_of(timeframe: str) -> int:
    """Length of one bar in seconds. Re-exported so feed callers need one import."""
    return period_seconds(canonical_name(timeframe))
