"""Where the pending stop order goes.

The two rules are exact, so the tests are exact:

* BUY STOP  = ``M1.high + 10 points``
* SELL STOP = ``M1.low  - 10 points``

The point is worth 0.1 price units on this specification, so 10 points is 1.0 price unit.
A test that hard-coded "plus 1.0" would pass on this specification and fail on every other
one, which is precisely the "1 point = $1" mistake the specification forbids. These tests
all go through the specification instead.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from stop_order_scalp.domain.enums import OrderKind, Side
from stop_order_scalp.domain.exceptions import DomainError
from stop_order_scalp.domain.models import Candle
from stop_order_scalp.domain.value_objects import SymbolSpecification
from stop_order_scalp.strategy.entry_rules import (
    buy_stop_price,
    entry_level,
    sell_stop_price,
    validate_entry_candle,
)
from strategy.conftest import M1, M15, m1, m15, price

HIGH = "40050.0"
LOW = "39950.0"
OFFSET = 10


def candle() -> Candle:
    return m1(0, high=HIGH, low=LOW)


class TestBuyStop:
    def test_the_price_is_the_high_plus_the_offset(self, spec: SymbolSpecification) -> None:
        # 10 points x point 0.1 = 1.0 price unit, so 40050.0 -> 40051.0.
        level = buy_stop_price(candle(), OFFSET, spec)
        assert level.price == price("40051.0")
        assert level.side is Side.SIDE_BUY
        assert level.order_kind is OrderKind.ORDER_KIND_BUY_STOP

    def test_the_offset_is_resolved_through_the_specification(
        self, spec: SymbolSpecification
    ) -> None:
        level = buy_stop_price(candle(), OFFSET, spec)
        assert level.offset_distance == spec.points_to_price(OFFSET)
        assert level.offset_distance == Decimal("1.0")
        assert level.offset_points == OFFSET

    def test_the_anchor_is_the_candle_high(self, spec: SymbolSpecification) -> None:
        level = buy_stop_price(candle(), OFFSET, spec)
        assert level.anchor == price(HIGH)

    def test_it_is_always_above_the_candle_high(self, spec: SymbolSpecification) -> None:
        level = buy_stop_price(candle(), OFFSET, spec)
        bar = candle()
        assert level.price > bar.high

    def test_a_zero_offset_places_it_exactly_on_the_high(
        self, spec: SymbolSpecification
    ) -> None:
        level = buy_stop_price(candle(), 0, spec)
        assert level.price == price(HIGH)
        assert level.rounded is False


class TestSellStop:
    def test_the_price_is_the_low_minus_the_offset(self, spec: SymbolSpecification) -> None:
        # 39950.0 - 1.0 = 39949.0.
        level = sell_stop_price(candle(), OFFSET, spec)
        assert level.price == price("39949.0")
        assert level.side is Side.SIDE_SELL
        assert level.order_kind is OrderKind.ORDER_KIND_SELL_STOP

    def test_the_anchor_is_the_candle_low(self, spec: SymbolSpecification) -> None:
        level = sell_stop_price(candle(), OFFSET, spec)
        assert level.anchor == price(LOW)

    def test_it_is_always_below_the_candle_low(self, spec: SymbolSpecification) -> None:
        level = sell_stop_price(candle(), OFFSET, spec)
        bar = candle()
        assert level.price < bar.low


class TestPointsAreNotDollars:
    """The specification says *do not assume 1 point = $1*. These tests hold that line.

    If someone ever replaces ``points_to_price`` with ``* 1``, every test here fails.
    """

    def test_a_larger_point_makes_a_larger_offset(self) -> None:
        coarse = SymbolSpecification(
            name="X", digits=2, point=Decimal("0.01"), tick_size=Decimal("0.01"),
            tick_value=Decimal("1.0"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"), volume_max=Decimal("50.0"), volume_step=Decimal("0.1"),
        )
        bar = m1(0, high="40050.00", low="39950.00", digits=2)
        level = buy_stop_price(bar, 100, coarse)
        # 100 points x 0.01 = 1.0 price unit, the same absolute move as 10 x 0.1.
        assert level.price == price("40051.00", digits=2)

    def test_the_offset_scales_with_the_point_not_with_the_offset_alone(
        self, spec: SymbolSpecification
    ) -> None:
        ten = buy_stop_price(candle(), 10, spec)
        hundred = buy_stop_price(candle(), 100, spec)
        assert ten.offset_distance == Decimal("1.0")
        assert hundred.offset_distance == Decimal("10.0")
        assert hundred.price - ten.price == Decimal("9.0")

    def test_sub_point_quoting_still_works(self) -> None:
        fine = SymbolSpecification(
            name="X", digits=3, point=Decimal("0.01"), tick_size=Decimal("0.001"),
            tick_value=Decimal("1.0"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"), volume_max=Decimal("50.0"), volume_step=Decimal("0.1"),
        )
        bar = m1(0, high="40050.000", low="39950.000", digits=3)
        level = buy_stop_price(bar, 10, fine)
        assert level.price.digits == 3
        assert level.price.value == Decimal("40050.1")


class TestRounding:
    """Rounding only bites when a price arrives off the tick grid.

    Since ``point`` is a whole multiple of ``tick_size`` -- which
    ``SymbolSpecification`` enforces -- a well-behaved broker's price plus a whole number
    of points always lands on the grid. These tests therefore feed an off-grid candle, which
    is the only way to reach the rounding branch, and assert the branch is *correct* rather
    than merely present.
    """

    def test_an_on_grid_price_does_not_round(self, spec: SymbolSpecification) -> None:
        level = buy_stop_price(candle(), 1, spec)
        assert level.rounded is False
        assert level.price.value == Decimal("40050.1")

    def test_a_buy_stop_rounds_up_away_from_the_candle(
        self, spec: SymbolSpecification
    ) -> None:
        off_grid = m1(0, high="40050.03", low="39950.0")
        level = buy_stop_price(off_grid, OFFSET, spec)
        assert level.rounded is True
        # 40050.03 + 1.0 = 40051.03; ceiling on the 0.1 grid is 40051.1, not 40051.0.
        assert level.price.value == Decimal("40051.1")
        assert level.price > off_grid.high + Decimal("1.0")

    def test_a_sell_stop_rounds_down_away_from_the_candle(
        self, spec: SymbolSpecification
    ) -> None:
        off_grid = m1(0, high="40050.0", low="39949.97")
        level = sell_stop_price(off_grid, OFFSET, spec)
        assert level.rounded is True
        # 39949.97 - 1.0 = 39948.97; floor on the 0.1 grid is 39948.9, not 39949.0.
        assert level.price.value == Decimal("39948.9")
        assert level.price < off_grid.low - Decimal("1.0")

    def test_entry_rounding_is_the_opposite_of_stop_loss_rounding(
        self, spec: SymbolSpecification
    ) -> None:
        """The distinction that is easy to get wrong and inverts the trade if you do.

        Anchored on the entry, a BUY stop rounds down. Anchored on the candle high, a BUY
        STOP entry rounds up. Reusing one method for the other silently moves every entry
        a tick toward the market.
        """
        raw = Decimal("40051.03")
        assert spec.round_entry_price(raw, Side.SIDE_BUY).value == Decimal("40051.1")
        assert spec.round_stop_price(raw, Side.SIDE_BUY).value == Decimal("40051.0")


class TestEntryLevelDispatch:
    def test_buy_dispatches_to_the_buy_rule(self, spec: SymbolSpecification) -> None:
        bar = candle()
        assert entry_level(bar, Side.SIDE_BUY, OFFSET, spec) == buy_stop_price(
            bar, OFFSET, spec
        )

    def test_sell_dispatches_to_the_sell_rule(self, spec: SymbolSpecification) -> None:
        bar = candle()
        assert entry_level(bar, Side.SIDE_SELL, OFFSET, spec) == sell_stop_price(
            bar, OFFSET, spec
        )


class TestRefusals:
    def test_a_negative_offset_is_refused(self, spec: SymbolSpecification) -> None:
        with pytest.raises(DomainError, match="non-negative"):
            buy_stop_price(candle(), -1, spec)

    def test_a_negative_offset_explains_the_danger(self, spec: SymbolSpecification) -> None:
        with pytest.raises(DomainError, match="inside the candle"):
            sell_stop_price(candle(), -5, spec)

    def test_an_m15_candle_is_refused_by_the_m1_rule(self) -> None:
        with pytest.raises(DomainError, match="expected a M1 candle"):
            validate_entry_candle(m15(0), M1)

    def test_the_error_names_both_timeframes(self) -> None:
        with pytest.raises(DomainError, match="M15"):
            validate_entry_candle(m15(0), M1)

    def test_a_matching_timeframe_is_accepted(self) -> None:
        validate_entry_candle(m1(0), M1)

    def test_the_m15_candle_would_have_been_a_valid_candle_otherwise(self) -> None:
        # Sanity: the refusal is about the timeframe, not about a malformed object.
        assert m15(0).timeframe == M15


class TestStringForm:
    def test_it_names_the_order_kind_and_price(self, spec: SymbolSpecification) -> None:
        text = str(buy_stop_price(candle(), OFFSET, spec))
        assert "BUY_STOP" in text
        assert "40051.0" in text

    def test_is_buy_stop_reflects_the_kind(self, spec: SymbolSpecification) -> None:
        assert buy_stop_price(candle(), OFFSET, spec).is_buy_stop
        assert not sell_stop_price(candle(), OFFSET, spec).is_buy_stop
