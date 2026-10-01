"""The simulated venue behaves like a venue.

The point of this suite is that ``DRY_RUN`` proves something. If the simulator filled at a
mid price, ignored commission, or triggered stops on the wrong side, then a passing dry run
would be worse than no dry run -- it would be a confident wrong answer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from stop_order_scalp.domain.enums import CommissionMode, Side
from stop_order_scalp.domain.exceptions import (
    BrokerError,
    BrokerNotConnectedError,
    BrokerRejectedError,
    OrderValidationError,
    SymbolNotFoundError,
)
from stop_order_scalp.domain.models import OrderIntent, TradePlan
from stop_order_scalp.domain.value_objects import Price, SymbolSpecification, Volume
from stop_order_scalp.execution import simulated_broker as venue
from stop_order_scalp.execution.simulated_broker import SimulatedBroker

NOW = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)


def make_intent(plan: TradePlan, **changes: object) -> OrderIntent:
    """The intent the manager would send, with individual fields varied for a test."""
    base = OrderIntent.from_plan(plan, magic_number=20260930, deviation_points=10)
    return OrderIntent(
        plan_id=changes.get("plan_id", base.plan_id),  # type: ignore[arg-type]
        client_tag=changes.get("client_tag", base.client_tag),  # type: ignore[arg-type]
        symbol=changes.get("symbol", base.symbol),  # type: ignore[arg-type]
        kind=changes.get("kind", base.kind),  # type: ignore[arg-type]
        volume=changes.get("volume", base.volume),  # type: ignore[arg-type]
        entry=changes.get("entry", base.entry),  # type: ignore[arg-type]
        stop_loss=changes.get("stop_loss", base.stop_loss),  # type: ignore[arg-type]
        take_profit=changes.get("take_profit", base.take_profit),  # type: ignore[arg-type]
        magic_number=base.magic_number,
        comment=base.comment,
        deviation_points=base.deviation_points,
    )


def fill_through(broker: SimulatedBroker, plan: TradePlan, price: str = "40010.0") -> None:
    """Rest the order and drive price through it, leaving an open position.

    40010 sits above the 40000 entry but well below the 40020 target. A price at the target
    would fill *and* close in the same settle, which is correct venue behaviour and useless
    as a test of an open position.
    """
    broker.place_order(make_intent(plan))
    broker.publish("US30", Decimal(price), Decimal(price) + Decimal("0.5"), digits=1)


class TestConnection:
    def test_reading_before_connecting_is_refused(
        self, dry_run_settings: object, configured_venue: object
    ) -> None:
        broker = SimulatedBroker(dry_run_settings)  # type: ignore[arg-type]
        with pytest.raises(BrokerNotConnectedError, match="not connected"):
            broker.orders()

    def test_connect_and_shutdown_are_idempotent(self, broker: SimulatedBroker) -> None:
        broker.shutdown()
        broker.connect()
        broker.connect()
        assert broker.is_connected
        broker.shutdown()
        broker.shutdown()
        assert not broker.is_connected


class TestPricing:
    def test_the_spread_is_real(self, broker: SimulatedBroker) -> None:
        """A buy fills at the ask and a sell at the bid. Collapsing them to a mid price is
        how a backtest stops matching live fills."""
        quote = broker.tick("US30")
        assert quote is not None
        assert quote.ask > quote.bid

    def test_publishing_a_crossed_quote_is_refused(self, broker: SimulatedBroker) -> None:
        with pytest.raises(BrokerError, match="below bid"):
            broker.publish("US30", Decimal("40100.0"), Decimal("40000.0"), digits=1)

    def test_an_unpriced_symbol_has_no_tick(self, broker: SimulatedBroker) -> None:
        assert broker.tick("NOSUCH") is None


class TestPendingOrders:
    def test_a_buy_stop_rests_until_price_reaches_it(
        self, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        broker.place_order(make_intent(plan))

        assert len(broker.orders()) == 1
        assert broker.positions() == []

    def test_price_through_the_stop_fills_it(
        self, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        fill_through(broker, plan)

        positions = broker.positions()
        assert len(positions) == 1
        assert positions[0].side is Side.SIDE_BUY
        assert broker.orders() == [], "a filled order must leave the book"

    def test_a_filled_order_records_the_position_it_created(
        self, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        placed = broker.place_order(make_intent(plan))
        broker.publish("US30", Decimal("40010.0"), Decimal("40010.5"), digits=1)

        position = broker.positions()[0]
        assert position.source_order_ticket == placed.ticket
        assert position.client_tag == placed.client_tag

    def test_cancelling_removes_the_order_from_the_book(
        self, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        placed = broker.place_order(make_intent(plan))

        assert broker.cancel_order(placed.ticket) is True
        assert broker.orders() == []

    def test_cancelling_an_absent_order_succeeds(self, broker: SimulatedBroker) -> None:
        """The goal is that it is not on the book, and it is not."""
        assert broker.cancel_order(999999) is True


class TestValidation:
    def test_a_stop_on_the_wrong_side_of_entry_is_refused(
        self, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        """A BUY stop at or above the entry would not protect the position."""
        with pytest.raises(OrderValidationError, match="does not protect"):
            broker.place_order(make_intent(plan, stop_loss=Price.parse("40010.0", 1)))

    def test_a_volume_below_the_minimum_is_refused(
        self, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        with pytest.raises(BrokerRejectedError, match="outside"):
            broker.place_order(make_intent(plan, volume=Volume.of(Decimal("0.001"))))

    def test_an_unmodelled_symbol_is_refused(
        self, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        """Refusing beats approximating an instrument whose economics are unknown."""
        with pytest.raises(SymbolNotFoundError, match="does not model"):
            broker.place_order(make_intent(plan, symbol="SPX500"))


class TestProtectiveLevels:
    def test_the_take_profit_closes_the_position(
        self, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        fill_through(broker, plan)
        assert broker.positions()

        target = plan.take_profit.value
        broker.publish("US30", target, target + Decimal("0.5"), digits=1)

        assert broker.positions() == []

    def test_the_stop_loss_closes_the_position(
        self, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        fill_through(broker, plan)

        stop = plan.stop_loss.value
        broker.publish("US30", stop - Decimal("1"), stop - Decimal("0.5"), digits=1)

        assert broker.positions() == []

    def test_price_between_the_levels_leaves_the_position_open(
        self, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        """A protective level must not fire merely because price moved."""
        fill_through(broker, plan)

        broker.publish("US30", Decimal("40005.0"), Decimal("40005.5"), digits=1)

        assert len(broker.positions()) == 1

    def test_modifying_a_stop_is_reflected_in_the_position(
        self, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        fill_through(broker, plan)
        ticket = broker.positions()[0].ticket

        assert broker.modify_position(ticket, stop_loss=Price.parse("40005.0", 1)) is True
        assert broker.positions()[0].stop_loss == Price.parse("40005.0", 1)


class TestMoney:
    def test_a_winning_close_credits_the_balance(
        self, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        fill_through(broker, plan)
        position = broker.positions()[0]
        before = broker.account().balance.amount

        realised = broker.close(position.ticket, at=Decimal("40050.0"))

        assert realised.amount > 0
        assert broker.account().balance.amount == before + realised.amount

    def test_commission_makes_a_flat_trade_a_small_loss(
        self, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        """The whole reason commission is modelled: a flat trade is not free.

        Sizing adds the cost to the risk budget, so a dry run that ignored it would report
        equity the live account would never show.
        """
        fill_through(broker, plan)
        position = broker.positions()[0]
        before = broker.account().balance.amount

        # Exit at exactly the entry, so gross P/L is zero.
        realised = broker.close(position.ticket, at=plan.entry.value)

        assert realised.amount < 0, "a flat trade must lose the commission"
        assert broker.account().balance.amount == before + realised.amount

    def test_floating_profit_is_gross_and_excludes_commission(
        self, broker: SimulatedBroker, plan: TradePlan, us30: SymbolSpecification
    ) -> None:
        """Matches what a live terminal shows, and avoids charging commission twice."""
        fill_through(broker, plan)

        broker.publish("US30", Decimal("40015.0"), Decimal("40015.5"), digits=1)
        floating = broker.positions()[0].profit.amount

        expected = (
            (Decimal("40015.0") - plan.entry.value) * us30.contract_size * plan.volume.lots
        )
        assert floating == expected

    def test_equity_includes_open_positions(
        self, broker: SimulatedBroker, plan: TradePlan
    ) -> None:
        fill_through(broker, plan)
        before = broker.account().equity.amount

        broker.publish("US30", Decimal("40015.0"), Decimal("40015.5"), digits=1)

        assert broker.account().equity.amount > before

    def test_commission_mode_decides_the_number_of_charges(self) -> None:
        """PER_LOT_PER_SIDE doubles the cost, and that changes position size."""
        assert CommissionMode.PER_LOT_ROUND_TRIP.sides == 1
        assert CommissionMode.PER_LOT_PER_SIDE.sides == 2

    def test_per_side_commission_costs_twice_as_much(
        self, us30: SymbolSpecification, dry_run_settings: object
    ) -> None:
        """The same exit at the entry price, charged under each commission convention."""

        def realised_with(mode: CommissionMode) -> Decimal:
            venue.configure_specification(
                us30, commission=Decimal("6.0"), commission_mode=mode
            )
            broker = SimulatedBroker(dry_run_settings)  # type: ignore[arg-type]
            broker.connect()
            plan = _flat_plan()
            broker.publish("US30", plan.entry.value, plan.entry.value + Decimal("0.5"), digits=1)
            broker.place_order(make_intent(plan))
            position = broker.positions()[0]
            return broker.close(position.ticket, at=plan.entry.value).amount

        round_trip = realised_with(CommissionMode.PER_LOT_ROUND_TRIP)
        per_side = realised_with(CommissionMode.PER_LOT_PER_SIDE)

        assert round_trip < 0, "a flat trade is a loss under either convention"
        assert per_side < round_trip, "per-side doubles the charge"


def _flat_plan() -> TradePlan:
    """A minimal buy plan used only for commission comparison."""
    from stop_order_scalp.domain.enums import OrderKind, TargetMode
    from stop_order_scalp.domain.models import TradeSignal
    from stop_order_scalp.domain.value_objects import Money

    signal = TradeSignal(
        symbol="US30",
        side=Side.SIDE_BUY,
        timeframe="M1",
        direction_timeframe="M15",
        source_candle_open_time=NOW,
        direction_candle_open_time=NOW,
        order_kind=OrderKind.ORDER_KIND_BUY_STOP,
        reference_price=Price.parse("40020.0", 1),
    )
    entry = Price.parse("40020.0", 1)
    return TradePlan(
        plan_id="plan0000000001",
        signal=signal,
        entry=entry,
        stop_loss=Price.parse("40010.0", 1),
        take_profit=Price.parse("40040.0", 1),
        volume=Volume.of(Decimal("0.10")),
        price_risk=Money.of(1),
        commission=Money.of(Decimal("0.6")),
        total_risk=Money.of(Decimal("1.6")),
        target_mode=TargetMode.TARGET_MODE_RISK_REWARD,
        created_at=NOW,
    )


class TestClock:
    def test_server_time_comes_from_the_injected_clock(self, broker: SimulatedBroker) -> None:
        """Nothing here reads a wall clock, which is what makes a replay reproducible."""
        assert broker.server_time() == NOW


class TestVenueIsolation:
    def test_reset_clears_configured_instruments(self) -> None:
        """The venue's economics live in module state, so tests undo them."""
        spec = SymbolSpecification(
            name="TEST",
            digits=2,
            point=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            tick_value=Decimal("1"),
            contract_size=Decimal("100000"),
            volume_min=Decimal("0.01"),
            volume_max=Decimal("1"),
            volume_step=Decimal("0.01"),
        )
        venue.configure_specification(spec)
        assert "TEST" in venue._SPECS

        venue.reset_venue()

        assert "TEST" not in venue._SPECS

    def test_an_unmodelled_symbol_reports_not_found(
        self, broker: SimulatedBroker
    ) -> None:
        with pytest.raises(SymbolNotFoundError, match="does not model"):
            broker.specification("NOSUCH")
