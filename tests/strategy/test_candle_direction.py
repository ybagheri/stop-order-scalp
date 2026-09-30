"""The M15 direction filter.

The rule under test is one sentence: *the last fully closed M15 candle's body decides the
direction*. Most of these tests exist because of the ways that sentence can be quietly
broken -- by reading a forming candle, by inventing a direction for a doji, or by treating
a missing candle as a neutral one.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from stop_order_scalp.domain.enums import Side
from stop_order_scalp.domain.exceptions import MarketDataError
from stop_order_scalp.domain.models import Candle
from stop_order_scalp.strategy.candle_direction import (
    DirectionDecision,
    DirectionVerdict,
    decide_direction,
    previous_direction_candle,
    select_direction_candle,
    summarise,
)
from strategy.conftest import (
    BASE,
    M15,
    at,
    bearish_m15,
    bullish_m15,
    doji_m15,
    last_m15_close,
    m1_series,
    m15_series,
)


class TestTheBaselineRule:
    def test_a_bullish_closed_candle_permits_buy(self) -> None:
        decision = decide_direction([bullish_m15(0)], reference=last_m15_close())
        assert decision.side is Side.SIDE_BUY
        assert decision.verdict == DirectionVerdict.ALLOW_BUY
        assert decision.allows_trading

    def test_a_bearish_closed_candle_permits_sell(self) -> None:
        decision = decide_direction([bearish_m15(0)], reference=last_m15_close())
        assert decision.side is Side.SIDE_SELL
        assert decision.verdict == DirectionVerdict.ALLOW_SELL
        assert decision.allows_trading

    def test_the_newest_closed_candle_decides(self) -> None:
        # The last one is bearish, so the answer must be SELL even though an earlier bar
        # was bullish.
        candles = [bullish_m15(0), bullish_m15(1), bearish_m15(2)]
        decision = decide_direction(candles, reference=last_m15_close(index=2))
        assert decision.side is Side.SIDE_SELL
        assert decision.candle is not None
        assert decision.candle.open_time == BASE + timedelta(seconds=1800)

    def test_input_order_does_not_matter(self) -> None:
        candles = [bullish_m15(0), bullish_m15(1)]
        reference = last_m15_close(index=1)
        forward = decide_direction(candles, reference=reference)
        backward = decide_direction(list(reversed(candles)), reference=reference)
        assert forward == backward

    def test_the_reason_names_the_candle_and_the_body(self) -> None:
        decision = decide_direction([bullish_m15(0)], reference=last_m15_close())
        assert "M15" in decision.reason
        assert "50.0" in decision.reason
        assert "BUY" in decision.reason


class TestClosedCandlesOnly:
    def test_a_forming_candle_is_not_consulted(self) -> None:
        # The bar opens at BASE and closes at BASE+15m. At BASE+1m it is still forming and
        # must not be read.
        decision = decide_direction([bullish_m15(0)], reference=at(minutes=1))
        assert decision.verdict == DirectionVerdict.NO_CANDLES
        assert decision.side is None
        assert not decision.allows_trading

    def test_the_boundary_is_inclusive(self) -> None:
        decision = decide_direction([bullish_m15(0)], reference=last_m15_close())
        assert decision.allows_trading

    def test_one_second_before_the_close_is_still_forming(self) -> None:
        decision = decide_direction([bullish_m15(0)], reference=last_m15_close() - timedelta(seconds=1))
        assert decision.verdict == DirectionVerdict.NO_CANDLES

    def test_a_forming_bar_cannot_flip_a_closed_decision(self) -> None:
        # A closed bullish bar, then a forming bearish one. The forming one must be ignored.
        forming = bearish_m15(1)
        decision = decide_direction(
            [bullish_m15(0), forming], reference=last_m15_close(index=1) - timedelta(minutes=1)
        )
        assert decision.side is Side.SIDE_BUY
        assert decision.candle is not None
        assert decision.candle.open_time == BASE


class TestTheDojiCase:
    def test_a_doji_permits_nothing(self) -> None:
        decision = decide_direction([doji_m15(0)], reference=last_m15_close())
        assert decision.side is None
        assert decision.verdict == DirectionVerdict.DOJI
        assert not decision.allows_trading

    def test_the_doji_verdict_is_not_indeterminate(self) -> None:
        # A doji is real information -- the market went nowhere. Calling it "could not
        # tell" would blur a quiet market into a broken feed.
        assert decide_direction([doji_m15(0)], reference=last_m15_close()).is_indeterminate is False

    def test_the_doji_candle_is_still_reported(self) -> None:
        decision = decide_direction([doji_m15(0)], reference=last_m15_close())
        assert decision.candle is not None
        assert decision.candle.is_doji
        assert "doji" in decision.reason

    def test_a_doji_does_not_fall_back_to_the_previous_candle(self) -> None:
        # Tempting and wrong: the previous bar was bullish, so "use the last one with a
        # direction" would trade. The rule is the *last* candle, full stop.
        candles = [bullish_m15(0), doji_m15(1)]
        decision = decide_direction(candles, reference=last_m15_close(index=1))
        assert decision.verdict == DirectionVerdict.DOJI
        assert decision.side is None


class TestNoCandles:
    def test_an_empty_series_is_not_an_error(self) -> None:
        decision = decide_direction([], reference=BASE)
        assert decision.verdict == DirectionVerdict.NO_CANDLES
        assert decision.candle is None
        assert decision.is_indeterminate

    def test_m1_candles_are_not_m15_candles(self) -> None:
        # Handing the filter the wrong timeframe is a caller bug; it must be reported as
        # "nothing to decide from", not silently answered from M1 bars.
        decision = decide_direction(m1_series(5), reference=at(minutes=10))
        assert decision.verdict == DirectionVerdict.NO_CANDLES
        assert "no M15" in decision.reason

    def test_the_reason_counts_how_many_were_forming(self) -> None:
        candles = m15_series(3)
        decision = decide_direction(candles, reference=at(minutes=1))
        assert "3" in decision.reason


class TestTimeframeHandling:
    def test_a_different_direction_timeframe_can_be_used(self) -> None:
        # The mechanism is general even though the baseline is M15. A bullish M1 bar read
        # as the direction timeframe must permit BUY.
        from strategy.conftest import m1

        decision = decide_direction(
            [m1(0, open_="40000.0", high="40060.0", low="39990.0", close="40050.0")],
            reference=at(minutes=1),
            timeframe="M1",
        )
        assert decision.allows_trading
        assert decision.side is Side.SIDE_BUY

    def test_a_bullish_m1_does_not_answer_an_m15_question(self) -> None:
        from strategy.conftest import m1

        decision = decide_direction(
            [m1(0, open_="40000.0", high="40060.0", low="39990.0", close="40050.0")],
            reference=at(minutes=1),
        )
        assert decision.verdict == DirectionVerdict.NO_CANDLES

    def test_an_unknown_timeframe_is_refused(self) -> None:
        with pytest.raises(MarketDataError, match="unknown timeframe"):
            decide_direction([bullish_m15(0)], reference=BASE, timeframe="M99")

    def test_a_monthly_timeframe_with_no_monthly_candles_decides_nothing(self) -> None:
        # MN1 is refused at *construction* by StopOrderStrategy, because a month has no
        # fixed number of seconds. Reaching the filter at all means the guard was bypassed,
        # and the honest answer with no matching candles is still "nothing to decide" --
        # not a crash from a function that is not the place for that policy.
        decision = decide_direction([bullish_m15(0)], reference=BASE, timeframe="MN1")
        assert decision.verdict == DirectionVerdict.NO_CANDLES
        assert decision.side is None

    def test_a_naive_reference_is_refused(self) -> None:
        naive = datetime(2026, 3, 12, 12, 0)
        with pytest.raises(MarketDataError, match="naive"):
            decide_direction([bullish_m15(0)], reference=naive)

    def test_an_mt5_timeframe_code_is_accepted(self) -> None:
        decision = decide_direction([bullish_m15(0)], reference=last_m15_close(), timeframe=15)
        assert decision.allows_trading


class TestAccessors:
    def test_select_direction_candle_returns_the_same_candle(self) -> None:
        candles = m15_series(2)
        reference = last_m15_close(index=1)
        assert (
            select_direction_candle(candles, reference=reference)
            == decide_direction(candles, reference=reference).candle
        )

    def test_select_direction_candle_is_none_when_none_exists(self) -> None:
        assert select_direction_candle([], reference=BASE) is None

    def test_the_previous_candle_is_available_for_later_filters(self) -> None:
        candles = [bullish_m15(0), bearish_m15(1)]
        previous = previous_direction_candle(candles, reference=last_m15_close(index=1))
        assert previous is not None
        assert previous.open_time == BASE

    def test_there_is_no_previous_candle_when_only_one_is_closed(self) -> None:
        assert previous_direction_candle([bullish_m15(0)], reference=last_m15_close()) is None


class TestSummary:
    def test_it_serialises_a_trading_verdict(self) -> None:
        payload = summarise(decide_direction([bullish_m15(0)], reference=last_m15_close()))
        assert payload["verdict"] == DirectionVerdict.ALLOW_BUY
        assert payload["allows_trading"] is True
        assert payload["candle"] is not None
        assert payload["candle"]["is_doji"] is False

    def test_it_serialises_an_empty_verdict_with_nulls(self) -> None:
        payload = summarise(decide_direction([], reference=BASE))
        assert payload["candle"] is None
        assert payload["side"] is None
        assert payload["is_indeterminate"] is True

    def test_the_string_form_names_the_candle(self) -> None:
        text = str(decide_direction([bullish_m15(0)], reference=last_m15_close()))
        assert "M15@" in text
        assert "allow_buy" in text

    def test_the_string_form_works_without_a_candle(self) -> None:
        assert "no_candles" in str(decide_direction([], reference=BASE))


class TestVerdictConsistency:
    def test_only_two_verdicts_authorise_trading(self) -> None:
        allowed = {DirectionVerdict.ALLOW_BUY, DirectionVerdict.ALLOW_SELL}
        for verdict in (
            DirectionVerdict.ALLOW_BUY,
            DirectionVerdict.ALLOW_SELL,
            DirectionVerdict.NO_CANDLES,
            DirectionVerdict.DOJI,
            DirectionVerdict.TIMEFRAME_MISMATCH,
        ):
            candle = bullish_m15(0)
            decision = _decision_with_verdict(verdict, candle)
            assert decision.allows_trading is (verdict in allowed)

    def test_an_authorising_verdict_always_carries_a_side(self) -> None:
        for candles, reference in (
            ([bullish_m15(0)], last_m15_close()),
            ([bearish_m15(0)], last_m15_close()),
        ):
            decision = decide_direction(candles, reference=reference)
            assert decision.allows_trading
            assert decision.side is not None


def _decision_with_verdict(verdict: str, candle: Candle) -> DirectionDecision:
    return DirectionDecision(
        side=Side.SIDE_BUY,
        verdict=verdict,
        candle=candle,
        reason="constructed for the consistency test",
        reference=BASE,
    )


def test_the_timeframe_constant_is_m15() -> None:
    # Stated as a test because "M15" appearing as a bare string somewhere is exactly the
    # kind of thing that drifts.
    assert M15 == "M15"
