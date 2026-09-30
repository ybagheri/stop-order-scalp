"""Enumerations shared across layers.

All members derive from :class:`enum.StrEnum` so that they serialise to readable JSON
without a custom encoder, and so that a persisted state file survives a rename of the
Python class name.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "BreakEventKind",
    "BreakEvenMode",
    "CommissionMode",
    "Environment",
    "ExecutionMode",
    "LifecycleState",
    "OrderKind",
    "PositionState",
    "RiskMode",
    "Side",
    "TargetMode",
    "TimeframeSelection",
    "TradeEventKind",
]


class Side(StrEnum):
    """Direction of a position or order.

    ``SIDE_BUY`` and ``SIDE_SELL`` mirror MetaTrader 5's ``ORDER_TYPE_BUY`` /
    ``ORDER_TYPE_SELL``. The naming follows MQL5 so that translating to and from the
    terminal is mechanical and auditable, rather than relying on a mapping table that
    can fall out of step.
    """

    SIDE_BUY = "BUY"
    SIDE_SELL = "SELL"

    @property
    def opposite(self) -> Side:
        """The side that closes a position of this side."""
        return Side.SIDE_SELL if self is Side.SIDE_BUY else Side.SIDE_BUY

    def __invert__(self) -> Side:
        return self.opposite


class OrderKind(StrEnum):
    """What an order does when it fills.

    The strategy uses only pending stop orders. Market orders are present because
    ``Broker`` is a general abstraction and the simulated broker models fills generally;
    the strategy layer never requests one.
    """

    ORDER_KIND_BUY_STOP = "BUY_STOP"
    ORDER_KIND_SELL_STOP = "SELL_STOP"
    ORDER_KIND_BUY_LIMIT = "BUY_LIMIT"
    ORDER_KIND_SELL_LIMIT = "SELL_LIMIT"
    ORDER_KIND_MARKET_BUY = "MARKET_BUY"
    ORDER_KIND_MARKET_SELL = "MARKET_SELL"

    @property
    def side(self) -> Side:
        """The side that the resulting position would have."""
        if self in (
            OrderKind.ORDER_KIND_BUY_STOP,
            OrderKind.ORDER_KIND_BUY_LIMIT,
            OrderKind.ORDER_KIND_MARKET_BUY,
        ):
            return Side.SIDE_BUY
        return Side.SIDE_SELL

    @property
    def is_pending(self) -> bool:
        """Whether the order rests on the book before it triggers."""
        return self in (
            OrderKind.ORDER_KIND_BUY_STOP,
            OrderKind.ORDER_KIND_SELL_STOP,
            OrderKind.ORDER_KIND_BUY_LIMIT,
            OrderKind.ORDER_KIND_SELL_LIMIT,
        )

    @property
    def is_stop(self) -> bool:
        """Whether the order triggers in the direction of the position."""
        return self in (OrderKind.ORDER_KIND_BUY_STOP, OrderKind.ORDER_KIND_SELL_STOP)

    @property
    def entry_kind(self) -> OrderKind:
        """This order expressed as the pending stop order the strategy places.

        Used by the simulator and the lifecycle to compare "the order I intended" with
        "the order on the broker" without caring which spelling each uses.
        """
        if self.side is Side.SIDE_BUY:
            return OrderKind.ORDER_KIND_BUY_STOP
        return OrderKind.ORDER_KIND_SELL_STOP


class RiskMode(StrEnum):
    """How position volume is chosen."""

    RISK_MODE_PERCENT_BALANCE = "percent_balance"
    RISK_MODE_FIXED_LOT = "fixed_lot"


class TargetMode(StrEnum):
    """How the take profit is derived.

    Precedence when a signal itself supplies a target is defined in
    ``docs/strategy/BASELINE.md``: ``signal_defined`` wins, then ``risk_reward``, then
    ``fixed_points``. The mode is a *preference*, not an override of a better answer.
    """

    TARGET_MODE_FIXED_POINTS = "fixed_points"
    TARGET_MODE_RISK_REWARD = "risk_reward"
    TARGET_MODE_SIGNAL_DEFINED = "signal_defined"


class CommissionMode(StrEnum):
    """Whether the configured commission figure covers one side or the round trip.

    This must never be guessed. ``COMMISSION_PER_LOT`` in the configuration is
    interpreted according to this value, and the interpretation changes position size.
    The baseline treats $6/lot as ``PER_LOT_ROUND_TRIP``, which is the convention the
    strategy specification was written against.
    """

    PER_LOT_ROUND_TRIP = "per_lot_round_trip"
    PER_LOT_PER_SIDE = "per_lot_per_side"

    @property
    def sides(self) -> int:
        """How many commission charges one lot incurs."""
        return 2 if self is CommissionMode.PER_LOT_PER_SIDE else 1


class BreakEvenMode(StrEnum):
    """Where the break-even stop is placed."""

    #: SL moves exactly to the entry price.
    BREAK_EVEN_MODE_ENTRY = "entry"
    #: SL moves to the entry price plus the estimated round-trip cost, so that a
    #: position closed at the break-even price is not a net loss.
    BREAK_EVEN_MODE_COMMISSION_AWARE = "commission_aware"


class Environment(StrEnum):
    """Execution environment.

    Ordered from safest to most dangerous. Transitions are one-way in practice:
    ``DRY_RUN`` -> ``PAPER`` -> ``DEMO`` -> ``LIVE``. Nothing in the codebase moves
    the system forward automatically.
    """

    DRY_RUN = "DRY_RUN"
    PAPER = "PAPER"
    DEMO = "DEMO"
    LIVE = "LIVE"

    @property
    def is_real(self) -> bool:
        """Whether orders in this environment reach a broker's matching engine."""
        return self is Environment.LIVE


class ExecutionMode(StrEnum):
    """How the process was started.

    Distinct from :class:`Environment`: a ``DRY_RUN`` process executes the *whole*
    pipeline against the simulated broker, whereas a ``PAPER`` process watches live
    prices and decides, but routes nothing.
    """

    MODE_RUN = "run"
    MODE_DRY_RUN = "dry_run"
    MODE_BACKTEST = "backtest"


class TimeframeSelection(StrEnum):
    """Which candle supplies the directional or entry input.

    ``LAST_CLOSED`` is the default and the only look-ahead-free option for a strategy
    that decides before acting. ``CURRENT_FORMING`` exists for research and for
    operators who knowingly accept repainting; it must be enabled explicitly.
    """

    LAST_CLOSED = "last_closed"
    CURRENT_FORMING = "current_forming"


class PositionState(StrEnum):
    """Lifecycle of a single trade.

    A distinct vocabulary from :class:`LifecycleState`: this describes one trade from
    intent to close, whereas ``LifecycleState`` describes what the *system* is doing,
    which includes waiting for a signal when nothing is in flight.
    """

    POSITION_PLANNED = "planned"
    POSITION_PENDING_ORDER = "pending_order"
    POSITION_TRIGGERED = "triggered"
    POSITION_OPEN = "open"
    POSITION_BREAK_EVEN_ARMED = "break_even_armed"
    POSITION_TRAILING = "trailing"
    POSITION_CLOSED = "closed"
    POSITION_CANCELLED = "cancelled"
    POSITION_FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """Whether no further transition is expected from this state."""
        return self in (
            PositionState.POSITION_CLOSED,
            PositionState.POSITION_CANCELLED,
            PositionState.POSITION_FAILED,
        )

    @property
    def is_active(self) -> bool:
        """Whether broker state must exist for this state to be coherent."""
        return self in (
            PositionState.POSITION_PENDING_ORDER,
            PositionState.POSITION_OPEN,
            PositionState.POSITION_BREAK_EVEN_ARMED,
            PositionState.POSITION_TRAILING,
        )


class LifecycleState(StrEnum):
    """What the trading system is doing right now.

    The transitions between these are declared in
    ``lifecycle.state_machine.TRANSITIONS``. Any edge not in that table raises
    :class:`~stop_order_scalp.domain.exceptions.IllegalTransitionError`, which is what
    keeps the orchestration from becoming spaghetti.
    """

    STATE_IDLE = "idle"
    STATE_WAITING_FOR_SIGNAL = "waiting_for_signal"
    STATE_SIGNAL_DETECTED = "signal_detected"
    STATE_VALIDATING = "validating"
    STATE_PENDING_ORDER_PLACED = "pending_order_placed"
    STATE_WAITING_FOR_TRIGGER = "waiting_for_trigger"
    STATE_POSITION_OPEN = "position_open"
    STATE_BREAK_EVEN_ARMED = "break_even_armed"
    STATE_TRAILING = "trailing"
    STATE_POSITION_CLOSED = "position_closed"
    STATE_RECONCILING = "reconciling"
    STATE_VERIFYING = "verifying"
    STATE_BLOCKED = "blocked"
    STATE_HALTED = "halted"

    @property
    def is_trading_state(self) -> bool:
        """Whether the system currently owns broker exposure or an intent to."""
        return self in (
            LifecycleState.STATE_PENDING_ORDER_PLACED,
            LifecycleState.STATE_WAITING_FOR_TRIGGER,
            LifecycleState.STATE_POSITION_OPEN,
            LifecycleState.STATE_BREAK_EVEN_ARMED,
            LifecycleState.STATE_TRAILING,
        )

    @property
    def is_recoverable(self) -> bool:
        """Whether restarting the process from this state is meaningful."""
        return not self.is_terminal and self is not LifecycleState.STATE_HALTED


class BreakEventKind(StrEnum):
    """Why a managed stop loss moved."""

    BREAK_EVENT_INITIAL = "initial"
    BREAK_EVENT_BREAK_EVEN = "break_even"
    BREAK_EVENT_TRAILING = "trailing"
    BREAK_EVENT_BROKER_ADJUSTED = "broker_adjusted"


class TradeEventKind(StrEnum):
    """Journal entries.

    The journal is the operator's record of what the system did and why. It is written
    from the same code paths that make decisions, not reconstructed afterwards.
    """

    TRADE_EVENT_SIGNAL_ACCEPTED = "signal_accepted"
    TRADE_EVENT_SIGNAL_REJECTED = "signal_rejected"
    TRADE_EVENT_PLAN_BUILT = "plan_built"
    TRADE_EVENT_ORDER_PLACED = "order_placed"
    TRADE_EVENT_ORDER_REJECTED = "order_rejected"
    TRADE_EVENT_ORDER_CANCELLED = "order_cancelled"
    TRADE_EVENT_ORDER_EXPIRED = "order_expired"
    TRADE_EVENT_POSITION_OPENED = "position_opened"
    TRADE_EVENT_SL_MODIFIED = "sl_modified"
    TRADE_EVENT_BREAK_EVEN = "break_even"
    TRADE_EVENT_TRAILING = "trailing"
    TRADE_EVENT_POSITION_CLOSED = "position_closed"
    TRADE_EVENT_REJECTED_DUPLICATE = "duplicate_suppressed"
    TRADE_EVENT_RECONCILED = "reconciled"
    TRADE_EVENT_HALTED = "halted"