"""Closed-candle selection, and the no-look-ahead guarantee.

The property under test throughout is one the specification cares about more than any
other: **appending future candles must not change a decision taken at time *t***. It is
stated as a `hypothesis` property rather than an example, because an example only proves
the function worked on the case somebody thought of.

Everything here runs without MetaTrader 5 installed, which is the point of the layering.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from stop_order_scalp.domain.exceptions import MarketDataError
from stop_order_scalp.domain.models import Candle
from stop_order_scalp.domain.value_objects import Price
from stop_order_scalp.market_data.candles import (
    freeze_closed_bars,
    require_closed_only,
    select_closed,
)
from stop_order_scalp.market_data.timeframes import Timeframe, floor_time, is_aligned

BASE = datetime(2026, 3, 12, 12, 0, 0, tzinfo=UTC)
M1 = Timeframe.M1
M15 = Timeframe.M15


def _series(
    count: int,
    *,
    timeframe: str = M1,
    start: datetime = BASE,
    price_base: int = 40000,
) -> list[Candle]:
    """``count`` consecutive bars starting at ``start``, oldest first."""
    seconds = 60 if timeframe == M1 else 900
    out: list[Candle] = []
    for index in range(count):
        moment = start + timedelta(seconds=seconds * index)
        offset = Decimal(index)
        out.append(
            Candle(
                open_time=moment,
                open=_price(price_base + offset),
                high=_price(price_base + 10 + offset),
                low=_price(price_base - 10 + offset),
                close=_price(price_base + 5 + offset),
                timeframe_seconds=seconds,
                timeframe=timeframe,
            )
        )
    return out


def _price(value: int | Decimal) -> Price:
    return Price.parse(str(value), 1)


def _continue(bars: list[Candle], extra: int, *, timeframe: str = M1) -> list[Candle]:
    """``extra`` bars immediately *after* ``bars``.

    Used to extend a series without repeating it, so a no-look-ahead test appends only
    genuinely later bars rather than colliding with the ones already there.
    """
    seconds = 60 if timeframe == M1 else 900
    start = bars[-1].open_time + timedelta(seconds=seconds)
    price_base = int(bars[-1].open.value)
    return _series(extra, timeframe=timeframe, start=start, price_base=price_base)


# =============================================================================
# The core rule
# =============================================================================


class TestFreezeClosedBars:
    def test_a_forming_bar_is_dropped(self) -> None:
        bars = _series(5)
        # At 12:02:30 the bar that opened at 12:02:00 closes at 12:03:00 and is still
        # forming, so only the two bars that closed before 12:02:30 survive.
        kept, report = freeze_closed_bars(bars, reference=BASE + timedelta(seconds=150))

        assert [c.open_time for c in kept] == [BASE, BASE + timedelta(seconds=60)]
        assert report.dropped_forming == 3
        assert report.returned == 2

    def test_the_boundary_is_inclusive(self) -> None:
        bars = _series(3)
        reference = BASE + timedelta(seconds=120)
        kept, _ = freeze_closed_bars(bars, reference=reference)
        # The bar opening at 12:01:00 closes exactly at the reference, so it counts.
        assert len(kept) == 2

    def test_one_second_earlier_drops_that_bar(self) -> None:
        bars = _series(3)
        kept, _ = freeze_closed_bars(bars, reference=BASE + timedelta(seconds=119))
        assert len(kept) == 1

    def test_an_empty_input_is_not_an_error(self) -> None:
        kept, report = freeze_closed_bars([], reference=BASE)
        assert kept == ()
        assert report.is_empty
        assert report.requested == 0

    def test_all_forming_yields_nothing_but_says_why(self) -> None:
        bars = _series(3)
        kept, report = freeze_closed_bars(bars, reference=BASE)
        assert kept == ()
        assert report.dropped_forming == 3
        assert report.saw_forming_bar
        # The *newest* forming bar is the one in progress, which is what an operator
        # wants to see named in the report.
        assert report.forming_open == BASE + timedelta(seconds=120)

    def test_the_result_is_oldest_first(self) -> None:
        bars = _series(4)
        kept, _ = freeze_closed_bars(bars, reference=BASE + timedelta(seconds=600))
        assert list(kept) == sorted(kept, key=lambda c: c.open_time)


class TestInputOrder:
    def test_newest_first_input_is_normalised(self) -> None:
        bars = _series(5)
        kept, report = freeze_closed_bars(
            list(reversed(bars)), reference=BASE + timedelta(seconds=600)
        )
        assert [c.open_time for c in kept] == [c.open_time for c in bars]
        assert report.reordered is True

    def test_already_sorted_input_is_not_reported_as_reordered(self) -> None:
        _kept, report = freeze_closed_bars(_series(5), reference=BASE + timedelta(seconds=600))
        assert report.reordered is False

    def test_ordering_the_result_is_independent_of_input_order(self) -> None:
        bars = _series(6)
        forward, _ = freeze_closed_bars(bars, reference=BASE + timedelta(seconds=900))
        backward, _ = freeze_closed_bars(
            list(reversed(bars)), reference=BASE + timedelta(seconds=900)
        )
        assert forward == backward


# =============================================================================
# The report
# =============================================================================


class TestFreezeReport:
    def test_it_records_the_boundaries_of_what_was_kept(self) -> None:
        _, report = freeze_closed_bars(_series(5), reference=BASE + timedelta(seconds=200))
        assert report.oldest_open == BASE
        assert report.newest_open == BASE + timedelta(seconds=120)
        # The newest bar the feed offered that was still forming.
        assert report.forming_open == BASE + timedelta(seconds=240)

    def test_the_reference_is_recorded(self) -> None:
        reference = BASE + timedelta(seconds=200)
        _, report = freeze_closed_bars(_series(5), reference=reference)
        assert report.reference == reference

    def test_it_serialises_for_the_audit_log(self) -> None:
        _, report = freeze_closed_bars(_series(3), reference=BASE)
        payload = report.to_dict()
        assert payload["requested"] == 3
        assert payload["returned"] == 0
        assert payload["reference"] == BASE.isoformat()
        assert "Traceback" not in str(report)

    def test_an_empty_report_serialises_with_nulls_not_a_crash(self) -> None:
        _, report = freeze_closed_bars([], reference=BASE)
        payload = report.to_dict()
        assert payload["oldest_open"] is None
        assert payload["newest_open"] is None
        assert payload["forming_open"] is None

    def test_requested_and_returned_always_add_up(self) -> None:
        bars = _series(7)
        _, report = freeze_closed_bars(bars, reference=BASE + timedelta(seconds=250))
        assert report.requested == report.returned + report.dropped_forming + report.dropped_unaligned


# =============================================================================
# Refusing bad input rather than guessing
# =============================================================================


class TestRefusals:
    def test_a_naive_reference_is_refused(self) -> None:
        with pytest.raises(MarketDataError, match="naive"):
            freeze_closed_bars(_series(3), reference=datetime(2026, 3, 12, 12, 0))

    def test_a_naive_reference_names_server_time(self) -> None:
        with pytest.raises(MarketDataError, match="server time"):
            freeze_closed_bars(_series(3), reference=datetime(2026, 3, 12, 12, 0))

    def test_a_duplicate_bar_is_refused(self) -> None:
        bars = _series(3)
        with pytest.raises(MarketDataError, match="duplicate"):
            freeze_closed_bars([*bars, bars[-1]], reference=BASE + timedelta(seconds=600))

    def test_a_mixed_timeframe_series_is_refused(self) -> None:
        # A caller that passed the wrong array must be told, not quietly handed M15 bars
        # when it asked for M1. The two segments start at different times so the refusal
        # is about the timeframe, not about a duplicate open time.
        later = BASE + timedelta(seconds=900)
        mixed = [*_series(2, timeframe=M1), *_series(2, timeframe=M15, start=later)]
        with pytest.raises(MarketDataError, match="expected M1"):
            freeze_closed_bars(mixed, reference=BASE + timedelta(seconds=3600), timeframe=M1)

    def test_a_length_that_contradicts_the_name_is_refused(self) -> None:
        bars = _series(2)
        broken = [
            Candle(
                open_time=c.open_time,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                timeframe_seconds=900,
                timeframe=M1,
            )
            for c in bars
        ]
        with pytest.raises(MarketDataError, match="seconds"):
            freeze_closed_bars(broken, reference=BASE + timedelta(seconds=3600), timeframe=M1)

    def test_no_timeframe_means_no_timeframe_check(self) -> None:
        # Omitting the timeframe is a legitimate choice: the caller only wants closure.
        later = BASE + timedelta(seconds=900)
        mixed = [*_series(2, timeframe=M1), *_series(2, timeframe=M15, start=later)]
        kept, _ = freeze_closed_bars(mixed, reference=BASE + timedelta(seconds=3600))
        assert len(kept) == 4

    def test_misaligned_bars_are_counted_not_silently_kept(self) -> None:
        offset = BASE + timedelta(seconds=30)
        bars = _series(3, start=offset)
        kept, report = freeze_closed_bars(bars, reference=BASE + timedelta(seconds=600), timeframe=M1)
        assert report.dropped_unaligned == 3
        assert kept == ()


# =============================================================================
# select_closed and require_closed_only
# =============================================================================


class TestSelectClosed:
    def test_it_returns_newest_first(self) -> None:
        newest = select_closed(_series(5), reference=BASE + timedelta(seconds=600))
        assert newest[0].open_time == BASE + timedelta(seconds=240)

    def test_the_limit_takes_the_newest(self) -> None:
        newest = select_closed(_series(5), reference=BASE + timedelta(seconds=600), limit=2)
        assert [c.open_time for c in newest] == [
            BASE + timedelta(seconds=240),
            BASE + timedelta(seconds=180),
        ]

    def test_a_non_positive_limit_is_refused(self) -> None:
        with pytest.raises(MarketDataError, match="limit must be positive"):
            select_closed(_series(3), reference=BASE, limit=0)


class TestRequireClosedOnly:
    def test_closed_only_passes(self) -> None:
        kept, _ = freeze_closed_bars(_series(5), reference=BASE + timedelta(seconds=600))
        require_closed_only(kept, reference=BASE + timedelta(seconds=600))

    def test_a_forming_bar_is_caught(self) -> None:
        bars = _series(3)
        with pytest.raises(MarketDataError, match="not closed"):
            require_closed_only(bars, reference=BASE)

    def test_the_error_names_the_offending_bar(self) -> None:
        with pytest.raises(MarketDataError, match="12:00"):
            require_closed_only(_series(1), reference=BASE)

    def test_an_empty_sequence_passes(self) -> None:
        require_closed_only([], reference=BASE)


# =============================================================================
# Timezones
# =============================================================================


class TestTimezoneHandling:
    def test_the_same_instant_in_another_offset_gives_the_same_answer(self) -> None:
        bars = _series(3)
        utc_reference = BASE + timedelta(seconds=150)
        # Asia/Tehran is UTC+3:30, a deliberately awkward offset.
        tehran = timezone(timedelta(hours=3, minutes=30))
        shifted = utc_reference.astimezone(tehran)

        from_utc, _ = freeze_closed_bars(bars, reference=utc_reference)
        from_tehran, _ = freeze_closed_bars(bars, reference=shifted)
        assert from_utc == from_tehran

    def test_a_bar_time_in_another_offset_still_compares_correctly(self) -> None:
        bars = _series(3)
        reference = BASE + timedelta(seconds=150)
        localised = [
            Candle(
                open_time=c.open_time.astimezone(timezone(timedelta(hours=3, minutes=30))),
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                timeframe_seconds=c.timeframe_seconds,
                timeframe=c.timeframe,
            )
            for c in bars
        ]
        kept, _ = freeze_closed_bars(localised, reference=reference, timeframe=M1)
        assert len(kept) == 2

    def test_alignment_is_evaluated_in_utc(self) -> None:
        # floor_time returns UTC. A moment written in Tehran offset and the same instant
        # written in UTC must floor to the same bar, because the arithmetic is on the
        # absolute instant, never on the wall-clock fields.
        tehran = timezone(timedelta(hours=3, minutes=30))
        moment = datetime(2026, 3, 12, 15, 0, 0, tzinfo=tehran)
        assert is_aligned(moment, M15)
        assert floor_time(moment, M15) == floor_time(moment.astimezone(UTC), M15)

    @pytest.mark.parametrize("offset_hours", [0, 1, 2, 3, 4, 5, 8])
    def test_minute_boundaries_coincide_for_common_broker_offsets(
        self, offset_hours: int
    ) -> None:
        """Why flooring in UTC is safe for the timeframes this strategy uses.

        MetaTrader 5 anchors bars to *server* time. Flooring in UTC only matches server-time
        flooring when the broker's UTC offset is a whole number of the timeframe. Every
        common broker offset -- 0, +1, +2, +3, +4, +5, +8 -- is a multiple of 15 minutes, so
        for M1 and M15 the two agree. The baseline strategy uses only M1 and M15, so this
        holds for it by construction rather than by luck.
        """
        offset = timezone(timedelta(hours=offset_hours))
        server_midnight = datetime(2026, 3, 12, 0, 0, tzinfo=offset)
        for timeframe in (M1, M15):
            assert is_aligned(server_midnight.astimezone(UTC), timeframe), (
                f"{timeframe} boundaries disagree for a UTC+{offset_hours} broker"
            )

    def test_daily_boundaries_do_not_coincide_and_that_is_known(self) -> None:
        """The documented limit of the above.

        A broker whose server day starts at a non-multiple of 24 hours in UTC -- UTC+5:30 is
        the real example -- has daily bars that UTC-flooring would misplace by 5.5 hours.
        The project does not use D1, and this test exists so that anyone who reaches for a
        daily timeframe sees the boundary rather than discovering it in a backtest.
        """
        kolkata = timezone(timedelta(hours=5, minutes=30))
        server_midnight = datetime(2026, 3, 12, 0, 0, tzinfo=kolkata)
        in_utc = server_midnight.astimezone(UTC)
        assert not is_aligned(in_utc, "D1")
        # M15 still agrees, which is why the strategy is safe.
        assert is_aligned(in_utc, M15)


# =============================================================================
# The no-look-ahead property
# =============================================================================


@st.composite
def _bars_and_reference(draw: st.DrawFn) -> tuple[list[Candle], datetime]:
    count = draw(st.integers(min_value=1, max_value=25))
    offset_seconds = draw(st.integers(min_value=0, max_value=25 * 60))
    step = draw(st.sampled_from([60, 900]))
    base = BASE
    bars: list[Candle] = []
    for index in range(count):
        moment = base + timedelta(seconds=step * index)
        bars.append(
            Candle(
                open_time=moment,
                open=_price(40000 + index),
                high=_price(40100 + index),
                low=_price(39900 + index),
                close=_price(40050 + index),
                timeframe_seconds=step,
                timeframe=M1 if step == 60 else M15,
            )
        )
    return bars, base + timedelta(seconds=offset_seconds)


class TestNoLookAhead:
    @given(_bars_and_reference())
    def test_appending_future_bars_cannot_change_the_answer(
        self, sample: tuple[list[Candle], datetime]
    ) -> None:
        bars, reference = sample
        partial, _ = freeze_closed_bars(bars, reference=reference)
        extended, _ = freeze_closed_bars(
            [*bars, *_continue(bars, 30, timeframe=bars[-1].timeframe)],
            reference=reference,
        )
        # The extended series may add bars that were *already closed* before the
        # reference; it must never remove or alter one that the partial series had.
        assert extended[: len(partial)] == partial

    @given(_bars_and_reference())
    def test_a_forming_bar_never_survives(
        self, sample: tuple[list[Candle], datetime]
    ) -> None:
        bars, reference = sample
        kept, _ = freeze_closed_bars(bars, reference=reference)
        require_closed_only(kept, reference=reference)

    @given(_bars_and_reference())
    def test_moving_the_reference_forward_never_removes_a_bar(
        self, sample: tuple[list[Candle], datetime]
    ) -> None:
        # Monotonicity: as time passes the kept set only grows. A bar that was closed does
        # not become unclosed.
        bars, reference = sample
        earlier, _ = freeze_closed_bars(bars, reference=reference)
        later, _ = freeze_closed_bars(bars, reference=reference + timedelta(hours=1))
        assert later[: len(earlier)] == earlier

    @given(_bars_and_reference())
    def test_the_same_input_always_gives_the_same_output(
        self, sample: tuple[list[Candle], datetime]
    ) -> None:
        bars, reference = sample
        first, first_report = freeze_closed_bars(bars, reference=reference)
        second, second_report = freeze_closed_bars(bars, reference=reference)
        assert first == second
        assert first_report == second_report


# =============================================================================
# Agreement with the independent Al Brooks implementation
# =============================================================================


class TestAlBrooksAgreement:
    """Decision 11: two independent implementations of the same rule must agree.

    Skipped cleanly when the optional extra is absent. A skip marker is acceptable here
    and only here, because ``albrooks`` is a genuinely optional dependency -- and the
    alternative, making it required, would put a third-party package in the path of every
    contributor for the sake of one cross-check.
    """

    def test_both_implementations_keep_the_same_bars(self) -> None:
        albrooks = pytest.importorskip("albrooks")
        del albrooks

        from albrooks.core.bars import freeze_closed_bars as theirs

        reference = BASE + timedelta(seconds=150)
        ours_bars = _series(5)
        kept, _ = freeze_closed_bars(ours_bars, reference=reference, timeframe=M1)

        bars = theirs([_as_bar(c) for c in ours_bars], 60, now=reference.timestamp())
        their_open_times = [bar.time for bar in bars]
        our_open_times = [c.open_time.timestamp() for c in kept]
        assert our_open_times == their_open_times


def _as_bar(candle: Candle) -> object:
    """A minimal stand-in exposing the epoch-second ``time`` the Al Brooks ``Bar`` wants."""
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Bar:
        time: float
        open: float
        high: float
        low: float
        close: float
        volume: int = 0
        index: int = 0

    return Bar(
        time=float(candle.open_time.timestamp()),
        open=float(candle.open.value),
        high=float(candle.high.value),
        low=float(candle.low.value),
        close=float(candle.close.value),
    )


def test_candles_module_has_no_broker_dependency() -> None:
    # The freeze logic must stay pure. A module that could reach a terminal would make
    # every one of the properties above dependent on a running MetaTrader 5.
    source = (
        Path("src/stop_order_scalp/market_data/candles.py").read_text(encoding="utf-8")
    )
    assert "MetaTrader5" not in source
