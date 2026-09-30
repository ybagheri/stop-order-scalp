"""Structured logging.

Two rules shape this module.

**Secrets are structurally unloggable.** There is no secret field anywhere on the log
record types, and credential-carrying settings expose ``password_configured: bool``
rather than the value. A redaction filter can be forgotten, removed, or bypassed by a
new call site; a field that does not exist cannot be leaked. This is the pattern adopted
from the sibling ``auto-trade`` project, and it is the reason no filter is configured
here.

**One event, one line, one file.** JSON Lines, rotated at 5 MB with 5 backups. JSON Lines
rather than a JSON array so that a crash truncates the last line instead of destroying
the file, and so that ``tail`` is useful during an incident.

The logger name is process-global, so :func:`configure` re-homes handlers rather than
adding a second set. Two ``AuditLogger`` instances pointed at different directories
therefore move the trail instead of writing into each other.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, TextIO

__all__ = [
    "AUDIT_LOGGER_NAME",
    "AuditLogger",
    "AuditRecord",
    "EventField",
    "MemoryAuditSink",
    "NullAuditSink",
    "StderrAuditSink",
    "StreamAuditSink",
    "configure",
    "get_logger",
    "log_path",
    "read_records",
]

#: The one logger this project writes structured records to.
AUDIT_LOGGER_NAME: Final[str] = "stop_order_scalp.audit"

_MAX_BYTES: Final[int] = 5 * 1024 * 1024
_BACKUP_COUNT: Final[int] = 5

#: Canonical event field names.
#
# Listed explicitly so that the specification's required field list is checkable by
# reading this file, and so a typo in a call site shows up as a missing key in a test
# rather than as a silently absent field in a log.
EventField: Final[tuple[str, ...]] = (
    "timestamp",  # when the event happened, UTC ISO-8601 with a Z suffix
    "event",  # what kind of event this is
    "environment",  # DRY_RUN / PAPER / DEMO / LIVE
    "symbol",
    "timeframe",  # entry timeframe
    "direction_timeframe",  # M15
    "candle_id",  # identifies the exact candle pair the decision used
    "signal",  # BUY / SELL / NONE
    "signal_source",  # which producer emitted the signal
    "side",  # the project's canonical spelling
    "direction",  # the specification's spelling of `side`, emitted alongside it
    "entry",
    "stop_loss",
    "take_profit",
    "volume",
    "price_risk",  # entry-to-stop monetary risk, commission excluded
    "commission",
    "total_risk",  # price_risk + commission: the figure the 0.5 % budget bounds
    "estimated_risk",  # the specification's spelling of `total_risk`
    "risk_fraction",
    "order_ticket",
    "position_ticket",
    "client_tag",
    "state",  # lifecycle state before or after the transition
    "previous_state",
    "transition",
    "broker_response",
    "broker_retcode",
    "error_code",
    "error",
    "retry",
    "retry_count",
    "break_even",
    "trailing",
    "close_reason",
    "plan_id",
    "account",
    "server",
    "balance",
    "equity",
)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """A structured event on its way to the log.

    Frozen, so a record cannot be edited after it has been emitted -- which would make
    the log disagree with the decision that produced it.
    """

    event: str
    fields: Mapping[str, Any] = field(default_factory=dict)
    moment: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "timestamp": (self.moment or datetime.now(UTC)).astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "event": self.event,
        }
        for key, value in sorted(self.fields.items()):
            if value is None:
                continue
            record[key] = _jsonable(value)
        return record


class MemoryAuditSink:
    """Collects records in a list.

    Used by tests and by ``run --dry-run`` when no log directory is configured. Bounded
    so that a long paper-trading session cannot exhaust memory: the oldest records are
    dropped once the cap is reached.
    """

    __slots__ = ("_limit", "_records")

    def __init__(self, limit: int = 10_000) -> None:
        self._records: list[dict[str, Any]] = []
        self._limit = limit

    def emit(self, event: dict[str, Any]) -> None:
        self._records.append(dict(event))
        if len(self._records) > self._limit:
            del self._records[: len(self._records) - self._limit]

    def flush(self) -> None:
        return None

    @property
    def records(self) -> list[dict[str, Any]]:
        return list(self._records)

    def events(self) -> list[str]:
        return [str(record.get("event")) for record in self._records]

    def find(self, event: str) -> list[dict[str, Any]]:
        return [record for record in self._records if record.get("event") == event]

    def clear(self) -> None:
        self._records.clear()

    def __len__(self) -> int:
        return len(self._records)


class NullAuditSink:
    """Discards everything. Used by tests that assert on behaviour, not on logs."""

    __slots__ = ()

    def emit(self, event: dict[str, Any]) -> None:
        del event  # the parameter exists to satisfy AuditSink; it is deliberately dropped
        return None

    def flush(self) -> None:
        return None


class AuditLogger:
    """Writes JSON Lines to a rotating file.

    Also usable as a :class:`~stop_order_scalp.domain.interfaces.AuditSink`: it has
    ``emit`` and ``flush``, so no separate adapter is needed.
    """

    __slots__ = ("_handler", "_logger", "_path")

    def __init__(self, directory: Path | str, *, filename: str = "audit.log", level: int = logging.INFO) -> None:
        self._path = Path(directory).expanduser().resolve()
        self._path.mkdir(parents=True, exist_ok=True)
        # The concrete handler is kept, not a reference read back out of the logger:
        # ``baseFilename`` is declared on FileHandler, not on the Handler base class.
        handler = logging.handlers.RotatingFileHandler(
            self._path / filename,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        self._logger = _install(handler, level=level)
        self._handler = handler

    @property
    def path(self) -> Path:
        return self._path

    @property
    def file_path(self) -> Path:
        return Path(self._handler.baseFilename)

    def emit(self, event: Mapping[str, Any]) -> None:
        """Write one record. Accepts a mapping so any protocol implementation works."""
        self._logger.info(json.dumps(_normalise(event), default=str, sort_keys=True))

    def flush(self) -> None:
        self._handler.flush()

    def close(self) -> None:
        self._logger.removeHandler(self._handler)
        self._handler.close()

    def __enter__(self) -> AuditLogger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"AuditLogger({self.file_path})"


def _install(handler: logging.Handler, *, level: int) -> logging.Logger:
    """Attach ``handler`` to the audit logger, replacing any handler already there.

    ``logging`` loggers are process-global singletons. Without the replacement,
    constructing a second :class:`AuditLogger` would leave the first file handler
    attached, and every event would be written to both the old and the new location --
    an operational trap that a single dict of ``log_directory`` values would hide.
    """
    logger = logging.getLogger(AUDIT_LOGGER_NAME)
    logger.setLevel(level)
    # Propagation would double every record onto the root handler. The audit trail has
    # exactly one destination.
    logger.propagate = False
    # The record is already a complete JSON object; the formatter must add nothing.
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.setLevel(level)
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        existing.close()
    logger.addHandler(handler)
    return logger


class StderrAuditSink:
    """Writes JSON Lines to stderr. Used when no log directory is configured.

    stderr rather than stdout so that machine-readable CLI output on stdout stays
    parseable by a supervisor process.
    """

    __slots__ = ("_stream",)

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr

    def emit(self, event: Mapping[str, Any]) -> None:
        self._stream.write(json.dumps(_normalise(event), default=str, sort_keys=True) + "\n")

    def flush(self) -> None:
        self._stream.flush()


class StreamAuditSink(StderrAuditSink):
    """Writes JSON Lines to a chosen stream. Used by ``--dry-run`` on the console."""

    def __init__(self, stream: TextIO | None = None) -> None:
        super().__init__(stream if stream is not None else sys.stdout)


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child of the audit logger for ad-hoc messages."""
    if name is None:
        return logging.getLogger(AUDIT_LOGGER_NAME)
    return logging.getLogger(f"{AUDIT_LOGGER_NAME}.{name}")


def log_path(directory: Path | str, filename: str = "audit.log") -> Path:
    """Where :class:`AuditLogger` would write. Used by ``diagnostics``."""
    return Path(directory).expanduser().resolve() / filename


def read_records(path: Path | str) -> list[dict[str, Any]]:
    """Parse a JSON Lines audit file, skipping unparsable lines.

    Skipping rather than raising is deliberate here and only here: the file is the
    diagnostic artefact, and refusing to read it would hide the very lines around the
    damage. The state ledger, whose integrity affects behaviour, raises instead.
    """
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
    return records


def _normalise(event: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce a record to JSON, dropping ``None`` fields.

    Dropping ``None`` rather than writing ``null`` keeps the common case -- a trade with
    no take profit yet -- from putting a meaningless key in every single line of the log,
    and matches what :meth:`AuditRecord.to_dict` does.
    """
    return {key: _jsonable(value) for key, value in event.items() if value is not None}


def _jsonable(value: Any) -> Any:
    """Reduce a value to something ``json.dumps`` can always handle.

    ``Decimal``, ``datetime`` and the domain value objects all render as strings, which
    is lossless in a log. Nothing falls back to ``repr``: a log full of
    ``object at 0x...`` is worse than no log, because it looks like data.
    """
    from stop_order_scalp.domain.value_objects import Money, Price, Volume

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, (Money, Volume, Price)):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return str(value)


def configure(
    directory: Path | str | None, *, level: int = logging.INFO
) -> AuditLogger | StderrAuditSink:
    """Build the audit sink for this run.

    With a directory, records go to a rotating file. Without one, they go to stderr.
    Either way a decision is always explainable afterwards, which is the point: an
    unexplained trade is indistinguishable from a bug.
    """
    if directory is None:
        return StderrAuditSink()
    return AuditLogger(directory, level=level)
