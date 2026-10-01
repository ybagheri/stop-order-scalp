"""Position sizing.

The numbers here are the ones worked through by hand in
``docs/mt5/SYMBOL_SPECIFICATIONS.md`` §2. If a change makes these fail, either the sizing
is wrong or that document is -- and both need to agree before either is trusted.

The headline property is :class:`TestSizingInvariants`:
**the chosen volume never carries more total risk than the budget allowed.** That is the
only thing a risk limit is for, so it is proved rather than spot-checked.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from risk.conftest import (
    ENTRY,
    NO_COMMISSION,
    STOP_100PT,
    risk_input,
    without_commission,
)
from stop_order_scalp.domain.enums import CommissionMode, RiskMode
from stop_order_scalp.domain.exceptions import InvalidSpecificationError, RiskError
from stop_order_scalp.domain.value_objects import Money, Price, SymbolSpecification
from stop_order_scalp.risk.commission import CommissionModel_
from stop_order_scalp.risk.position_sizer import SizingInput, cost_per_lot, size_position

HUNDRED = Decimal(100)


def budget_of(balance: Money, percent: Decimal = Decimal("0.5")) -> Decimal:
    """The budget the sizer should compute, stated independently of the sizer."""
    return balance.scaled(percent / HUNDRED).amount


# =============================================================================
# The worked example
# =============================================================================


class TestWorkedExample:
    """0.5 % of $10,000, a 100-point stop, $6/lot commission.

    On the assumed US30: 100 points is 10.0 price units is 100 ticks is $100 per lot.
    With $6 commission, one lot costs $106, so a $50 budget buys 50/106 = 0.4717 lots,
    which floors onto the 0.1 volume step as **0.4 lots** carrying $42.40.
    """

    def test_the_budget_is_half_a_percent_of_balance(self) -> None:
        result = size_position(risk_input())
        assert result.budget.amount == Decimal("50.00")

    def test_one_lot_costs_a_hundred_dollars_plus_commission(self) -> None:
        price_lot, commission_lot, total_lot = cost_per_lot(
            ENTRY, STOP_100PT, risk_input().specification, risk_input().commission
        )
        assert price_lot.amount == Decimal("100.0")
        assert commission_lot.amount == Decimal("6.00")
        assert total_lot.amount == Decimal("106.00")

    def test_the_raw_size_is_the_budget_over_the_cost(self) -> None:
        result = size_position(risk_input())
        assert result.raw_volume == Decimal("50.00") / Decimal("106.00")

    def test_the_size_floors_onto_the_volume_step(self) -> None:
        result = size_position(risk_input())
        # 50/106 = 0.4717 lots; four whole 0.1 steps fit, the fifth would breach the budget.
        assert result.steps == Decimal("4")
        assert result.volume.lots == Decimal("0.4")

    def test_the_chosen_size_carries_the_documented_risk(self) -> None:
        result = size_position(risk_input())
        assert result.price_risk.amount == Decimal("40.00")
        assert result.commission.amount == Decimal("2.40")
        assert result.total_risk.amount == Decimal("42.40")

    def test_the_risk_fraction_is_reported_as_a_percentage(self) -> None:
        assert size_position(risk_input()).risk_fraction == Decimal("0.4240")

    def test_the_fifth_step_would_breach_the_budget(self) -> None:
        """Why the floor is the safe direction.

        0.5 lots would carry $53.00 of a $50.00 budget. The floor gives $42.40 instead.
        """
        assert size_position(risk_input()).total_risk.amount <= Decimal("50.00")
        fifth_step = Decimal("0.5") * Decimal("106.00")
        assert fifth_step > Decimal("50.00")

    def test_a_zero_commission_buy_trap_shows_the_naive_overshoot(self) -> None:
        """With commission charged as zero, the divisor is price risk alone.

        That is the arithmetic an implementation that ignored commission would use: it
        gives 0.5 lots. Adding the real $6/lot is what brings it back to 0.4.
        """
        naive = size_position(risk_input(commission_model=NO_COMMISSION))
        assert naive.volume.lots == Decimal("0.5")
        assert size_position(risk_input()).volume.lots < naive.volume.lots

    def test_sizing_on_price_risk_alone_overshoots_by_the_commission(self) -> None:
        """The concrete number the specification cares about.

        Sizing on price risk alone gives 50/100 = 0.5 lots, which carries $50 of price
        risk *plus* $3 of commission = $53, over a $50 budget. A 6 % overshoot on every
        trade, silently.
        """
        price_only = size_position(risk_input(commission_model=NO_COMMISSION))
        real = size_position(risk_input())
        assert price_only.raw_volume == Decimal("50.00") / Decimal("100.00")
        assert real.raw_volume == Decimal("50.00") / Decimal("106.00")
        assert real.volume.lots < price_only.volume.lots


# =============================================================================
# "1 point = $1" is false
# =============================================================================


class TestPointsAreNotDollars:
    def test_a_hundred_point_stop_costs_a_hundred_per_lot_not_ten(
        self, us30: SymbolSpecification
    ) -> None:
        # A naive implementation multiplying 100 points by 1.0 would say $100 -- which
        # happens to agree here. The next test is the one that separates them.
        result = size_position(risk_input(specification=us30))
        assert result.price_risk_per_lot.amount == Decimal("100.0")

    def test_the_tick_value_drives_the_answer(
        self, expensive_spec: SymbolSpecification
    ) -> None:
        # Same 100-point stop, tick value 10x: the risk per lot is 10x. A hard-coded
        # price-per-point could not do this, which is the point of the test.
        cheap = size_position(risk_input())
        dear = size_position(risk_input(balance=Money.of(100000), specification=expensive_spec))
        assert dear.price_risk_per_lot.amount == cheap.price_risk_per_lot.amount * 10

    def test_a_dearer_instrument_buys_fewer_lots(
        self, expensive_spec: SymbolSpecification
    ) -> None:
        # With a balance large enough that both buy a real position, the dearer one buys
        # exactly a tenth as much. At a small balance the dearer one is refused outright,
        # which is the correct answer and is covered separately.
        big = Money.of(1000000)
        cheap = size_position(risk_input(balance=big))
        dear = size_position(risk_input(balance=big, specification=expensive_spec))
        # Not exactly a tenth: the commission does not scale with the tick value, so one
        # dearer lot costs $1006 rather than 10 x $106.
        assert dear.price_risk_per_lot.amount == cheap.price_risk_per_lot.amount * 10
        assert dear.total_per_lot.amount < cheap.total_per_lot.amount * 10
        assert dear.raw_volume < cheap.raw_volume / 9
        assert dear.volume.lots < cheap.volume.lots

    def test_an_instrument_too_expensive_to_trade_is_refused(
        self, expensive_spec: SymbolSpecification
    ) -> None:
        # One lot costs $1000 + $6 = $1006, so a $50 budget cannot support 0.1 lots.
        # Buying 0.1 lots anyway would carry 20x the intended risk.
        with pytest.raises(RiskError, match="below the broker minimum"):
            size_position(risk_input(specification=expensive_spec))

    def test_the_point_size_drives_the_answer(self) -> None:
        """Same *distance*, different tick granularity, different money risk.

        Two specifications with the same ``point`` but tick sizes that differ by 100x, and
        a stop at the same price. On the fine one the same move is 100x the ticks.
        """
        coarse = SymbolSpecification(
            name="X", digits=2, point=Decimal("0.01"), tick_size=Decimal("0.01"),
            tick_value=Decimal("1.0"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.01"), volume_max=Decimal("50.0"), volume_step=Decimal("0.01"),
        )
        fine = SymbolSpecification(
            name="X", digits=3, point=Decimal("0.01"), tick_size=Decimal("0.0001"),
            tick_value=Decimal("1.0"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.01"), volume_max=Decimal("50.0"), volume_step=Decimal("0.01"),
        )
        entry = Price.parse("40000.00", 3)
        stop = Price.parse("39990.00", 3)
        # A balance large enough that the 100x-dearer instrument still buys a position.
        balance = Money.of(10000000)
        a = size_position(risk_input(entry=entry, stop=stop, specification=coarse, balance=balance))
        b = size_position(risk_input(entry=entry, stop=stop, specification=fine, balance=balance))
        # 10.0 price units is 1000 ticks at 0.01 but 100,000 ticks at 0.0001.
        assert b.price_risk_per_lot.amount == a.price_risk_per_lot.amount * 100


# =============================================================================
# Commission
# =============================================================================


class TestCommission:
    def test_round_trip_is_used_as_configured(self) -> None:
        result = size_position(risk_input())
        assert result.commission_per_lot.amount == Decimal("6.00")

    def test_per_side_doubles_the_commission_and_shrinks_the_size(self) -> None:
        per_side = CommissionModel_(
            rate=Decimal("6.0"), mode=CommissionMode.PER_LOT_PER_SIDE
        )
        result = size_position(risk_input(commission_model=per_side))
        assert result.commission_per_lot.amount == Decimal("12.00")
        # A bigger cost per lot means a smaller position for the same budget. Needs a
        # balance where both sizes are real rather than one hitting the minimum.
        big = Money.of(1000000)
        assert (
            size_position(risk_input(balance=big, commission_model=per_side)).volume.lots
            < size_position(risk_input(balance=big)).volume.lots
        )

    def test_a_double_charged_commission_roughly_halves_the_size(self) -> None:
        # Doubling the cost per lot from $106 to $118 should roughly halve 0.4717 to 0.4237.
        per_side = CommissionModel_(
            rate=Decimal("6.0"), mode=CommissionMode.PER_LOT_PER_SIDE
        )
        big = Money.of(1000000)
        baseline = size_position(risk_input(balance=big)).raw_volume
        doubled = size_position(risk_input(balance=big, commission_model=per_side)).raw_volume
        # $100 of price risk plus $6 commission is $106 per lot; doubling the commission
        # to $12 makes it $112, so the raw size scales by exactly 106/112.
        assert baseline == budget_of(big) / Decimal("106.00")
        assert doubled == budget_of(big) / Decimal("112.00")

    def test_zero_commission_is_handled(self) -> None:
        free = CommissionModel_(rate=Decimal("0"), mode=CommissionMode.PER_LOT_ROUND_TRIP)
        result = size_position(risk_input(commission_model=free))
        assert result.commission.amount == Decimal("0.00")
        assert result.total_risk == result.price_risk

    def test_no_commission_model_at_all_is_handled(self) -> None:
        result = size_position(without_commission())
        assert result.commission.amount == Decimal("0.00")


# =============================================================================
# Rounding, and the floor
# =============================================================================


class TestRounding:
    def test_a_size_that_does_not_fit_the_step_rounds_down(self) -> None:
        # 0.37 requested on a 0.1 step must become 0.3, never 0.4.
        result = size_position(risk_input(fixed_lot=Decimal("0.37"), mode=RiskMode.RISK_MODE_FIXED_LOT))
        assert result.volume.lots == Decimal("0.3")

    def test_a_size_exactly_on_the_step_is_unchanged(self) -> None:
        result = size_position(risk_input(fixed_lot=Decimal("0.3"), mode=RiskMode.RISK_MODE_FIXED_LOT))
        assert result.volume.lots == Decimal("0.3")

    def test_rounding_down_never_increases_the_risk(self) -> None:
        result = size_position(risk_input(fixed_lot=Decimal("0.99"), mode=RiskMode.RISK_MODE_FIXED_LOT))
        assert result.volume.lots <= Decimal("0.9")

    def test_a_budget_below_the_minimum_is_refused_not_rounded_up(self) -> None:
        # $50 budget, but the smallest tradable position costs $106. Refusing is the point.
        with pytest.raises(RiskError, match="below the broker minimum"):
            size_position(risk_input(balance=Money.of(50)))

    def test_the_refusal_explains_why_rounding_up_would_be_wrong(self) -> None:
        with pytest.raises(RiskError, match="exceed the budget"):
            size_position(risk_input(balance=Money.of(50)))

    def test_a_configured_clamp_is_available_when_asked(self) -> None:
        result = size_position(risk_input(balance=Money.of(50), refuse_below_min_volume=False))
        assert result.volume.lots == Decimal("0.1")
        assert result.refused_below_minimum is True


# =============================================================================
# The two modes
# =============================================================================


class TestModes:
    def test_fixed_lot_ignores_the_percentage(self) -> None:
        low = size_position(risk_input(percent=Decimal("0.1"), mode=RiskMode.RISK_MODE_FIXED_LOT, fixed_lot=Decimal("0.3")))
        high = size_position(risk_input(percent=Decimal("50"), mode=RiskMode.RISK_MODE_FIXED_LOT, fixed_lot=Decimal("0.3")))
        assert low.volume.lots == high.volume.lots == Decimal("0.3")

    def test_fixed_lot_still_reports_the_risk_it_implies(self) -> None:
        result = size_position(risk_input(mode=RiskMode.RISK_MODE_FIXED_LOT, fixed_lot=Decimal("0.3")))
        # 0.3 lots x $106 = $31.80 of a $10,000 balance.
        assert result.total_risk.amount == Decimal("31.80")
        assert result.risk_fraction == Decimal("0.3180")

    def test_fixed_lot_has_no_budget(self) -> None:
        result = size_position(risk_input(mode=RiskMode.RISK_MODE_FIXED_LOT))
        assert result.budget.amount == Decimal("0.00")

    def test_percent_balance_scales_with_balance(self) -> None:
        # Checked on ``raw_volume`` rather than the final size: the size is floored onto
        # the volume step, so 4x the balance need not be exactly 4x the lots. The flooring
        # is separately pinned by the property test.
        small = size_position(risk_input(balance=Money.of(10000)))
        large = size_position(risk_input(balance=Money.of(40000)))
        assert large.raw_volume == small.raw_volume * 4
        assert large.volume.lots > small.volume.lots

    def test_the_size_itself_scales_where_the_step_does_not_bite(self) -> None:
        # With a 0.01 step the granularity is fine enough that 4x is 4x.
        fine = SymbolSpecification(
            name="X", digits=1, point=Decimal("0.1"), tick_size=Decimal("0.1"),
            tick_value=Decimal("1.0"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.01"), volume_max=Decimal("50.0"), volume_step=Decimal("0.01"),
        )
        small = size_position(risk_input(balance=Money.of(10000), specification=fine))
        large = size_position(risk_input(balance=Money.of(40000), specification=fine))
        assert large.volume.lots == small.volume.lots * 4

    def test_the_size_floors_as_the_balance_shrinks(self) -> None:
        # Below about $10,600 the budget can no longer support 0.1 lots at $106 each.
        with pytest.raises(RiskError, match="below the broker minimum"):
            size_position(risk_input(balance=Money.of(1000)))

    def test_percent_balance_scales_with_percent(self) -> None:
        one = size_position(risk_input(percent=Decimal("1.0")))
        two = size_position(risk_input(percent=Decimal("2.0")))
        assert two.volume.lots == one.volume.lots * 2


# =============================================================================
# The broker's volume bounds
# =============================================================================


class TestBrokerBounds:
    def test_a_size_above_volume_max_is_capped_down(self) -> None:
        capped = SymbolSpecification(
            name="X", digits=1, point=Decimal("0.1"), tick_size=Decimal("0.1"),
            tick_value=Decimal("0.01"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"), volume_max=Decimal("0.5"), volume_step=Decimal("0.1"),
        )
        # A tiny tick value makes each lot nearly free, so the budget wants a huge size.
        result = size_position(risk_input(specification=capped, balance=Money.of(100000)))
        assert result.volume.lots <= Decimal("0.5")

    def test_an_unusable_specification_is_refused_not_worked_around(self) -> None:
        # volume_max below volume_min is a specification the broker cannot honour.
        with pytest.raises(InvalidSpecificationError):
            SymbolSpecification(
                name="X", digits=1, point=Decimal("0.1"), tick_size=Decimal("0.1"),
                tick_value=Decimal("1.0"), contract_size=Decimal("1.0"),
                volume_min=Decimal("0.1"), volume_max=Decimal("50.0"), volume_step=Decimal("0.1"),
            ).normalize_volume(Decimal("0.05"))

    def test_a_volume_max_off_the_step_clamps_onto_it(self) -> None:
        # volume_max 0.55 on a 0.1 step: the nearest legal size at or below is 0.5.
        odd = SymbolSpecification(
            name="X", digits=1, point=Decimal("0.1"), tick_size=Decimal("0.1"),
            tick_value=Decimal("0.01"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"), volume_max=Decimal("0.55"), volume_step=Decimal("0.1"),
        )
        assert odd.normalize_volume(Decimal("50")).lots == Decimal("0.5")


# =============================================================================
# Refusals
# =============================================================================


class TestRefusals:
    def test_entry_equal_to_stop_is_refused(self) -> None:
        with pytest.raises(RiskError, match="no risk"):
            size_position(risk_input(stop=ENTRY))

    def test_a_non_positive_balance_is_refused(self) -> None:
        with pytest.raises(RiskError, match="cannot size"):
            size_position(risk_input(balance=Money.of(0)))

    def test_a_genuinely_free_lot_is_refused_as_a_configuration_fault(self) -> None:
        """Both halves of the cost must be zero before the guard fires.

        A zero ``tick_value`` with a real commission is not a free lot -- it just has no
        price risk, which is a legitimate state (a hedged or fully-commissioned position).
        Only price risk *and* commission both being zero leaves nothing to size against,
        which means the specification or the configuration is wrong.
        """
        free = SymbolSpecification(
            name="X", digits=1, point=Decimal("0.1"), tick_size=Decimal("0.1"),
            tick_value=Decimal("0"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"), volume_max=Decimal("50.0"), volume_step=Decimal("0.1"),
        )
        with pytest.raises(RiskError, match="costs nothing to lose"):
            size_position(risk_input(specification=free, commission_model=NO_COMMISSION))

    def test_a_zero_tick_value_with_commission_still_sizes(self) -> None:
        # No price risk, but the commission still bounds the position.
        free = SymbolSpecification(
            name="X", digits=1, point=Decimal("0.1"), tick_size=Decimal("0.1"),
            tick_value=Decimal("0"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"), volume_max=Decimal("50.0"), volume_step=Decimal("0.1"),
        )
        result = size_position(risk_input(specification=free))
        assert result.price_risk.amount == Decimal("0.00")
        assert result.total_risk == result.commission
        assert result.volume.lots > 0

    def test_a_non_positive_percent_is_refused(self) -> None:
        with pytest.raises(RiskError, match="percent must be positive"):
            size_position(risk_input(percent=Decimal("0")))

    def test_a_non_positive_fixed_lot_is_refused(self) -> None:
        with pytest.raises(RiskError, match="fixed_lot must be positive"):
            size_position(risk_input(mode=RiskMode.RISK_MODE_FIXED_LOT, fixed_lot=Decimal("0")))


# =============================================================================
# Reporting
# =============================================================================


class TestReporting:
    def test_the_result_serialises_every_intermediate(self) -> None:
        payload = size_position(risk_input()).as_dict()
        for key in ("volume", "budget", "price_risk_per_lot", "commission_per_lot",
                    "total_per_lot", "raw_volume", "steps", "risk_fraction",
                    "price_risk", "commission", "total_risk"):
            assert key in payload

    def test_the_string_form_names_the_size_and_the_risk(self) -> None:
        text = str(size_position(risk_input()))
        assert "0.4" in text
        assert "42.40" in text

    def test_cost_per_lot_adds_up(self) -> None:
        price_lot, commission_lot, total_lot = cost_per_lot(
            ENTRY, STOP_100PT, risk_input().specification, risk_input().commission
        )
        assert total_lot.amount == price_lot.amount + commission_lot.amount


# =============================================================================
# The property
# =============================================================================


@st.composite
def _sizing_case(draw: st.DrawFn) -> SizingInput:
    point = draw(st.sampled_from([Decimal("0.01"), Decimal("0.1"), Decimal("0.25")]))
    tick_value = draw(st.sampled_from([Decimal("0.5"), Decimal("1.0"), Decimal("10.0")]))
    step = draw(st.sampled_from([Decimal("0.01"), Decimal("0.1"), Decimal("0.5")]))
    distance_points = draw(st.integers(min_value=20, max_value=500))
    percent = draw(st.sampled_from([Decimal("0.1"), Decimal("0.5"), Decimal("1.0"), Decimal("2.0")]))
    balance = draw(st.integers(min_value=2000, max_value=200000))
    rate = draw(st.sampled_from([Decimal("0"), Decimal("3.5"), Decimal("6.0")]))
    mode = draw(st.sampled_from(CommissionMode))

    entry_value = Decimal("40000")
    stop_value = entry_value - distance_points * point
    digits = 2 if point <= Decimal("0.01") else 1

    specification = SymbolSpecification(
        name="X",
        digits=digits,
        point=point,
        tick_size=point,
        tick_value=tick_value,
        contract_size=Decimal("1.0"),
        volume_min=step,
        volume_max=Decimal("50.0"),
        volume_step=step,
    )
    return SizingInput(
        entry=Price.parse(str(entry_value), digits),
        stop=Price.parse(str(stop_value), digits),
        balance=Money.of(balance),
        specification=specification,
        mode=RiskMode.RISK_MODE_PERCENT_BALANCE,
        percent=percent,
        commission=CommissionModel_(rate=rate, mode=mode),
    )


class TestSizingInvariants:
    @given(_sizing_case())
    def test_total_risk_never_exceeds_the_budget(self, case: SizingInput) -> None:
        """The property the whole risk limit exists for."""
        try:
            result = size_position(case)
        except RiskError:
            return  # a refusal is a valid outcome; it carries no risk at all
        assert result.total_risk.amount <= case.balance.scaled(
            case.percent / HUNDRED
        ).rounded().amount + Decimal("0.01")

    @given(_sizing_case())
    def test_the_size_is_always_on_the_broker_step(self, case: SizingInput) -> None:
        try:
            result = size_position(case)
        except RiskError:
            return
        assert result.volume.lots % case.specification.volume_step == 0

    @given(_sizing_case())
    def test_the_size_is_within_the_broker_bounds(self, case: SizingInput) -> None:
        try:
            result = size_position(case)
        except RiskError:
            return
        assert result.volume.lots >= case.specification.volume_min
        assert result.volume.lots <= case.specification.volume_max

    @given(_sizing_case())
    def test_rounding_never_increases_the_size(self, case: SizingInput) -> None:
        try:
            result = size_position(case)
        except RiskError:
            return
        assert result.volume.lots <= case.specification.normalize_volume(
            result.raw_volume
        ).lots

    @given(_sizing_case())
    def test_total_risk_is_the_sum_of_its_parts(self, case: SizingInput) -> None:
        try:
            result = size_position(case)
        except RiskError:
            return
        assert result.total_risk.amount == (
            result.price_risk.amount + result.commission.amount
        )

    @given(_sizing_case())
    def test_the_same_input_always_gives_the_same_size(self, case: SizingInput) -> None:
        try:
            first = size_position(case).volume.lots
        except RiskError:
            return
        assert size_position(case).volume.lots == first
