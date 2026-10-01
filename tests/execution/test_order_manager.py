"""Duplicate prevention and the no-resend guarantee.

These are the tests that justify the module's existence. Everything else in Phase 5 is
mechanical; this is the part where being wrong costs money twice.

The two properties:

1. **Idempotency.** Placing the same plan twice results in one order on the book. The second
   attempt is refused as a duplicate, matched on the deterministic client tag rather than on
   price.
2. **No resend after an unknown outcome.** When the venue may have accepted an order, the
   manager reports the ambiguity and does not send again.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from stop_order_scalp.domain.enums import Environment
from stop_order_scalp.domain.models import (
    AccountSnapshot,
    EnvironmentSettings,
    OrderIntent,
    TradePlan,
    TradeSignal,
)
from stop_order_scalp.domain.value_objects import SymbolSpecification
from stop_order_scalp.execution.order_manager import OrderManager, RefusalCode
from stop_order_scalp.execution.simulated_broker import SimulatedBroker
from stop_order_scalp.risk.risk_manager import RiskManager, RiskRequest

#: The environment passed to every ``place`` call below.
#:
#: A module constant rather than a fixture, because ``EnvironmentSettings`` is frozen and
#: carries no per-test state -- and because ``place()`` now requires the environment to be
#: stated explicitly, which is what stops a real broker being driven ungated.
DRY_RUN = EnvironmentSettings(environment=Environment.DRY_RUN)


class TestDuplicatePrevention:
    def test_the_same_plan_placed_twice_produces_one_order(
        self, manager: OrderManager, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        first = manager.place(broker, plan, settings=DRY_RUN)
        assert first.placed

        second = manager.place(broker, plan, settings=DRY_RUN)

        assert second.refused
        assert second.code == RefusalCode.DUPLICATE_ORDER
        assert len(broker.orders()) == 1

    def test_duplicate_detection_matches_the_tag_the_order_would_carry(
        self, manager: OrderManager, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        """The check must ask exactly the question the sent order answers.

        If these two derivations ever disagreed, duplicate protection would silently stop
        matching -- so the test compares them rather than trusting either.
        """
        manager.place(broker, plan, settings=DRY_RUN)
        resting = broker.orders()[0]

        assert resting.client_tag == OrderIntent.client_tag_for(plan)
        assert manager.is_duplicate(broker, plan)

    def test_a_plan_for_a_different_candle_is_not_a_duplicate(
        self,
        manager: OrderManager,
        broker: SimulatedBroker,
        plan: TradePlan,
        signal: TradeSignal,
        risk_manager: RiskManager,
        account: AccountSnapshot,
        us30: SymbolSpecification,
    ) -> None:
        """Identity includes the authorising candle, so a new candle is a new trade.

        A stale pending order from the previous candle must be replaced, not adopted, and
        that is only possible if the tag differs between candles.
        """
        later = TradeSignal(
            symbol=signal.symbol,
            side=signal.side,
            timeframe=signal.timeframe,
            direction_timeframe=signal.direction_timeframe,
            source_candle_open_time=signal.source_candle_open_time + timedelta(minutes=1),
            direction_candle_open_time=signal.direction_candle_open_time,
            order_kind=signal.order_kind,
            reference_price=signal.reference_price,
        )
        next_plan = risk_manager.plan_for(
            RiskRequest(
                signal=later,
                account=account,
                specification=us30,
                risk=risk_manager.risk_settings,
                target=risk_manager.target_settings,
            )
        )
        manager.place(broker, plan, settings=DRY_RUN)

        assert not manager.is_duplicate(broker, next_plan)
        assert manager.place(broker, next_plan, settings=DRY_RUN).placed
        assert len(broker.orders()) == 2

    def test_a_cancelled_order_does_not_block_a_replacement(
        self, manager: OrderManager, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        """Only *active* orders count. A cancelled one is gone."""
        placed = manager.place(broker, plan, settings=DRY_RUN)
        ticket = placed.require_order().ticket
        broker.cancel_order(ticket)

        assert not manager.is_duplicate(broker, plan)
        assert manager.place(broker, plan, settings=DRY_RUN).placed


class TestAlreadyInPosition:
    def test_a_second_order_is_refused_while_a_position_is_open(
        self, manager: OrderManager, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        """The strategy holds at most one position per symbol.

        Filling the pending order makes it a position, and the order is no longer on the
        book -- so without this check the next tick would place a second order.
        """
        manager.place(broker, plan, settings=DRY_RUN)
        # Drive price through the pending stop so it fills into a position.
        broker.publish("US30", Decimal("40010.0"), Decimal("40010.5"), digits=1)

        assert broker.positions()
        outcome = manager.place(broker, plan, settings=DRY_RUN)

        assert outcome.refused
        assert outcome.code == RefusalCode.ALREADY_IN_POSITION


class TestUnknownOutcome:
    """The venue took the order but never told us. Resending would double the position."""

    def test_an_unknown_outcome_is_reported_and_never_resent(
        self, manager: OrderManager, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        broker.fail_next_send(outcome="unknown")

        outcome = manager.place(broker, plan, settings=DRY_RUN)

        assert outcome.refused
        assert outcome.code == "execution_unknown"
        assert broker.unknown_outcomes == 1
        # Exactly one send was attempted. This is the whole point.
        assert len(broker.send_attempts) == 1

    def test_the_ambiguous_order_is_actually_on_the_book(
        self, manager: OrderManager, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        """Re-reading the book is what resolves the ambiguity, so it has to find the order."""
        broker.fail_next_send(outcome="unknown")
        manager.place(broker, plan, settings=DRY_RUN)

        resting = broker.orders()

        assert len(resting) == 1
        assert resting[0].client_tag == OrderIntent.client_tag_for(plan)

    def test_re_observation_finds_the_order_so_the_next_attempt_is_a_duplicate(
        self, manager: OrderManager, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        """The recovery path: re-read, adopt what is there, do not send again.

        Without this, the caller is forced into a choice between leaving a phantom order and
        creating a real duplicate.
        """
        broker.fail_next_send(outcome="unknown")
        assert manager.place(broker, plan, settings=DRY_RUN).refused

        # Next cycle: the manager re-reads the book first.
        second = manager.place(broker, plan, settings=DRY_RUN)

        assert second.refused
        assert second.code == RefusalCode.DUPLICATE_ORDER
        assert len(broker.send_attempts) == 1, "the order was sent twice"
        assert len(broker.orders()) == 1

    def test_the_failure_is_one_shot(
        self, manager: OrderManager, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        """Armed failures do not accumulate; the venue recovers."""
        broker.fail_next_send(outcome="unknown")
        manager.place(broker, plan, settings=DRY_RUN)

        assert len(broker.send_attempts) == 1
        broker.cancel_order(broker.orders()[0].ticket)
        assert manager.place(broker, plan, settings=DRY_RUN).placed


class TestGateIntegration:
    def test_a_closed_gate_refuses_before_any_broker_call(
        self, manager: OrderManager, broker: SimulatedBroker, plan: TradePlan, live_settings: EnvironmentSettings
    ) -> None:
        """Cheap local checks come first, so a refusal costs no broker round trip."""
        outcome = manager.place(broker, plan, settings=live_settings)

        assert outcome.refused
        assert outcome.code == RefusalCode.GATE_CLOSED
        assert broker.send_attempts == []

    def test_an_open_gate_lets_a_duplicate_check_through(
        self,
        manager: OrderManager,
        broker: SimulatedBroker,
        plan: TradePlan,
        live_settings: EnvironmentSettings,
    ) -> None:
        """The gate is about permission, not about duplication. Both still apply."""
        from stop_order_scalp.execution.gates import OrderGate

        permitted = OrderManager(
            manager.execution_settings, manager.order_settings, manager.risk, gate=OrderGate(enabled=True)
        )
        assert permitted.place(broker, plan, settings=live_settings).placed
        assert permitted.place(broker, plan, settings=live_settings).refused


class TestRiskIntegration:
    def test_the_manager_re_assesses_against_live_account_state(
        self, manager: OrderManager, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        """A size computed earlier is not trusted; the balance may have moved."""
        verdict = manager.assess(plan, broker)

        assert verdict.accepted
        assert verdict.volume is not None
        assert verdict.total_risk is not None

    def test_a_rejected_risk_verdict_stops_the_placement(
        self, manager: OrderManager, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        from stop_order_scalp.domain.models import RiskAssessment

        rejected = RiskAssessment.reject("stale_signal", "the signal is no longer valid")

        outcome = manager.place(broker, plan, settings=DRY_RUN, assessment=rejected)

        assert outcome.refused
        assert outcome.code == RefusalCode.NOT_ASSESSED
        assert broker.send_attempts == []
