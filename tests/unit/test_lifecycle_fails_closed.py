"""A real gate that cannot be evaluated refuses; it does not wave the order through.

The lifecycle used to consult the order gate only when a caller passed ``settings`` -- and the
ordinary path, ``tick`` -> ``place_order(plan)``, passed none. A simulated broker cannot lose
money, so nothing noticed. These tests fix the behaviour a real broker needs.
"""

from __future__ import annotations

from pathlib import Path

from stop_order_scalp.domain.enums import Environment
from stop_order_scalp.domain.models import EnvironmentSettings
from stop_order_scalp.execution.gates import DemoOrderGate
from stop_order_scalp.infrastructure.clock import FixedClock
from stop_order_scalp.infrastructure.persistence import StateLedger
from stop_order_scalp.lifecycle.trade_lifecycle import TradeLifecycle

from .test_trade_lifecycle import MAGIC, NOW, FakeBroker, FakeOrderManager, plan


def _lifecycle(
    path: Path, broker: FakeBroker, gate: object, settings: EnvironmentSettings | None
) -> TradeLifecycle:
    return TradeLifecycle(
        broker,
        StateLedger.load(path),
        FakeOrderManager(gate=gate),
        clock=FixedClock(NOW),
        magic_number=MAGIC,
        symbol="US30",
        settings=settings,
    )


def test_a_real_gate_with_no_environment_refuses(tmp_path: Path) -> None:
    broker = FakeBroker()
    lifecycle = _lifecycle(tmp_path / "s.json", broker, DemoOrderGate(enabled=True), None)

    step = lifecycle.tick(plan())

    assert step.actions == ("gate_refused",)
    assert broker.sends == []


def test_a_closed_real_gate_refuses_with_an_environment(tmp_path: Path) -> None:
    demo = EnvironmentSettings(environment=Environment.DEMO, allow_order=True)
    broker = FakeBroker()
    lifecycle = _lifecycle(tmp_path / "s.json", broker, DemoOrderGate(enabled=False), demo)

    step = lifecycle.tick(plan())

    assert step.actions == ("gate_refused",)
    assert broker.sends == []


def test_an_open_real_gate_places(tmp_path: Path) -> None:
    demo = EnvironmentSettings(environment=Environment.DEMO, allow_order=True)
    broker = FakeBroker()
    gate = DemoOrderGate(enabled=True, account_confirmed_demo=True)
    lifecycle = _lifecycle(tmp_path / "s.json", broker, gate, demo)

    lifecycle.tick(plan())

    assert len(broker.sends) == 1


def test_a_closed_gate_also_stops_a_cancel(tmp_path: Path) -> None:
    demo = EnvironmentSettings(environment=Environment.DEMO, allow_order=True)
    broker = FakeBroker()
    lifecycle = _lifecycle(tmp_path / "s.json", broker, DemoOrderGate(enabled=False), demo)

    step = lifecycle.cancel_pending(556)

    assert step.actions == ("gate_refused",)
    assert broker.cancels == []


class TestARejectionIsSettledNotRaised:
    def test_it_is_recorded_and_the_machine_is_free_for_the_next_plan(self, tmp_path: Path) -> None:
        from stop_order_scalp.domain.exceptions import BrokerRejectedError
        from stop_order_scalp.infrastructure.persistence import IntentOutcome

        from .test_trade_lifecycle import _tag_of

        broker = FakeBroker()
        broker.send_error = BrokerRejectedError("retcode 10015 (Invalid price)")
        ledger_path = tmp_path / "s.json"
        lifecycle = TradeLifecycle(
            broker,
            StateLedger.load(ledger_path),
            FakeOrderManager(),
            clock=FixedClock(NOW),
            magic_number=MAGIC,
            symbol="US30",
        )

        step = lifecycle.tick(plan())

        assert step.actions == ("rejected",)
        assert any("10015" in note for note in step.notes)
        entry = StateLedger.load(ledger_path).get(_tag_of(plan()))
        assert entry is not None
        assert entry.outcome == IntentOutcome.REJECTED
        assert lifecycle.state.name.endswith("WAITING_FOR_SIGNAL")
