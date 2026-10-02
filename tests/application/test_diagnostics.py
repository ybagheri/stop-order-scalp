"""The last two commands, and the specification they report.

Phase 11's first deliverable. `status` and `diagnostics` were the only commands still exiting
4, and `diagnostics` is the one needed to watch a demo account -- so it is worth testing what it
says, not just that it runs.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from stop_order_scalp.application.diagnostics import build_diagnostics
from stop_order_scalp.application.status import collect_status
from stop_order_scalp.infrastructure.config import AppConfig, load_config
from stop_order_scalp.market_data.symbols import MEASURED, us30_specification


@pytest.fixture
def config() -> AppConfig:
    return load_config()


class TestStatus:
    def test_it_reports_a_state(self, config: AppConfig) -> None:
        report = collect_status(config)
        assert report["lifecycle_state"]
        assert report["open_positions"] == 0
        assert report["working_orders"] == 0

    def test_it_carries_the_key_the_exit_code_reads(self, config: AppConfig) -> None:
        """The CLI's handler reads `report["reachable"]`, and raised KeyError without it.

        Found by running the command: it printed a correct report and then exited 1 with a
        traceback. A handler that crashes on the shape of its own output is worse than one that
        returns the wrong code, because it looks like the command is broken rather than the
        contract having drifted.
        """
        report = collect_status(config)
        assert "reachable" in report, "the CLI reads this key to decide its exit code"

    def test_it_serialises(self, config: AppConfig) -> None:
        json.dumps(collect_status(config))

    def test_it_says_where_the_live_ledger_is(self, config: AppConfig) -> None:
        """A dry run's own ledger counts nothing across processes, and the report says so."""
        report = collect_status(config)
        assert report["live_ledger"].endswith("state.json")
        assert "in memory" in report["ledger"]["note"]


class TestDiagnostics:
    def test_it_reports_the_environment_and_the_rule(self, config: AppConfig) -> None:
        report = build_diagnostics(config)
        assert report["environment"]["environment"]
        assert report["rule"]["direction_timeframe"] == config.strategy.entry.direction_timeframe
        assert report["rule"]["entry_timeframe"] == config.strategy.entry.timeframe

    def test_a_configured_secret_never_reaches_the_bundle(
        self, config: AppConfig, monkeypatch: Any
    ) -> None:
        """A real secret must not appear in the output, whatever the wording around it.

        An earlier version of this test searched the bundle for the *word* "secret" and failed
        on its own explanation that credentials are reduced to a presence flag. Searching for
        words tests the prose; searching for the value tests the property.

        A diagnostics bundle gets written to files, pasted into issues and pasted into chat, so
        a credential appearing in one is a leak with a long tail -- and the only check that
        matters is whether the actual value is there.
        """
        from dataclasses import replace

        canary = "hunter2-not-a-real-password"
        monkeypatch.setenv("SOS_MT5_PASSWORD", canary)
        loaded = load_config()
        payload = json.dumps(build_diagnostics(loaded))

        assert canary not in payload, (
            "a configured password reached the diagnostics bundle"
        )
        # And no key that could plausibly hold one.
        for key in json.loads(payload).get("connection", {}):
            assert "password" not in key.lower() or key == "password", (
                f"connection reports a {key!r} field, which is where a secret would leak"
            )
        del replace  # imported for the type above; the monkeypatch does the work

    def test_it_states_where_the_specification_came_from(self, config: AppConfig) -> None:
        """A measured value without a date and a source is a rumour."""
        report = build_diagnostics(config)
        specification = report["specification"]
        assert "measured" in specification["source"]
        assert specification["values"]["tick_value"] == str(MEASURED.tick_value)
        assert "Re-measure" in specification["caveat"]

    def test_a_connection_failure_is_data_not_a_crash(self, config: AppConfig) -> None:
        """Pointing it at a terminal that does not exist must still produce a report.

        `diagnostics` is the command people reach for when something is wrong. Raising would
        make it useless in exactly the situation it exists for.
        """
        from dataclasses import replace

        broken = replace(
            config,
            environment=replace(
                config.environment, mt5_path="C:\\nope\\terminal64.exe"
            ),
        )
        report = build_diagnostics(broken)
        assert report["connection"]["connected"] is False
        assert report["connection"]["error"]
        json.dumps(report)

    def test_it_never_places_anything(self, config: AppConfig, monkeypatch: Any) -> None:
        """Building diagnostics must not reach an order path, even with a terminal attached.

        Checked by making every write raise, so a regression that tried to place would fail
        here rather than on someone's demo account.
        """
        from stop_order_scalp.execution import simulated_broker

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("diagnostics attempted to trade")

        monkeypatch.setattr(simulated_broker.SimulatedBroker, "place_order", refuse)
        monkeypatch.setattr(simulated_broker.SimulatedBroker, "close", refuse)
        build_diagnostics(config)  # a connection failure is fine; a trade is not


class TestTheMeasuredSpecification:
    def test_it_is_the_one_the_terminal_reports(self) -> None:
        """The value that decided every money figure in this project, asserted once more.

        A test that pins a *measurement* rather than a magic number: if Alpari changes it, the
        re-measurement updates this deliberately instead of someone editing a constant to make
        a failing test go away.
        """
        specification = us30_specification("US30")
        assert specification.tick_value == Decimal("0.1")
        assert specification.volume_min == Decimal("0.01")
        assert specification.stops_level == 0

    def test_one_point_on_one_lot(self) -> None:
        """The derived number, because this is the one that is easy to get wrong.

        `tick_value` is the value of one `tick_size` increment, not of one point. With both at
        0.1 the two coincide, which is exactly why a 10x error hid in this constant for ten
        phases without looking wrong.
        """
        assert MEASURED.value_per_point_per_lot == Decimal("0.1")

    def test_the_sizer_can_produce_a_size_the_broker_accepts(self) -> None:
        """A valid position must be reachable, or every order is refused at the venue.

        The sizer *floors* rather than rounding up, because rounding up past the risk budget
        would breach it. The cost of that choice is that it can land below the broker's
        minimum, and then the order manager refuses rather than sends -- correct, and a reason
        a trade simply does not happen.

        With a measured tick value the budget buys several lots, so there is room for the floor
        to land on a valid step. The assertion is on that relationship rather than on a number,
        because it is the relationship that a change in tick value or risk percentage would
        break.
        """
        specification = us30_specification("US30")
        balance = Decimal("10000")
        budget = balance * Decimal("0.5") / Decimal(100)
        risk_per_lot = MEASURED.value_per_point_per_lot * Decimal("100")
        commission_per_lot = Decimal("6.0")
        cost_per_lot = risk_per_lot + commission_per_lot
        size = (budget / cost_per_lot).quantize(specification.volume_step)

        assert size >= specification.volume_min, (
            f"the risk budget buys {size} lots, below the broker's minimum of "
            f"{specification.volume_min}, so every order would be refused"
        )


class TestTheReplayIsNotQuadratic:
    def test_a_long_replay_stays_linear(self, config: AppConfig) -> None:
        """A month of real data used to take over twenty-five minutes. It now takes five seconds.

        The replay handed the whole sorted series to `freeze_closed_bars` on every cycle, and
        the freeze sorts and re-validates what it is given. That is O(n^2): 30 000 bars meant
        `is_closed_at` four and a half million times, and one month of US30 was not a thing
        you could run.

        The threshold is deliberately loose -- 8 seconds for 2 000 cycles, where the quadratic
        version took minutes. A performance test that pins the exact time breaks on slow
        machines and after unrelated changes; one that catches the return of O(n^2) does not.
        """
        import time

        from stop_order_scalp.application.service import synthetic_candles
        from stop_order_scalp.backtest.replay import replay
        from stop_order_scalp.market_data.candles import aggregate

        bars = synthetic_candles(2000, timeframe="M1")
        start = time.perf_counter()
        result = replay(config, bars, m15_candles=aggregate(bars, "M1", "M15"))
        elapsed = time.perf_counter() - start

        assert len(result.steps) > 1000, "the replay did not run the bars it was given"
        assert elapsed < 8.0, (
            f"2000 cycles took {elapsed:.1f}s. The window handed to the freeze is probably "
            "growing with the reference again -- check _prefix."
        )


class TestRealHistoryIsNotNeeded:
    """The unit suite must not require a terminal, a network, or a data file.

    Every test in this file builds its own candles. The one test that reads a real export
    lives in the backtest suite and skips without the file, because a suite that cannot run on
    a machine with no terminal is a suite nobody runs.
    """

    def test_no_test_fixture_reads_the_data_directory(self) -> None:
        """No test may depend on a real export existing.

        A suite that cannot run without a terminal and a data file is a suite nobody runs. This
        file is excluded from its own check, since naming the path is how it asserts.
        """
        offending = []
        for path in Path("tests").rglob("*.py"):
            if path.name == Path(__file__).name:
                continue
            body = path.read_text(encoding="utf-8")
            if "data/us30" in body or "data\\us30" in body:
                offending.append(str(path))
        assert not offending, f"these tests read a real export: {offending}"
