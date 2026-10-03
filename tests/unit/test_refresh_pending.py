"""A resting pending order must follow the decision, or be removed when none supports it.

``docs/execution/EXECUTION.md`` says a new candle produces a new tag "which is what lets a
stale pending order be replaced". Before ``entry.refresh_pending`` nothing did that: the first
order stayed on the book, GTC, until price happened to cross it -- on 7 800 bars of a
volatility-realistic random walk the replay placed one order and then reported
"order already resting" 7 799 times.

Each test below is a different way the refresh could go wrong. The one that matters most is
the last: ``cancel_order`` reports success for an order that has *already filled*, so a refresh
that trusted it would orphan a live position's bookkeeping.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from stop_order_scalp.domain.enums import LifecycleState
from stop_order_scalp.domain.models import TradePlan
from stop_order_scalp.domain.value_objects import Price
from stop_order_scalp.infrastructure.clock import FixedClock
from stop_order_scalp.infrastructure.persistence import StateLedger
from stop_order_scalp.lifecycle.trade_lifecycle import TradeLifecycle

from .test_trade_lifecycle import (
    MAGIC,
    NOW,
    FakeBroker,
    FakeOrderManager,
    plan,
    position,
)


def _lifecycle(ledger_path: Path, broker: FakeBroker, *, refresh: bool) -> TradeLifecycle:
    return TradeLifecycle(
        broker,
        StateLedger.load(ledger_path),
        FakeOrderManager(),
        clock=FixedClock(NOW),
        magic_number=MAGIC,
        symbol="US30",
        refresh_pending=refresh,
    )


def _next_candle_plan(entry: str = "40005.0") -> TradePlan:
    """The same trade one M1 candle later, anchored on a different price."""
    base = plan()
    signal = replace(base.signal, source_candle_open_time=NOW + timedelta(minutes=1))
    return replace(
        base,
        plan_id="US30-M15-1-M1-2",
        signal=signal,
        entry=Price.parse(entry, 1),
    )


class _NoTrade:
    """Anything that is not a ``TradePlan`` is the strategy declining."""

    reason = "doji"
    detail = ""


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


class TestRefreshPending:
    def test_disabled_keeps_the_first_order_forever(self, ledger_path: Path) -> None:
        broker = FakeBroker()
        lifecycle = _lifecycle(ledger_path, broker, refresh=False)
        lifecycle.place_order(plan())

        lifecycle.tick(_next_candle_plan())

        assert broker.cancels == []
        assert len(broker.sends) == 1

    def test_a_new_candle_replaces_the_stale_order(self, ledger_path: Path) -> None:
        broker = FakeBroker()
        lifecycle = _lifecycle(ledger_path, broker, refresh=True)
        lifecycle.place_order(plan())

        lifecycle.tick(_next_candle_plan("40005.0"))

        assert broker.cancels == [556]
        assert len(broker.sends) == 2
        assert [r.ticket for r in broker.orders()] == [557]
        assert lifecycle.state is LifecycleState.STATE_WAITING_FOR_TRIGGER

    def test_an_identical_decision_does_not_churn_the_order(self, ledger_path: Path) -> None:
        broker = FakeBroker()
        lifecycle = _lifecycle(ledger_path, broker, refresh=True)
        lifecycle.place_order(plan())

        lifecycle.tick(plan())

        assert broker.cancels == []
        assert len(broker.sends) == 1

    def test_no_trade_removes_the_stale_order(self, ledger_path: Path) -> None:
        broker = FakeBroker()
        lifecycle = _lifecycle(ledger_path, broker, refresh=True)
        lifecycle.place_order(plan())

        lifecycle.tick(_NoTrade())

        assert broker.cancels == [556]
        assert broker.orders() == []
        assert len(broker.sends) == 1
        assert lifecycle.state is LifecycleState.STATE_WAITING_FOR_SIGNAL

    def test_a_filled_order_is_never_cancelled(self, ledger_path: Path) -> None:
        """The race: the order filled, the book now shows a position and no working order."""
        broker = FakeBroker()
        lifecycle = _lifecycle(ledger_path, broker, refresh=True)
        lifecycle.place_order(plan())
        broker.positions_open.append(position(tag=broker.orders()[0].client_tag))

        lifecycle.tick(_next_candle_plan())

        assert broker.cancels == []
        assert len(broker.sends) == 1

    def test_the_decision_does_not_have_to_be_a_plan_to_be_safe(self, ledger_path: Path) -> None:
        """With nothing resting, a refresh pass is a no-op rather than an error."""
        broker = FakeBroker()
        lifecycle = _lifecycle(ledger_path, broker, refresh=True)
        lifecycle.recover()

        step: Any = lifecycle.tick(_NoTrade())

        assert broker.cancels == []
        assert "no_trade" in step.actions
