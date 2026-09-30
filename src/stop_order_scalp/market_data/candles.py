"""Closed-candle selection, and the report that says what was excluded.

This module exists because of one failure mode: reading a candle that has not finished
yet. A forming candle's high and low keep moving, so an entry price computed from one is
a number that will change -- and in a historical replay, where the recorded high is the
high the bar *eventually* reached, it is the future. That is look-ahead bias, and it makes
a backtest look far better than reality.

The defence is structural rather than a matter of discipline:

* :func:`freeze_closed_bars` is the only way candles reach a decision. It drops every bar
  whose close time is after an explicitly supplied reference moment and returns a
  :class:`FreezeReport` describing exactly what it removed.
* The reference moment is an argument. Nothing here reads a clock, so the same input always
  produces the same output and the result is testable.
* A forming bar is still reachable, but only by asking for it by name. There is no way to
  get one by accident.

The Al Brooks engine has an independent implementation of the same rule
(``freeze_closed_bars`` in ``albrooks``). Decision 11 of the project requires the two to
agree; ``tests/unit/test_candles.py`` cross-tests them once the optional extra is
installed, and skips that comparison cleanly when it is not.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from stop_order_scalp.domain.exceptions import MarketDataError
from stop_order_scalp.domain.models import Candle
from stop_order_scalp.market_data.timeframes import canonical_name, is_aligned, period_seconds

__all__ = [
    "FreezeReport",
    "freeze_closed_bars",
    "require_closed_only",
    "select_closed",
]


@dataclass(frozen=True, slots=True)
class FreezeReport:
    """What :func:`freeze_closed_bars` kept, and what it deliberately dropped.

    Recorded rather than merely logged, because "which bars did the decision actually see"
    is the first question anyone asks when a result looks wrong, and reconstructing it
    later from logs is guesswork. :attr:`dropped_forming` is the number that matters: it is
    the count of bars that were available from the feed and withheld as not yet closed.
    """

    timeframe: str
    reference: datetime
    requested: int
    returned: int
    dropped_forming: int
    dropped_unaligned: int
    reordered: bool
    oldest_open: datetime | None
    newest_open: datetime | None
    forming_open: datetime | None = None

    @property
    def is_empty(self) -> bool:
        """No closed bar survived. Callers must treat this as "no signal", not "retry"."""
        return self.returned == 0

    @property
    def saw_forming_bar(self) -> bool:
        """Whether the feed offered at least one bar that was still forming."""
        return self.dropped_forming > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "reference": self.reference.isoformat(),
            "requested": self.requested,
            "returned": self.returned,
            "dropped_forming": self.dropped_forming,
            "dropped_unaligned": self.dropped_unaligned,
            "reordered": self.reordered,
            "oldest_open": None if self.oldest_open is None else self.oldest_open.isoformat(),
            "newest_open": None if self.newest_open is None else self.newest_open.isoformat(),
            "forming_open": None if self.forming_open is None else self.forming_open.isoformat(),
        }

    def __str__(self) -> str:
        return (
            f"FreezeReport({self.timeframe}, kept {self.returned}/{self.requested}, "
            f"dropped {self.dropped_forming} forming, {self.dropped_unaligned} unaligned, "
            f"reference {self.reference.isoformat()})"
        )


def freeze_closed_bars(
    series: Iterable[Candle],
    *,
    reference: datetime,
    timeframe: str | int | None = None,
) -> tuple[tuple[Candle, ...], FreezeReport]:
    """Keep only the bars that had fully formed at ``reference``.

    :param series: candles from any source, in any order.
    :param reference: the moment to judge closure against, in **broker server time**. Not
        read from a clock, by design: the caller owns the decision of which clock it is.
    :param timeframe: expected timeframe. When given, a bar of a different timeframe is an
        error rather than something to silently skip -- a mixed series means the caller
        passed the wrong array, and quietly returning the other timeframe's bars would
        hide that.
    :returns: the closed bars oldest-first, and a :class:`FreezeReport`.
    :raises MarketDataError: for a naive ``reference``, a mixed series, or duplicate bars.
    """
    _require_aware(reference)
    expected = None if timeframe is None else canonical_name(timeframe)
    bars = list(series)

    ordered = tuple(sorted(bars, key=lambda candle: candle.open_time))
    reordered = any(a.open_time != b.open_time for a, b in zip(ordered, bars, strict=False))

    _reject_duplicates(ordered)
    if expected is not None:
        _reject_mixed_timeframes(ordered, expected)

    kept: list[Candle] = []
    dropped_forming = 0
    dropped_unaligned = 0
    forming_open: datetime | None = None

    for candle in ordered:
        if expected is not None and not is_aligned(candle.open_time, expected):
            dropped_unaligned += 1
            continue
        if candle.is_closed_at(reference):
            kept.append(candle)
        else:
            dropped_forming += 1
            if forming_open is None or candle.open_time > forming_open:
                forming_open = candle.open_time

    report = FreezeReport(
        timeframe=expected or "",
        reference=reference,
        requested=len(bars),
        returned=len(kept),
        dropped_forming=dropped_forming,
        dropped_unaligned=dropped_unaligned,
        reordered=reordered,
        oldest_open=kept[0].open_time if kept else None,
        newest_open=kept[-1].open_time if kept else None,
        forming_open=forming_open,
    )
    return tuple(kept), report


def select_closed(
    series: Sequence[Candle],
    *,
    reference: datetime,
    timeframe: str | int | None = None,
    limit: int | None = None,
) -> tuple[Candle, ...]:
    """The closed bars, newest first, without the report.

    For call sites that only want the answer. :func:`freeze_closed_bars` is the one to use
    when the exclusion has to be visible afterwards.
    """
    kept, _ = freeze_closed_bars(series, reference=reference, timeframe=timeframe)
    newest_first = tuple(reversed(kept))
    if limit is not None:
        if limit <= 0:
            raise MarketDataError(f"limit must be positive, got {limit}")
        return newest_first[:limit]
    return newest_first


def require_closed_only(candles: Sequence[Candle], *, reference: datetime) -> None:
    """Raise unless every bar in ``candles`` was closed at ``reference``.

    A guard for functions that must never see a forming bar -- entry pricing, the M15
    direction filter, backtest statistics. Cheap, and it turns a silent look-ahead bug into
    a loud failure at the point of misuse.
    """
    _require_aware(reference)
    offenders = [candle.open_time for candle in candles if not candle.is_closed_at(reference)]
    if offenders:
        raise MarketDataError(
            f"{len(offenders)} candle(s) were not closed at {reference.isoformat()}; "
            f"newest offending open time {max(offenders).isoformat()}"
        )


def _reject_duplicates(ordered: Sequence[Candle]) -> None:
    seen: set[datetime] = set()
    for candle in ordered:
        if candle.open_time in seen:
            raise MarketDataError(
                f"duplicate candle for {candle.timeframe} open time "
                f"{candle.open_time.isoformat()}; a feed must not repeat a bar"
            )
        seen.add(candle.open_time)


def _reject_mixed_timeframes(ordered: Sequence[Candle], expected: str) -> None:
    offenders = sorted({candle.timeframe for candle in ordered if candle.timeframe != expected})
    if offenders:
        raise MarketDataError(
            f"expected {expected} candles but the series contains {offenders}; "
            "the caller passed the wrong array rather than a wrong count"
        )
    seconds = period_seconds(expected)
    wrong_length = sorted({candle.timeframe_seconds for candle in ordered if candle.timeframe_seconds != seconds})
    if wrong_length:
        raise MarketDataError(
            f"{expected} is {seconds} seconds but the series carries {wrong_length}; "
            "candle length and timeframe name disagree"
        )


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise MarketDataError(
            f"naive datetime {moment!r}; candle closure must be judged in broker server time"
        )
