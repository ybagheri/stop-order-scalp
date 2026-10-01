"""The ledger is the thing that stops a restart becoming a duplicate order.

The cases that matter here are the failure ones: a ledger that cannot be read, an entry that
tries to overwrite another, and an intent whose outcome is unknown. Each is a situation where
the *wrong* behaviour is to continue as though nothing happened.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from stop_order_scalp.domain.enums import LifecycleState, OrderKind, Side
from stop_order_scalp.domain.exceptions import PersistenceError
from stop_order_scalp.domain.models import OrderIntent, OrderRecord
from stop_order_scalp.domain.value_objects import Price, Volume
from stop_order_scalp.infrastructure.persistence import (
    IntentOutcome,
    LedgerEntry,
    StateLedger,
    atomic_write_json,
)

NOW = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)


def intent(tag: str = "abc123def456abc123def4") -> OrderIntent:
    return OrderIntent(
        plan_id="US30-M15-1-M1-1",
        client_tag=tag,
        symbol="US30",
        kind=OrderKind.ORDER_KIND_BUY_STOP,
        volume=Volume.of(Decimal("0.4")),
        entry=Price.parse("40000.0", 1),
        stop_loss=Price.parse("39990.0", 1),
        take_profit=Price.parse("40020.0", 1),
        magic_number=20260930,
        comment="buy stop",
        deviation_points=20,
    )


def entry(tag: str = "abc123def456abc123def4", **kwargs: object) -> LedgerEntry:
    return LedgerEntry(
        client_tag=tag,
        intent=intent(tag),
        recorded_at=kwargs.pop("recorded_at", NOW),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "state.json"


class TestFirstRun:
    def test_an_absent_file_is_a_genuine_empty_ledger(self, path: Path) -> None:
        ledger = StateLedger.load(path)

        assert len(ledger) == 0
        assert not path.exists(), "loading must not create the file"

    def test_an_empty_ledger_reports_no_unresolved(self, path: Path) -> None:
        assert StateLedger.load(path).unresolved() == ()


class TestRecordingBeforeSending:
    def test_recording_writes_immediately(self, path: Path) -> None:
        """The write must be durable *before* the send, not batched at exit."""
        ledger = StateLedger.load(path)

        ledger.record(entry())

        assert path.exists(), "the intent must be on disk before anything can be sent"
        assert len(StateLedger.load(path)) == 1

    def test_a_recorded_intent_starts_unresolved(self, path: Path) -> None:
        ledger = StateLedger.load(path)
        ledger.record(entry())

        recorded = ledger.get("abc123def456abc123def4")
        assert recorded is not None
        assert recorded.unresolved
        assert len(ledger.unresolved()) == 1

    def test_recording_two_distinct_intents_is_allowed(self, path: Path) -> None:
        ledger = StateLedger.load(path)

        ledger.record(entry("tag111111111111111111"))
        ledger.record(entry("tag222222222222222222"))

        assert len(ledger) == 2


class TestRefuseToOverwrite:
    def test_the_same_tag_twice_is_refused(self, path: Path) -> None:
        """Two intents with one identity means the tag derivation regressed."""
        ledger = StateLedger.load(path)
        ledger.record(entry())

        with pytest.raises(PersistenceError, match="already recorded"):
            ledger.record(entry())

    def test_the_refusal_names_the_tag(self, path: Path) -> None:
        ledger = StateLedger.load(path)
        ledger.record(entry())

        with pytest.raises(PersistenceError, match="abc123def456abc123def4"):
            ledger.record(entry())

    def test_a_refused_record_is_not_stored(self, path: Path) -> None:
        ledger = StateLedger.load(path)
        ledger.record(entry())

        with pytest.raises(PersistenceError):
            ledger.record(entry())

        assert len(ledger) == 1


class TestSettling:
    def test_settling_records_the_ticket(self, path: Path) -> None:
        ledger = StateLedger.load(path)
        ledger.record(entry())

        settled = ledger.settle("abc123def456abc123def4", IntentOutcome.PLACED, ticket=555)

        assert settled.resolved
        assert settled.ticket == 555
        reloaded = ledger.get("abc123def456abc123def4")
        assert reloaded is not None
        assert reloaded.ticket == 555

    def test_settling_from_a_broker_record(self, path: Path) -> None:
        ledger = StateLedger.load(path)
        ledger.record(entry())
        record = OrderRecord(
            ticket=556,
            client_tag="abc123def456abc123def4",
            symbol="US30",
            kind=OrderKind.ORDER_KIND_BUY_STOP,
            volume=Volume.of(Decimal("0.4")),
            entry=Price.parse("40000.0", 1),
            stop_loss=Price.parse("39990.0", 1),
            take_profit=Price.parse("40020.0", 1),
            magic_number=20260930,
            comment="",
            placed_at=NOW,
        )

        settled = ledger.settle_from_record("abc123def456abc123def4", record)

        assert settled.ticket == 556

    def test_settling_twice_is_refused(self, path: Path) -> None:
        """Overwriting the first outcome with a second guess loses the fact that happened."""
        ledger = StateLedger.load(path)
        ledger.record(entry())
        ledger.settle("abc123def456abc123def4", IntentOutcome.PLACED, ticket=555)

        with pytest.raises(PersistenceError, match="already"):
            ledger.settle("abc123def456abc123def4", IntentOutcome.REJECTED)

    def test_settling_an_unrecorded_tag_is_refused(self, path: Path) -> None:
        """Settling something never recorded means write-intent-before-act was bypassed."""
        ledger = StateLedger.load(path)

        with pytest.raises(PersistenceError, match="never recorded"):
            ledger.settle("tag111111111111111111", IntentOutcome.PLACED)


class TestUnknownOutcome:
    def test_marking_unknown_is_a_resolution_not_a_rejection(
        self, path: Path
    ) -> None:
        """The distinction the whole recovery path depends on.

        An unknown outcome is resolved *as unknown*. Recording it as rejected would make the
        next cycle re-send an order that may be sitting on the venue.
        """
        ledger = StateLedger.load(path)
        ledger.record(entry())

        ledger.mark_unknown("abc123def456abc123def4", "terminal lost track")

        found = ledger.get("abc123def456abc123def4")
        assert found is not None
        assert found.outcome == IntentOutcome.UNKNOWN

    @pytest.mark.parametrize(
        "outcome",
        [IntentOutcome.PLACED, IntentOutcome.UNKNOWN],
    )
    def test_was_sent_is_true_for_placed_and_unknown(
        self, path: Path, outcome: str
    ) -> None:
        ledger = StateLedger.load(path)
        ledger.record(entry())

        ledger.settle("abc123def456abc123def4", outcome, ticket=555)

        assert ledger.was_sent("abc123def456abc123def4")

    def test_was_sent_is_true_for_an_unresolved_intent(self, path: Path) -> None:
        """The crash case: written, possibly sent, outcome never recorded.

        Treating this as "not sent" is exactly how a restart produces a duplicate order.
        """
        ledger = StateLedger.load(path)
        ledger.record(entry())

        assert ledger.was_sent("abc123def456abc123def4")

    def test_was_sent_is_false_for_a_rejection(self, path: Path) -> None:
        ledger = StateLedger.load(path)
        ledger.record(entry())

        ledger.settle_rejected("abc123def456abc123def4", "invalid stops")

        assert not ledger.was_sent("abc123def456abc123def4")

    def test_was_sent_is_false_for_a_tag_never_seen(self, path: Path) -> None:
        assert not StateLedger.load(path).was_sent("tag111111111111111111")


class TestNeverStartFresh:
    def test_an_unparseable_ledger_is_refused(self, path: Path) -> None:
        """Losing the idempotency record is the condition under which duplicates appear."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json", encoding="utf-8")

        with pytest.raises(PersistenceError, match="cannot be read"):
            StateLedger.load(path)

    def test_the_refusal_explains_what_to_do(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("garbage", encoding="utf-8")

        with pytest.raises(PersistenceError, match="Move the file aside"):
            StateLedger.load(path)

    def test_a_ledger_of_the_wrong_version_is_refused(self, path: Path) -> None:
        """A migration cannot know what the old rows meant."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 999, "entries": []}), encoding="utf-8")

        with pytest.raises(PersistenceError, match="version"):
            StateLedger.load(path)

    def test_a_structurally_invalid_entry_is_refused(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1, "entries": [{"client_tag": "x"}]}),
            encoding="utf-8",
        )

        with pytest.raises(PersistenceError, match="structurally invalid"):
            StateLedger.load(path)

    def test_an_empty_object_is_treated_as_empty_not_invalid(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1}), encoding="utf-8")

        assert len(StateLedger.load(path)) == 0


class TestAtomicWrites:
    def test_the_file_is_replaced_not_appended(self, path: Path) -> None:
        """The reader must see the whole ledger or the previous one, never a prefix."""
        atomic_write_json(path, {"version": 1, "entries": [1, 2, 3]})
        atomic_write_json(path, {"version": 1, "entries": [4]})

        assert json.loads(path.read_text(encoding="utf-8"))["entries"] == [4]

    def test_no_temporary_files_are_left_behind(self, path: Path) -> None:
        ledger = StateLedger.load(path)

        for index in range(5):
            ledger.record(entry(f"tag{index:016d}"))

        leftovers = [p.name for p in path.parent.iterdir() if p.name != path.name]
        assert leftovers == [], f"stray temporary files: {leftovers}"

    def test_a_failed_write_leaves_the_previous_file_intact(
        self, path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash mid-write must not destroy the record that was already durable."""
        ledger = StateLedger.load(path)
        ledger.record(entry())
        before = path.read_text(encoding="utf-8")

        def explode(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("stop_order_scalp.infrastructure.persistence.os.fsync", explode)

        with pytest.raises(OSError):
            ledger.record(entry("tag222222222222222222"))

        assert path.read_text(encoding="utf-8") == before


class TestRoundTrip:
    def test_an_intent_survives_a_round_trip_exactly(self, path: Path) -> None:
        """Precision matters: a ledger that loses digits would mislead an investigation."""
        ledger = StateLedger.load(path)
        original = OrderIntent(
            plan_id="US30-M15-1-M1-1",
            client_tag="abc123def456abc123def4",
            symbol="US30",
            kind=OrderKind.ORDER_KIND_BUY_STOP,
            volume=Volume.of(Decimal("0.47")),
            entry=Price.parse("40000.05", 2),
            stop_loss=Price.parse("39990.05", 2),
            take_profit=Price.parse("40020.07", 2),
            magic_number=20260930,
            comment="buy stop",
            deviation_points=20,
            expiration=NOW,
        )
        ledger.record(
            LedgerEntry(
                client_tag=original.client_tag,
                intent=original,
                recorded_at=NOW,
                state=LifecycleState.STATE_VALIDATING,
            )
        )

        restored = StateLedger.load(path).get(original.client_tag)

        assert restored is not None
        assert restored.intent == original
        assert restored.state is LifecycleState.STATE_VALIDATING
        assert restored.recorded_at == NOW

    def test_money_is_stored_as_a_string_not_a_float(self, path: Path) -> None:
        """A float round trip would quietly lose precision on the numbers that matter."""
        ledger = StateLedger.load(path)
        ledger.record(entry())

        raw = path.read_text(encoding="utf-8")

        assert '"0.4"' in raw
        assert "0.4," not in raw.replace('"0.4"', "")


class TestOrdering:
    def test_entries_come_back_oldest_first(self, path: Path) -> None:
        from datetime import timedelta

        ledger = StateLedger.load(path)
        ledger.record(entry("tag111111111111111111", recorded_at=NOW))
        ledger.record(entry("tag222222222222222222", recorded_at=NOW + timedelta(seconds=1)))
        ledger.record(entry("tag333333333333333333", recorded_at=NOW - timedelta(seconds=1)))

        assert [e.client_tag for e in ledger.entries()] == [
            "tag333333333333333333",
            "tag111111111111111111",
            "tag222222222222222222",
        ]

    def test_a_ledger_can_be_forgotten_deliberately(self, path: Path) -> None:
        ledger = StateLedger.load(path)
        ledger.record(entry())

        ledger.forget("abc123def456abc123def4")

        assert len(ledger) == 0
        assert path.exists()


class TestReporting:
    def test_as_dict_reports_unresolved(self, path: Path) -> None:
        ledger = StateLedger.load(path)
        ledger.record(entry())

        report = ledger.as_dict()

        assert report["entries"] == 1
        assert report["unresolved"] == 1

    def test_repr_names_the_path(self, path: Path) -> None:
        assert str(path) in repr(StateLedger.load(path))

    def test_membership_test(self, path: Path) -> None:
        ledger = StateLedger.load(path)
        ledger.record(entry())

        assert "abc123def456abc123def4" in ledger


class TestSellIntent:
    def test_a_sell_intent_round_trips(self, path: Path) -> None:
        ledger = StateLedger.load(path)
        short = OrderIntent(
            plan_id="US30-M15-2-M1-2",
            client_tag="def456abc123def456abc1",
            symbol="US30",
            kind=OrderKind.ORDER_KIND_SELL_STOP,
            volume=Volume.of(Decimal("0.1")),
            entry=Price.parse("40000.0", 1),
            stop_loss=Price.parse("40010.0", 1),
            take_profit=Price.parse("39980.0", 1),
            magic_number=20260930,
            comment="sell stop",
            deviation_points=20,
        )
        ledger.record(
            LedgerEntry(client_tag=short.client_tag, intent=short, recorded_at=NOW)
        )

        restored = StateLedger.load(path).get(short.client_tag)

        assert restored is not None
        assert restored.intent.kind is OrderKind.ORDER_KIND_SELL_STOP
        assert restored.intent.kind.side is Side.SIDE_SELL
