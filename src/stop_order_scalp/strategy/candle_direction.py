"""Which M15 candle decides the direction, and what it decides.

The baseline rule is one sentence: **the last fully closed M15 candle's body decides the
direction.** A close above its open permits BUY; a close below permits SELL.

Three things about that rule are worth stating, because each is a way it could quietly
become a different strategy.

**Closed, always, in the baseline.** A forming candle's close is not yet known. Trading off
it is repainting in live and look-ahead in a backtest. The selection is therefore
configurable (:class:`~stop_order_scalp.domain.enums.TimeframeSelection`) but the
non-baseline option is never chosen implicitly.

**A doji has no direction.** ``close == open`` is not a weak buy or a weak sell, it is an
absence of information, and the honest response to it is not to trade. ``Candle.direction``
already returns ``None``; this module refuses to invent one.

**A missing M15 candle is not a neutral candle.** If the feed has no closed M15 bar, the
answer is "cannot decide", which is a different result from "decides flat". Both are
represented, and neither is an error, because neither is exceptional in a running system.

Every function here is pure: no clock, no I/O, no configuration lookup. The reference
moment is always an argument.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from stop_order_scalp.domain.enums import Side
from stop_order_scalp.domain.models import Candle
from stop_order_scalp.domain.value_objects import Price
from stop_order_scalp.market_data.candles import FreezeReport, freeze_closed_bars
from stop_order_scalp.market_data.timeframes import Timeframe, canonical_name

__all__ = [
    "DirectionDecision",
    "DirectionVerdict",
    "decide_direction",
    "direction_candle",
    "select_direction_candle",
]


class DirectionVerdict:
    """Why the direction filter produced what it produced.

    A namespace of constants rather than an enum, so a verdict can be compared with ``==``
    against a plain string in a log or a test assertion without importing anything.

    ``ALLOW_BUY`` and ``ALLOW_SELL`` are the only two that lead to a trade. Every other
    value means *do nothing*, and each names a distinct reason so an operator can tell a
    quiet market from a broken feed.
    """

    ALLOW_BUY: str = "allow_buy"
    ALLOW_SELL: str = "allow_sell"
    NO_CANDLES: str = "no_candles"
    DOJI: str = "doji"
    TIMEFRAME_MISMATCH: str = "timeframe_mismatch"


@dataclass(frozen=True, slots=True)
class DirectionDecision:
    """The direction filter's answer, and the candle that produced it.

    ``side is None`` with a ``verdict`` of ``ALLOW_BUY`` or ``ALLOW_SELL`` would be a
    contradiction, and the constructor rejects it, so a caller that checks only ``side``
    cannot be misled by a verdict.
    """

    side: Side | None
    verdict: str
    #: The M15 candle the decision was read from, by open time. ``None`` when none was
    #: available, which is the case an operator most needs to see named.
    candle: Candle | None
    reason: str
    reference: datetime

    @property
    def allows_trading(self) -> bool:
        """Whether this verdict authorises a trade. Check this, not ``side is not None``."""
        return self.verdict in (DirectionVerdict.ALLOW_BUY, DirectionVerdict.ALLOW_SELL)

    @property
    def is_indeterminate(self) -> bool:
        """Whether the filter could not decide, as opposed to deciding not to trade."""
        return self.verdict in (DirectionVerdict.NO_CANDLES, DirectionVerdict.TIMEFRAME_MISMATCH)

    def __str__(self) -> str:
        if self.candle is None:
            return f"DirectionDecision({self.verdict}: {self.reason})"
        stamp = self.candle.open_time.isoformat()
        return f"DirectionDecision({self.verdict} from M15@{stamp}: {self.reason})"


def decide_direction(
    candles: Sequence[Candle],
    *,
    reference: datetime,
    timeframe: str | int = Timeframe.M15,
) -> DirectionDecision:
    """Read the direction from the last fully closed ``timeframe`` candle.

    :param candles: any order, from any source. Only closed bars are considered.
    :param reference: the moment to judge closure against, in broker server time. Passed
        in rather than read, so the answer is a pure function of its inputs.
    :param timeframe: the direction timeframe. ``M15`` for the baseline.

    The result is one of five verdicts. Two authorise a trade; three do not, and each of
    those three is a different operational situation.
    """
    name = canonical_name(timeframe)
    usable = [candle for candle in candles if candle.timeframe == name]

    if not usable:
        return DirectionDecision(
            side=None,
            verdict=DirectionVerdict.NO_CANDLES,
            candle=None,
            reason=f"no {name} candles were supplied; the direction cannot be decided",
            reference=reference,
        )

    closed, _ = freeze_closed_bars(usable, reference=reference, timeframe=name)
    if not closed:
        return DirectionDecision(
            side=None,
            verdict=DirectionVerdict.NO_CANDLES,
            candle=None,
            reason=(
                f"none of the {len(usable)} {name} candles had closed at "
                f"{reference.isoformat()}"
            ),
            reference=reference,
        )

    candle = closed[-1]
    if candle.direction is None:
        return DirectionDecision(
            side=None,
            verdict=DirectionVerdict.DOJI,
            candle=candle,
            reason=(
                f"{name}@{candle.open_time.isoformat()} closed exactly on its open; "
                "a doji carries no direction and must not be traded as one"
            ),
            reference=reference,
        )

    side = candle.direction
    body = candle.body
    return DirectionDecision(
        side=side,
        verdict=(
            DirectionVerdict.ALLOW_BUY if side is Side.SIDE_BUY else DirectionVerdict.ALLOW_SELL
        ),
        candle=candle,
        reason=(
            f"{name}@{candle.open_time.isoformat()} closed {side} its open by {body} "
            f"({candle.open} -> {candle.close})"
        ),
        reference=reference,
    )


def select_direction_candle(
    candles: Sequence[Candle],
    *,
    reference: datetime,
    timeframe: str | int = Timeframe.M15,
) -> Candle | None:
    """The candle :func:`decide_direction` would read, or ``None``.

    Provided so a caller that needs the candle itself — a journal entry, a diagnostic — does
    not have to re-implement the selection and risk disagreeing with the decision.
    """
    return decide_direction(candles, reference=reference, timeframe=timeframe).candle


def direction_candle(
    candles: Sequence[Candle],
    *,
    reference: datetime,
    timeframe: str | int = Timeframe.M15,
) -> tuple[Candle | None, FreezeReport | None]:
    """The selected candle plus the freeze report, for a caller that wants both.

    Returns ``(None, None)`` when there is no candle at all, so a caller can distinguish
    "nothing to decide from" from "decided from this".
    """
    name = canonical_name(timeframe)
    usable = [candle for candle in candles if candle.timeframe == name]
    if not usable:
        return None, None
    closed, report = freeze_closed_bars(usable, reference=reference, timeframe=name)
    return (closed[-1] if closed else None), report


def previous_direction_candle(
    candles: Sequence[Candle],
    *,
    reference: datetime,
    timeframe: str | int = Timeframe.M15,
) -> Candle | None:
    """The closed candle *before* the one that currently decides.

    Not used by the baseline, which is a single-candle rule. It exists so that a Phase 12
    filter asking "did the direction agree with the previous bar?" does not have to
    re-derive the selection, and cannot accidentally disagree with it.
    """
    name = canonical_name(timeframe)
    usable = [candle for candle in candles if candle.timeframe == name]
    closed, _ = freeze_closed_bars(usable, reference=reference, timeframe=name)
    return closed[-2] if len(closed) >= 2 else None


def summarise(decision: DirectionDecision) -> dict[str, Any]:
    """A JSON-friendly view, for the audit log and the ``status`` command."""
    return {
        "side": None if decision.side is None else str(decision.side),
        "verdict": decision.verdict,
        "reason": decision.reason,
        "reference": decision.reference.isoformat(),
        "allows_trading": decision.allows_trading,
        "is_indeterminate": decision.is_indeterminate,
        "candle": None
        if decision.candle is None
        else {
            "open_time": decision.candle.open_time.isoformat(),
            "timeframe": decision.candle.timeframe,
            "open": str(decision.candle.open),
            "high": str(decision.candle.high),
            "low": str(decision.candle.low),
            "close": str(decision.candle.close),
            "body": str(decision.candle.body),
            "is_doji": decision.candle.is_doji,
        },
    }


def price_of(candle: Candle | None) -> Price | None:
    """The candle's close, or ``None``. A convenience for callers holding a decision."""
    return None if candle is None else candle.close
