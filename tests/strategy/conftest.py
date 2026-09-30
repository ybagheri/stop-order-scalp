"""Shared fixtures and builders for the strategy tests.

Candle series are built here rather than inline in each module so that the direction and
entry tests describe *intent* ("a bullish M15 followed by three M1 bars") instead of
re-deriving open times and price ladders every time.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from stop_order_scalp.domain.enums import Side, TimeframeSelection
from stop_order_scalp.domain.models import Candle, InstrumentPolicy
from stop_order_scalp.domain.value_objects import Price, SymbolSpecification
from stop_order_scalp.infrastructure.config import EntrySettings, StrategySettings
from stop_order_scalp.market_data.timeframes import Timeframe

#: 2026-03-12 12:00:00 UTC. A Thursday, so it is an ordinary trading day.
BASE = datetime(2026, 3, 12, 12, 0, 0, tzinfo=UTC)

M1 = Timeframe.M1
M15 = Timeframe.M15

_PERIOD = {M1: 60, M15: 900}
_DIGITS = {M1: 1, M15: 1}


def price(value: str | int | Decimal, digits: int = 1) -> Price:
    return Price.parse(str(value), digits)


def m15(
    offset_index: int,
    *,
    open_: str = "40000.0",
    high: str = "40050.0",
    low: str = "39950.0",
    close: str | None = None,
    start: datetime = BASE,
    digits: int = 1,
) -> Candle:
    """One M15 bar, ``offset_index`` bars after ``start``."""
    return _bar(
        start + timedelta(seconds=900 * offset_index),
        open_,
        high,
        low,
        close if close is not None else open_,
        M15,
        digits,
    )


def m1(
    offset_minutes: int,
    *,
    open_: str = "40000.0",
    high: str = "40050.0",
    low: str = "39950.0",
    close: str | None = None,
    start: datetime = BASE,
    digits: int = 1,
) -> Candle:
    """One M1 bar, ``offset_minutes`` minutes after ``start``."""
    return _bar(
        start + timedelta(minutes=offset_minutes),
        open_,
        high,
        low,
        close if close is not None else open_,
        M1,
        digits,
    )


def _bar(
    open_time: datetime,
    open_: str,
    high: str,
    low: str,
    close: str,
    timeframe: str,
    digits: int,
) -> Candle:
    return Candle(
        open_time=open_time,
        open=price(open_, digits),
        high=price(high, digits),
        low=price(low, digits),
        close=price(close, digits),
        timeframe_seconds=_PERIOD[timeframe],
        timeframe=timeframe,
    )


def bullish_m15(index: int = 0, *, start: datetime = BASE) -> Candle:
    """An M15 bar that closed above its open: permits BUY."""
    return m15(index, open_="40000.0", high="40060.0", low="39990.0", close="40050.0", start=start)


def bearish_m15(index: int = 0, *, start: datetime = BASE) -> Candle:
    """An M15 bar that closed below its open: permits SELL."""
    return m15(index, open_="40050.0", high="40060.0", low="39990.0", close="40000.0", start=start)


def doji_m15(index: int = 0, *, start: datetime = BASE) -> Candle:
    """An M15 bar that closed exactly on its open: no direction."""
    return m15(index, open_="40000.0", high="40060.0", low="39990.0", close="40000.0", start=start)


def m1_series(count: int, *, start: datetime = BASE, base_level: int = 40000) -> list[Candle]:
    """``count`` consecutive M1 bars, oldest first."""
    return [
        m1(
            index,
            open_=str(base_level + index),
            high=str(base_level + 10 + index),
            low=str(base_level - 10 + index),
            close=str(base_level + 5 + index),
            start=start,
        )
        for index in range(count)
    ]


def m15_series(count: int, *, start: datetime = BASE) -> list[Candle]:
    """``count`` consecutive bullish M15 bars, oldest first."""
    return [bullish_m15(index, start=start) for index in range(count)]


# =============================================================================
# Configuration
# =============================================================================


@pytest.fixture
def entry() -> EntrySettings:
    """The baseline entry settings, matching ``config/default.yaml``."""
    return EntrySettings(
        timeframe=M1,
        direction_timeframe=M15,
        offset_points=10,
        candle_selection=TimeframeSelection.LAST_CLOSED,
        lookback=5,
    )


@pytest.fixture
def settings(entry: EntrySettings) -> StrategySettings:
    return StrategySettings(symbol="US30", symbol_aliases=("US30",), entry=entry)


@pytest.fixture
def policy() -> InstrumentPolicy:
    return InstrumentPolicy(
        logical_symbol="US30",
        accepted_names=frozenset({"US30", "US30.cash"}),
    )


@pytest.fixture
def spec() -> SymbolSpecification:
    """The project's assumed US30 specification, identical to the conftest one.

    Duplicated here so the strategy tests read as self-contained. The two must agree;
    ``test_specification_matches_the_shared_fixture`` checks that they do.
    """
    return SymbolSpecification(
        name="US30",
        digits=1,
        point=Decimal("0.1"),
        tick_size=Decimal("0.1"),
        tick_value=Decimal("1.0"),
        contract_size=Decimal("1.0"),
        volume_min=Decimal("0.1"),
        volume_max=Decimal("50.0"),
        volume_step=Decimal("0.1"),
        stops_level=10,
        freeze_level=0,
    )


# =============================================================================
# Reference moments
# =============================================================================


def at(**kwargs: float) -> datetime:
    """A reference moment relative to :data:`BASE`."""
    return BASE + timedelta(**kwargs)


def last_m15_close(*, start: datetime = BASE, index: int = 0) -> datetime:
    """The moment the ``index``-th M15 bar closes."""
    return start + timedelta(seconds=900 * (index + 1))


def last_m1_close(*, start: datetime = BASE, minutes: int = 0) -> datetime:
    """The moment the M1 bar ``minutes`` after ``start`` closes."""
    return start + timedelta(minutes=minutes + 1)


def closed_m1(count: int, *, start: datetime = BASE) -> tuple[list[Candle], datetime]:
    """``count`` M1 bars and the moment the last of them has closed."""
    return m1_series(count, start=start), last_m1_close(start=start, minutes=count - 1)


def closed_m15(count: int, *, start: datetime = BASE) -> tuple[list[Candle], datetime]:
    """``count`` M15 bars and the moment the last of them has closed."""
    return m15_series(count, start=start), last_m15_close(start=start, index=count - 1)


def only_buy_or_trade(decision: object) -> bool:
    """Whether a decision is one of the two trading outcomes."""
    from stop_order_scalp.strategy.signal import TradeDecision

    return isinstance(decision, TradeDecision) and decision.side in (Side.SIDE_BUY, Side.SIDE_SELL)


def as_sequence(items: Sequence[Candle]) -> Sequence[Candle]:
    return items
