"""Shared fixtures for the execution tests.

The safety properties tested here are not about code paths, so the fixtures are built to
make the *dangerous* situations easy to reach: a plan that is already on the book, a venue
that accepts an order but never answers, and a terminal that reports an ambiguous retcode.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from stop_order_scalp.domain.enums import CommissionMode, Environment, OrderKind, Side, TargetMode
from stop_order_scalp.domain.models import (
    AccountSnapshot,
    EnvironmentSettings,
    TradePlan,
    TradeSignal,
)
from stop_order_scalp.domain.value_objects import Money, Price, SymbolSpecification
from stop_order_scalp.execution import simulated_broker as venue
from stop_order_scalp.execution.order_manager import OrderManager
from stop_order_scalp.execution.simulated_broker import SimulatedBroker
from stop_order_scalp.infrastructure.clock import FixedClock
from stop_order_scalp.infrastructure.config import (
    ExecutionSettings,
    OrderSettings,
    RiskSettings,
    TargetSettings,
)
from stop_order_scalp.risk.risk_manager import RiskManager, RiskRequest

NOW = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
ENTRY = Price.parse("40000.0", 1)


@pytest.fixture(autouse=True)
def clean_venue() -> Any:
    """Reset the module-level venue economics around every test.

    The simulated venue keeps its instruments in module state so that
    :func:`configure_specification` can be called from a composition root. That is
    convenient and it is global, so it is undone here rather than trusted.
    """
    venue.reset_venue()
    yield
    venue.reset_venue()


@pytest.fixture
def us30() -> SymbolSpecification:
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
def configured_venue(us30: SymbolSpecification) -> SymbolSpecification:
    """A venue that knows US30 and charges the baseline $6 per lot round trip."""
    venue.configure_specification(
        us30, commission=Decimal("6.0"), commission_mode=CommissionMode.PER_LOT_ROUND_TRIP
    )
    return us30


@pytest.fixture
def dry_run_settings() -> EnvironmentSettings:
    return EnvironmentSettings(environment=Environment.DRY_RUN)


@pytest.fixture
def live_settings() -> EnvironmentSettings:
    """A fully opted-in LIVE configuration. Reaching it requires all three switches."""
    return EnvironmentSettings(
        environment=Environment.LIVE,
        allow_live=True,
        allow_order=True,
        allow_close=True,
    )


@pytest.fixture
def account() -> AccountSnapshot:
    return AccountSnapshot(
        login=1234567,
        server="DemoServer",
        currency="USD",
        balance=Money.of(10000),
        equity=Money.of(10000),
        margin_used=Money.of(0),
        margin_free=Money.of(10000),
    )


@pytest.fixture
def signal() -> TradeSignal:
    return TradeSignal(
        symbol="US30",
        side=Side.SIDE_BUY,
        timeframe="M1",
        direction_timeframe="M15",
        source_candle_open_time=NOW,
        direction_candle_open_time=NOW,
        order_kind=OrderKind.ORDER_KIND_BUY_STOP,
        reference_price=ENTRY,
    )


@pytest.fixture
def risk_manager() -> RiskManager:
    return RiskManager(
        RiskSettings(),
        TargetSettings(mode=TargetMode.TARGET_MODE_RISK_REWARD, risk_reward=Decimal("2.0")),
    )


@pytest.fixture
def plan(
    risk_manager: RiskManager, signal: TradeSignal, account: AccountSnapshot, us30: SymbolSpecification
) -> TradePlan:
    """A real, accepted plan -- sized by the real risk engine, not hand-made."""
    return risk_manager.plan_for(
        RiskRequest(
            signal=signal,
            account=account,
            specification=us30,
            risk=risk_manager.risk_settings,
            target=risk_manager.target_settings,
        )
    )


@pytest.fixture
def broker(dry_run_settings: EnvironmentSettings, configured_venue: SymbolSpecification) -> SimulatedBroker:
    """A connected simulated venue, priced at the plan's entry so nothing triggers yet."""
    venue = SimulatedBroker(dry_run_settings, clock=FixedClock(NOW))
    venue.connect()
    venue.publish("US30", Decimal("39999.0"), Decimal("39999.5"), digits=1)
    return venue


@pytest.fixture
def manager(risk_manager: RiskManager) -> OrderManager:
    return OrderManager(ExecutionSettings(), OrderSettings(), risk_manager)
