"""``book`` / ``cancel`` / ``close`` / ``flatten``: what an operator does by hand.

Built on the same terminal double as the demo run -- one that exposes only functions the real
``MetaTrader5`` package has -- and the real :class:`MetaTrader5Broker` over it. The things worth
pinning are the safety properties, not the happy path: that nothing is sent without ``--yes``,
that only the strategy's own trades can be touched, and that each gate refuses on its own.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from stop_order_scalp.application.operator import (
    OperatorService,
    OperatorUnavailable,
    build_operator,
)
from stop_order_scalp.cli.main import EXIT_OK, EXIT_UNAVAILABLE, main
from stop_order_scalp.domain.enums import Environment
from stop_order_scalp.domain.models import EnvironmentSettings
from stop_order_scalp.execution.gates import DemoCloseGate, DemoOrderGate
from stop_order_scalp.execution.mt5_broker import MetaTrader5Broker
from stop_order_scalp.infrastructure.config import AppConfig
from stop_order_scalp.market_data.mt5_module import MT5Module

from .test_demo_service import ACTION_DEAL, ACTION_REMOVE, FakeTerminal, _bitcoin_yaml

MAGIC = 20260930


@pytest.fixture
def terminal() -> FakeTerminal:
    return FakeTerminal()


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    from stop_order_scalp.infrastructure.config import load_config

    for name in [n for n in os.environ if n.startswith("SOS_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("SOS_ENVIRONMENT", "DEMO")
    monkeypatch.setenv("SOS_ALLOW_ORDER", "true")
    monkeypatch.setenv("SOS_ALLOW_CLOSE", "true")
    monkeypatch.setenv("SOS_SYMBOL", "BITCOIN")
    return load_config(
        config_path=_bitcoin_yaml(tmp_path), root=tmp_path, env_file=tmp_path / "none.env"
    )


def _order(ticket: int, *, magic: int = MAGIC) -> dict[str, Any]:
    return {
        "ticket": ticket, "symbol": "BITCOIN", "type": 4, "volume_current": 0.15,
        "price_open": 85_130.5, "sl": 85_030.5, "tp": 85_430.5, "magic": magic,
        "comment": "abc|SOS", "time_setup": 1_790_000_000, "time_expiration": 0,
    }  # fmt: skip


def _position(ticket: int, *, magic: int = MAGIC, long: bool = True) -> dict[str, Any]:
    return {
        "ticket": ticket, "symbol": "BITCOIN", "type": 0 if long else 1, "volume": 0.15,
        "price_open": 85_130.5, "sl": 85_030.5, "tp": 85_430.5, "magic": magic,
        "comment": "abc|SOS", "time": 1_790_000_000, "profit": 3.2,
    }  # fmt: skip


def _settings(**overrides: Any) -> EnvironmentSettings:
    values: dict[str, Any] = {
        "environment": Environment.DEMO,
        "allow_order": True,
        "allow_close": True,
        "symbol": "BITCOIN",
        "magic_number": MAGIC,
    }
    values.update(overrides)
    return EnvironmentSettings(**values)


def _service(terminal: FakeTerminal, **settings: Any) -> OperatorService:
    holder = MT5Module()
    holder._module = terminal  # type: ignore[assignment]
    cfg = _settings(**settings)
    broker = MetaTrader5Broker(holder, cfg)
    broker.connect()
    return OperatorService(
        settings=cfg,
        broker=broker,
        symbol="BITCOIN",
        order_gate=DemoOrderGate(enabled=True, account_confirmed_demo=True),
        close_gate=DemoCloseGate(enabled=True, account_confirmed_demo=True),
    )


class TestBook:
    def test_it_lists_only_this_strategys_trades(self, terminal: FakeTerminal) -> None:
        terminal.order_rows = [_order(1), _order(2, magic=999)]
        terminal.position_rows = [_position(10), _position(11, magic=999)]

        book = _service(terminal).book()

        assert [o["ticket"] for o in book["resting_orders"]] == [1]
        assert [p["ticket"] for p in book["positions"]] == [10]
        assert book["account"]["is_demo"] is True

    def test_it_reports_the_floating_profit(self, terminal: FakeTerminal) -> None:
        terminal.position_rows = [_position(10), _position(11)]

        assert _service(terminal).book()["floating_profit"] == "6.4"

    def test_it_sends_nothing(self, terminal: FakeTerminal) -> None:
        terminal.order_rows = [_order(1)]
        terminal.position_rows = [_position(10)]

        _service(terminal).book()

        assert terminal.sent == []


class TestPreviewIsTheDefault:
    def test_cancel_without_yes_sends_nothing_and_says_what_it_would_do(
        self, terminal: FakeTerminal
    ) -> None:
        terminal.order_rows = [_order(1)]

        report = _service(terminal).cancel()

        assert terminal.sent == []
        assert report["preview"] is True
        assert [m["ticket"] for m in report["matched"]] == [1]
        assert "--yes" in report["hint"]

    def test_close_without_yes_sends_nothing(self, terminal: FakeTerminal) -> None:
        terminal.position_rows = [_position(10)]

        report = _service(terminal).close()

        assert terminal.sent == []
        assert report["preview"] is True
        assert report["results"] == []

    def test_a_preview_says_when_the_action_would_not_be_permitted(
        self, terminal: FakeTerminal
    ) -> None:
        terminal.position_rows = [_position(10)]

        report = _service(terminal, allow_close=False).close()

        assert report["permitted"] is False
        assert "SOS_ALLOW_CLOSE" in report["why_not"]
        assert terminal.sent == []


class TestCancel:
    def test_with_yes_it_removes_the_resting_orders(self, terminal: FakeTerminal) -> None:
        terminal.order_rows = [_order(1), _order(2)]

        report = _service(terminal).cancel(confirm=True)

        assert [r["outcome"] for r in report["results"]] == ["cancelled", "cancelled"]
        assert terminal.sent_actions() == [ACTION_REMOVE, ACTION_REMOVE]
        assert terminal.order_rows == []

    def test_one_ticket_only(self, terminal: FakeTerminal) -> None:
        terminal.order_rows = [_order(1), _order(2)]

        _service(terminal).cancel(ticket=2, confirm=True)

        assert [r["ticket"] for r in terminal.order_rows] == [1]

    def test_an_order_opened_by_hand_is_refused_by_name(
        self, terminal: FakeTerminal
    ) -> None:
        terminal.order_rows = [_order(1), _order(2, magic=999)]

        with pytest.raises(OperatorUnavailable, match="not one of this strategy's"):
            _service(terminal).cancel(ticket=2, confirm=True)

        assert terminal.sent == []

    def test_it_needs_allow_order(self, terminal: FakeTerminal) -> None:
        terminal.order_rows = [_order(1)]

        with pytest.raises(OperatorUnavailable, match="SOS_ALLOW_ORDER"):
            _service(terminal, allow_order=False).cancel(confirm=True)

        assert terminal.sent == []


class TestClose:
    def test_with_yes_it_closes_at_market(self, terminal: FakeTerminal) -> None:
        terminal.position_rows = [_position(10)]

        report = _service(terminal).close(confirm=True)

        assert report["results"] == [{"ticket": 10, "outcome": "closed"}]
        assert terminal.sent_actions() == [ACTION_DEAL]
        assert terminal.sent[0]["position"] == 10
        assert terminal.position_rows == []

    def test_it_needs_its_own_switch_not_allow_order(self, terminal: FakeTerminal) -> None:
        """Placing and closing are separate risks and separate switches."""
        terminal.position_rows = [_position(10)]

        with pytest.raises(OperatorUnavailable, match="SOS_ALLOW_CLOSE"):
            _service(terminal, allow_order=True, allow_close=False).close(confirm=True)

        assert terminal.sent == []

    def test_a_position_that_closed_in_the_meantime_is_not_an_error(
        self, terminal: FakeTerminal
    ) -> None:
        """A stop-loss hit between ``book`` and ``close`` must not look like a failure."""

        class Racing(FakeTerminal):
            def positions_get(self, *, symbol: str | None = None, ticket: int | None = None) -> Any:
                if ticket is not None:
                    return ()  # gone by the time the broker looks it up by ticket
                return super().positions_get(symbol=symbol, ticket=ticket)

        racing = Racing()
        racing.position_rows = [_position(10)]

        report = _service(racing).close(confirm=True)

        assert report["results"][0]["outcome"] == "already_gone"

    def test_a_refusal_for_one_does_not_stop_the_rest(self, terminal: FakeTerminal) -> None:
        terminal.position_rows = [_position(10), _position(11)]
        calls = {"n": 0}
        original = terminal.order_check

        def flaky(request: dict[str, Any]) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                terminal.check_retcode = 10018
            else:
                terminal.check_retcode = 0
            return original(request)

        terminal.order_check = flaky  # type: ignore[method-assign]

        report = _service(terminal).close(confirm=True)

        assert [r["outcome"] for r in report["results"]] == ["refused", "closed"]


class TestFlatten:
    def test_orders_are_cancelled_before_positions_are_closed(
        self, terminal: FakeTerminal
    ) -> None:
        """So a pending stop cannot fill into a fresh position between the two steps."""
        terminal.order_rows = [_order(1)]
        terminal.position_rows = [_position(10)]

        report = _service(terminal).flatten(confirm=True)

        assert terminal.sent_actions() == [ACTION_REMOVE, ACTION_DEAL]
        assert report["book_after"]["resting_orders"] == []
        assert report["book_after"]["positions"] == []

    def test_a_preview_changes_nothing(self, terminal: FakeTerminal) -> None:
        terminal.order_rows = [_order(1)]
        terminal.position_rows = [_position(10)]

        report = _service(terminal).flatten()

        assert terminal.sent == []
        assert report["preview"] is True
        assert report["book_after"] is None


class TestStartingTheOperator:
    def test_not_in_the_demo_environment(
        self, config: AppConfig, terminal: FakeTerminal, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:  # fmt: skip
        from stop_order_scalp.infrastructure.config import load_config

        monkeypatch.setenv("SOS_ENVIRONMENT", "DRY_RUN")
        cfg = load_config(config_path=_bitcoin_yaml(tmp_path), root=tmp_path, env_file=tmp_path / "x")
        holder = MT5Module()
        holder._module = terminal  # type: ignore[assignment]

        with pytest.raises(OperatorUnavailable, match="SOS_ENVIRONMENT"):
            build_operator(cfg, module=holder)

    def test_when_the_terminal_does_not_say_demo(self, config: AppConfig) -> None:
        holder = MT5Module()
        holder._module = FakeTerminal(trade_mode=2)  # type: ignore[assignment]

        with pytest.raises(OperatorUnavailable, match="did not confirm"):
            build_operator(config, module=holder)

    def test_with_live_trading_enabled(
        self, config: AppConfig, terminal: FakeTerminal, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:  # fmt: skip
        from stop_order_scalp.infrastructure.config import load_config

        del config
        monkeypatch.setenv("SOS_ALLOW_LIVE", "true")
        cfg = load_config(config_path=_bitcoin_yaml(tmp_path), root=tmp_path, env_file=tmp_path / "x")
        holder = MT5Module()
        holder._module = terminal  # type: ignore[assignment]

        with pytest.raises(OperatorUnavailable, match="SOS_ALLOW_LIVE"):
            build_operator(cfg, module=holder)

    def test_a_confirmed_demo_account_resolves_the_symbol(
        self, config: AppConfig, terminal: FakeTerminal
    ) -> None:
        holder = MT5Module()
        holder._module = terminal  # type: ignore[assignment]

        service = build_operator(config, module=holder, log=lambda _l: None)

        assert service.symbol == "BITCOIN"


class TestTheCommandLine:
    def _run(
        self, argv: list[str], monkeypatch: pytest.MonkeyPatch, terminal: FakeTerminal,
        config: AppConfig,
    ) -> tuple[int, dict[str, Any], str]:  # fmt: skip
        holder = MT5Module()
        holder._module = terminal  # type: ignore[assignment]
        monkeypatch.setattr(
            "stop_order_scalp.application.operator.MT5Module", lambda: holder
        )
        out, err = io.StringIO(), io.StringIO()
        code = main(argv, stdout=out, stderr=err)
        text = out.getvalue()
        return code, (json.loads(text) if text.strip() else {}), err.getvalue()

    def test_book_prints_the_account_and_the_strategys_trades(
        self, monkeypatch: pytest.MonkeyPatch, terminal: FakeTerminal, config: AppConfig,
        tmp_path: Path,
    ) -> None:  # fmt: skip
        terminal.position_rows = [_position(10)]

        code, report, _err = self._run(
            ["book", "--config", str(tmp_path / "btc.yaml")], monkeypatch, terminal, config
        )

        assert code == EXIT_OK
        assert report["command"] == "book"
        assert [p["ticket"] for p in report["positions"]] == [10]

    def test_close_without_yes_is_a_preview(
        self, monkeypatch: pytest.MonkeyPatch, terminal: FakeTerminal, config: AppConfig,
        tmp_path: Path,
    ) -> None:  # fmt: skip
        terminal.position_rows = [_position(10)]

        code, report, _err = self._run(
            ["close", "--config", str(tmp_path / "btc.yaml")], monkeypatch, terminal, config
        )

        assert code == EXIT_OK
        assert report["preview"] is True
        assert terminal.sent == []

    def test_close_with_yes_acts(
        self, monkeypatch: pytest.MonkeyPatch, terminal: FakeTerminal, config: AppConfig,
        tmp_path: Path,
    ) -> None:  # fmt: skip
        monkeypatch.setenv("SOS_ALLOW_CLOSE", "true")
        terminal.position_rows = [_position(10)]

        code, report, _err = self._run(
            ["close", "--yes", "--config", str(tmp_path / "btc.yaml")], monkeypatch, terminal, config
        )

        assert code == EXIT_OK
        assert report["results"] == [{"ticket": 10, "outcome": "closed"}]

    def test_a_refusal_exits_with_the_unavailable_code_and_a_reason(
        self, monkeypatch: pytest.MonkeyPatch, terminal: FakeTerminal, config: AppConfig,
        tmp_path: Path,
    ) -> None:  # fmt: skip
        monkeypatch.setenv("SOS_ALLOW_CLOSE", "false")
        terminal.position_rows = [_position(10)]

        code, _report, err = self._run(
            ["close", "--yes", "--config", str(tmp_path / "btc.yaml")], monkeypatch, terminal, config
        )

        assert code == EXIT_UNAVAILABLE
        assert "SOS_ALLOW_CLOSE" in err
        assert terminal.sent == []


def _deal(
    ticket: int, *, entry: int, kind: int, profit: float = 0.0, commission: float = 0.0,
    reason: int = 3, magic: int = MAGIC, position: int = 10,
) -> dict[str, Any]:  # fmt: skip
    return {
        "ticket": ticket, "order": ticket, "time": 1_790_000_000 + ticket, "type": kind,
        "entry": entry, "magic": magic, "position_id": position, "reason": reason,
        "volume": 0.15, "price": 85_130.5, "commission": commission, "swap": 0.0,
        "profit": profit, "symbol": "BITCOIN", "comment": "abc|SOS",
    }  # fmt: skip


class TestHistory:
    def test_a_closed_trade_reports_how_it_ended_and_what_it_cost(
        self, terminal: FakeTerminal
    ) -> None:
        terminal.deal_rows = [
            _deal(1, entry=0, kind=0, commission=-0.5),
            _deal(2, entry=1, kind=1, profit=12.0, commission=-0.5, reason=5),
            _deal(3, entry=0, kind=0, commission=-0.5, position=11),
            _deal(4, entry=1, kind=1, profit=-9.0, commission=-0.5, reason=4, position=11),
        ]

        report = _service(terminal).history()

        summary = report["summary"]
        assert summary["closed_trades"] == 2
        assert summary["winning"] == 1
        assert summary["net"] == pytest.approx(1.0), "12 - 9 = 3, less four commissions of 0.5"
        assert summary["how_trades_ended"] == {"take_profit": 1, "stop_loss": 1}

    def test_it_ignores_other_magic_numbers_and_balance_operations(
        self, terminal: FakeTerminal
    ) -> None:
        terminal.deal_rows = [
            _deal(1, entry=1, kind=1, profit=5.0),
            _deal(2, entry=1, kind=1, profit=999.0, magic=999),
            _deal(3, entry=0, kind=2, profit=10_000.0),  # a balance operation
        ]

        report = _service(terminal).history()

        assert [d["ticket"] for d in report["deals"]] == [1]
        assert report["summary"]["net"] == pytest.approx(5.0)

    def test_it_sends_nothing(self, terminal: FakeTerminal) -> None:
        terminal.deal_rows = [_deal(1, entry=1, kind=1, profit=5.0)]

        _service(terminal).history()

        assert terminal.sent == []

    def test_no_trades_is_an_empty_summary_not_an_error(self, terminal: FakeTerminal) -> None:
        report = _service(terminal).history(days=3)

        assert report["summary"]["closed_trades"] == 0
        assert report["summary"]["net"] == 0.0
