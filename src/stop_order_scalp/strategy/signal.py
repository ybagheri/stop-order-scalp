"""The decision: a :class:`TradeSignal` or a :class:`NoTrade`.

**Not trading is a result, not a failure.** Most of the time the strategy must do nothing:
the M15 candle is a doji, the feed has no closed M15 bar, the M1 candle has not closed,
the instrument is not permitted. Each of those is an ordinary, expected outcome, so it is
modelled as a first-class value with a named reason. If "no trade" were an exception, or a
``None``, or a signal with a null side, the callers that must distinguish *quiet* from
*broken* would all have to re-derive the reason, and would eventually disagree.

The two results are siblings. :class:`Decision` is the union, so a caller handles both in
one place and cannot forget the second case::

    decision = strategy.evaluate(...)
    if isinstance(decision, NoTrade):
        log.info(decision.reason)
        return
    plan = risk.assess(decision.signal)      # only a TradeSignal gets here

Everything is pure. ``evaluate`` takes candles and a reference moment; it reads no clock,
does no I/O, and does not consult configuration beyond the settings object handed to it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from stop_order_scalp.domain.enums import Side
from stop_order_scalp.domain.exceptions import InstrumentNotAllowedError
from stop_order_scalp.domain.models import Candle, InstrumentPolicy, TradeSignal
from stop_order_scalp.domain.value_objects import SymbolSpecification
from stop_order_scalp.infrastructure.config import EntrySettings
from stop_order_scalp.market_data.candles import FreezeReport, freeze_closed_bars
from stop_order_scalp.strategy.candle_direction import (
    DirectionDecision,
    DirectionVerdict,
    decide_direction,
)
from stop_order_scalp.strategy.entry_rules import EntryLevel, entry_level, validate_entry_candle

__all__ = [
    "Decision",
    "NoTrade",
    "NoTradeReason",
    "TradeDecision",
    "build_signal",
]


class NoTradeReason:
    """Why nothing was traded. A namespace of plain strings, as with DirectionVerdict."""

    NO_DIRECTION: str = "no_direction"
    DOJI_DIRECTION: str = "doji_direction"
    ENTRY_CANDLE_FORMING: str = "entry_candle_forming"
    NO_ENTRY_CANDLE: str = "no_entry_candle"
    INSTRUMENT_NOT_ALLOWED: str = "instrument_not_allowed"
    INSTRUMENT_QUARANTINED: str = "instrument_quarantined"
    #: The optional Al Brooks engine was consulted and declined. Distinct from every reason
    #: above because it is the only one produced by a third party, and an operator whose
    #: trades have quietly stopped needs to be able to say which filter is responsible.
    AL_BROOKS_VETO: str = "al_brooks_veto"


@dataclass(frozen=True, slots=True)
class NoTrade:
    """A decision to do nothing, and the reason for it.

    Carries the evidence, not just the verdict. A journal that recorded only "no trade"
    would be useless for working out afterwards whether the strategy was quiet or broken.
    """

    reason: str
    detail: str
    reference: datetime
    symbol: str
    direction: DirectionDecision | None = None
    entry_candle: Candle | None = None
    freeze: FreezeReport | None = None

    @property
    def is_indeterminate(self) -> bool:
        """Whether this is "could not tell" rather than "told, and the answer is no".

        The three data-shortage reasons qualify. ``ENTRY_CANDLE_FORMING`` is the ordinary
        state of the world for the first second of every minute, and an operator watching
        the log wants it counted as "not enough information yet" rather than confused with
        a doji, which is a real answer.

        A quarantine refusal and a doji are both perfectly good information, and are not
        indeterminate.
        """
        return self.reason in (
            NoTradeReason.NO_DIRECTION,
            NoTradeReason.NO_ENTRY_CANDLE,
            NoTradeReason.ENTRY_CANDLE_FORMING,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": "no_trade",
            "reason": self.reason,
            "detail": self.detail,
            "symbol": self.symbol,
            "reference": self.reference.isoformat(),
            "is_indeterminate": self.is_indeterminate,
            "direction": self.direction.verdict if self.direction else None,
            "entry_candle_open_time": None
            if self.entry_candle is None
            else self.entry_candle.open_time.isoformat(),
        }

    def __str__(self) -> str:
        return f"NoTrade({self.reason}: {self.detail})"


@dataclass(frozen=True, slots=True)
class TradeDecision:
    """A decision to trade, carrying the signal and the geometry behind it."""

    signal: TradeSignal
    level: EntryLevel
    direction: DirectionDecision
    entry_candle: Candle

    @property
    def side(self) -> Side:
        return self.signal.side

    @property
    def candle_id(self) -> str:
        """Stable key for the candle pair. Two decisions from the same pair are the same trade."""
        return self.signal.candle_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": "trade",
            "candle_id": self.candle_id,
            "symbol": self.signal.symbol,
            "side": str(self.signal.side),
            "order_kind": str(self.signal.order_kind),
            "reference_price": str(self.signal.reference_price),
            "anchor": str(self.level.anchor),
            "offset_points": self.level.offset_points,
            "rounded": self.level.rounded,
            "direction_candle_open_time": self.direction.candle.open_time.isoformat()
            if self.direction.candle
            else None,
            "entry_candle_open_time": self.entry_candle.open_time.isoformat(),
        }

    def __str__(self) -> str:
        return f"TradeDecision({self.signal.order_kind} at {self.signal.reference_price})"


#: The union. Every consumer handles both arms, so "no trade" cannot be forgotten.
Decision = TradeDecision | NoTrade


def build_signal(
    *,
    symbol: str,
    side: Side,
    level: EntryLevel,
    entry_candle: Candle,
    direction: DirectionDecision,
    direction_timeframe: str,
    entry_timeframe: str,
    stop_loss: Any = None,
    take_profit: Any = None,
    context: dict[str, Any] | None = None,
) -> TradeSignal:
    """Assemble a :class:`TradeSignal` from a resolved entry level.

    Thin on purpose: the decision about *what* to trade was already made, and duplicated
    here would be duplicated wrongly later. The strategy layer supplies no stop or target
    by default — those come from configuration in the risk engine, and a signal that
    invented its own would make it impossible to tell which signals carried geometry.
    """
    return TradeSignal(
        symbol=symbol,
        side=side,
        timeframe=entry_timeframe,
        direction_timeframe=direction_timeframe,
        source_candle_open_time=entry_candle.open_time,
        direction_candle_open_time=(
            direction.candle.open_time if direction.candle else entry_candle.open_time
        ),
        order_kind=level.order_kind,
        reference_price=level.price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        source="m15_m1_stop",
        context=dict(context or {}),
    )


def no_trade(
    reason: str,
    detail: str,
    *,
    reference: datetime,
    symbol: str,
    direction: DirectionDecision | None = None,
    entry_candle: Candle | None = None,
    freeze: FreezeReport | None = None,
) -> NoTrade:
    """Construct a :class:`NoTrade`. Named so call sites read as a decision, not a return."""
    return NoTrade(
        reason=reason,
        detail=detail,
        reference=reference,
        symbol=symbol,
        direction=direction,
        entry_candle=entry_candle,
        freeze=freeze,
    )


def check_instrument(symbol: str, policy: InstrumentPolicy) -> None:
    """Refuse a symbol the strategy is not permitted to trade.

    :raises InstrumentNotAllowedError: for a quarantined name, and for any name the policy
        does not accept. Both refusals are exceptions rather than ``NoTrade`` results on
        purpose: reaching here means the *configuration or the feed* is wrong, not that the
        market is quiet, and a misconfigured run must fail loudly rather than sit there
        declining to trade for a day.

    Matching is exact after case folding, so ``US30`` can never match ``US30mini`` or
    ``EURUSD30``.
    """
    if policy.is_quarantined(symbol):
        raise InstrumentNotAllowedError(
            f"{symbol!r} is quarantined: observed on the broker but not approved. "
            "Add it to symbol_aliases only after deciding it is the intended instrument."
        )
    if not policy.allows(symbol):
        raise InstrumentNotAllowedError(
            f"{symbol!r} is not permitted; {policy.logical_symbol} accepts "
            f"{sorted(policy.accepted_names)}"
        )


def select_entry_candle(
    m1_candles: Sequence[Candle],
    *,
    reference: datetime,
    timeframe: str,
) -> tuple[Candle | None, FreezeReport | None, str | None]:
    """The last fully closed entry-timeframe candle.

    Returns ``(candle, report, None)`` on success and ``(None, report, reason)`` otherwise.
    The reason is returned rather than raised: not having a closed M1 bar is a normal
    condition at the start of a minute, not an error.
    """
    usable = [candle for candle in m1_candles if candle.timeframe == timeframe]
    if not usable:
        return None, None, NoTradeReason.NO_ENTRY_CANDLE
    closed, report = freeze_closed_bars(usable, reference=reference, timeframe=timeframe)
    if not closed:
        return None, report, NoTradeReason.ENTRY_CANDLE_FORMING
    return closed[-1], report, None


def evaluate(
    *,
    symbol: str,
    m15_candles: Sequence[Candle],
    m1_candles: Sequence[Candle],
    reference: datetime,
    entry: EntrySettings,
    specification: SymbolSpecification,
    policy: InstrumentPolicy | None = None,
) -> Decision:
    """The whole baseline decision, as a pure function.

    The order of the checks is deliberate and is the order an operator would want to read
    in a log:

    1. **Is the instrument permitted?** A configuration fault, so it raises rather than
       returning a no-trade.
    2. **Is there a closed M15 candle, and does it permit a direction?** No direction means
       no trade, and the reason names which of the four ways it failed.
    3. **Is there a closed M1 candle to derive the entry from?** The entry level is a
       function of that candle's high or low, so without it there is nothing to place.
    4. **Is it the right timeframe?** Checked last of the data checks, because a wrong
       timeframe is a caller bug and should not be masked by a quiet market.

    :param reference: the moment to judge closure against, in broker server time. This is
        the only thing that makes the result reproducible.
    """
    if policy is not None:
        check_instrument(symbol, policy)

    direction = decide_direction(
        m15_candles, reference=reference, timeframe=entry.direction_timeframe
    )
    if not direction.allows_trading:
        reason = (
            NoTradeReason.DOJI_DIRECTION
            if direction.verdict == DirectionVerdict.DOJI
            else NoTradeReason.NO_DIRECTION
        )
        return no_trade(
            reason,
            direction.reason,
            reference=reference,
            symbol=symbol,
            direction=direction,
        )

    candle, report, entry_reason = select_entry_candle(
        m1_candles, reference=reference, timeframe=entry.timeframe
    )
    if candle is None or entry_reason is not None:
        return no_trade(
            entry_reason if entry_reason is not None else NoTradeReason.NO_ENTRY_CANDLE,
            f"no closed {entry.timeframe} candle at {reference.isoformat()}",
            reference=reference,
            symbol=symbol,
            direction=direction,
            freeze=report,
        )

    validate_entry_candle(candle, entry.timeframe)
    assert direction.side is not None  # guaranteed by allows_trading; narrows for mypy

    level = entry_level(candle, direction.side, entry.offset_points, specification)
    signal = build_signal(
        symbol=symbol,
        side=direction.side,
        level=level,
        entry_candle=candle,
        direction=direction,
        direction_timeframe=entry.direction_timeframe,
        entry_timeframe=entry.timeframe,
        context={
            "direction_verdict": direction.verdict,
            "direction_reason": direction.reason,
            "rounded": level.rounded,
        },
    )
    return TradeDecision(
        signal=signal,
        level=level,
        direction=direction,
        entry_candle=candle,
    )
