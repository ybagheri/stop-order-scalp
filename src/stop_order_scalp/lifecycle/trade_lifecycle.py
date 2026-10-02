"""The trading loop, as an explicit sequence of states.

One class holds the state machine, the durable ledger, and the collaborators that place and
manage orders. It does one thing per entry point: decide, act, and record.

The order of operations in :meth:`TradeLifecycle.place_order` is the whole design
------------------------------------------------------------------------

.. code-block:: python

    0. ask the gate                      # before anything is written
    1. record the intent in the ledger   # durable, before anything can be sent
    2. re-read the broker's book         # idempotency, immediately before the send
    3. send, exactly once
    4. settle the ledger entry

Step 1 before step 3 closes the window in which a *crash* creates a duplicate: an intent that
might become an order is durable before the send that might create it. Step 2 immediately
before step 3 closes the window created by a duplicate tick, a reconnect, or a previous process.

Step 0 comes before step 1 so that a gate refusal leaves **no trace in the ledger**. A refused
order that had been recorded would later be "resolved" by recovery as an intent that might
have been sent -- which is a phantom risk for a trade that never existed.

Step 4 handles the outcomes distinctly, and collapsing them is the bug this ordering prevents:

=============  ==============================================================
placed         settle with the broker's ticket
rejected       settle as rejected; return to waiting
unknown        settle as ``unknown``; move to ``VERIFYING``
=============  ==============================================================

``unknown`` is not a failure. The order may be on the venue, so the system does not re-enter;
it moves to ``VERIFYING``, whose only exits require broker state to have been read first. That
is why ``VERIFYING`` cannot reach ``VALIDATING`` in the transition table.

The replacement order
---------------------

The specification says to place a replacement immediately after a position closes. It goes
through the **same** path as any other entry -- full re-evaluation from closed candles, the
same gate, the same duplicate check -- never by copying the previous plan.

:meth:`TradeLifecycle.on_position_closed` journals the close and stops there. It does not
re-enter, because "the position closed" is not evidence that a new trade is warranted: only a
fresh decision from closed candles is. The next :meth:`tick` takes a decision while the
machine is in ``POSITION_CLOSED`` and places from it, which is the replacement path and the
first-entry path being literally the same code.

Close detection is a comparison, not an event
---------------------------------------------

The venue closes positions at stops and targets without telling this system. So
:meth:`_detect_closes` compares the positions it now sees against the ones it saw last, and a
disappearance is what a close *looks like*. It cannot distinguish a stop-out from a
take-profit and does not try; the recovery path settles what happened, not this comparison.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from stop_order_scalp.domain.enums import LifecycleState, PositionState, TradeEventKind
from stop_order_scalp.domain.exceptions import (
    BrokerNotConnectedError,
    ExecutionUnknownError,
)
from stop_order_scalp.domain.interfaces import Broker, Clock
from stop_order_scalp.domain.models import (
    OrderIntent,
    OrderRecord,
    PositionRecord,
    RiskAssessment,
    TradePlan,
)
from stop_order_scalp.infrastructure.persistence import IntentOutcome, LedgerEntry, StateLedger
from stop_order_scalp.lifecycle.recovery import Reconciler, Reconciliation
from stop_order_scalp.lifecycle.state_machine import (
    LifecycleMachine,
    TransitionListener,
)

__all__ = ["LifecycleStep", "TradeLifecycle"]


@dataclass(frozen=True, slots=True)
class LifecycleStep:
    """What one pass of the loop did.

    Returned rather than only journalled, so a caller -- a test, or ``run --dry-run`` -- can
    assert on what happened without scraping log output.
    """

    state: LifecycleState
    #: Stable machine-readable outcomes, one per decision taken this pass.
    actions: tuple[str, ...] = ()
    placed: tuple[OrderRecord, ...] = ()
    #: Everything the pass wrote to the journal, in order.
    events: tuple[TradeEventKind, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def idle(self) -> bool:
        """Whether the pass did nothing at all. The common case, and worth asserting on."""
        return not self.actions

    def __str__(self) -> str:
        return f"LifecycleStep({self.state}, {', '.join(self.actions) or 'no action'})"


class TradeLifecycle:
    """Drives one trade at a time, from a signal through to its replacement.

    Stateless with respect to price history: each entry point takes the decision it should act
    on as an argument. That keeps the class free of candle bookkeeping and makes every step
    independently testable, and it is why one instance serves the live loop, the backtester
    and a test without configuration differences between them.
    """

    __slots__ = (
        "_broker",
        "_clock",
        "_events",
        "_interlock",
        "_last_positions",
        "_ledger",
        "_machine",
        "_magic",
        "_notes",
        "_order_manager",
        "_position_manager",
        "_reconciler",
        "_specification",
        "_symbol",
        "_tick",
    )

    def __init__(
        self,
        broker: Broker,
        ledger: StateLedger,
        order_manager: Any,
        *,
        clock: Clock | None = None,
        position_manager: Any = None,
        magic_number: int | None = None,
        symbol: str | None = None,
        interlock: Any = None,
        listeners: Sequence[TransitionListener] | None = None,
    ) -> None:
        self._broker = broker
        self._ledger = ledger
        self._order_manager = order_manager
        self._position_manager = position_manager
        self._machine = LifecycleMachine(listeners=listeners)
        self._symbol = symbol or ""
        # Duck-typed on purpose. `lifecycle` must not import `execution` -- the architecture
        # gate enforces it -- so the interlock arrives as an argument with the shape this class
        # needs rather than as a type this layer would have to import. The gate itself is
        # reached the same way, through the injected order manager.
        self._interlock = interlock
        self._clock = clock
        self._magic = (
            magic_number
            if magic_number is not None
            else getattr(order_manager, "magic_number", 0)
        )
        self._reconciler = Reconciler(magic_number=self._magic, symbol=self._symbol)
        self._events: list[TradeEventKind] = []
        self._notes: list[str] = []
        self._last_positions: tuple[PositionRecord, ...] = ()
        #: Set by :meth:`attach_market`, which the application layer calls once per cycle.
        self._tick: Any = None
        self._specification: Any = None

    # --- accessors -------------------------------------------------------

    @property
    def state(self) -> LifecycleState:
        return self._machine.state

    @property
    def machine(self) -> LifecycleMachine:
        return self._machine

    @property
    def ledger(self) -> StateLedger:
        return self._ledger

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def interlock(self) -> Any:
        """The injected interlock, for ``status``. ``None`` when none was supplied."""
        return self._interlock

    def __repr__(self) -> str:
        return f"TradeLifecycle({self._machine.state}, symbol={self._symbol!r})"

    def attach_market(self, tick: Any, specification: Any) -> None:
        """Bind the current tick and the instrument specification.

        Explicit rather than constructor arguments because the specification is fetched from the
        broker after connecting, and the tick changes every cycle. Keeping both out of the
        constructor is what lets a test drive the lifecycle with two arguments and no feed.
        """
        self._tick = tick
        self._specification = specification

    def remember_positions(self, positions: Sequence[PositionRecord]) -> None:
        """Seed the close-detection baseline. Used after recovery, and by tests."""
        self._last_positions = tuple(positions)

    @property
    def last_positions(self) -> tuple[PositionRecord, ...]:
        return self._last_positions

    # --- startup ---------------------------------------------------------

    def recover(self) -> LifecycleStep:
        """Establish what is true before anything else.

        The broker is authoritative and the ledger is settled before the machine is placed
        anywhere. Called once at startup, before any gate is open -- which is safe because
        :class:`~stop_order_scalp.lifecycle.recovery.Reconciler` is read-only by construction.

        :raises StaleStateError: from the reconciler when local and remote contradict each
            other. Propagated deliberately: the system must not start on a guess.
        """
        self._begin()
        self._machine.reset(LifecycleState.STATE_RECONCILING)
        result = self._reconciler.reconcile(
            self._broker,
            self._ledger,
            previous_state=LifecycleState.STATE_IDLE,
        )
        self._machine.reset(result.resolved_state)
        self._last_positions = result.positions
        self._events.append(TradeEventKind.TRADE_EVENT_RECONCILED)
        self._notes.append(_describe(result))
        for note in result.notes:
            self._notes.append(note)
        return self._step((result.code,))

    # --- the loop --------------------------------------------------------

    def tick(self, decision: Any | None = None) -> LifecycleStep:
        """One cycle: managed exits, then close detection, then a new entry if offered.

        The order is deliberate. Managed exits first, because a stop that has moved is
        protection that should be in place before any new risk is taken. Close detection
        second, because a position that closed on the previous tick must be noticed before
        anything else places an order on a book that may no longer reflect it.
        """
        self._begin()
        actions: list[str] = []

        if self._position_manager is not None and self._machine.state.is_trading_state:
            actions.extend(self._manage_positions())

        closed = self._detect_closes()
        if closed:
            actions.extend(self._on_position_closed(closed))

        if decision is not None and self._machine.quiet:
            actions.extend(self._enter(decision))

        return self._step(tuple(actions))

    def _enter(self, decision: Any) -> list[str]:
        """Act on a strategy decision, if one is a plan and the system is at rest.

        This is also the replacement path. After a close the machine sits in
        ``POSITION_CLOSED``, which is quiet, so a decision offered on the next tick is placed
        by exactly this code -- the first entry and the replacement are one path, not two that
        have to agree.
        """
        if self._machine.can(LifecycleState.STATE_SIGNAL_DETECTED):
            self._machine.move(LifecycleState.STATE_SIGNAL_DETECTED, reason="decision offered")
        if not isinstance(decision, TradePlan):
            # A `NoTrade`, or anything that is not a plan. The strategy declining is the
            # expected outcome most of the time and must not be an error.
            self._notes.append("no trade")
            if self._machine.can(LifecycleState.STATE_WAITING_FOR_SIGNAL):
                self._machine.move(LifecycleState.STATE_WAITING_FOR_SIGNAL, reason="no trade")
            return ["no_trade"]
        return [self.place_order(decision).actions[0] or "entering"]

    # --- placement -------------------------------------------------------

    def _now(self) -> datetime:
        """The current time, from the injected clock.

        No fallback to ``datetime.now()``: the architecture gate refuses a wall-clock read in
        this layer, and rightly so -- a ledger timestamp that came from somewhere other than
        the injected clock would make a replay irreproducible. A caller that supplies no clock
        gets an explicit error rather than a hidden dependency.
        """
        if self._clock is None:
            raise BrokerNotConnectedError(
                "TradeLifecycle needs a Clock. Injecting one is what keeps ledger timestamps "
                "reproducible; a wall-clock fallback here would hide that."
            )
        return self._clock.now()

    def place_order(
        self,
        plan: TradePlan,
        *,
        settings: Any = None,
        assessment: RiskAssessment | None = None,
        now: datetime | None = None,
    ) -> LifecycleStep:
        """Place a plan, recording the intent first and sending at most once.

        See the module docstring for why the five steps are in this order.
        """
        self._begin()
        moment = now or self._now()

        # 0. The gate, before anything is written. A refusal must leave no ledger trace, or
        #    recovery would later "resolve" an intent for a trade that never existed.
        if settings is not None:
            gate: Any = self._order_manager.gate.check(settings)
            if gate.refused:
                self._events.append(TradeEventKind.TRADE_EVENT_ORDER_REJECTED)
                self._notes.append(f"gate refused: {gate.code}")
                return self._step(("gate_refused",))

        if assessment is not None and not assessment.accepted:
            # A caller that already sized the trade gets the verdict respected. Refusing here
            # rather than re-assessing: re-running the risk engine would silently ignore the
            # assessment it was handed, and two risk answers for one decision is one too many.
            self._events.append(TradeEventKind.TRADE_EVENT_ORDER_REJECTED)
            self._notes.append(f"risk refused: {assessment.code}: {assessment.reason}")
            return self._step(("risk_refused",))

        if not self._machine.quiet and not self._machine.state.is_terminal:
            # The strongest guard, and the one that fires most often: the system already holds
            # exposure or a resting order, so nothing may be sent at all. It deliberately comes
            # *before* the identity checks, so a duplicate is reported as "busy" rather than as
            # a duplicate while a position is open -- the refusal is the same and the reason is
            # more accurate. The identity checks below then catch the remaining case: a resting
            # order that this system no longer has a record of.
            self._notes.append(
                f"refusing to place from {self._machine.state}: the system already holds "
                "exposure or a resting order"
            )
            return self._step(("busy",))
        if self._machine.state.is_terminal:
            self._notes.append("refusing to place: the system is halted")
            return self._step(("halted",))

        if self._machine.state is LifecycleState.STATE_IDLE:
            # Walk the ordinary route rather than jumping. Resetting to VALIDATING would take
            # a state the table does not permit from idle, and this is the one place that
            # would have done it -- which is exactly why it is done explicitly here.
            self._machine.move(
                LifecycleState.STATE_WAITING_FOR_SIGNAL, reason="first entry"
            )
        if self._machine.can(LifecycleState.STATE_SIGNAL_DETECTED):
            self._machine.move(LifecycleState.STATE_SIGNAL_DETECTED, reason="placing")
        if self._machine.can(LifecycleState.STATE_VALIDATING):
            self._machine.move(LifecycleState.STATE_VALIDATING, reason="assessing")

        # 1. Record the intent, durably, before anything can be sent.
        #
        #    The ledger is consulted for a *pre-existing* record of this identity first. That
        #    is the cross-restart half of duplicate protection: an intent written by a
        #    previous process whose outcome was never recorded must stop this one sending.
        #    It has to happen before the record is written, or the check finds the entry this
        #    call just created and refuses every placement as a duplicate of itself.
        intent = self._order_manager.intent_for(plan, now=moment)
        if self._ledger.get(intent.client_tag) is not None:
            existing = self._ledger.get(intent.client_tag)
            self._notes.append(
                f"the ledger already holds identity {intent.client_tag} "
                f"(outcome {None if existing is None else existing.outcome!r}); "
                "refusing to record or send it a second time"
            )
            return self._step(("already_recorded",))
        self._ledger.record(
            LedgerEntry(
                client_tag=intent.client_tag,
                intent=intent,
                recorded_at=moment,
                state=self._machine.state,
            )
        )

        # 2. Re-read the book immediately before the send: the this-instant half.
        if self._is_duplicate(intent):
            self._ledger.settle(
                intent.client_tag,
                IntentOutcome.CANCELLED,
                reason="duplicate: an order with this identity already exists",
            )
            self._events.append(TradeEventKind.TRADE_EVENT_REJECTED_DUPLICATE)
            self._notes.append(f"duplicate suppressed for {intent.client_tag}")
            return self._step(("duplicate",))

        # 3. Send. Exactly once, whatever the outcome.
        try:
            record = self._broker.place_order(intent)
        except ExecutionUnknownError as exc:
            self._ledger.mark_unknown(intent.client_tag, str(exc))
            self._machine.move(
                LifecycleState.STATE_VERIFYING,
                reason="unknown send outcome",
                detail={"tag": intent.client_tag},
            )
            self._events.append(TradeEventKind.TRADE_EVENT_ORDER_REJECTED)
            self._notes.append(f"unknown outcome, not resent: {exc}")
            return self._step(("execution_unknown",))

        # 4. Settle. The order is on the book and the durable record now says so.
        self._ledger.settle_from_record(intent.client_tag, record)
        self._events.append(TradeEventKind.TRADE_EVENT_ORDER_PLACED)
        self._machine.move(
            LifecycleState.STATE_PENDING_ORDER_PLACED,
            reason="order placed",
            detail={"ticket": record.ticket, "tag": intent.client_tag},
        )
        self._machine.move(LifecycleState.STATE_WAITING_FOR_TRIGGER, reason="resting on the book")
        self._notes.append(f"placed {record.kind} ticket {record.ticket}")
        step = self._step(("placed",))
        return LifecycleStep(
            state=step.state,
            actions=step.actions,
            placed=(record,),
            events=step.events,
            notes=step.notes,
        )

    def cancel_pending(self, ticket: int) -> LifecycleStep:
        """Cancel a working order.

        An ambiguous outcome is **not** retried. The goal is that the order is not on the
        book, and that is a question the next reconciliation answers -- asking again would be
        acting on a guess about whether the first attempt landed.
        """
        self._begin()
        try:
            gone = self._broker.cancel_order(ticket)
        except ExecutionUnknownError:
            self._notes.append(
                f"cancel outcome unknown for ticket {ticket}; will re-observe, not resend"
            )
            return self._step(("cancel_unknown",))
        if gone:
            self._events.append(TradeEventKind.TRADE_EVENT_ORDER_CANCELLED)
            self._notes.append(f"cancelled ticket {ticket}")
        return self._step(("cancelled" if gone else "cancel_refused",))

    # --- internals -------------------------------------------------------

    def _begin(self) -> None:
        """Clear the per-pass journals.

        Deliberately a separate call: the state and the ledger persist across passes, and only
        the notes and events are per-pass. Making that explicit keeps ``LifecycleStep`` from
        accidentally reporting a previous pass's actions.
        """
        self._events = []
        self._notes = []

    def _step(self, actions: tuple[str, ...]) -> LifecycleStep:
        return LifecycleStep(
            state=self._machine.state,
            actions=actions,
            events=tuple(self._events),
            notes=tuple(self._notes),
        )

    def _is_duplicate(self, intent: OrderIntent) -> bool:
        """Whether a working order with this identity is already on the book.

        Only the **book** is consulted here, not the ledger. The ledger's own memory of this
        identity is checked *before* the record is written, as the ``already_recorded`` branch
        above; asking again afterwards would always find the entry this call had just written
        and refuse every placement as a duplicate of itself.

        The two checks cover different failures and neither subsumes the other: the ledger
        remembers an order placed by a previous process whose outcome was never recorded, and
        the book sees this instant.
        """
        has_tag = getattr(self._order_manager, "has_tag", None)
        if has_tag is None:
            return False
        return bool(has_tag(self._broker, intent.client_tag))

    def _manage_positions(self) -> list[str]:
        """Apply break-even and trailing, and move the machine to match.

        Delegates wholesale. The ordering and idempotency guarantees are already proved in
        :mod:`stop_order_scalp.execution.position_manager` and
        :mod:`stop_order_scalp.trailing`; reimplementing any of it here would be a second
        copy to keep in step.
        """
        if self._tick is None or self._specification is None:
            self._notes.append("no market bound; managed exits skipped")
            return []
        update = self._position_manager.update(
            self._broker,
            self._tick,
            self._specification,
            magic_number=self._reconciler._magic,
            symbol=self._symbol or None,
        )
        actions: list[str] = []
        for action in update:
            if not action.changed:
                continue
            actions.append(action.kind)
            self._events.append(TradeEventKind.TRADE_EVENT_SL_MODIFIED)
            target = _lifecycle_state_for(action.next_state)
            if target is not None and self._machine.can(target):
                self._machine.move(target, reason=action.kind)
        return actions

    def _detect_closes(self) -> tuple[PositionRecord, ...]:
        """Positions that were present and are not now.

        A comparison rather than an event, because the venue closes at stops and targets without
        telling this system. The answer is "something closed", never "a take-profit filled".
        """
        if not self._last_positions:
            return ()
        present = self._broker.positions(
            magic_number=self._reconciler._magic, symbol=self._symbol or None
        )
        tickets = {position.ticket for position in present}
        gone = tuple(
            position for position in self._last_positions if position.ticket not in tickets
        )
        self._last_positions = tuple(
            position for position in present if position.ticket in tickets
        )
        return gone

    def _on_position_closed(self, closed: Sequence[PositionRecord]) -> list[str]:
        """Journal the close and hand control back for a fresh decision.

        **No replacement is placed here.** A close says nothing about whether a new trade is
        warranted; only a decision from closed candles does. So this records what happened and
        moves to ``POSITION_CLOSED``, which is a quiet state -- the next :meth:`tick` may then
        be offered a decision and :meth:`_enter` places it through the ordinary path.
        """
        for _ in closed:
            self._events.append(TradeEventKind.TRADE_EVENT_POSITION_CLOSED)
        self._notes.append(
            f"{len(closed)} position(s) closed; awaiting a fresh decision for a replacement"
        )
        if self._machine.can(LifecycleState.STATE_POSITION_CLOSED):
            self._machine.move(
                LifecycleState.STATE_POSITION_CLOSED, reason="venue closed a position"
            )
        return [f"closed:{position.ticket}" for position in closed]

    def _adopt_open_position(self, reason: str) -> None:
        """Move to ``POSITION_OPEN`` by whichever legal route exists from here.

        The book says a position is open, and the book is authoritative -- so the machine must
        reach the state that reflects it. From a resting state there is no direct edge, because
        ``POSITION_OPEN`` is only reachable through a placement or a trigger. The route taken
        is the real one: a resting order was placed and then filled.

        Walking real edges rather than resetting is the point. ``reset`` would paper over a
        genuinely unreachable state, and this is the situation where that would matter: the
        machine must always be able to describe the exposure it actually holds.
        """
        if self._machine.state is LifecycleState.STATE_POSITION_OPEN:
            return
        route = [
            LifecycleState.STATE_WAITING_FOR_SIGNAL,
            LifecycleState.STATE_SIGNAL_DETECTED,
            LifecycleState.STATE_VALIDATING,
            LifecycleState.STATE_PENDING_ORDER_PLACED,
            LifecycleState.STATE_WAITING_FOR_TRIGGER,
            LifecycleState.STATE_POSITION_OPEN,
        ]
        for target in route:
            if self._machine.can(target):
                self._machine.move(target, reason=reason)
                if target is LifecycleState.STATE_POSITION_OPEN:
                    return
        self._notes.append(
            f"the broker holds a position but {self._machine.state} does not permit "
            "acting; an operator must clear this"
        )

    def observe_fills(self) -> LifecycleStep:
        """Adopt positions that appeared, moving the machine to match.

        The other half of close detection: a pending order can fill without this system being
        told, so the book has to be re-read and the machine advanced.
        """
        self._begin()
        present = tuple(
            self._broker.positions(
                magic_number=self._reconciler._magic, symbol=self._symbol or None
            )
        )
        appeared = [
            position
            for position in present
            if position.ticket not in {known.ticket for known in self._last_positions}
        ]
        if not appeared:
            return self._step(())
        self._last_positions = present
        for _ in appeared:
            self._events.append(TradeEventKind.TRADE_EVENT_POSITION_OPENED)
        self._notes.append(f"{len(appeared)} position(s) opened")
        self._adopt_open_position("order filled")
        return self._step(tuple(f"opened:{p.ticket}" for p in appeared))


#: ``PositionState`` -> ``LifecycleState``.
#:
#: The two enums are deliberately different vocabularies -- one describes a *trade*, the other
#: what the *system* is doing -- and passing a value from one to the other is a bug that only
#: shows up at runtime, because both are ``StrEnum`` and the machine stores either without
#: complaint. The first version of :meth:`TradeLifecycle._manage_positions` did exactly that
#: and put the lifecycle into a ``PositionState``, where the next ``quiet`` check raised
#: ``AttributeError`` three cycles into a dry run. An explicit table is the fix; the
#: alternative is one vocabulary pretending to be both.
_POSITION_TO_LIFECYCLE: dict[PositionState, LifecycleState] = {
    PositionState.POSITION_OPEN: LifecycleState.STATE_POSITION_OPEN,
    PositionState.POSITION_BREAK_EVEN_ARMED: LifecycleState.STATE_BREAK_EVEN_ARMED,
    PositionState.POSITION_TRAILING: LifecycleState.STATE_TRAILING,
    PositionState.POSITION_CLOSED: LifecycleState.STATE_POSITION_CLOSED,
}


def _lifecycle_state_for(next_state: PositionState) -> LifecycleState | None:
    """Translate a position's next state into the system's, or ``None`` if it has no bearing.

    ``None`` for the rest of the vocabulary on purpose: a state such as
    ``POSITION_CANCELLED`` says something about a trade, not about what the system should do
    next, and guessing would move the machine on the strength of a word.
    """
    return _POSITION_TO_LIFECYCLE.get(next_state)


def _describe(result: Reconciliation) -> str:
    if result.positions:
        return f"holding {len(result.positions)} position(s)"
    if result.orders:
        return f"working orders: {len(result.orders)}"
    return "flat: no positions and no working orders"
