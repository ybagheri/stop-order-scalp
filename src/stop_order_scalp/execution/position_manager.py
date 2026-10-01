"""Decides when a managed stop moves, and sends the one request that moves it.

This is the layer between the two *proposers* in :mod:`stop_order_scalp.trailing` and the
broker. The split is deliberate: break-even and trailing decide **what** the stop should be,
purely from a position, a tick and a specification, with no I/O. That is what makes the
monotonicity guarantee testable over generated price sequences without a broker. This module
decides **whether now is the time to send it**, and never recomputes the level itself.

The ordering is break-even, then trailing
----------------------------------------

Break-even is evaluated first and, once its trigger is reached, it has priority: the stop
moves to entry before trailing is allowed to consider anything. Trailing then refines it on
subsequent ticks. The alternative -- evaluating both and taking whichever moves the stop
further -- produces the same end state in ordinary conditions but differs when the two
disagree, and "whichever is further" is ambiguous once one of them has been refused for
being a no-op.

Both proposers are idempotent, so the common case costs one broker read and no write: a tick
that changes nothing produces ``ALREADY_APPLIED`` or ``NOT_MONOTONIC`` and returns before
:mod:`stop_order_scalp.execution.retry` or the broker is ever reached.

No send is ever retried
-----------------------

:meth:`PositionManager.update` calls ``modify_position`` exactly once when it has a real
change, and lets an :class:`~stop_order_scalp.domain.exceptions.ExecutionUnknownError`
propagate. Retrying a stop modification is as wrong as retrying an order placement: the stop
may have moved, and a second request moves it again or is interpreted relative to a state
that no longer exists. ``MetaTrader5Broker.modify_position`` classifies the retcode with the
same ``classify`` used for placements, so the same three-bucket table applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stop_order_scalp.domain.enums import BreakEventKind, PositionState, Side
from stop_order_scalp.domain.interfaces import OrderBookReader
from stop_order_scalp.domain.models import ManagedStop, PositionRecord, Tick
from stop_order_scalp.domain.value_objects import Price, SymbolSpecification
from stop_order_scalp.execution.retry import retry_read
from stop_order_scalp.infrastructure.config import (
    BreakEvenSettings,
    RetrySettings,
    TrailingSettings,
)
from stop_order_scalp.trailing.break_even import BreakEvenProvider, ConfiguredBreakEvenProvider
from stop_order_scalp.trailing.trailing_stop import TrailingProvider, TrailingStopProvider

__all__ = ["PositionAction", "PositionManager", "PositionUpdate"]


class ActionKind:
    """What happened to a position on one evaluation. An interface, not an enum.

    Plain strings, because these values reach the journal and alerts verbatim, and renaming
    an enum member would silently change what an operator's saved query matches.
    """

    CLOSED: str = "closed"
    BREAK_EVEN_ARMED: str = "break_even_armed"
    TRAILED: str = "trailed"
    HOLD: str = "hold"
    REFUSED: str = "refused"


@dataclass(frozen=True, slots=True)
class PositionAction:
    """One decision about one position, and whether it was sent."""

    kind: str
    position_ticket: int
    #: The proposal that was acted on, when one was.
    proposal: ManagedStop | None = None
    #: Why nothing was sent. A stable code from the proposer, or ``""``.
    code: str = ""
    reason: str = ""
    #: Whether a request actually reached the broker.
    sent: bool = False
    #: The state the position moves to when nothing else applies.
    next_state: PositionState = PositionState.POSITION_OPEN

    @property
    def changed(self) -> bool:
        return self.sent

    def require_sent(self) -> ManagedStop:
        if not self.sent or self.proposal is None:
            raise ValueError(f"nothing was sent: {self.kind}/{self.code}: {self.reason}")
        return self.proposal

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "position_ticket": self.position_ticket,
            "code": self.code,
            "reason": self.reason,
            "sent": self.sent,
            "old_stop": None if self.proposal is None else str(self.proposal.current_stop),
            "new_stop": None if self.proposal is None else str(self.proposal.proposed_stop),
        }

    def __str__(self) -> str:
        if self.proposal is not None:
            return f"PositionAction({self.kind} {self.position_ticket} -> {self.proposal})"
        return f"PositionAction({self.kind} {self.position_ticket}: {self.code})"


@dataclass(frozen=True, slots=True)
class PositionUpdate:
    """The outcome of evaluating every position once."""

    actions: tuple[PositionAction, ...]

    def __iter__(self) -> Any:
        return iter(self.actions)

    def __len__(self) -> int:
        return len(self.actions)

    @property
    def sent_count(self) -> int:
        """How many requests actually reached the broker."""
        return sum(1 for action in self.actions if action.sent)

    @property
    def closed(self) -> tuple[PositionAction, ...]:
        return tuple(a for a in self.actions if a.kind == ActionKind.CLOSED)

    def __str__(self) -> str:
        return f"PositionUpdate({len(self.actions)} positions, {self.sent_count} sent)"


class PositionManager:
    """Applies break-even and trailing to open positions, and notices when one closes.

    Holds no trading state: every evaluation is a pure function of the positions it is given
    plus the injected tick. That keeps one instance usable by the live loop, the backtester
    and a test, and means a manager cannot disagree with itself about what happened.
    """

    __slots__ = ("_break_even", "_retry", "_trailing")

    def __init__(
        self,
        break_even_settings: BreakEvenSettings,
        trailing_settings: TrailingSettings,
        *,
        break_even_provider: BreakEvenProvider | None = None,
        trailing_provider: TrailingProvider | None = None,
        retry_settings: RetrySettings | None = None,
    ) -> None:
        self._break_even = break_even_provider or ConfiguredBreakEvenProvider(
            break_even_settings
        )
        self._trailing = trailing_provider or TrailingStopProvider(trailing_settings)
        self._retry: RetrySettings | None = retry_settings

    @property
    def break_even(self) -> BreakEvenProvider:
        return self._break_even

    @property
    def trailing(self) -> TrailingProvider:
        return self._trailing

    def update(
        self,
        broker: Any,
        tick: Tick,
        specification: SymbolSpecification,
        *,
        magic_number: int | None = None,
        symbol: str | None = None,
    ) -> PositionUpdate:
        """Evaluate every open position once and send at most one request each.

        A position the broker no longer reports is reported as
        :data:`ActionKind.CLOSED` -- the venue closed it at its stop or target, and
        detecting that is how Phase 7 knows to place a replacement order.
        """
        positions = self._read_positions(broker, magic_number, symbol)
        return PositionUpdate(
            tuple(
                self._apply(broker, position, tick, specification) for position in positions
            )
        )

    def evaluate(
        self, position: PositionRecord, tick: Tick, specification: SymbolSpecification
    ) -> PositionAction:
        """Decide what should happen to one position, without sending anything.

        The whole decision path, separated from the I/O so it can be tested directly and so
        a dry run can report what it *would* have done.
        """
        decision = self._decide(position, tick, specification)
        return PositionAction(
            kind=decision.kind,
            position_ticket=position.ticket,
            proposal=decision.proposal,
            code=decision.code,
            reason=decision.reason,
            sent=False,
            next_state=decision.next_state,
        )

    # --- internals -------------------------------------------------------

    def _decide(
        self, position: PositionRecord, tick: Tick, specification: SymbolSpecification
    ) -> PositionAction:
        break_even = self._break_even.evaluate(position, tick, specification)
        if break_even.proposed:
            return PositionAction(
                kind=ActionKind.BREAK_EVEN_ARMED,
                position_ticket=position.ticket,
                proposal=break_even.proposal,
                code=break_even.code,
                reason=break_even.reason,
                next_state=PositionState.POSITION_BREAK_EVEN_ARMED,
            )
        if break_even.armed:
            # Break-even has taken this position; trailing refines it from here.
            trailing = self._trailing.evaluate(position, tick, specification)
            if trailing.proposed:
                return PositionAction(
                    kind=ActionKind.TRAILED,
                    position_ticket=position.ticket,
                    proposal=trailing.proposal,
                    code=trailing.code,
                    reason=trailing.reason,
                    next_state=PositionState.POSITION_TRAILING,
                )
            return PositionAction(
                kind=ActionKind.HOLD,
                position_ticket=position.ticket,
                code=trailing.code,
                reason=trailing.reason,
                next_state=PositionState.POSITION_BREAK_EVEN_ARMED,
            )
        # Break-even is disabled or not yet armed. Trailing may still be configured.
        trailing = self._trailing.evaluate(position, tick, specification)
        if trailing.proposed:
            return PositionAction(
                kind=ActionKind.TRAILED,
                position_ticket=position.ticket,
                proposal=trailing.proposal,
                code=trailing.code,
                reason=trailing.reason,
                next_state=PositionState.POSITION_TRAILING,
            )
        # Both rules declined. Report whichever one *got as far as having an opinion* -- a
        # disabled or not-yet-armed rule has no opinion, so naming it here would mask the
        # reason trailing actually refused, which is the one an operator needs.
        code = trailing.code if trailing.armed or trailing.proposed else break_even.code
        return PositionAction(
            kind=ActionKind.HOLD,
            position_ticket=position.ticket,
            code=code,
            reason=trailing.reason if code == trailing.code else break_even.reason,
        )

    def _apply(
        self,
        broker: Any,
        position: PositionRecord,
        tick: Tick,
        specification: SymbolSpecification,
    ) -> PositionAction:
        decision = self._decide(position, tick, specification)
        if decision.proposal is None:
            return decision
        # Exactly one call. No loop, no catch-and-retry: an ExecutionUnknownError propagates
        # so the caller re-reads broker state, and a rejection propagates so the reason
        # reaches the journal instead of becoming a silent no-op.
        broker.modify_position(
            position.ticket, stop_loss=decision.proposal.proposed_stop
        )
        return PositionAction(
            kind=decision.kind,
            position_ticket=decision.position_ticket,
            proposal=decision.proposal,
            code=decision.code,
            reason=decision.reason,
            sent=True,
            next_state=decision.next_state,
        )

    def _read_positions(
        self, broker: OrderBookReader, magic_number: int | None, symbol: str | None
    ) -> list[PositionRecord]:
        """Positions, read safely.

        A read is idempotent, so a transient terminal failure is retried with bounded
        backoff -- unlike every write in this module.
        """

        def read() -> list[PositionRecord]:
            return list(broker.positions(magic_number=magic_number, symbol=symbol))

        if self._retry is None:
            return read()
        return retry_read(read, self._retry, context="reading positions").value

    def event_for(self, action: PositionAction) -> BreakEventKind:
        """The journal event kind matching an action.

        Kept next to the decision so the journal and the decision cannot drift apart --
        an action recorded under the wrong event kind is a trade history that lies.
        """
        return {
            ActionKind.BREAK_EVEN_ARMED: BreakEventKind.BREAK_EVENT_BREAK_EVEN,
            ActionKind.TRAILED: BreakEventKind.BREAK_EVENT_TRAILING,
            ActionKind.CLOSED: BreakEventKind.BREAK_EVENT_BROKER_ADJUSTED,
            ActionKind.HOLD: BreakEventKind.BREAK_EVENT_INITIAL,
        }.get(action.kind, BreakEventKind.BREAK_EVENT_INITIAL)

    def describe(
        self, position: PositionRecord, tick: Tick, specification: SymbolSpecification
    ) -> str:
        """A one-line summary of where each rule currently points. For ``status``."""
        break_even = self._break_even.evaluate(position, tick, specification)
        trailing = self._trailing.evaluate(position, tick, specification)
        return (
            f"ticket={position.ticket} {position.side} "
            f"stop={position.stop_loss} "
            f"be={'armed' if break_even.armed else 'not armed'} "
            f"be_target={break_even.target} "
            f"trailing_level={trailing.trailing_level}"
        )


def monotonic_ok(old: Price | None, new: Price | None, side: Side) -> bool:
    """Whether moving a stop from ``old`` to ``new`` respects the direction's rule.

    Exposed because the property is the specification's, not this module's, and a caller
    that assembles its own proposal needs the same check. ``None`` means "no stop to
    compare", which is always acceptable.
    """
    if old is None or new is None:
        return True
    if side is Side.SIDE_BUY:
        return new >= old
    return new <= old
