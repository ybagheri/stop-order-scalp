"""Reconciling local state against the broker after a restart.

The broker is authoritative. Not as a preference between two sources, but because local state
can be missing, truncated or simply never written, while the venue's book is the thing that
actually determines whether this account holds exposure. A local file that says "flat" while
the broker says otherwise is not a tie to be broken; it is a position nobody is managing.

What this module does
---------------------

Reads the broker's orders and positions for this magic number, compares them against the
ledger, and produces a :class:`Reconciliation` describing what the system's state should be.
It **never** places, cancels or modifies anything. Every decision it reaches is a statement
about the world, and acting on it is the lifecycle's job -- which keeps the one place that can
move money auditable.

The two cases that matter
------------------------

**An unresolved intent.** The ledger holds an intent with no outcome: written before a send,
and the process died before recording what happened. The order may be on the book, may never
have been sent, or may have filled and closed while the process was down. The reconciler asks
the broker by client tag and settles the entry with what it finds. **An unresolved intent is
never treated as "not sent"** -- that assumption is what produces a duplicate order.

**A contradiction.** The ledger says placed at ticket 555 but the broker has no such order, and
no position either. Something happened that the ledger does not explain. That is a
:class:`StaleStateError`: the system stops and asks for an operator, rather than guessing.
Guessing here means either abandoning a real position or re-entering one that already exists.

Why it does not place a replacement
-----------------------------------

Because "the position closed" and "the system never noticed" are the same observable fact, and
only the *newest closed candle* can say whether a fresh trade is still warranted. A
reconciler that re-entered on the strength of a position having closed would re-enter using
stale signal data. The replacement goes through
:meth:`stop_order_scalp.lifecycle.trade_lifecycle.TradeLifecycle.on_position_closed`, which
re-evaluates the strategy from closed candles like any other decision.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from stop_order_scalp.domain.enums import LifecycleState
from stop_order_scalp.domain.exceptions import StaleStateError
from stop_order_scalp.domain.models import OrderRecord, PositionRecord
from stop_order_scalp.infrastructure.persistence import IntentOutcome, LedgerEntry, StateLedger

__all__ = [
    "Reconciler",
    "Reconciliation",
    "ReconciliationCode",
    "settled_intents",
]


class ReconciliationCode:
    """Stable outcomes of a reconciliation.

    An interface, like ``risk/risk_manager.RejectionCode``: these reach operator alerts, and
    renaming one silently changes what a saved query matches.
    """

    CLEAN: str = "clean"
    #: An intent with no outcome was resolved by asking the broker.
    RESOLVED_UNRESOLVED_INTENT: str = "resolved_unresolved_intent"
    #: Local state claims an order that the broker does not have.
    ORDER_MISSING_AT_BROKER: str = "order_missing_at_broker"
    #: The broker has an order this system does not recognise at all.
    UNKNOWN_ORDER_AT_BROKER: str = "unknown_order_at_broker"
    #: The broker has a position this system does not recognise.
    UNKNOWN_POSITION_AT_BROKER: str = "unknown_position_at_broker"
    #: A ledger entry could not be matched to anything and needs an operator.
    CONTRADICTION: str = "contradiction"


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What the broker says, and what should happen about it.

    A value rather than an exception in the ordinary cases: a restart that finds a position is
    normal, not a failure. Only a genuine contradiction raises.
    """

    #: The lifecycle state the system should adopt.
    resolved_state: LifecycleState
    #: Everything the broker holds for this magic number.
    positions: tuple[PositionRecord, ...] = ()
    orders: tuple[OrderRecord, ...] = ()
    #: Intents whose outcome was unknown and has now been established.
    settled: tuple[LedgerEntry, ...] = ()
    #: What happened, for the journal.
    code: str = ReconciliationCode.CLEAN
    notes: tuple[str, ...] = ()
    #: Contradictions an operator must resolve. Non-empty means :class:`StaleStateError` was
    #: raised, so this is populated only on the exception path.
    contradictions: tuple[str, ...] = ()

    @property
    def has_position(self) -> bool:
        return bool(self.positions)

    @property
    def has_order(self) -> bool:
        return bool(self.orders)

    @property
    def is_clean(self) -> bool:
        return not self.contradictions and self.code == ReconciliationCode.CLEAN

    @property
    def quiet(self) -> bool:
        """Whether the system has nothing to do: no position, no working order, nothing pending.

        This is the state in which a replacement order may be considered, and it is
        deliberately a *property of the broker's book* rather than of local bookkeeping.
        """
        return not self.positions and not self.orders

    def as_dict(self) -> dict[str, Any]:
        return {
            "resolved_state": str(self.resolved_state),
            "positions": len(self.positions),
            "orders": len(self.orders),
            "settled": len(self.settled),
            "code": self.code,
            "notes": list(self.notes),
            "contradictions": list(self.contradictions),
            "quiet": self.quiet,
        }

    def __str__(self) -> str:
        return (
            f"Reconciliation({self.resolved_state}, {self.code}, "
            f"{len(self.positions)} positions, {len(self.orders)} orders)"
        )


class Reconciler:
    """Compares the broker's book against the ledger.

    Read-only by construction: it is handed a ``Broker`` and calls only ``positions`` and
    ``orders``. That is what makes it safe to run at startup, before any gate is open -- it
    cannot trade even by mistake.
    """

    __slots__ = ("_magic", "_symbol")

    def __init__(self, *, magic_number: int, symbol: str) -> None:
        self._magic = magic_number
        self._symbol = symbol

    def reconcile(
        self,
        broker: Any,
        ledger: StateLedger,
        *,
        previous_state: LifecycleState = LifecycleState.STATE_IDLE,
    ) -> Reconciliation:
        """Establish what is true, and settle what the ledger left open.

        :raises StaleStateError: when the ledger and the broker contradict each other in a way
            that cannot be resolved by observation -- a ledger entry claiming a ticket the
            broker has never heard of. The system stops rather than guessing, because guessing
            either abandons a real position or creates a duplicate.
        """
        positions = tuple(
            sorted(
                broker.positions(magic_number=self._magic, symbol=self._symbol),
                key=lambda record: record.ticket,
            )
        )
        orders = tuple(
            sorted(
                broker.orders(magic_number=self._magic, symbol=self._symbol),
                key=lambda record: record.ticket,
            )
        )
        settled = settled_intents(ledger, orders)
        by_tag = {order.client_tag: order for order in orders}

        notes: list[str] = []
        contradictions: list[str] = []
        known_tags = {entry.client_tag for entry in ledger.entries()}

        # A ledger entry that says "placed", with neither a matching working order nor a
        # matching position, is the *ordinary* filled-then-closed case: the trade completed
        # while the process was down. There is no exposure, so it is a note. An earlier version
        # of this code treated it as a contradiction, which would have halted the system on
        # every take-profit after every restart.
        for entry in ledger.entries():
            if not entry.resolved or entry.outcome != IntentOutcome.PLACED:
                continue
            if entry.client_tag in by_tag:
                continue
            if any(position.client_tag == entry.client_tag for position in positions):
                continue
            notes.append(
                f"intent {entry.client_tag} was recorded at ticket {entry.ticket}, which is "
                "on neither the working-order book nor the position list; treating it as "
                "filled and since closed"
            )

        # The dangerous case is exposure that cannot be *attributed*. A working order or a
        # position carrying this magic number, whose identity matches no ledger entry, is
        # something this system placed and forgot, or something it did not place at all.
        # Either way it may become, or already is, exposure that nobody is managing -- and a
        # working order in particular can still fill. Guessing here is how a real position
        # ends up unattended, so it stops for an operator instead.
        for held in orders:
            if held.client_tag not in known_tags:
                contradictions.append(
                    f"the broker holds working order {held.ticket} on {held.symbol} with "
                    f"identity {held.client_tag or '(unreadable)'!r}, which matches no entry in "
                    "the ledger. It may have been placed by a previous build, by hand in the "
                    "terminal, or by another tool sharing this magic number -- and it can still "
                    "fill. Refusing to decide."
                )

        for held in positions:
            if held.client_tag not in known_tags:
                contradictions.append(
                    f"the broker holds position {held.ticket} on {held.symbol} "
                    f"{held.volume.lots} with identity {held.client_tag or '(unreadable)'!r}, "
                    "which matches no entry in the ledger. This is unmanaged exposure. "
                    "Refusing to decide."
                )

        if contradictions:
            raise StaleStateError(
                "local state and broker state cannot be reconciled without an operator:\n  - "
                + "\n  - ".join(contradictions)
            )

        code = ReconciliationCode.CLEAN
        if settled:
            code = ReconciliationCode.RESOLVED_UNRESOLVED_INTENT

        return Reconciliation(
            resolved_state=self._resolve_state(positions, orders, previous_state),
            positions=positions,
            orders=orders,
            settled=settled,
            code=code,
            notes=tuple(notes),
        )

    def _resolve_state(
        self,
        positions: Sequence[PositionRecord],
        orders: Sequence[OrderRecord],
        previous_state: LifecycleState,
    ) -> LifecycleState:
        """What the system should believe, given what the broker holds.

        Broker state wins outright. A local belief that a position is open when the broker has
        none is discarded; a broker position the local state never knew about is adopted. Both
        directions are the same rule, which is why it is worth stating as one rule rather than
        two cases.
        """
        if positions:
            if previous_state in (
                LifecycleState.STATE_BREAK_EVEN_ARMED,
                LifecycleState.STATE_TRAILING,
            ):
                return previous_state
            return LifecycleState.STATE_POSITION_OPEN
        if orders:
            if previous_state in (
                LifecycleState.STATE_PENDING_ORDER_PLACED,
                LifecycleState.STATE_WAITING_FOR_TRIGGER,
            ):
                return previous_state
            return LifecycleState.STATE_WAITING_FOR_TRIGGER
        if previous_state in (
            LifecycleState.STATE_POSITION_CLOSED,
            LifecycleState.STATE_WAITING_FOR_SIGNAL,
        ):
            # Already owing or already owing again; nothing to decide beyond that. Note that
            # ``STATE_IDLE`` is deliberately *not* inherited: idle means "not started", and a
            # process that has just recovered with a flat book has started and owes a decision.
            return previous_state
        # Nothing at the broker and nothing worth inheriting: the system owes a decision, and
        # it will take one from a closed candle on the next cycle.
        return LifecycleState.STATE_WAITING_FOR_SIGNAL


def settled_intents(
    ledger: StateLedger, orders: Sequence[OrderRecord]
) -> tuple[LedgerEntry, ...]:
    """Resolve every intent whose outcome the broker still has to answer.

    The matching is by ``client_tag``, which is the only identity that survives a restart --
    a ticket is not known before the send, and a position's ticket is not the order's.

    Both never-recorded and recorded-``unknown`` entries are considered. The second group is
    the point: an ambiguous send is recorded as ``UNKNOWN``, which is an outcome but not a
    *known* one, and recovery is the only thing that can resolve it. Ignoring that group would
    leave ``VERIFYING`` with nothing to verify and the ambiguity permanent.

    An intent that matches nothing stays ``UNKNOWN``. That is the conservative direction:
    "not on the book" is not proof of "never sent", and the lifecycle re-reads on the next cycle
    rather than acting on the difference.
    """
    by_tag = {order.client_tag: order for order in orders if order.client_tag}
    resolved: list[LedgerEntry] = []
    for entry in ledger.awaiting_confirmation():
        order = by_tag.get(entry.client_tag)
        if order is None:
            if entry.unresolved:
                resolved.append(
                    ledger.mark_unknown(
                        entry.client_tag,
                        "recovery found no matching order on the book; the send may still "
                        "have reached the venue, so this is recorded as unknown rather than "
                        "rejected",
                    )
                )
            # An entry already marked unknown and still unmatched stays unknown. Re-marking
            # would fail the double-settle guard and would imply a fresh decision.
            continue
        if entry.unresolved:
            # Never recorded at all: this is the first outcome, so an ordinary settle.
            resolved.append(ledger.settle_from_record(entry.client_tag, order))
        else:
            # Already recorded unknown: now the broker has answered, so it is upgraded.
            resolved.append(ledger.confirm(entry.client_tag, order.ticket))
    return tuple(resolved)


def position_exposure(positions: Sequence[PositionRecord]) -> Decimal:
    """Total lots held, for reporting.

    Summed across positions rather than assumed to be at most one: the reconciler's job is to
    describe the broker's book faithfully, and a strategy that somehow holds two positions
    must not have that fact rounded away in the log.
    """
    return sum((position.volume.lots for position in positions), Decimal(0))


def summarise_for_operator(reconciliation: Reconciliation) -> str:
    """A one-line description for ``status`` after a restart."""
    if reconciliation.positions:
        held = ", ".join(
            f"{position.ticket} {position.side} {position.volume.lots}"
            f" @{position.entry}"
            + (
                f" sl={position.stop_loss}"
                if position.stop_loss is not None
                else " sl=none"
            )
            for position in reconciliation.positions
        )
        return f"holding {held}"
    if reconciliation.orders:
        pending = ", ".join(
            f"{order.ticket} {order.kind} {order.volume.lots} @{order.entry}"
            for order in reconciliation.orders
        )
        return f"working orders: {pending}"
    return "flat: no positions and no working orders"
