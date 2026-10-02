"""The composition root, tested by running it.

Every other suite tests a component. This one runs the assembled system, because the bugs it
is here for were never component bugs: an order born expired, a state machine moved into the
wrong enum, and a bridge between strategy and risk that did not exist. All three were
invisible until the parts were wired together and executed.
"""

from __future__ import annotations

import io
import json
from dataclasses import replace
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
from stop_order_scalp.domain.enums import Environment, LifecycleState
from stop_order_scalp.infrastructure.config import AppConfig, load_config
from stop_order_scalp.infrastructure.persistence import StateLedger
from stop_order_scalp.market_data.symbols import MEASURED


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
    paths = replace(config.paths, state_file=tmp_path / "state.json", state_file_is_default=False)
    return replace(config, paths=paths)


def _shared_paths(config: AppConfig, tmp_path: Path) -> AppConfig:
    """A configuration whose ledger both runs in a test will share.

    ``state_file_is_default=False`` on purpose: these tests are about two runs hitting the
    *same* file, which is what an explicitly configured path gives. The default path is
    scoped per environment, and is covered separately below.
    """
    paths = replace(config.paths, state_file=tmp_path / "state.json", state_file_is_default=False)
    return replace(config, paths=paths)


def _run(config: AppConfig, *, cycles: int) -> Any:
    """Build and run a service over the deterministic series."""
    candles = synthetic_candles(120, timeframe="M1")
    service = build_service(
        config,
        dry_run=True,
        candles=candles,
        m15_candles=aggregate(candles, "M1", "M15"),
    )
    service.run(max_cycles=cycles)
    return service


def _plant_unresolved(state_file: Path) -> None:
    """Write an intent with no recorded outcome -- the state recovery exists to resolve.

    Hand-written rather than produced by a crashed run, because provoking a real crash
    between the write and the settle is not something a test should try to do reliably.
    """
    from datetime import UTC, datetime
    from decimal import Decimal as D

    from stop_order_scalp.domain.enums import OrderKind
    from stop_order_scalp.domain.models import OrderIntent
    from stop_order_scalp.domain.value_objects import Price, Volume
    from stop_order_scalp.infrastructure.persistence import LedgerEntry, StateLedger

    intent = OrderIntent(
        plan_id="planted-plan",
        client_tag="planted-unresolved-intent",
        symbol="US30",
        kind=OrderKind.ORDER_KIND_BUY_STOP,
        volume=Volume.of(D("0.1")),
        entry=Price.parse("40000.0", 1),
        stop_loss=Price.parse("39990.0", 1),
        take_profit=Price.parse("40100.0", 1),
        magic_number=1,
        comment="planted by a test",
        deviation_points=20,
        expiration=None,
    )
    ledger = StateLedger(state_file)
    ledger.record(
        LedgerEntry(
            client_tag=intent.client_tag,
            intent=intent,
            recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )


def config_percent(service: Any) -> Decimal:
    """The configured risk percentage, read rather than assumed."""
    percent: Decimal = service.config.strategy.risk.percent
    return percent


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
        """The size is arithmetic, and this checks the arithmetic rather than memorising it.

        This asserted ``0.4`` lots, and it passed -- for the wrong reason. The tick value was
        hand-written as ``1.0`` when Alpari's US30 is ``0.1``, so a 100-point stop looked like
        $100 per lot instead of $10, and the sizer produced a position a tenth of the intended
        risk. The constant was the bug, not the sizer.

        Phase 11 measured the real specification, the size became 3.12 lots, and the *same*
        calculation is now written out:

            budget        0.5% of 10,000                = 50.00
            risk per lot  100 points x 0.1 USD/point   = 10.00
            commission    6.00 per lot, round trip      =  6.00
            size          50 / (10 + 6) = 3.125, floored to the 0.01 step = 3.12

        So the assertion states the budget, the per-lot risk and the commission, and requires
        the total to land just under the budget without exceeding it. A change in the tick
        value, the stop distance or the commission now moves this test rather than requiring
        someone to remember to update a magic number.
        """
        service.run(max_cycles=30)
        record = next(iter(service.broker._orders.values()))

        balance = Decimal("10000")
        budget = balance * config_percent(service) / Decimal(100)
        per_point = MEASURED.value_per_point_per_lot
        stop_points = Decimal("100")
        risk_per_lot = per_point * stop_points
        commission_per_lot = Decimal("6.0")
        cost_per_lot = risk_per_lot + commission_per_lot

        lots = record.volume.lots
        assert lots == (budget / cost_per_lot).quantize(MEASURED.volume_step), (
            f"sized at {lots} lots, which is not the risk budget divided by the per-lot cost"
        )
        assert lots * cost_per_lot <= budget, "the position risks more than the budget allows"
        assert (lots + MEASURED.volume_step) * cost_per_lot > budget, (
            "a step larger would still have fitted, so the position is smaller than the "
            "budget allows -- risk is not being taken"
        )

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


class TestAcrossTwoRuns:
    """What a *second* run sees. The class of bug this file is really about.

    Every test above builds a service and runs it once, in a fresh temporary directory. That
    is why 1116 tests passed while the restart-recovery path was dead code and the ledger was
    being overwritten on every run: nothing in the suite ever had two runs share a file.

    The bugs these cover were found by hand, after the suite was green, by running the same
    command twice. Each test here is that experiment, made permanent.
    """

    def test_a_second_run_does_not_erase_the_first_runs_ledger(
        self, config: AppConfig, tmp_path: Path
    ) -> None:
        """The first write of run two must not discard run one's records.

        ``flush`` rewrites the whole file from memory. If run two starts from an empty
        in-memory ledger, it writes an empty file over the top -- and the record whose entire
        purpose is to prevent a duplicate order is gone.

        Checked with a **sentinel** entry, not by comparing counts. Two earlier versions of
        this assertion were both vacuous: one compared disk against memory, which agree even
        when both are wrong, and one compared entry counts, which match because run two
        re-records the *same* client tag and so replaces run one's row one-for-one. A
        distinct tag is the only thing that can show the row was dropped.
        """
        shared = _shared_paths(config, tmp_path)
        _run(shared, cycles=8)
        _plant_unresolved(shared.paths.state_file)
        sentinel = "planted-unresolved-intent"

        assert StateLedger.load(shared.paths.state_file).get(sentinel) is not None

        second = _run(shared, cycles=8)
        assert second.lifecycle.ledger.durable, "the shared ledger should be persistent"

        survived = StateLedger.load(shared.paths.state_file)
        assert survived.get(sentinel) is not None, (
            "run two's first write discarded a record that was already on disk; the ledger "
            "that exists to prevent a duplicate order has been deleted by running the program"
        )

    def test_a_second_run_recovers_rather_than_starting_blind(
        self, config: AppConfig, tmp_path: Path
    ) -> None:
        """An intent left unresolved by run one must be visible to run two.

        This is the whole point of the write-intent ledger: an intent with no recorded outcome
        may have reached the broker, and only the broker can say. Recovery asks. If run two
        cannot see run one's entry, the question is never asked and a duplicate is possible.
        """
        shared = _shared_paths(config, tmp_path)
        first = _run(shared, cycles=8)

        # Simulate a crash between the write and the outcome being recorded, which is the
        # exact window the ledger exists to cover.
        for entry in list(first.lifecycle.ledger.entries()):
            first.lifecycle.ledger.forget(entry.client_tag)
            break
        _plant_unresolved(shared.paths.state_file)

        second = _run(shared, cycles=4)

        assert len(second.lifecycle.ledger.awaiting_confirmation()) == 1, (
            "run two could not see the unresolved intent run one left behind"
        )

    def test_a_dry_run_and_a_live_run_never_share_a_ledger(
        self, config: AppConfig, tmp_path: Path
    ) -> None:
        """A simulated venue's records are not claims about a real venue.

        With one shared file, a dry run's synthetic intent makes ``was_sent`` answer *yes*
        for a real run over the same candles, and the live run refuses to place an order it
        never sent. The system becomes unable to trade, silently.
        """
        resolved = {
            environment: config.paths.state_file_for(environment) for environment in Environment
        }

        assert resolved[Environment.DRY_RUN] != resolved[Environment.LIVE]
        assert resolved[Environment.DRY_RUN] != resolved[Environment.PAPER]
        assert resolved[Environment.LIVE] != resolved[Environment.PAPER]

    def test_a_dry_run_is_repeatable(self, config: AppConfig, tmp_path: Path) -> None:
        """Running the identical command twice must give the identical answer.

        This is the property a dry run exists to have. It comes from the ledger being scoped
        per environment: with one shared file the second invocation found its own first
        invocation's entries and reported ``already_recorded`` instead of ``placed``.

        The point of the test is the *default* path resolution, so it deliberately does not
        override ``state_file`` -- doing so is what made an earlier version of this test pass
        a shared ledger and then fail to describe anything real.
        """
        scoped = replace(config.paths, state_file_is_default=True)
        paths = replace(
            scoped,
            state_file=tmp_path / "state.json",  # same name, but flagged as the default
        )
        first_config = replace(config, paths=paths)
        assert paths.state_file_for(Environment.DRY_RUN).name == "state-dry_run.json"

        first = _run(first_config, cycles=8)
        second = _run(first_config, cycles=8)

        assert [c.action for c in first.cycles] == [c.action for c in second.cycles], (
            "two identical dry runs disagreed"
        )
        actions = [cycle.action for cycle in second.cycles]
        assert "already_recorded" not in actions, (
            f"a fresh dry run was blocked by its own previous entries: {actions}"
        )
        assert "placed" in actions, f"the repeat run placed nothing: {actions}"

    def test_an_explicitly_shared_ledger_still_blocks_a_duplicate(
        self, config: AppConfig, tmp_path: Path
    ) -> None:
        """The other half of the same coin: when two runs *do* share a file, the guard fires.

        A dry run is repeatable because it gets its own file, not because the duplicate check
        was weakened. Point two runs at one ledger and the second must decline to re-send an
        identity the first already recorded -- that is the whole reason the ledger exists.
        """
        shared = _shared_paths(config, tmp_path)
        _run(shared, cycles=8)
        second = _run(shared, cycles=8)

        actions = [cycle.action for cycle in second.cycles]
        assert "already_recorded" in actions, (
            f"a shared ledger failed to block a repeat send: {actions}"
        )

    def test_an_explicit_state_file_is_used_verbatim(
        self, config: AppConfig, tmp_path: Path
    ) -> None:
        """A path the operator chose is a path they meant exactly. No suffixing.

        Only the *default* is scoped per environment. Suffixing a configured path would mean
        the ledger the operator pointed at is quietly not the one that gets written.
        """
        chosen = tmp_path / "my-ledger.json"
        paths = replace(config.paths, state_file=chosen, state_file_is_default=False)

        for environment in Environment:
            assert paths.state_file_for(environment) == chosen

    def test_asking_the_journal_does_not_erase_the_journal(
        self, config: AppConfig, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Reading the ledger must not write to it.

        ``journal`` used to construct the ledger rather than load it, and the context manager
        flushes on exit -- so simply asking what had happened replaced the file with an empty
        one. The worst possible failure for a record kept for exactly this reason.
        """
        shared = _shared_paths(config, tmp_path)
        _run(shared, cycles=8)
        before = StateLedger.load(shared.paths.state_file)
        assert len(before) > 0, "nothing to lose"

        # Called with *this test's* config rather than through argv: the CLI re-reads the
        # project config from disk, and an earlier version of this test passed a `--config`
        # flag that does not exist, so the command read a different ledger than the one the
        # test had populated and passed against the bug it was written for.
        import argparse

        from stop_order_scalp.cli import main as cli_main

        captured = io.StringIO()
        exit_code = cli_main._cmd_journal(  # the unit under test, hence the private name
            argparse.Namespace(limit=None), shared, captured, io.StringIO()
        )
        capsys.readouterr()

        assert exit_code == 0
        assert json.loads(captured.getvalue())["count"] > 0, "the journal read nothing"

        after = StateLedger.load(shared.paths.state_file)
        assert len(after) == len(before), (
            f"reading the journal changed the ledger from {len(before)} to {len(after)} entries"
        )

    def test_the_default_ledger_is_scoped_per_environment(
        self, config: AppConfig
    ) -> None:
        paths = replace(config.paths, state_file_is_default=True)
        resolved = {env: paths.state_file_for(env) for env in Environment}

        assert len(set(resolved.values())) == len(Environment)
        assert resolved[Environment.LIVE].name == "state.json", (
            "the live ledger should keep the name an operator will look for"
        )
        assert resolved[Environment.DRY_RUN].name == "state-dry_run.json"


class TestWhatTheReportClaims:
    def test_the_action_label_and_its_detail_come_from_one_source(
        self, config: AppConfig, tmp_path: Path
    ) -> None:
        """A cycle labelled `hold` must be described by the `hold` branch.

        The report used to synthesise ``"hold"`` when a step carried no actions, while
        ``_describe`` read that same emptiness as ``""`` and fell through to the plan. So a
        line could read "hold" beside "planned 0.4 lots at 40006.3" while the venue held an
        order at 40005.7. Two places deciding what "no action" means is how it happened.
        """
        service = _run(_shared_paths(config, tmp_path), cycles=8)

        for cycle in service.cycles:
            if cycle.action != "hold":
                continue
            assert not cycle.detail.startswith("planned "), (
                f"cycle {cycle.index} is labelled 'hold' but describes a plan: "
                f"{cycle.detail!r}"
            )

    def test_a_hold_names_the_resting_order_not_the_plan(
        self, config: AppConfig, tmp_path: Path
    ) -> None:
        """A cycle that placed nothing must name what the venue holds, not a fresh plan.

        The reader sees "planned 0.4 lots at 40006.3" beside a venue holding an order placed
        at 40005.7, and reasonably concludes there are two orders. There is one.

        No ``continue`` guard here. An earlier version skipped any detail it did not
        recognise, which meant that with the bug present -- every detail reading "planned" --
        the loop body never ran and the test passed. A guard that skips the failing case is
        worse than no assertion.
        """
        service = _run(_shared_paths(config, tmp_path), cycles=8)
        holds = [cycle for cycle in service.cycles if cycle.action == "hold"]
        assert holds, "expected at least one hold cycle after a placement"

        resting = {
            str(record.entry) for record in service.broker.orders()
        } | {str(position.entry) for position in service.broker.positions()}
        assert resting, "expected the venue to be holding something during a hold"

        for cycle in holds:
            assert not cycle.detail.startswith("planned "), (
                f"cycle {cycle.index} is a hold but quotes a plan nobody is working: "
                f"{cycle.detail!r}"
            )
            assert any(entry in cycle.detail for entry in resting), (
                f"cycle {cycle.index} reported {cycle.detail!r}, which names none of the "
                f"entries the venue actually holds ({sorted(resting)})"
            )

    def test_a_placed_cycle_reports_the_brokers_numbers(
        self, config: AppConfig, tmp_path: Path
    ) -> None:
        service = _run(_shared_paths(config, tmp_path), cycles=8)
        placed = [cycle for cycle in service.cycles if cycle.action == "placed"]
        assert placed, "expected a placement"

        cycle = placed[0]
        record = next(iter(service.broker._orders.values()))
        for value in (record.ticket, str(record.entry), str(record.stop_loss)):
            assert str(value) in cycle.detail, (
                f"the placement line {cycle.detail!r} does not carry {value!r} from the venue"
            )
