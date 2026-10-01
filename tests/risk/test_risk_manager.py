"""The risk manager's verdict, and the rejection codes.

The codes are the point of this module's test coverage. A rejection that only says "too
risky" forces every caller to parse prose; a code can be counted, alerted on and compared
across runs. So each rejection path has a test that asserts the *code*, not just that the
trade was refused.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from risk.conftest import account_with, signal_with
from stop_order_scalp.domain.enums import RiskMode, Side, TargetMode
from stop_order_scalp.domain.exceptions import RiskError
from stop_order_scalp.domain.models import AccountSnapshot, TradeSignal
from stop_order_scalp.domain.value_objects import Money, Price, SymbolSpecification
from stop_order_scalp.infrastructure.config import RiskSettings, TargetSettings
from stop_order_scalp.risk.risk_manager import (
    RejectionCode,
    RiskManager,
    RiskRequest,
)


def request_for(
    *,
    signal: TradeSignal | None = None,
    account: AccountSnapshot | None = None,
    specification: SymbolSpecification | None = None,
    risk: RiskSettings | None = None,
    target: TargetSettings | None = None,
) -> RiskRequest:
    from risk.conftest import us30_default

    return RiskRequest(
        signal=signal if signal is not None else signal_with(),
        account=account if account is not None else _default_account(),
        specification=specification if specification is not None else us30_default(),
        risk=risk if risk is not None else RiskSettings(),
        target=target if target is not None else TargetSettings(),
    )


def _default_account() -> AccountSnapshot:
    return AccountSnapshot(
        login=1234567,
        server="DemoServer",
        currency="USD",
        balance=Money.of(10000),
        equity=Money.of(10000),
        margin_used=Money.of(0),
        margin_free=Money.of(10000),
    )


@pytest.fixture
def manager() -> RiskManager:
    return RiskManager(RiskSettings(), TargetSettings())


class TestTheAcceptedPath:
    def test_the_baseline_trade_is_accepted(self, manager: RiskManager) -> None:
        verdict = manager.assess(request_for())
        assert verdict.accepted
        assert verdict.code == "ok"

    def test_it_reports_the_worked_example_numbers(self, manager: RiskManager) -> None:
        verdict = manager.assess(request_for())
        assert verdict.volume is not None and verdict.volume.lots == Decimal("0.4")
        assert verdict.price_risk is not None and verdict.price_risk.amount == Decimal("40.00")
        assert verdict.commission is not None and verdict.commission.amount == Decimal("2.40")
        assert verdict.total_risk is not None and verdict.total_risk.amount == Decimal("42.40")

    def test_it_reports_the_risk_fraction(self, manager: RiskManager) -> None:
        assert manager.assess(request_for()).risk_fraction == Decimal("0.4240")

    def test_the_reason_is_human_readable(self, manager: RiskManager) -> None:
        assert "0.4 lots" in manager.assess(request_for()).reason

    def test_a_sell_is_sized_the_same_way(self, manager: RiskManager) -> None:
        from stop_order_scalp.domain.enums import OrderKind

        sell = signal_with(side=Side.SIDE_SELL, order_kind=OrderKind.ORDER_KIND_SELL_STOP)
        assert manager.assess(request_for(signal=sell)).accepted


class TestThePlan:
    def test_the_plan_carries_the_three_separate_figures(
        self, manager: RiskManager
    ) -> None:
        plan = manager.plan_for(request_for())
        assert plan.price_risk.amount == Decimal("40.00")
        assert plan.commission.amount == Decimal("2.40")
        assert plan.total_risk.amount == Decimal("42.40")

    def test_the_plan_places_the_stop_and_target_by_the_baseline(
        self, manager: RiskManager
    ) -> None:
        plan = manager.plan_for(request_for())
        # 100 points below, 1000 above, on point 0.1.
        assert plan.stop_loss == Price.parse("39990.0", 1)
        assert plan.take_profit == Price.parse("40100.0", 1)

    def test_the_plan_id_is_stable(self, manager: RiskManager) -> None:
        assert manager.plan_for(request_for()).plan_id == manager.plan_for(request_for()).plan_id

    def test_the_plan_id_is_safe_for_a_comment(
        self, manager: RiskManager
    ) -> None:
        # The plan id reaches a broker comment, which is 31 characters.
        assert "/" not in manager.plan_for(request_for()).plan_id
        assert "@" not in manager.plan_for(request_for()).plan_id


class TestRejections:
    def test_a_disabled_account_is_refused_with_a_code(self, manager: RiskManager) -> None:
        blocked = account_with(10000, trade_allowed=False)
        verdict = manager.assess(request_for(account=blocked))
        assert not verdict.accepted
        assert verdict.code == RejectionCode.ACCOUNT_NOT_TRADEABLE

    def test_a_non_positive_balance_is_refused_with_a_code(self, manager: RiskManager) -> None:
        verdict = manager.assess(request_for(account=account_with(0)))
        assert verdict.code == RejectionCode.BALANCE_NOT_POSITIVE

    def test_a_budget_below_the_broker_minimum_is_refused_with_a_code(
        self, manager: RiskManager
    ) -> None:
        verdict = manager.assess(request_for(account=account_with(500)))
        assert verdict.code == RejectionCode.BELOW_MIN_VOLUME

    def test_the_refusal_explains_why_it_did_not_round_up(
        self, manager: RiskManager
    ) -> None:
        verdict = manager.assess(request_for(account=account_with(500)))
        assert "exceed the budget" in verdict.reason

    def test_a_stop_closer_than_stops_level_is_refused_with_a_code(
        self, manager: RiskManager
    ) -> None:
        # A 5-point stop against a broker minimum of 10.
        tight = RiskSettings()
        tight_target = TargetSettings(stop_loss_points=5, take_profit_points=1000)
        small = RiskManager(tight, tight_target)
        verdict = small.assess(request_for(risk=tight, target=tight_target))
        assert not verdict.accepted
        assert verdict.code == RejectionCode.STOP_TOO_CLOSE

    def test_the_risk_ceiling_is_a_second_independent_brake(
        self, manager: RiskManager
    ) -> None:
        # percent 5 % against a ceiling of 1 %. The ceiling is configured as a *fraction*
        # (0..1, default 1.0 meaning disabled), so 0.01 is a 1 % ceiling.
        risk = RiskSettings(percent=Decimal("5"), max_total_risk_fraction=Decimal("0.01"))
        verdict = RiskManager(risk, TargetSettings()).assess(request_for(risk=risk))
        assert verdict.code == RejectionCode.ABOVE_MAX_RISK

    def test_the_default_ceiling_never_binds(self, manager: RiskManager) -> None:
        """``max_total_risk_fraction: 1.0`` means disabled, so a 5 % trade passes it."""
        risk = RiskSettings(percent=Decimal("5"))
        assert RiskSettings().max_total_risk_fraction == Decimal("1.0")
        assert RiskManager(risk, TargetSettings()).assess(request_for(risk=risk)).accepted

    def test_a_stop_on_the_wrong_side_is_refused_with_a_code(
        self, manager: RiskManager
    ) -> None:
        bad = signal_with(stop_loss=Price.parse("40050.0", 1))
        verdict = manager.assess(request_for(signal=bad))
        assert verdict.code == RejectionCode.STOP_ON_WRONG_SIDE

    def test_a_signal_target_on_the_wrong_side_falls_back_rather_than_refusing(
        self, manager: RiskManager
    ) -> None:
        """A bad *target* must not stop a trade whose risk is already bounded.

        The stop is what bounds risk. Refusing the whole trade over a malformed target
        would be the wrong trade-off, so the target falls back to the configured one.
        """
        odd = signal_with(take_profit=Price.parse("39900.0", 1))
        verdict = manager.assess(request_for(signal=odd))
        assert verdict.accepted

    def test_a_signal_stop_is_honoured_when_protective(self, manager: RiskManager) -> None:
        # 50 points below entry: beyond the broker's 10-point stops_level, and cheap enough
        # that a $50 budget still supports a position.
        tighter = signal_with(stop_loss=Price.parse("39995.0", 1))
        plan = manager.plan_for(request_for(signal=tighter))
        assert plan.stop_loss == Price.parse("39995.0", 1)
        # A cheaper stop means a larger position for the same budget: $56 per lot rather
        # than $106, so $50 buys 0.8 lots instead of 0.4. Which is exactly why the stop has
        # to be fixed *before* the size, never after.
        assert plan.volume.lots == Decimal("0.8")

    def test_a_signal_stop_that_is_too_tight_is_refused_by_stops_level(
        self, manager: RiskManager
    ) -> None:
        # 5 points below, against a broker minimum of 10.
        tight = signal_with(stop_loss=Price.parse("39999.5", 1))
        verdict = manager.assess(request_for(signal=tight))
        assert verdict.code == RejectionCode.STOP_TOO_CLOSE


class TestCodesAreStable:
    def test_the_codes_are_lowercase_and_snake_case(self) -> None:
        for name in dir(RejectionCode):
            if name.startswith("_"):
                continue
            value = getattr(RejectionCode, name)
            assert value == value.lower()
            assert " " not in value

    def test_the_codes_are_unique(self) -> None:
        values = [
            getattr(RejectionCode, name)
            for name in dir(RejectionCode)
            if not name.startswith("_")
        ]
        assert len(values) == len(set(values))

    def test_every_refusal_names_a_known_code(self, manager: RiskManager) -> None:
        known = {
            getattr(RejectionCode, name)
            for name in dir(RejectionCode)
            if not name.startswith("_")
        }
        for account in (account_with(0), account_with(500), account_with(10000, trade_allowed=False)):
            verdict = manager.assess(request_for(account=account))
            assert verdict.code in known


class TestManagerConfiguration:
    def test_it_exposes_its_commission_model(self, manager: RiskManager) -> None:
        assert manager.commission.per_lot_round_trip == Decimal("6.0")

    def test_the_providers_are_replaceable(self) -> None:
        from stop_order_scalp.risk.stop_loss import FixedPointsStopProvider
        from stop_order_scalp.risk.take_profit import FixedPointsTargetProvider

        built = RiskManager(
            RiskSettings(),
            TargetSettings(),
            stop_provider=FixedPointsStopProvider(250),
            target_provider=FixedPointsTargetProvider(500),
        )
        plan = built.plan_for(request_for())
        assert plan.stop_loss == Price.parse("39975.0", 1)
        assert plan.take_profit == Price.parse("40050.0", 1)

    def test_plan_for_raises_on_a_refusal(self, manager: RiskManager) -> None:
        with pytest.raises(RiskError, match=RejectionCode.BELOW_MIN_VOLUME):
            manager.plan_for(request_for(account=account_with(500)))

    def test_the_raised_error_carries_the_code(self, manager: RiskManager) -> None:
        """So a caller that catches it can still log something machine-readable."""
        with pytest.raises(RiskError) as caught:
            manager.plan_for(request_for(account=account_with(500)))
        assert RejectionCode.BELOW_MIN_VOLUME in str(caught.value)


class TestModesThroughTheManager:
    def test_fixed_lot_mode_ignores_the_percentage(self) -> None:
        risk = RiskSettings(mode=RiskMode.RISK_MODE_FIXED_LOT, fixed_lot=Decimal("0.2"))
        verdict = RiskManager(risk, TargetSettings()).assess(request_for(risk=risk))
        assert verdict.volume is not None and verdict.volume.lots == Decimal("0.2")

    def test_risk_reward_mode_derives_the_target(self) -> None:
        target = TargetSettings(mode=TargetMode.TARGET_MODE_RISK_REWARD, risk_reward=Decimal("1.0"))
        plan = RiskManager(RiskSettings(), target).plan_for(request_for(target=target))
        # A 100-point stop with a 1:1 ratio gives a 100-point target.
        assert plan.take_profit == Price.parse("40010.0", 1)

    def test_the_baseline_is_ten_to_one(self, manager: RiskManager) -> None:
        """1000 points of target against a 100-point stop. Stated so it is not a surprise."""
        plan = manager.plan_for(request_for())
        entry, stop, target = plan.entry, plan.stop_loss, plan.take_profit
        risk = entry.absolute_distance_to(stop)
        reward = target.absolute_distance_to(entry)
        assert reward / risk == Decimal("10.0")
