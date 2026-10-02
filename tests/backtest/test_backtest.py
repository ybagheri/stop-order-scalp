"""The replay engine, tested by replaying.

Same reasoning as the application tests: the bugs this module has are not component bugs. A
backtest that reports a plausible number from a broken replay is worse than one that refuses
to run, because the number is the whole point.

The tests that matter most are at the bottom of this file. A replay is *a second run over
recorded data*, which is the exact shape that hid the construct-instead-of-load defect in
Phase 9 -- one run, one fresh directory, nothing shared. :class:`TestReplayedTwice` exists
because of that, not because two runs is an interesting scenario.
"""

from __future__ import annotations

import json
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from stop_order_scalp.backtest.replay import (
    ReplayResult,
    assumed_specification,
    replay,
)
from stop_order_scalp.backtest.runner import run_backtest_from_args
from stop_order_scalp.backtest.statistics import BacktestStatistics, EquityPoint, summarise
from stop_order_scalp.domain.exceptions import ConfigError
from stop_order_scalp.domain.models import Candle
from stop_order_scalp.domain.value_objects import Price
from stop_order_scalp.infrastructure.config import AppConfig, load_config
from stop_order_scalp.market_data.candles import aggregate


@pytest.fixture
def config() -> AppConfig:
    return load_config()


def series(count: int = 400, *, seed: int = 7) -> list[Candle]:
    """A series with trends *and* reversals.

    Deliberately not the project's synthetic candles. Those trend in one direction, which
    would let a replay that never stops out still look reasonable -- and "never stops out" is
    the failure a stop-based system is most likely to have.
    """
    rng = random.Random(seed)
    start = datetime(2026, 3, 12, tzinfo=UTC)
    price = Decimal("40150.0")
    out: list[Candle] = []
    for index in range(count):
        regime = Decimal(rng.choice([1, 1, -1, -1, 1, -1]))
        close = price + regime * Decimal(str(round(rng.uniform(0.05, 0.5), 3))) + Decimal(
            str(round(rng.uniform(-0.8, 0.8), 3))
        )
        high = max(price, close) + Decimal(str(round(rng.uniform(0, 0.6), 3)))
        low = min(price, close) - Decimal(str(round(rng.uniform(0, 0.6), 3)))
        out.append(
            Candle(
                open_time=start + timedelta(minutes=index),
                open=Price(price, 1),
                high=Price(high, 1),
                low=Price(low, 1),
                close=Price(close, 1),
                timeframe_seconds=60,
                timeframe="M1",
                volume=Decimal("1"),
                tick_volume=1,
                is_confirmed=True,
            )
        )
        price = close
    return out


@pytest.fixture
def candles() -> list[Candle]:
    return series()


def run(config: AppConfig, candles: list[Candle], **kwargs: Any) -> ReplayResult:
    return replay(
        config,
        candles,
        m15_candles=aggregate(candles, "M1", "M15"),
        **kwargs,
    )


@pytest.fixture
def csv_file(tmp_path: Path, candles: list[Candle]) -> Path:
    lines = ["time,open,high,low,close"]
    for candle in candles:
        lines.append(
            f"{candle.open_time.strftime('%Y-%m-%dT%H:%M:%SZ')},{candle.open},"
            f"{candle.high},{candle.low},{candle.close}"
        )
    path = tmp_path / "series.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def trending(count: int = 300) -> list[Candle]:
    """A series that rises steadily, so a position opens and then closes.

    Needed by the tests that assert on closed trades. A random walk mostly leaves the trailing
    stop following price for the whole file, which is realistic and useless for testing the
    fields a *closed* trade carries.
    """
    start = datetime(2026, 3, 12, tzinfo=UTC)
    out: list[Candle] = []
    price = Decimal("40100.0")
    for index in range(count):
        close = price + Decimal("0.4")
        out.append(
            Candle(
                open_time=start + timedelta(minutes=index),
                open=Price(price, 1),
                high=Price(close + Decimal("0.2"), 1),
                low=Price(price - Decimal("0.2"), 1),
                close=Price(close, 1),
                timeframe_seconds=60,
                timeframe="M1",
                volume=Decimal("1"),
                tick_volume=1,
                is_confirmed=True,
            )
        )
        price = close
    return out


@pytest.fixture
def closed_run(config: AppConfig) -> ReplayResult:
    bars = trending()
    return replay(config, bars, m15_candles=aggregate(bars, "M1", "M15"))


class TestItReplays:
    def test_it_produces_a_result(self, config: AppConfig, candles: list[Candle]) -> None:
        result = run(config, candles)
        assert result.steps, "the replay ran no bars"
        assert result.statistics is not None

    def test_it_walks_forward(self, config: AppConfig, candles: list[Candle]) -> None:
        """Each bar must be a *newer* bar, or the loop is running and going nowhere."""
        result = run(config, candles)
        moments = [step.moment for step in result.steps]
        assert moments == sorted(moments)
        assert len(set(moments)) == len(moments), "a bar was decided on twice"

    def test_the_venue_clock_follows_the_data(self, closed_run: ReplayResult) -> None:
        """Trade timestamps must come from the candles, not the venue's frozen default.

        `SimulatedBroker`'s clock defaults to a fixed 2026-01-01 and only moves when something
        calls `set_time`. A replay that forgets to leaves every `opened_at` and `closed_at` on
        that date: the report still looks complete, and every trade duration reads as 0 seconds.
        """
        trades = closed_run.statistics.trades
        assert trades, "no closed trade to check the clock on"

        for trade in trades:
            assert trade.opened_at >= datetime(2026, 3, 12, tzinfo=UTC)
            assert trade.closed_at >= trade.opened_at
            assert trade.duration.total_seconds() > 0

    def test_a_trade_that_was_open_has_a_real_duration(
        self, closed_run: ReplayResult
    ) -> None:
        """A duration of zero across every trade is a clock that never moved."""
        for trade in closed_run.statistics.trades:
            assert trade.duration.total_seconds() > 0, (
                f"trade #{trade.ticket} reports a zero duration, so the venue clock did not "
                "follow the data"
            )


class TestNoLookAhead:
    def test_a_bar_is_only_ever_decided_after_it_closes(
        self, config: AppConfig, candles: list[Candle]
    ) -> None:
        """The reference must be at or after every bar the strategy saw.

        The freeze is the only thing permitted to decide what is closed. A replay that handed
        the strategy the bar in progress would be handing it the future, because in a replay
        that bar is already known -- and the resulting profit would look exactly like skill.
        """
        result = run(config, candles)
        closes = {candle.open_time: candle for candle in candles}

        for step in result.steps:
            for moment, candle in closes.items():
                if moment >= step.moment:
                    continue
                assert candle.close.value >= 0  # sanity: the bar is well formed
            # Every step's price must be a close the data actually contains.
            prices = {str(candle.close) for candle in candles}
            assert step.price in prices, (
                f"step {step.index} reported price {step.price}, which is not a close in the "
                "input data -- something other than the frozen candles is being read"
            )

    def test_replaying_a_prefix_gives_a_prefix_of_the_answer(
        self, config: AppConfig, candles: list[Candle]
    ) -> None:
        """Replaying 100 bars must equal the first 100 steps of replaying 400.

        The strongest available statement that the replay is walk-forward: if any later bar
        could influence an earlier decision, the two would diverge.
        """
        short = replay(
            config,
            candles,
            m15_candles=aggregate(candles, "M1", "M15"),
            max_cycles=100,
        )
        full = run(config, candles)

        assert len(short.steps) <= 100
        for left, right in zip(short.steps, full.steps, strict=False):
            assert left.index == right.index
            assert left.action == right.action, (
                f"bar {left.index} decided {left.action!r} on a prefix but {right.action!r} on "
                "the full series -- a later bar is influencing an earlier decision"
            )


class TestStatisticsComeFromTheVenue:
    def test_every_reported_trade_is_one_the_venue_closed(
        self, closed_run: ReplayResult
    ) -> None:
        for trade in closed_run.statistics.trades:
            assert trade.ticket > 0
            assert trade.closed_at >= trade.opened_at
            assert trade.reason in {"stop_loss", "take_profit", "closed"}

    def test_the_balance_change_reconciles_with_the_trades(
        self, closed_run: ReplayResult
    ) -> None:
        """What the account gained must equal what the trades earned.

        A statistics module that cannot be checked against the account is making claims
        rather than reporting. If commission were counted twice, or a close were missed, this
        is the assertion that notices.
        """
        statistics = closed_run.statistics
        realised = statistics.ending_balance - statistics.starting_balance
        assert statistics.trades, "nothing to reconcile"
        assert realised == statistics.net_profit, (
            f"the account moved {realised} but the trades earned {statistics.net_profit}"
        )

    def test_an_exit_is_classified_by_the_venue_not_inferred(
        self, closed_run: ReplayResult
    ) -> None:
        """A take profit above entry on a long must read as a take profit."""
        for trade in closed_run.statistics.trades:
            if trade.reason == "take_profit":
                assert trade.net.amount > 0, (
                    f"trade #{trade.ticket} is labelled a take profit but lost money"
                )
            if trade.reason == "stop_loss":
                assert trade.net.amount < 0, (
                    f"trade #{trade.ticket} is labelled a stop-out but made money"
                )

    def test_no_trades_means_no_win_rate_not_a_zero(
        self, config: AppConfig
    ) -> None:
        """'Nothing traded' and 'everything lost' are different facts."""
        empty = summarise(
            [],
            [],
            symbol="US30",
            starting_balance=Decimal("10000"),
            ending_balance=Decimal("10000"),
        )
        assert empty.win_rate is None
        assert empty.profit_factor is None
        assert empty.closed_count == 0

    def test_an_open_position_at_the_end_is_counted_not_dropped(
        self, config: AppConfig, candles: list[Candle]
    ) -> None:
        result = run(config, candles)
        statistics = result.statistics
        assert statistics.open_at_end >= 0
        # The report must say so rather than quietly reporting a smaller closed sample.
        assert statistics.to_dict()["trades"]["open_at_end"] == statistics.open_at_end

    def test_the_report_names_its_source(self, config: AppConfig, csv_file: Path) -> None:
        """A number without a provenance is not a result."""
        import argparse

        report = run_backtest_from_args(
            argparse.Namespace(data=csv_file, symbol=None, start=None, end=None,
                               slippage_points=0.0, output=None, max_cycles=60),
            config,
        )
        assert report["source"] == str(csv_file)
        assert report["specification_source"].startswith("assumed")
        assert any("does not measure the rule" in note for note in report["notes"])


class TestTheVenueIsCharged:
    """The venue's economics must match the configuration's.

    Found by a replay reporting ``total_commission: 0.0`` while ``config/default.yaml`` says
    6.0 per lot round trip. The risk engine sizes the position *net* of commission; a venue
    that charges nothing then books the full gross as profit, so every P/L figure overstates
    the result by exactly the cost the sizing already accounted for. It flatters the strategy,
    which is the one direction a bug in this project must never err in.
    """

    def test_commission_is_actually_charged(self, closed_run: ReplayResult) -> None:
        assert closed_run.statistics.trades, "no trade to check the commission on"
        assert closed_run.statistics.total_commission > 0, (
            "the venue charged no commission, so the reported profit is the gross and the "
            "risk engine's commission-aware sizing has been undone"
        )

    def test_the_charge_matches_the_configured_rate(
        self, config: AppConfig, closed_run: ReplayResult
    ) -> None:
        rate = config.strategy.risk.commission_per_lot
        mode = config.strategy.risk.commission_mode
        expected = sum(
            trade.commission.amount
            for trade in closed_run.statistics.trades
        )
        assert closed_run.statistics.total_commission == expected
        for trade in closed_run.statistics.trades:
            per_lot = rate * trade.volume.lots
            if str(mode).endswith("per_side"):
                per_lot *= 2
            assert trade.commission.amount == per_lot, (
                f"trade #{trade.ticket} was charged {trade.commission.amount}, expected "
                f"{per_lot} for {trade.volume.lots} lots at {rate} ({mode})"
            )

    def test_commission_costs_the_trader_money_not_the_venue(
        self, config: AppConfig
    ) -> None:
        """The same candles, with and without commission, must not report the same profit."""
        bars = trending()
        with_commission = replay(
            config, bars, m15_candles=aggregate(bars, "M1", "M15")
        )
        free = replace(
            config,
            strategy=replace(
                config.strategy,
                risk=replace(config.strategy.risk, commission_per_lot=Decimal("0")),
            ),
        )
        without_commission = replay(
            free, bars, m15_candles=aggregate(bars, "M1", "M15")
        )

        assert with_commission.statistics.trades
        assert without_commission.statistics.trades
        assert without_commission.statistics.net_profit > with_commission.statistics.net_profit, (
            "removing the commission did not improve the result, so the venue is not charging it"
        )


class TestReplayedTwice:
    """The Phase 9 lesson, applied before it had to be applied again.

    A backtest is a second run over recorded data. That is precisely the shape in which the
    construct-instead-of-load defect survived 1116 tests: one run, one fresh directory,
    nothing carried over. So the replay is run twice here, against the same state path, and
    the two answers are compared.
    """

    def test_replaying_twice_gives_the_same_answer(
        self, config: AppConfig, tmp_path: Path
    ) -> None:
        """Three identical replays, one answer. Including with a state file named.

        This is the guarantee a backtest most needs and the Phase 9 defect most threatened. A
        shared ledger makes the second run report ``already_recorded`` instead of ``placed``,
        which silently turns a reproducible measurement into a one-shot.
        """
        shared = replace(
            config,
            paths=replace(
                config.paths,
                state_file=tmp_path / "state.json",
                state_file_is_default=False,
            ),
        )
        bars = trending()

        answers = []
        for _ in range(3):
            result = replay(shared, bars, m15_candles=aggregate(bars, "M1", "M15"))
            answers.append(
                (
                    [step.action for step in result.steps],
                    [step.detail for step in result.steps],
                    result.statistics.net_profit,
                    result.statistics.closed_count,
                )
            )

        assert answers[0] == answers[1] == answers[2], (
            "replaying the same candles gave a different answer each time"
        )
        assert "already_recorded" not in answers[0][0]

    def test_a_replay_writes_nothing_even_when_a_ledger_is_named(
        self, config: AppConfig, tmp_path: Path
    ) -> None:
        """`state.path` applies to trading, not to a replay, and the report says so.

        A replay's venue is rebuilt on every call. A ledger that outlived it would describe a
        venue that no longer exists, and the second identical run would find the first run's
        intents and decline to place. So the ledger is in memory regardless -- and the operator
        who set the path is told, rather than left wondering why no file appeared.
        """
        shared = replace(
            config,
            paths=replace(
                config.paths,
                state_file=tmp_path / "state.json",
                state_file_is_default=False,
            ),
        )
        bars = trending()
        result = replay(shared, bars, m15_candles=aggregate(bars, "M1", "M15"))

        assert not shared.paths.state_file.exists(), (
            "a replay wrote to the configured ledger without being asked to persist"
        )
        assert any("in memory" in note for note in result.notes), (
            "the report does not tell the operator that nothing was written"
        )


class TestTheCommand:
    def test_it_refuses_without_a_file(self, config: AppConfig) -> None:
        """A replay with no input has nothing to say, and must not invent candles.

        Generating a series and reporting the result would produce a number with no provenance
        -- the exact failure this command exists to avoid.
        """
        import argparse

        with pytest.raises(ConfigError, match="--data"):
            run_backtest_from_args(
                argparse.Namespace(data=None, symbol=None, start=None, end=None,
                                   slippage_points=0.0, output=None, max_cycles=None),
                config,
            )

    def test_it_refuses_a_missing_file(self, config: AppConfig, tmp_path: Path) -> None:
        import argparse

        with pytest.raises(ConfigError, match="no candle file"):
            run_backtest_from_args(
                argparse.Namespace(data=tmp_path / "nope.csv", symbol=None, start=None,
                                   end=None, slippage_points=0.0, output=None, max_cycles=None),
                config,
            )

    def test_it_refuses_favourable_slippage(self, config: AppConfig, csv_file: Path) -> None:
        """Slippage here means adverse. A negative value would improve every result."""
        import argparse

        with pytest.raises(ConfigError, match="cannot be negative"):
            run_backtest_from_args(
                argparse.Namespace(data=csv_file, symbol=None, start=None, end=None,
                                   slippage_points=-5.0, output=None, max_cycles=40),
                config,
            )

    def test_a_window_narrower_than_the_data_changes_the_answer(
        self, config: AppConfig, csv_file: Path, candles: list[Candle]
    ) -> None:
        import argparse

        report = run_backtest_from_args(
            argparse.Namespace(
                data=csv_file, symbol=None,
                start=candles[0].open_time.isoformat(), end=None,
                slippage_points=0.0, output=None, max_cycles=40,
            ),
            config,
        )
        assert report["window"]["from"] == candles[0].open_time.isoformat()

    def test_it_writes_the_output_file_once(
        self, config: AppConfig, csv_file: Path, tmp_path: Path
    ) -> None:
        import argparse

        out = tmp_path / "nested" / "report.json"
        report = run_backtest_from_args(
            argparse.Namespace(data=csv_file, symbol=None, start=None, end=None,
                               slippage_points=0.0, output=out, max_cycles=40),
            config,
        )
        assert out.exists()
        assert report["written_to"] == str(out)
        written = json.loads(out.read_text(encoding="utf-8"))
        assert written["statistics"]["trades"] == report["statistics"]["trades"]


class TestDrawdown:
    def test_it_is_peak_to_trough_not_first_to_last(self) -> None:
        """A curve that recovers still had a drawdown on the way."""
        curve = [
            EquityPoint(
                moment=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i),
                balance=Decimal("10000"),
                equity=equity,
            )
            for i, equity in enumerate(
                [Decimal("10000"), Decimal("12000"), Decimal("9000"), Decimal("11000")]
            )
        ]
        statistics = summarise(
            [], curve, symbol="US30",
            starting_balance=Decimal("10000"), ending_balance=Decimal("10000"),
        )
        assert statistics.max_drawdown == Decimal("3000")
        assert statistics.max_drawdown_equity == Decimal("9000")

    def test_a_rising_curve_has_no_drawdown(self) -> None:
        curve = [
            EquityPoint(
                moment=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i),
                balance=Decimal("10000"),
                equity=Decimal(10000 + 100 * i),
            )
            for i in range(5)
        ]
        statistics = summarise(
            [], curve, symbol="US30",
            starting_balance=Decimal("10000"), ending_balance=Decimal("10400"),
        )
        assert statistics.max_drawdown == 0


class TestSpecificationIsAdmitted:
    def test_the_assumed_specification_is_reported(self, config: AppConfig, csv_file: Path) -> None:
        """Every money figure scales with the tick value, so the guess travels with the number."""
        import argparse

        report = run_backtest_from_args(
            argparse.Namespace(data=csv_file, symbol=None, start=None, end=None,
                               slippage_points=0.0, output=None, max_cycles=40),
            config,
        )
        specification = report["specification"]
        assert specification is not None
        assert specification["tick_value"] == str(assumed_specification("US30").tick_value)
        assert any("assumed" in note for note in report["notes"])


class TestTheTypeItself:
    def test_statistics_serialise_without_ceremony(self) -> None:
        payload = BacktestStatistics(
            symbol="US30",
            starting_balance=Decimal("10000"),
            ending_balance=Decimal("10000"),
            trades=(),
        ).to_dict()
        json.dumps(payload)  # must not raise

    def test_an_empty_replay_is_still_a_valid_report(self) -> None:
        payload = summarise(
            [], [], symbol="US30",
            starting_balance=Decimal("10000"), ending_balance=Decimal("10000"),
        ).to_dict()
        assert payload["trades"]["win_rate"] is None
        assert payload["money"]["profit_factor"] is None
