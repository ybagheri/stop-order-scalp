"""Recovery: the broker is authoritative, and contradictions stop the system.

The headline property is the crash case. An intent written before a send whose outcome was
never recorded may or may not be on the venue, and treating that as "not sent" is precisely
how a restart becomes a duplicate order. Every test below is arranged around that question.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from stop_order_scalp.domain.enums import LifecycleState, OrderKind, Side
from stop_order_scalp.domain.exceptions import StaleStateError
from stop_order_scalp.domain.models import OrderIntent, OrderRecord, PositionRecord
from stop_order_scalp.domain.value_objects import Money, Price, Volume
from stop_order_scalp.infrastructure.persistence import (
    IntentOutcome,
    LedgerEntry,
    StateLedger,
)
from stop_order_scalp.lifecycle.recovery import (
    Reconciler,
    Reconciliation,
    ReconciliationCode,
    settled_intents,
)

NOW = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
MAGIC = 20260930
TAG = "abc123def456abc123def4"


def intent(tag: str = TAG) -> OrderIntent:
    return OrderIntent(
        plan_id="US30-M15-1-M1-1",
        client_tag=tag,
        symbol="US30",
        kind=OrderKind.ORDER_KIND_BUY_STOP,
        volume=Volume.of(Decimal("0.4")),
        entry=Price.parse("40000.0", 1),
        stop_loss=Price.parse("39990.0", 1),
        take_profit=Price.parse("40020.0", 1),
        magic_number=MAGIC,
        comment="",
        deviation_points=20,
    )


def order(ticket: int = 555, tag: str = TAG, active: bool = True) -> OrderRecord:
    return OrderRecord(
        ticket=ticket,
        client_tag=tag,
        symbol="US30",
        kind=OrderKind.ORDER_KIND_BUY_STOP,
        volume=Volume.of(Decimal("0.4")),
        entry=Price.parse("40000.0", 1),
        stop_loss=Price.parse("39990.0", 1),
        take_profit=Price.parse("40020.0", 1),
        magic_number=MAGIC,
        comment="",
        placed_at=NOW,
        state="PLACED" if active else "CANCELLED",
    )


def position(ticket: int = 900, tag: str = TAG) -> PositionRecord:
    return PositionRecord(
        ticket=ticket,
        symbol="US30",
        side=Side.SIDE_BUY,
        volume=Volume.of(Decimal("0.4")),
        entry=Price.parse("40000.0", 1),
        stop_loss=Price.parse("39995.0", 1),
        take_profit=Price.parse("40020.0", 1),
        magic_number=MAGIC,
        comment="",
        opened_at=NOW,
        profit=Money.of(100),
        client_tag=tag,
    )


class FakeBook:
    """A broker's book, as the reconciler sees it."""

    def __init__(
        self,
        *,
        orders: list[OrderRecord] | None = None,
        positions: list[PositionRecord] | None = None,
    ) -> None:
        self._orders = orders or []
        self._positions = positions or []

    def orders(self, **_: Any) -> list[OrderRecord]:
        return [record for record in self._orders if record.is_active]

    def positions(self, **_: Any) -> list[PositionRecord]:
        return list(self._positions)


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


@pytest.fixture
def reconciler() -> Reconciler:
    return Reconciler(magic_number=MAGIC, symbol="US30")


def _ledger_with(path: Path, tag: str = TAG, outcome: str = IntentOutcome.PLACED) -> StateLedger:
    """A ledger that already knows about ``tag``.

    Reconciliation needs exposure to be attributable, so tests that place a position or an
    order on the book must first say this system put it there.
    """
    ledger = StateLedger.load(path)
    ledger.record(LedgerEntry(client_tag=tag, intent=intent(tag), recorded_at=NOW))
    ledger.settle(tag, outcome, ticket=555)
    return ledger


class TestCleanStart:
    def test_a_flat_book_reconciles_cleanly(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        result = reconciler.reconcile(FakeBook(), StateLedger.load(ledger_path))

        assert result.is_clean
        assert result.quiet
        assert result.resolved_state is LifecycleState.STATE_WAITING_FOR_SIGNAL

    def test_the_reconciler_sends_nothing(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        """Read-only by construction: it may run before any gate is open.

        The fake book has no write methods at all, so calling one would be an ``AttributeError``
        rather than a test failure -- which is the strongest available statement that the
        reconciler cannot trade.
        """
        book = FakeBook()
        assert not hasattr(book, "place_order")
        assert not hasattr(book, "cancel_order")
        assert not hasattr(book, "modify_position")

        result = reconciler.reconcile(book, StateLedger.load(ledger_path))

        assert result.settled == ()


class TestBrokerIsAuthoritative:
    """Where the broker and local state disagree, the broker decides.

    Every case here seeds a ledger entry for the tag, because exposure that matches no ledger
    entry is unattributable and correctly stops the system instead (see
    :class:`TestContradictions`). Adopting a position this system cannot name is not the same
    as reconciling one it can.
    """

    def test_a_position_the_system_had_forgotten_is_adopted(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        """Local bookkeeping said flat; the broker says otherwise. The broker wins."""
        ledger = _ledger_with(ledger_path)

        result = reconciler.reconcile(FakeBook(positions=[position()]), ledger)

        assert result.has_position
        assert result.resolved_state is LifecycleState.STATE_POSITION_OPEN

    def test_a_working_order_is_adopted_as_waiting_for_trigger(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        ledger = _ledger_with(ledger_path)

        result = reconciler.reconcile(FakeBook(orders=[order()]), ledger)

        assert result.has_order
        assert result.resolved_state is LifecycleState.STATE_WAITING_FOR_TRIGGER

    def test_a_managed_position_keeps_its_state(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        """Trailing is not something recovery should undo by going back to "open"."""
        result = reconciler.reconcile(
            FakeBook(positions=[position()]),
            _ledger_with(ledger_path),
            previous_state=LifecycleState.STATE_TRAILING,
        )

        assert result.resolved_state is LifecycleState.STATE_TRAILING

    def test_a_position_outranks_a_working_order(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        """Holding an open position is the more urgent fact."""
        result = reconciler.reconcile(
            FakeBook(orders=[order()], positions=[position()]),
            _ledger_with(ledger_path),
        )

        assert result.resolved_state is LifecycleState.STATE_POSITION_OPEN

    def test_a_cancelled_order_is_not_on_the_book(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        result = reconciler.reconcile(
            FakeBook(orders=[order(active=False)]), StateLedger.load(ledger_path)
        )

        assert result.quiet


class TestUnresolvedIntent:
    """The crash case: written, possibly sent, outcome never recorded."""

    def test_it_is_settled_by_finding_the_order(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        ledger = StateLedger.load(ledger_path)
        ledger.record(LedgerEntry(client_tag=TAG, intent=intent(), recorded_at=NOW))

        result = reconciler.reconcile(FakeBook(orders=[order()]), ledger)

        assert len(result.settled) == 1
        assert result.settled[0].outcome == IntentOutcome.PLACED
        assert result.settled[0].ticket == 555

    def test_an_intent_matching_nothing_is_unknown_not_rejected(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        """"Not on the book" is not proof of "never sent"."""
        ledger = StateLedger.load(ledger_path)
        ledger.record(LedgerEntry(client_tag=TAG, intent=intent(), recorded_at=NOW))

        result = reconciler.reconcile(FakeBook(), ledger)

        assert result.settled[0].outcome == IntentOutcome.UNKNOWN

    def test_an_unknown_intent_still_counts_as_sent(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        """The property that stops a restart becoming a duplicate order."""
        ledger = StateLedger.load(ledger_path)
        ledger.record(LedgerEntry(client_tag=TAG, intent=intent(), recorded_at=NOW))

        reconciler.reconcile(FakeBook(), ledger)

        assert ledger.was_sent(TAG), "an unresolved intent may have reached the venue"

    def test_the_settlement_is_durable(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        ledger = StateLedger.load(ledger_path)
        ledger.record(LedgerEntry(client_tag=TAG, intent=intent(), recorded_at=NOW))

        reconciler.reconcile(FakeBook(orders=[order()]), ledger)

        reloaded = StateLedger.load(ledger_path)
        found = reloaded.get(TAG)
        assert found is not None
        assert found.outcome == IntentOutcome.PLACED

    def test_recovery_reports_what_it_resolved(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        ledger = StateLedger.load(ledger_path)
        ledger.record(LedgerEntry(client_tag=TAG, intent=intent(), recorded_at=NOW))

        result = reconciler.reconcile(FakeBook(orders=[order()]), ledger)

        assert result.code == ReconciliationCode.RESOLVED_UNRESOLVED_INTENT

    def test_an_intent_that_filled_and_closed_is_a_note_not_a_contradiction(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        """Ticket 555 is on neither list, and there is no position.

        That is a normal filled-then-closed order and must not raise. Raising here would halt
        the system on every ordinary take-profit after every restart.
        """
        ledger = StateLedger.load(ledger_path)
        ledger.record(LedgerEntry(client_tag=TAG, intent=intent(), recorded_at=NOW))
        ledger.settle(TAG, IntentOutcome.PLACED, ticket=555)

        result = reconciler.reconcile(FakeBook(), ledger)

        assert any("filled and since closed" in note for note in result.notes)


class TestContradictions:
    """Exposure that cannot be *attributed* is the case that stops the system.

    Note what is deliberately **not** a contradiction: a ledger entry that says "placed" with
    an empty book. That is the ordinary filled-then-closed case, and treating it as a
    contradiction would halt the system on every take-profit following every restart.
    """

    def test_an_unattributable_position_stops_the_system(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        """A position nobody is managing is the worst thing to discover late."""
        with pytest.raises(StaleStateError) as caught:
            reconciler.reconcile(
                FakeBook(positions=[position(tag="someoneelse12345678")]),
                StateLedger.load(ledger_path),
            )

        assert "unmanaged exposure" in str(caught.value)

    def test_an_unattributable_working_order_stops_the_system(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        """A working order can still fill, so ignoring it risks unmanaged exposure later."""
        with pytest.raises(StaleStateError) as caught:
            reconciler.reconcile(
                FakeBook(orders=[order(tag="someoneelse12345678")]),
                StateLedger.load(ledger_path),
            )

        assert "can still" in str(caught.value)

    def test_an_unreadable_identity_stops_the_system(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        """A tag that did not survive the comment is unattributable, and that is not safe."""
        with pytest.raises(StaleStateError):
            reconciler.reconcile(
                FakeBook(positions=[position(tag="")]),
                StateLedger.load(ledger_path),
            )

    def test_the_error_names_the_ticket_and_symbol(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        with pytest.raises(StaleStateError) as caught:
            reconciler.reconcile(
                FakeBook(positions=[position(ticket=777)]),
                StateLedger.load(ledger_path),
            )

        assert "777" in str(caught.value)
        assert "US30" in str(caught.value)

    def test_an_attributable_position_resolves_it(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        """The ordinary case: the ledger knows about it."""
        ledger = StateLedger.load(ledger_path)
        ledger.record(LedgerEntry(client_tag=TAG, intent=intent(), recorded_at=NOW))
        ledger.settle(TAG, IntentOutcome.PLACED, ticket=555)

        result = reconciler.reconcile(FakeBook(positions=[position()]), ledger)

        assert result.has_position

    def test_a_rejected_entry_raises_nothing(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        """A rejection was recorded honestly; there is nothing to reconcile."""
        ledger = StateLedger.load(ledger_path)
        ledger.record(LedgerEntry(client_tag=TAG, intent=intent(), recorded_at=NOW))
        ledger.settle_rejected(TAG, "invalid stops")

        result = reconciler.reconcile(FakeBook(), ledger)

        assert result.is_clean


class TestSettledIntents:
    def test_it_settles_by_client_tag_not_by_ticket(
        self, ledger_path: Path
    ) -> None:
        """A ticket is not known before the send; the tag is the only identity that survives."""
        ledger = StateLedger.load(ledger_path)
        ledger.record(LedgerEntry(client_tag=TAG, intent=intent(), recorded_at=NOW))

        settled = settled_intents(ledger, [order(ticket=999)])

        assert settled[0].ticket == 999

    def test_it_ignores_already_settled_entries(self, ledger_path: Path) -> None:
        ledger = StateLedger.load(ledger_path)
        ledger.record(LedgerEntry(client_tag=TAG, intent=intent(), recorded_at=NOW))
        ledger.settle(TAG, IntentOutcome.PLACED, ticket=555)

        assert settled_intents(ledger, []) == ()

    def test_an_order_with_no_tag_matches_nothing(
        self, ledger_path: Path
    ) -> None:
        """An order with no readable identity cannot be matched, and guessing is worse."""
        ledger = StateLedger.load(ledger_path)
        ledger.record(LedgerEntry(client_tag=TAG, intent=intent(), recorded_at=NOW))

        settled = settled_intents(ledger, [order(tag="")])

        assert settled[0].outcome == IntentOutcome.UNKNOWN


class TestReconciliationReporting:
    def test_as_dict_is_serialisable(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        import json

        result = reconciler.reconcile(
            FakeBook(positions=[position()]), _ledger_with(ledger_path)
        )

        assert json.loads(json.dumps(result.as_dict()))["positions"] == 1

    def test_str_names_the_state_and_code(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        result = reconciler.reconcile(FakeBook(), StateLedger.load(ledger_path))

        assert "waiting_for_signal" in str(result)

    def test_contradictions_are_empty_on_a_clean_run(
        self, reconciler: Reconciler, ledger_path: Path
    ) -> None:
        result = reconciler.reconcile(FakeBook(), StateLedger.load(ledger_path))

        assert result.contradictions == ()


class TestReconciliationIsAValue:
    def test_it_can_be_built_directly_for_tests(self) -> None:
        """A dataclass, not something only the reconciler can produce."""
        result = Reconciliation(resolved_state=LifecycleState.STATE_IDLE)

        assert result.quiet
        assert result.is_clean
