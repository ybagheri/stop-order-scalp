"""The write-intent-before-act ledger.

Every order this system sends is written here **before** the send. If the process dies
between the write and the send, recovery finds an intent with no outcome and knows to ask the
broker. If it dies after the send but before the outcome is recorded, recovery finds an intent
with no outcome and knows the same thing. There is no window in which an order could exist at
the broker and be unknown locally, because the intent that could produce it was already
durable.

That is the whole design. It is deliberately boring: a JSON file, written atomically.

Three properties, and each one exists because its absence produces a duplicate order
---------------------------------------------------------------------

**Atomic writes.** A state file is rewritten in full every time. A half-written file is
unparseable, and an unparseable ledger is indistinguishable from a lost one -- so the write
goes to a temporary file in the same directory, is flushed and ``fsync``-ed, and is then
``os.replace``-d over the target. On Windows ``os.replace`` is atomic where a delete-then-write
would not be, which is why no separate unlink step exists.

**Refuse to overwrite.** :meth:`Ledger.record` raises rather than replacing an entry with the
same key. Two intents with one client tag means the tag derivation has regressed, and
silently keeping the second would discard exactly the record that explains the duplicate.

**Never "start fresh".** :meth:`Ledger.load` raises :class:`PersistenceError` on an unreadable
or unparseable ledger. It never returns an empty ledger for a file that exists, because losing
the idempotency record is the condition under which duplicate orders appear. An absent file is
different: that is a genuine first run.

What is stored
--------------

Intent and outcome are **separate records**, not one row with a nullable outcome. The absence
of an outcome *is* the signal, and modelling it as a nullable column invites a query that
filters it away. :meth:`Ledger.unresolved` is the question the recovery path actually asks.

The file is JSON rather than SQLite because it must be readable by a human at 3am with a text
editor, and because a single account's state is a few kilobytes. Phase 9's backtester will
want more and gets its own store.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from stop_order_scalp.domain.enums import LifecycleState
from stop_order_scalp.domain.exceptions import PersistenceError
from stop_order_scalp.domain.models import OrderIntent, OrderRecord

__all__ = [
    "IntentOutcome",
    "JsonStateLedger",
    "LedgerEntry",
    "StateLedger",
    "atomic_write_json",
]

#: Bumped when the on-disk shape changes incompatibly. A ledger with a different version is
#: refused rather than migrated, because a migration cannot know what the old rows meant.
LEDGER_VERSION: int = 1


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` so that a reader sees either the old file or the new one.

    The temporary file is created in the *same directory* as the target so that
    ``os.replace`` stays within one filesystem -- a temp file on another volume would make the
    replace a copy, which is not atomic at all.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # The temporary file is created in the *same directory* as the target so the replace
    # stays within one filesystem -- a temp file on another volume makes the replace a copy,
    # which is not atomic at all.
    descriptor, name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=_encode)
            handle.flush()
            # fsync before the replace: without it the rename can be durable while the contents
            # are not, which is the one failure atomicity alone does not cover.
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        # Never leave a stray temp file behind on the failure path.
        temporary.unlink(missing_ok=True)
        raise


def _encode(value: Any) -> Any:
    """Make the handful of domain types the ledger stores JSON-safe.

    Money becomes a string, never a float: a state file that round-trips through ``float``
    would quietly lose precision on exactly the numbers a duplicate-order investigation needs.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, LifecycleState):
        return str(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"{type(value).__name__} is not JSON-serialisable")


class IntentOutcome:
    """How a send ended. The vocabulary is deliberately the same as the exception layer's.

    ``unknown`` is a member rather than a gap, because an intent with no outcome is precisely
    the state recovery has to handle and it must not be represented by absence.
    """

    PLACED: str = "placed"
    REJECTED: str = "rejected"
    CANCELLED: str = "cancelled"
    UNKNOWN: str = "unknown"


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One intent and, once known, what became of it.

    An entry with ``outcome is None`` is an **unresolved intent**: written, possibly sent,
    possibly not. Recovery treats it as a question for the broker rather than as a failure.
    """

    client_tag: str
    #: The full intent, so a restart can re-derive the identity check without the strategy.
    intent: OrderIntent
    recorded_at: datetime
    outcome: str | None = None
    ticket: int | None = None
    reason: str = ""
    #: The lifecycle state the system believed it was in when the intent was written.
    state: LifecycleState = LifecycleState.STATE_VALIDATING
    #: Free-form, for operator notes. Never carries a secret: the model has no field for one.
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.outcome is not None

    @property
    def unresolved(self) -> bool:
        """Whether this intent's fate has never been recorded at all.

        The absence of an outcome is the signal, which is why this exists as a named property
        rather than as ``entry.outcome is None`` scattered through the recovery code.
        """
        return self.outcome is None

    @property
    def needs_observation(self) -> bool:
        """Whether the broker still has to be asked about this intent.

        Wider than :attr:`unresolved`, and deliberately so: an intent recorded ``UNKNOWN`` has
        *an* outcome but not a *known* one, and it is exactly those that recovery exists to
        resolve. Treating them as settled would leave ``VERIFYING`` with nothing to verify.
        """
        return self.outcome is None or self.outcome == IntentOutcome.UNKNOWN

    def confirmed(self, ticket: int, reason: str = "") -> LedgerEntry:
        """Upgrade an ``UNKNOWN`` entry to ``PLACED`` once the broker confirms it.

        Separate from :meth:`with_outcome` because the ledger's ``settle`` guard refuses to
        overwrite a recorded outcome -- a rule that must not block recovering from the one
        outcome that means "we do not know yet".
        """
        if self.outcome != IntentOutcome.UNKNOWN:
            raise PersistenceError(
                f"cannot confirm {self.client_tag!r}: its outcome is {self.outcome!r}, not "
                "unknown. Only an unknown outcome may be upgraded."
            )
        return LedgerEntry(
            client_tag=self.client_tag,
            intent=self.intent,
            recorded_at=self.recorded_at,
            outcome=IntentOutcome.PLACED,
            ticket=ticket,
            reason=reason,
            state=self.state,
            context=dict(self.context),
        )

    def with_outcome(
        self, outcome: str, *, ticket: int | None = None, reason: str = ""
    ) -> LedgerEntry:
        return LedgerEntry(
            client_tag=self.client_tag,
            intent=self.intent,
            recorded_at=self.recorded_at,
            outcome=outcome,
            ticket=ticket,
            reason=reason,
            state=self.state,
            context=dict(self.context),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_tag": self.client_tag,
            "intent": _intent_to_dict(self.intent),
            "recorded_at": self.recorded_at.isoformat(),
            "outcome": self.outcome,
            "ticket": self.ticket,
            "reason": self.reason,
            "state": str(self.state),
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LedgerEntry:
        return cls(
            client_tag=str(raw["client_tag"]),
            intent=_intent_from_dict(raw["intent"]),
            recorded_at=datetime.fromisoformat(str(raw["recorded_at"])),
            outcome=raw.get("outcome"),
            ticket=raw.get("ticket"),
            reason=str(raw.get("reason", "")),
            state=_as_state(raw.get("state")),
            context=dict(raw.get("context") or {}),
        )


class StateLedger:
    """A durable, append-mostly record of every intent this system formed.

    Keyed by ``client_tag``, because that is the identity that survives a restart, and because
    two entries with one tag is the condition :meth:`record` refuses.
    """

    __slots__ = ("_dirty", "_entries", "_path")

    def __init__(self, path: Path, entries: dict[str, LedgerEntry] | None = None) -> None:
        self._path: Path | None = path
        self._entries: dict[str, LedgerEntry] = dict(entries or {})
        #: Whether anything has been written since this ledger was opened. Only
        #: :class:`JsonStateLedger` reads it; a plain :class:`StateLedger` flushes on every
        #: mutation and has no use for it.
        self._dirty = False

    @property
    def path(self) -> Path | None:
        """Where this ledger is written, or ``None`` when it is in memory."""
        return self._path

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, client_tag: object) -> bool:
        return client_tag in self._entries

    # --- reading ---------------------------------------------------------

    def get(self, client_tag: str) -> LedgerEntry | None:
        return self._entries.get(client_tag)

    def entries(self) -> tuple[LedgerEntry, ...]:
        """Every entry, oldest first. Deterministic ordering for a readable file."""
        return tuple(sorted(self._entries.values(), key=lambda entry: entry.recorded_at))

    def unresolved(self) -> tuple[LedgerEntry, ...]:
        """Intents whose outcome was never recorded. The question recovery asks first."""
        return tuple(entry for entry in self.entries() if entry.unresolved)

    def awaiting_confirmation(self) -> tuple[LedgerEntry, ...]:
        """Intents whose real outcome the broker still has to answer.

        Both never-recorded and recorded-unknown. The second group is why this exists: after an
        ambiguous send the entry *has* an outcome, and treating that as settled would leave the
        lifecycle in ``VERIFYING`` with nothing left to verify.
        """
        return tuple(entry for entry in self.entries() if entry.needs_observation)

    def was_sent(self, client_tag: str) -> bool:
        """Whether this identity is already known to have reached the broker.

        The duplicate check's question after a restart, where local trading state is gone but
        the ledger is not. An *unresolved* intent counts: it may have been sent, and assuming
        otherwise is how a duplicate is created.
        """
        entry = self._entries.get(client_tag)
        if entry is None:
            return False
        return entry.outcome in (IntentOutcome.PLACED, IntentOutcome.UNKNOWN, None)

    # --- writing ---------------------------------------------------------

    def record(self, entry: LedgerEntry) -> LedgerEntry:
        """Add an intent **before** the send it describes.

        :raises PersistenceError: if the tag is already recorded. Refusing rather than
            overwriting is deliberate: two intents sharing a client tag means the tag
            derivation has regressed, and keeping only the second would discard the record
            that explains the duplicate.
        """
        existing = self._entries.get(entry.client_tag)
        if existing is not None:
            raise PersistenceError(
                f"client tag {entry.client_tag!r} is already recorded (recorded at "
                f"{existing.recorded_at.isoformat()}, outcome {existing.outcome!r}). "
                "Two intents must never share an identity; the tag derivation has regressed."
            )
        self._entries[entry.client_tag] = entry
        self._dirty = True
        self.flush()
        return entry

    def settle(
        self, client_tag: str, outcome: str, *, ticket: int | None = None, reason: str = ""
    ) -> LedgerEntry:
        """Record what became of a recorded intent.

        :raises PersistenceError: for an unknown tag, or for settling one twice. Settling an
            entry twice would overwrite the first outcome with a second guess, and the first is
            the one that happened.
        """
        entry = self._entries.get(client_tag)
        if entry is None:
            raise PersistenceError(
                f"cannot settle {client_tag!r}: it was never recorded. The write-intent-"
                "before-act rule was bypassed."
            )
        if entry.resolved:
            raise PersistenceError(
                f"cannot settle {client_tag!r} again: already {entry.outcome!r}"
                + (f" at ticket {entry.ticket}" if entry.ticket else "")
            )
        updated = entry.with_outcome(outcome, ticket=ticket, reason=reason)
        self._entries[client_tag] = updated
        self._dirty = True
        self.flush()
        return updated

    def settle_from_record(self, client_tag: str, record: OrderRecord) -> LedgerEntry:
        """Settle an intent from the broker's own record of it."""
        return self.settle(client_tag, IntentOutcome.PLACED, ticket=record.ticket)

    def settle_rejected(self, client_tag: str, reason: str) -> LedgerEntry:
        return self.settle(client_tag, IntentOutcome.REJECTED, reason=reason)

    def mark_unknown(self, client_tag: str, reason: str = "") -> LedgerEntry:
        """Mark an intent as sent-but-unacknowledged.

        Deliberately **not** settled to a failure. The order may be on the venue; recording it
        as rejected would make the next cycle re-send it.
        """
        return self.settle(client_tag, IntentOutcome.UNKNOWN, reason=reason)

    def confirm(self, client_tag: str, ticket: int, reason: str = "") -> LedgerEntry:
        """Upgrade an ``unknown`` entry to ``placed`` now that the broker has confirmed it.

        The one case where a recorded outcome is replaced, and it is safe because ``unknown``
        is an explicit statement of ignorance rather than a fact about the venue.
        """
        entry = self._entries.get(client_tag)
        if entry is None:
            raise PersistenceError(f"cannot confirm {client_tag!r}: it was never recorded")
        confirmed = entry.confirmed(ticket, reason)
        self._entries[client_tag] = confirmed
        self._dirty = True
        self.flush()
        return confirmed

    def forget(self, client_tag: str) -> None:
        """Drop an entry, for an operator resolving a contradiction by hand.

        Never called by the trading path. Exposed because an operator staring at a stale ledger
        needs a way to clear it that is deliberate rather than a file edit.
        """
        if client_tag in self._entries:
            del self._entries[client_tag]
            self._dirty = True
            self.flush()

    # --- durability ------------------------------------------------------

    def flush(self) -> None:
        """Write the whole ledger atomically. A no-op for an in-memory ledger."""
        if self._path is None:
            return
        atomic_write_json(
            self._path,
            {
                "version": LEDGER_VERSION,
                "entries": [entry.to_dict() for entry in self.entries()],
            },
        )

    @classmethod
    def in_memory(cls) -> StateLedger:
        """A ledger that keeps every rule except surviving the process.

        For a venue that does not outlive the run. A dry run builds a fresh
        ``SimulatedBroker`` every time, so a durable ledger beside it would claim that
        identities reached a venue that has no record of them -- and the duplicate guard
        would then refuse a legitimate placement on the second run of an identical command.

        The ledger's value is that it and the venue agree on what was sent, so their
        lifetimes have to match. Everything else still holds: record before send, refuse a
        repeated tag, settle once. Only :meth:`flush` stops writing.

        Restart recovery is a property of a venue that outlives the process, so it is proven
        against a persistent ledger in the application tests and against a real terminal in
        Phase 11 -- not here, where there is nothing to restart.
        """
        ledger = cls.__new__(cls)
        ledger._path = None
        ledger._entries = {}
        ledger._dirty = False
        return ledger

    @property
    def durable(self) -> bool:
        """Whether this ledger is written to disk. False only for :meth:`in_memory`."""
        return self._path is not None

    # --- loading ---------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> StateLedger:
        """Read the ledger, or return an empty one for a genuine first run.

        :raises PersistenceError: if the file exists but cannot be read or parsed. **Never**
            returns an empty ledger for a file that exists: losing the idempotency record is
            the exact condition under which duplicate orders appear, and silently starting
            fresh would hide it.
        """
        if not path.exists():
            return cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PersistenceError(
                f"the state ledger at {path} exists but cannot be read ({exc}). Refusing to "
                "start: an unreadable ledger is indistinguishable from a lost one, and "
                "starting fresh is how duplicate orders appear. Move the file aside to "
                "deliberately begin again."
            ) from exc
        version = raw.get("version")
        if version != LEDGER_VERSION:
            raise PersistenceError(
                f"the ledger at {path} is version {version!r}, this build writes version "
                f"{LEDGER_VERSION}. Refusing rather than migrating: a migration cannot know "
                "what the old rows meant."
            )
        entries: dict[str, LedgerEntry] = {}
        try:
            for row in raw.get("entries") or []:
                entry = LedgerEntry.from_dict(row)
                entries[entry.client_tag] = entry
        except (KeyError, TypeError, ValueError) as exc:
            raise PersistenceError(
                f"the ledger at {path} is structurally invalid ({exc}). Refusing to start for "
                "the same reason as an unparseable one."
            ) from exc
        return cls(path, entries)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": LEDGER_VERSION,
            "path": str(self._path),
            "entries": len(self._entries),
            "unresolved": len(self.unresolved()),
        }

    def __repr__(self) -> str:
        return (
            f"StateLedger({self._path}, entries={len(self._entries)}, "
            f"unresolved={len(self.unresolved())})"
        )


class JsonStateLedger(StateLedger):
    """The name the CLI has expected for :class:`StateLedger` since Phase 1.

    A subclass rather than an alias, and deliberately thin. The CLI asks for this name by
    string, so renaming the class under it would have been a silent break; a subclass keeps the
    name meaningful while staying a single implementation.

    Adds a context manager, which is how the ``journal`` command wants to use it. The manager
    flushes **only if something was written** -- see :meth:`__exit__` for why that is not the
    same as always flushing.
    """

    def __enter__(self) -> JsonStateLedger:
        self._dirty = False
        return self

    def __exit__(self, *_: object) -> None:
        # Flush only when this ledger was actually modified. Every mutating method already
        # flushes as it goes -- durability is a property of the write, not of a later flush
        # someone might forget -- so an unconditional flush here bought nothing and cost a
        # file: `journal` is a *reader*, and opening a context manager around it created the
        # ledger it had just reported as absent. A read that brings a file into existence is
        # how a "no trades yet" state becomes indistinguishable from a real one.
        if self._dirty:
            self.flush()
            self._dirty = False

    @classmethod
    def open(cls, path: Path) -> JsonStateLedger:
        return cls.load(path)  # type: ignore[return-value]

    def journal(self, limit: int) -> list[dict[str, Any]]:
        """The most recent ``limit`` entries, newest last.

        The journal is a subset of the ledger, not a second store: an intent that was recorded
        is an intent the operator should be able to read back. Ordering is by record time, so
        the last element is the most recent.
        """
        entries = self.entries()
        selected = entries[-limit:] if limit > 0 else ()
        return [entry.to_dict() for entry in selected]


def _as_state(raw: object) -> LifecycleState:
    if raw is None:
        return LifecycleState.STATE_IDLE
    try:
        return LifecycleState(str(raw))
    except ValueError:
        return LifecycleState.STATE_IDLE


def _intent_to_dict(intent: OrderIntent) -> dict[str, Any]:
    return {
        "plan_id": intent.plan_id,
        "client_tag": intent.client_tag,
        "symbol": intent.symbol,
        "kind": str(intent.kind),
        "volume": str(intent.volume.lots),
        "entry": str(intent.entry.value),
        "digits": intent.entry.digits,
        "stop_loss": str(intent.stop_loss.value),
        "take_profit": str(intent.take_profit.value),
        "magic_number": intent.magic_number,
        "comment": intent.comment,
        "deviation_points": intent.deviation_points,
        "expiration": None if intent.expiration is None else intent.expiration.isoformat(),
    }


def _intent_from_dict(raw: dict[str, Any]) -> OrderIntent:
    from stop_order_scalp.domain.enums import OrderKind
    from stop_order_scalp.domain.value_objects import Price, Volume

    digits = int(raw["digits"])
    return OrderIntent(
        plan_id=str(raw["plan_id"]),
        client_tag=str(raw["client_tag"]),
        symbol=str(raw["symbol"]),
        kind=OrderKind(str(raw["kind"])),
        volume=Volume.of(Decimal(str(raw["volume"]))),
        entry=Price.parse(str(raw["entry"]), digits),
        stop_loss=Price.parse(str(raw["stop_loss"]), digits),
        take_profit=Price.parse(str(raw["take_profit"]), digits),
        magic_number=int(raw["magic_number"]),
        comment=str(raw["comment"]),
        deviation_points=int(raw["deviation_points"]),
        expiration=(
            None
            if raw.get("expiration") is None
            else datetime.fromisoformat(str(raw["expiration"]))
        ),
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
