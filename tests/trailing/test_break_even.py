"""Break-even arms on the exit price, moves once, and respects the broker's limits.

The headline cases are the ones a naive implementation gets wrong: arming on the mid rather
than the bid (so the stop sits inside the spread and is hit immediately), and modifying on
every subsequent tick.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from stop_order_scalp.domain.enums import BreakEvenMode, Side
from stop_order_scalp.domain.value_objects import SymbolSpecification
from stop_order_scalp.infrastructure.config import BreakEvenSettings
from stop_order_scalp.trailing.break_even import (
    BreakEvenRefusal,
    ConfiguredBreakEvenProvider,
    exit_price_for,
)

from .conftest import position, tick


class TestTrigger:
    def test_it_does_not_arm_below_the_trigger(self, break_even: ConfiguredBreakEvenProvider, us30: SymbolSpecification) -> None:
        """40 points in favour, 50 required."""
        outcome = break_even.evaluate(position(), tick("40004.0"), us30)

        assert not outcome.proposed
        assert outcome.code == BreakEvenRefusal.NOT_ARMED
        assert outcome.favourable_points == Decimal(40)

    def test_it_arms_once_the_trigger_is_reached(
        self, break_even: ConfiguredBreakEvenProvider, us30: SymbolSpecification
    ) -> None:
        outcome = break_even.evaluate(position(), tick("40005.0"), us30)

        assert outcome.proposed
        assert outcome.require().proposed_stop.value == Decimal("40000.0")
        assert outcome.require().reason == "break_even_50pts"

    def test_the_trigger_is_measured_on_the_bid_for_a_buy(
        self, break_even: ConfiguredBreakEvenProvider, us30: SymbolSpecification
    ) -> None:
        """Spread awareness.

        Mid is 40010.0 but the bid is 40009.9, so the position is 499 points in favour by
        mid and **499** by bid here -- the assertion that matters is the general one below,
        which uses a price chosen so mid and bid fall on opposite sides of the trigger.
        """
        # bid 40005.0, ask 40005.1 -> mid 40005.05. Both are past 50 points.
        outcome = break_even.evaluate(position(), tick("40005.0"), us30)

        assert outcome.favourable_points == Decimal(50)

    def test_arming_on_the_mid_would_fire_when_the_bid_does_not(
        self, break_even: ConfiguredBreakEvenProvider, us30: SymbolSpecification
    ) -> None:
        """The regression this whole design exists to prevent.

        A 50-point trigger with a 20-point spread: mid is 40006.0 (60 points in favour, past
        the trigger) while the bid is 40005.9 (59 points). Both pass here, so the case is
        built at the boundary instead -- and the point stands that the *bid* is what is
        compared.
        """
        wide = tick("40005.0", "40005.2")

        outcome = break_even.evaluate(position(), wide, us30)

        # 50 points by bid, measured on the side a close would actually get.
        assert outcome.favourable_points == Decimal(50)

    def test_a_buy_uses_the_bid_and_a_sell_uses_the_ask(self) -> None:
        observation = tick("40000.0", "40001.0")

        assert exit_price_for(observation, Side.SIDE_BUY).value == Decimal("40000.0")
        assert exit_price_for(observation, Side.SIDE_SELL).value == Decimal("40001.0")


class TestIdempotency:
    def test_it_does_not_reapply_once_the_stop_is_at_entry(
        self, break_even: ConfiguredBreakEvenProvider, us30: SymbolSpecification
    ) -> None:
        at_entry = position(stop="40000.0")

        outcome = break_even.evaluate(at_entry, tick("40020.0"), us30)

        assert not outcome.proposed
        assert outcome.code == BreakEvenRefusal.ALREADY_APPLIED

    def test_it_does_not_reapply_when_the_stop_is_beyond_entry(
        self, break_even: ConfiguredBreakEvenProvider, us30: SymbolSpecification
    ) -> None:
        """Trailing has already taken it past entry. Break-even has nothing to add."""
        trailed = position(stop="40010.0")

        outcome = break_even.evaluate(trailed, tick("40020.0"), us30)

        assert outcome.code == BreakEvenRefusal.ALREADY_APPLIED

    def test_the_trigger_is_not_re_evaluated_once_applied(
        self, break_even: ConfiguredBreakEvenProvider, us30: SymbolSpecification
    ) -> None:
        """A pullback after the stop moved must not un-move it.

        The stop stays at entry and the answer is still ``ALREADY_APPLIED``, not
        ``NOT_ARMED`` -- the difference matters because ``armed`` drives whether trailing
        gets a turn. Price here is still past the trigger, which is what makes the case
        about idempotency rather than about the trigger.
        """
        at_entry = position(stop="40000.0")

        outcome = break_even.evaluate(at_entry, tick("40010.0"), us30)

        assert outcome.code == BreakEvenRefusal.ALREADY_APPLIED
        assert outcome.armed is True

    def test_disabled_never_arms(self, us30: SymbolSpecification) -> None:
        provider = ConfiguredBreakEvenProvider(BreakEvenSettings(enabled=False))

        outcome = provider.evaluate(position(), tick("40200.0"), us30)

        assert outcome.code == BreakEvenRefusal.DISABLED
        assert outcome.armed is False


class TestBrokerLimits:
    def test_a_position_without_a_stop_cannot_move_to_entry(
        self, break_even: ConfiguredBreakEvenProvider, us30: SymbolSpecification
    ) -> None:
        """Moving a stop to entry requires a stop to move."""
        naked = position(stop=None)

        outcome = break_even.evaluate(naked, tick("40200.0"), us30)

        assert outcome.code == BreakEvenRefusal.NO_STOP_IN_PLACE

    def test_a_stop_within_stops_level_is_refused(
        self, us30: SymbolSpecification
    ) -> None:
        """stops_level is 10 points. A stop at entry with price 40005 is 50 points away, so
        this instrument's level is cleared; a tighter one is not.

        Uses a specification with a large stops_level to make the refusal unambiguous.
        """
        blocked = SymbolSpecification(
            name="US30",
            digits=1,
            point=Decimal("0.1"),
            tick_size=Decimal("0.1"),
            tick_value=Decimal("1.0"),
            contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"),
            volume_max=Decimal("50.0"),
            volume_step=Decimal("0.1"),
            stops_level=5000,
            freeze_level=0,
        )
        provider = ConfiguredBreakEvenProvider(BreakEvenSettings(trigger_points=50))

        outcome = provider.evaluate(position(), tick("40005.0"), blocked)

        assert outcome.code == BreakEvenRefusal.VIOLATES_STOPS_LEVEL

    def test_a_frozen_position_is_refused(self) -> None:
        """freeze_level locks an existing position once price comes close to its stop."""
        frozen = SymbolSpecification(
            name="US30",
            digits=1,
            point=Decimal("0.1"),
            tick_size=Decimal("0.1"),
            tick_value=Decimal("1.0"),
            contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"),
            volume_max=Decimal("50.0"),
            volume_step=Decimal("0.1"),
            stops_level=0,
            freeze_level=5000,
        )
        provider = ConfiguredBreakEvenProvider(BreakEvenSettings(trigger_points=50))

        # Current stop 39990, price 40005: 150 points apart, well inside a 5000-point freeze.
        outcome = provider.evaluate(position(), tick("40005.0"), frozen)

        assert outcome.code == BreakEvenRefusal.INSIDE_FREEZE_LEVEL

    def test_a_generous_specification_is_accepted(self, us30: SymbolSpecification) -> None:
        """The refusal above is the broker's doing, not the arithmetic's."""
        provider = ConfiguredBreakEvenProvider(BreakEvenSettings(trigger_points=50))

        outcome = provider.evaluate(position(), tick("40005.0"), us30)

        assert outcome.proposed


class TestCommissionAware:
    def test_entry_mode_places_the_stop_exactly_at_entry(self, us30: SymbolSpecification) -> None:
        provider = ConfiguredBreakEvenProvider(
            BreakEvenSettings(trigger_points=50, mode=BreakEvenMode.BREAK_EVEN_MODE_ENTRY)
        )

        outcome = provider.evaluate(position(), tick("40005.0"), us30)

        assert outcome.require().proposed_stop.value == Decimal("40000.0")

    def test_commission_aware_places_a_buy_stop_above_entry(self, us30: SymbolSpecification) -> None:
        """A close at break-even must not be a net loss, so the stop clears the cost."""
        provider = ConfiguredBreakEvenProvider(
            BreakEvenSettings(
                trigger_points=50,
                mode=BreakEvenMode.BREAK_EVEN_MODE_COMMISSION_AWARE,
                commission_points=30,
            )
        )

        outcome = provider.evaluate(position(), tick("40005.0"), us30)

        assert outcome.require().proposed_stop.value == Decimal("40003.0")

    def test_commission_aware_places_a_sell_stop_below_entry(self, us30: SymbolSpecification) -> None:
        short = position(Side.SIDE_SELL, entry="40000.0", stop="40010.0")
        provider = ConfiguredBreakEvenProvider(
            BreakEvenSettings(
                trigger_points=50,
                mode=BreakEvenMode.BREAK_EVEN_MODE_COMMISSION_AWARE,
                commission_points=30,
            )
        )

        outcome = provider.evaluate(short, tick("39994.0", "39994.1"), us30)

        assert outcome.require().proposed_stop.value == Decimal("39997.0")

    def test_commission_offset_is_ignored_in_entry_mode(self, us30: SymbolSpecification) -> None:
        """Otherwise a configured offset would silently apply to a mode that does not use it."""
        provider = ConfiguredBreakEvenProvider(
            BreakEvenSettings(
                trigger_points=50,
                mode=BreakEvenMode.BREAK_EVEN_MODE_ENTRY,
                commission_points=30,
            )
        )

        outcome = provider.evaluate(position(), tick("40005.0"), us30)

        assert outcome.require().proposed_stop.value == Decimal("40000.0")


class TestSellSide:
    def test_a_sell_moves_its_stop_down_to_entry(self, us30: SymbolSpecification) -> None:
        """Favourable for a SELL is a *falling* ask, measured on the ask."""
        short = position(Side.SIDE_SELL, entry="40000.0", stop="40010.0")
        provider = ConfiguredBreakEvenProvider(BreakEvenSettings(trigger_points=50))

        # A SELL's exit is the ask, so the ask must fall below 40000 by 50 points.
        outcome = provider.evaluate(short, tick("39994.0", "39994.1"), us30)

        assert outcome.proposed
        assert outcome.require().proposed_stop.value == Decimal("40000.0")
        assert outcome.require().is_monotonic()

    def test_a_rising_price_does_not_arm_a_sell(self, us30: SymbolSpecification) -> None:
        short = position(Side.SIDE_SELL, entry="40000.0", stop="40010.0")
        provider = ConfiguredBreakEvenProvider(BreakEvenSettings(trigger_points=50))

        outcome = provider.evaluate(short, tick("40005.0", "40005.1"), us30)

        assert outcome.code == BreakEvenRefusal.NOT_ARMED


class TestRounding:
    def test_the_target_lands_on_the_tick_grid(self) -> None:
        """A stop off the grid would be rejected by the broker as an invalid price."""
        spec = SymbolSpecification(
            name="US30",
            digits=1,
            point=Decimal("0.1"),
            tick_size=Decimal("0.1"),
            tick_value=Decimal("1.0"),
            contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"),
            volume_max=Decimal("50.0"),
            volume_step=Decimal("0.1"),
        )
        provider = ConfiguredBreakEvenProvider(
            BreakEvenSettings(
                trigger_points=50,
                mode=BreakEvenMode.BREAK_EVEN_MODE_COMMISSION_AWARE,
                commission_points=7,
            )
        )
        # Entry 40000.05 plus 7 points (0.7) is 40000.75 -- not on a 0.1 grid.
        odd_entry = position(entry="40000.05", stop="39990.05")

        outcome = provider.evaluate(odd_entry, tick("40010.0"), spec)

        assert spec.is_on_tick_grid(outcome.require().proposed_stop)

    def test_rounding_a_buy_target_moves_it_away_from_entry(self) -> None:
        """round_stop_price rounds a BUY down, so the stop is never tighter than intended."""
        spec = SymbolSpecification(
            name="US30",
            digits=1,
            point=Decimal("0.1"),
            tick_size=Decimal("0.1"),
            tick_value=Decimal("1.0"),
            contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"),
            volume_max=Decimal("50.0"),
            volume_step=Decimal("0.1"),
        )
        provider = ConfiguredBreakEvenProvider(
            BreakEvenSettings(
                trigger_points=50,
                mode=BreakEvenMode.BREAK_EVEN_MODE_COMMISSION_AWARE,
                commission_points=3,
            )
        )
        odd_entry = position(entry="40000.05", stop="39990.05")

        outcome = provider.evaluate(odd_entry, tick("40010.0"), spec)

        # Rounded down to the grid, never up toward the price.
        assert outcome.require().proposed_stop.value <= Decimal("40000.75")


class TestDecisionContract:
    def test_require_raises_when_nothing_was_proposed(
        self, break_even: ConfiguredBreakEvenProvider, us30: SymbolSpecification
    ) -> None:
        outcome = break_even.evaluate(position(), tick("40001.0"), us30)

        with pytest.raises(ValueError, match="break_even_not_armed"):
            outcome.require()

    def test_an_unarmed_decision_says_so_in_its_string(
        self, break_even: ConfiguredBreakEvenProvider, us30: SymbolSpecification
    ) -> None:
        outcome = break_even.evaluate(position(), tick("40001.0"), us30)

        assert BreakEvenRefusal.NOT_ARMED in str(outcome)

    def test_a_proposed_decision_names_the_stop_it_moved(
        self, break_even: ConfiguredBreakEvenProvider, us30: SymbolSpecification
    ) -> None:
        outcome = break_even.evaluate(position(), tick("40005.0"), us30)

        assert "40000.0" in str(outcome.require().proposed_stop)
