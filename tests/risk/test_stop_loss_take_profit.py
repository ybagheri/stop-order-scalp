"""Stop loss and take profit providers, and the precedence rule between targets.

The rounding assertions here are the load-bearing part. A stop rounded *toward* entry is
tighter protection than the risk engine approved, which means the approved size is too
large for the protection actually in place -- the real risk becomes a different number from
the one that was checked, and nothing raises.

The precedence test is the other: three sources can supply a target, and the rule is that a
better-defined answer wins rather than a configured mode overriding it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from risk.conftest import ENTRY
from stop_order_scalp.domain.enums import Side, TargetMode
from stop_order_scalp.domain.exceptions import RiskError
from stop_order_scalp.domain.value_objects import Price, SymbolSpecification
from stop_order_scalp.risk.stop_loss import (
    FixedPointsStopProvider,
    SignalStopProvider,
    default_stop_provider,
    stop_distance_points,
)
from stop_order_scalp.risk.take_profit import (
    FixedPointsTargetProvider,
    RiskRewardTargetProvider,
    SignalTargetProvider,
    default_target_provider,
    describe_precedence,
    resolve_target,
)

STOP_100 = Price.parse("39990.0", 1)
TARGET_100 = Price.parse("40100.0", 1)


# =============================================================================
# Stops
# =============================================================================


class TestFixedPointsStop:
    def test_a_buy_stop_sits_the_configured_distance_below(self, us30: SymbolSpecification) -> None:
        level = FixedPointsStopProvider(100).stop_for(ENTRY, Side.SIDE_BUY, us30)
        # 100 points x point 0.1 = 10.0 price units below entry.
        assert level.price == Price.parse("39990.0", 1)
        assert level.distance_points == Decimal("100")

    def test_a_sell_stop_sits_the_configured_distance_above(self, us30: SymbolSpecification) -> None:
        level = FixedPointsStopProvider(100).stop_for(ENTRY, Side.SIDE_SELL, us30)
        assert level.price == Price.parse("40010.0", 1)

    def test_the_distance_is_derived_from_the_specification_not_assumed(
        self, expensive_spec: SymbolSpecification
    ) -> None:
        # Same 100-point request on a point-0.01 instrument is 1.0 price unit, not 10.0.
        fine = SymbolSpecification(
            name="X", digits=2, point=Decimal("0.01"), tick_size=Decimal("0.01"),
            tick_value=Decimal("1.0"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"), volume_max=Decimal("50.0"), volume_step=Decimal("0.1"),
        )
        del expensive_spec
        level = FixedPointsStopProvider(100).stop_for(
            Price.parse("40000.00", 2), Side.SIDE_BUY, fine
        )
        assert level.price == Price.parse("39999.00", 2)
        assert level.distance_points == Decimal("100")

    def test_a_non_positive_distance_is_refused(self) -> None:
        with pytest.raises(RiskError, match="must be positive"):
            FixedPointsStopProvider(0)

    def test_the_source_names_the_distance(self, us30: SymbolSpecification) -> None:
        level = FixedPointsStopProvider(250).stop_for(ENTRY, Side.SIDE_BUY, us30)
        assert level.source == "fixed_points:250"

    def test_it_serialises(self, us30: SymbolSpecification) -> None:
        payload = FixedPointsStopProvider(100).stop_for(ENTRY, Side.SIDE_BUY, us30).as_dict()
        assert payload["side"] == "BUY"
        assert payload["rounded"] is False

    def test_the_default_provider_is_the_fixed_one(self) -> None:
        assert isinstance(default_stop_provider(100), FixedPointsStopProvider)


class TestStopRounding:
    """Rounding only bites when a price arrives off the tick grid.

    ``SymbolSpecification`` requires ``tick_size <= point``, and ``point`` is a whole
    multiple of the tick, so a whole number of points from an on-grid price always lands on
    the grid. These tests therefore feed an off-grid entry, which is the only way to reach
    the rounding branch -- and then assert the branch is *correct*, not merely present.
    """

    def test_a_buy_stop_rounds_down_away_from_entry(self) -> None:
        spec = SymbolSpecification(
            name="X", digits=2, point=Decimal("0.1"), tick_size=Decimal("0.1"),
            tick_value=Decimal("1.0"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"), volume_max=Decimal("50.0"), volume_step=Decimal("0.1"),
        )
        level = FixedPointsStopProvider(10).stop_for(
            Price.parse("40000.07", 2), Side.SIDE_BUY, spec
        )
        assert level.rounded is True
        # 40000.07 - 1.0 = 39999.07, floored onto the 0.1 grid.
        assert level.price.value == Decimal("39999.0")

    def test_a_sell_stop_rounds_up_away_from_entry(self) -> None:
        spec = SymbolSpecification(
            name="X", digits=2, point=Decimal("0.1"), tick_size=Decimal("0.1"),
            tick_value=Decimal("1.0"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"), volume_max=Decimal("50.0"), volume_step=Decimal("0.1"),
        )
        level = FixedPointsStopProvider(10).stop_for(
            Price.parse("40000.07", 2), Side.SIDE_SELL, spec
        )
        assert level.rounded is True
        assert level.price.value == Decimal("40001.1")

    def test_an_on_grid_price_does_not_round(self, us30: SymbolSpecification) -> None:
        level = FixedPointsStopProvider(100).stop_for(ENTRY, Side.SIDE_BUY, us30)
        assert level.rounded is False
        assert level.price == STOP_100

    def test_the_stop_rounding_is_not_the_entry_rounding(self, us30: SymbolSpecification) -> None:
        """The distinction Phase 3 found, pinned where it now matters.

        A stop is anchored on the entry and rounds away from it. An *entry* is anchored on
        the candle and rounds away from that. They are opposite, and using one for the
        other silently moves a level a tick the wrong way.
        """
        raw = Decimal("39990.07")
        assert us30.round_stop_price(raw, Side.SIDE_BUY).value == Decimal("39990.0")
        assert us30.round_entry_price(raw, Side.SIDE_BUY).value == Decimal("39990.1")


class TestSignalStop:
    def test_a_supplied_stop_is_used(self, us30: SymbolSpecification) -> None:
        provider = SignalStopProvider(100, FixedPointsStopProvider(100))
        level = provider.stop_for(
            ENTRY, Side.SIDE_BUY, us30, supplied=Price.parse("39950.0", 1)
        )
        assert level.price == Price.parse("39950.0", 1)
        assert level.source == "signal_defined"

    def test_without_one_the_fallback_is_used(self, us30: SymbolSpecification) -> None:
        provider = SignalStopProvider(100, FixedPointsStopProvider(100))
        level = provider.stop_for(ENTRY, Side.SIDE_BUY, us30)
        assert level.price == STOP_100
        assert level.source == "fixed_points:100"

    def test_a_stop_above_a_buy_entry_is_refused(self, us30: SymbolSpecification) -> None:
        """It would be a target, not a stop, and silently inverting it is worse than failing."""
        provider = SignalStopProvider(100, FixedPointsStopProvider(100))
        with pytest.raises(RiskError, match="wrong side"):
            provider.stop_for(ENTRY, Side.SIDE_BUY, us30, supplied=Price.parse("40050.0", 1))

    def test_a_stop_on_the_entry_is_refused(self, us30: SymbolSpecification) -> None:
        provider = SignalStopProvider(100, FixedPointsStopProvider(100))
        with pytest.raises(RiskError, match="no protection"):
            provider.stop_for(ENTRY, Side.SIDE_BUY, us30, supplied=ENTRY)


class TestStopDistance:
    def test_it_measures_the_distance_in_points(self, us30: SymbolSpecification) -> None:
        assert stop_distance_points(ENTRY, STOP_100, Side.SIDE_BUY, us30) == Decimal("100")

    def test_a_stop_on_the_wrong_side_is_refused(self, us30: SymbolSpecification) -> None:
        with pytest.raises(RiskError, match="not protective"):
            stop_distance_points(ENTRY, Price.parse("40050.0", 1), Side.SIDE_BUY, us30)


# =============================================================================
# Targets
# =============================================================================


class TestFixedPointsTarget:
    def test_a_buy_target_sits_the_configured_distance_above(self, us30: SymbolSpecification) -> None:
        level = FixedPointsTargetProvider(1000).target_for(ENTRY, Side.SIDE_BUY, us30)
        # 1000 points x 0.1 = 100.0 price units above entry.
        assert level.price == Price.parse("40100.0", 1)
        assert level.distance_points == Decimal("1000")

    def test_a_sell_target_sits_the_configured_distance_below(self, us30: SymbolSpecification) -> None:
        level = FixedPointsTargetProvider(1000).target_for(ENTRY, Side.SIDE_SELL, us30)
        assert level.price == Price.parse("39900.0", 1)

    def test_it_reports_the_baseline_mode(self, us30: SymbolSpecification) -> None:
        level = FixedPointsTargetProvider(1000).target_for(ENTRY, Side.SIDE_BUY, us30)
        assert level.mode is TargetMode.TARGET_MODE_FIXED_POINTS

    def test_a_non_positive_distance_is_refused(self) -> None:
        with pytest.raises(RiskError, match="must be positive"):
            FixedPointsTargetProvider(0)


class TestTargetRounding:
    def test_a_buy_target_rounds_up_away_from_entry(self) -> None:
        spec = SymbolSpecification(
            name="X", digits=2, point=Decimal("0.1"), tick_size=Decimal("0.1"),
            tick_value=Decimal("1.0"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"), volume_max=Decimal("50.0"), volume_step=Decimal("0.1"),
        )
        level = FixedPointsTargetProvider(10).target_for(
            Price.parse("40000.07", 2), Side.SIDE_BUY, spec
        )
        assert level.rounded is True
        # 40000.07 + 1.0 = 40001.07, ceiled onto the 0.1 grid.
        assert level.price.value == Decimal("40001.1")


class TestRiskRewardTarget:
    def test_one_to_one_gives_a_target_as_far_as_the_stop(self, us30: SymbolSpecification) -> None:
        provider = RiskRewardTargetProvider(Decimal("1.0"), 1000)
        level = provider.target_from_stop(ENTRY, Side.SIDE_BUY, STOP_100, us30)
        # Stop is 100 points away, so a 1:1 target is also 100 points away.
        assert level.distance_points == Decimal("100")
        assert level.price == Price.parse("40010.0", 1)

    def test_two_to_one_doubles_the_distance(self, us30: SymbolSpecification) -> None:
        provider = RiskRewardTargetProvider(Decimal("2.0"), 1000)
        level = provider.target_from_stop(ENTRY, Side.SIDE_BUY, STOP_100, us30)
        assert level.distance_points == Decimal("200")

    def test_without_a_stop_it_falls_back_to_fixed_points(self, us30: SymbolSpecification) -> None:
        provider = RiskRewardTargetProvider(Decimal("1.0"), 1000)
        level = provider.target_for(ENTRY, Side.SIDE_BUY, us30)
        assert level.source == "fixed_points:1000"

    def test_the_baseline_is_ten_to_one_not_one_to_one(self, us30: SymbolSpecification) -> None:
        """1000 points of target against a 100-point stop is 10:1. Worth stating."""
        target = FixedPointsTargetProvider(1000).target_for(ENTRY, Side.SIDE_BUY, us30)
        stop = FixedPointsStopProvider(100).stop_for(ENTRY, Side.SIDE_BUY, us30)
        ratio = target.distance_points / stop.distance_points
        assert ratio == Decimal("10")

    def test_a_non_positive_ratio_is_refused(self) -> None:
        with pytest.raises(RiskError, match="risk_reward must be positive"):
            RiskRewardTargetProvider(Decimal("0"), 1000)


class TestSignalTarget:
    def test_a_supplied_target_is_used(self, us30: SymbolSpecification) -> None:
        provider = SignalTargetProvider(1000, FixedPointsTargetProvider(1000))
        level = provider.target_for(
            ENTRY, Side.SIDE_BUY, us30, supplied=Price.parse("40050.0", 1)
        )
        assert level.price == Price.parse("40050.0", 1)
        assert level.mode is TargetMode.TARGET_MODE_SIGNAL_DEFINED

    def test_a_target_on_the_wrong_side_is_refused(self, us30: SymbolSpecification) -> None:
        provider = SignalTargetProvider(1000, FixedPointsTargetProvider(1000))
        with pytest.raises(RiskError, match="wrong side"):
            provider.target_for(ENTRY, Side.SIDE_BUY, us30, supplied=Price.parse("39950.0", 1))

    def test_a_target_at_the_entry_is_refused(self, us30: SymbolSpecification) -> None:
        provider = SignalTargetProvider(1000, FixedPointsTargetProvider(1000))
        with pytest.raises(RiskError, match="not a target"):
            provider.target_for(ENTRY, Side.SIDE_BUY, us30, supplied=ENTRY)


# =============================================================================
# Precedence
# =============================================================================


class TestPrecedence:
    def test_a_signal_target_beats_a_configured_mode(self, us30: SymbolSpecification) -> None:
        configured = default_target_provider(
            TargetMode.TARGET_MODE_FIXED_POINTS,
            take_profit_points=1000,
            risk_reward=Decimal("1.0"),
        )
        level = resolve_target(
            entry=ENTRY,
            side=Side.SIDE_BUY,
            stop=STOP_100,
            specification=us30,
            configured=configured,
            supplied=Price.parse("40050.0", 1),
        )
        assert level.price == Price.parse("40050.0", 1)

    def test_a_ratio_beats_fixed_points_when_a_stop_is_known(self, us30: SymbolSpecification) -> None:
        configured = RiskRewardTargetProvider(Decimal("1.0"), 1000)
        level = resolve_target(
            entry=ENTRY,
            side=Side.SIDE_BUY,
            stop=STOP_100,
            specification=us30,
            configured=configured,
        )
        assert level.distance_points == Decimal("100")

    def test_fixed_points_is_the_fallback(self, us30: SymbolSpecification) -> None:
        configured = FixedPointsTargetProvider(1000)
        level = resolve_target(
            entry=ENTRY,
            side=Side.SIDE_BUY,
            stop=STOP_100,
            specification=us30,
            configured=configured,
        )
        assert level.distance_points == Decimal("1000")

    def test_the_rule_is_stated_in_one_place(self) -> None:
        assert describe_precedence() == "signal_defined > risk_reward > fixed_points"

    def test_each_mode_maps_to_its_provider(self) -> None:
        assert isinstance(
            default_target_provider(
                TargetMode.TARGET_MODE_FIXED_POINTS,
                take_profit_points=1000,
                risk_reward=Decimal("1.0"),
            ),
            FixedPointsTargetProvider,
        )
        assert isinstance(
            default_target_provider(
                TargetMode.TARGET_MODE_RISK_REWARD,
                take_profit_points=1000,
                risk_reward=Decimal("1.0"),
            ),
            RiskRewardTargetProvider,
        )
        assert isinstance(
            default_target_provider(
                TargetMode.TARGET_MODE_SIGNAL_DEFINED,
                take_profit_points=1000,
                risk_reward=Decimal("1.0"),
            ),
            SignalTargetProvider,
        )


# =============================================================================
# Properties
# =============================================================================


class TestLevelProperties:
    @given(
        entry=st.decimals(min_value=1000, max_value=100000, places=1),
        points=st.integers(min_value=1, max_value=5000),
        side=st.sampled_from([Side.SIDE_BUY, Side.SIDE_SELL]),
    )
    def test_a_stop_is_always_on_the_protective_side(
        self, entry: Decimal, points: int, side: Side
    ) -> None:
        spec = SymbolSpecification(
            name="X", digits=1, point=Decimal("0.1"), tick_size=Decimal("0.1"),
            tick_value=Decimal("1.0"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"), volume_max=Decimal("50.0"), volume_step=Decimal("0.1"),
        )
        price = Price.parse(str(entry), 1)
        level = FixedPointsStopProvider(points).stop_for(price, side, spec)
        if side is Side.SIDE_BUY:
            assert level.price < price
        else:
            assert level.price > price

    @given(
        entry=st.decimals(min_value=1000, max_value=100000, places=1),
        points=st.integers(min_value=1, max_value=5000),
        side=st.sampled_from([Side.SIDE_BUY, Side.SIDE_SELL]),
    )
    def test_a_target_is_always_on_the_profitable_side(
        self, entry: Decimal, points: int, side: Side
    ) -> None:
        spec = SymbolSpecification(
            name="X", digits=1, point=Decimal("0.1"), tick_size=Decimal("0.1"),
            tick_value=Decimal("1.0"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"), volume_max=Decimal("50.0"), volume_step=Decimal("0.1"),
        )
        price = Price.parse(str(entry), 1)
        level = FixedPointsTargetProvider(points).target_for(price, side, spec)
        if side is Side.SIDE_BUY:
            assert level.price > price
        else:
            assert level.price < price

    @given(
        entry=st.decimals(min_value=1000, max_value=100000, places=1),
        points=st.integers(min_value=1, max_value=5000),
    )
    def test_rounding_never_makes_a_stop_tighter(
        self, entry: Decimal, points: int
    ) -> None:
        """A tighter stop than requested is the failure that matters."""
        spec = SymbolSpecification(
            name="X", digits=2, point=Decimal("0.1"), tick_size=Decimal("0.03"),
            tick_value=Decimal("1.0"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"), volume_max=Decimal("50.0"), volume_step=Decimal("0.1"),
        )
        price = Price.parse(str(entry), 2)
        level = FixedPointsStopProvider(points).stop_for(price, Side.SIDE_BUY, spec)
        assert level.price <= price
        assert level.distance_points >= Decimal(points) - Decimal(1)

    @given(
        entry=st.decimals(min_value=1000, max_value=100000, places=1),
        points=st.integers(min_value=1, max_value=5000),
    )
    def test_the_distance_is_always_positive(self, entry: Decimal, points: int) -> None:
        spec = SymbolSpecification(
            name="X", digits=1, point=Decimal("0.1"), tick_size=Decimal("0.1"),
            tick_value=Decimal("1.0"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"), volume_max=Decimal("50.0"), volume_step=Decimal("0.1"),
        )
        price = Price.parse(str(entry), 1)
        for level in (
            FixedPointsStopProvider(points).stop_for(price, Side.SIDE_BUY, spec),
            FixedPointsTargetProvider(points).target_for(price, Side.SIDE_BUY, spec),
        ):
            assert level.distance_points > 0
