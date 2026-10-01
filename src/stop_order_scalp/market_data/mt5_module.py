"""The MetaTrader 5 boundary: one lazy import, typed protocols, and pure converters.

Three responsibilities, deliberately in one module so that there is exactly one place
that knows how the terminal is reached:

1. **Load** the terminal's Python package, lazily, inside a function. Nothing above this
   module may import ``MetaTrader5``; ``scripts/check_architecture.py`` enforces that this
   module and :mod:`stop_order_scalp.execution.mt5_broker` are the only two that may.
2. **Describe** the slice of the API actually used, as protocols. The package ships no
   type information, and a hand-written protocol is strictly better than ``Any``: it makes
   the test suite able to substitute a fake, and it documents the dependency surface.
3. **Convert** the terminal's rows into domain objects, with no I/O and no clock. Every
   converter is a pure function, so each is testable without a terminal.

Why the rows are duck-typed rather than numpy arrays
-----------------------------------------------------
``copy_rates_from_pos`` returns a numpy structured array. numpy is deliberately **not** a
core dependency -- the project is importable and testable with stdlib plus a YAML parser
alone. Fortunately a structured array supports ``len()``, iteration, and ``row["time"]``,
which is all this module needs, so :class:`RatesTable` describes a *protocol* and the
converters never import numpy. That keeps the dependency rule structural rather than a
comment, and it is why the conversion helpers accept any row supporting string indexing.

Field access goes through :func:`field` rather than attribute access, because the terminal
returns namedtuples while a hand-written fake usually uses a dataclass or a dict. Both are
supported, and the test suite exercises both.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from types import ModuleType
from typing import Any, Final, Protocol, runtime_checkable

from stop_order_scalp.domain.exceptions import (
    BrokerNotConnectedError,
    InvalidSpecificationError,
    MarketDataError,
)
from stop_order_scalp.domain.models import AccountSnapshot, Candle, Tick
from stop_order_scalp.domain.value_objects import Money, Price, SymbolSpecification
from stop_order_scalp.market_data.timeframes import canonical_name, period_seconds

__all__ = [
    "MT5Api",
    "MT5Module",
    "RatesRow",
    "RatesTable",
    "account_info_to_snapshot",
    "epoch_to_datetime",
    "field",
    "row_to_candle",
    "symbol_info_to_specification",
    "tick_info_to_tick",
]

#: MetaTrader 5 error code for "function call failed for an internal reason". Distinguished
#: because it is the one that usually means a terminal that is not running.
_MT5_INTERNAL_ERROR: Final[int] = -1


# =============================================================================
# The slice of the API we use
# =============================================================================


@runtime_checkable
class RatesRow(Protocol):
    """One bar as the terminal returns it: support ``row["field"]``."""

    def __getitem__(self, key: str) -> Any: ...


@runtime_checkable
class RatesTable(Protocol):
    """A bar series: something with a length that yields :class:`RatesRow`.

    Matches a numpy structured array, a list of namedtuples, and a list of dicts, so no
    numpy import is needed to describe what we consume.
    """

    def __len__(self) -> int: ...

    def __iter__(self) -> Iterator[RatesRow]: ...


@runtime_checkable
class MT5Api(Protocol):
    """Exactly the terminal functions this project calls.

    Every parameter is keyword-only in the real package, and the signatures mirror that, so
    a mistake shows up in the type checker rather than at the terminal. ``@runtime_checkable``
    is present so a test can assert a fake conforms; it has only methods, so ``isinstance``
    is meaningful here.
    """

    def initialize(
        self,
        *,
        path: str | None = ...,
        login: int | None = ...,
        password: str | None = ...,
        server: str | None = ...,
        timeout: int | None = ...,
        portable: bool = ...,
    ) -> bool: ...

    def shutdown(self) -> None: ...

    def last_error(self) -> tuple[int, str]: ...

    def version(self) -> tuple[int, int]: ...

    def terminal_info(self) -> Any: ...

    def account_info(self) -> Any: ...

    def symbol_info(self, name: str) -> Any: ...

    def symbol_select(self, name: str, select: bool) -> bool: ...

    def symbol_info_tick(self, name: str) -> Any: ...

    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start_pos: int, count: int
    ) -> RatesTable | None: ...

    def copy_rates_range(
        self, symbol: str, timeframe: int, start: int, count: int
    ) -> RatesTable | None: ...

    def time_current(self) -> int: ...

    # --- execution surface -----------------------------------------------
    #
    # Phase 5 added these. They are declared here rather than in the execution layer for
    # one reason: this module is *the* description of the dependency on the terminal, and a
    # second, private description in execution/mt5_broker.py would be a second thing to
    # keep honest. Every call below goes through a keyword argument because that is how the
    # real package takes them, so a positional mistake fails type checking here.

    def order_send(self, request: dict[str, Any]) -> Any:
        """Send a trade request. Returns a result row carrying ``retcode``."""

    def order_get(self, *, ticket: int) -> Any:
        """One working order by ticket, or ``None``."""

    def orders_get(self, *, symbol: str | None = ...) -> Any:
        """Working orders, optionally for one symbol."""

    def positions_get(
        self, *, symbol: str | None = ..., ticket: int | None = ...
    ) -> Any:
        """Open positions, optionally filtered by symbol and/or ticket."""


# =============================================================================
# Loading
# =============================================================================


class MT5Module:
    """Lazy holder for the terminal package.

    Constructing this object is free and touches nothing. The import happens on the first
    :meth:`api` access, which is what allows the whole project -- and its entire test suite
    -- to run on a machine with no broker software installed.

    :raises BrokerNotConnectedError: from :meth:`api` when the package is not installed.
        The message names the extra, because "no module named MetaTrader5" is not an
        actionable message for an operator.
    """

    __slots__ = ("_module",)

    def __init__(self) -> None:
        self._module: ModuleType | None = None

    @property
    def loaded(self) -> bool:
        """Whether the package has been imported yet. Says nothing about a terminal."""
        return self._module is not None

    def api(self) -> MT5Api:
        """Import and return the terminal package.

        Deliberately a function body rather than a module-level import: the architecture
        check requires ``MetaTrader5`` to appear only inside a function, so that importing
        this project can never require a terminal.
        """
        if self._module is None:
            self._module = self._load()
        module: MT5Api = self._module
        return module

    @staticmethod
    def _load() -> ModuleType:
        try:
            import MetaTrader5
        except ImportError as exc:
            raise BrokerNotConnectedError(
                "the MetaTrader5 package is not installed; install the broker-supplied "
                'package with: pip install -e ".[mt5]"'
            ) from exc
        found: ModuleType = MetaTrader5
        return found

    def describe_last_error(self) -> tuple[int, str]:
        """``(code, message)`` from the terminal, or a synthetic one if not loaded.

        Returning a synthetic pair rather than raising lets ``test-connection`` report
        "not installed" through the same shape as "terminal refused", which is what an
        operator actually wants to see.
        """
        if self._module is None:
            return (_MT5_INTERNAL_ERROR, "MetaTrader5 is not installed or not yet imported")
        code, message = self._module.last_error()
        return int(code), str(message)

    def __repr__(self) -> str:
        return f"MT5Module(loaded={self.loaded})"


# =============================================================================
# Pure converters -- no I/O, no clock, fully testable
# =============================================================================


def field(source: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a namedtuple, dataclass, mapping or object.

    The terminal returns namedtuples; a fake in a test is more naturally a dict or a
    dataclass. Supporting all four here means the converters need no branching and the
    fakes stay readable.
    """
    if isinstance(source, dict):
        return source.get(name, default)
    if hasattr(source, name):
        return getattr(source, name)
    try:
        return source[name]
    except (TypeError, KeyError, IndexError):
        return default


def epoch_to_datetime(seconds: int | float) -> datetime:
    """Terminal epoch seconds to a timezone-aware UTC datetime.

    MetaTrader 5 reports bar and tick times as seconds since the Unix epoch. Those
    instants are absolute, so UTC is the correct frame and no broker offset is applied
    here. Broker *server* time matters for deciding which bar has closed, and that is a
    separate question answered with :class:`ServerClock` in
    :mod:`stop_order_scalp.market_data.mt5_feed`.
    """
    return datetime.fromtimestamp(float(seconds), tz=UTC)


def row_to_candle(
    row: RatesRow,
    *,
    timeframe: str | int,
    digits: int,
    is_confirmed: bool = True,
) -> Candle:
    """One bar row to a :class:`~stop_order_scalp.domain.models.Candle`.

    :param digits: the symbol's price precision, taken from the specification rather than
        guessed, so a price is never rendered at the wrong scale.
    """
    name = canonical_name(timeframe)
    price_of = _price_reader(digits)
    return Candle(
        open_time=epoch_to_datetime(_require_time(row)),
        open=price_of(row, "open"),
        high=price_of(row, "high"),
        low=price_of(row, "low"),
        close=price_of(row, "close"),
        timeframe_seconds=period_seconds(name),
        timeframe=name,
        volume=Decimal(str(field(row, "tick_volume", 0) or 0)),
        tick_volume=int(field(row, "tick_volume", 0) or 0),
        is_confirmed=is_confirmed,
    )


def _require_time(row: RatesRow) -> int | float:
    value = field(row, "time")
    if value is None:
        raise MarketDataError("bar row has no 'time' field")
    return value  # type: ignore[no-any-return]


def _price_reader(digits: int) -> Any:
    def read(row: RatesRow, key: str) -> Price:
        value = field(row, key)
        if value is None:
            raise MarketDataError(f"bar row has no {key!r} field")
        return Price.parse(str(value), digits)

    return read


def tick_info_to_tick(info: Any, *, digits: int) -> Tick:
    """A ``symbol_info_tick`` row to a :class:`~stop_order_scalp.domain.models.Tick`.

    Bid and ask are both required. Collapsing them to a mid price is how a backtest stops
    matching live fills, so a missing side is an error rather than a default.
    """
    bid = field(info, "bid")
    ask = field(info, "ask")
    if bid is None or ask is None:
        raise MarketDataError("tick row is missing bid or ask; a mid price is not acceptable")
    moment = field(info, "time")
    return Tick(
        moment=epoch_to_datetime(moment) if moment is not None else datetime.fromtimestamp(0, tz=UTC),
        bid=Price.parse(str(bid), digits),
        ask=Price.parse(str(ask), digits),
        volume=Decimal(str(field(info, "last_volume", 0) or 0)),
    )


def symbol_info_to_specification(info: Any) -> SymbolSpecification:
    """A ``symbol_info`` row to a :class:`~stop_order_scalp.domain.value_objects.SymbolSpecification`.

    The broker's own numbers, mapped by name. Anything the broker omits becomes a
    documented default rather than an invented value -- with the exception of the fields
    without which no arithmetic is possible, which are simply absent and therefore rejected
    by ``SymbolSpecification`` itself.
    """
    digits = int(field(info, "digits", 0) or 0)
    name = str(field(info, "name", "") or "")
    try:
        return SymbolSpecification(
            name=name,
            digits=digits,
            point=Decimal(str(_present(info, "point"))),
            tick_size=Decimal(str(field(info, "trade_tick_size") or _present(info, "point"))),
            tick_value=Decimal(str(_present(info, "trade_tick_value"))),
            contract_size=Decimal(str(_present(info, "trade_contract_size"))),
            volume_min=Decimal(str(_present(info, "volume_min"))),
            volume_max=Decimal(str(_present(info, "volume_max"))),
            volume_step=Decimal(str(_present(info, "volume_step"))),
            stops_level=int(field(info, "trade_stops_level", 0) or 0),
            freeze_level=int(field(info, "trade_freeze_level", 0) or 0),
            currency=str(field(info, "currency", "USD") or "USD"),
        )
    except InvalidSpecificationError as exc:
        raise InvalidSpecificationError(f"{name or 'symbol'}: unusable broker specification: {exc}") from exc


def _present(info: Any, name: str) -> Any:
    value = field(info, name)
    if value is None:
        raise InvalidSpecificationError(
            f"the broker did not report {name!r}; without it no point-to-money arithmetic "
            "is possible, so the instrument is refused rather than guessed"
        )
    return value


def account_info_to_snapshot(info: Any) -> AccountSnapshot:
    """An ``account_info`` row to an :class:`~stop_order_scalp.domain.models.AccountSnapshot`."""
    currency = str(field(info, "currency", "USD") or "USD")
    money = _money_reader(currency)
    return AccountSnapshot(
        login=int(field(info, "login", 0) or 0),
        server=str(field(info, "server", "") or ""),
        currency=currency,
        balance=money(field(info, "balance", 0) or 0),
        equity=money(field(info, "equity", 0) or 0),
        margin_used=money(field(info, "margin", 0) or 0),
        margin_free=money(field(info, "margin_free", 0) or 0),
        leverage=int(field(info, "leverage", 100) or 100),
        is_demo=_is_demo(info),
        trade_allowed=bool(field(info, "trade_allowed", True)),
    )


def _money_reader(currency: str) -> Any:
    def read(value: Any) -> Money:
        return Money(Decimal(str(value)), currency)

    return read


def _is_demo(info: Any) -> bool:
    """Whether this is a demo account.

    Read from the terminal's own ``trade_mode`` where present, and **defaulted to True**.
    The default is the safe direction: a misdetected live account must fail closed, and
    nothing in this project is allowed to trade live without three independent opt-ins
    anyway.
    """
    mode = field(info, "trade_mode")
    if mode is None:
        return True
    # MetaTrader5: ACCOUNT_TRADE_MODE_DEMO = 0, ACCOUNT_TRADE_MODE_CONTEST = 1,
    # ACCOUNT_TRADE_MODE_REAL = 2.
    return int(mode) in (0, 1)


def as_sequence(table: RatesTable | None) -> Sequence[RatesRow]:
    """Normalise a possibly-``None`` rates result to a sequence.

    The terminal returns ``None`` on failure rather than raising, and an empty array when
    there is simply no data. Both must be distinguishable from a real error, which is why
    the caller checks :meth:`MT5Api.last_error` rather than inferring from emptiness.
    """
    if table is None:
        return ()
    return list(table)
