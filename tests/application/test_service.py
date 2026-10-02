"""The composition root, tested by running it.

Every other suite tests a component. This one runs the assembled system, because the bugs it
is here for were never component bugs: an order born expired, a state machine moved into the
wrong enum, and a bridge between strategy and risk that did not exist. All three were
invisible until the parts were wired together and executed.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from stop_order_scalp.application.service import (
    LiveTradingUnavailable,
    aggregate,
    build_service,
    load_candles_csv,
    synthetic_candles,
)
from stop_order_scalp.domain.enums import LifecycleState
from stop_order_scalp.infrastructure.config import AppConfig, load_config


@pytest.fixture
def config() -> AppConfig:
    return load_config()


@pytest.fixture
def service(config: AppConfig, tmp_path: Path) -> Any:
    config = _with_state_in(config, tmp_path)
    candles = synthetic_candles(120, timeframe="M1")
    return build_service(
        config,
        dry_run=True,
        candles=candles,
        m15_candles=aggregate(candles, "M1", "M15"),
    )


def _with_state_in(config: AppConfig, tmp_path: Path) -> AppConfig:
    """Point the ledger at a temporary directory.

    A dry run writes a real ledger, and a test that writes to ``state/state.json`` is a test
    that fails on a second run and leaves debris behind on the first.
    """
    from dataclasses import replace


    paths = replace(config.paths, state_file=tmp_path / "state.json")
    return replace(config, paths=paths)


class TestItRuns:
    def test_a_dry_run_returns_ok(self, service: Any) -> None:
        assert service.run(max_cycles=30) > 0

    def test_it_walks_forward_through_the_data(self, service: Any) -> None:
        """Each cycle must see a *newer* bar, or the loop is running and going nowhere."""
        service.run(max_cycles=20)
        prices = [cycle.price for cycle in service.cycles]

        assert len(set(prices)) > 1, "every cycle decided on the same bar"
        times = [cycle.moment for cycle in service.cycles]
        assert times == sorted(times)

    def test_it_places_an_order(self, service: Any) -> None:
        service.run(max_cycles=30)

        assert any(cycle.placed for cycle in service.cycles), "no order was ever placed"

    def test_the_order_carries_both_protective_levels(self, service: Any) -> None:
        """A stop without a target is not a scalping trade; it is an open-ended risk."""
        service.run(max_cycles=30)
        # Read the internal book, not ``orders()``: by now the order has filled, so it has
        # left the working-orders list and the level it was sent with is what is under test.
        record = next(iter(service.broker._orders.values()))

        assert record.stop_loss is not None
        assert record.take_profit is not None
        assert record.entry > record.stop_loss, "a BUY stop must sit below entry"
        assert record.entry < record.take_profit, "a BUY target must sit above entry"

    def test_the_order_is_sized_from_the_risk_budget(self, service: Any) -> None:
        service.run(max_cycles=30)
        record = next(iter(service.broker._orders.values()))

        # 0.5% of a $10,000 balance with a 100-point stop and $6/lot commission.
        assert record.volume.lots == Decimal("0.4")

    def test_a_full_lifecycle_runs(self, service: Any) -> None:
        """Place, fill, then manage the stop while the position is open.

        The whole point of the phase: each of these was broken in a way no component test
        could see.
        """
        service.run(max_cycles=60)

        assert any(cycle.placed for cycle in service.cycles)
        assert any(
            cycle.action in ("trailed", "break_even_armed") for cycle in service.cycles
        ), "the position was never managed"

    def test_the_state_machine_stays_in_its_own_vocabulary(self, service: Any) -> None:
        """A regression test for the ``PositionState``/``LifecycleState`` mix-up.

        Both are ``StrEnum``, so the machine accepted either and the failure surfaced three
        cycles later as an ``AttributeError`` on ``quiet``.
        """
        service.run(max_cycles=40)

        for cycle in service.cycles:
            LifecycleState(cycle.state)  # raises if it is a PositionState

    def test_the_report_is_serialisable(self, service: Any) -> None:
        import json

        service.run(max_cycles=10)
        json.dumps(service.last_report())

    def test_the_report_says_the_specification_is_assumed(self, service: Any) -> None:
        """A number computed on assumed prices must be labelled assumed."""
        service.run(max_cycles=5)

        assert "assumed" in service.last_report()["specification_source"]

    def test_the_report_warns_that_a_dry_run_proves_nothing_about_the_strategy(
        self, service: Any
    ) -> None:
        service.run(max_cycles=5)

        assert "not the strategy" in service.last_report()["note"]


class TestItRefusesWhatItCannotDo:
    def test_live_is_refused_by_name(self, config: AppConfig) -> None:
        with pytest.raises(LiveTradingUnavailable, match="not available"):
            build_service(config, live=True)

    def test_live_is_refused_before_anything_is_wired(
        self, config: AppConfig, tmp_path: Path
    ) -> None:
        """The refusal must come first, so nothing is constructed on the way out."""
        with pytest.raises(LiveTradingUnavailable):
            build_service(_with_state_in(config, tmp_path), live=True)

    def test_a_dry_run_uses_the_simulated_broker(self, service: Any) -> None:
        assert type(service.broker).__name__ == "SimulatedBroker"

    def test_a_dry_run_is_not_live(self, service: Any) -> None:
        assert not service.settings.allow_live
        assert str(service.settings.environment) == "DRY_RUN"


class TestDeterminism:
    def test_two_runs_of_the_same_input_agree(self, config: AppConfig, tmp_path: Path) -> None:
        """A replay that depends on when it was run is not a replay."""

        def run_once(slot: str) -> list[tuple[int, str, str, bool]]:
            candles = synthetic_candles(120, timeframe="M1")
            service = build_service(
                _with_state_in(config, tmp_path / slot),
                dry_run=True,
                candles=candles,
                m15_candles=aggregate(candles, "M1", "M15"),
            )
            service.run(max_cycles=30)
            return [(c.index, c.action, c.detail, c.placed) for c in service.cycles]

        assert run_once("first") == run_once("second")


class TestCandleData:
    def test_the_synthetic_series_trends(self) -> None:
        """A zigzag would make the correct behaviour "never trade", which reads as broken."""
        candles = synthetic_candles(60, timeframe="M1")

        assert candles[-1].close > candles[0].close

    def test_the_synthetic_series_is_deterministic(self) -> None:
        first = synthetic_candles(20, timeframe="M1")
        second = synthetic_candles(20, timeframe="M1")

        assert [c.close for c in first] == [c.close for c in second]

    def test_aggregation_groups_the_right_number_of_bars(self) -> None:
        candles = synthetic_candles(60, timeframe="M1")

        grouped = aggregate(candles, "M1", "M15")

        assert len(grouped) == 4
        assert grouped[0].timeframe == "M15"

    def test_aggregation_takes_the_extremes_of_each_group(self) -> None:
        candles = synthetic_candles(30, timeframe="M1")

        grouped = aggregate(candles, "M1", "M15")

        assert grouped[0].high == max(c.high for c in candles[:15])
        assert grouped[0].low == min(c.low for c in candles[:15])

    def test_aggregation_of_an_empty_series_is_empty(self) -> None:
        assert aggregate([], "M1", "M15") == []

    def test_aggregation_refuses_to_split_a_bar(self) -> None:
        """A target shorter than the source is refused rather than invented.

        Splitting an M15 into three M1 bars would mean fabricating prices the data does not
        contain, which is the shape of a look-ahead bug.
        """
        m15 = aggregate(synthetic_candles(60, timeframe="M1"), "M1", "M15")

        assert aggregate(m15, "M15", "M1") == []


class TestCsvLoading:
    def _write(self, tmp_path: Path, text: str) -> Path:
        path = tmp_path / "candles.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_it_reads_epoch_seconds(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "time,open,high,low,close\n1773316800,1,2,0.5,1.5\n")

        candles = load_candles_csv(path, timeframe="M1")

        assert len(candles) == 1
        assert candles[0].close.value == Decimal("1.5")

    def test_it_reads_iso_timestamps(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "time,open,high,low,close\n2026-03-12T12:00:00,1,2,0.5,1.5\n")

        candles = load_candles_csv(path, timeframe="M1")

        assert candles[0].open_time.hour == 12

    def test_it_treats_a_naive_timestamp_as_utc(self, tmp_path: Path) -> None:
        """Machine-local time is exactly the bug the project's clock rule prevents."""
        path = self._write(tmp_path, "time,open,high,low,close\n2026-03-12 12:00:00,1,2,0.5,1.5\n")

        candles = load_candles_csv(path, timeframe="M1")

        assert candles[0].open_time.tzinfo is not None

    def test_it_sorts_by_time(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "time,open,high,low,close\n"
            "2026-03-12T12:02:00,1,2,0.5,1.5\n"
            "2026-03-12T12:00:00,1,2,0.5,1.5\n",
        )

        candles = load_candles_csv(path, timeframe="M1")

        assert candles[0].open_time < candles[1].open_time

    def test_a_short_row_names_the_line(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "time,open,high,low,close\n2026-03-12T12:00:00,1,2\n")

        with pytest.raises(ValueError, match="line 2"):
            load_candles_csv(path, timeframe="M1")

    def test_a_bad_number_names_the_line(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path, "time,open,high,low,close\n2026-03-12T12:00:00,1,two,0.5,1.5\n"
        )

        with pytest.raises(ValueError, match="line 2"):
            load_candles_csv(path, timeframe="M1")

    def test_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "time,open,high,low,close\n2026-03-12T12:00:00,1,2,0.5,1.5\n\n",
        )

        assert len(load_candles_csv(path, timeframe="M1")) == 1

    def test_a_file_without_a_header_still_loads(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "2026-03-12T12:00:00,1,2,0.5,1.5\n")

        assert len(load_candles_csv(path, timeframe="M1")) == 1


class TestOrderExpiry:
    """A regression test for the bug that made every order die on arrival.

    ``OrderManager.intent_for(plan, now=...)`` passed ``now`` straight through as the
    *expiration*, so every order was born already expired. No unit test caught it because
    they all called it without ``now``; the dry run did, on its first order.
    """

    def test_an_order_is_not_born_expired(self, config: AppConfig, tmp_path: Path) -> None:
        candles = synthetic_candles(120, timeframe="M1")
        service = build_service(
            _with_state_in(config, tmp_path),
            dry_run=True,
            candles=candles,
            m15_candles=aggregate(candles, "M1", "M15"),
        )
        service.run(max_cycles=30)

        record = next(iter(service.broker._orders.values()))
        assert record.expires_at is None, "a null lifetime must mean GTC, not expired"

    def test_the_order_survives_long_enough_to_fill(self, config: AppConfig, tmp_path: Path) -> None:
        candles = synthetic_candles(120, timeframe="M1")
        service = build_service(
            _with_state_in(config, tmp_path),
            dry_run=True,
            candles=candles,
            m15_candles=aggregate(candles, "M1", "M15"),
        )
        service.run(max_cycles=30)

        record = next(iter(service.broker._orders.values()))
        assert record.state == "FILLED", f"the order ended as {record.state}"
