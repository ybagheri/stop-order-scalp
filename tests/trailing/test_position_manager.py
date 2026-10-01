"""The position manager sequences break-even and trailing, and sends one request per change.

Two things are being pinned down here. First, the **ordering**: break-even wins outright, and
trailing is only consulted when break-even proposed nothing. Second, **idempotency in
practice**: an ordinary tick must cost no broker write at all, because the alternative is one
modify request per tick for the lifetime of a position.

The ordering is load-bearing rather than cosmetic, and
``tests/trailing/test_property_monotonic.py::TestBothRulesInOneTick`` states why: both
proposals are computed against the same position, so applying both in one tick applies a
trailing level derived from a stop that has already moved.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from stop_order_scalp.domain.enums import BreakEventKind, PositionState, Side
from stop_order_scalp.domain.exceptions import BrokerRejectedError, ExecutionUnknownError
from stop_order_scalp.domain.models import PositionRecord
from stop_order_scalp.domain.value_objects import SymbolSpecification
from stop_order_scalp.execution.position_manager import ActionKind, PositionManager
from stop_order_scalp.infrastructure.config import (
    BreakEvenSettings,
    RetrySettings,
    TrailingSettings,
)
from stop_order_scalp.trailing.trailing_stop import TrailingRefusal

from .conftest import position, tick


class RecordingBroker:
    """A broker that records every modification instead of performing one.

    Counts matter as much as the values: "no request was sent" is the assertion behind every
    idempotency test here.
    """

    def __init__(self, positions: list[PositionRecord] | None = None) -> None:
        self._positions = positions or []
        self.calls: list[tuple[int, Any]] = []
        self.reads = 0
        self.fail_with: Exception | None = None

    def positions(self, **_: Any) -> list[PositionRecord]:
        self.reads += 1
        if self.fail_with is not None:
            raise self.fail_with
        return list(self._positions)

    def modify_position(self, ticket: int, *, stop_loss: Any = None, take_profit: Any = None) -> bool:
        if self.fail_with is not None and isinstance(self.fail_with, type(self.fail_with)):
            raise self.fail_with
        self.calls.append((ticket, stop_loss))
        return True


class TestNoOpTicksCostNothing:
    def test_a_falling_price_sends_nothing(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        """Below the trailing level, nothing moves.

        Note this is *not* a price below the break-even trigger: with
        ``arm_after_points = 0`` a trailing stop is live from the start, so a rising price
        legitimately trails even while break-even is still un-armed. The case that must be
        free of requests is a price that would move the stop backwards.
        """
        broker = RecordingBroker([position(stop="40000.0")])

        update = manager.update(broker, tick("40001.0"), us30)

        assert broker.calls == []
        assert update.sent_count == 0
        assert update.actions[0].kind == ActionKind.HOLD

    def test_a_repeated_tick_after_a_move_sends_nothing(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        """The property that keeps a flat market from producing a request per tick."""
        broker = RecordingBroker([position()])
        armed = tick("40005.0")

        first = manager.update(broker, armed, us30)
        # The broker now reports the stop break-even just set, as a real one would.
        broker._positions = [position(stop="40000.0")]
        second = manager.update(broker, armed, us30)

        assert first.sent_count == 1
        assert second.sent_count == 0, "the same price must not send the same stop twice"
        assert len(broker.calls) == 1

    def test_a_falling_price_after_break_even_sends_nothing(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        """A pullback must not un-move the stop, and must not cost a request either."""
        broker = RecordingBroker([position()])
        manager.update(broker, tick("40005.0"), us30)
        broker._positions = [position(stop="40000.0")]

        after = manager.update(broker, tick("39999.0"), us30)

        assert after.sent_count == 0


class TestBreakEvenWins:
    def test_break_even_takes_precedence_over_trailing(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        """Both rules want a move; only break-even's is applied."""
        broker = RecordingBroker([position()])

        update = manager.update(broker, tick("40005.0"), us30)

        action = update.actions[0]
        assert action.kind == ActionKind.BREAK_EVEN_ARMED
        assert action.require_sent().proposed_stop.value == Decimal("40000.0")
        assert len(broker.calls) == 1

    def test_trailing_takes_over_after_break_even_is_applied(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        """The next real move is trailing's, not break-even repeating itself."""
        # The broker now reports the stop at entry, which is what break-even just set.
        moved = position(stop="40000.0")
        broker = RecordingBroker([moved])

        update = manager.update(broker, tick("40200.0"), us30)

        action = update.actions[0]
        assert action.kind == ActionKind.TRAILED
        # 100 points below a bid of 40200.0.
        assert action.require_sent().proposed_stop.value == Decimal("40190.0")

    def test_disabling_break_even_lets_trailing_through_immediately(
        self, us30: SymbolSpecification
    ) -> None:
        manager = PositionManager(
            BreakEvenSettings(enabled=False), TrailingSettings(distance_points=100)
        )
        broker = RecordingBroker([position()])

        update = manager.update(broker, tick("40200.0"), us30)

        assert update.actions[0].kind == ActionKind.TRAILED


class TestTrailingRule:
    def test_a_buy_stop_trails_the_bid_by_the_configured_distance(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        broker = RecordingBroker([position(stop="40000.0")])

        update = manager.update(broker, tick("40200.0"), us30)

        # 40200.0 bid minus 100 points (10.0 price units on point=0.1).
        assert update.actions[0].require_sent().proposed_stop.value == Decimal("40190.0")

    def test_a_sell_stop_trails_the_ask_by_the_configured_distance(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        short = position(Side.SIDE_SELL, entry="40000.0", stop="40000.0")
        broker = RecordingBroker([short])

        update = manager.update(broker, tick("39800.0", "39800.1"), us30)

        # ask 39800.1 plus 10.0
        assert update.actions[0].require_sent().proposed_stop.value == Decimal("39810.1")

    def test_a_falling_price_does_not_trail_a_buy_stop_down(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        """Monotonicity at the manager level, on the case the specification warns about."""
        broker = RecordingBroker([position(stop="40000.0")])

        update = manager.update(broker, tick("40001.0"), us30)

        assert update.sent_count == 0
        assert update.actions[0].code == TrailingRefusal.NOT_MONOTONIC

    def test_the_refusal_code_is_reported_on_a_hold(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        broker = RecordingBroker([position(stop="40000.0")])

        action = manager.update(broker, tick("40001.0"), us30).actions[0]

        assert action.code
        assert action.proposal is None


class TestMinimumStep:
    """``min_step_points``, for a market that drifts by a tick at a time.

    The baseline stop in these tests sits at 40050.0 so that a bid of 40100.0 produces a
    trailing level of 40090.0 -- an *improvement* over the current stop, so the minimum-step
    check is what decides, rather than monotonicity refusing first.
    """

    def test_a_move_below_the_minimum_step_is_refused(self, us30: SymbolSpecification) -> None:
        manager = PositionManager(
            BreakEvenSettings(enabled=False),
            TrailingSettings(distance_points=100, min_step_points=500),
        )
        broker = RecordingBroker([position(stop="40050.0")])

        update = manager.update(broker, tick("40100.0"), us30)

        # 40090.0 - 40050.0 is 400 points, below the 500 required.
        assert update.actions[0].code == TrailingRefusal.BELOW_MIN_STEP
        assert broker.calls == []

    def test_a_move_above_the_minimum_step_is_accepted(self, us30: SymbolSpecification) -> None:
        manager = PositionManager(
            BreakEvenSettings(enabled=False),
            TrailingSettings(distance_points=100, min_step_points=400),
        )
        broker = RecordingBroker([position(stop="40050.0")])

        update = manager.update(broker, tick("40100.0"), us30)

        assert update.sent_count == 1
        assert update.actions[0].require_sent().proposed_stop.value == Decimal("40090.0")


class TestFailureHandling:
    def test_an_unknown_outcome_propagates_and_is_not_retried(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        """Retrying a stop modification is as wrong as retrying a placement.

        The stop may have moved; a second request either moves it again or is interpreted
        against a state that no longer exists.
        """
        broker = RecordingBroker([position()])
        broker.fail_with = ExecutionUnknownError("the terminal lost track")

        with pytest.raises(ExecutionUnknownError):
            manager.update(broker, tick("40005.0"), us30)

        assert broker.calls == [], "the modification must not be retried"

    def test_a_rejection_propagates_rather_than_being_swallowed(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        """A silent no-op would hide the reason the stop never moved."""
        broker = RecordingBroker([position()])
        broker.fail_with = BrokerRejectedError("invalid stops")

        with pytest.raises(BrokerRejectedError):
            manager.update(broker, tick("40005.0"), us30)

    def test_positions_are_read_once_per_update(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        broker = RecordingBroker([position(), position(ticket=501)])

        manager.update(broker, tick("40001.0"), us30)

        assert broker.reads == 1

    def test_a_transient_read_failure_is_retried(
        self, us30: SymbolSpecification
    ) -> None:
        """A read is idempotent, so unlike a write it is safe to repeat."""
        from stop_order_scalp.domain.exceptions import BrokerNotConnectedError

        manager = PositionManager(
            BreakEvenSettings(trigger_points=50),
            TrailingSettings(distance_points=100),
            retry_settings=RetrySettings(max_attempts=3),
        )
        broker = RecordingBroker([position()])
        original = broker.positions

        calls = {"n": 0}

        def flaky(**kwargs: Any) -> list[PositionRecord]:
            calls["n"] += 1
            if calls["n"] < 3:
                raise BrokerNotConnectedError("terminal restarting")
            return original(**kwargs)

        broker.positions = flaky  # type: ignore[method-assign]

        update = manager.update(broker, tick("40001.0"), us30)

        assert calls["n"] == 3
        assert len(update) == 1


class TestEvaluationWithoutSending:
    def test_evaluate_never_touches_a_broker(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        """The decision path, separated from I/O, so a dry run can report what it would do."""
        action = manager.evaluate(position(), tick("40005.0"), us30)

        assert action.kind == ActionKind.BREAK_EVEN_ARMED
        assert action.sent is False
        assert action.proposal is not None

    def test_an_unsent_action_refuses_to_hand_over_its_proposal(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        action = manager.evaluate(position(), tick("40005.0"), us30)

        with pytest.raises(ValueError, match="nothing was sent"):
            action.require_sent()

    def test_a_sent_action_yields_its_proposal(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        broker = RecordingBroker([position()])

        action = manager.update(broker, tick("40005.0"), us30).actions[0]

        assert action.require_sent().proposed_stop.value == Decimal("40000.0")


class TestStatesAndEvents:
    def test_break_even_moves_the_position_to_its_own_state(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        broker = RecordingBroker([position()])

        action = manager.update(broker, tick("40005.0"), us30).actions[0]

        assert action.next_state is PositionState.POSITION_BREAK_EVEN_ARMED

    def test_trailing_moves_the_position_to_the_trailing_state(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        broker = RecordingBroker([position(stop="40000.0")])

        action = manager.update(broker, tick("40200.0"), us30).actions[0]

        assert action.next_state is PositionState.POSITION_TRAILING

    @pytest.mark.parametrize(
        ("kind", "event"),
        [
            (ActionKind.BREAK_EVEN_ARMED, BreakEventKind.BREAK_EVENT_BREAK_EVEN),
            (ActionKind.TRAILED, BreakEventKind.BREAK_EVENT_TRAILING),
            (ActionKind.HOLD, BreakEventKind.BREAK_EVENT_INITIAL),
        ],
    )
    def test_each_action_maps_to_its_journal_event(
        self, manager: PositionManager, kind: str, event: BreakEventKind
    ) -> None:
        """The journal and the decision must not drift apart."""
        from stop_order_scalp.execution.position_manager import PositionAction

        action = PositionAction(kind=kind, position_ticket=1)

        assert manager.event_for(action) is event

    def test_describe_reports_where_each_rule_points(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        """`status` needs to show both rules, not just the one that fired."""
        text = manager.describe(position(), tick("40200.0"), us30)

        assert "be=" in text
        assert "trailing_level=" in text


class TestNoPositions:
    def test_an_empty_book_produces_an_empty_update(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        update = manager.update(RecordingBroker([]), tick("40005.0"), us30)

        assert len(update) == 0
        assert update.sent_count == 0
        assert update.closed == ()

    def test_a_position_for_another_magic_number_is_filtered_by_the_broker(
        self, manager: PositionManager, us30: SymbolSpecification
    ) -> None:
        """The manager trusts the broker's filter rather than re-checking magic numbers."""
        broker = RecordingBroker([position()])

        manager.update(broker, tick("40005.0"), us30, magic_number=20260930)

        assert broker.reads == 1
