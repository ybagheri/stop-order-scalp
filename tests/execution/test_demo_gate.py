"""The demo gate opens for a confirmed demo account and nothing else.

Four independent conditions, and a gate whose whole value is that it fails closed: each test
below turns exactly one of them off and expects a refusal, and the last proves a live
configuration cannot be argued through it.
"""

from __future__ import annotations

from typing import Any

import pytest

from stop_order_scalp.domain.enums import Environment
from stop_order_scalp.domain.models import EnvironmentSettings
from stop_order_scalp.execution.gates import DemoOrderGate, GateRefusal, OrderGate


def _demo(**overrides: Any) -> EnvironmentSettings:
    values: dict[str, Any] = {"environment": Environment.DEMO, "allow_order": True}
    values.update(overrides)
    return EnvironmentSettings(**values)


def _open_gate() -> DemoOrderGate:
    return DemoOrderGate(enabled=True, account_confirmed_demo=True)


class TestOpensOnlyWhenEverythingHolds:
    def test_all_four_conditions_open_it(self) -> None:
        assert _open_gate().check(_demo()).open

    def test_it_is_closed_by_default(self) -> None:
        assert DemoOrderGate().check(_demo()).code == GateRefusal.DISABLED

    def test_without_the_runs_own_opt_in(self) -> None:
        gate = DemoOrderGate(enabled=False, account_confirmed_demo=True)

        decision = gate.check(_demo())

        assert decision.refused
        assert decision.code == GateRefusal.DISABLED

    def test_without_allow_order(self) -> None:
        decision = _open_gate().check(_demo(allow_order=False))

        assert decision.code == GateRefusal.ALLOW_OPERATION_MISSING

    def test_without_the_terminals_confirmation_of_a_demo_account(self) -> None:
        gate = DemoOrderGate(enabled=True, account_confirmed_demo=False)

        decision = gate.check(_demo())

        assert decision.refused
        assert decision.code == GateRefusal.DEMO_REQUIRED
        assert "might be real" in decision.reason

    @pytest.mark.parametrize("environment", [Environment.DRY_RUN, Environment.PAPER])
    def test_in_a_simulated_environment(self, environment: Environment) -> None:
        decision = _open_gate().check(_demo(environment=environment))

        assert decision.code == GateRefusal.DEMO_REQUIRED


class TestItCannotBeArguedIntoALiveOrder:
    def test_live_is_refused_even_with_every_other_switch_on(self) -> None:
        live = EnvironmentSettings(
            environment=Environment.LIVE, allow_live=True, allow_order=True, allow_close=True
        )

        decision = _open_gate().check(live)

        assert decision.refused
        assert "LIVE" in decision.reason or "DEMO" in decision.reason

    def test_require_raises_rather_than_returning(self) -> None:
        with pytest.raises(Exception, match="gate"):
            DemoOrderGate().require(_demo())

    def test_the_live_gate_is_untouched_and_still_refuses_demo(self) -> None:
        """Adding a demo gate must not have opened the live one to demo."""
        assert OrderGate(enabled=True).check(_demo()).refused


class TestSimulatedMarker:
    def test_a_real_gate_never_calls_itself_simulated(self) -> None:
        assert DemoOrderGate().simulated is False
        assert OrderGate().simulated is False
