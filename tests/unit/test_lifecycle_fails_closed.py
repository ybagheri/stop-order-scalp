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
