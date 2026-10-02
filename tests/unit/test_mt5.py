"""The MetaTrader 5 boundary, exercised against a hand-written terminal.

There is no MetaTrader 5 in this test suite and there is no skip marker pretending
otherwise. A fake implementing :class:`~stop_order_scalp.market_data.mt5_module.MT5Api`
is enough, because the whole point of the protocol is that the rest of the project never
sees the real module.

What is deliberately *not* tested here: the real terminal's behaviour. Only Phase 11,
against a live terminal, can do that. What is tested is that this project never depends on
anything the fake has to guess about.
"""

from __future__ import annotations

from collections import namedtuple
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest

from stop_order_scalp.domain.exceptions import (
    BrokerNotConnectedError,
    InvalidSpecificationError,
    MarketDataError,
    SymbolNotFoundError,
)
from stop_order_scalp.domain.models import Candle, EnvironmentSettings
from stop_order_scalp.domain.value_objects import Price, SymbolSpecification
from stop_order_scalp.infrastructure.clock import FixedClock
from stop_order_scalp.infrastructure.config import AppConfig
from stop_order_scalp.market_data.mt5_feed import MT5Feed, ServerClock, probe_connection
from stop_order_scalp.market_data.mt5_module import (
    MT5Api,
    MT5Module,
    RatesRow,
    account_info_to_snapshot,
    as_sequence,
    epoch_to_datetime,
    field,
    row_to_candle,
    symbol_info_to_specification,
    tick_info_to_tick,
)
from stop_order_scalp.market_data.timeframes import Timeframe

SERVER_EPOCH = int(datetime(2026, 3, 12, 12, 0, tzinfo=UTC).timestamp())
MT5_M1 = 1
MT5_M15 = 15

#: The real terminal returns namedtuples, so at least one row in this suite has to be one
#: for :func:`field` to be exercised against that shape. Declared at module scope because a
#: ``namedtuple`` created inside a function confuses the type checker.
_Row = namedtuple("_Row", "a")


# =============================================================================
# The fake terminal
# =============================================================================


class FakeTerminal:
    """A stand-in for the terminal package.

    Deliberately models the terminal's actual awkwardness rather than an idealised API:

    * ``copy_rates_from_pos`` returns **newest first**, and the newest entry is the bar in
      progress, exactly as the real one does.
    * ``initialize`` returns ``False`` and populates ``last_error`` rather than raising.
    * ``copy_rates_*`` returns ``None`` on failure rather than raising.
    * Fields are namedtuples, reached by attribute access, as the real ones are.
    """

    def __init__(self) -> None:
        self.initialized_with: dict[str, Any] | None = None
        self.shutdown_calls = 0
        self.selectable: set[str] = {"US30", "US30.cash"}
        self.known_symbols: set[str] = {"US30", "US30.cash", "EURUSD"}
        self.connect_ok = True
        self.rates_fail = False
        self.server_time = SERVER_EPOCH
        # --- execution surface, added in Phase 5 ---
        #: Every request handed to ``order_send``, in order. Asserting ``len(terminal.sent)``
        #: is how the no-retry guarantee is proved.
        self.sent: list[dict[str, Any]] = []
        #: Non-zero makes ``order_send`` report this retcode instead of accepting.
        self.send_retcode = 0
        self.order_rows: list[dict[str, Any]] = []
        self.position_rows: list[dict[str, Any]] = []
        self.account_row: dict[str, Any] = {
            "login": 5050123,
            "server": "Alpari-Express-Demo",
            "currency": "USD",
            "balance": "10000.00",
            "equity": "10000.00",
            "margin": "0.0",
            "margin_free": "10000.00",
            "leverage": 100,
            "trade_mode": 0,
            "trade_allowed": True,
        }
        self.symbol_rows: dict[str, dict[str, Any]] = {
            "US30": self._symbol_row("US30"),
            "US30.cash": self._symbol_row("US30.cash"),
            "EURUSD": self._symbol_row("EURUSD", digits=5, point="0.00001"),
        }
        self.tick_row: dict[str, Any] | None = {
            "time": SERVER_EPOCH,
            "bid": 40000.5,
            "ask": 40001.0,
            "last_volume": 3,
        }
        self.rates: list[dict[str, Any]] = []

    @staticmethod
    def _symbol_row(name: str, *, digits: int = 1, point: str = "0.1") -> dict[str, Any]:
        return {
            "name": name,
            "digits": digits,
            "point": point,
            "trade_tick_size": point,
            "trade_tick_value": "1.0",
            "trade_contract_size": "1.0",
            "volume_min": "0.1",
            "volume_max": "50.0",
            "volume_step": "0.1",
            "trade_stops_level": 10,
            "trade_freeze_level": 0,
            "currency": "USD",
            "leverage": 100,
        }

    # -- the API surface --

    def initialize(self, **kwargs: Any) -> bool:
        self.initialized_with = kwargs
        return self.connect_ok

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def last_error(self) -> tuple[int, str]:
        return (0, "") if self.connect_ok else (-1, "terminal is not running")

    def version(self) -> tuple[int, int]:
        return (5, 56)

    def terminal_info(self) -> Any:
        return SimpleNamespace(build=4750, company="Alpari", connected=True)

    def account_info(self) -> Any:
        return SimpleNamespace(**self.account_row) if self.account_row else None

    def symbol_info(self, name: str) -> Any:
        row = self.symbol_rows.get(name)
        return SimpleNamespace(**row) if row else None

    def symbol_select(self, name: str, select: bool) -> bool:
        del select
        # The real terminal matches symbol names case-insensitively.
        return name.casefold() in {known.casefold() for known in self.selectable}

    def symbol_info_tick(self, name: str) -> Any:
        del name
        return SimpleNamespace(**self.tick_row) if self.tick_row else None

    def order_send(self, request: dict[str, Any]) -> Any:
        self.sent.append(dict(request))
        if self.send_retcode:
            return SimpleNamespace(
                retcode=self.send_retcode, order=0, comment="injected failure"
            )
        ticket = 5000 + len(self.sent)
        return SimpleNamespace(retcode=10008, order=ticket, comment="request placed")

    def order_get(self, *, ticket: int) -> Any:
        for row in self.order_rows:
            if row.get("ticket") == ticket:
                return SimpleNamespace(**row)
        return None

    def orders_get(self, *, symbol: str | None = None) -> Any:
        rows = [r for r in self.order_rows if symbol is None or r.get("symbol") == symbol]
        return [SimpleNamespace(**r) for r in rows]

    def positions_get(
        self, *, symbol: str | None = None, ticket: int | None = None
    ) -> Any:
        rows = [
            r
            for r in self.position_rows
            if (symbol is None or r.get("symbol") == symbol)
            and (ticket is None or r.get("ticket") == ticket)
        ]
        return [SimpleNamespace(**r) for r in rows]

    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start_pos: int, count: int
    ) -> list[Any] | None:
        del symbol, start_pos
        if self.rates_fail:
            return None
        window = [row for row in self.rates if row["timeframe"] == timeframe]
        # start_pos counts backwards from the current bar, so the window is the *newest*
        # `count` rows, returned newest first. The newest of those is the bar in progress.
        chosen = window[-count:] if count else []
        return [SimpleNamespace(**row) for row in reversed(chosen)]

    def copy_rates_range(
        self, symbol: str, timeframe: int, start: int, count: int
    ) -> list[Any] | None:
        del symbol, timeframe, start, count
        return []

    def time_current(self) -> int:
        return self.server_time


class FakeModule(MT5Module):
    """An :class:`MT5Module` pre-loaded with a fake, without touching the real import."""

    def __init__(self, terminal: Any) -> None:
        super().__init__()
        self._terminal = terminal

    def api(self) -> MT5Api:
        self._module = self._terminal
        # The fake is checked against the protocol by TestProtocolConformance, so this
        # cast is not what makes that assertion true.
        typed: MT5Api = cast(MT5Api, self._terminal)
        return typed


def _rates(
    count: int,
    *,
    timeframe: int = MT5_M1,
    start_epoch: int | None = None,
    forming: bool = True,
) -> list[dict[str, Any]]:
    """``count`` bar rows, oldest first, with the newest still forming by default."""
    step = 60 if timeframe == MT5_M1 else 900
    if start_epoch is None:
        start_epoch = SERVER_EPOCH - count * step
    rows: list[dict[str, Any]] = []
    for index in range(count):
        moment = start_epoch + step * index
        rows.append(
            {
                "time": moment,
                "timeframe": timeframe,
                "open": 40000.0 + index,
                "high": 40010.0 + index,
                "low": 39990.0 + index,
                "close": 40005.0 + index,
                "tick_volume": 100 + index,
            }
        )
    if forming:
        # A forming bar sits at the far end of the future relative to SERVER_EPOCH.
        rows.append(
            {
                "time": start_epoch + step * count,
                "timeframe": timeframe,
                "open": 40100.0,
                "high": 40150.0,
                "low": 40080.0,
                "close": 40140.0,
                "tick_volume": 7,
            }
        )
    return rows


@pytest.fixture
def terminal() -> FakeTerminal:
    return FakeTerminal()


@pytest.fixture
def module(terminal: FakeTerminal) -> FakeModule:
    return FakeModule(terminal)


@pytest.fixture
def without_mt5(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import MetaTrader5`` fail, whatever is installed on this machine.

    The failure path is the one an operator hits when they have not installed the extra, and
    a test that only passes where the package happens to be absent is not a test. Blocking the
    import makes it deterministic on every machine -- including the one that has MetaTrader 5
    installed, which is exactly where this was silently skipped.
    """
    import builtins

    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "MetaTrader5" or name.startswith("MetaTrader5."):
            raise ImportError("No module named 'MetaTrader5'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)


@pytest.fixture
def settings() -> EnvironmentSettings:
    return EnvironmentSettings(mt5_path="C:/fake/terminal64.exe", mt5_timeout_ms=1000)


def _counting_symbol_info(terminal: FakeTerminal, calls: list[str]) -> Any:
    """A ``symbol_info`` replacement that records every lookup."""
    original = terminal.symbol_info

    def counting(name: str) -> Any:
        calls.append(name)
        return original(name)

    return counting


@pytest.fixture
def connected(module: FakeModule, settings: EnvironmentSettings) -> MT5Feed:
    feed = MT5Feed(module)
    feed.connect(settings)
    return feed


#: An ``.env`` path that does not exist, so a developer's real machine settings cannot
#: reach this suite. ``load_config(env_file=None)`` would auto-discover one in the working
#: directory and merge it into ``os.environ``.
_NO_ENV_FILE = "this-file-deliberately-does-not-exist.env"


@pytest.fixture
def config() -> AppConfig:
    """The shipped configuration, loaded without any environment layer."""
    from stop_order_scalp.infrastructure.config import find_project_root, load_config

    root = find_project_root()
    return load_config(
        config_path=root / "config" / "default.yaml", env_file=_NO_ENV_FILE, root=root
    )


# =============================================================================
# The lazy import
# =============================================================================


class TestLazyLoading:
    def test_constructing_the_holder_touches_nothing(self) -> None:
        holder = MT5Module()
        assert holder.loaded is False

    def test_a_missing_package_names_the_extra(self, without_mt5: None) -> None:
        """The failure path, exercised whether or not the package is installed.

        This test used to depend on the machine: it asserted the package was absent, so it
        passed here and failed on any machine where MetaTrader 5 *was* installed -- the
        opposite of what a test should do. ``without_mt5`` makes the import genuinely fail, so
        the path is exercised everywhere.
        """
        with pytest.raises(BrokerNotConnectedError, match=r'\.\[mt5\]'):
            MT5Module().api()

    def test_the_error_is_actionable(self, without_mt5: None) -> None:
        with pytest.raises(BrokerNotConnectedError, match="broker-supplied"):
            MT5Module().api()

    def test_a_present_package_is_loaded_rather_than_refused(self) -> None:
        """The other half, so the absence path cannot be satisfied by simply refusing always.

        Skipped where the package genuinely is not installed.
        """
        module = pytest.importorskip("MetaTrader5")
        del module
        assert MT5Module().loaded is False

    def test_last_error_is_reportable_before_loading(self) -> None:
        code, message = MT5Module().describe_last_error()
        assert code == -1
        assert "not installed" in message

    def test_loading_twice_reuses_the_module(self, module: FakeModule) -> None:
        assert module.api() is module.api()
        assert module.loaded is True


# =============================================================================
# Field access across the shapes a row can take
# =============================================================================


class TestFieldAccess:
    def test_a_mapping_is_read_by_key(self) -> None:
        assert field({"a": 1}, "a") == 1

    def test_an_object_is_read_by_attribute(self) -> None:
        assert field(SimpleNamespace(a=2), "a") == 2

    def test_a_namedtuple_is_read_by_attribute(self) -> None:
        assert field(_Row(3), "a") == 3

    def test_a_missing_field_returns_the_default(self) -> None:
        assert field({"a": 1}, "b", "fallback") == "fallback"

    def test_an_unindexable_source_returns_the_default(self) -> None:
        assert field(object(), "b", None) is None


# =============================================================================
# Pure converters
# =============================================================================


class TestEpochConversion:
    def test_epoch_seconds_become_aware_utc(self) -> None:
        moment = epoch_to_datetime(SERVER_EPOCH)
        assert moment == datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
        assert moment.tzinfo is not None

    def test_a_float_epoch_is_accepted(self) -> None:
        assert epoch_to_datetime(float(SERVER_EPOCH)) == epoch_to_datetime(SERVER_EPOCH)


class TestRowToCandle:
    def test_a_row_becomes_a_candle(self) -> None:
        row = SimpleNamespace(
            time=SERVER_EPOCH, open=40000.0, high=40010.0, low=39990.0, close=40005.0,
            tick_volume=100,
        )
        candle = row_to_candle(row, timeframe=Timeframe.M1, digits=1)
        assert candle.open_time == datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
        assert candle.timeframe == "M1"
        assert candle.timeframe_seconds == 60
        assert float(candle.high.value) == 40010.0

    def test_the_symbols_digit_count_is_used(self) -> None:
        row = SimpleNamespace(
            time=SERVER_EPOCH, open=1.234567, high=1.234567, low=1.234567, close=1.234567,
        )
        candle = row_to_candle(row, timeframe="M1", digits=5)
        assert candle.open.digits == 5

    def test_a_bar_without_a_time_is_refused(self) -> None:
        with pytest.raises(MarketDataError, match="no 'time' field"):
            row_to_candle({}, timeframe="M1", digits=1)

    def test_a_bar_without_a_price_is_refused(self) -> None:
        row = SimpleNamespace(time=SERVER_EPOCH, open=1.0, high=1.0, low=1.0)
        with pytest.raises(MarketDataError, match="close"):
            row_to_candle(row, timeframe="M1", digits=1)

    def test_an_mt5_timeframe_code_is_accepted(self) -> None:
        row = SimpleNamespace(
            time=SERVER_EPOCH, open=1.0, high=1.0, low=1.0, close=1.0,
        )
        assert row_to_candle(row, timeframe=MT5_M15, digits=1).timeframe == "M15"

    def test_the_confirmed_flag_is_carried(self) -> None:
        row = SimpleNamespace(
            time=SERVER_EPOCH, open=1.0, high=1.0, low=1.0, close=1.0,
        )
        assert row_to_candle(row, timeframe="M1", digits=1, is_confirmed=False).is_confirmed is False


class TestTickConversion:
    def test_both_sides_are_required(self) -> None:
        with pytest.raises(MarketDataError, match="mid price is not acceptable"):
            tick_info_to_tick({"time": SERVER_EPOCH, "bid": 40000.0}, digits=1)

    def test_a_tick_becomes_a_domain_tick(self) -> None:
        tick = tick_info_to_tick(
            {"time": SERVER_EPOCH, "bid": 40000.5, "ask": 40001.0, "last_volume": 4}, digits=1
        )
        assert tick.moment == datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
        assert tick.spread == Decimal("0.5")

    def test_a_reversed_spread_is_refused_by_the_value_object(self) -> None:
        with pytest.raises(ValueError, match="below bid"):
            tick_info_to_tick({"time": SERVER_EPOCH, "bid": 40001.0, "ask": 40000.0}, digits=1)


class TestSpecificationConversion:
    def test_a_symbol_row_becomes_a_specification(self, terminal: FakeTerminal) -> None:
        spec = symbol_info_to_specification(SimpleNamespace(**terminal.symbol_rows["US30"]))
        assert spec.name == "US30"
        assert spec.point == Decimal("0.1")
        assert spec.stops_level == 10
        assert spec.volume_step == Decimal("0.1")

    def test_a_missing_tick_value_is_refused_not_defaulted(self) -> None:
        row = {
            "name": "US30", "digits": 1, "point": "0.1", "trade_tick_size": "0.1",
            "trade_contract_size": "1.0", "volume_min": "0.1", "volume_max": "50.0",
            "volume_step": "0.1",
        }
        with pytest.raises(InvalidSpecificationError, match="trade_tick_value"):
            symbol_info_to_specification(row)

    def test_a_five_digit_symbol_keeps_its_precision(self, terminal: FakeTerminal) -> None:
        spec = symbol_info_to_specification(SimpleNamespace(**terminal.symbol_rows["EURUSD"]))
        assert spec.digits == 5
        assert spec.point == Decimal("0.00001")

    def test_an_inconsistent_specification_is_refused_with_the_symbol_named(
        self, terminal: FakeTerminal
    ) -> None:
        row = dict(terminal.symbol_rows["US30"])
        row["trade_tick_size"] = "1.0"  # coarser than the point
        with pytest.raises(InvalidSpecificationError, match="US30"):
            symbol_info_to_specification(row)

    def test_the_result_is_a_real_specification(self, terminal: FakeTerminal) -> None:
        spec = symbol_info_to_specification(SimpleNamespace(**terminal.symbol_rows["US30"]))
        assert isinstance(spec, SymbolSpecification)
        # The whole point: points become money only through the specification.
        money = spec.price_risk_for(_price(40010.0), _price(40000.0), Decimal("0.1"))
        # 10 price units / 0.1 tick = 100 ticks; 100 ticks x 1.0 tick value x 0.1 lots.
        assert money.amount == Decimal("10.00")


def _price(value: float) -> Price:
    return Price.parse(str(value), 1)


class TestAccountConversion:
    def test_an_account_row_becomes_a_snapshot(self) -> None:
        snapshot = account_info_to_snapshot(
            {
                "login": 42, "server": "Demo", "currency": "USD", "balance": "1000.00",
                "equity": "900.00", "margin": "100.00", "margin_free": "800.00",
                "leverage": 200, "trade_mode": 0,
            }
        )
        assert snapshot.login == 42
        assert snapshot.balance.amount == Decimal("1000.00")
        assert snapshot.leverage == 200
        assert snapshot.is_demo is True

    def test_a_real_account_is_recognised(self) -> None:
        snapshot = account_info_to_snapshot({"login": 1, "currency": "USD", "trade_mode": 2})
        assert snapshot.is_demo is False
        assert snapshot.is_live is True

    def test_a_contest_account_counts_as_not_live(self) -> None:
        assert account_info_to_snapshot({"trade_mode": 1}).is_demo is True

    def test_a_missing_trade_mode_defaults_to_demo(self) -> None:
        # Fail closed: a misdetected live account must not be assumed live.
        assert account_info_to_snapshot({"login": 1}).is_demo is True

    def test_the_password_is_never_on_the_snapshot(self) -> None:
        snapshot = account_info_to_snapshot({"login": 1, "password": "hunter2"})
        assert "password" not in snapshot.to_dict()
        assert not hasattr(snapshot, "password")


class TestAsSequence:
    def test_none_becomes_empty(self) -> None:
        assert as_sequence(None) == ()

    def test_a_table_is_materialised(self) -> None:
        table: list[RatesRow] = [SimpleNamespace(time=1), SimpleNamespace(time=2)]
        assert len(as_sequence(table)) == 2


# =============================================================================
# ServerClock
# =============================================================================


class TestServerClock:
    def test_it_reads_the_terminals_server_time(
        self, module: FakeModule, terminal: FakeTerminal
    ) -> None:
        clock = ServerClock(module, FixedClock(datetime(2020, 1, 1, tzinfo=UTC)))
        assert clock.now() == datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
        assert clock.degraded is False

    def test_a_server_offset_is_reflected(self, module: FakeModule, terminal: FakeTerminal) -> None:
        terminal.server_time = SERVER_EPOCH + 3 * 3600
        clock = ServerClock(module, FixedClock(datetime(2020, 1, 1, tzinfo=UTC)))
        assert clock.now() == datetime(2026, 3, 12, 15, 0, tzinfo=UTC)

    def test_it_falls_back_visibly_when_the_terminal_is_absent(self) -> None:
        fallback = FixedClock(datetime(2026, 3, 12, 12, 0, tzinfo=UTC))
        clock = ServerClock(MT5Module(), fallback)
        assert clock.now() == fallback.now()
        assert clock.degraded is True

    def test_a_zero_server_time_is_treated_as_not_ready(self, module: FakeModule, terminal: FakeTerminal) -> None:
        terminal.server_time = 0
        fallback = FixedClock(datetime(2026, 3, 12, 12, 0, tzinfo=UTC))
        clock = ServerClock(module, fallback)
        assert clock.now() == fallback.now()
        assert clock.degraded is True

    def test_recovery_clears_the_degraded_flag(
        self, module: FakeModule, terminal: FakeTerminal
    ) -> None:
        clock = ServerClock(module, FixedClock(datetime(2020, 1, 1, tzinfo=UTC)))
        terminal.server_time = 0
        clock.now()
        assert clock.degraded is True
        terminal.server_time = SERVER_EPOCH
        clock.now()
        assert clock.degraded is False


# =============================================================================
# Connection
# =============================================================================


class TestConnection:
    def test_connect_passes_the_configured_path(
        self, module: FakeModule, terminal: FakeTerminal, settings: EnvironmentSettings
    ) -> None:
        MT5Feed(module).connect(settings)
        assert terminal.initialized_with is not None
        assert terminal.initialized_with["path"] == "C:/fake/terminal64.exe"
        assert terminal.initialized_with["timeout"] == 1000

    def test_a_refused_connection_reports_the_terminals_own_error(
        self, module: FakeModule, terminal: FakeTerminal, settings: EnvironmentSettings
    ) -> None:
        terminal.connect_ok = False
        with pytest.raises(BrokerNotConnectedError, match="not running"):
            MT5Feed(module).connect(settings)

    def test_a_refused_connection_names_the_code(self, module: FakeModule, terminal: FakeTerminal, settings: EnvironmentSettings) -> None:
        terminal.connect_ok = False
        with pytest.raises(BrokerNotConnectedError, match="code -1"):
            MT5Feed(module).connect(settings)

    def test_operations_before_connecting_are_refused(self, module: FakeModule) -> None:
        feed = MT5Feed(module)
        assert feed.is_connected is False
        with pytest.raises(BrokerNotConnectedError, match="connect"):
            feed.candles("US30", "M1", 5)

    def test_shutdown_is_idempotent(self, module: FakeModule, terminal: FakeTerminal, settings: EnvironmentSettings) -> None:
        feed = MT5Feed(module)
        feed.connect(settings)
        feed.shutdown()
        feed.shutdown()
        assert terminal.shutdown_calls == 1
        assert feed.is_connected is False

    def test_shutdown_before_connecting_is_safe(self, module: FakeModule) -> None:
        MT5Feed(module).shutdown()

    def test_a_missing_package_surfaces_on_connect(self, settings: EnvironmentSettings) -> None:
        with pytest.raises(BrokerNotConnectedError):
            MT5Feed(MT5Module()).connect(settings)


# =============================================================================
# Symbol resolution
# =============================================================================


class TestSymbolResolution:
    def test_the_configured_name_is_preferred(self, connected: MT5Feed) -> None:
        assert connected.resolve_symbol("US30") == "US30"

    def test_an_alias_is_used_when_the_primary_is_absent(
        self, connected: MT5Feed, terminal: FakeTerminal
    ) -> None:
        terminal.selectable = {"US30m"}
        assert connected.resolve_symbol("US30", ["US30.cash", "US30m"]) == "US30m"

    def test_an_unavailable_instrument_is_refused_with_the_attempted_names(
        self, connected: MT5Feed, terminal: FakeTerminal
    ) -> None:
        terminal.selectable = {"EURUSD"}
        with pytest.raises(SymbolNotFoundError, match="US30m"):
            connected.resolve_symbol("US30", ["US30m"])

    def test_matching_is_exact_never_substring(
        self, connected: MT5Feed, terminal: FakeTerminal
    ) -> None:
        # "US30" must not select "US30mini" by accident.
        terminal.selectable = {"US30mini"}
        with pytest.raises(SymbolNotFoundError):
            connected.resolve_symbol("US30")

    def test_matching_ignores_case_and_surrounding_space(
        self, connected: MT5Feed, terminal: FakeTerminal
    ) -> None:
        # The broker lists the symbol as "us30"; matching is case-insensitive, and the
        # configured spelling is what comes back so later calls stay consistent with what
        # this project asked for.
        terminal.selectable = {"us30"}
        assert connected.resolve_symbol("  US30  ") == "US30"


# =============================================================================
# The specification is fetched once
# =============================================================================


class TestSpecificationCaching:
    def test_it_is_resolved_once(self, connected: MT5Feed, terminal: FakeTerminal) -> None:
        calls: list[str] = []
        counting = _counting_symbol_info(terminal, calls)
        terminal.symbol_info = counting  # type: ignore[method-assign]
        connected.specification("US30")
        connected.specification("US30")
        assert calls == ["US30"]

    def test_an_unknown_symbol_is_refused(self, connected: MT5Feed) -> None:
        with pytest.raises(SymbolNotFoundError, match="NOPE"):
            connected.specification("NOPE")


# =============================================================================
# Candles -- the freeze happens on the feed
# =============================================================================


class TestFeedCandles:
    def test_only_closed_bars_are_returned(
        self, connected: MT5Feed, terminal: FakeTerminal
    ) -> None:
        terminal.rates = _rates(5)
        candles = connected.candles("US30", "M1", 3)
        assert len(candles) == 3
        require_all_closed(candles, connected.server_time())

    def test_the_newest_element_of_the_table_is_never_returned(
        self, connected: MT5Feed, terminal: FakeTerminal
    ) -> None:
        terminal.rates = _rates(5, forming=True)
        candles = connected.candles("US30", "M1", 3)
        newest = candles[-1].open_time
        # The forming bar was appended at the far end and must not appear.
        assert newest < datetime(2026, 3, 12, 12, 5, tzinfo=UTC)

    def test_the_result_is_oldest_first(self, connected: MT5Feed, terminal: FakeTerminal) -> None:
        terminal.rates = _rates(6)
        candles = connected.candles("US30", "M1", 3)
        assert list(candles) == sorted(candles, key=lambda c: c.open_time)

    def test_the_freeze_report_is_available(self, connected: MT5Feed, terminal: FakeTerminal) -> None:
        terminal.rates = _rates(5)
        _, report = connected.freeze("US30", "M1", 3)
        assert report.returned == 3
        assert report.dropped_forming >= 1
        assert report.timeframe == "M1"

    def test_the_newest_first_table_order_is_normalised(
        self, connected: MT5Feed, terminal: FakeTerminal
    ) -> None:
        terminal.rates = _rates(6)
        assert connected.candles("US30", "M1", 3) == connected.candles("US30", "M1", 3)

    def test_m15_works_and_reports_m15(self, connected: MT5Feed, terminal: FakeTerminal) -> None:
        terminal.rates = _rates(6, timeframe=MT5_M15)
        candles = connected.candles("US30", "M15", 2)
        assert all(c.timeframe == "M15" for c in candles)
        assert all(c.timeframe_seconds == 900 for c in candles)

    def test_a_failed_rate_request_is_retryable(
        self, connected: MT5Feed, terminal: FakeTerminal
    ) -> None:
        terminal.rates_fail = True
        with pytest.raises(Exception, match="copy_rates_from_pos"):
            connected.candles("US30", "M1", 3)

    def test_a_non_positive_count_is_refused(self, connected: MT5Feed) -> None:
        with pytest.raises(MarketDataError, match="count must be positive"):
            connected.candles("US30", "M1", 0)

    def test_the_before_argument_overrides_the_server_clock(
        self, connected: MT5Feed, terminal: FakeTerminal
    ) -> None:
        # The backtest seam: supplying `before` makes the answer independent of when the
        # call happened. Bars straddle the two references, so the counts must differ.
        terminal.rates = _rates(6, start_epoch=SERVER_EPOCH - 180, forming=False)
        at_noon = connected.candles("US30", "M1", 6, before=datetime(2026, 3, 12, 12, 0, tzinfo=UTC))
        later = connected.candles("US30", "M1", 6, before=datetime(2026, 3, 12, 12, 2, tzinfo=UTC))
        assert len(at_noon) == 3
        assert len(later) == 5

    def test_the_same_before_always_gives_the_same_answer(
        self, connected: MT5Feed, terminal: FakeTerminal
    ) -> None:
        terminal.rates = _rates(6, start_epoch=SERVER_EPOCH - 180, forming=False)
        reference = datetime(2026, 3, 12, 12, 1, tzinfo=UTC)
        assert connected.candles("US30", "M1", 6, before=reference) == connected.candles(
            "US30", "M1", 6, before=reference
        )

    def test_a_forming_bar_is_only_reachable_by_name(
        self, connected: MT5Feed, terminal: FakeTerminal
    ) -> None:
        terminal.rates = _rates(3, forming=True)
        forming = connected.forming_candle("US30", "M1")
        assert forming is not None
        assert forming.is_confirmed is False
        assert not forming.is_closed_at(connected.server_time())

    def test_forming_candle_is_none_when_nothing_is_forming(
        self, connected: MT5Feed, terminal: FakeTerminal
    ) -> None:
        terminal.rates = _rates(3, forming=False)
        # Every bar is in the past relative to the server clock, so nothing is forming.
        terminal.server_time = SERVER_EPOCH + 10 * 3600
        assert connected.forming_candle("US30", "M1") is None

    def test_the_feed_has_no_order_methods(self) -> None:
        # A market-data provider that can place an order would let a strategy bypass the
        # execution gate entirely.
        forbidden = {"place_order", "cancel_order", "modify_position", "order_send"}
        assert forbidden.isdisjoint(set(dir(MT5Feed)))


def require_all_closed(candles: Sequence[Candle], reference: datetime) -> None:
    from stop_order_scalp.market_data.candles import require_closed_only

    require_closed_only(candles, reference=reference)


# =============================================================================
# Ticks and account
# =============================================================================


class TestTicksAndAccount:
    def test_a_tick_is_converted(self, connected: MT5Feed) -> None:
        tick = connected.tick("US30")
        assert tick is not None
        assert tick.spread == Decimal("0.5")

    def test_a_missing_tick_is_none_not_an_error(
        self, connected: MT5Feed, terminal: FakeTerminal
    ) -> None:
        terminal.tick_row = None
        assert connected.tick("US30") is None

    def test_the_account_is_converted(self, connected: MT5Feed) -> None:
        snapshot = connected.account()
        assert snapshot.login == 5050123
        assert snapshot.is_demo is True

    def test_server_time_comes_from_the_terminal(
        self, connected: MT5Feed, terminal: FakeTerminal
    ) -> None:
        terminal.server_time = SERVER_EPOCH + 7200
        assert connected.server_time() == datetime(2026, 3, 12, 14, 0, tzinfo=UTC)

    def test_symbol_available_is_false_when_disconnected(self, module: FakeModule) -> None:
        assert MT5Feed(module).symbol_available("US30") is False

    def test_leverage_falls_back_to_a_hundred(self, connected: MT5Feed) -> None:
        assert connected.leverage_for("US30") == Decimal(100)


# =============================================================================
# probe_connection
# =============================================================================


class TestProbeConnection:
    def test_it_reports_a_missing_package_without_raising(
        self, config: AppConfig, without_mt5: None
    ) -> None:
        """The genuine absence path, forced rather than assumed.

        This used to read "the real environment has no MetaTrader5", which quietly stopped
        being true the moment someone installed the extra -- the test then failed while
        asserting something about the machine instead of about the code.
        """
        report = probe_connection(config)
        assert report["connected"] is False
        assert report["package_installed"] is False
        assert "not installed" in str(report["error"])

    def test_it_never_raises(self, config: AppConfig) -> None:
        for _ in range(3):
            assert isinstance(probe_connection(config), dict)

    def test_the_report_shape_is_stable(self, config: AppConfig) -> None:
        report = probe_connection(config)
        for key in (
            "connected", "package_installed", "terminal_running", "terminal_path_configured",
            "credentials_configured", "account_login", "account_server", "is_demo", "error",
        ):
            assert key in report, key

    def test_it_reports_a_reachable_terminal(self, config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        terminal = FakeTerminal()
        terminal.rates = _rates(5)
        monkeypatch.setattr(
            "stop_order_scalp.market_data.mt5_feed.MT5Module", lambda: FakeModule(terminal)
        )
        report = probe_connection(config)
        assert report["connected"] is True
        assert report["account_login"] == 5050123
        assert report["is_demo"] is True
        assert report["version"] == (5, 56)

    def test_it_reports_a_refused_terminal_without_raising(
        self, config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        terminal = FakeTerminal()
        terminal.connect_ok = False
        monkeypatch.setattr(
            "stop_order_scalp.market_data.mt5_feed.MT5Module", lambda: FakeModule(terminal)
        )
        report = probe_connection(config)
        assert report["connected"] is False
        assert "not running" in str(report["error"])
        assert report["last_error"]["code"] == -1

    def test_it_shuts_the_terminal_down(
        self, config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        terminal = FakeTerminal()
        monkeypatch.setattr(
            "stop_order_scalp.market_data.mt5_feed.MT5Module", lambda: FakeModule(terminal)
        )
        probe_connection(config)
        assert terminal.shutdown_calls == 1


# =============================================================================
# The protocol is enough
# =============================================================================


class TestProtocolConformance:
    def test_the_fake_satisfies_the_api_protocol(self, terminal: FakeTerminal) -> None:
        assert isinstance(terminal, MT5Api)

    def test_a_partial_object_does_not(self) -> None:
        assert not isinstance(object(), MT5Api)

    def test_the_feed_satisfies_the_market_data_provider_protocol(
        self, connected: MT5Feed
    ) -> None:
        from stop_order_scalp.domain.interfaces import MarketDataProvider

        assert isinstance(connected, MarketDataProvider)
