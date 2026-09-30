"""Price, volume and symbol specification arithmetic.

The specification's hardest constraint on this code is: *do not assume 1 point = $1*.
These tests are where that constraint is made concrete. If they ever pass while the
broker's real tick value differs from the fixture's, the risk engine is wrong -- not the
fixture.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from stop_order_scalp.domain.enums import Side
from stop_order_scalp.domain.exceptions import DomainError, InvalidSpecificationError
from stop_order_scalp.domain.value_objects import Money, Price, SymbolSpecification, Volume


class TestPrice:
    def test_price_parses_from_text_without_float_error(self) -> None:
        assert Price.parse("40000.1").value == Decimal("40000.1")

    def test_price_refuses_naN(self) -> None:
        with pytest.raises(DomainError):
            Price.parse("NaN")

    def test_price_refuses_infinity(self) -> None:
        with pytest.raises(DomainError):
            Price.parse("Infinity")

    def test_price_refuses_bool(self) -> None:
        with pytest.raises(DomainError):
            Price.of(True)

    def test_arithmetic_yields_decimal_not_price(self) -> None:
        # A bare Decimal result cannot know the symbol's digit count, so it must not be
        # silently promoted back to a Price with the wrong digits.
        result = Price.parse("40000.0", 1) + Decimal("10.0")
        assert isinstance(result, Decimal)
        # Price and Decimal are disjoint classes, so the type checker already knows this
        # comparison is False. The assertion pins the runtime behaviour, which is the
        # actual regression this test exists to catch.
        assert not isinstance(result, Price)  # type: ignore[unreachable]

    def test_distance_is_unsigned(self) -> None:
        a = Price.parse("40000.0")
        b = Price.parse("39900.0")
        assert a.absolute_distance_to(b) == Decimal("100.0")
        assert b.absolute_distance_to(a) == Decimal("100.0")

    def test_rendering_uses_the_symbols_digit_count(self) -> None:
        assert str(Price.parse("40000.04", 1)) == "40000.0"
        assert str(Price.parse("40000.04", 2)) == "40000.04"

    def test_comparison_accepts_decimal_and_price(self) -> None:
        price = Price.parse("40000.0")
        assert price > Decimal("39999.9")
        assert price < Price.parse("40000.1")
        assert price >= 40000

    def test_comparison_refuses_a_price(self) -> None:
        with pytest.raises(DomainError):
            _ = Price.parse("1.0") < "1.0"  # type: ignore[operator]


class TestMoney:
    def test_money_is_distinct_from_price(self) -> None:
        assert not isinstance(Money.of(100), Price)

    def test_money_refuses_mixed_currencies(self) -> None:
        with pytest.raises(DomainError):
            Money.of(100, "USD") + Money.of(100, "EUR")

    def test_money_scales(self) -> None:
        assert Money.of(Decimal("6.00")).scaled(Decimal("2.5")) == Money.of(Decimal("15.00"))

    def test_money_rejects_a_non_alphabetic_currency(self) -> None:
        with pytest.raises(DomainError):
            Money.of(100, "US1")


class TestVolume:
    def test_volume_must_be_positive(self) -> None:
        with pytest.raises(DomainError):
            Volume.of(0)

    def test_volume_refuses_negative(self) -> None:
        with pytest.raises(DomainError):
            Volume.of(Decimal("-0.1"))


class TestSymbolSpecificationValidation:
    def test_rejects_zero_point(self, us30_spec: SymbolSpecification) -> None:
        with pytest.raises(InvalidSpecificationError, match="point must be positive"):
            SymbolSpecification(**{**_fields(us30_spec), "point": Decimal("0")})

    def test_rejects_zero_tick_size(self, us30_spec: SymbolSpecification) -> None:
        with pytest.raises(InvalidSpecificationError, match="tick_size"):
            SymbolSpecification(**{**_fields(us30_spec), "tick_size": Decimal("0")})

    def test_rejects_tick_size_above_point_as_inconsistent(self, us30_spec: SymbolSpecification) -> None:
        # Sub-point quoting (point 0.01, tick 0.001) is ordinary. A tick *coarser* than
        # the point means a point-denominated stop distance cannot be represented at all,
        # so the broker's own fields disagree with each other.
        with pytest.raises(InvalidSpecificationError, match="inconsistent"):
            SymbolSpecification(
                **{**_fields(us30_spec), "point": Decimal("0.01"), "tick_size": Decimal("0.1")}
            )

    def test_accepts_sub_point_quoting(self) -> None:
        # point 0.01 with tick 0.001 is a legitimate specification and must not be rejected.
        sub_point = SymbolSpecification(
            **{**_fields(_spec()), "point": Decimal("0.01"), "tick_size": Decimal("0.001"), "digits": 3}
        )
        assert sub_point.tick_size == Decimal("0.001")

    def test_rejects_volume_max_below_min(self, us30_spec: SymbolSpecification) -> None:
        with pytest.raises(InvalidSpecificationError, match="below volume_min"):
            SymbolSpecification(**{**_fields(us30_spec), "volume_max": Decimal("0.05")})

    def test_rejects_zero_volume_step(self, us30_spec: SymbolSpecification) -> None:
        with pytest.raises(InvalidSpecificationError, match="volume_step"):
            SymbolSpecification(**{**_fields(us30_spec), "volume_step": Decimal("0")})

    def test_rejects_negative_stops_level(self, us30_spec: SymbolSpecification) -> None:
        with pytest.raises(InvalidSpecificationError, match="stops_level"):
            SymbolSpecification(**{**_fields(us30_spec), "stops_level": -1})


class TestPointConversion:
    """The point/price bridge. This is where "1 point is not $1" lives."""

    def test_points_become_price_distance_via_the_brokers_point(
        self, us30_spec: SymbolSpecification
    ) -> None:
        # 10 points x point 0.1 = 1.0 price unit. Derived, not assumed.
        assert us30_spec.points_to_price(10) == Decimal("1.0")

    def test_the_same_point_count_costs_different_money_on_a_different_specification(self) -> None:
        # The identical "100 point" move costs $100/lot on an index-style specification and
        # $1000/lot on a futures-style one. Only tick_value distinguishes them; a system
        # that assumed 1 point = $1 would get both wrong, and would get the first one
        # wrong by a factor of ten.
        index_like = _spec(point=Decimal("0.1"), tick_value=Decimal("1.0"), digits=1)
        futures_like = _spec(point=Decimal("0.01"), tick_value=Decimal("10.0"), digits=2)
        entry = Price.parse("40000.00", 2)
        stop = Price.parse("39990.00", 2)
        # 10.00 price units: index_like = 100 ticks x $1; futures_like = 1000 ticks x $10
        assert index_like.price_risk_for(entry, stop, Volume.of(1)).amount == Decimal("100.0")
        assert futures_like.price_risk_for(entry, stop, Volume.of(1)).amount == Decimal("10000.0")

    def test_one_point_is_not_one_dollar_on_the_fixture(self, us30_spec: SymbolSpecification) -> None:
        # On the assumed US30 specification one point (0.1) is one tick worth $1.00 per
        # lot -- but that is the *broker's* tick value, not an arithmetic identity. This
        # test documents the assumption so that changing the fixture is a visible event.
        assert us30_spec.points_to_price(1) == Decimal("0.1")
        assert us30_spec.tick_count_for(1) == Decimal("1")
        entry = Price.parse("40000.0", 1)
        stop = Price.parse("39999.9", 1)
        assert us30_spec.price_risk_for(entry, stop, Volume.of(1)).amount == Decimal("1.0")

    def test_price_to_points_is_exact_when_the_spec_allows_it(
        self, us30_spec: SymbolSpecification
    ) -> None:
        assert us30_spec.price_to_points(Decimal("1.0")) == Decimal("10")

    def test_price_to_points_reports_a_fraction_rather_than_truncating(
        self, us30_spec: SymbolSpecification
    ) -> None:
        # 0.05 price units is 0.5 points. Silently truncating to 0 would turn a
        # 100-point trailing distance into a different number of points.
        assert us30_spec.price_to_points(Decimal("0.05")) == Decimal("0.5")

    def test_signed_distance_points(self, us30_spec: SymbolSpecification) -> None:
        low = Price.parse("40000.0", 1)
        high = Price.parse("40001.0", 1)
        assert us30_spec.distance_points(low, high) == Decimal("10")
        assert us30_spec.distance_points(high, low) == Decimal("-10")

    def test_tick_count_bridges_points_to_broker_ticks(self, us30_spec: SymbolSpecification) -> None:
        # 100 points at point=0.1 and tick_size=0.1 is 100 ticks.
        assert us30_spec.tick_count_for(100) == Decimal("100")

    def test_tick_count_is_correct_when_point_and_tick_differ(self) -> None:
        # point 0.01, tick 0.001: 10 points is 0.1 price units is 100 ticks.
        spec = _spec(point=Decimal("0.01"), tick_size=Decimal("0.001"), digits=3)
        assert spec.tick_count_for(10) == Decimal("100")


class TestPriceNormalisation:
    def test_normalize_snaps_to_the_tick_grid(self, us30_spec: SymbolSpecification) -> None:
        assert str(us30_spec.normalize_price(Price.parse("40000.07", 2))) == "40000.1"

    def test_buy_stop_rounds_away_from_entry_downwards(self, us30_spec: SymbolSpecification) -> None:
        # A BUY stop sits below entry. Rounding toward entry (up) would make the
        # protection tighter than the risk engine approved, so it rounds down.
        rounded = us30_spec.round_stop_price(Price.parse("39999.94", 2), Side.SIDE_BUY)
        assert rounded.value == Decimal("39999.9")

    def test_sell_stop_rounds_away_from_entry_upwards(self, us30_spec: SymbolSpecification) -> None:
        # A SELL stop sits above entry, so "away" means rounding up.
        rounded = us30_spec.round_stop_price(Price.parse("40000.06", 2), Side.SIDE_SELL)
        assert rounded.value == Decimal("40000.1")

    def test_buy_target_rounds_up_and_sell_target_rounds_down(
        self, us30_spec: SymbolSpecification
    ) -> None:
        # A target rounded *toward* entry would be easier to reach than intended.
        assert us30_spec.round_target_price(Price.parse("40000.04", 2), Side.SIDE_BUY).value == Decimal(
            "40000.1"
        )
        assert us30_spec.round_target_price(Price.parse("39999.96", 2), Side.SIDE_SELL).value == Decimal(
            "39999.9"
        )

    def test_rounding_never_moves_a_stop_toward_entry(self, us30_spec: SymbolSpecification) -> None:
        entry = Price.parse("40000.0", 1)
        for raw in ("39999.94", "39999.96", "39999.99"):
            candidate = Price.parse(raw, 2)
            rounded = us30_spec.round_stop_price(candidate, Side.SIDE_BUY)
            assert rounded <= entry
            assert rounded <= candidate

    def test_on_tick_grid_detection(self, us30_spec: SymbolSpecification) -> None:
        assert us30_spec.is_on_tick_grid(Price.parse("40000.1", 1))
        assert not us30_spec.is_on_tick_grid(Price.parse("40000.15", 2))


class TestVolumeNormalisation:
    def test_rounds_down_to_the_volume_step(self, us30_spec: SymbolSpecification) -> None:
        # Rounding up would produce a position larger than the risk engine approved.
        assert us30_spec.normalize_volume(Decimal("0.17")).lots == Decimal("0.1")

    def test_clamped_to_the_broker_maximum(self, us30_spec: SymbolSpecification) -> None:
        assert us30_spec.normalize_volume(Decimal("500")).lots == Decimal("50.0")

    def test_refuses_a_volume_that_rounds_below_the_broker_minimum(
        self, us30_spec: SymbolSpecification
    ) -> None:
        with pytest.raises(InvalidSpecificationError, match="below the broker minimum"):
            us30_spec.normalize_volume(Decimal("0.01"))

    def test_explicit_ceiling_rounding_is_available_but_never_the_default(
        self, us30_spec: SymbolSpecification
    ) -> None:
        assert (
            us30_spec.normalize_volume(Decimal("0.17"), rounding=ROUND_CEILING).lots
            == Decimal("0.2")
        )
        assert us30_spec.normalize_volume(Decimal("0.17"), rounding=ROUND_FLOOR).lots == Decimal("0.1")

    def test_a_maximum_that_floors_below_the_minimum_is_reported(
        self, us30_spec: SymbolSpecification
    ) -> None:
        # volume_max 1.0 on a 0.3 step floors to 0.9, which is below this instrument's
        # 0.95 minimum. The two broker fields are contradictory and must be reported
        # rather than silently producing an untradeable volume.
        spec = _spec(volume_step=Decimal("0.3"), volume_min=Decimal("0.95"), volume_max=Decimal("1.0"))
        with pytest.raises(InvalidSpecificationError, match="not on the volume step"):
            spec.normalize_volume(Decimal("100"))


class TestMonetaryRisk:
    def test_price_risk_excludes_commission(self, us30_spec: SymbolSpecification) -> None:
        entry = Price.parse("40000.0", 1)
        stop = Price.parse("39990.0", 1)
        # 10.0 price units / 0.1 tick = 100 ticks x $1.00 x 1 lot = $100
        assert us30_spec.price_risk_for(entry, stop, Volume.of(1)).amount == Decimal("100.0")

    def test_price_risk_scales_with_volume(self, us30_spec: SymbolSpecification) -> None:
        entry = Price.parse("40000.0", 1)
        stop = Price.parse("39990.0", 1)
        assert us30_spec.price_risk_for(entry, stop, Volume.of(2)).amount == Decimal("200.0")

    def test_price_risk_is_unchanged_by_which_side_lost(self, us30_spec: SymbolSpecification) -> None:
        # A short position risks the same distance. Direction must not change the size.
        entry = Price.parse("40000.0", 1)
        stop = Price.parse("40010.0", 1)
        assert us30_spec.price_risk_for(entry, stop, Volume.of(1)).amount == Decimal("100.0")


class TestPriceProperties:
    @given(
        points=st.integers(min_value=0, max_value=100_000),
        point=st.sampled_from([Decimal("0.01"), Decimal("0.1"), Decimal("0.001")]),
    )
    def test_points_to_price_is_exact_and_never_drifts(self, points: int, point: Decimal) -> None:
        spec = _spec(point=point, tick_size=point, digits=5)
        expected = Decimal(points) * point
        assert spec.points_to_price(points) == expected
        # Round-tripping back must recover the original point count exactly.
        assert spec.price_to_points(spec.points_to_price(points)) == Decimal(points)

    @given(lots=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100"), places=4))
    def test_normalized_volume_never_exceeds_the_request(self, lots: Decimal) -> None:
        spec = _spec(volume_step=Decimal("0.1"), volume_min=Decimal("0.1"), volume_max=Decimal("50"))
        try:
            normalized = spec.normalize_volume(lots)
        except InvalidSpecificationError:
            return  # below the broker minimum: refusing is the correct behaviour
        assert normalized.lots <= lots
        assert spec.is_volume_on_step(normalized.lots)


def _fields(spec: SymbolSpecification) -> dict[str, Any]:
    """Keyword arguments for rebuilding ``spec`` with overrides.

    ``Any`` rather than ``object``: the result is splatted straight back into
    ``SymbolSpecification(**...)`` with one field overridden, and ``object`` would make
    every splatted value un-typeable at the call site.
    """
    return {
        "name": spec.name,
        "digits": spec.digits,
        "point": spec.point,
        "tick_size": spec.tick_size,
        "tick_value": spec.tick_value,
        "contract_size": spec.contract_size,
        "volume_min": spec.volume_min,
        "volume_max": spec.volume_max,
        "volume_step": spec.volume_step,
        "stops_level": spec.stops_level,
        "freeze_level": spec.freeze_level,
        "currency": spec.currency,
    }


def _spec(
    *,
    point: Decimal = Decimal("0.1"),
    tick_size: Decimal | None = None,
    tick_value: Decimal = Decimal("1.0"),
    digits: int = 1,
    volume_step: Decimal = Decimal("0.1"),
    volume_min: Decimal = Decimal("0.1"),
    volume_max: Decimal = Decimal("50.0"),
) -> SymbolSpecification:
    return SymbolSpecification(
        name="TEST",
        digits=digits,
        point=point,
        tick_size=tick_size if tick_size is not None else point,
        tick_value=tick_value,
        contract_size=Decimal("1"),
        volume_min=volume_min,
        volume_max=volume_max,
        volume_step=volume_step,
        currency="USD",
    )
