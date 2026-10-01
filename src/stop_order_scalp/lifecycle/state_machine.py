"""The system's lifecycle, as a table.

One :class:`LifecycleState` says what the system is doing; a transition table says what may
follow. Any edge not in the table raises
:class:`~stop_order_scalp.domain.exceptions.IllegalTransitionError`.

Why a table rather than ``if`` statements
----------------------------------------

An orchestration written as branching conditions looks equivalent and is not. The problem is
not the individual conditions -- each is defensible -- it is that the *set* of reachable paths
is then implicit and undocumented. A table makes the reachable set the artifact: it can be
printed, reviewed, and asserted against. It is also what makes a listener possible, because a
listener must not care which piece of code caused a transition.

The table is data, so a test can assert properties of it directly rather than inferring them by
driving the machine -- "is ``STATE_HALTED`` reachable from idle?" and "can any state reach
anything after halt?" are questions about the table, and asking them of the table is exact.

Two properties worth stating about the table itself
---------------------------------------------------

**``STATE_HALTED`` is absorbing.** Its only successor is ``STATE_IDLE``, and reaching it means
the system has stopped for an operator. Anything else would let a halted system resume on its
own, which is exactly what a halt is for. ``LifecycleState.is_terminal`` says the same thing,
and the two agree by test.

**Every state can reach idle.** There is no trap. A state from which the system cannot return
to a resting state is a bug an operator would discover as a process that will not start again.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable

from stop_order_scalp.domain.enums import LifecycleState
from stop_order_scalp.domain.exceptions import IllegalTransitionError

__all__ = [
    "TRANSITIONS",
    "LifecycleMachine",
    "TransitionListener",
    "TransitionRecord",
    "allowed_targets",
    "can_transition",
    "describe_table",
]


#: state -> the states reachable from it in one step.
#:
#: The single source of truth. :class:`LifecycleMachine` reads it and nothing else decides
#: what is legal, so there is no second copy to fall out of step.
TRANSITIONS: Final[dict[LifecycleState, frozenset[LifecycleState]]] = {
    LifecycleState.STATE_IDLE: frozenset(
        {
            LifecycleState.STATE_WAITING_FOR_SIGNAL,
            LifecycleState.STATE_RECONCILING,
            # A restart with an unresolved intent must be able to re-observe without first
            # waiting for a new signal, so VERIFYING is reachable from idle too.
            LifecycleState.STATE_VERIFYING,
            LifecycleState.STATE_HALTED,
        }
    ),
    LifecycleState.STATE_WAITING_FOR_SIGNAL: frozenset(
        {
            LifecycleState.STATE_SIGNAL_DETECTED,
            LifecycleState.STATE_BLOCKED,
            LifecycleState.STATE_HALTED,
            LifecycleState.STATE_RECONCILING,
        }
    ),
    LifecycleState.STATE_SIGNAL_DETECTED: frozenset(
        {
            LifecycleState.STATE_VALIDATING,
            # Rejection returns to waiting, not to idle: the system is healthy, this trade
            # simply did not qualify.
            LifecycleState.STATE_WAITING_FOR_SIGNAL,
            LifecycleState.STATE_BLOCKED,
            LifecycleState.STATE_HALTED,
        }
    ),
    LifecycleState.STATE_VALIDATING: frozenset(
        {
            LifecycleState.STATE_PENDING_ORDER_PLACED,
            LifecycleState.STATE_WAITING_FOR_SIGNAL,
            # An ambiguous send outcome. The only exit is re-observation: this state must not
            # be able to reach a placement again without the broker having been asked.
            LifecycleState.STATE_VERIFYING,
            LifecycleState.STATE_BLOCKED,
            LifecycleState.STATE_HALTED,
        }
    ),
    LifecycleState.STATE_PENDING_ORDER_PLACED: frozenset(
        {
            LifecycleState.STATE_WAITING_FOR_TRIGGER,
            # Filled before we were told, or found on the book during recovery.
            LifecycleState.STATE_POSITION_OPEN,
            LifecycleState.STATE_VERIFYING,
            LifecycleState.STATE_RECONCILING,
            LifecycleState.STATE_BLOCKED,
            LifecycleState.STATE_HALTED,
        }
    ),
    LifecycleState.STATE_WAITING_FOR_TRIGGER: frozenset(
        {
            LifecycleState.STATE_POSITION_OPEN,
            LifecycleState.STATE_WAITING_FOR_SIGNAL,
            LifecycleState.STATE_RECONCILING,
            LifecycleState.STATE_VERIFYING,
            LifecycleState.STATE_BLOCKED,
            LifecycleState.STATE_HALTED,
        }
    ),
    LifecycleState.STATE_POSITION_OPEN: frozenset(
        {
            LifecycleState.STATE_BREAK_EVEN_ARMED,
            LifecycleState.STATE_TRAILING,
            LifecycleState.STATE_POSITION_CLOSED,
            # The position disappeared from the book. Not a close -- recovery has to find out
            # which, and RECONCILING is where that happens.
            LifecycleState.STATE_RECONCILING,
            LifecycleState.STATE_BLOCKED,
            LifecycleState.STATE_HALTED,
        }
    ),
    LifecycleState.STATE_BREAK_EVEN_ARMED: frozenset(
        {
            LifecycleState.STATE_TRAILING,
            LifecycleState.STATE_POSITION_CLOSED,
            LifecycleState.STATE_RECONCILING,
            LifecycleState.STATE_BLOCKED,
            LifecycleState.STATE_HALTED,
        }
    ),
    LifecycleState.STATE_TRAILING: frozenset(
        {
            LifecycleState.STATE_POSITION_CLOSED,
            LifecycleState.STATE_RECONCILING,
            LifecycleState.STATE_BLOCKED,
            LifecycleState.STATE_HALTED,
        }
    ),
    LifecycleState.STATE_POSITION_CLOSED: frozenset(
        {
            # The specification: place the replacement immediately after a close.
            LifecycleState.STATE_VALIDATING,
            LifecycleState.STATE_WAITING_FOR_SIGNAL,
            LifecycleState.STATE_RECONCILING,
            LifecycleState.STATE_HALTED,
        }
    ),
    LifecycleState.STATE_RECONCILING: frozenset(
        {
            # Every state the broker's authoritative state can imply. Recovery is the only
            # place that decides which, so it needs to reach all of them.
            LifecycleState.STATE_WAITING_FOR_SIGNAL,
            LifecycleState.STATE_PENDING_ORDER_PLACED,
            LifecycleState.STATE_POSITION_OPEN,
            LifecycleState.STATE_POSITION_CLOSED,
            LifecycleState.STATE_VERIFYING,
            LifecycleState.STATE_BLOCKED,
            LifecycleState.STATE_HALTED,
            LifecycleState.STATE_IDLE,
        }
    ),
    LifecycleState.STATE_VERIFYING: frozenset(
        {
            # Having asked the broker, the ambiguity is resolved either way. The point is that
            # VERIFYING cannot reach VALIDATING directly: nothing may be sent from here until
            # broker state has been read.
            LifecycleState.STATE_PENDING_ORDER_PLACED,
            LifecycleState.STATE_WAITING_FOR_SIGNAL,
            LifecycleState.STATE_POSITION_OPEN,
            LifecycleState.STATE_POSITION_CLOSED,
            LifecycleState.STATE_RECONCILING,
            LifecycleState.STATE_BLOCKED,
            LifecycleState.STATE_HALTED,
        }
    ),
    LifecycleState.STATE_BLOCKED: frozenset(
        {
            # Operator action, or a reconciliation that cleared the contradiction.
            LifecycleState.STATE_IDLE,
            LifecycleState.STATE_RECONCILING,
            LifecycleState.STATE_HALTED,
        }
    ),
    LifecycleState.STATE_HALTED: frozenset({LifecycleState.STATE_IDLE}),
}


def allowed_targets(state: LifecycleState) -> frozenset[LifecycleState]:
    """The states reachable from ``state``."""
    return TRANSITIONS[state]


def can_transition(source: LifecycleState, target: LifecycleState) -> bool:
    """Whether the edge exists. For a caller that wants to ask rather than be refused."""
    return target in allowed_targets(source)


@runtime_checkable
class TransitionListener(Protocol):
    """What a listener is handed.

    A ``Protocol``, and that inheritance is load-bearing rather than decorative: declared as a
    plain class it would be a *nominal* base, so only something explicitly subclassing it would
    satisfy it, and the duck-typed listener this module exists to permit would not. The type
    checker caught exactly that.
    """

    def on_transition(self, record: TransitionRecord) -> None: ...


class _Unavailable(Exception):
    """Carries the reachable set as an exception's context.

    A private type used only as a ``raise ... from`` cause, so the information reaches a
    traceback without adding a field to :class:`IllegalTransitionError` -- which is a
    domain type other layers catch and whose constructor is part of the contract.
    """


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    """One edge, taken.

    ``from_state`` and ``to_state`` rather than ``previous``/``current``, because the words
    "from" and "to" are the ones that cannot be read backwards from a stack trace.
    """

    from_state: LifecycleState
    to_state: LifecycleState
    #: What caused it. Free-form and stable enough to alert on.
    reason: str = ""
    #: Anything the caller wants carried: a ticket, a client tag, a rejection code.
    detail: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_state": str(self.from_state),
            "to_state": str(self.to_state),
            "reason": self.reason,
            "detail": self.detail or {},
        }

    def __str__(self) -> str:
        note = f" ({self.reason})" if self.reason else ""
        return f"{self.from_state} -> {self.to_state}{note}"


Listener = Callable[[TransitionRecord], None]
"""A listener as a plain callable.

:class:`TransitionListener` is the richer protocol; this alias is for the common case of a
function that takes the record. Both work, because the machine only calls
``on_transition`` on objects and a callable is adapted at the point of use.
"""


class LifecycleMachine:
    """Holds the current state and refuses edges the table does not have.

    Stateless with respect to trading decisions -- the caller supplies both ends of every
    edge -- so one instance can serve the live loop, a backtest and a test, and a test can
    drive any path by naming the edges rather than by replaying the code that takes them.
    """

    __slots__ = ("_listeners", "_state")

    def __init__(
        self,
        state: LifecycleState = LifecycleState.STATE_IDLE,
        listeners: Iterable[TransitionListener | Listener] | None = None,
    ) -> None:
        self._state = state
        self._listeners: list[TransitionListener | Listener] = list(listeners or ())

    @property
    def state(self) -> LifecycleState:
        return self._state

    def can(self, target: LifecycleState) -> bool:
        return can_transition(self._state, target)

    @property
    def quiet(self) -> bool:
        """Whether the system holds broker exposure and owes nothing.

        True for the resting states and false for every state that owns exposure or an intent
        to. Exposed because "may I take new risk right now" is asked from the machine rather
        than re-derived from a list of states at each call site.
        """
        return not self._state.is_trading_state

    def move(
        self,
        target: LifecycleState,
        *,
        reason: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> TransitionRecord:
        """Transition, or refuse.

        :raises IllegalTransitionError: if the edge is not in the table. The message names
            both ends and what *was* available, because the usual cause is a caller assuming
            a path exists and the useful answer is which ones do.
        """
        if target not in allowed_targets(self._state):
            # Name what *was* available, because the usual cause of a refusal is a caller
            # assuming a path exists and the useful answer is which ones do. Attached as the
            # cause so it survives into a traceback without adding a field to a domain type
            # whose constructor other layers rely on.
            reachable = ", ".join(
                sorted(str(state) for state in allowed_targets(self._state))
            )
            raise IllegalTransitionError(self._state, target) from _Unavailable(reachable)

        record = TransitionRecord(
            from_state=self._state,
            to_state=target,
            reason=reason,
            detail=dict(detail) if detail else None,
        )
        self._state = target
        for listener in self._listeners:
            _notify(listener, record)
        return record

    def add_listener(self, listener: TransitionListener | Listener) -> None:
        self._listeners.append(listener)

    def reset(self, state: LifecycleState = LifecycleState.STATE_IDLE) -> None:
        """Force the state without taking an edge.

        Only for recovery and for tests, and deliberately not called ``move``: it is the one
        operation that can put the machine somewhere the table forbids, so it must be visible
        in review.
        """
        self._state = state

    def __repr__(self) -> str:
        return f"LifecycleMachine({self._state})"


def describe_table() -> str:
    """The table, rendered. For the documentation and for reviewing a change to it."""
    lines = []
    for source in sorted(TRANSITIONS, key=lambda state: str(state)):
        targets = ", ".join(sorted(str(state) for state in allowed_targets(source)))
        lines.append(f"{source} -> {targets or '(terminal)'}")
    return "\n".join(lines)


def _notify(listener: TransitionListener | Listener, record: TransitionRecord) -> None:
    """Call a listener, accepting either the protocol or a plain callable.

    Supporting both is worth the four lines: ``AuditLogger`` wants the protocol because it
    journals, and a test wants a one-line lambda that appends to a list.
    """
    method = getattr(listener, "on_transition", None)
    if method is not None:
        method(record)
    else:
        listener(record)  # type: ignore[operator]
