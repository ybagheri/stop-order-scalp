"""Shared fixtures for the risk tests.

The specification fixture carries the project's assumed ``US30`` numbers, and the tests
that matter here are the ones that state the *arithmetic* rather than the code path:
``docs/mt5/SYMBOL_SPECIFICATIONS.md`` §2 works the same numbers through by hand, and a
change to either must break the other.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from stop_order_scalp.domain.enums import CommissionMode, OrderKind, RiskMode, Side, TargetMode
from stop_order_scalp.domain.models import AccountSnapshot, TradeSignal
from stop_order_scalp.domain.value_objects import Money, Price, SymbolSpecification
from stop_order_scalp.infrastructure.config import (
    OrderSettings,
    RiskSettings,
    TargetSettings,
)
from stop_order_scalp.risk.commission import CommissionModel_
from stop_order_scalp.risk.position_sizer import SizingInput

NOW = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)

ENTRY = Price.parse("40000.0", 1)
#: 100 points below entry on point 0.1 -- i.e. 10.0 price units.
STOP_100PT = Price.parse("39990.0", 1)


@pytest.fixture
def us30() -> SymbolSpecification:
    """The project's assumed US30 specification, matching ``docs/mt5``."""
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
def expensive_spec() -> SymbolSpecification:
    """An instrument whose tick value makes 100 points cost ten times as much.

    Exists so a test can assert the sizing responds to ``tick_value`` rather than to a
    hard-coded assumption. On this one a 100-point stop costs $1000 per lot, so the same
    $50 budget that buys 0.4 lots on ``us30`` must buy far less here.
    """
    return SymbolSpecification(
        name="SPX",
        digits=1,
        point=Decimal("0.1"),
        tick_size=Decimal("0.1"),
        tick_value=Decimal("10.0"),
        contract_size=Decimal("1.0"),
        volume_min=Decimal("0.1"),
        volume_max=Decimal("50.0"),
        volume_step=Decimal("0.1"),
    )


@pytest.fixture
def account() -> AccountSnapshot:
    """A $10,000 demo account -- the balance the worked example uses."""
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
def risk() -> RiskSettings:
    """The baseline risk settings, matching ``config/default.yaml``."""
    return RiskSettings()


@pytest.fixture
def target() -> TargetSettings:
    return TargetSettings()


@pytest.fixture
def target_1r() -> TargetSettings:
    return TargetSettings(mode=TargetMode.TARGET_MODE_RISK_REWARD, risk_reward=Decimal("1.0"))


@pytest.fixture
def orders() -> OrderSettings:
    return OrderSettings()


@pytest.fixture
def commission() -> CommissionModel_:
    return CommissionModel_(
        rate=Decimal("6.0"), mode=CommissionMode.PER_LOT_ROUND_TRIP
    )


@pytest.fixture
def buy_signal() -> TradeSignal:
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
def sell_signal() -> TradeSignal:
    return TradeSignal(
        symbol="US30",
        side=Side.SIDE_SELL,
        timeframe="M1",
        direction_timeframe="M15",
        source_candle_open_time=NOW,
        direction_candle_open_time=NOW,
        order_kind=OrderKind.ORDER_KIND_SELL_STOP,
        reference_price=ENTRY,
    )


#: The baseline commission, and an explicit "no model at all" sentinel.
#:
#: ``None`` cannot mean "no commission", because *absence of a model* is a real case the
#: sizer must handle -- and a test that wanted zero commission would otherwise silently get
#: the baseline $6 and assert the wrong thing.
NO_COMMISSION: CommissionModel_ = CommissionModel_(
    rate=Decimal("0"), mode=CommissionMode.PER_LOT_ROUND_TRIP
)


def risk_input(
    *,
    entry: Price = ENTRY,
    stop: Price = STOP_100PT,
    balance: Money | None = None,
    specification: SymbolSpecification | None = None,
    commission_model: CommissionModel_ | None = None,
    mode: RiskMode = RiskMode.RISK_MODE_PERCENT_BALANCE,
    percent: Decimal = Decimal("0.5"),
    fixed_lot: Decimal = Decimal("0.10"),
    refuse_below_min_volume: bool = True,
) -> SizingInput:
    """Build a :class:`SizingInput` with the baseline everywhere and one thing varied.

    ``commission_model=None`` means *the baseline*, not *no commission*. Pass
    :data:`NO_COMMISSION` for a genuinely free instrument, and omit the ``commission``
    field of :class:`SizingInput` to exercise the no-model path.
    """
    return SizingInput(
        entry=entry,
        stop=stop,
        balance=balance if balance is not None else Money.of(10000),
        specification=specification if specification is not None else us30_default(),
        mode=mode,
        percent=percent,
        fixed_lot=fixed_lot,
        commission=commission_model
        if commission_model is not None
        else CommissionModel_(rate=Decimal("6.0"), mode=CommissionMode.PER_LOT_ROUND_TRIP),
        refuse_below_min_volume=refuse_below_min_volume,
    )


def without_commission(**overrides: Any) -> SizingInput:
    """A :class:`SizingInput` with **no** commission model attached at all.

    Distinct from :data:`NO_COMMISSION`, which is an explicit zero-rate model. The two
    reach the sizer by different routes and both have to work.
    """
    base = risk_input(**overrides)
    return SizingInput(
        entry=base.entry,
        stop=base.stop,
        balance=base.balance,
        specification=base.specification,
        mode=base.mode,
        percent=base.percent,
        fixed_lot=base.fixed_lot,
        commission=None,
        refuse_below_min_volume=base.refuse_below_min_volume,
    )


def us30_default() -> SymbolSpecification:
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


def account_with(balance: str | int | Decimal, **kwargs: Any) -> AccountSnapshot:
    """An account with a chosen balance, so a rejection path can be exercised."""
    return AccountSnapshot(
        login=1234567,
        server="DemoServer",
        currency="USD",
        balance=Money.of(balance),
        equity=Money.of(balance),
        margin_used=Money.of(0),
        margin_free=Money.of(balance),
        **kwargs,
    )


def signal_with(**kwargs: Any) -> TradeSignal:
    """A buy signal with individual fields overridden."""
    base: dict[str, Any] = {
        "symbol": "US30",
        "side": Side.SIDE_BUY,
        "timeframe": "M1",
        "direction_timeframe": "M15",
        "source_candle_open_time": NOW,
        "direction_candle_open_time": NOW,
        "order_kind": OrderKind.ORDER_KIND_BUY_STOP,
        "reference_price": ENTRY,
    }
    base.update(kwargs)
    return TradeSignal(**base)
