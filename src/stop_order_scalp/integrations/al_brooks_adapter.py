"""The only module in the project that may import ``albrooks``.

Everything this project knows about the third-party engine is in this file. That is a hard
boundary, enforced by ``scripts/check_architecture.py``, and it exists so that an optional
extra cannot leak into the layers that decide whether to trade.

The engine is ``al-brooks-price-action-engine`` (import name ``albrooks``, stdlib only, MIT).
It has no broker, no orders and no sizing — it produces *geometry*. What it produces:

======================  ==========================================================
``Decision.action``     ``"BUY"`` / ``"SELL"`` / ``"WAIT"`` / ``"NO_TRADE"``
``Decision.plan``       a dict with ``direction`` (+1 / -1 / 0), ``entry``,
                        ``stop``, ``target``, ``reward_to_risk``
======================  ==========================================================

There is no ``Signal`` class on the other side, and the engine has no forming-bar concept: a
bar is closed iff ``time + period_seconds <= now``.

Three rules, and each one exists because of something specific about this engine
-----------------------------------------------------------------------------

**A suggestion is not a signal.** The engine's own ``to_dict`` hard-codes
``"is_recommendation": False`` — it is explicit that its output is *not* a recommendation.
So an Al Brooks ``BUY`` becomes a domain signal only when an operator has said so in
configuration, and by default this adapter is inert.

**The engine never invents a trade here, and neither may we.** ``WAIT`` and ``NO_TRADE`` are
*answers*, and this project maps them to :class:`~stop_order_scalp.strategy.signal.NoTrade`
rather than to a signal. A decision that declines is not a decision to trade the other way,
and treating it as one would mean the adapter was a source of trades rather than a filter.

**Geometry is opt-in, separately.** Even when the engine is enabled, its ``entry``/``stop``/
``target`` are ignored unless ``allow_geometry`` is also set. This project's entry rule is
M1-extremum-plus-offset and its stops come from the risk engine; adopting a third source for
the same number would make it impossible to tell afterwards which rule produced a level. The
two switches are independent so that an operator can take the engine's *direction* while
keeping this project's *geometry*.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, Final

from stop_order_scalp.domain.enums import OrderKind, Side
from stop_order_scalp.domain.exceptions import ComponentNotAvailableError, DomainError
from stop_order_scalp.domain.models import TradeSignal
from stop_order_scalp.domain.value_objects import Price
from stop_order_scalp.infrastructure.config import AlBrooksSettings
from stop_order_scalp.strategy.signal import NoTrade

__all__ = [
    "AdaptedDecision",
    "AlBrooksAdapter",
    "AlBrooksRefusal",
    "import_engine",
]

#: The import name of the third-party package. No underscore.
PACKAGE: Final[str] = "albrooks"

#: The engine's actions, verbatim. Compared as given rather than mapped through an enum, so a
#: new action upstream is a refusal rather than a silent default.
ACTION_BUY: Final[str] = "BUY"
ACTION_SELL: Final[str] = "SELL"
ACTION_WAIT: Final[str] = "WAIT"
ACTION_NO_TRADE: Final[str] = "NO_TRADE"

#: Actions that mean "do not trade". Neither is ever mapped to a signal.
DECLINING_ACTIONS: Final[frozenset[str]] = frozenset({ACTION_WAIT, ACTION_NO_TRADE})

#: action -> the side and pending-order kind it implies. A pending *stop*, because that is
#: what this strategy places; the engine's geometry is read separately and optionally.
_ACTION_SIDE: Final[dict[str, tuple[Side, OrderKind]]] = {
    ACTION_BUY: (Side.SIDE_BUY, OrderKind.ORDER_KIND_BUY_STOP),
    ACTION_SELL: (Side.SIDE_SELL, OrderKind.ORDER_KIND_SELL_STOP),
}

#: ``direction`` int -> side. The engine's own convention: +1 long, -1 short, 0 flat.
_DIRECTION_SIDE: Final[dict[int, Side]] = {1: Side.SIDE_BUY, -1: Side.SIDE_SELL}


class AlBrooksRefusal:
    """Stable reasons the adapter declines to produce a signal.

    A namespace of plain strings, like ``NoTradeReason``: these reach the journal, and
    "something went wrong" is not something an operator can act on months later.
    """

    DISABLED: str = "al_brooks_disabled"
    WAIT: str = "al_brooks_wait"
    NO_TRADE: str = "al_brooks_no_trade"
    #: The action is not one the engine documents.
    UNKNOWN_ACTION: str = "al_brooks_unknown_action"
    #: ``action`` says BUY while ``plan['direction']`` says short, or vice versa. Refused
    #: rather than resolved in favour of either.
    DIRECTION_CONFLICTS_ACTION: str = "al_brooks_direction_conflicts_action"
    #: ``action`` trades but ``direction`` is 0, which is contradictory.
    DIRECTION_MISSING: str = "al_brooks_direction_missing"
    #: The geometry was requested but the plan does not carry a usable entry.
    GEOMETRY_UNUSABLE: str = "al_brooks_geometry_unusable"


@dataclass(frozen=True, slots=True)
class AdaptedDecision:
    """What the engine said, translated.

    Either a signal or a reason, never both and never neither. A decision with neither would
    be a bug the caller would have to guess about, and a decision with both would let a
    decline be read as a trade.
    """

    #: The domain signal, or ``None``.
    signal: TradeSignal | None = None
    #: Why not, or ``""``.
    code: str = ""
    reason: str = ""
    #: The engine's action verbatim, for the journal. Present in every outcome.
    action: str = ""
    #: The engine's plan verbatim, for the journal and for the geometry path.
    plan: dict[str, Any] | None = None

    @property
    def is_signal(self) -> bool:
        return self.signal is not None

    @property
    def is_declined(self) -> bool:
        return self.signal is None

    def require_signal(self) -> TradeSignal:
        if self.signal is None:
            raise ValueError(f"no signal: {self.code}: {self.reason}")
        return self.signal

    def to_no_trade(self, *, reference: datetime, symbol: str) -> NoTrade:
        """This refusal as a :class:`NoTrade`, so callers handle one union.

        The engine declining is a legitimate no-trade reason, and folding it into
        ``NoTrade`` means every consumer already handles it without knowing the engine exists.
        """
        return NoTrade(
            reason=self.code or "al_brooks_declined",
            detail=self.reason or f"the engine returned {self.action!r}",
            reference=reference,
            symbol=symbol,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "code": self.code,
            "reason": self.reason,
            "has_signal": self.signal is not None,
        }

    def __str__(self) -> str:
        if self.signal is not None:
            return f"AdaptedDecision({self.action} -> {self.signal.order_kind})"
        return f"AdaptedDecision({self.action}: {self.code})"


class AlBrooksAdapter:
    """Translates one engine decision into this project's vocabulary.

    Stateless. It is also the only place ``albrooks`` is named, so nothing above it can
    depend on the engine's shape.
    """

    __slots__ = ("_digits", "_settings", "_symbol")

    def __init__(
        self,
        settings: AlBrooksSettings,
        symbol: str,
        *,
        digits: int = 1,
    ) -> None:
        self._settings = settings
        self._symbol = symbol
        self._digits = digits

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    @property
    def allow_geometry(self) -> bool:
        return self._settings.allow_geometry

    def adapt(
        self,
        decision: Any,
        *,
        source_candle_open_time: datetime,
        direction_timeframe: str = "M15",
        entry_timeframe: str = "M1",
    ) -> AdaptedDecision:
        """Translate one engine decision.

        ``decision`` is duck-typed on ``action`` and ``plan`` rather than imported, so this
        module's own import graph does not pull the engine in and so a test can drive it with
        a fake.
        """
        action = str(getattr(decision, "action", "") or "")
        plan = getattr(decision, "plan", None)
        as_dict = plan if isinstance(plan, dict) else _plan_to_dict(plan)

        if not self._settings.enabled:
            return AdaptedDecision(
                None,
                AlBrooksRefusal.DISABLED,
                "the Al Brooks integration is disabled; the M15/M1 baseline is authoritative",
                action,
                as_dict,
            )

        if action in DECLINING_ACTIONS:
            # Explicitly *not* a signal. A WAIT is an answer, and answering it by trading the
            # other way would make this adapter a source of trades rather than a filter.
            code = (
                AlBrooksRefusal.WAIT
                if action == ACTION_WAIT
                else AlBrooksRefusal.NO_TRADE
            )
            return AdaptedDecision(
                None,
                code,
                f"the engine returned {action!r}, which is not a signal",
                action,
                as_dict,
            )

        if action not in _ACTION_SIDE:
            return AdaptedDecision(
                None,
                AlBrooksRefusal.UNKNOWN_ACTION,
                f"{action!r} is not an action this adapter knows; refusing rather than "
                "guessing which way it meant",
                action,
                as_dict,
            )

        side, order_kind = _ACTION_SIDE[action]

        # The engine carries direction twice -- in the action and in the plan. When they
        # disagree the input is self-contradictory, and resolving it in favour of either half
        # would be inventing a signal from a broken decision.
        direction = _direction_of(as_dict)
        if direction == 0:
            return AdaptedDecision(
                None,
                AlBrooksRefusal.DIRECTION_MISSING,
                f"the action says {action!r} but the plan's direction is 0",
                action,
                as_dict,
            )
        if _DIRECTION_SIDE.get(direction) is not side:
            return AdaptedDecision(
                None,
                AlBrooksRefusal.DIRECTION_CONFLICTS_ACTION,
                f"the action says {action!r} but the plan's direction is {direction}; "
                "refusing to guess which was meant",
                action,
                as_dict,
            )

        entry = _entry_price(as_dict, self._digits)
        if entry is None:
            return AdaptedDecision(
                None,
                AlBrooksRefusal.GEOMETRY_UNUSABLE,
                "the plan carries no usable entry price",
                action,
                as_dict,
            )

        # Geometry is opt-in, separately from enabling the engine. With it off the signal
        # carries no stop or target, and the risk engine's configured levels apply -- which
        # keeps a single, traceable source for the level an order actually uses. With it on
        # the engine's own stop and target ride along, and the risk engine can still refuse
        # the trade on its own rules.
        stop_loss = _price_or_none(as_dict.get("stop"), self._digits)
        take_profit = _price_or_none(as_dict.get("target"), self._digits)
        if self._settings.allow_geometry:
            geometry_source = "al_brooks"
            stop = stop_loss
            target = take_profit
        else:
            geometry_source = "m15_m1_baseline"
            stop = None
            target = None

        signal = TradeSignal(
            symbol=self._symbol,
            side=side,
            timeframe=entry_timeframe,
            direction_timeframe=direction_timeframe,
            source_candle_open_time=source_candle_open_time,
            direction_candle_open_time=source_candle_open_time,
            order_kind=order_kind,
            reference_price=entry,
            stop_loss=stop,
            take_profit=target,
            source="al_brooks",
            context={
                "al_brooks_action": action,
                "al_brooks_reward_to_risk": as_dict.get("reward_to_risk"),
                # ``is_recommendation`` is hard-coded False upstream. Recorded so a reader of
                # the journal knows the direction came from a third-party engine rather than
                # from this project's rules.
                "al_brooks_is_recommendation": False,
                "geometry_source": geometry_source,
            },
        )
        return AdaptedDecision(signal, "", "", action, as_dict)

    def __repr__(self) -> str:
        return (
            f"AlBrooksAdapter(symbol={self._symbol!r}, enabled={self.enabled}, "
            f"allow_geometry={self.allow_geometry})"
        )


def import_engine() -> ModuleType:
    """Import ``albrooks`` lazily, or explain how to get it.

    A module-level import would make the optional extra required, which is the opposite of
    what an optional extra is for. The architecture check requires the import to sit inside a
    function for the same reason it does for ``MetaTrader5``.
    """
    try:
        import albrooks
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ComponentNotAvailableError(
            "the optional 'albrooks' extra is not installed. Install it with: "
            'pip install -e ".[albrooks]" -- or leave the integration disabled, which is the '
            "default and needs nothing extra."
        ) from exc
    found: ModuleType = albrooks
    return found


def _price_or_none(raw: Any, digits: int) -> Price | None:
    """A price from the plan, or ``None`` if it is absent or unparseable.

    ``None`` rather than a raised error: a plan with a direction but no stop is still a
    usable *direction*, and refusing the whole decision over a missing optional level would
    throw away the part that is good.
    """
    if raw is None:
        return None
    try:
        return Price.parse(str(raw), digits)
    except (DomainError, ValueError, ArithmeticError):
        return None


def _plan_to_dict(plan: Any) -> dict[str, Any]:
    """The engine's plan as a dict, whether it is already one or exposes ``to_dict``."""
    if plan is None:
        return {}
    if isinstance(plan, dict):
        return plan
    to_dict = getattr(plan, "to_dict", None)
    if callable(to_dict):
        converted: dict[str, Any] = to_dict()
        return converted
    return {}


def _direction_of(plan: dict[str, Any]) -> int:
    raw = plan.get("direction", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _entry_price(plan: dict[str, Any], digits: int) -> Price | None:
    """The plan's entry, as a :class:`Price`, or ``None`` if it is unusable.

    This is the price the engine was looking at, and it becomes the signal's
    ``reference_price`` — a record of what the engine saw, not the level an order would go
    to. ``allow_geometry`` is **not** consulted here: whether the engine's ``entry``/``stop``/
    ``target`` may be used as the order's geometry is decided by
    :mod:`stop_order_scalp.integrations.al_brooks_signal_provider`, which is the only place
    that turns a suggestion into a level. Keeping that decision in one place is what stops
    the engine's geometry leaking in through a second door.
    """
    raw = plan.get("entry")
    if raw is None:
        return None
    try:
        return Price.parse(str(raw), digits)
    except (DomainError, ValueError, ArithmeticError):
        return None


def _utc_now() -> datetime:
    return datetime.now(UTC)
