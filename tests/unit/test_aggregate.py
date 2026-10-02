"""`aggregate()` must group by timestamp, and Phase 11 proved why the old way was wrong.

The bug
-------
`aggregate()` took every 15th bar and called it an M15 candle. That is only correct if the
series starts exactly on a 15-minute boundary and contains no gaps. Real data satisfies
neither: Alpari's US30 M1 export starts at 2026-09-02 11:42, closes for the weekend and for
an hour every day, and 30 022 bars over 30 days is 69.5% coverage.

The result was 1 822 of 2 001 "M15" candles that were not on a 15-minute boundary at all, and
49 of them straddled a session break -- one candle opening Friday 23:57 and closing Saturday
01:13. The M15 direction filter then read a series of candles that never existed, taking high
and low across the gap.

**Nothing crashed, and the backtest reported a confident number about it.** That is what makes
it worth three tests rather than one.

The second failure, worth recording
------------------------------------
The check that found the bug first *confirmed* the bug's absence. It compared our aggregated
candles to the terminal's own M15 bars and reported "175/175 agree, 100%". True -- and
worthless: the 179 bars that overlapped were precisely the ones whose open times matched, so
the comparison could only ever confirm the subset that was already aligned. A verification
that cannot fail is worse than none, and the third test here exists to prevent a repeat.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from stop_order_scalp.domain.models import Candle
from stop_order_scalp.domain.value_objects import Price
from stop_order_scalp.market_data.candles import aggregate


def bars(start: datetime, count: int, *, price: Decimal = Decimal("100")) -> list[Candle]:
    """``count`` consecutive M1 bars one minute apart."""
    return [
        Candle(
            open_time=start + timedelta(minutes=index),
            open=Price(price + index, 1),
            high=Price(price + index + Decimal("0.5"), 1),
            low=Price(price + index - Decimal("0.5"), 1),
            close=Price(price + index + Decimal("0.1"), 1),
            timeframe_seconds=60,
            timeframe="M1",
            volume=Decimal("1"),
            tick_volume=1,
            is_confirmed=True,
        )
        for index in range(count)
    ]


class TestAggregationIsAligned:
    def test_a_series_starting_off_boundary_still_aligns(self) -> None:
        """The exact shape of the real export: the first bar is at 11:42, not 11:45.

        Position-based grouping carries that offset into every candle it produces, forever.
        """
        start = datetime(2026, 9, 2, 11, 42, tzinfo=UTC)  # 3 minutes past the boundary
        result = aggregate(bars(start, 60), "M1", "M15")

        assert result, "nothing was aggregated"
        for candle in result:
            assert candle.open_time.minute % 15 == 0, (
                f"an M15 candle opened at {candle.open_time:%H:%M}, which is not a 15-minute "
                "boundary -- the series' starting offset leaked into the aggregation"
            )
            assert candle.open_time.second == 0

    def test_every_candle_opens_at_the_window_containing_its_bars(self) -> None:
        """A group must be the bars inside one window, labelled with that window's start.

        Written twice, because the first version of this test could not fail.

        It derived each candle's members from *the candle's own* ``open_time`` and checked the
        candle against them. Position-based grouping sets ``open_time`` to the first bar it
        happened to take, so on a contiguous series the members and the label agree and the
        test passes against the bug. It now compares against windows computed independently
        from the source timestamps, which is the only thing the old code cannot fake.
        """
        start = datetime(2026, 9, 2, 11, 42, tzinfo=UTC)  # 3 minutes past the boundary
        source = bars(start, 60)

        # The windows, derived from the source and from nothing else.
        expected: dict[datetime, list[Candle]] = {}
        for candle in source:
            minute = candle.open_time.minute - (candle.open_time.minute % 15)
            expected.setdefault(
                candle.open_time.replace(minute=minute, second=0), []
            ).append(candle)

        result = aggregate(source, "M1", "M15")
        produced = {candle.open_time: candle for candle in result}

        assert set(produced) == set(expected), (
            "the set of windows does not match the windows the source timestamps describe; "
            f"missing {sorted(set(expected) - set(produced))[:3]}, "
            f"unexpected {sorted(set(produced) - set(expected))[:3]}"
        )

        for window_start, members in expected.items():
            candle = produced[window_start]
            ordered = sorted(members, key=lambda m: m.open_time)
            assert candle.tick_volume == len(members), (
                f"the candle at {window_start:%H:%M} claims {candle.tick_volume} bars but "
                f"{len(members)} fall in its window"
            )
            assert candle.open == ordered[0].open
            assert candle.close == ordered[-1].close
            assert candle.high == max(m.high for m in members)
            assert candle.low == min(m.low for m in members)

    def test_a_gap_does_not_produce_one_candle_spanning_it(self) -> None:
        """A session break must not become a single candle from Friday into Saturday.

        The old grouping counted *bars*, so a 49-hour weekend gap was invisible to it and the
        last 7 bars of Friday were joined to the first 8 of Saturday. The high and low of that
        candle were taken across two days of price action, and the direction filter read it as
        a normal 15-minute bar.
        """
        friday = bars(datetime(2026, 9, 4, 23, 45, tzinfo=UTC), 10)  # to 23:54
        monday = bars(datetime(2026, 9, 7, 1, 0, tzinfo=UTC), 10)
        result = aggregate([*friday, *monday], "M1", "M15")

        for candle in result:
            span = candle.open_time + timedelta(minutes=15)
            assert not (candle.open_time < datetime(2026, 9, 5, tzinfo=UTC) and span > datetime(2026, 9, 7, tzinfo=UTC)), (
                f"a candle at {candle.open_time} spans the weekend gap"
            )
        # And the two sessions are separate candles, not one.
        assert len(result) >= 2

    def test_a_partial_window_is_kept_rather_than_dropped(self) -> None:
        """Fewer bars than a full period is a weaker fact, not a reason to say nothing.

        Dropping the partial window would hide the fact that the market was open. A candle
        built from the bars that exist is honest; an absent one is not.
        """
        start = datetime(2026, 9, 2, 11, 45, tzinfo=UTC)
        result = aggregate(bars(start, 4), "M1", "M15")
        assert len(result) == 1
        assert result[0].open_time == start


class TestTheComparisonThatLies:
    """A guard on the verification, because it lied once.

    Comparing aggregated candles to the terminal's own M15 bars by matching open times can only
    ever confirm the subset that is already aligned. On the real data it reported 100% agreement
    on 175 of 2 001 candles and that read as a pass.
    """

    def test_alignment_is_checked_over_every_candle_not_just_the_matching_ones(self) -> None:
        """This is the check that would have caught the bug the comparison endorsed.

        Count *all* produced candles, not the ones that happen to line up with a reference.
        A ratio computed over a self-selecting subset is not a measurement.
        """
        start = datetime(2026, 9, 2, 11, 42, tzinfo=UTC)  # off-boundary, as the export was
        result = aggregate(bars(start, 300), "M1", "M15")

        aligned = sum(1 for c in result if c.open_time.minute % 15 == 0)
        assert len(result) > 0
        assert aligned == len(result), (
            f"only {aligned} of {len(result)} candles are on a 15-minute boundary"
        )


class TestOrderingAndShape:
    def test_output_is_oldest_first(self) -> None:
        result = aggregate(bars(datetime(2026, 1, 1, tzinfo=UTC), 60), "M1", "M15")
        times = [c.open_time for c in result]
        assert times == sorted(times)

    def test_the_target_timeframe_is_stamped_on_the_result(self) -> None:
        result = aggregate(bars(datetime(2026, 1, 1, tzinfo=UTC), 30), "M1", "M15")
        for candle in result:
            assert candle.timeframe == "M15"
            assert candle.timeframe_seconds == 900

    def test_unsorted_input_is_ordered(self) -> None:
        """The loader sorts, but `aggregate` should not depend on a caller having done so."""
        source = bars(datetime(2026, 9, 2, 11, 42, tzinfo=UTC), 30)
        shuffled = [source[i] for i in (5, 0, 17, 2, 29, 1)]
        result = aggregate(shuffled, "M1", "M15")
        times = [c.open_time for c in result]
        assert times == sorted(times)

    def test_a_longer_target_than_source_returns_nothing(self) -> None:
        source = bars(datetime(2026, 9, 2, 11, 42, tzinfo=UTC), 30)
        assert aggregate(source, "M15", "M1") == []

    def test_empty_input_returns_nothing(self) -> None:
        assert aggregate([], "M1", "M15") == []

    @pytest.mark.parametrize("count", [15, 16, 29, 30, 31])
    def test_counts_around_a_boundary(self, count: int) -> None:
        """Every residue modulo 15 must produce aligned candles, whatever the count."""
        start = datetime(2026, 9, 2, 11, 42, tzinfo=UTC)
        result = aggregate(bars(start, count), "M1", "M15")
        for candle in result:
            assert candle.open_time.minute % 15 == 0
            assert candle.open_time.second == 0
