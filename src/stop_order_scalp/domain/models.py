"""Domain models.

Every model is a frozen dataclass. Nothing here performs I/O, reads the clock, or knows
that MetaTrader 5 exists. Anything that changes is replaced, not mutated, so a value
handed to another layer cannot be altered underneath it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Self

from stop_order_scalp.domain.enums import (
    BreakEvenMode,
    CommissionMode,
    Environment,
    OrderKind,
    PositionState,
    RiskMode,
    Side,
    TargetMode,
    TimeframeSelection,
)
from stop_order_scalp.domain.value_objects import Money, Price, Volume

__all__ = [
    "AccountSnapshot",
    "Candle",
    "InstrumentPolicy",
    "ManagedStop",
    "OrderIntent",
    "OrderRecord",
    "PositionRecord",
    "RiskAssessment",
    "Tick",
    "TradePlan",
    "TradeSignal",
    "strategy_id",
    "utc_now",
]

#: Identifiers that appear in filenames, broker comments and journal keys must be
#: boring. Anything else is either a bug or an injection attempt.
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: Longest order comment MetaTrader 5 accepts, in characters.
_MAX_COMMENT_LENGTH = 31


def utc_now() -> datetime:
    """Timezone-aware current UTC time.

    Every timestamp in this project comes from here or from an injected
    :class:`~stop_order_scalp.domain.interfaces.Clock`. A naive ``datetime`` anywhere in
    the codebase is a bug: broker server time, UTC and the local PC clock can differ by
    hours, and only an explicit offset keeps candle boundaries honest.
    """
    return datetime.now(UTC)


def strategy_id(value: str) -> str:
    """Validate an identifier destined for a filename, a broker comment or a journal key."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"identifier must match {_IDENTIFIER_PATTERN.pattern}, got {value!r}"
        )
    return value


def _require_offset_datetime(value: datetime, name: str) -> datetime:
    """Refuse naive datetimes. Naive time is how look-ahead bias sneaks in."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{name} must be timezone-aware, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class Candle:
    """One OHLC bar.

    ``open_time`` is the bar's **open** timestamp, matching MetaTrader 5 and the Al
    Brooks engine. The bar is closed when
    ``open_time + timeframe_seconds <= observed_at``; :attr:`is_closed` is therefore a
    function of both the candle and an explicit observation time, never of the wall
    clock. That is what makes look-ahead bias testable.
    """

    open_time: datetime
    open: Price
    high: Price
    low: Price
    close: Price
    timeframe_seconds: int
    timeframe: str = "M1"
    volume: Decimal = field(default_factory=lambda: Decimal(0))
    tick_volume: int = 0
    is_confirmed: bool = True

    def __post_init__(self) -> None:
        _require_offset_datetime(self.open_time, "open_time")
        if self.timeframe_seconds <= 0:
            raise ValueError(f"timeframe_seconds must be positive, got {self.timeframe_seconds}")
        if self.high < self.low:
            raise ValueError(f"high {self.high} is below low {self.low}")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"open {self.open} is outside [{self.low}, {self.high}]")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"close {self.close} is outside [{self.low}, {self.high}]")

    @property
    def close_time(self) -> datetime:
        """Instant at which this bar becomes complete."""
        from datetime import timedelta

        return self.open_time + timedelta(seconds=self.timeframe_seconds)

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def is_doji(self) -> bool:
        return self.close == self.open

    @property
    def body(self) -> Decimal:
        return abs(self.close.value - self.open.value)

    @property
    def price_range(self) -> Decimal:
        return self.high.value - self.low.value

    @property
    def direction(self) -> Side | None:
        """Direction implied by this candle's body.

        ``None`` for a doji. A doji has no direction, and silently picking one would
        manufacture a trade the specification never asked for.
        """
        if self.is_bullish:
            return Side.SIDE_BUY
        if self.is_bearish:
            return Side.SIDE_SELL
        return None

    def is_closed_at(self, moment: datetime) -> bool:
        """Whether this bar has fully formed as of ``moment``.

        The boundary is inclusive: a bar whose close time equals ``moment`` is closed.
        """
        _require_offset_datetime(moment, "moment")
        return self.close_time <= moment

    def close_time_is_floor(self) -> bool:
        """Whether ``open_time`` sits exactly on a timeframe boundary.

        Candles whose open time is not aligned to the timeframe indicate a feed whose
        session start does not divide evenly, which silently shifts every boundary
        calculation. Detectable, so it is detected.
        """
        epoch = int(self.open_time.timestamp())
        return epoch % self.timeframe_seconds == 0

    def __str__(self) -> str:
        return f"{self.timeframe}@{self.open_time.isoformat()} O{self.open} H{self.high} L{self.low} C{self.close}"


@dataclass(frozen=True, slots=True)
class Tick:
    """A bid/ask observation.

    Bid and ask are both required. The strategy fills buys at the ask and sells at the
    bid, and stops trigger on the opposite side; collapsing them into a single mid price
    produces backtests that cannot be reproduced live.
    """

    moment: datetime
    bid: Price
    ask: Price
    volume: Decimal = field(default_factory=lambda: Decimal(0))

    def __post_init__(self) -> None:
        _require_offset_datetime(self.moment, "moment")
        if self.ask < self.bid:
            raise ValueError(f"ask {self.ask} is below bid {self.bid}")

    @property
    def spread(self) -> Decimal:
        """Bid/ask spread in price units."""
        return self.ask.value - self.bid.value

    @property
    def mid(self) -> Decimal:
        return (self.ask.value + self.bid.value) / 2

    def price_for(self, side: Side) -> Price:
        """The price a position in ``side`` is valued or filled at."""
        return self.ask if side is Side.SIDE_BUY else self.bid

    def __str__(self) -> str:
        return f"tick@{self.moment.isoformat()} bid={self.bid} ask={self.ask}"


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Account state needed for sizing and validation.

    ``balance`` is used for the 0.5 % risk calculation, never ``equity``. Sizing from
    equity would silently increase risk after a losing streak, which is the exact moment
    when risk should not grow.
    """

    login: int
    server: str
    currency: str
    balance: Money
    equity: Money
    margin_used: Money
    margin_free: Money
    leverage: int = 100
    is_demo: bool = True
    trade_allowed: bool = True

    def __post_init__(self) -> None:
        if self.leverage <= 0:
            raise ValueError(f"leverage must be positive, got {self.leverage}")

    @property
    def is_live(self) -> bool:
        return not self.is_demo

    def to_dict(self) -> dict[str, Any]:
        return {
            "login": self.login,
            "server": self.server,
            "currency": self.currency,
            "balance": str(self.balance.amount),
            "equity": str(self.equity.amount),
            "margin_used": str(self.margin_used.amount),
            "margin_free": str(self.margin_free.amount),
            "leverage": self.leverage,
            "is_demo": self.is_demo,
            "trade_allowed": self.trade_allowed,
        }


@dataclass(frozen=True, slots=True)
class TradeSignal:
    """A direction plus the geometric intent that came with it.

    Produced by :mod:`stop_order_scalp.strategy` (the M15/M1 baseline) or by an
    integration such as :mod:`stop_order_scalp.integrations.al_brooks_adapter`. The rest
    of the system sees only this type, which is what keeps a third-party library from
    leaking into execution.

    ``stop_loss`` and ``take_profit`` are optional. A signal that carries geometry
    supplies it; a signal that does not defers to configuration. Silently synthesising a
    missing level inside the adapter would make it impossible to tell which signals were
    externally defined.
    """

    symbol: str
    side: Side
    timeframe: str
    direction_timeframe: str
    #: The M1 candle the entry was derived from, by open time.
    source_candle_open_time: datetime
    #: The M15 candle that authorised the direction, by open time.
    direction_candle_open_time: datetime
    order_kind: OrderKind
    reference_price: Price
    stop_loss: Price | None = None
    take_profit: Price | None = None
    #: Reward-to-risk, when the producer knows it. ``None`` means "derive it".
    risk_reward: Decimal | None = None
    #: Who produced this signal. Recorded in the journal and in the order comment.
    source: str = "m15_m1_stop"
    #: Set when an integration supplied geometry, so the journal can prove it.
    external: bool = False
    #: Free-form, non-executed context. Must never contain a secret.
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        strategy_id(self.source)
        _require_offset_datetime(self.source_candle_open_time, "source_candle_open_time")
        _require_offset_datetime(self.direction_candle_open_time, "direction_candle_open_time")
        if not self.symbol or not self.symbol.strip():
            raise ValueError("symbol must not be empty")
        if self.order_kind.side is not self.side:
            raise ValueError(f"order_kind {self.order_kind} contradicts side {self.side}")
        if self.stop_loss is not None and self.risk_reward is not None:
            raise ValueError("a signal carries either a stop loss or a ratio, not both")

    @property
    def candle_id(self) -> str:
        """Stable key for the candle pair this signal was derived from."""
        return f"{self.symbol}/{self.direction_timeframe}@{int(self.direction_candle_open_time.timestamp())}/{self.timeframe}@{int(self.source_candle_open_time.timestamp())}"

    def __str__(self) -> str:
        return f"TradeSignal({self.symbol} {self.side} ref={self.reference_price} src={self.source})"


@dataclass(frozen=True, slots=True)
class TradePlan:
    """A fully resolved, validated intent to trade.

    This is the boundary object between planning and execution. Everything the risk
    engine decided -- volume, the SL/TP actually used, and the three distinguishable
    monetary figures -- is already baked in, so the execution layer performs no
    arithmetic on money.
    """

    plan_id: str
    signal: TradeSignal
    entry: Price
    stop_loss: Price
    take_profit: Price
    volume: Volume
    #: Theoretical price risk: entry to stop, commission excluded.
    price_risk: Money
    #: Estimated commission for the whole planned life of the trade.
    commission: Money
    #: price_risk + commission. This is what "0.5 % of balance" is compared against.
    total_risk: Money
    target_mode: TargetMode
    created_at: datetime

    def __post_init__(self) -> None:
        strategy_id(self.plan_id)
        _require_offset_datetime(self.created_at, "created_at")
        # Volume positivity is not re-checked here: Volume's own constructor already
        # rejects a non-positive size, and a value object that can be built into an
        # invalid state is not a value object.

    @property
    def symbol(self) -> str:
        return self.signal.symbol

    @property
    def side(self) -> Side:
        return self.signal.side

    @property
    def risk_distance(self) -> Decimal:
        """Absolute entry-to-stop distance in price units."""
        return self.entry.absolute_distance_to(self.stop_loss)

    @property
    def reward_distance(self) -> Decimal:
        """Absolute entry-to-target distance in price units."""
        return self.entry.absolute_distance_to(self.take_profit)

    @property
    def achieved_risk_reward(self) -> Decimal:
        """Reward divided by risk. The plan's own numbers, not the requested ratio."""
        risk = self.risk_distance
        if risk == 0:
            return Decimal(0)
        return self.reward_distance / risk

    @property
    def comment(self) -> str:
        """Order comment, truncated to the broker's limit.

        Structured, not prose, so a reader in the terminal can identify the strategy
        without opening a log.
        """
        text = f"SOS/{self.signal.source}/{self.side}"
        return text[:_MAX_COMMENT_LENGTH]

    def __str__(self) -> str:
        return (
            f"TradePlan({self.plan_id} {self.side} {self.volume} @ {self.entry} "
            f"SL {self.stop_loss} TP {self.take_profit} risk {self.total_risk})"
        )


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """The exact request handed to the broker, plus the identity used to deduplicate it.

    ``client_tag`` is a deterministic function of the plan and the candle the plan was
    derived from. It is what makes "did I already place this order?" answerable without
    trusting local state.
    """

    plan_id: str
    client_tag: str
    symbol: str
    kind: OrderKind
    volume: Volume
    entry: Price
    stop_loss: Price
    take_profit: Price
    magic_number: int
    comment: str
    deviation_points: int
    expiration: datetime | None = None

    def __post_init__(self) -> None:
        strategy_id(self.plan_id)
        strategy_id(self.client_tag)
        if len(self.comment) > _MAX_COMMENT_LENGTH:
            raise ValueError(f"comment exceeds {_MAX_COMMENT_LENGTH} characters: {self.comment!r}")
        if self.expiration is not None:
            _require_offset_datetime(self.expiration, "expiration")

    @classmethod
    def from_plan(
        cls,
        plan: TradePlan,
        *,
        magic_number: int,
        deviation_points: int,
        expiration: datetime | None = None,
    ) -> OrderIntent:
        """Derive the intent, giving the client tag its deterministic identity.

        The tag binds the plan id to the candle that authorised it. Two different
        evaluations of the same still-open candle produce the same tag and are therefore
        recognisably the same order; an evaluation of a *new* candle produces a different
        tag, which is what tells the reconciler that a stale pending order must be
        replaced rather than adopted.
        """
        signal = plan.signal
        tag_source = (
            f"{plan.plan_id}|{signal.direction_timeframe}"
            f"@{int(signal.direction_candle_open_time.timestamp())}"
            f"|{signal.timeframe}@{int(signal.source_candle_open_time.timestamp())}"
            f"|{signal.side}"
        )
        return cls(
            plan_id=plan.plan_id,
            client_tag=_short_digest(tag_source),
            symbol=plan.symbol,
            kind=signal.order_kind,
            volume=plan.volume,
            entry=plan.entry,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            magic_number=magic_number,
            comment=plan.comment,
            deviation_points=deviation_points,
            expiration=expiration,
        )


@dataclass(frozen=True, slots=True)
class OrderRecord:
    """An order as the broker reports it."""

    ticket: int
    client_tag: str
    symbol: str
    kind: OrderKind
    volume: Volume
    entry: Price
    stop_loss: Price | None
    take_profit: Price | None
    magic_number: int
    comment: str
    placed_at: datetime
    state: str = "PLACED"
    filled_volume: Volume | None = None
    position_id: int | None = None
    expires_at: datetime | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        _require_offset_datetime(self.placed_at, "placed_at")

    @property
    def is_active(self) -> bool:
        """Whether the order still rests on the book."""
        return self.state in ("PLACED", "ACCEPTED", "PARTIALLY_FILLED")

    @property
    def stops_set(self) -> bool:
        """Whether both protective levels are present."""
        return self.stop_loss is not None and self.take_profit is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "client_tag": self.client_tag,
            "symbol": self.symbol,
            "kind": str(self.kind),
            "volume": str(self.volume.lots),
            "entry": str(self.entry),
            "stop_loss": str(self.stop_loss) if self.stop_loss else None,
            "take_profit": str(self.take_profit) if self.take_profit else None,
            "magic_number": self.magic_number,
            "state": self.state,
            "placed_at": self.placed_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class PositionRecord:
    """A position as the broker reports it."""

    ticket: int
    symbol: str
    side: Side
    volume: Volume
    entry: Price
    stop_loss: Price | None
    take_profit: Price | None
    magic_number: int
    comment: str
    opened_at: datetime
    profit: Money = field(default_factory=lambda: Money.of(0))
    #: Ticket of the pending order this position came from, when known.
    source_order_ticket: int | None = None
    #: The client tag of the originating plan. Survives the pending-to-position hop
    #: because the broker copies the comment and the position comment is our own.
    client_tag: str = ""

    def __post_init__(self) -> None:
        _require_offset_datetime(self.opened_at, "opened_at")

    @property
    def has_stop_loss(self) -> bool:
        return self.stop_loss is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "symbol": self.symbol,
            "side": str(self.side),
            "volume": str(self.volume.lots),
            "entry": str(self.entry),
            "stop_loss": str(self.stop_loss) if self.stop_loss else None,
            "take_profit": str(self.take_profit) if self.take_profit else None,
            "profit": str(self.profit.amount),
            "opened_at": self.opened_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ManagedStop:
    """A proposed change to a position's stop loss.

    The trailing and break-even components *propose*; they never modify. Deciding
    whether a proposal is acceptable given broker minimum distance, freeze level and
    spread is a separate concern, which keeps the monotonicity guarantee testable in
    isolation from broker behaviour.
    """

    position_ticket: int
    side: Side
    current_stop: Price | None
    proposed_stop: Price
    reason: str
    distance_points: Decimal

    @property
    def is_noop(self) -> bool:
        """Whether the proposal would not change anything."""
        if self.current_stop is None:
            return False
        return self.current_stop == self.proposed_stop

    def is_monotonic(self) -> bool:
        """Whether the proposal respects the direction's monotonicity rule.

        This is the invariant the specification calls out by name:

        * a BUY stop loss may only move **up**
        * a SELL stop loss may only move **down**
        """
        if self.current_stop is None:
            return True
        if self.side is Side.SIDE_BUY:
            return self.proposed_stop >= self.current_stop
        return self.proposed_stop <= self.current_stop

    def __str__(self) -> str:
        return (
            f"ManagedStop(ticket={self.position_ticket} {self.side} "
            f"{self.current_stop} -> {self.proposed_stop} reason={self.reason})"
        )


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    """The risk engine's verdict on a prospective trade."""

    accepted: bool
    reason: str
    code: str = ""
    volume: Volume | None = None
    price_risk: Money | None = None
    commission: Money | None = None
    total_risk: Money | None = None
    risk_fraction: Decimal | None = None

    @classmethod
    def reject(cls, code: str, reason: str) -> Self:
        """A rejection verdict. A rejection always carries a machine-readable ``code``."""
        return cls(accepted=False, reason=reason, code=code)

    def __str__(self) -> str:
        verdict = "ACCEPT" if self.accepted else "REJECT"
        return f"RiskAssessment({verdict} {self.code}: {self.reason})"


@dataclass(frozen=True, slots=True)
class InstrumentPolicy:
    """Which instruments this strategy is permitted to trade.

    The strategy specification says US30 only, and says not to silently trade
    something else. A broker's symbol name is machine-specific (``US30``, ``US30.cash``,
    ``US30m``, ``DJ30``), so the policy holds a set of *accepted names* against the
    single logical instrument.

    Matching is exact after case folding. Substring matching would let ``EURUSD30`` or
    ``US30mini`` through, which is exactly the failure the specification forbids.
    """

    logical_symbol: str = "US30"
    accepted_names: frozenset[str] = frozenset({"US30"})
    #: Names observed on a broker but not yet approved. Reported by ``status``.
    #: Never traded automatically.
    quarantine: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        strategy_id(self.logical_symbol)
        normalised = frozenset(name.strip().upper() for name in self.accepted_names if name.strip())
        if not normalised:
            raise ValueError("instrument policy must accept at least one symbol name")
        object.__setattr__(self, "accepted_names", normalised)
        object.__setattr__(
            self,
            "quarantine",
            frozenset(name.strip().upper() for name in self.quarantine if name.strip()),
        )

    def allows(self, symbol: str) -> bool:
        return symbol.strip().upper() in self.accepted_names

    def is_quarantined(self, symbol: str) -> bool:
        return symbol.strip().upper() in self.quarantine

    def __str__(self) -> str:
        return f"InstrumentPolicy({self.logical_symbol} accepts {sorted(self.accepted_names)})"


@dataclass(frozen=True, slots=True)
class CommissionModel:
    """Resolved commission configuration.

    ``per_lot`` is the configured dollar figure. :attr:`sides` makes explicit how many
    charges it stands for, so a reader of the code never has to guess whether ``6.0``
    is one side or a round trip.
    """

    per_lot: Decimal
    mode: CommissionMode = CommissionMode.PER_LOT_ROUND_TRIP

    def __post_init__(self) -> None:
        if self.per_lot < 0:
            raise ValueError(f"commission_per_lot must be non-negative, got {self.per_lot}")

    @property
    def sides(self) -> int:
        return self.mode.sides

    def total_for(self, volume: Volume | Decimal) -> Money:
        """Estimated total commission for a trade of ``volume`` lots."""
        lots = volume.lots if isinstance(volume, Volume) else Decimal(str(volume))
        return Money(self.per_lot * self.sides * lots)

    def per_lot_total(self) -> Decimal:
        """The configured figure interpreted as a round trip."""
        return self.per_lot * self.sides

    def __str__(self) -> str:
        return f"CommissionModel(per_lot={self.per_lot} mode={self.mode} total_per_lot={self.per_lot_total()})"


@dataclass(frozen=True, slots=True)
class PositionProgress:
    """How a live position has progressed through the management ladder.

    Break-even and trailing are separate stages rather than one flag, because they have
    different triggers and because a position can arm break-even and then trail without
    ever having had a trailing stop set by this system.
    """

    ticket: int
    state: PositionState
    break_even_armed: bool = False
    break_even_applied: bool = False
    trailing_active: bool = False
    highest_price: Price | None = None
    lowest_price: Price | None = None
    last_applied_stop: Price | None = None
    updates: int = 0

    def __post_init__(self) -> None:
        if self.break_even_applied and not self.break_even_armed:
            raise ValueError("break_even_applied cannot be true while break_even_armed is false")

    @property
    def is_managed(self) -> bool:
        return self.break_even_armed or self.trailing_active


@dataclass(frozen=True, slots=True)
class EnvironmentSettings:
    """Machine-local execution settings, resolved from the environment.

    Deliberately separate from the strategy configuration: this object is where
    credentials and paths live, and the strategy must never be able to read it.
    """

    environment: Environment = Environment.DRY_RUN
    allow_live: bool = False
    allow_order: bool = False
    allow_close: bool = False
    symbol: str = "US30"
    magic_number: int = 20260930
    mt5_path: str | None = None
    mt5_login: int | None = None
    mt5_server: str | None = None
    mt5_timeout_ms: int = 60000

    def __post_init__(self) -> None:
        if self.mt5_timeout_ms <= 0:
            raise ValueError("mt5_timeout_ms must be positive")

    @property
    def password_present(self) -> bool:
        """Whether a terminal password was supplied -- a bool, never the value.

        This is the pattern the whole logging layer relies on: credentials are reported
        as presence, so there is nothing to redact because nothing secret was ever
        handed to the logger. The password itself is never stored on this object.
        """
        return _PASSWORD_PRESENT.get()

    @property
    def has_credentials(self) -> bool:
        return bool(self.mt5_login) and self.password_present

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": str(self.environment),
            "allow_live": self.allow_live,
            "allow_order": self.allow_order,
            "allow_close": self.allow_close,
            "symbol": self.symbol,
            "magic_number": self.magic_number,
            "mt5_path_configured": bool(self.mt5_path),
            "mt5_login_configured": bool(self.mt5_login),
            "mt5_password_configured": self.password_present,
            "mt5_server": self.mt5_server,
        }


@dataclass(frozen=True, slots=True)
class RiskConfigSnapshot:
    """Read-only view of the resolved risk parameters, for status output."""

    mode: RiskMode
    percent: Decimal
    fixed_lot: Decimal
    commission_per_lot: Decimal
    commission_mode: CommissionMode
    target_mode: TargetMode
    take_profit_points: int
    risk_reward: Decimal
    stop_loss_points: int
    break_even_enabled: bool
    break_even_trigger_points: int
    break_even_mode: BreakEvenMode
    trailing_enabled: bool
    trailing_distance_points: int
    candle_selection: TimeframeSelection

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": str(self.mode),
            "percent": str(self.percent),
            "fixed_lot": str(self.fixed_lot),
            "commission_per_lot": str(self.commission_per_lot),
            "commission_mode": str(self.commission_mode),
            "target_mode": str(self.target_mode),
            "take_profit_points": self.take_profit_points,
            "risk_reward": str(self.risk_reward),
            "stop_loss_points": self.stop_loss_points,
            "break_even_enabled": self.break_even_enabled,
            "break_even_trigger_points": self.break_even_trigger_points,
            "break_even_mode": str(self.break_even_mode),
            "trailing_enabled": self.trailing_enabled,
            "trailing_distance_points": self.trailing_distance_points,
            "candle_selection": str(self.candle_selection),
        }


class _PasswordPresence:
    """Mutable one-bit holder, deliberately not a dataclass.

    Kept out of :class:`EnvironmentSettings` on purpose: a frozen dataclass is trivially
    serialisable and therefore trivially leakable. The password lives in the process
    environment and is reduced to this flag at construction time; application code never
    reads the variable again.
    """

    __slots__ = ("_present",)

    def __init__(self) -> None:
        self._present = False

    def get(self) -> bool:
        return self._present

    def set(self, present: bool) -> None:
        self._present = bool(present)


#: Process-wide holder. Module-level state is normally a design smell; this one is a
#: single boolean that exists precisely so that a secret does *not* become object state.
_PASSWORD_PRESENT = _PasswordPresence()


def set_password_present(present: bool) -> None:
    """Record whether a terminal password was supplied, without recording it."""
    _PASSWORD_PRESENT.set(present)


def _short_digest(source: str) -> str:
    """A short, deterministic, filesystem- and comment-safe digest.

    Not a security primitive. It exists so the client tag is stable across restarts and
    unique per distinct trade intent, and stays inside MetaTrader 5's 31-character
    comment limit.
    """
    import hashlib

    return hashlib.blake2b(source.encode("utf-8"), digest_size=10).hexdigest()
