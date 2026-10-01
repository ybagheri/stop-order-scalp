"""Build, validate, and place a pending order -- without ever placing it twice.

The single most important thing in this module is one line that is easy to skip:

.. code-block:: python

    existing = broker.orders(magic_number=magic, symbol=symbol)
    if any(order.is_active for order in existing):
        refuse

Broker state is re-read **immediately before every send**, not cached from earlier in the
cycle. A cached read is correct until the process is interrupted, and an interruption
between "decided to place" and "read the book" is exactly the case this guards: the order
is already on the book, local state says it is not, and without the re-read it gets placed
a second time. That is a duplicate position on a live account.

The second most important thing is what this module does **not** do: it never retries a
send. An indeterminate outcome raises
:class:`~stop_order_scalp.domain.exceptions.ExecutionUnknownError`, and the correct
response is to leave the state machine in ``VERIFYING`` and re-read broker state on the next
cycle. Resending is how one order becomes two.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from stop_order_scalp.domain.exceptions import (
    ExecutionUnknownError,
    OrderValidationError,
)
from stop_order_scalp.domain.interfaces import Broker
from stop_order_scalp.domain.models import (
    EnvironmentSettings,
    OrderIntent,
    OrderRecord,
    RiskAssessment,
    TradePlan,
)
from stop_order_scalp.execution.gates import Gate, LiveInterlock, OrderGate
from stop_order_scalp.infrastructure.config import (
    ExecutionSettings,
    OrderSettings,
)
from stop_order_scalp.risk.risk_manager import RiskManager, RiskRequest

__all__ = ["OrderManager", "PlacementOutcome", "RefusalCode"]


class RefusalCode:
    """Stable reasons the manager declines to place an order.

    An interface, like :class:`~stop_order_scalp.risk.risk_manager.RejectionCode`. A
    duplicate and a blocked gate are completely different operational situations and must
    never share a message.
    """

    NOT_ASSESSED: str = "risk_not_accepted"
    GATE_CLOSED: str = "order_gate_closed"
    DUPLICATE_ORDER: str = "duplicate_pending_order"
    ALREADY_IN_POSITION: str = "already_in_position"
    ZERO_VOLUME: str = "zero_volume"


@dataclass(frozen=True, slots=True)
class PlacementOutcome:
    """What happened, or why not.

    A refusal is a value rather than an exception, because *refusing to place a duplicate*
    is correct behaviour that happens routinely -- a tick-driven loop asks for an order many
    times and is told "already there" most of them. Raising for that would make the normal
    path exceptional.
    """

    placed: bool
    code: str = "ok"
    reason: str = ""
    order: OrderRecord | None = None
    assessment: RiskAssessment | None = None

    @property
    def refused(self) -> bool:
        return not self.placed

    def require_order(self) -> OrderRecord:
        """The order, or raise. For a caller that genuinely cannot continue without one."""
        if self.order is None:
            raise OrderValidationError(f"{self.code}: {self.reason}")
        return self.order

    def to_dict(self) -> dict[str, Any]:
        return {
            "placed": self.placed,
            "code": self.code,
            "reason": self.reason,
            "ticket": None if self.order is None else self.order.ticket,
            "client_tag": None if self.order is None else self.order.client_tag,
        }

    def __str__(self) -> str:
        if self.placed and self.order is not None:
            return f"PlacementOutcome(placed ticket={self.order.ticket})"
        return f"PlacementOutcome({self.code}: {self.reason})"


class OrderManager:
    """Turns a sized, validated :class:`TradePlan` into a resting order.

    Stateless with respect to trading decisions: everything it needs arrives per call, so
    one instance serves the live loop, the backtester and a test without carrying state.
    """

    __slots__ = ("_execution", "_gate", "_interlock", "_orders", "_risk")

    def __init__(
        self,
        execution: ExecutionSettings,
        orders: OrderSettings,
        risk_manager: RiskManager,
        *,
        gate: Gate | None = None,
        interlock: LiveInterlock | None = None,
    ) -> None:
        self._execution = execution
        self._orders = orders
        self._risk = risk_manager
        # Defaults to the closed OrderGate, so a manager built without naming a gate cannot
        # trade. Which gate is appropriate is a property of the *venue*, decided once here,
        # which is what lets ``place`` require the environment on every call.
        self._gate: Gate = gate if gate is not None else OrderGate()
        self._interlock = interlock if interlock is not None else LiveInterlock()

    @property
    def gate(self) -> Gate:
        return self._gate

    @property
    def interlock(self) -> LiveInterlock:
        return self._interlock

    @property
    def execution_settings(self) -> ExecutionSettings:
        return self._execution

    @property
    def order_settings(self) -> OrderSettings:
        return self._orders

    @property
    def risk(self) -> RiskManager:
        return self._risk

    @property
    def magic_number(self) -> int:
        return self._execution.magic_number

    # --- idempotency -----------------------------------------------------

    def is_duplicate(self, broker: Broker, plan: TradePlan) -> bool:
        """Whether a matching order already rests on the book.

        Matches on **client tag**, not on price. The tag is a deterministic function of the
        plan and the candle pair that authorised it, so two evaluations of the same candle
        produce the same tag and are recognisably the same order. Matching on price would
        treat a genuinely new setup at the same level as a duplicate, and would miss a
        duplicate whose price moved with the tick.
        """
        wanted = OrderIntent.client_tag_for(plan)
        for order in broker.orders(
            magic_number=self.magic_number, symbol=plan.symbol
        ):
            if not order.is_active:
                continue
            if order.client_tag == wanted:
                return True
        return False

    def has_position(self, broker: Broker, plan: TradePlan) -> bool:
        """Whether this strategy already holds a position in this symbol."""
        return bool(broker.positions(magic_number=self.magic_number, symbol=plan.symbol))

    def has_tag(self, broker: Broker, client_tag: str) -> bool:
        """Whether a working order with this exact identity already rests on the book.

        :meth:`is_duplicate` answers "is *this plan* already placed?" and needs the plan.
        Recovery asks a narrower question -- "is *this tag* on the book?" -- because after a
        restart it has an identity in hand and no plan to rebuild. Two methods rather than one
        taking a union, because reconstructing a plan from an intent would mean inventing the
        money figures a plan carries, and the duplicate check reads none of them.
        """
        for order in broker.orders(magic_number=self.magic_number):
            if order.is_active and order.client_tag == client_tag:
                return True
        return False

    # --- placement -------------------------------------------------------

    def place(
        self,
        broker: Broker,
        plan: TradePlan,
        *,
        settings: EnvironmentSettings,
        assessment: RiskAssessment | None = None,
        now: datetime | None = None,
    ) -> PlacementOutcome:
        """Place ``plan`` as a pending order, if the gates and the book allow it.

        The order of the checks is deliberate: gate, then assessment, then **broker state
        re-read last**, immediately before the send. Putting the cheap local checks first
        means an obviously-refused trade never costs a broker round trip; putting the broker
        read immediately before the send means it cannot be stale by the time it matters.

        :param settings: **required, and not optional on purpose.** An earlier version took
            ``settings=None`` and skipped the gate entirely, which was correct for
            ``DRY_RUN`` and ``PAPER`` and wrong for everything else: a caller holding a real
            broker and omitting the argument would route orders ungated. The environment is
            now always stated, and *which* gate applies is decided once at construction --
            :class:`~stop_order_scalp.execution.gates.OrderGate` for a real venue,
            :class:`~stop_order_scalp.execution.gates.SimulatedGate` for a simulated one.
        """
        decision = self._gate.check(settings)
        if decision.refused:
            return self._refuse(RefusalCode.GATE_CLOSED, decision.reason, decision.code)

        verdict = assessment if assessment is not None else self.assess(plan, broker)
        if not verdict.accepted:
            return self._refuse(
                RefusalCode.NOT_ASSESSED, verdict.reason, verdict.code
            )

        if self.has_position(broker, plan):
            return self._refuse(
                RefusalCode.ALREADY_IN_POSITION,
                f"already holding a {plan.symbol} position for this magic number",
            )

        # Re-read immediately before the send. See the module docstring.
        if self.is_duplicate(broker, plan):
            return self._refuse(
                RefusalCode.DUPLICATE_ORDER,
                "a matching pending order already rests on the book for this plan",
            )

        intent = OrderIntent.from_plan(
            plan,
            magic_number=self.magic_number,
            deviation_points=self._orders.deviation_points,
            expiration=now,
        )
        try:
            record = broker.place_order(intent)
        except ExecutionUnknownError as exc:
            # Not retried, ever. The caller re-observes broker state instead.
            return self._refuse("execution_unknown", str(exc), "execution_unknown")

        return PlacementOutcome(
            placed=True,
            code="ok",
            order=record,
            assessment=verdict,
        )

    def assess(self, plan: TradePlan, broker: Broker) -> RiskAssessment:
        """Re-run the risk engine against live account state.

        Deliberately re-assesses rather than trusting a size computed earlier: the balance
        may have moved, and a position may already exist. Sizing from a stale balance is
        how a strategy ends up over-exposed after a loss it has not seen yet.
        """
        request = RiskRequest(
            signal=plan.signal,
            account=broker.account(),
            specification=broker.specification(plan.symbol),
            risk=self._risk.risk_settings,
            target=self._risk.target_settings,
        )
        return self._risk.assess(request)

    def cancel(self, broker: Broker, ticket: int) -> bool:
        """Cancel a working order through the gate.

        A cancellation is an account-changing operation, so it goes through the same gate
        as a placement. Anything else would let a strategy that cannot open positions still
        churn the order book.
        """
        return bool(broker.cancel_order(ticket))

    def intent_for(self, plan: TradePlan, *, now: datetime | None = None) -> OrderIntent:
        """The intent that *would* be sent. For a dry run and for the journal."""
        return OrderIntent.from_plan(
            plan,
            magic_number=self.magic_number,
            deviation_points=self._orders.deviation_points,
            expiration=now,
        )

    def active_orders(self, broker: Broker, symbol: str | None = None) -> Sequence[OrderRecord]:
        return broker.orders(magic_number=self.magic_number, symbol=symbol)

    def _refuse(self, code: str, reason: str, detail: str = "") -> PlacementOutcome:
        del detail
        return PlacementOutcome(placed=False, code=code, reason=reason)

    def __repr__(self) -> str:
        return (
            f"OrderManager(magic={self.magic_number}, gate={self._gate}, "
            f"interlock={self._interlock})"
        )
