"""The gates refuse by default, and LIVE needs all three switches.

These tests exist because the failure they guard against is invisible: a gate that defaults
open produces no test failure, it produces a live order.
"""

from __future__ import annotations

import pytest

from stop_order_scalp.domain.enums import Environment
from stop_order_scalp.domain.exceptions import ConfigError
from stop_order_scalp.domain.models import EnvironmentSettings
from stop_order_scalp.execution.gates import (
    CloseGate,
    GateRefusal,
    LiveInterlock,
    OrderGate,
    SimulatedGate,
    describe_safety,
    require_configured,
)
from stop_order_scalp.infrastructure.config import TargetSettings


class TestDefaults:
    def test_both_gates_are_closed_unless_told_otherwise(self) -> None:
        assert OrderGate().enabled is False
        assert CloseGate().enabled is False

    @pytest.mark.parametrize("gate", [OrderGate(), CloseGate()])
    def test_a_closed_gate_refuses_even_when_everything_else_is_permitted(
        self, gate: OrderGate | CloseGate, live_settings: EnvironmentSettings
    ) -> None:
        """The gate's own switch is not implied by SOS_ALLOW_LIVE."""
        decision = gate.check(live_settings)
        assert decision.refused
        assert decision.code == GateRefusal.DISABLED


class TestThreeFoldInterlock:
    """LIVE needs environment, allow_live, and the operation's own switch.

    Each case below removes exactly one of the three, so a gate that checked only two would
    fail one of them.
    """

    def test_all_three_present_opens_the_order_gate(self, live_settings: EnvironmentSettings) -> None:
        assert OrderGate(enabled=True).check(live_settings).open is True

    def test_missing_allow_live_refuses(self) -> None:
        settings = EnvironmentSettings(
            environment=Environment.LIVE, allow_live=False, allow_order=True
        )
        decision = OrderGate(enabled=True).check(settings)
        assert decision.refused
        assert decision.code == GateRefusal.ALLOW_LIVE_MISSING

    def test_missing_allow_order_refuses(self) -> None:
        """allow_live alone is not enough to place an order."""
        settings = EnvironmentSettings(
            environment=Environment.LIVE, allow_live=True, allow_order=False
        )
        decision = OrderGate(enabled=True).check(settings)
        assert decision.refused
        assert decision.code == GateRefusal.ALLOW_OPERATION_MISSING

    @pytest.mark.parametrize(
        "environment", [Environment.DRY_RUN, Environment.PAPER, Environment.DEMO]
    )
    def test_no_other_environment_opens_the_gate(self, environment: Environment) -> None:
        """DRY_RUN, PAPER and DEMO all refuse, so only an explicit LIVE can trade."""
        settings = EnvironmentSettings(
            environment=environment, allow_live=True, allow_order=True
        )
        decision = OrderGate(enabled=True).check(settings)
        assert decision.refused
        assert decision.code == GateRefusal.NOT_LIVE


class TestGatesAreIndependent:
    def test_opening_the_order_gate_does_not_open_the_close_gate(
        self, live_settings: EnvironmentSettings
    ) -> None:
        """Automatic closing on a losing streak is exactly when an operator wants it off."""
        assert OrderGate(enabled=True).check(live_settings).open is True
        assert CloseGate().check(live_settings).refused

    def test_allow_order_does_not_permit_closing(self) -> None:
        settings = EnvironmentSettings(
            environment=Environment.LIVE, allow_live=True, allow_order=True, allow_close=False
        )
        assert OrderGate(enabled=True).check(settings).open is True
        decision = CloseGate(enabled=True).check(settings)
        assert decision.refused
        assert decision.code == GateRefusal.ALLOW_OPERATION_MISSING


class TestRequire:
    def test_require_raises_on_a_closed_gate(self, live_settings: EnvironmentSettings) -> None:
        with pytest.raises(PermissionError, match=GateRefusal.DISABLED):
            OrderGate().require(live_settings)

    def test_require_is_silent_when_open(self, live_settings: EnvironmentSettings) -> None:
        OrderGate(enabled=True).require(live_settings)


class TestInterlock:
    def test_live_reachable_needs_both(self) -> None:
        assert LiveInterlock(environment=Environment.LIVE, allow_live=True).live_reachable
        assert not LiveInterlock(
            environment=Environment.LIVE, allow_live=False
        ).live_reachable
        assert not LiveInterlock(
            environment=Environment.PAPER, allow_live=True
        ).live_reachable

    def test_from_settings_mirrors_the_configuration(self, live_settings: EnvironmentSettings) -> None:
        assert LiveInterlock.from_settings(live_settings).live_reachable


class TestSimulatedGate:
    """The gate a simulated venue uses.

    Not simply an open gate: it refuses ``LIVE``. That is what makes it safe to use as the
    default in a test or a ``DRY_RUN``, because a composition mistake that wired it to a real
    broker fails closed instead of trading.
    """

    def test_it_opens_for_dry_run(self, dry_run_settings: EnvironmentSettings) -> None:
        assert SimulatedGate().check(dry_run_settings).open is True

    def test_it_opens_for_paper(self) -> None:
        settings = EnvironmentSettings(environment=Environment.PAPER)

        assert SimulatedGate().check(settings).open is True

    def test_it_refuses_live(self, live_settings: EnvironmentSettings) -> None:
        """A real venue needs OrderGate and its three independent switches."""
        decision = SimulatedGate().check(live_settings)

        assert decision.refused
        assert decision.code == GateRefusal.NOT_LIVE

    def test_it_reports_itself_enabled(self) -> None:
        """Required by the shared ``Gate`` protocol, and true rather than absent."""
        assert SimulatedGate().enabled is True

    def test_a_manager_defaulting_to_the_closed_gate_cannot_trade(
        self, live_settings: EnvironmentSettings
    ) -> None:
        """The default must be closed, so an unwired manager refuses rather than trades."""
        from stop_order_scalp.execution.order_manager import OrderManager
        from stop_order_scalp.infrastructure.config import (
            ExecutionSettings,
            OrderSettings,
            RiskSettings,
        )
        from stop_order_scalp.risk.risk_manager import RiskManager

        manager = OrderManager(
            ExecutionSettings(),
            OrderSettings(),
            RiskManager(RiskSettings(), TargetSettings()),
        )

        assert isinstance(manager.gate, OrderGate)
        assert manager.gate.enabled is False


class TestConfigurationConsistency:
    def test_live_without_allow_live_is_rejected_at_load(self) -> None:
        """Caught before it matters, not at the first order."""
        with pytest.raises(ConfigError, match="ALLOW_LIVE"):
            require_configured(
                EnvironmentSettings(environment=Environment.LIVE, allow_live=False)
            )

    def test_a_consistent_live_configuration_loads(self, live_settings: EnvironmentSettings) -> None:
        require_configured(live_settings)


class TestDescribeSafety:
    def test_names_every_switch(self, live_settings: EnvironmentSettings) -> None:
        text = describe_safety(live_settings, OrderGate(enabled=True), CloseGate())
        for expected in ("LIVE", "allow_live", "order_gate", "close_gate", "live_reachable"):
            assert expected in text

    def test_shows_a_closed_gate_as_closed(self, live_settings: EnvironmentSettings) -> None:
        assert "order_gate=False" in describe_safety(live_settings, OrderGate(), CloseGate())
