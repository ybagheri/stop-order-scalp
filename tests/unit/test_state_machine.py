"""The transition table, asserted as data.

These tests ask questions *of the table* rather than by driving the machine through code that
happens to take an edge. That is the difference between "the reachable states are documented"
and "I read the code and agreed".
"""

from __future__ import annotations

import pytest

from stop_order_scalp.domain.enums import LifecycleState
from stop_order_scalp.domain.exceptions import IllegalTransitionError
from stop_order_scalp.lifecycle.state_machine import (
    TRANSITIONS,
LifecycleMachine,
    TransitionRecord,
    allowed_targets,
    can_transition,
    describe_table,
)

ALL = set(LifecycleState)


class TestTableShape:
    def test_every_state_has_an_entry(self) -> None:
        """A state with no row is unreachable, and the omission is invisible otherwise."""
        assert set(TRANSITIONS) == ALL

    def test_no_target_is_outside_the_enum(self) -> None:
        for source, targets in TRANSITIONS.items():
            assert targets <= ALL, f"{source} points outside the enum"

    def test_no_state_lists_itself(self) -> None:
        """A self-edge would make a transition record with no meaning."""
        for source, targets in TRANSITIONS.items():
            assert source not in targets, f"{source} has a self-edge"

    def test_describe_table_covers_every_state(self) -> None:
        text = describe_table()

        for state in ALL:
            assert str(state) in text


class TestHaltIsAbsorbing:
    def test_halted_reaches_only_idle(self) -> None:
        """Anything else would let a halted system resume on its own."""
        assert allowed_targets(LifecycleState.STATE_HALTED) == frozenset(
            {LifecycleState.STATE_IDLE}
        )

    def test_halt_is_reachable_from_every_trading_state(self) -> None:
        """An operator must be able to stop the system from wherever it is."""
        for state in ALL:
            if state is LifecycleState.STATE_HALTED:
                continue
            assert LifecycleState.STATE_HALTED in allowed_targets(state), (
                f"cannot halt from {state}"
            )

    def test_the_enum_and_the_table_agree_about_being_terminal(self) -> None:
        """``LifecycleState.is_terminal`` and the table are two statements of one fact."""
        for state in ALL:
            absorbing = allowed_targets(state) <= {LifecycleState.STATE_IDLE}
            if state is LifecycleState.STATE_HALTED:
                assert absorbing, "halted must be absorbing"
            else:
                assert state.is_terminal is False


class TestNoTraps:
    def test_every_state_can_reach_idle(self) -> None:
        """A state that cannot return to a resting state is a process that will not restart."""
        for state in ALL:
            assert _reaches(state, LifecycleState.STATE_IDLE), f"{state} is a trap"

    def test_every_state_can_reach_reconciling(self) -> None:
        """Recovery must be reachable from anywhere; that is the point of it."""
        for state in ALL:
            if state is LifecycleState.STATE_RECONCILING:
                continue
            assert _reaches(state, LifecycleState.STATE_RECONCILING), (
                f"cannot reconcile from {state}"
            )


class TestVerifyIsASafePlace:
    def test_verifying_cannot_reach_validating_directly(self) -> None:
        """The reason ``VERIFYING`` exists.

        Nothing may be sent from the state that exists because a send's outcome is unknown,
        until broker state has been read. An edge to ``VALIDATING`` would be a path from
        "maybe an order is out there" straight to sending another one.
        """
        assert not can_transition(
            LifecycleState.STATE_VERIFYING, LifecycleState.STATE_VALIDATING
        )

    def test_verifying_cannot_reach_signal_detected(self) -> None:
        assert not can_transition(
            LifecycleState.STATE_VERIFYING, LifecycleState.STATE_SIGNAL_DETECTED
        )

    def test_verifying_can_still_resolve_either_way(self) -> None:
        """The ambiguity resolves into an order existing or not; both must be reachable."""
        targets = allowed_targets(LifecycleState.STATE_VERIFYING)
        assert LifecycleState.STATE_PENDING_ORDER_PLACED in targets
        assert LifecycleState.STATE_WAITING_FOR_SIGNAL in targets

    def test_idle_can_reach_verifying_for_a_restart_with_an_open_question(self) -> None:
        """A process restarting with an unresolved intent must not have to fake a signal."""
        assert can_transition(
            LifecycleState.STATE_IDLE, LifecycleState.STATE_VERIFYING
        )


class TestValidationIsTheOnlyDoorToSending:
    def test_placing_an_order_is_reachable_only_through_validating(self) -> None:
        """``PENDING_ORDER_PLACED`` means an order exists, so nothing may jump to it."""
        sources = [
            state
            for state in ALL
            if LifecycleState.STATE_PENDING_ORDER_PLACED in allowed_targets(state)
        ]

        assert set(sources) == {
            LifecycleState.STATE_RECONCILING,
            LifecycleState.STATE_VALIDATING,
            LifecycleState.STATE_VERIFYING,
        }

    def test_replacement_after_a_close_goes_through_validation(self) -> None:
        """The specification's replacement path must not bypass risk assessment."""
        assert can_transition(
            LifecycleState.STATE_POSITION_CLOSED, LifecycleState.STATE_VALIDATING
        )

    def test_position_closed_cannot_place_directly(self) -> None:
        assert not can_transition(
            LifecycleState.STATE_POSITION_CLOSED, LifecycleState.STATE_PENDING_ORDER_PLACED
        )


class TestMachine:
    def test_it_starts_idle(self) -> None:
        assert LifecycleMachine().state is LifecycleState.STATE_IDLE

    def test_a_legal_edge_moves(self) -> None:
        machine = LifecycleMachine()

        machine.move(LifecycleState.STATE_WAITING_FOR_SIGNAL)

        assert machine.state is LifecycleState.STATE_WAITING_FOR_SIGNAL

    def test_an_illegal_edge_raises(self) -> None:
        machine = LifecycleMachine()

        with pytest.raises(IllegalTransitionError):
            machine.move(LifecycleState.STATE_POSITION_OPEN)

    def test_a_refusal_names_both_ends(self) -> None:
        machine = LifecycleMachine()

        with pytest.raises(IllegalTransitionError) as caught:
            machine.move(LifecycleState.STATE_TRAILING)

        assert caught.value.source is LifecycleState.STATE_IDLE
        assert caught.value.target is LifecycleState.STATE_TRAILING

    def test_a_refusal_lists_what_was_available(self) -> None:
        """The usual cause is a caller assuming a path exists; the useful answer is which."""
        machine = LifecycleMachine()

        with pytest.raises(IllegalTransitionError) as caught:
            machine.move(LifecycleState.STATE_TRAILING)

        assert "waiting_for_signal" in str(caught.value.__cause__)

    def test_a_refusal_leaves_the_state_untouched(self) -> None:
        machine = LifecycleMachine()

        with pytest.raises(IllegalTransitionError):
            machine.move(LifecycleState.STATE_POSITION_OPEN)

        assert machine.state is LifecycleState.STATE_IDLE

    def test_can_asks_without_refusing(self) -> None:
        machine = LifecycleMachine()

        assert machine.can(LifecycleState.STATE_WAITING_FOR_SIGNAL)
        assert not machine.can(LifecycleState.STATE_POSITION_OPEN)


class TestQuietness:
    """``quiet`` is the question "may I take new risk right now"."""

    @pytest.mark.parametrize(
        "state",
        [
            LifecycleState.STATE_IDLE,
            LifecycleState.STATE_WAITING_FOR_SIGNAL,
            LifecycleState.STATE_POSITION_CLOSED,
            LifecycleState.STATE_SIGNAL_DETECTED,
        ],
    )
    def test_resting_states_are_quiet(self, state: LifecycleState) -> None:
        assert LifecycleMachine(state).quiet

    @pytest.mark.parametrize(
        "state",
        [
            LifecycleState.STATE_PENDING_ORDER_PLACED,
            LifecycleState.STATE_WAITING_FOR_TRIGGER,
            LifecycleState.STATE_POSITION_OPEN,
            LifecycleState.STATE_BREAK_EVEN_ARMED,
            LifecycleState.STATE_TRAILING,
        ],
    )
    def test_states_holding_exposure_are_not_quiet(self, state: LifecycleState) -> None:
        assert not LifecycleMachine(state).quiet

    def test_quiet_agrees_with_is_trading_state(self) -> None:
        """One definition, two names. They must not be able to drift."""
        for state in ALL:
            machine = LifecycleMachine(state)
            assert machine.quiet is (not state.is_trading_state), (
                f"{state}: quiet and is_trading_state disagree"
            )


class RecordingListener:
    """Captures every edge. The protocol form, not a lambda."""

    def __init__(self) -> None:
        self.records: list[TransitionRecord] = []

    def on_transition(self, record: TransitionRecord) -> None:
        self.records.append(record)


class TestListeners:
    def test_a_listener_sees_every_edge(self) -> None:
        seen: list[TransitionRecord] = []

        machine = LifecycleMachine(listeners=[seen.append])
        machine.move(LifecycleState.STATE_WAITING_FOR_SIGNAL)
        machine.move(LifecycleState.STATE_SIGNAL_DETECTED)

        assert len(seen) == 2

    def test_the_protocol_form_works_too(self) -> None:
        listener = RecordingListener()
        machine = LifecycleMachine(listeners=[listener])

        machine.move(LifecycleState.STATE_WAITING_FOR_SIGNAL)

        assert len(listener.records) == 1

    def test_a_listener_receives_both_ends(self) -> None:
        listener = RecordingListener()
        machine = LifecycleMachine(listeners=[listener])

        record = machine.move(
            LifecycleState.STATE_WAITING_FOR_SIGNAL, reason="start", detail={"n": 1}
        )

        assert record.from_state is LifecycleState.STATE_IDLE
        assert record.to_state is LifecycleState.STATE_WAITING_FOR_SIGNAL
        assert record.reason == "start"
        assert record.detail == {"n": 1}

    def test_a_listener_can_be_added_later(self) -> None:
        listener = RecordingListener()
        machine = LifecycleMachine()

        machine.add_listener(listener)
        machine.move(LifecycleState.STATE_WAITING_FOR_SIGNAL)

        assert len(listener.records) == 1

    def test_a_failing_listener_does_not_corrupt_the_state(self) -> None:
        """The edge is already taken by the time listeners run, so the state is consistent.

        Whether the exception propagates is the caller's problem; what matters is that the
        machine is not left describing a transition it did not make.
        """

        class Exploding:
            def on_transition(self, record: TransitionRecord) -> None:
                raise RuntimeError("listener failed")

        machine = LifecycleMachine(listeners=[Exploding()])

        with pytest.raises(RuntimeError):
            machine.move(LifecycleState.STATE_WAITING_FOR_SIGNAL)

        assert machine.state is LifecycleState.STATE_WAITING_FOR_SIGNAL

    def test_a_record_serialises_for_the_journal(self) -> None:
        listener = RecordingListener()
        machine = LifecycleMachine(listeners=[listener])

        machine.move(LifecycleState.STATE_WAITING_FOR_SIGNAL, reason="start")
        record = listener.records[0]

        assert record.to_dict()["to_state"] == "waiting_for_signal"

class TestReset:
    def test_reset_forces_a_state_without_an_edge(self) -> None:
        """Recovery establishes state; it does not move there through the table."""
        machine = LifecycleMachine()

        machine.reset(LifecycleState.STATE_POSITION_OPEN)

        assert machine.state is LifecycleState.STATE_POSITION_OPEN

    def test_reset_defaults_to_idle(self) -> None:
        machine = LifecycleMachine(LifecycleState.STATE_TRAILING)

        machine.reset()

        assert machine.state is LifecycleState.STATE_IDLE


def _walk_to(machine: LifecycleMachine, target: LifecycleState) -> None:
    """Take the ordinary route *to and including* ``target``.

    Every state on the way is reached by moving, never by resetting -- otherwise the test
    would pass against a table with those edges removed, which is the whole point of driving
    it through the real machine.
    """
    route = [
        LifecycleState.STATE_WAITING_FOR_SIGNAL,
        LifecycleState.STATE_SIGNAL_DETECTED,
        LifecycleState.STATE_VALIDATING,
        LifecycleState.STATE_PENDING_ORDER_PLACED,
        LifecycleState.STATE_WAITING_FOR_TRIGGER,
        LifecycleState.STATE_POSITION_OPEN,
        LifecycleState.STATE_BREAK_EVEN_ARMED,
        LifecycleState.STATE_TRAILING,
        LifecycleState.STATE_POSITION_CLOSED,
    ]
    for state in route:
        machine.move(state)
        if state is target:
            return
    raise AssertionError(f"{target} is not on the documented route")


class TestTheDocumentedPath:
    def test_the_full_happy_path_is_legal(self) -> None:
        """The specification's path, end to end, one assertion per edge."""
        machine = LifecycleMachine()
        path = [
            LifecycleState.STATE_WAITING_FOR_SIGNAL,
            LifecycleState.STATE_SIGNAL_DETECTED,
            LifecycleState.STATE_VALIDATING,
            LifecycleState.STATE_PENDING_ORDER_PLACED,
            LifecycleState.STATE_WAITING_FOR_TRIGGER,
            LifecycleState.STATE_POSITION_OPEN,
            LifecycleState.STATE_BREAK_EVEN_ARMED,
            LifecycleState.STATE_TRAILING,
            LifecycleState.STATE_POSITION_CLOSED,
            # The replacement: full re-evaluation, not a copy of the previous plan.
            LifecycleState.STATE_VALIDATING,
            LifecycleState.STATE_PENDING_ORDER_PLACED,
            LifecycleState.STATE_WAITING_FOR_TRIGGER,
        ]

        for target in path:
            machine.move(target)

        assert machine.state is LifecycleState.STATE_WAITING_FOR_TRIGGER

    def test_the_unknown_outcome_path_is_legal(self) -> None:
        """Placing, not hearing back, re-observing, then finding the order."""
        machine = LifecycleMachine()
        _walk_to(machine, LifecycleState.STATE_VALIDATING)

        machine.move(LifecycleState.STATE_VERIFYING)
        machine.move(LifecycleState.STATE_PENDING_ORDER_PLACED)

        assert machine.state is LifecycleState.STATE_PENDING_ORDER_PLACED

    def test_the_unknown_outcome_path_can_also_find_nothing(self) -> None:
        machine = LifecycleMachine()
        _walk_to(machine, LifecycleState.STATE_VALIDATING)

        machine.move(LifecycleState.STATE_VERIFYING)
        machine.move(LifecycleState.STATE_WAITING_FOR_SIGNAL)

        assert machine.state is LifecycleState.STATE_WAITING_FOR_SIGNAL


def _reaches(source: LifecycleState, target: LifecycleState) -> bool:
    """Breadth-first reachability over the table.

    Computed rather than hard-coded, so a change to the table is reflected in the answer
    instead of being contradicted by a stale expectation.
    """
    if source is target:
        return True
    seen = {source}
    frontier = [source]
    while frontier:
        current = frontier.pop()
        for nxt in allowed_targets(current):
            if nxt is target:
                return True
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return False
