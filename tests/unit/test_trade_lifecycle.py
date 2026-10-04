"""The loop: record intent, re-read, send once, settle.

The cases that earn their keep are the ones where a duplicate order could plausibly appear:
a second tick with the same signal, a reconnect, a restart with an unresolved intent, and a
retry after an unknown send outcome. Each gets its own test, because each is a different
mechanism preventing the same catastrophe.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from stop_order_scalp.domain.enums import LifecycleState, OrderKind, Side, TradeEventKind
from stop_order_scalp.domain.exceptions import ExecutionUnknownError, StaleStateError
from stop_order_scalp.domain.models import (
    OrderIntent,
    OrderRecord,
    PositionRecord,
    RiskAssessment,
    TradePlan,
    TradeSignal,
)
from stop_order_scalp.domain.value_objects import Money, Price, Volume
from stop_order_scalp.infrastructure.clock import FixedClock
from stop_order_scalp.infrastructure.persistence import IntentOutcome, LedgerEntry, StateLedger
from stop_order_scalp.lifecycle.trade_lifecycle import TradeLifecycle

NOW = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
MAGIC = 20260930


def plan(side: Side = Side.SIDE_BUY, plan_id: str = "US30-M15-1-M1-1") -> TradePlan:
    """A real plan, built by hand but with the strategy's own shape."""
    kind = (
        OrderKind.ORDER_KIND_BUY_STOP
        if side is Side.SIDE_BUY
        else OrderKind.ORDER_KIND_SELL_STOP
    )
    entry = Price.parse("40000.0", 1)
    signal = TradeSignal(
        symbol="US30",
        side=side,
        timeframe="M1",
        direction_timeframe="M15",
        source_candle_open_time=NOW,
        direction_candle_open_time=NOW,
        order_kind=kind,
        reference_price=entry,
    )
    return TradePlan(
        plan_id=plan_id,
        signal=signal,
        entry=entry,
        stop_loss=Price.parse("39990.0", 1),
        take_profit=Price.parse("40020.0", 1),
        volume=Volume.of(Decimal("0.4")),
        price_risk=Money.of(100),
        commission=Money.of(Decimal("2.4")),
        total_risk=Money.of(Decimal("102.4")),
        target_mode=None,  # type: ignore[arg-type]
        created_at=NOW,
    )


def order(ticket: int = 555, tag: str = "abc123def456abc123def4") -> OrderRecord:
    return OrderRecord(
        ticket=ticket,
        client_tag=tag,
        symbol="US30",
        kind=OrderKind.ORDER_KIND_BUY_STOP,
        volume=Volume.of(Decimal("0.4")),
        entry=Price.parse("40000.0", 1),
        stop_loss=Price.parse("39990.0", 1),
        take_profit=Price.parse("40020.0", 1),
        magic_number=MAGIC,
        comment="",
        placed_at=NOW,
    )


def position(ticket: int = 900, tag: str = "abc123def456abc123def4") -> PositionRecord:
    return PositionRecord(
        ticket=ticket,
        symbol="US30",
        side=Side.SIDE_BUY,
        volume=Volume.of(Decimal("0.4")),
        entry=Price.parse("40000.0", 1),
        stop_loss=Price.parse("39995.0", 1),
        take_profit=Price.parse("40020.0", 1),
        magic_number=MAGIC,
        comment="",
        opened_at=NOW,
        profit=Money.of(50),
        client_tag=tag,
    )


class FakeBroker:
    """A book with no opinion of its own, so every test drives it explicitly."""

    def __init__(
        self,
        *,
        orders: list[OrderRecord] | None = None,
        positions: list[PositionRecord] | None = None,
    ) -> None:
        self.orders_on_book = list(orders or [])
        self.positions_open = list(positions or [])
        self.sends: list[OrderIntent] = []
        self.cancels: list[int] = []
        self.send_error: Exception | None = None
        self._next_ticket = 555

    def orders(self, **_: Any) -> list[OrderRecord]:
        return [r for r in self.orders_on_book if r.is_active]

    def positions(self, **_: Any) -> list[PositionRecord]:
        return list(self.positions_open)

    def place_order(self, intent: OrderIntent) -> OrderRecord:
        self.sends.append(intent)
        if self.send_error is not None:
            raise self.send_error
        self._next_ticket += 1
        record = order(self._next_ticket, intent.client_tag)
        self.orders_on_book.append(record)
        return record

    def cancel_order(self, ticket: int) -> bool:
        self.cancels.append(ticket)
        self.orders_on_book = [r for r in self.orders_on_book if r.ticket != ticket]
        return True

    def modify_position(self, ticket: int, **_: Any) -> bool:
        return True

    # --- the rest of the Broker protocol ---------------------------------
    #
    # Present so this fake satisfies ``Broker`` structurally. The lifecycle never calls them;
    # they exist because an incomplete fake would make the type checker report the *fake* as
    # wrong rather than the code under test.

    def connect(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    @property
    def is_connected(self) -> bool:
        return True

    def specification(self, symbol: str) -> Any:
        raise NotImplementedError

    def account(self) -> Any:
        raise NotImplementedError

    def symbol_available(self, symbol: str) -> bool:
        return True

    def server_time(self) -> datetime:
        return NOW

    def leverage_for(self, symbol: str) -> Decimal:
        del symbol
        return Decimal(100)

    def stream_ticks(self, symbol: str) -> Any:
        raise NotImplementedError

    def position_by_ticket(self, ticket: int) -> PositionRecord | None:
        for held in self.positions_open:
            if held.ticket == ticket:
                return held
        return None


class FakeOrderManager:
    """Only the surface the lifecycle uses, including the tag-based duplicate check."""

    def __init__(self, *, gate: Any = None) -> None:
        self.gate = gate if gate is not None else _OpenGate()
        self.magic_number = MAGIC

    def intent_for(self, plan: TradePlan, *, now: datetime | None = None) -> OrderIntent:
        return OrderIntent.from_plan(plan, magic_number=MAGIC, deviation_points=20)

    def has_tag(self, broker: Any, client_tag: str) -> bool:
        return any(
            r.is_active and r.client_tag == client_tag for r in broker.orders()
        )


class _OpenGate:
    #: These tests use a fake broker that cannot lose money. A gate that does not say so is
    #: refused when the lifecycle has no environment to judge it by -- see ``_gate_refusal``.
    simulated = True

    def check(self, settings: Any = None) -> Any:
        return type("D", (), {"open": True, "refused": False, "code": "", "reason": ""})()


class _ClosedGate:
    simulated = False

    def check(self, settings: Any = None) -> Any:
        return type(
            "D",
            (),
            {"open": False, "refused": True, "code": "gate_disabled", "reason": "off"},
        )()


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


def build(
    ledger_path: Path,
    broker: FakeBroker,
    *,
    order_manager: Any = None,
) -> TradeLifecycle:
    return TradeLifecycle(
        broker,
        StateLedger.load(ledger_path),
        order_manager if order_manager is not None else FakeOrderManager(),
        clock=FixedClock(NOW),
        magic_number=MAGIC,
        symbol="US30",
    )


def _ledger_knowing(ledger_path: Path, tag: str | None = None) -> StateLedger:
    """A ledger that already holds a settled entry for ``tag``.

    Reconciliation refuses exposure it cannot attribute, so a test that puts a tagged position
    on the book must first say this system put it there.
    """
    ledger = StateLedger.load(ledger_path)
    ledger.record(
        LedgerEntry(
            client_tag=tag or _tag_of(plan()),
            intent=OrderIntent.from_plan(plan(), magic_number=MAGIC, deviation_points=20),
            recorded_at=NOW,
        )
    )
    ledger.settle(tag or _tag_of(plan()), IntentOutcome.PLACED, ticket=555)
    return ledger


class TestWriteIntentBeforeSend:
    def test_the_intent_is_on_disk_before_the_order_is_sent(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """The ordering that closes the crash window.

        Checked by inspecting the disk from inside ``place_order``'s send, which is the only
        moment where the ordering actually matters.
        """
        observed: dict[str, int] = {}
        lifecycle = build(ledger_path, broker)
        original = broker.place_order

        def spy(intent: OrderIntent) -> OrderRecord:
            observed["entries"] = len(StateLedger.load(ledger_path))
            return original(intent)

        broker.place_order = spy  # type: ignore[method-assign]
        lifecycle.place_order(plan())

        assert observed["entries"] == 1, "the intent must be durable before the send"

    def test_a_successful_placement_settles_the_entry(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        lifecycle = build(ledger_path, broker)

        step = lifecycle.place_order(plan())

        assert step.actions == ("placed",)
        entry = StateLedger.load(ledger_path).get(_tag_of(plan()))
        assert entry is not None
        assert entry.outcome == IntentOutcome.PLACED
        assert entry.ticket is not None

    def test_the_state_reaches_waiting_for_trigger(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        lifecycle = build(ledger_path, broker)

        lifecycle.place_order(plan())

        assert lifecycle.state is LifecycleState.STATE_WAITING_FOR_TRIGGER

    def test_placement_journals_the_order(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        lifecycle = build(ledger_path, broker)

        step = lifecycle.place_order(plan())

        assert TradeEventKind.TRADE_EVENT_ORDER_PLACED in step.events


class TestUnknownOutcome:
    def test_it_is_recorded_as_unknown_and_never_resent(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """The property the whole phase exists for."""
        broker.send_error = ExecutionUnknownError("the terminal lost track")
        lifecycle = build(ledger_path, broker)

        step = lifecycle.place_order(plan())

        assert step.actions == ("execution_unknown",)
        assert len(broker.sends) == 1
        entry = StateLedger.load(ledger_path).get(_tag_of(plan()))
        assert entry is not None
        assert entry.outcome == IntentOutcome.UNKNOWN

    def test_it_moves_to_verifying(self, ledger_path: Path, broker: FakeBroker) -> None:
        """VERIFYING's only exits require broker state to have been read."""
        broker.send_error = ExecutionUnknownError("lost")
        lifecycle = build(ledger_path, broker)

        lifecycle.place_order(plan())

        assert lifecycle.state is LifecycleState.STATE_VERIFYING

    def test_verifying_cannot_send_again(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """The state machine, not a convention, is what stops the second send."""
        broker.send_error = ExecutionUnknownError("lost")
        lifecycle = build(ledger_path, broker)
        lifecycle.place_order(plan())

        step = lifecycle.place_order(plan())

        assert len(broker.sends) == 1, "the order was sent twice"
        assert "busy" in step.actions or "already_recorded" in step.actions


class TestDuplicateTicks:
    """Four mechanisms preventing the same catastrophe, each catching a different cause.

    ==========================  =========================================  =============
    mechanism                   caught by                                reported as
    ==========================  =========================================  =============
    a resting order             a duplicate tick, mid-session              ``busy``
    a ledger entry              a restart, or a prior cancelled attempt    ``already_recorded``
    an order on the book        a resting order with no ledger record      ``duplicate``
    an open position            anything at all                           ``busy``
    ==========================  =========================================  =============

    ``busy`` shadows the identity checks while exposure exists, which is intended: the refusal
    is the same and the reason is more accurate.
    """

    def test_the_same_plan_twice_sends_once(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        lifecycle = build(ledger_path, broker)

        first = lifecycle.place_order(plan())
        second = lifecycle.place_order(plan())

        assert first.actions == ("placed",)
        assert second.actions == ("busy",)
        assert len(broker.sends) == 1

    def test_a_duplicate_is_refused_even_though_the_ledger_also_knows(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        lifecycle = build(ledger_path, broker)
        lifecycle.place_order(plan())

        lifecycle.place_order(plan())

        assert len(broker.sends) == 1, "the order was sent twice"

    def test_a_restarted_process_refuses_on_the_ledger(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """The cross-restart half: local trading state is gone, the ledger is not."""
        build(ledger_path, broker).place_order(plan())

        second = build(ledger_path, broker)
        step = second.place_order(plan())

        assert step.actions == ("already_recorded",)
        assert len(broker.sends) == 1

    def test_an_order_on_the_book_with_no_ledger_record_is_a_duplicate(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """A fresh ledger, but the broker already holds this identity.

        The ledger check cannot catch it -- there is no entry -- so the book check must.
        """
        broker.orders_on_book = [order(tag=_tag_of(plan()))]
        lifecycle = build(ledger_path, broker)
        lifecycle.machine.reset(LifecycleState.STATE_RECONCILING)

        step = lifecycle.place_order(plan())

        assert step.actions == ("duplicate",)
        assert broker.sends == []

    def test_a_duplicate_is_journalled_as_a_duplicate(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        broker.orders_on_book = [order(tag=_tag_of(plan()))]
        lifecycle = build(ledger_path, broker)
        lifecycle.machine.reset(LifecycleState.STATE_RECONCILING)

        step = lifecycle.place_order(plan())

        assert TradeEventKind.TRADE_EVENT_REJECTED_DUPLICATE in step.events

    def test_a_duplicate_leaves_no_unresolved_intent(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """A suppressed duplicate is settled, so recovery has no phantom question."""
        broker.orders_on_book = [order(tag=_tag_of(plan()))]
        lifecycle = build(ledger_path, broker)
        lifecycle.machine.reset(LifecycleState.STATE_RECONCILING)

        lifecycle.place_order(plan())

        assert StateLedger.load(ledger_path).unresolved() == ()

    def test_a_different_candle_is_not_a_duplicate(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """Identity includes the authorising candle, so a new candle is a new trade.

        Placed from a reconciling state, which is quiet, so the identity checks -- not the
        busy guard -- are what decide.
        """
        lifecycle = build(ledger_path, broker)
        lifecycle.machine.reset(LifecycleState.STATE_RECONCILING)

        step = lifecycle.place_order(plan(plan_id="US30-M15-2-M1-2"))

        assert step.actions == ("placed",)


class TestDuplicateAcrossRestart:
    def test_an_unresolved_intent_blocks_a_resend_after_restart(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """The crash-and-restart case, stated as the property it is.

        The first process wrote the intent and died before hearing anything. The second
        process must not send again, because the first send may have landed.
        """
        first = build(ledger_path, broker)
        broker.send_error = ExecutionUnknownError("process died")
        first.place_order(plan())
        broker.send_error = None

        # A fresh process, same ledger, same broker.
        second = build(ledger_path, broker)
        step = second.place_order(plan())

        assert len(broker.sends) == 1
        assert step.actions[0] in ("busy", "duplicate", "already_recorded")

    def test_recovery_settles_an_unresolved_intent_found_on_the_book(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        broker.send_error = ExecutionUnknownError("died")
        build(ledger_path, broker).place_order(plan())
        # The order did land, despite the error -- which is exactly the case recovery exists
        # for, and exactly why the fake has to be told rather than inferred.
        broker.send_error = None
        broker.orders_on_book = [order(tag=_tag_of(plan()))]

        build(ledger_path, broker).recover()

        entry = StateLedger.load(ledger_path).get(_tag_of(plan()))
        assert entry is not None
        assert entry.outcome == IntentOutcome.PLACED

    def test_recovery_adopts_a_position_the_broker_filled(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """The order filled while the process was down; the position carries its tag.

        Reconciliation refuses exposure it cannot attribute, so the ledger must know about the
        tag first -- which it does, because the send was recorded before it ever happened.
        """
        broker.send_error = ExecutionUnknownError("died")
        build(ledger_path, broker).place_order(plan())
        broker.send_error = None
        broker.orders_on_book = []
        broker.positions_open = [position(tag=_tag_of(plan()))]

        lifecycle = build(ledger_path, broker)
        lifecycle.recover()

        assert lifecycle.state is LifecycleState.STATE_POSITION_OPEN

    def test_a_contradiction_stops_the_startup(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """Unattributable exposure must stop the system, not be adopted."""
        broker.positions_open = [position(tag="someoneelse12345678")]

        with pytest.raises(StaleStateError):
            build(ledger_path, broker).recover()


class TestGateBeforeLedger:
    def test_a_refused_gate_leaves_no_ledger_trace(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """Otherwise recovery would "resolve" an intent for a trade that never existed."""
        lifecycle = build(ledger_path, broker, order_manager=FakeOrderManager(gate=_ClosedGate()))

        step = lifecycle.place_order(plan(), settings=object())

        assert step.actions == ("gate_refused",)
        assert len(StateLedger.load(ledger_path)) == 0
        assert broker.sends == []

    def test_an_open_gate_allows_the_send(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        lifecycle = build(ledger_path, broker)

        step = lifecycle.place_order(plan(), settings=object())

        assert step.actions == ("placed",)

    def test_a_rejected_assessment_is_refused(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """A caller's risk verdict is respected, not quietly re-derived."""
        lifecycle = build(ledger_path, broker)
        rejected = RiskAssessment.reject("no_money", "not enough free margin")

        step = lifecycle.place_order(plan(), assessment=rejected)

        assert step.actions == ("risk_refused",)
        assert broker.sends == []


class TestBusyRefusal:
    def test_a_resting_order_blocks_a_second_placement(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        lifecycle = build(ledger_path, broker)
        lifecycle.place_order(plan())

        step = lifecycle.place_order(plan(plan_id="US30-M15-9-M1-9"))

        assert step.actions == ("busy",)
        assert len(broker.sends) == 1

    def test_a_halted_system_refuses_to_place(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        lifecycle = build(ledger_path, broker)
        lifecycle.machine.reset(LifecycleState.STATE_HALTED)

        step = lifecycle.place_order(plan())

        assert step.actions == ("halted",)
        assert broker.sends == []


class TestCloseAndReplacement:
    def _managing_a_position(
        self, ledger_path: Path, broker: FakeBroker
    ) -> TradeLifecycle:
        """A lifecycle that is genuinely managing a position.

        The machine reaches ``POSITION_OPEN`` through ``observe_fills`` rather than by reset,
        so the close that follows is detected from a state that really held the exposure --
        otherwise the test asserts on a machine that never held anything.
        """
        broker.positions_open = [position()]
        lifecycle = build(ledger_path, broker)
        lifecycle.observe_fills()
        # Asserted here rather than in each caller: the point of the helper is that the machine
        # really is managing the position, and narrowing the type here keeps the checker's
        # knowledge of `state` honest for the tests that follow.
        assert lifecycle.state is LifecycleState.STATE_POSITION_OPEN
        return lifecycle

    def test_a_disappeared_position_is_detected_as_a_close(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """Close detection is a comparison, because the venue does not announce closes."""
        lifecycle = self._managing_a_position(ledger_path, broker)

        broker.positions_open = []
        step = lifecycle.tick()

        assert any(action.startswith("closed:") for action in step.actions)
        assert str(lifecycle.state) == str(LifecycleState.STATE_POSITION_CLOSED)

    def test_a_close_is_journalled(self, ledger_path: Path, broker: FakeBroker) -> None:
        lifecycle = self._managing_a_position(ledger_path, broker)
        broker.positions_open = []

        step = lifecycle.tick()

        assert TradeEventKind.TRADE_EVENT_POSITION_CLOSED in step.events

    def test_no_close_is_detected_while_the_position_remains(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        lifecycle = self._managing_a_position(ledger_path, broker)

        step = lifecycle.tick()

        assert not any(action.startswith("closed:") for action in step.actions)

    def test_a_replacement_goes_through_full_risk_evaluation(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """The specification's replacement path, as the same code as a first entry.

        A close is not evidence that a new trade is warranted, so the replacement is placed
        only when a fresh decision arrives -- and it is placed through ``place_order``, which
        records the intent, re-reads the book and passes the gate, exactly as a first entry.
        """
        lifecycle = self._managing_a_position(ledger_path, broker)
        broker.positions_open = []

        idle = lifecycle.tick()
        assert broker.sends == []
        assert any("awaiting a fresh decision" in note for note in idle.notes)

        step = lifecycle.tick(plan(plan_id="US30-M15-5-M1-5"))

        assert step.actions == ("placed",)
        assert len(broker.sends) == 1

    def test_a_replacement_is_not_a_copy_of_the_previous_plan(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """The replacement is built from the *new* decision, not remembered from the last."""
        lifecycle = self._managing_a_position(ledger_path, broker)
        broker.positions_open = []

        lifecycle.tick()
        lifecycle.tick(plan(plan_id="US30-M15-5-M1-5"))

        assert broker.sends[0].client_tag == _tag_of(plan(plan_id="US30-M15-5-M1-5"))


class TestNoTrade:
    def test_a_non_plan_decision_is_a_no_trade_not_an_error(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """The strategy declining is the expected outcome most of the time."""
        lifecycle = build(ledger_path, broker)

        step = lifecycle.tick(object())

        assert step.actions == ("no_trade",)
        assert broker.sends == []

    def test_it_returns_to_waiting_for_signal(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        lifecycle = build(ledger_path, broker)

        lifecycle.tick(object())

        assert lifecycle.state is LifecycleState.STATE_WAITING_FOR_SIGNAL


class TestObserveFills:
    def test_a_new_position_is_adopted(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """A pending order can fill without this system being told."""
        lifecycle = build(ledger_path, broker)
        broker.positions_open = [position()]

        step = lifecycle.observe_fills()

        assert step.actions == ("opened:900",)
        assert lifecycle.state is LifecycleState.STATE_POSITION_OPEN

    def test_nothing_happens_when_the_book_is_unchanged(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        broker.positions_open = [position()]
        lifecycle = build(ledger_path, broker)
        lifecycle.observe_fills()

        step = lifecycle.observe_fills()

        assert step.idle


class TestCancellation:
    def test_a_cancel_is_journalled(self, ledger_path: Path, broker: FakeBroker) -> None:
        lifecycle = build(ledger_path, broker)
        lifecycle.place_order(plan())

        step = lifecycle.cancel_pending(555)

        assert step.actions == ("cancelled",)
        assert TradeEventKind.TRADE_EVENT_ORDER_CANCELLED in step.events

    def test_an_unknown_cancel_is_not_resent(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """The goal is that the order is not on the book; that is a question, not an action."""
        lifecycle = build(ledger_path, broker)

        def explode(ticket: int) -> bool:
            raise ExecutionUnknownError("lost")

        broker.cancel_order = explode  # type: ignore[method-assign]
        step = lifecycle.cancel_pending(555)

        assert step.actions == ("cancel_unknown",)
        assert broker.cancels == []


class TestClockRequired:
    def test_placing_without_a_clock_is_refused_explicitly(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        """No silent wall-clock fallback: a ledger timestamp must be reproducible."""
        lifecycle = TradeLifecycle(
            broker,
            StateLedger.load(ledger_path),
            FakeOrderManager(),
            magic_number=MAGIC,
            symbol="US30",
        )

        with pytest.raises(Exception, match="needs a Clock"):
            lifecycle.place_order(plan())


class TestIdleIsNormal:
    def test_an_unchanged_book_produces_an_idle_step(
        self, ledger_path: Path, broker: FakeBroker
    ) -> None:
        lifecycle = build(ledger_path, broker)

        step = lifecycle.tick()

        assert step.idle


def _tag_of(plan: TradePlan) -> str:
    return OrderIntent.client_tag_for(plan)
