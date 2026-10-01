"""The trailing rule itself: ``bid - d`` for a BUY, ``ask + d`` for a SELL.

Break-even has its own file. What is left here is the level arithmetic, the arming gate, the
minimum step, and the monotonicity guard stated on its own rather than as a property.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from stop_order_scalp.domain.enums import Side
from stop_order_scalp.domain.value_objects import SymbolSpecification
from stop_order_scalp.infrastructure.config import TrailingSettings
from stop_order_scalp.trailing.trailing_stop import TrailingRefusal, TrailingStopProvider

from .conftest import position, tick


class TestLevel:
    def test_a_buy_level_is_the_bid_minus_the_distance(
        self, trailing: TrailingStopProvider, us30: SymbolSpecification
    ) -> None:
        level = trailing.trailing_level(us30, tick("40200.0"), Side.SIDE_BUY)

        # 40200.0 - 100 points (10.0 price units on point = 0.1).
        assert level.value == Decimal("40190.0")

    def test_a_sell_level_is_the_ask_plus_the_distance(
        self, trailing: TrailingStopProvider, us30: SymbolSpecification
    ) -> None:
        level = trailing.trailing_level(us30, tick("39800.0", "39800.1"), Side.SIDE_SELL)

        assert level.value == Decimal("39810.1")

    def test_a_buy_level_uses_the_bid_not_the_mid(
        self, trailing: TrailingStopProvider, us30: SymbolSpecification
    ) -> None:
        """Using the mid would place the stop half a spread closer than reality."""
        observation = tick("40200.0", "40201.0")

        level = trailing.trailing_level(us30, observation, Side.SIDE_BUY)

        assert level.value == Decimal("40190.0"), "the ask must not enter a BUY's level"

    def test_the_distance_is_configurable(
        self, us30: SymbolSpecification
    ) -> None:
        provider = TrailingStopProvider(TrailingSettings(distance_points=250))

        level = provider.trailing_level(us30, tick("40200.0"), Side.SIDE_BUY)

        assert level.value == Decimal("40175.0")

    def test_the_level_lands_on_the_tick_grid(
        self, trailing: TrailingStopProvider, us30: SymbolSpecification
    ) -> None:
        level = trailing.trailing_level(us30, tick("40200.0", "40200.05"), Side.SIDE_SELL)

        assert us30.is_on_tick_grid(level)

    def test_a_sell_level_rounds_up_not_down(
        self, us30: SymbolSpecification
    ) -> None:
        """Rounding away from entry means up for a SELL, so the trailing distance is never
        tighter than configured."""
        provider = TrailingStopProvider(TrailingSettings(distance_points=100))

        level = provider.trailing_level(us30, tick("39800.0", "39800.03"), Side.SIDE_SELL)

        # 39800.03 + 10.0 = 39810.03, which must round up to 39810.1.
        assert level.value == Decimal("39810.1")


class TestArming:
    def test_it_does_not_arm_before_arm_after_points(
        self, us30: SymbolSpecification
    ) -> None:
        """A bid of 40100.0 against an entry of 40000.0 is 100 price units.

        With ``point = 0.1`` that is **1000 points**, not 100 -- a factor of ten that is easy
        to get wrong when reading the settings by eye, and the reason the threshold here is
        3000 rather than 300.
        """
        provider = TrailingStopProvider(
            TrailingSettings(distance_points=100, arm_after_points=3000)
        )

        outcome = provider.evaluate(position(stop="40000.0"), tick("40100.0"), us30)

        assert outcome.favourable_points == Decimal(1000)
        assert outcome.code == TrailingRefusal.NOT_ARMED

    def test_it_arms_at_the_configured_threshold(
        self, us30: SymbolSpecification
    ) -> None:
        provider = TrailingStopProvider(
            TrailingSettings(distance_points=100, arm_after_points=1000)
        )

        outcome = provider.evaluate(position(stop="40000.0"), tick("40100.0"), us30)

        assert outcome.favourable_points == Decimal(1000)
        assert outcome.proposed

    def test_a_disabled_trailer_never_proposes(self, us30: SymbolSpecification) -> None:
        provider = TrailingStopProvider(
            TrailingSettings(enabled=False, distance_points=100)
        )

        outcome = provider.evaluate(position(stop="40000.0"), tick("40500.0"), us30)

        assert outcome.code == TrailingRefusal.DISABLED
        assert outcome.armed is False

    def test_a_position_without_a_stop_cannot_trail(
        self, trailing: TrailingStopProvider, us30: SymbolSpecification
    ) -> None:
        outcome = trailing.evaluate(position(stop=None), tick("40200.0"), us30)

        assert outcome.code == TrailingRefusal.NO_STOP_IN_PLACE
        assert outcome.armed is False

    def test_arm_after_zero_trails_from_the_first_tick(
        self, trailing: TrailingStopProvider, us30: SymbolSpecification
    ) -> None:
        """The baseline: trailing is live from the start, unlike break-even."""
        outcome = trailing.evaluate(position(stop="39950.0"), tick("40000.5"), us30)

        assert outcome.proposed


class TestMonotonicity:
    def test_a_buy_level_below_the_current_stop_is_refused(
        self, trailing: TrailingStopProvider, us30: SymbolSpecification
    ) -> None:
        """The guard that stops a pullback walking the stop down through entry."""
        outcome = trailing.evaluate(position(stop="40000.0"), tick("40001.0"), us30)

        assert outcome.code == TrailingRefusal.NOT_MONOTONIC

    def test_a_sell_level_above_the_current_stop_is_refused(
        self, trailing: TrailingStopProvider, us30: SymbolSpecification
    ) -> None:
        short = position(Side.SIDE_SELL, entry="40000.0", stop="40000.0")

        outcome = trailing.evaluate(short, tick("39999.0", "39999.1"), us30)

        assert outcome.code == TrailingRefusal.NOT_MONOTONIC

    def test_an_equal_level_is_not_an_improvement(
        self, trailing: TrailingStopProvider, us30: SymbolSpecification
    ) -> None:
        """Proposing a stop identical to the one in place would be a modify that changes
        nothing -- one request per tick for the life of the position."""
        outcome = trailing.evaluate(position(stop="40190.0"), tick("40200.0"), us30)

        assert not outcome.proposed

    def test_the_refusal_still_reports_the_computed_level(
        self, trailing: TrailingStopProvider, us30: SymbolSpecification
    ) -> None:
        """`status` wants to show where the rule points even when nothing may move."""
        outcome = trailing.evaluate(position(stop="40000.0"), tick("40001.0"), us30)

        assert outcome.trailing_level is not None
        assert outcome.trailing_level.value == Decimal("39991.0")


class TestMinimumStep:
    def test_a_move_below_the_minimum_is_refused(
        self, us30: SymbolSpecification
    ) -> None:
        provider = TrailingStopProvider(
            TrailingSettings(distance_points=100, min_step_points=500)
        )

        outcome = provider.evaluate(position(stop="40050.0"), tick("40100.0"), us30)

        assert outcome.code == TrailingRefusal.BELOW_MIN_STEP

    def test_a_move_at_or_above_the_minimum_is_accepted(
        self, us30: SymbolSpecification
    ) -> None:
        provider = TrailingStopProvider(
            TrailingSettings(distance_points=100, min_step_points=400)
        )

        outcome = provider.evaluate(position(stop="40050.0"), tick("40100.0"), us30)

        assert outcome.proposed

    def test_the_default_minimum_is_one_point(
        self, us30: SymbolSpecification
    ) -> None:
        """The smallest move the broker could distinguish at all."""
        provider = TrailingStopProvider(TrailingSettings(distance_points=100))

        # 39900.0 -> 39990.0 is 900 points, far above the default minimum.
        outcome = provider.evaluate(position(stop="39900.0"), tick("40000.0"), us30)

        assert outcome.proposed


class TestBrokerLimits:
    def test_a_level_inside_stops_level_is_refused(
        self, us30: SymbolSpecification
    ) -> None:
        """stops_level is 10 points. Bid 40100.0 and a level of 40090.0 are 100 points apart,
        which clears it; the tight instrument below does not."""
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
        provider = TrailingStopProvider(TrailingSettings(distance_points=100))

        outcome = provider.evaluate(position(stop="40050.0"), tick("40100.0"), blocked)

        assert outcome.code == TrailingRefusal.VIOLATES_STOPS_LEVEL

    def test_a_frozen_position_is_refused(self) -> None:
        """freeze_level locks an existing position once price nears its stop."""
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
        provider = TrailingStopProvider(TrailingSettings(distance_points=100))

        outcome = provider.evaluate(position(stop="40050.0"), tick("40100.0"), frozen)

        assert outcome.code == TrailingRefusal.INSIDE_FREEZE_LEVEL


class TestDecisionContract:
    def test_require_raises_when_nothing_was_proposed(
        self, trailing: TrailingStopProvider, us30: SymbolSpecification
    ) -> None:
        outcome = trailing.evaluate(position(stop="40000.0"), tick("40001.0"), us30)

        with pytest.raises(ValueError, match="trailing_not_monotonic"):
            outcome.require()

    def test_a_proposal_records_its_reason_and_distance(
        self, trailing: TrailingStopProvider, us30: SymbolSpecification
    ) -> None:
        outcome = trailing.evaluate(position(stop="39900.0"), tick("40200.0"), us30)

        assert outcome.require().reason == "trailing_2000pts"
        assert outcome.require().distance_points == Decimal(2000)

    def test_every_refusal_is_distinguishable(self) -> None:
        """A refusal that only says "no" forces every caller to parse prose."""
        codes = [
            value
            for name, value in vars(TrailingRefusal).items()
            if not name.startswith("_") and isinstance(value, str)
        ]

        assert len(codes) == len(set(codes))
        assert len(codes) >= 6
