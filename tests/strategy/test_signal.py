"""The decision, and the property the whole project rests on.

The headline test here is :class:`TestNoLookAhead`. Everything else establishes that the
decision is built from the right candles; this class establishes that it is built from
*only* the right candles, which is a different and much stronger claim.

A backtest that peeks looks profitable. That is the whole danger, and it is invisible in
the output, so it is stated as a property over generated series rather than as an example.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import TypedDict

import pytest
from hypothesis import given
from hypothesis import strategies as st

from stop_order_scalp.domain.enums import OrderKind, Side
from stop_order_scalp.domain.exceptions import InstrumentNotAllowedError
from stop_order_scalp.domain.models import Candle, InstrumentPolicy
from stop_order_scalp.domain.value_objects import SymbolSpecification
from stop_order_scalp.infrastructure.config import EntrySettings
from stop_order_scalp.strategy.signal import (
    NoTrade,
    NoTradeReason,
    TradeDecision,
    build_signal,
    check_instrument,
    evaluate,
    no_trade,
    select_entry_candle,
)
from strategy.conftest import (
    BASE,
    M1,
    M15,
    at,
    bearish_m15,
    bullish_m15,
    doji_m15,
    last_m1_close,
    last_m15_close,
    m1,
    m1_series,
    m15_series,
    price,
)


class _Case(TypedDict):
    """The full keyword set :func:`evaluate` takes.

    A ``TypedDict`` rather than ``dict[str, object]`` so that ``evaluate(**case)`` is
    type-checked. Splatting a plain dict into keyword-only parameters defeats the checker
    entirely, and these tests are about a decision that must be built from the right inputs
    -- a call site the compiler cannot see is exactly the wrong place to lose that.
    """

    symbol: str
    m15_candles: list[Candle]
    m1_candles: list[Candle]
    reference: datetime
    entry: EntrySettings
    specification: SymbolSpecification
    policy: InstrumentPolicy | None


def _ready(entry: EntrySettings) -> _Case:
    """A context where every check passes, so each test can break exactly one thing.

    The reference is deliberately *after* the M15 bar closes: a reference before it would
    make every test here about a forming direction candle rather than about the thing each
    test is actually checking.
    """
    return {
        "symbol": "US30",
        "m15_candles": m15_series(1),
        "m1_candles": m1_series(5),
        "reference": last_m15_close(),
        "entry": entry,
        "specification": _spec(),
        "policy": None,
    }


def _kwargs(overrides: dict[str, object]) -> _Case:
    base = _ready(EntrySettings())
    merged: dict[str, object] = {**base, **overrides}
    return merged  # type: ignore[return-value]


# =============================================================================
# The happy path
# =============================================================================


class TestTradingDecisions:
    def test_a_bullish_m15_and_a_closed_m1_buy_stop(
        self, entry: EntrySettings, spec: SymbolSpecification
    ) -> None:
        decision = evaluate(
            **_kwargs(
                {
                    "m15_candles": [bullish_m15(0)],
                    "m1_candles": m1_series(5),
                    "specification": spec,
                }
            )
        )
        assert isinstance(decision, TradeDecision)
        assert decision.side is Side.SIDE_BUY
        assert decision.signal.order_kind is OrderKind.ORDER_KIND_BUY_STOP
        # The newest closed M1 bar, opened at 12:04, has high 40014.0; +10 points = +1.0.
        assert decision.signal.reference_price == price("40015.0")
        assert decision.entry_candle.open_time == BASE + timedelta(minutes=4)

    def test_a_bearish_m15_sell_stop(
        self, entry: EntrySettings, spec: SymbolSpecification
    ) -> None:
        decision = evaluate(
            **_kwargs(
                {
                    "m15_candles": [bearish_m15(0)],
                    "m1_candles": m1_series(5),
                    "specification": spec,
                }
            )
        )
        assert isinstance(decision, TradeDecision)
        assert decision.side is Side.SIDE_SELL
        assert decision.signal.order_kind is OrderKind.ORDER_KIND_SELL_STOP
        # The newest closed M1 bar has low 39994.0; -10 points = -1.0.
        assert decision.signal.reference_price == price("39993.0")

    def test_the_signal_records_both_candles(
        self, entry: EntrySettings, spec: SymbolSpecification
    ) -> None:
        decision = evaluate(
            **_kwargs(
                {
                    "m15_candles": [bullish_m15(0)],
                    "reference": last_m15_close(),
                    "specification": spec,
                }
            )
        )
        assert isinstance(decision, TradeDecision)
        assert decision.signal.direction_candle_open_time == BASE
        assert decision.signal.direction_timeframe == M15
        assert decision.signal.timeframe == M1
        assert decision.signal.source_candle_open_time == decision.entry_candle.open_time

    def test_the_signal_source_names_the_baseline(self, spec: SymbolSpecification) -> None:
        decision = evaluate(
            **_kwargs({"specification": spec})
        )
        assert isinstance(decision, TradeDecision)
        assert decision.signal.source == "m15_m1_stop"
        assert decision.signal.external is False

    def test_the_signal_carries_no_geometry_by_default(
        self, spec: SymbolSpecification
    ) -> None:
        # Stop and target come from configuration in the risk engine. A signal that invented
        # its own would make it impossible to tell which signals carried geometry.
        decision = evaluate(
            **_kwargs({"specification": spec})
        )
        assert isinstance(decision, TradeDecision)
        assert decision.signal.stop_loss is None
        assert decision.signal.take_profit is None

    def test_the_same_candles_produce_the_same_candle_id(
        self, spec: SymbolSpecification
    ) -> None:
        first = evaluate(**_kwargs({"specification": spec}))
        second = evaluate(**_kwargs({"specification": spec}))
        assert isinstance(first, TradeDecision)
        assert isinstance(second, TradeDecision)
        assert first.candle_id == second.candle_id


# =============================================================================
# NoTrade is a first-class result
# =============================================================================


class TestNoTradeResults:
    def test_a_doji_direction_gives_a_doji_reason(
        self, entry: EntrySettings, spec: SymbolSpecification
    ) -> None:
        decision = evaluate(
            **_kwargs({"m15_candles": [doji_m15(0)], "specification": spec})
        )
        assert isinstance(decision, NoTrade)
        assert decision.reason == NoTradeReason.DOJI_DIRECTION

    def test_a_doji_no_trade_is_not_indeterminate(
        self, entry: EntrySettings, spec: SymbolSpecification
    ) -> None:
        decision = evaluate(
            **_kwargs({"m15_candles": [doji_m15(0)], "specification": spec})
        )
        assert isinstance(decision, NoTrade)
        assert decision.is_indeterminate is False

    def test_no_m15_candles_is_indeterminate(
        self, entry: EntrySettings, spec: SymbolSpecification
    ) -> None:
        decision = evaluate(
            **_kwargs({"m15_candles": [], "specification": spec})
        )
        assert isinstance(decision, NoTrade)
        assert decision.reason == NoTradeReason.NO_DIRECTION
        assert decision.is_indeterminate is True

    def test_a_forming_m15_candle_gives_no_direction(
        self, entry: EntrySettings, spec: SymbolSpecification
    ) -> None:
        decision = evaluate(
            **_kwargs(
                {
                    "m15_candles": [bullish_m15(0)],
                    "m1_candles": m1_series(5),
                    "reference": at(minutes=1),
                    "specification": spec,
                }
            )
        )
        assert isinstance(decision, NoTrade)
        assert decision.reason == NoTradeReason.NO_DIRECTION

    def test_a_forming_m1_candle_gives_the_entry_candle_reason(
        self, entry: EntrySettings, spec: SymbolSpecification
    ) -> None:
        # The M15 bar has closed, so the direction is fine. But the only M1 bar opened at
        # 12:15 and 30 seconds later has not closed, so there is no candle to derive an
        # entry from. This is the normal state of the world for the first minute of every
        # minute, and it must be a quiet no-trade rather than an error or a stale entry.
        decision = evaluate(
            **_kwargs(
                {
                    "m15_candles": [bullish_m15(0)],
                    "m1_candles": [m1(15)],
                    "reference": at(minutes=15, seconds=30),
                    "specification": spec,
                }
            )
        )
        assert isinstance(decision, NoTrade)
        assert decision.reason == NoTradeReason.ENTRY_CANDLE_FORMING
        assert decision.is_indeterminate is True

    def test_the_newest_bar_is_forming_so_the_previous_closed_one_is_used(
        self, entry: EntrySettings, spec: SymbolSpecification
    ) -> None:
        """The rule stated positively, which is the case that actually runs every minute.

        At 12:15:30 the bar that opened at 12:15 is still forming. The last *closed* bar is
        the one that opened at 12:14, so that is the one the entry is derived from. Using
        the forming bar instead would repaint: the same decision a second later would sit
        at a different price.

        The two bars are given deliberately different highs, because bars with the same high
        would make this test pass whether or not the rule was implemented.
        """
        closed_bar = m1(14, high="40024.0", low="39994.0")
        forming_bar = m1(15, high="40025.0", low="39995.0")
        decision = evaluate(
            **_kwargs(
                {
                    "m15_candles": [bullish_m15(0)],
                    "m1_candles": [closed_bar, forming_bar],
                    "reference": at(minutes=15, seconds=30),
                    "specification": spec,
                }
            )
        )
        assert isinstance(decision, TradeDecision)
        assert decision.entry_candle is closed_bar
        # 40024.0 + 10 points (1.0) = 40025.0. The forming bar would have given 40026.0.
        assert decision.signal.reference_price == price("40025.0")

    def test_the_same_decision_would_otherwise_be_repainting(
        self, entry: EntrySettings, spec: SymbolSpecification
    ) -> None:
        # One second later the 12:15 bar has still not closed, so the answer must not move.
        kwargs: dict[str, object] = {
            "m15_candles": [bullish_m15(0)],
            "m1_candles": [m1(14, high="40024.0"), m1(15, high="40025.0")],
            "specification": spec,
        }
        first = evaluate(**_kwargs({**kwargs, "reference": at(minutes=15, seconds=30)}))
        second = evaluate(**_kwargs({**kwargs, "reference": at(minutes=15, seconds=59)}))
        assert isinstance(first, TradeDecision)
        assert isinstance(second, TradeDecision)
        assert first.signal.reference_price == second.signal.reference_price
        # And the moment the forming bar closes, the answer does move -- which is the
        # difference between "the rule is stable" and "the rule is frozen".
        third = evaluate(
            **_kwargs({**kwargs, "reference": at(minutes=16, seconds=1)})
        )
        assert isinstance(third, TradeDecision)
        assert third.entry_candle is not first.entry_candle

    def test_no_m1_candles_at_all_is_reported_separately(
        self, entry: EntrySettings, spec: SymbolSpecification
    ) -> None:
        decision = evaluate(
            **_kwargs(
                {
                    "m15_candles": [bullish_m15(0)],
                    "m1_candles": [],
                    "reference": last_m15_close(),
                    "specification": spec,
                }
            )
        )
        assert isinstance(decision, NoTrade)
        assert decision.reason == NoTradeReason.NO_ENTRY_CANDLE

    def test_the_direction_failure_is_reported_before_the_entry_failure(
        self, entry: EntrySettings, spec: SymbolSpecification
    ) -> None:
        # Order matters: if both are broken, the reason should name the first thing that
        # would have to be fixed, which is the direction.
        decision = evaluate(
            **_kwargs(
                {
                    "m15_candles": [doji_m15(0)],
                    "m1_candles": [],
                    "reference": last_m15_close(),
                    "specification": spec,
                }
            )
        )
        assert isinstance(decision, NoTrade)
        assert decision.reason == NoTradeReason.DOJI_DIRECTION

    def test_a_no_trade_carries_its_evidence(
        self, entry: EntrySettings, spec: SymbolSpecification
    ) -> None:
        decision = evaluate(
            **_kwargs({"m15_candles": [doji_m15(0)], "specification": spec})
        )
        assert isinstance(decision, NoTrade)
        assert decision.direction is not None
        assert decision.direction.candle is not None
        assert decision.detail


class TestSerialisation:
    def test_a_trade_decision_serialises(self, spec: SymbolSpecification) -> None:
        decision = evaluate(**_kwargs({"specification": spec}))
        assert isinstance(decision, TradeDecision)
        payload = decision.to_dict()
        assert payload["decision"] == "trade"
        assert payload["side"] == "BUY"
        assert payload["reference_price"] == "40015.0"

    def test_a_no_trade_serialises(self, spec: SymbolSpecification) -> None:
        decision = evaluate(
            **_kwargs({"m15_candles": [doji_m15(0)], "specification": spec})
        )
        assert isinstance(decision, NoTrade)
        payload = decision.to_dict()
        assert payload["decision"] == "no_trade"
        assert payload["reason"] == NoTradeReason.DOJI_DIRECTION
        assert payload["direction"] == "doji"

    def test_both_serialise_to_json(self, spec: SymbolSpecification) -> None:
        import json

        for candles in ([bullish_m15(0)], [doji_m15(0)]):
            decision = evaluate(
                **_kwargs({"m15_candles": candles, "specification": spec})
            )
            json.dumps(decision.to_dict())

    def test_the_string_forms_are_readable(self, spec: SymbolSpecification) -> None:
        trade = evaluate(**_kwargs({"specification": spec}))
        assert isinstance(trade, TradeDecision)
        assert "BUY_STOP" in str(trade)
        quiet = evaluate(
            **_kwargs({"m15_candles": [], "specification": spec})
        )
        assert "no_direction" in str(quiet)


# =============================================================================
# The instrument policy
# =============================================================================


class TestInstrumentPolicy:
    def test_an_accepted_symbol_passes(self, policy: InstrumentPolicy) -> None:
        check_instrument("US30", policy)
        check_instrument("us30", policy)
        check_instrument("  US30  ", policy)
        check_instrument("US30.CASH", policy)

    def test_an_unapproved_symbol_raises(self, policy: InstrumentPolicy) -> None:
        with pytest.raises(InstrumentNotAllowedError, match="not permitted"):
            check_instrument("EURUSD", policy)

    def test_a_substring_never_matches(self, policy: InstrumentPolicy) -> None:
        # The specific failure the specification forbids.
        for name in ("US30mini", "EURUSD30", "US30X", "XUS30"):
            with pytest.raises(InstrumentNotAllowedError):
                check_instrument(name, policy)

    def test_a_quarantined_symbol_raises_with_its_own_reason(
        self,
    ) -> None:
        guarded = InstrumentPolicy(
            logical_symbol="US30",
            accepted_names=frozenset({"US30"}),
            quarantine=frozenset({"US30m"}),
        )
        with pytest.raises(InstrumentNotAllowedError, match="quarantined"):
            check_instrument("US30m", guarded)

    def test_the_refusal_raises_rather_than_returning_a_no_trade(
        self, entry: EntrySettings, spec: SymbolSpecification, policy: InstrumentPolicy
    ) -> None:
        # A misconfigured run must fail loudly. A NoTrade here would let the system sit
        # declining to trade all day, which is the worst possible outcome.
        with pytest.raises(InstrumentNotAllowedError):
            evaluate(
                **_kwargs({"symbol": "EURUSD", "specification": spec, "policy": policy})
            )

    def test_a_permitted_symbol_still_trades(
        self, entry: EntrySettings, spec: SymbolSpecification, policy: InstrumentPolicy
    ) -> None:
        decision = evaluate(
            **_kwargs({"specification": spec, "policy": policy})
        )
        assert isinstance(decision, TradeDecision)


# =============================================================================
# Entry candle selection
# =============================================================================


class TestEntryCandleSelection:
    def test_it_returns_the_newest_closed_bar(self) -> None:
        candle, report, reason = select_entry_candle(
            m1_series(5), reference=last_m1_close(minutes=4), timeframe=M1
        )
        assert reason is None
        assert candle is not None
        assert candle.open_time == BASE + timedelta(minutes=4)
        assert report is not None

    def test_a_forming_only_series_reports_the_reason(self) -> None:
        candle, _report, reason = select_entry_candle(
            [m1(0)], reference=at(seconds=30), timeframe=M1
        )
        assert candle is None
        assert reason == NoTradeReason.ENTRY_CANDLE_FORMING

    def test_an_empty_series_reports_no_candle(self) -> None:
        candle, report, reason = select_entry_candle([], reference=BASE, timeframe=M1)
        assert candle is None
        assert report is None
        assert reason == NoTradeReason.NO_ENTRY_CANDLE

    def test_m15_bars_are_not_entry_candles(self) -> None:
        candle, _report, reason = select_entry_candle(
            m15_series(2), reference=last_m15_close(index=1), timeframe=M1
        )
        assert candle is None
        assert reason == NoTradeReason.NO_ENTRY_CANDLE


class TestHelpers:
    def test_build_signal_can_carry_geometry(self) -> None:
        from stop_order_scalp.strategy.candle_direction import decide_direction
        from stop_order_scalp.strategy.entry_rules import buy_stop_price

        bar = m1(0)
        level = buy_stop_price(bar, 10, _spec())
        signal = build_signal(
            symbol="US30",
            side=Side.SIDE_BUY,
            level=level,
            entry_candle=bar,
            direction=decide_direction([bullish_m15(0)], reference=last_m15_close()),
            direction_timeframe=M15,
            entry_timeframe=M1,
            stop_loss=price("39900.0"),
        )
        assert signal.stop_loss == price("39900.0")
        assert signal.risk_reward is None

    def test_no_trade_is_constructible_directly(self) -> None:
        decision = no_trade("custom", "a reason", reference=BASE, symbol="US30")
        assert decision.reason == "custom"
        assert "custom" in str(decision)


def _spec() -> SymbolSpecification:
    return SymbolSpecification(
        name="US30", digits=1, point=_d("0.1"), tick_size=_d("0.1"), tick_value=_d("1.0"),
        contract_size=_d("1.0"), volume_min=_d("0.1"), volume_max=_d("50.0"),
        volume_step=_d("0.1"),
    )


def _d(value: str) -> Decimal:
    return Decimal(value)


# =============================================================================
# The property
# =============================================================================


@st.composite
def _scenario(draw: st.DrawFn) -> tuple[list[Candle], list[Candle], datetime]:
    """A random but well-formed market: some M15 bars, some M1 bars, a reference moment."""
    m15_count = draw(st.integers(min_value=1, max_value=6))
    m1_count = draw(st.integers(min_value=1, max_value=30))
    m15_bodies = draw(
        st.lists(
            st.sampled_from(["bull", "bear", "doji"]),
            min_size=m15_count,
            max_size=m15_count,
        )
    )
    bars: list[Candle] = []
    for index, body in enumerate(m15_bodies):
        if body == "bull":
            bars.append(bullish_m15(index))
        elif body == "bear":
            bars.append(bearish_m15(index))
        else:
            bars.append(doji_m15(index))

    m1_bars = m1_series(m1_count)
    reference = BASE + timedelta(seconds=draw(st.integers(min_value=0, max_value=20 * 60)))
    return bars, m1_bars, reference


class TestNoLookAhead:
    """The project's headline correctness property.

    Appending future candles must not change a decision taken at time *t*. If it does, the
    strategy is reading something that was not knowable at *t*, and every backtest built on
    it is fiction.
    """

    @given(_scenario())
    def test_appending_future_bars_cannot_change_the_decision(
        self, scenario: tuple[list[Candle], list[Candle], datetime]
    ) -> None:
        m15_bars, m1_bars, reference = scenario
        entry = EntrySettings()
        spec = _spec()

        before = evaluate(
            symbol="US30",
            m15_candles=m15_bars,
            m1_candles=m1_bars,
            reference=reference,
            entry=entry,
            specification=spec,
        )
        after = evaluate(
            symbol="US30",
            m15_candles=m15_bars,
            m1_candles=[*m1_bars, *_future_m1(m1_bars, reference)],
            reference=reference,
            entry=entry,
            specification=spec,
        )
        assert _fingerprint(before) == _fingerprint(after)

    @given(_scenario())
    def test_a_decision_never_uses_a_bar_that_had_not_closed(
        self, scenario: tuple[list[Candle], list[Candle], datetime]
    ) -> None:
        m15_bars, m1_bars, reference = scenario
        decision = evaluate(
            symbol="US30",
            m15_candles=m15_bars,
            m1_candles=m1_bars,
            reference=reference,
            entry=EntrySettings(),
            specification=_spec(),
        )
        if isinstance(decision, TradeDecision):
            assert decision.entry_candle.is_closed_at(reference)
            assert decision.direction.candle is not None
            assert decision.direction.candle.is_closed_at(reference)

    @given(_scenario())
    def test_the_same_inputs_always_give_the_same_decision(
        self, scenario: tuple[list[Candle], list[Candle], datetime]
    ) -> None:
        m15_bars, m1_bars, reference = scenario
        kwargs: dict[str, object] = {
            "symbol": "US30",
            "m15_candles": m15_bars,
            "m1_candles": m1_bars,
            "reference": reference,
            "entry": EntrySettings(),
            "specification": _spec(),
            "policy": None,
        }
        case: _Case = kwargs  # type: ignore[assignment]
        assert _fingerprint(evaluate(**case)) == _fingerprint(evaluate(**case))

    @given(_scenario())
    def test_moving_the_reference_forward_never_removes_an_entry_candle(
        self, scenario: tuple[list[Candle], list[Candle], datetime]
    ) -> None:
        # As time passes the decision may only change from "no trade" to "trade", never
        # from one trade to a different one at the same moment.
        m15_bars, m1_bars, reference = scenario
        entry = EntrySettings()
        spec = _spec()
        earlier = evaluate(
            symbol="US30", m15_candles=m15_bars, m1_candles=m1_bars,
            reference=reference, entry=entry, specification=spec,
        )
        later = evaluate(
            symbol="US30", m15_candles=m15_bars, m1_candles=m1_bars,
            reference=reference + timedelta(minutes=20),
            entry=entry, specification=spec,
        )
        if isinstance(earlier, TradeDecision) and isinstance(later, TradeDecision):
            # Both traded; the later one may use a newer entry candle but must be one of
            # the bars that existed earlier or a genuinely newer one.
            assert later.entry_candle.open_time >= earlier.entry_candle.open_time

    @given(_scenario())
    def test_input_order_does_not_change_the_decision(
        self, scenario: tuple[list[Candle], list[Candle], datetime]
    ) -> None:
        m15_bars, m1_bars, reference = scenario
        entry = EntrySettings()
        spec = _spec()
        forward = evaluate(
            symbol="US30", m15_candles=m15_bars, m1_candles=m1_bars,
            reference=reference, entry=entry, specification=spec,
        )
        backward = evaluate(
            symbol="US30", m15_candles=list(reversed(m15_bars)),
            m1_candles=list(reversed(m1_bars)),
            reference=reference, entry=entry, specification=spec,
        )
        assert _fingerprint(forward) == _fingerprint(backward)


def _future_m1(bars: list[Candle], reference: datetime) -> list[Candle]:
    """Bars that open strictly *after* ``reference``, so no decision could have seen them.

    Anchored on the reference, not merely on the last existing bar. A helper that only
    looked at the last bar would emit bars the reference could legitimately see, and the
    property would fail for the right reason in the wrong way: the strategy was correct and
    the test was wrong. This was not hypothetical -- it is exactly what happened the first
    time this test was written.
    """
    start = max(bars[-1].open_time, reference) if bars else max(BASE, reference)
    return [
        m1(
            0,
            open_="41000.0",
            high="42000.0",
            low="40000.0",
            close="41500.0",
            start=start + timedelta(minutes=offset + 1),
        )
        for offset in range(10)
    ]


def _fingerprint(decision: object) -> tuple[object, ...]:
    """Everything about a decision that a caller could act on.

    A trade and a no-trade are different fingerprints, and so are two trades at different
    prices -- which is exactly what must not change when future bars are appended.
    """
    if isinstance(decision, TradeDecision):
        return (
            "trade",
            str(decision.side),
            str(decision.signal.order_kind),
            str(decision.signal.reference_price),
            decision.signal.candle_id,
        )
    if isinstance(decision, NoTrade):
        return ("no_trade", decision.reason)
    raise AssertionError(f"unexpected decision type {type(decision)!r}")
