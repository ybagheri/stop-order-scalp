"""The demo run, driven end to end through the real broker and feed classes.

The terminal here is a fake, and the thing that makes it worth anything is how it is built: it
exposes **only functions the real MetaTrader5 package has**, and its ``order_send`` refuses any
``action`` that is not one of the published constants. The code that was in the repository
before this run existed would fail every test below -- it sent ``action=1`` (``DEAL``) for a
pending order, asked for ``order_get`` and ``time_current``, and sent ``tp=0`` on every trailing
step -- and passed all of its own tests, because those doubles agreed with the code rather than
with the package.

None of this has touched a real terminal. These tests are the reason the first real run should
be an *observing* one.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from stop_order_scalp.application.demo import (
    DemoUnavailable,
    build_demo_service,
    compare_specifications,
)
from stop_order_scalp.infrastructure.config import AppConfig, load_config
from stop_order_scalp.market_data.mt5_module import MT5Module
from stop_order_scalp.market_data.symbols import MEASURED_BITCOIN, us30_specification

REPO = Path(__file__).resolve().parents[2]

# Published MetaTrader5 constants.
ACTION_DEAL, ACTION_PENDING, ACTION_SLTP, ACTION_REMOVE = 1, 5, 6, 8
TYPE_BUY_STOP = 4

#: The "server" clock. Deliberately nowhere near the machine's own UTC: rates carry server time,
#: and a run that quietly consulted the local clock would find every bar in the future or past.
SERVER_NOW = datetime(2026, 10, 3, 14, 7, 30, tzinfo=UTC)


class FakeTerminal:
    """The slice of the ``MetaTrader5`` package the project calls -- and nothing else."""

    TRADE_ACTION_PENDING = ACTION_PENDING
    TRADE_ACTION_SLTP = ACTION_SLTP
    TRADE_ACTION_REMOVE = ACTION_REMOVE

    def __init__(self, *, trade_mode: int = 0, tick_value: str = "0.01") -> None:
        self.trade_mode = trade_mode
        self.tick_value = tick_value
        self.sent: list[dict[str, Any]] = []
        self.order_rows: list[dict[str, Any]] = []
        self.position_rows: list[dict[str, Any]] = []
        self.now = SERVER_NOW
        self.bid = 100_120.0
        self.initialized_with: dict[str, Any] | None = None
        self.m1 = self._bars()
        self._ticket = 9000
        #: When set, a pending order is answered with this retcode and nothing is booked.
        self.pending_retcode: int | None = None
        #: ``order_check`` refuses with this retcode, or cannot evaluate when ``check_none``.
        self.check_retcode = 0
        self.check_none = False
        #: ``order_send`` returns nothing, with ``last_error`` holding the cause.
        self.send_none = False
        self.last_error_pair: tuple[int, str] = (0, "")
        self.checked: list[dict[str, Any]] = []
        self.deal_rows: list[dict[str, Any]] = []

    # --- market ----------------------------------------------------------

    @staticmethod
    def _bars() -> list[dict[str, Any]]:
        """Two hours of rising one-minute bars up to and including the forming 14:07 bar."""
        rows: list[dict[str, Any]] = []
        start = datetime(2026, 10, 3, 12, 8, tzinfo=UTC)
        price = 100_000.0
        for i in range(120):
            opened = start + timedelta(minutes=i)
            rows.append(
                {
                    "time": int(opened.timestamp()),
                    "open": price,
                    "high": price + 1.5,
                    "low": price - 0.5,
                    "close": price + 1.0,
                    "tick_volume": 10,
                    "spread": 10,
                    "real_volume": 0,
                }
            )
            price += 1.0
        return rows

    def _m15(self) -> list[dict[str, Any]]:
        groups: dict[int, list[dict[str, Any]]] = {}
        for row in self.m1:
            groups.setdefault(row["time"] - row["time"] % 900, []).append(row)
        return [
            {
                "time": opened,
                "open": bars[0]["open"],
                "high": max(b["high"] for b in bars),
                "low": min(b["low"] for b in bars),
                "close": bars[-1]["close"],
                "tick_volume": 10 * len(bars),
                "spread": 10,
                "real_volume": 0,
            }
            for opened, bars in sorted(groups.items())
        ]

    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start_pos: int, count: int
    ) -> list[Any]:
        assert symbol == "BITCOIN"
        assert start_pos == 0
        source = self.m1 if timeframe == 1 else self._m15()
        return [SimpleNamespace(**row) for row in reversed(source[-count:])]

    def symbol_info_tick(self, name: str) -> Any:
        assert name == "BITCOIN"
        return SimpleNamespace(
            time=int(self.now.timestamp()), bid=self.bid, ask=self.bid + 1.0, last_volume=1
        )

    def symbol_info(self, name: str) -> Any:
        if name != "BITCOIN":
            return None
        return SimpleNamespace(
            name="BITCOIN",
            digits=2,
            point=0.01,
            trade_tick_size=0.01,
            trade_tick_value=float(self.tick_value),
            trade_contract_size=1.0,
            volume_min=0.01,
            volume_max=300.0,
            volume_step=0.01,
            trade_stops_level=0,
            trade_freeze_level=0,
            filling_mode=1,
            currency="USD",
            leverage=100,
        )

    def symbol_select(self, name: str, select: bool) -> bool:
        del select
        return name == "BITCOIN"

    # --- account and connection -----------------------------------------

    def initialize(self, **kwargs: Any) -> bool:
        self.initialized_with = kwargs
        return True

    def shutdown(self) -> None:
        return None

    def last_error(self) -> tuple[int, str]:
        return self.last_error_pair

    def account_info(self) -> Any:
        return SimpleNamespace(
            login=5050123,
            server="Alpari-MT5-Demo",
            currency="USD",
            balance=10_000.0,
            equity=10_000.0,
            margin=0.0,
            margin_free=10_000.0,
            leverage=100,
            trade_mode=self.trade_mode,
            trade_allowed=True,
        )

    # --- trading ---------------------------------------------------------

    def history_deals_get(self, date_from: Any, date_to: Any) -> Any:
        del date_from, date_to
        return tuple(SimpleNamespace(**r) for r in self.deal_rows)

    def order_check(self, request: dict[str, Any]) -> Any:
        self.checked.append(dict(request))
        if len(request.get("comment", "")) >= 31:
            # Measured on a real terminal: a 31-character comment is refused, not truncated.
            self.last_error_pair = (-2, 'Invalid "comment" argument')
            return None
        if self.check_none:
            return None
        return SimpleNamespace(retcode=self.check_retcode, comment="Done" if not self.check_retcode else "refused")

    def order_send(self, request: dict[str, Any]) -> Any:
        self.sent.append(dict(request))
        if self.send_none:
            return None
        action = request.get("action")
        if action == ACTION_PENDING and self.pending_retcode is not None:
            comments = {10027: "AutoTrading disabled by client", 10030: "Unsupported filling mode"}
            return SimpleNamespace(
                retcode=self.pending_retcode,
                order=0,
                comment=comments.get(self.pending_retcode, "refused"),
            )
        if action == ACTION_PENDING:
            self._ticket += 1
            self.order_rows.append(
                {
                    "ticket": self._ticket,
                    "symbol": request["symbol"],
                    "type": request["type"],
                    "volume_current": request["volume"],
                    "price_open": request["price"],
                    "sl": request["sl"],
                    "tp": request["tp"],
                    "magic": request["magic"],
                    "comment": request["comment"],
                    "time_setup": int(self.now.timestamp()),
                    "time_expiration": 0,
                }
            )
            return SimpleNamespace(retcode=10008, order=self._ticket, comment="placed")
        if action == ACTION_DEAL and "position" in request:
            self.position_rows = [
                r for r in self.position_rows if r["ticket"] != request["position"]
            ]
            return SimpleNamespace(retcode=10009, order=0, comment="closed")
        if action == ACTION_REMOVE:
            self.order_rows = [r for r in self.order_rows if r["ticket"] != request["order"]]
            return SimpleNamespace(retcode=10009, order=request["order"], comment="removed")
        if action == ACTION_SLTP:
            for row in self.position_rows:
                if row["ticket"] == request["position"]:
                    row["sl"], row["tp"] = request["sl"], request["tp"]
            return SimpleNamespace(retcode=10009, order=0, comment="modified")
        return SimpleNamespace(retcode=10013, order=0, comment="invalid request")

    def orders_get(self, *, symbol: str | None = None, ticket: int | None = None) -> Any:
        return tuple(
            SimpleNamespace(**r)
            for r in self.order_rows
            if (symbol is None or r["symbol"] == symbol)
            and (ticket is None or r["ticket"] == ticket)
        )

    def positions_get(self, *, symbol: str | None = None, ticket: int | None = None) -> Any:
        return tuple(
            SimpleNamespace(**r)
            for r in self.position_rows
            if (symbol is None or r["symbol"] == symbol)
            and (ticket is None or r["ticket"] == ticket)
        )

    # --- test controls ---------------------------------------------------

    def fill_resting_order(self) -> None:
        """The venue triggers the resting stop: it becomes a position with the same levels."""
        order = self.order_rows.pop()
        self.position_rows.append(
            {
                "ticket": order["ticket"],
                "symbol": order["symbol"],
                "type": 0,
                "volume": order["volume_current"],
                "price_open": order["price_open"],
                "sl": order["sl"],
                "tp": order["tp"],
                "magic": order["magic"],
                "comment": order["comment"],
                "time": int(self.now.timestamp()),
                "profit": 0.0,
            }
        )

    def next_minute(self) -> None:
        """A new bar closes: the forming bar is finished and a new one begins."""
        last = self.m1[-1]
        opened = datetime.fromtimestamp(last["time"] + 60, UTC)
        self.m1.append(
            {
                "time": int(opened.timestamp()),
                "open": last["close"],
                "high": last["close"] + 1.5,
                "low": last["close"] - 0.5,
                "close": last["close"] + 1.0,
                "tick_volume": 10,
                "spread": 10,
                "real_volume": 0,
            }
        )
        self.now += timedelta(minutes=1)

    def sent_actions(self) -> list[int]:
        return [int(r["action"]) for r in self.sent]


# =============================================================================
# Fixtures
# =============================================================================


def _bitcoin_yaml(tmp_path: Path) -> Path:
    """``config/default.yaml`` re-scaled to a coin worth ~100 000, asserting every edit lands."""
    text = (REPO / "config" / "default.yaml").read_text(encoding="utf-8")
    edits = [
        (r"^symbol: US30$", "symbol: BITCOIN"),
        (r"^symbol_aliases:\n(?:  - .*\n)+", "symbol_aliases:\n  - BITCOIN\n"),
        (r"^  offset_points: 10$", "  offset_points: 500"),
        (r"^  refresh_pending: false$", "  refresh_pending: true"),
        (r"^  commission_per_lot: 6.0$", "  commission_per_lot: 0.0"),
        (r"^  take_profit_points: 1000$", "  take_profit_points: 30000"),
        (r"^  stop_loss_points: 100$", "  stop_loss_points: 10000"),
        (r"^  trigger_points: 50$", "  trigger_points: 5000"),
        (r"^  distance_points: 100$", "  distance_points: 10000"),
        (r"^  min_step_points: 1$", "  min_step_points: 100"),
        (r"^  deviation_points: 20$", "  deviation_points: 200"),
    ]
    for pattern, replacement in edits:
        text, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
        assert count == 1, f"default.yaml no longer matches {pattern!r}; update this helper"
    path = tmp_path / "btc.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def terminal() -> FakeTerminal:
    return FakeTerminal()


@pytest.fixture
def module(terminal: FakeTerminal) -> MT5Module:
    holder = MT5Module()
    holder._module = terminal  # type: ignore[assignment]
    return holder


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    for name in [n for n in __import__("os").environ if n.startswith("SOS_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("SOS_ENVIRONMENT", "DEMO")
    monkeypatch.setenv("SOS_ALLOW_ORDER", "true")
    monkeypatch.setenv("SOS_SYMBOL", "BITCOIN")
    return load_config(config_path=_bitcoin_yaml(tmp_path), root=tmp_path, env_file=tmp_path / "none.env")


def _no_sleep(_seconds: float) -> None:
    return None


# =============================================================================
# Refusals: every precondition, and that each one fails closed
# =============================================================================


class TestItRefusesToStart:
    def test_not_in_the_demo_environment(
        self, config: AppConfig, module: MT5Module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("SOS_ENVIRONMENT", "DRY_RUN")
        cfg = load_config(config_path=_bitcoin_yaml(tmp_path), root=tmp_path, env_file=tmp_path / "x")

        with pytest.raises(DemoUnavailable, match="SOS_ENVIRONMENT"):
            build_demo_service(cfg, module=module)

    def test_with_live_trading_enabled_anywhere(
        self, module: MT5Module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: AppConfig
    ) -> None:
        del config
        monkeypatch.setenv("SOS_ALLOW_LIVE", "true")
        cfg = load_config(config_path=_bitcoin_yaml(tmp_path), root=tmp_path, env_file=tmp_path / "x")

        with pytest.raises(DemoUnavailable, match="SOS_ALLOW_LIVE"):
            build_demo_service(cfg, module=module)

    def test_placing_needs_its_own_environment_switch(
        self, module: MT5Module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: AppConfig
    ) -> None:
        del config
        monkeypatch.setenv("SOS_ALLOW_ORDER", "false")
        cfg = load_config(config_path=_bitcoin_yaml(tmp_path), root=tmp_path, env_file=tmp_path / "x")

        with pytest.raises(DemoUnavailable, match="SOS_ALLOW_ORDER"):
            build_demo_service(cfg, place_orders=True, module=module)

    def test_when_the_terminal_does_not_say_demo(self, config: AppConfig) -> None:
        """A configuration that says DEMO over a real account is exactly the mistake."""
        for mode in (2, 1):  # real, contest
            holder = MT5Module()
            holder._module = FakeTerminal(trade_mode=mode)  # type: ignore[assignment]

            with pytest.raises(DemoUnavailable, match="did not confirm"):
                build_demo_service(config, place_orders=True, module=holder)

    def test_when_the_terminal_gives_no_trade_mode_at_all(self, config: AppConfig) -> None:
        class Silent(FakeTerminal):
            def account_info(self) -> Any:
                info = super().account_info()
                del info.trade_mode
                return info

        holder = MT5Module()
        holder._module = Silent()  # type: ignore[assignment]

        with pytest.raises(DemoUnavailable, match="did not confirm"):
            build_demo_service(config, module=holder)

    def test_when_the_contract_is_not_the_one_that_was_measured(self, config: AppConfig) -> None:
        holder = MT5Module()
        holder._module = FakeTerminal(tick_value="1.0")  # type: ignore[assignment]

        with pytest.raises(DemoUnavailable, match="tick_value"):
            build_demo_service(config, module=holder)

    def test_a_zero_order_limit(self, config: AppConfig, module: MT5Module) -> None:
        with pytest.raises(DemoUnavailable, match="max-orders"):
            build_demo_service(config, place_orders=True, max_orders=0, module=module)


class TestSpecificationComparison:
    def test_identical_contracts_have_no_differences(self) -> None:
        assert compare_specifications(us30_specification("BITCOIN"), us30_specification("BITCOIN")) == []

    def test_a_factor_of_ten_in_tick_value_is_caught(self) -> None:
        from dataclasses import replace

        recorded = us30_specification("BITCOIN")
        live = replace(recorded, tick_value=recorded.tick_value * 10)

        assert any("tick_value" in p for p in compare_specifications(live, recorded))

    def test_the_recorded_bitcoin_contract_is_what_was_measured(self) -> None:
        assert MEASURED_BITCOIN.point == us30_specification("BITCOIN").point


# =============================================================================
# Observe mode
# =============================================================================


class TestObserveSendsNothing:
    def test_it_reports_the_order_it_would_place_and_sends_none(
        self, config: AppConfig, module: MT5Module, terminal: FakeTerminal
    ) -> None:
        service = build_demo_service(config, module=module, log=lambda _l: None)

        report = service.run(max_polls=3, sleep=_no_sleep)

        assert terminal.sent == [], "observe mode must never call order_send"
        assert report["mode"] == "observe"
        kinds = [e["kind"] for e in report["events"]]
        assert "would_place" in kinds
        assert report["orders_placed"] == 0

    def test_it_decides_on_server_time_not_the_local_clock(
        self, config: AppConfig, module: MT5Module
    ) -> None:
        """The fake's server clock is in 2026-10; the machine's is whenever this runs."""
        service = build_demo_service(config, module=module, log=lambda _l: None)

        report = service.run(max_polls=1, sleep=_no_sleep)

        assert report["decisions"] == 1

    def test_one_decision_per_closed_candle(
        self, config: AppConfig, module: MT5Module, terminal: FakeTerminal
    ) -> None:
        service = build_demo_service(config, module=module, log=lambda _l: None)

        def tick_forward(_seconds: float) -> None:
            terminal.next_minute()

        report = service.run(max_polls=4, sleep=tick_forward)

        assert report["decisions"] == 4
        assert terminal.sent == []

    def test_a_market_that_is_closed_waits_instead_of_deciding(
        self, config: AppConfig, module: MT5Module, terminal: FakeTerminal
    ) -> None:
        """Server time moves on but no new bar arrives: nothing to decide on."""
        service = build_demo_service(config, module=module, log=lambda _l: None)
        service.run(max_polls=1, sleep=_no_sleep)

        def clock_only(_seconds: float) -> None:
            terminal.now += timedelta(minutes=5)

        report = service.run(max_polls=2, sleep=clock_only)

        assert report["decisions"] == 1
        assert any(e["kind"] == "waiting" for e in report["events"])


# =============================================================================
# Place mode
# =============================================================================


class TestPlacingOrders:
    def test_the_request_uses_the_published_constants(
        self, config: AppConfig, module: MT5Module, terminal: FakeTerminal
    ) -> None:
        service = build_demo_service(config, place_orders=True, module=module, log=lambda _l: None)

        service.run(max_polls=1, sleep=_no_sleep)

        request = terminal.sent[0]
        assert request["action"] == ACTION_PENDING
        assert request["type"] == TYPE_BUY_STOP
        assert request["type_filling"] == 2, "RETURN"
        assert request["symbol"] == "BITCOIN"
        assert request["sl"] < request["price"] < request["tp"]

    def test_the_order_is_sized_to_the_risk_budget_and_the_lot_step(
        self, config: AppConfig, module: MT5Module, terminal: FakeTerminal
    ) -> None:
        service = build_demo_service(config, place_orders=True, module=module, log=lambda _l: None)

        service.run(max_polls=1, sleep=_no_sleep)

        lots = terminal.sent[0]["volume"]
        # 0.5 % of 10 000 = 50, over a 100-point... a $100 stop on one coin per lot.
        assert lots == pytest.approx(0.5, abs=0.02)
        assert round(lots / 0.01) == pytest.approx(lots / 0.01)

    def test_it_attaches_without_a_login(
        self, config: AppConfig, module: MT5Module, terminal: FakeTerminal
    ) -> None:
        build_demo_service(config, module=module)

        assert terminal.initialized_with is not None
        assert "login" not in terminal.initialized_with
        assert "password" not in terminal.initialized_with

    def test_a_resting_order_is_cancelled_when_the_run_ends(
        self, config: AppConfig, module: MT5Module, terminal: FakeTerminal
    ) -> None:
        service = build_demo_service(config, place_orders=True, module=module, log=lambda _l: None)

        report = service.run(max_polls=1, sleep=_no_sleep)

        assert terminal.sent_actions() == [ACTION_PENDING, ACTION_REMOVE]
        assert terminal.order_rows == []
        assert report["orders_on_book"] == 0
        assert report["orders_placed"] == 1

    def test_keep_orders_leaves_it_and_says_so(
        self, config: AppConfig, module: MT5Module, terminal: FakeTerminal
    ) -> None:
        service = build_demo_service(
            config, place_orders=True, cancel_on_exit=False, module=module, log=lambda _l: None
        )

        report = service.run(max_polls=1, sleep=_no_sleep)

        assert len(terminal.order_rows) == 1
        assert any("left resting" in w for w in report["warnings"])

    def test_the_order_limit_is_respected(
        self, config: AppConfig, module: MT5Module, terminal: FakeTerminal
    ) -> None:
        service = build_demo_service(
            config, place_orders=True, max_orders=1, cancel_on_exit=False, module=module,
            log=lambda _l: None,
        )  # fmt: skip

        report = service.run(max_polls=4, sleep=lambda _s: terminal.next_minute())

        assert report["orders_placed"] == 1
        assert terminal.sent_actions().count(ACTION_PENDING) == 1
        assert any(e["kind"] == "limit" for e in report["events"])

    def test_a_stale_order_is_replaced_on_a_new_candle(
        self, config: AppConfig, module: MT5Module, terminal: FakeTerminal
    ) -> None:
        service = build_demo_service(
            config, place_orders=True, max_orders=3, module=module, log=lambda _l: None
        )

        service.run(max_polls=3, sleep=lambda _s: terminal.next_minute())

        actions = terminal.sent_actions()
        assert actions.count(ACTION_PENDING) >= 2
        assert ACTION_REMOVE in actions, "the old stop is removed before the new one is placed"

    def test_the_same_candle_is_never_ordered_twice(
        self, config: AppConfig, module: MT5Module, terminal: FakeTerminal
    ) -> None:
        service = build_demo_service(
            config, place_orders=True, max_orders=5, cancel_on_exit=False, module=module,
            log=lambda _l: None,
        )  # fmt: skip

        service.run(max_polls=6, sleep=_no_sleep)  # the clock never moves

        assert terminal.sent_actions().count(ACTION_PENDING) == 1


class TestTheGateIsEnforcedInsideTheLifecycle:
    def test_observe_mode_cannot_place_even_if_the_lifecycle_is_driven_directly(
        self, config: AppConfig, module: MT5Module, terminal: FakeTerminal
    ) -> None:
        """The hole this closes: ``tick`` -> ``place_order(plan)`` never asked the gate."""
        from stop_order_scalp.application.service import size_decision
        from stop_order_scalp.strategy.strategy import StrategyContext

        service = build_demo_service(config, module=module, log=lambda _l: None)  # observe
        m1 = service.feed.candles("BITCOIN", "M1", 5, before=SERVER_NOW)
        m15 = service.feed.candles("BITCOIN", "M15", 5, before=SERVER_NOW)
        decision = service.strategy.evaluate(
            StrategyContext("BITCOIN", m15, m1, SERVER_NOW, service.specification)
        )
        plan, _why = size_decision(
            service.risk_manager, service.broker.account(), service.specification, decision
        )
        assert plan is not None
        service.lifecycle.attach_market(service.broker.tick("BITCOIN"), service.specification)

        step = service.lifecycle.tick(plan)

        assert "gate_refused" in step.actions
        assert terminal.sent == []


# =============================================================================
# Positions: the take-profit must survive the stop moving
# =============================================================================


class TestAFilledPositionIsManaged:
    def test_trailing_moves_the_stop_and_keeps_the_target(
        self, config: AppConfig, module: MT5Module, terminal: FakeTerminal
    ) -> None:
        service = build_demo_service(
            config, place_orders=True, cancel_on_exit=False, module=module, log=lambda _l: None
        )
        service.run(max_polls=1, sleep=_no_sleep)
        original_tp = terminal.order_rows[0]["tp"]
        original_sl = terminal.order_rows[0]["sl"]
        entry = terminal.order_rows[0]["price_open"]
        terminal.fill_resting_order()
        terminal.bid = entry + 200.0  # well past break-even and the trailing distance

        service.run(max_polls=3, sleep=_no_sleep)

        modifies = [r for r in terminal.sent if r["action"] == ACTION_SLTP]
        assert modifies, "the stop should have been moved"
        assert all(m["tp"] == pytest.approx(original_tp) for m in modifies), (
            "a modify must not remove the take-profit"
        )
        assert modifies[-1]["sl"] > original_sl


# =============================================================================
# A refusal is an outcome, not a crash
# =============================================================================


class TestARejectedOrder:
    @staticmethod
    def _service(config: AppConfig, terminal: FakeTerminal, retcode: int, **kwargs: Any) -> Any:
        terminal.pending_retcode = retcode
        holder = MT5Module()
        holder._module = terminal  # type: ignore[assignment]
        return build_demo_service(
            config, place_orders=True, module=holder, log=lambda _l: None, **kwargs
        )

    def test_autotrading_off_is_reported_with_the_fix_and_does_not_crash(
        self, config: AppConfig, terminal: FakeTerminal
    ) -> None:
        service = self._service(config, terminal, 10027)

        report = service.run(max_polls=1, sleep=_no_sleep)

        rejected = [e for e in report["events"] if e["kind"] == "rejected"]
        assert len(rejected) == 1
        assert "10027" in rejected[0]["detail"]
        assert "Algo Trading" in rejected[0]["detail"], "the operator is told what to press"
        assert report["orders_placed"] == 0
        assert report["rejections"] == 1

    def test_an_unsupported_filling_mode_names_the_setting(
        self, config: AppConfig, terminal: FakeTerminal
    ) -> None:
        service = self._service(config, terminal, 10030)

        report = service.run(max_polls=1, sleep=_no_sleep)

        detail = next(e["detail"] for e in report["events"] if e["kind"] == "rejected")
        assert "filling_policy" in detail

    def test_the_run_stops_after_repeated_rejections_instead_of_hammering_the_server(
        self, config: AppConfig, terminal: FakeTerminal
    ) -> None:
        service = self._service(config, terminal, 10027)

        report = service.run(max_polls=50, sleep=lambda _s: terminal.next_minute())

        assert report["stop_reason"] == "rejected_repeatedly"
        assert terminal.sent_actions().count(ACTION_PENDING) == 3

    def test_a_rejection_leaves_nothing_on_the_book_and_the_machine_ready(
        self, config: AppConfig, terminal: FakeTerminal
    ) -> None:
        service = self._service(config, terminal, 10027)

        service.run(max_polls=1, sleep=_no_sleep)

        assert terminal.order_rows == []
        assert str(service.lifecycle.state).endswith("waiting_for_signal")

    def test_after_a_transient_rejection_the_next_candle_can_place(
        self, config: AppConfig, terminal: FakeTerminal
    ) -> None:
        service = self._service(config, terminal, 10015)  # invalid price: it moved

        service.run(max_polls=1, sleep=_no_sleep)
        terminal.pending_retcode = None
        terminal.next_minute()
        report = service.run(max_polls=1, sleep=_no_sleep)

        assert report["orders_placed"] == 1


class TestAnUnknownOutcomeStopsTheRun:
    """The real first run met ``retcode -1 ()`` and carried on polling for 37 cycles."""

    def test_an_ipc_failure_ends_the_run_with_a_warning_to_look_at_the_terminal(
        self, config: AppConfig, terminal: FakeTerminal
    ) -> None:
        terminal.send_none = True
        terminal.last_error_pair = (-10001, "Internal IPC send failed")
        holder = MT5Module()
        holder._module = terminal  # type: ignore[assignment]
        service = build_demo_service(config, place_orders=True, module=holder, log=lambda _l: None)

        report = service.run(max_polls=50, sleep=_no_sleep)

        assert report["stop_reason"] == "execution_unknown"
        assert terminal.sent_actions().count(ACTION_PENDING) == 1, "it must not resend"
        assert any("UNKNOWN" in w for w in report["warnings"])

    def test_auto_trading_disabled_is_a_refusal_not_an_unknown(
        self, config: AppConfig, terminal: FakeTerminal
    ) -> None:
        terminal.send_none = True
        terminal.last_error_pair = (-8, "Auto-trading disabled")
        holder = MT5Module()
        holder._module = terminal  # type: ignore[assignment]
        service = build_demo_service(config, place_orders=True, module=holder, log=lambda _l: None)

        report = service.run(max_polls=1, sleep=_no_sleep)

        assert report["stop_reason"] != "execution_unknown"
        assert report["rejections"] == 1
        detail = next(e["detail"] for e in report["events"] if e["kind"] == "rejected")
        assert "Algo Trading" in detail

    def test_the_order_check_refusal_reaches_the_operator_in_plain_words(
        self, config: AppConfig, terminal: FakeTerminal
    ) -> None:
        terminal.check_retcode = 10016
        holder = MT5Module()
        holder._module = terminal  # type: ignore[assignment]
        service = build_demo_service(config, place_orders=True, module=holder, log=lambda _l: None)

        report = service.run(max_polls=1, sleep=_no_sleep)

        assert terminal.sent == [], "a refused check must send nothing"
        assert report["rejections"] == 1


class TestTheFirstRealOrderRegression:
    def test_the_comment_the_project_sends_is_short_enough_for_the_real_terminal(
        self, config: AppConfig, module: MT5Module, terminal: FakeTerminal
    ) -> None:
        """Measured: a 31-character comment gave ``Invalid "comment" argument`` and, because
        ``order_send`` then returned ``None``, an "unknown outcome" with no cause."""
        service = build_demo_service(config, place_orders=True, module=module, log=lambda _l: None)

        report = service.run(max_polls=1, sleep=_no_sleep)

        assert report["orders_placed"] == 1
        assert report["rejections"] == 0
        assert len(terminal.sent[0]["comment"]) < 31
        assert terminal.sent[0]["comment"].split("|")[0], "the identity tag is still first"
