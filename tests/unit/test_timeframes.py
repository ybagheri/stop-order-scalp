"""Timeframes, candle boundaries and the closed/forming distinction.

The strategy's correctness depends on knowing exactly which candles were available at
decision time. These tests are where that knowledge is pinned down.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from conftest import CandleFactory
from stop_order_scalp.domain.exceptions import MarketDataError
from stop_order_scalp.market_data.timeframes import (
    MT5_ENCODED,
    PERIOD_SECONDS,
    Timeframe,
    align_to,
    canonical_name,
    floor_time,
    is_aligned,
    next_close,
    period_seconds,
    to_mt5,
)


class TestEncoding:
    def test_minutes_are_literal(self) -> None:
        # The MetaTrader 5 encoding for minute timeframes is the minute count itself.
        assert to_mt5(Timeframe.M1) == 1
        assert to_mt5(Timeframe.M15) == 15

    def test_hours_are_offset_by_16384(self) -> None:
        assert to_mt5(Timeframe.H1) == 16385
        assert to_mt5(Timeframe.H4) == 16388

    def test_day_week_and_month(self) -> None:
        # PERIOD_D1 is 16408. It is NOT 16384 + 1, which is PERIOD_H1 -- a real and easy
        # trap in MetaTrader 5's encoding.
        assert to_mt5(Timeframe.D1) == 16408
        assert to_mt5(Timeframe.D1) != to_mt5(Timeframe.H1)
        assert to_mt5(Timeframe.W1) == 32769
        assert to_mt5(Timeframe.MN1) == 49153

    def test_no_two_timeframes_share_an_mt5_code(self) -> None:
        codes = list(MT5_ENCODED.values())
        assert len(codes) == len(set(codes))

    def test_round_trip_from_code_to_name_to_code(self) -> None:
        for name, code in MT5_ENCODED.items():
            assert canonical_name(code) == name
            assert to_mt5(canonical_name(code)) == code

    def test_names_are_case_insensitive(self) -> None:
        assert canonical_name("m15") == "M15"
        assert canonical_name("  M15  ") == "M15"

    def test_unknown_name_is_an_error_not_a_default(self) -> None:
        # A silent fallback to M1 would make a typo look like a working system.
        with pytest.raises(MarketDataError, match="unknown timeframe"):
            canonical_name("M7")

    def test_unknown_code_is_an_error(self) -> None:
        with pytest.raises(MarketDataError, match="unknown MetaTrader 5 timeframe code"):
            canonical_name(99999)

    def test_a_bool_is_not_a_timeframe(self) -> None:
        with pytest.raises(MarketDataError):
            canonical_name(True)


class TestPeriods:
    def test_m15_is_fifteen_minutes(self) -> None:
        assert period_seconds("M15") == 900

    def test_m1_is_sixty_seconds(self) -> None:
        assert period_seconds("M1") == 60

    def test_every_name_except_mn1_has_a_period(self) -> None:
        for name in MT5_ENCODED:
            if name == Timeframe.MN1:
                continue
            assert period_seconds(name) > 0

    def test_month_has_no_fixed_period(self) -> None:
        # A month is not a fixed number of seconds. Computing one would put a look-ahead
        # bug in through the back door.
        with pytest.raises(MarketDataError, match="no fixed period"):
            period_seconds(Timeframe.MN1)


class TestBoundaries:
    @pytest.mark.parametrize(
        ("moment", "timeframe", "expected"),
        [
            (datetime(2026, 3, 12, 12, 34, 7, tzinfo=UTC), "M1", datetime(2026, 3, 12, 12, 34, 0, tzinfo=UTC)),
            (datetime(2026, 3, 12, 12, 34, 7, tzinfo=UTC), "M15", datetime(2026, 3, 12, 12, 30, 0, tzinfo=UTC)),
            (datetime(2026, 3, 12, 12, 34, 7, tzinfo=UTC), "H1", datetime(2026, 3, 12, 12, 0, 0, tzinfo=UTC)),
            (datetime(2026, 3, 12, 12, 34, 7, tzinfo=UTC), "D1", datetime(2026, 3, 12, 0, 0, 0, tzinfo=UTC)),
        ],
    )
    def test_floor_time(self, moment: datetime, timeframe: str, expected: datetime) -> None:
        assert floor_time(moment, timeframe) == expected

    def test_floor_time_rejects_a_naive_datetime(self) -> None:
        # A naive datetime means the caller lost the offset, and the floor would silently
        # use the local machine's timezone instead of the broker's.
        with pytest.raises(MarketDataError, match="naive datetime"):
            floor_time(datetime(2026, 3, 12, 12, 34, 7), "M1")

    def test_is_aligned(self) -> None:
        assert is_aligned(datetime(2026, 3, 12, 12, 30, tzinfo=UTC), "M15")
        assert not is_aligned(datetime(2026, 3, 12, 12, 31, tzinfo=UTC), "M15")

    def test_align_to_is_floor_time_under_a_readable_name(self) -> None:
        moment = datetime(2026, 3, 12, 12, 34, 7, tzinfo=UTC)
        assert align_to(moment, "M15") == floor_time(moment, "M15")

    def test_next_close_is_exactly_one_period_later(self) -> None:
        opened = datetime(2026, 3, 12, 12, 30, tzinfo=UTC)
        assert next_close(opened, "M15") == opened + timedelta(minutes=15)

    def test_boundary_arithmetic_round_trips(self) -> None:
        moment = datetime(2026, 3, 12, 12, 34, 7, tzinfo=UTC)
        for name in PERIOD_SECONDS:
            assert is_aligned(floor_time(moment, name), name)


class TestCandleClosure:
    def test_a_bar_is_closed_once_its_close_time_has_passed(self, candle_factory: CandleFactory) -> None:
        from datetime import timedelta as td

        opened = datetime(2026, 3, 12, 12, 30, tzinfo=UTC)
        candle = candle_factory(opened, "40010.0", "39990.0", "40000.0")

        assert not candle.is_closed_at(opened)
        assert not candle.is_closed_at(opened + td(seconds=59))
        # The boundary is inclusive: at exactly the close time the bar is complete.
        assert candle.is_closed_at(opened + td(seconds=60))

    def test_close_time_is_open_time_plus_the_period(self, candle_factory: CandleFactory) -> None:
        opened = datetime(2026, 3, 12, 12, 30, tzinfo=UTC)
        candle = candle_factory(opened, "40010.0", "39990.0", "40000.0", timeframe_seconds=900)
        assert candle.close_time == opened + timedelta(minutes=15)

    def test_closure_does_not_consult_the_wall_clock(self, candle_factory: CandleFactory) -> None:
        # Two calls at the same reference moment must agree forever. A candle that could
        # change its answer because the machine's clock moved would be untestable.
        opened = datetime(2026, 3, 12, 12, 30, tzinfo=UTC)
        candle = candle_factory(opened, "40010.0", "39990.0", "40000.0")
        reference = opened + timedelta(seconds=30)
        assert candle.is_closed_at(reference) == candle.is_closed_at(reference)

    def test_a_misaligned_feed_is_detectable(self, candle_factory: CandleFactory) -> None:
        # Candle open times that are not on a boundary mean the feed's boundaries differ
        # from ours, shifting every "is this closed" answer.
        misaligned = candle_factory(datetime(2026, 3, 12, 12, 30, 30, tzinfo=UTC), "40010.0", "39990.0", "40000.0")
        aligned = candle_factory(datetime(2026, 3, 12, 12, 30, 0, tzinfo=UTC), "40010.0", "39990.0", "40000.0")
        assert not misaligned.close_time_is_floor()
        assert aligned.close_time_is_floor()

    def test_a_m15_candle_is_closed_fifteen_minutes_after_it_opens(self, candle_factory: CandleFactory) -> None:
        opened = datetime(2026, 3, 12, 12, 30, tzinfo=UTC)
        candle = candle_factory(
            opened, "40010.0", "39990.0", "40000.0", timeframe="M15", timeframe_seconds=900
        )
        assert not candle.is_closed_at(opened + timedelta(minutes=14, seconds=59))
        assert candle.is_closed_at(opened + timedelta(minutes=15))
