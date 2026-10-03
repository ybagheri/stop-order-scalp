"""Slippage, continuous price paths and the target trigger in the simulated venue.

``--slippage-points`` used to shift every candle by the same amount. A uniform translation of a
price series changes no profit, so the flag was a silent no-op: results at 0, 50 and 500 points
were identical. These tests pin the behaviour that replaced it -- slippage that is applied to
*fills*, and adverse only.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from stop_order_scalp.domain.exceptions import BrokerError
from stop_order_scalp.domain.models import EnvironmentSettings, TradePlan
from stop_order_scalp.execution.simulated_broker import SimulatedBroker
from stop_order_scalp.infrastructure.clock import FixedClock

from .test_simulated_broker import NOW, make_intent

ONE = Decimal("1.0")


def _venue(
    settings: EnvironmentSettings, *, slippage: Decimal = Decimal(0), continuous: bool = False
) -> SimulatedBroker:
    broker = SimulatedBroker(
        settings, clock=FixedClock(NOW), slippage=slippage, continuous_path=continuous
    )
    broker.connect()
    broker.publish("US30", Decimal("39999.0"), Decimal("39999.5"), digits=1)
    return broker


def _open(broker: SimulatedBroker, plan: TradePlan) -> None:
    """Rest the order and cross it, without reaching the stop or the target."""
    broker.place_order(make_intent(plan))
    price = plan.entry.value + Decimal("2.0")
    broker.publish("US30", price, price + Decimal("0.5"), digits=1)
    assert broker.positions(), "the order should have filled"


class TestSlippageIsAdverse:
    def test_an_entry_fills_worse_than_its_level(
        self, dry_run_settings: EnvironmentSettings, configured_venue: object, plan: TradePlan
    ) -> None:
        broker = _venue(dry_run_settings, slippage=ONE)
        _open(broker, plan)

        assert broker.positions()[0].entry.value == plan.entry.value + ONE

    def test_a_stop_out_exits_worse_than_the_quote(
        self, dry_run_settings: EnvironmentSettings, configured_venue: object, plan: TradePlan
    ) -> None:
        broker = _venue(dry_run_settings, slippage=ONE)
        _open(broker, plan)
        bid = plan.stop_loss.value - Decimal("5.0")

        broker.publish("US30", bid, bid + Decimal("0.5"), digits=1)

        trade = broker.history()[-1]
        assert trade.reason == "stop_loss"
        assert trade.exit_price.value == bid - ONE

    def test_a_target_exit_is_not_slipped(
        self, dry_run_settings: EnvironmentSettings, configured_venue: object, plan: TradePlan
    ) -> None:
        broker = _venue(dry_run_settings, slippage=ONE)
        _open(broker, plan)
        bid = plan.take_profit.value + Decimal("3.0")

        broker.publish("US30", bid, bid + Decimal("0.5"), digits=1)

        trade = broker.history()[-1]
        assert trade.reason == "take_profit"
        assert trade.exit_price.value == bid

    def test_favourable_slippage_is_refused(
        self, dry_run_settings: EnvironmentSettings, configured_venue: object
    ) -> None:
        with pytest.raises(BrokerError):
            SimulatedBroker(dry_run_settings, slippage=Decimal("-1"))


class TestContinuousPath:
    def test_a_stop_crossed_between_samples_fills_at_the_stop(
        self, dry_run_settings: EnvironmentSettings, configured_venue: object, plan: TradePlan
    ) -> None:
        """The next sample may be a whole bar's range below the stop; a real stop is not."""
        broker = _venue(dry_run_settings, continuous=True)
        _open(broker, plan)
        bid = plan.stop_loss.value - Decimal("50.0")

        broker.publish("US30", bid, bid + Decimal("0.5"), digits=1)

        assert broker.history()[-1].exit_price == plan.stop_loss

    def test_a_target_crossed_between_samples_fills_at_the_target(
        self, dry_run_settings: EnvironmentSettings, configured_venue: object, plan: TradePlan
    ) -> None:
        broker = _venue(dry_run_settings, continuous=True)
        _open(broker, plan)
        bid = plan.take_profit.value + Decimal("50.0")

        broker.publish("US30", bid, bid + Decimal("0.5"), digits=1)

        assert broker.history()[-1].exit_price == plan.take_profit

    def test_the_legacy_venue_still_fills_at_the_quote(
        self, dry_run_settings: EnvironmentSettings, configured_venue: object, plan: TradePlan
    ) -> None:
        broker = _venue(dry_run_settings, continuous=False)
        _open(broker, plan)
        bid = plan.stop_loss.value - Decimal("50.0")

        broker.publish("US30", bid, bid + Decimal("0.5"), digits=1)

        assert broker.history()[-1].exit_price.value == bid

    def test_slippage_stacks_on_the_level(
        self, dry_run_settings: EnvironmentSettings, configured_venue: object, plan: TradePlan
    ) -> None:
        broker = _venue(dry_run_settings, slippage=ONE, continuous=True)
        _open(broker, plan)
        bid = plan.stop_loss.value - Decimal("50.0")

        broker.publish("US30", bid, bid + Decimal("0.5"), digits=1)

        assert broker.history()[-1].exit_price.value == plan.stop_loss.value - ONE


class TestTargetTriggersOnTheExitSide:
    def test_a_long_target_is_not_hit_by_the_ask_alone(
        self, dry_run_settings: EnvironmentSettings, configured_venue: object, plan: TradePlan
    ) -> None:
        """A long closes by selling at the bid. The ask touching the target is not a fill."""
        broker = _venue(dry_run_settings)
        _open(broker, plan)
        target = plan.take_profit.value
        # ask is at the target, bid a full spread below it
        broker.publish("US30", target - Decimal("1.5"), target, digits=1)

        assert broker.positions(), "the target must wait for the bid"
        assert not broker.history()
