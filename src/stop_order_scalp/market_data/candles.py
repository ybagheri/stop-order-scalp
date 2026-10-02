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

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from stop_order_scalp.domain.exceptions import MarketDataError
from stop_order_scalp.domain.models import Candle
from stop_order_scalp.domain.value_objects import Price
from stop_order_scalp.market_data.timeframes import canonical_name, is_aligned, period_seconds

__all__ = [
    "FreezeReport",
    "aggregate",
    "freeze_closed_bars",
    "load_candles_csv",
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


# =============================================================================
# Series construction
#
# Moved here from ``application/service.py``. All of it is pure candle manipulation with
# no knowledge of trading, and the layering requires the move: ``backtest`` sits inside
# ``application``, so it may import this module but not the one these used to live in.
# Leaving them there would have meant either duplicating them or reaching across a layer
# boundary to avoid a copy.
# =============================================================================


def load_candles_csv(path: Path, *, timeframe: str = "M1", digits: int = 1) -> list[Candle]:
    """Read ``time,open,high,low,close`` CSV, oldest first.

    ``time`` is epoch seconds, ISO-8601, or ``YYYY-MM-DD HH:MM:SS``. Rows whose bar has not
    closed by the time implied by the series are *kept* — the freeze decides what is closed,
    and pre-filtering here would duplicate that rule in a second place.

    **Lines whose first cell starts with ``#`` are comments**, anywhere in the file including
    before the header. So a data file can state its own provenance: the safest place for "this
    is synthetic, do not trade on it" is the top of the file itself, because a separate README
    is one more thing nobody opens and it drifts out of date. Line numbers in errors count
    every physical line, comments included, so a message points at the line the reader is
    actually looking at.

    :raises ValueError: with the line number, because a CSV with a bad row in it is
        otherwise a mystery that shows up much later as a wrong entry price.
    """
    seconds = period_seconds(timeframe)
    rows: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal]] = []
    header_seen = False
    with path.open(encoding="utf-8", newline="") as handle:
        for number, raw in enumerate(csv.reader(handle), start=1):
            if not raw or not any(cell.strip() for cell in raw):
                continue
            if raw[0].lstrip().startswith("#"):
                continue
            if not header_seen:
                header_seen = True
                if raw[0].strip().lower() in ("time", "datetime", "date"):
                    # A real header. Everything after it is data.
                    continue
                # No header at all: this row is data, and is parsed as such below.
            rows.append(_parse_row(raw, number))
    rows.sort(key=lambda row: row[0])
    del seconds
    return [
        Candle(
            open_time=moment,
            open=Price(open_, digits),
            high=Price(high, digits),
            low=Price(low, digits),
            close=Price(close, digits),
            timeframe_seconds=period_seconds(timeframe),
            timeframe=timeframe,
            volume=Decimal("1"),
            tick_volume=1,
            # Decided by freeze_closed_bars against the clock, never here.
            is_confirmed=True,
        )
        for moment, open_, high, low, close in rows
    ]


def _parse_row(
    raw: Sequence[str], number: int
) -> tuple[datetime, Decimal, Decimal, Decimal, Decimal]:
    if len(raw) < 5:
        raise ValueError(f"line {number}: expected 5 columns, got {len(raw)}: {raw!r}")
    try:
        moment = _parse_time(raw[0].strip())
        return (
            moment,
            Decimal(raw[1]),
            Decimal(raw[2]),
            Decimal(raw[3]),
            Decimal(raw[4]),
        )
    except (ValueError, ArithmeticError) as exc:
        raise ValueError(f"line {number}: {exc}") from exc


def _parse_time(text: str) -> datetime:
    if text.replace(".", "", 1).isdigit():
        return datetime.fromtimestamp(float(text), tz=UTC)
    cleaned = text.replace("Z", "+00:00")
    moment = datetime.fromisoformat(cleaned)
    if moment.tzinfo is None:
        # A naive timestamp is machine-local time, which is exactly the bug the project's
        # clock rule exists to prevent. Reading it as UTC is the safe, stated assumption.
        return moment.replace(tzinfo=UTC)
    return moment


def aggregate(candles: Sequence[Candle], source: str, target: str) -> list[Candle]:
    """Build ``target`` candles by grouping ``source`` bars.

    Needed for a dry run with a single M1 CSV: the M15 direction filter refuses a
    timeframe mismatch rather than trying to interpret M1 bars as M15, so the higher series
    has to exist. Aggregating here rather than lying to the filter is the point — a filter
    fed the wrong timeframe and *accepting* it would be a look-ahead-shaped bug.
    """
    if not candles:
        return []
    ratio = period_seconds(target) // period_seconds(source)
    if ratio < 1:
        return []
    grouped: list[Candle] = []
    for start in range(0, len(candles) - ratio + 1, ratio):
        window = candles[start : start + ratio]
        first = window[0]
        grouped.append(
            Candle(
                open_time=first.open_time,
                open=first.open,
                high=max(candle.high for candle in window),
                low=min(candle.low for candle in window),
                close=window[-1].close,
                timeframe_seconds=period_seconds(target),
                timeframe=target,
                volume=sum((candle.volume for candle in window), Decimal("0")),
                tick_volume=sum(candle.tick_volume for candle in window),
                is_confirmed=True,
            )
        )
    return grouped
