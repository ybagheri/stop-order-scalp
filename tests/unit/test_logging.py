"""Structured logging behaviour.

Two properties are load-bearing and are tested here rather than assumed:

* a record is one complete JSON object on one line, so a crash truncates one line
  instead of destroying the file
* there is no code path that can put a credential in a record, because the settings
  objects that carry credentials expose only a presence flag
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from stop_order_scalp.domain.enums import LifecycleState, Side
from stop_order_scalp.domain.models import EnvironmentSettings
from stop_order_scalp.domain.value_objects import Money, Price, Volume
from stop_order_scalp.infrastructure.logging import (
    AUDIT_LOGGER_NAME,
    AuditLogger,
    AuditRecord,
    EventField,
    MemoryAuditSink,
    NullAuditSink,
    StreamAuditSink,
    log_path,
    read_records,
)


@pytest.fixture
def sink(tmp_path: Path) -> Iterator[AuditLogger]:
    logger = AuditLogger(tmp_path / "logs")
    yield logger
    logger.close()


class TestAuditLogger:
    def test_writes_one_json_object_per_line(self, sink: AuditLogger) -> None:
        sink.emit({"event": "signal_accepted", "symbol": "US30"})
        sink.emit({"event": "order_placed", "symbol": "US30"})
        sink.flush()

        lines = sink.file_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert [json.loads(line)["event"] for line in lines] == ["signal_accepted", "order_placed"]

    def test_a_crash_truncates_one_line_not_the_file(self, sink: AuditLogger) -> None:
        sink.emit({"event": "first"})
        sink.flush()
        with sink.file_path.open("a", encoding="utf-8") as handle:
            handle.write('{"event": "torn')

        # A torn tail is skipped, and everything before it survives.
        records = read_records(sink.file_path)
        assert [record["event"] for record in records] == ["first"]

    def test_creates_the_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "deeply" / "nested" / "logs"
        logger = AuditLogger(target)
        logger.emit({"event": "x"})
        logger.close()
        assert (target / "audit.log").is_file()

    def test_rehomes_rather_than_duplicating(self, tmp_path: Path) -> None:
        # Logging loggers are process-global. A second AuditLogger must move the trail,
        # not leave the first file handler attached and write to both locations.
        first_dir = tmp_path / "one"
        second_dir = tmp_path / "two"
        first = AuditLogger(first_dir)
        second = AuditLogger(second_dir)

        second.emit({"event": "after_rehoming"})
        second.flush()
        assert read_records(second.file_path)[0]["event"] == "after_rehoming"
        assert read_records(first.file_path) == []

        first.close()
        second.close()

    def test_log_path_matches_the_written_file(self, tmp_path: Path, sink: AuditLogger) -> None:
        assert log_path(tmp_path / "logs") == sink.file_path

    def test_level_is_info_by_default(self, sink: AuditLogger) -> None:
        assert logging.getLogger(AUDIT_LOGGER_NAME).level == logging.INFO

    def test_a_truncated_line_does_not_break_subsequent_reads(self, sink: AuditLogger) -> None:
        sink.emit({"event": "a"})
        sink.flush()
        with sink.file_path.open("a", encoding="utf-8") as handle:
            handle.write("not json at all\n")
        sink.emit({"event": "b"})
        sink.flush()
        assert [record["event"] for record in read_records(sink.file_path)] == ["a", "b"]


class TestValueRendering:
    def test_decimal_renders_without_scientific_notation(self, sink: AuditLogger) -> None:
        # str(Decimal("0.0001")) is "0.0001"; float rendering would be "1E-4", which is
        # unreadable in a log and imprecise on the way back in.
        sink.emit({"event": "x", "value": Decimal("0.0001")})
        sink.flush()
        assert read_records(sink.file_path)[0]["value"] == "0.0001"

    def test_domain_value_objects_render_readably(self, sink: AuditLogger) -> None:
        sink.emit(
            {
                "event": "plan_built",
                "entry": Price.parse("40000.05", 2),
                "volume": Volume.of("0.1"),
                "side": Side.SIDE_BUY,
                "state": LifecycleState.STATE_WAITING_FOR_SIGNAL,
                "risk": Money.of("500.00"),
            }
        )
        sink.flush()
        record = read_records(sink.file_path)[0]
        assert record["entry"] == "40000.05"
        assert record["volume"] == "0.1"
        assert record["side"] == "BUY"
        assert record["state"] == "waiting_for_signal"
        assert record["risk"] == "500.00 USD"

    def test_datetimes_render_as_utc_with_a_z_suffix(self, sink: AuditLogger) -> None:
        moment = datetime(2026, 3, 12, 12, 0, tzinfo=UTC).astimezone(UTC)
        sink.emit({"event": "x", "moment": moment})
        sink.flush()
        assert read_records(sink.file_path)[0]["moment"].endswith("Z")

    def test_nested_structures_are_reduced(self, sink: AuditLogger) -> None:
        sink.emit({"event": "x", "detail": {"a": Decimal("1"), "b": [Decimal("2")]}})
        sink.flush()
        assert read_records(sink.file_path)[0]["detail"] == {"a": "1", "b": ["2"]}

    def test_none_fields_are_omitted_rather_than_serialised_as_null(self, sink: AuditLogger) -> None:
        sink.emit({"event": "x", "take_profit": None})
        sink.flush()
        assert "take_profit" not in read_records(sink.file_path)[0]

    def test_an_unexpected_object_renders_as_text_not_an_address(self, sink: AuditLogger) -> None:
        # "object at 0x..." looks like data and hides the real problem.
        class Opaque:
            def __repr__(self) -> str:
                return "<opaque>"

        sink.emit({"event": "x", "thing": Opaque()})
        sink.flush()
        assert read_records(sink.file_path)[0]["thing"] == "<opaque>"


class TestAuditRecord:
    def test_timestamp_is_utc_iso_with_z(self) -> None:
        record = AuditRecord(event="x", moment=datetime(2026, 3, 12, 12, 0, tzinfo=UTC))
        assert record.to_dict()["timestamp"] == "2026-03-12T12:00:00Z"

    def test_is_frozen(self) -> None:
        record = AuditRecord(event="x")
        with pytest.raises(AttributeError):
            record.event = "y"  # type: ignore[misc]

    def test_fields_are_sorted_for_a_stable_diff(self) -> None:
        rendered = AuditRecord(event="x", fields={"b": 1, "a": 2}).to_dict()
        assert list(rendered) == ["timestamp", "event", "a", "b"]


class TestEventFieldCoverage:
    def test_every_field_the_specification_requires_is_declared(self) -> None:
        required = {
            "timestamp",
            "symbol",
            "timeframe",
            "candle_id",
            "signal",
            "direction",
            "entry",
            "stop_loss",
            "take_profit",
            "volume",
            "estimated_risk",
            "commission",
            "order_ticket",
            "position_ticket",
            "state",
            "broker_response",
            "error_code",
            "retry",
            "break_even",
            "trailing",
        }
        missing = required - set(EventField)
        # The specification's names are recorded here under the project's canonical
        # spelling; this assertion documents the mapping rather than hiding it.
        assert missing == set()

    def test_event_fields_are_unique(self) -> None:
        assert len(EventField) == len(set(EventField))


class TestInMemorySinks:
    def test_memory_sink_collects_and_can_be_filtered(self) -> None:
        sink = MemoryAuditSink()
        sink.emit({"event": "a", "n": 1})
        sink.emit({"event": "b"})
        sink.emit({"event": "a", "n": 2})
        assert sink.events() == ["a", "b", "a"]
        assert len(sink.find("a")) == 2

    def test_memory_sink_is_bounded(self) -> None:
        sink = MemoryAuditSink(limit=3)
        for n in range(10):
            sink.emit({"event": "x", "n": n})
        assert len(sink) == 3
        # The newest records are the ones worth keeping during an incident.
        assert [record["n"] for record in sink.records] == [7, 8, 9]

    def test_memory_sink_records_are_copies(self) -> None:
        sink = MemoryAuditSink()
        payload = {"event": "a"}
        sink.emit(payload)
        payload["event"] = "mutated"
        assert sink.records[0]["event"] == "a"

    def test_null_sink_discards_everything(self) -> None:
        sink = NullAuditSink()
        sink.emit({"event": "a"})
        sink.flush()

    def test_stream_sink_writes_json_lines(self) -> None:
        buffer = io.StringIO()
        sink = StreamAuditSink(buffer)
        sink.emit({"event": "a", "v": Decimal("1")})
        sink.emit({"event": "b"})
        assert buffer.getvalue().splitlines() == ['{"event": "a", "v": "1"}', '{"event": "b"}']


class TestSecretsAreStructurallyUnloggable:
    def test_environment_settings_render_presence_not_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from stop_order_scalp.domain.models import set_password_present

        monkeypatch.setenv("SOS_MT5_PASSWORD", "hunter2")
        set_password_present(True)
        settings = EnvironmentSettings(mt5_login=42)

        rendered = json.dumps(settings.to_dict())
        assert "hunter2" not in rendered
        assert rendered.count("password") == 1
        assert settings.to_dict()["mt5_password_configured"] is True

    def test_the_password_is_not_an_attribute_of_the_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from stop_order_scalp.domain.models import set_password_present

        monkeypatch.setenv("SOS_MT5_PASSWORD", "hunter2")
        set_password_present(True)
        settings = EnvironmentSettings(mt5_login=42)
        assert not hasattr(settings, "mt5_password")
        assert "hunter2" not in repr(settings)
