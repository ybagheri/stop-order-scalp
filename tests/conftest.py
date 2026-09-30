"""Shared test configuration.

Three jobs, all of them about making a failure mean something:

**Hermetic time.** ``TZ`` is pinned and every test that needs a clock takes a
:class:`~stop_order_scalp.infrastructure.clock.FixedClock`. Nothing in the unit suite
calls ``datetime.now()``.

**Hermetic environment.** ``autouse`` :func:`clean_environment` deletes every ``SOS_*``
variable before each test. Without it, one test's ``monkeypatch.setenv`` leaking into
another silently changes configuration, which produces a failure that appears on one
machine and not another.

**One place for fixtures.** Symbol specifications, candles and account snapshots are
domain knowledge, not per-test knowledge. Two tests that disagree about what a US30
specification looks like would produce two different "correct" answers.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from stop_order_scalp.domain.enums import Side
from stop_order_scalp.domain.models import AccountSnapshot, Candle
from stop_order_scalp.domain.value_objects import Money, Price, SymbolSpecification, Volume

#: A fixed instant used as the reference "now" across the suite. Chosen to be a
#: Thursday 12:34:07 UTC -- deliberately mid-candle, so a "closed candle" test cannot pass
#: by accident on a boundary.
REFERENCE_NOW = datetime(2026, 3, 12, 12, 34, 7, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ``SOS_*`` variable so no test inherits another's configuration."""
    for key in [name for name in os.environ if name.startswith("SOS_")]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def pinned_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the process timezone so local-time formatting cannot vary by machine."""
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):  # pragma: no branch - Windows lacks tzset
        time.tzset()


@pytest.fixture
def reference_now() -> datetime:
    return REFERENCE_NOW


@pytest.fixture
def clock() -> "object":
    from stop_order_scalp.infrastructure.clock import FixedClock

    return FixedClock(REFERENCE_NOW)


# =============================================================================
# Symbol specification
# =============================================================================


@pytest.fixture
def us30_spec() -> SymbolSpecification:
    """A plausible US30 specification.

    **Assumed, not measured.** These values are the project's working assumption until
    Phase 11 captures the real values from the user's broker. Every risk calculation in
    the test suite is stated against this object, so when the real numbers arrive only
    this fixture changes.

    Shape: point 0.1, tick 0.1, tick value 1.0 per lot, contract size 1.0, volume step
    0.1. On these numbers, ten points is 1.0 price units and a 100-point move costs
    $1000 per lot -- which is exactly the "do not assume 1 point = $1" case made
    concrete.
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
        currency="USD",
    )


@pytest.fixture
def account() -> AccountSnapshot:
    """A $100,000 demo account."""
    return AccountSnapshot(
        login=1234567,
        server="DemoServer",
        currency="USD",
        balance=Money.of(100000),
        equity=Money.of(100000),
        margin_used=Money.of(0),
        margin_free=Money.of(100000),
        leverage=100,
        is_demo=True,
        trade_allowed=True,
    )


# =============================================================================
# Candles
# =============================================================================


def make_candle(
    open_time: datetime,
    high: str,
    low: str,
    close: str,
    *,
    open_price: str | None = None,
    timeframe: str = "M1",
    timeframe_seconds: int = 60,
    digits: int = 1,
) -> Candle:
    """Build a candle with sensible defaults for the omitted fields.

    ``open`` defaults to the midpoint of the range and is clamped into it, so a test that
    only cares about high/low does not have to invent a plausible open. A candle whose
    open sits outside its own range is invalid, and :class:`Candle` rejects it -- that
    check is worth having, so the helper works with it rather than around it.
    """
    high_price = Price.parse(high, digits)
    low_price = Price.parse(low, digits)
    close_price = Price.parse(close, digits)
    if open_price is not None:
        open_value = Price.parse(open_price, digits)
    else:
        midpoint = (high_price.value + low_price.value) / 2
        open_value = Price(min(max(midpoint, low_price.value), high_price.value), digits)
    return Candle(
        open_time=open_time,
        open=open_value,
        high=high_price,
        low=low_price,
        close=close_price,
        timeframe_seconds=timeframe_seconds,
        timeframe=timeframe,
    )


@pytest.fixture
def candle_factory():
    """The :func:`make_candle` helper, injected."""
    return make_candle


@pytest.fixture
def m1_candles(candle_factory) -> list[Candle]:
    """Three closed M1 candles and the currently forming one.

    ``closed_only`` on the last one is what makes this fixture useful for look-ahead
    tests: the forming candle has a high above the closed candles' highs, so code that
    wrongly reads it produces a visibly different entry price.
    """
    base = datetime(2026, 3, 12, 12, 30, 0, tzinfo=UTC)
    return [
        candle_factory(base, "40010.0", "39990.0", "40000.0"),
        candle_factory(base + timedelta(minutes=1), "40020.0", "40000.0", "40010.0"),
        candle_factory(base + timedelta(minutes=2), "40030.0", "40005.0", "40020.0"),
        candle_factory(base + timedelta(minutes=3), "40100.0", "40020.0", "40095.0"),
    ]


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def price(value: str, digits: int = 1) -> Price:
    return Price.parse(value, digits)


def volume(lots: str) -> Volume:
    return Volume.of(Decimal(lots))


def money(value: str) -> Money:
    return Money.of(Decimal(value), "USD")


__all__ = [
    "REFERENCE_NOW",
    "AccountSnapshot",
    "Candle",
    "Money",
    "Price",
    "REFERENCE_NOW",
    "Side",
    "SymbolSpecification",
    "Volume",
    "make_candle",
    "money",
    "price",
    "volume",
]