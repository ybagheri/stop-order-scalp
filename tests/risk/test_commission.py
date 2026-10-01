"""Commission, and the two meanings one number can have.

The risk here is entirely about interpretation. Reading ``$6/lot`` as per-side when it is
round-trip doubles the cost, and therefore halves the position size, without raising
anything.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from stop_order_scalp.domain.enums import CommissionMode
from stop_order_scalp.domain.value_objects import Volume
from stop_order_scalp.risk.commission import (
    CommissionModel_,
    commission_for,
    per_lot_cost,
    round_trip_cost,
)

SIX = Decimal("6.0")
ONE = Decimal(1)


class TestTheTwoModes:
    def test_round_trip_uses_the_rate_as_given(self) -> None:
        assert per_lot_cost(SIX, CommissionMode.PER_LOT_ROUND_TRIP) == SIX

    def test_per_side_doubles_it(self) -> None:
        assert per_lot_cost(SIX, CommissionMode.PER_LOT_PER_SIDE) == Decimal("12.0")

    def test_the_mode_reports_how_many_sides_it_charges(self) -> None:
        assert CommissionMode.PER_LOT_ROUND_TRIP.sides == 1
        assert CommissionMode.PER_LOT_PER_SIDE.sides == 2

    def test_a_zero_rate_is_valid(self) -> None:
        assert per_lot_cost(Decimal("0"), CommissionMode.PER_LOT_PER_SIDE) == Decimal("0")

    def test_a_negative_rate_is_refused(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            per_lot_cost(Decimal("-1"), CommissionMode.PER_LOT_ROUND_TRIP)


class TestRoundTripCost:
    def test_it_scales_with_volume(self) -> None:
        assert round_trip_cost(Decimal("0.4"), SIX, CommissionMode.PER_LOT_ROUND_TRIP).amount == Decimal("2.40")

    def test_it_accepts_a_volume_object(self) -> None:
        as_object = round_trip_cost(Volume.of(Decimal("0.4")), SIX, CommissionMode.PER_LOT_ROUND_TRIP)
        as_decimal = round_trip_cost(Decimal("0.4"), SIX, CommissionMode.PER_LOT_ROUND_TRIP)
        assert as_object == as_decimal

    def test_per_side_costs_double(self) -> None:
        one = round_trip_cost(ONE, SIX, CommissionMode.PER_LOT_ROUND_TRIP)
        two = round_trip_cost(ONE, SIX, CommissionMode.PER_LOT_PER_SIDE)
        assert two.amount == one.amount * 2

    def test_zero_volume_costs_nothing(self) -> None:
        assert round_trip_cost(Decimal("0"), SIX, CommissionMode.PER_LOT_ROUND_TRIP).amount == Decimal("0")

    def test_the_currency_is_carried_through(self) -> None:
        money = round_trip_cost(ONE, SIX, CommissionMode.PER_LOT_ROUND_TRIP, "EUR")
        assert money.currency == "EUR"

    def test_commission_for_is_the_same_function(self) -> None:
        a = commission_for(Decimal("0.3"), SIX, CommissionMode.PER_LOT_ROUND_TRIP)
        b = round_trip_cost(Decimal("0.3"), SIX, CommissionMode.PER_LOT_ROUND_TRIP)
        assert a == b


class TestCommissionModel:
    def test_it_binds_the_rate_to_its_interpretation(self) -> None:
        model = CommissionModel_(rate=SIX, mode=CommissionMode.PER_LOT_PER_SIDE)
        assert model.per_lot_round_trip == Decimal("12.0")

    def test_it_computes_for_a_volume(self) -> None:
        model = CommissionModel_(rate=SIX, mode=CommissionMode.PER_LOT_ROUND_TRIP)
        assert model.for_volume(Decimal("0.4")).amount == Decimal("2.40")

    def test_it_serialises_the_interpretation(self) -> None:
        payload = CommissionModel_(rate=SIX, mode=CommissionMode.PER_LOT_PER_SIDE).as_dict()
        assert payload["sides"] == 2
        assert payload["per_lot_round_trip"] == "12.0"

    def test_a_negative_rate_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            CommissionModel_(rate=Decimal("-1"), mode=CommissionMode.PER_LOT_ROUND_TRIP)

    def test_the_string_form_states_the_resolved_rate(self) -> None:
        text = str(CommissionModel_(rate=SIX, mode=CommissionMode.PER_LOT_PER_SIDE))
        assert "12.0" in text

    def test_the_two_modes_produce_different_money_for_one_lot(self) -> None:
        # The single fact the whole module exists to make unmissable.
        round_trip = CommissionModel_(rate=SIX, mode=CommissionMode.PER_LOT_ROUND_TRIP)
        per_side = CommissionModel_(rate=SIX, mode=CommissionMode.PER_LOT_PER_SIDE)
        assert round_trip.for_volume(ONE).amount * 2 == per_side.for_volume(ONE).amount


class TestProperties:
    @given(
        rate=st.decimals(min_value=0, max_value=1000, places=2),
        mode=st.sampled_from(list(CommissionMode)),
        lots=st.decimals(min_value=0, max_value=50, places=2),
    )
    def test_cost_is_rate_times_lots_times_sides(
        self, rate: Decimal, mode: CommissionMode, lots: Decimal
    ) -> None:
        expected = rate * lots * Decimal(mode.sides)
        assert round_trip_cost(lots, rate, mode).amount == expected

    @given(
        rate=st.decimals(min_value=0, max_value=1000, places=2),
        lots=st.decimals(min_value=0, max_value=50, places=2),
    )
    def test_cost_is_never_negative(self, rate: Decimal, lots: Decimal) -> None:
        for mode in CommissionMode:
            assert round_trip_cost(lots, rate, mode).amount >= 0

    @given(
        rate=st.decimals(min_value=0, max_value=1000, places=2),
        lots=st.decimals(min_value=0, max_value=50, places=2),
    )
    def test_cost_grows_with_volume(self, rate: Decimal, lots: Decimal) -> None:
        mode = CommissionMode.PER_LOT_ROUND_TRIP
        smaller = round_trip_cost(lots / 2, rate, mode).amount
        larger = round_trip_cost(lots, rate, mode).amount
        assert smaller <= larger

    @given(
        rate=st.decimals(min_value=0, max_value=1000, places=2),
        lots=st.decimals(min_value=0, max_value=50, places=2),
    )
    def test_the_two_agree_about_which_is_larger(self, rate: Decimal, lots: Decimal) -> None:
        one = round_trip_cost(lots, rate, CommissionMode.PER_LOT_ROUND_TRIP).amount
        two = round_trip_cost(lots, rate, CommissionMode.PER_LOT_PER_SIDE).amount
        assert one <= two
