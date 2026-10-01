"""Shared fixtures for the position-management tests.

The specification's numbers: a US30 with ``point = 0.1``, so **ten points is one price
unit** and a 100-point stop is a distance of 10.0. ``docs/mt5/SYMBOL_SPECIFICATIONS.md``
works the same numbers by hand, so a change to either must break the other.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from stop_order_scalp.domain.enums import Side
from stop_order_scalp.domain.models import PositionRecord, Tick
from stop_order_scalp.domain.value_objects import Money, Price, SymbolSpecification, Volume
from stop_order_scalp.execution.position_manager import PositionManager
from stop_order_scalp.infrastructure.config import BreakEvenSettings, TrailingSettings
from stop_order_scalp.trailing.break_even import ConfiguredBreakEvenProvider
from stop_order_scalp.trailing.trailing_stop import TrailingStopProvider

NOW = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)

ENTRY = Price.parse("40000.0", 1)


@pytest.fixture
def us30() -> SymbolSpecification:
    """The project's assumed US30 specification.

    ``stops_level = 10`` is 1 price unit, which is deliberately close enough to the trailing
    distances to make the distance checks bite rather than always pass.
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


@pytest.fixture
def tight_broker(us30: SymbolSpecification) -> SymbolSpecification:
    """An instrument whose stops and freeze levels would block almost any move.

    Exists so a test can assert a refusal is the *broker's* doing rather than the
    arithmetic's, by making the arithmetic unambiguous.
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
        stops_level=5000,
        freeze_level=5000,
    )


def tick(bid: str, ask: str | None = None, digits: int = 1) -> Tick:
    """A tick with a one-tick spread unless an ask is given.

    ``Price.__add__`` yields a bare ``Decimal``, so the default ask is re-wrapped with
    :meth:`Price.at_digits` -- a bare ``Decimal`` cannot know the digit count and must not be
    sent to a broker.
    """
    bid_price = Price.parse(bid, digits)
    spread = Price(bid_price.value + Decimal("0.1").scaleb(-digits), digits)
    return Tick(
        moment=NOW,
        bid=bid_price,
        ask=Price.parse(ask, digits) if ask is not None else spread,
    )


def position(
    side: Side = Side.SIDE_BUY,
    *,
    entry: str = "40000.0",
    stop: str | None = "39990.0",
    ticket: int = 500,
    digits: int = 1,
) -> PositionRecord:
    """An open position, defaulting to the strategy's own shape.

    A BUY at 40000 with a 100-point stop is exactly what Phase 4's worked example produces,
    so a test failure here means something about the real strategy rather than about a
    contrived fixture.
    """
    return PositionRecord(
        ticket=ticket,
        symbol="US30",
        side=side,
        volume=Volume.of(Decimal("0.4")),
        entry=Price.parse(entry, digits),
        stop_loss=Price.parse(stop, digits) if stop is not None else None,
        take_profit=Price.parse("40020.0", digits),
        magic_number=20260930,
        comment="buy stop",
        opened_at=NOW,
        profit=Money.of(0),
        client_tag="abc123",
    )


@pytest.fixture
def break_even_settings() -> BreakEvenSettings:
    """The baseline: armed 50 points in favour."""
    return BreakEvenSettings(trigger_points=50)


@pytest.fixture
def trailing_settings() -> TrailingSettings:
    """The baseline: 100 points, one-point minimum step."""
    return TrailingSettings(distance_points=100)


@pytest.fixture
def break_even(break_even_settings: BreakEvenSettings) -> ConfiguredBreakEvenProvider:
    return ConfiguredBreakEvenProvider(break_even_settings)


@pytest.fixture
def trailing(trailing_settings: TrailingSettings) -> TrailingStopProvider:
    return TrailingStopProvider(trailing_settings)


@pytest.fixture
def manager(break_even_settings: BreakEvenSettings, trailing_settings: TrailingSettings) -> PositionManager:
    return PositionManager(break_even_settings, trailing_settings)


def points(value: str | int | Decimal) -> Decimal:
    """``points`` on the assumed specification, as a Decimal count of points."""
    return Decimal(str(value)) * Decimal("10")
