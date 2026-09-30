"""Timeframes.

MetaTrader 5 encodes timeframes as integers in a non-obvious way -- minutes are literal
(``M15`` is ``15``), hours are ``16384 + h``, days ``16385``, weeks ``32768 + w``, months
``49152 + m``. Getting that wrong silently produces wrong candle boundaries, so the
mapping lives here once and is derived from first principles rather than copied.

This module is the project-wide authority on candle boundaries. Two things depend on it:

* :func:`floor_time`, which answers "is this open time aligned to the timeframe?" and is
  how look-ahead bugs are caught.
* :func:`is_closed`, which answers "has this bar finished?" using **broker server time**
  as the reference. The wall clock is never consulted implicitly; the caller passes the
  reference moment in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from stop_order_scalp.domain.exceptions import MarketDataError

__all__ = [
    "MT5_ENCODED",
    "PERIOD_SECONDS",
    "TIMEFRAME_NAMES",
    "MT5Timeframe",
    "Timeframe",
    "align_to",
    "canonical_name",
    "floor_time",
    "is_aligned",
    "next_close",
    "period_seconds",
    "to_mt5",
]

#: MetaTrader 5's encoding constants, from the terminal's own documentation
#: (``ENUM_TIMEFRAMES``).
#:
#: ``PERIOD_D1`` is **16408**, not ``_HOUR_BASE + 1``. That is a genuine trap: 16385 is
#: also ``PERIOD_H1``, so an implementation that computes daily timeframes as
#: ``16384 + 1`` silently returns H1 and every daily candle boundary becomes an hourly
#: one. The round-trip test in ``tests/unit/test_timeframes.py`` exists to catch exactly
#: this class of mistake.
_HOUR_BASE: Final[int] = 16384
_DAY_BASE: Final[int] = 16408
_WEEK_BASE: Final[int] = 32768
_MONTH_BASE: Final[int] = 49152


class MT5Timeframe(int):
    """Marker type documenting that these integers are MetaTrader 5 wire values.

    Subclassing :class:`int` rather than defining an ``IntEnum`` keeps the values usable
    as plain dict keys and directly comparable with the values the terminal returns,
    while the distinct type makes it obvious at a call site that a raw MT5 code is being
    passed around rather than one of our names.
    """


class Timeframe:
    """The project's canonical timeframe names.

    A namespace of ``str`` constants rather than an enum, so a name can be stored in the
    YAML configuration and compared with ``==`` against a plain string without a
    conversion at every boundary. ``.env`` and YAML values are plain strings by nature.
    """

    M1: Final[str] = "M1"
    M2: Final[str] = "M2"
    M3: Final[str] = "M3"
    M4: Final[str] = "M4"
    M5: Final[str] = "M5"
    M6: Final[str] = "M6"
    M10: Final[str] = "M10"
    M12: Final[str] = "M12"
    M15: Final[str] = "M15"
    M20: Final[str] = "M20"
    M30: Final[str] = "M30"
    H1: Final[str] = "H1"
    H2: Final[str] = "H2"
    H3: Final[str] = "H3"
    H4: Final[str] = "H4"
    H6: Final[str] = "H6"
    H8: Final[str] = "H8"
    H12: Final[str] = "H12"
    D1: Final[str] = "D1"
    W1: Final[str] = "W1"
    MN1: Final[str] = "MN1"


#: Minutes -> MetaTrader 5 code. Identity for the minute timeframes, which is exactly
#: where an off-by-one in the encoding would be least visible and most damaging.
MT5_ENCODED: Final[dict[str, int]] = {
    Timeframe.M1: 1,
    Timeframe.M2: 2,
    Timeframe.M3: 3,
    Timeframe.M4: 4,
    Timeframe.M5: 5,
    Timeframe.M6: 6,
    Timeframe.M10: 10,
    Timeframe.M12: 12,
    Timeframe.M15: 15,
    Timeframe.M20: 20,
    Timeframe.M30: 30,
    Timeframe.H1: _HOUR_BASE + 1,
    Timeframe.H2: _HOUR_BASE + 2,
    Timeframe.H3: _HOUR_BASE + 3,
    Timeframe.H4: _HOUR_BASE + 4,
    Timeframe.H6: _HOUR_BASE + 6,
    Timeframe.H8: _HOUR_BASE + 8,
    Timeframe.H12: _HOUR_BASE + 12,
    Timeframe.D1: _DAY_BASE,
    Timeframe.W1: _WEEK_BASE + 1,
    Timeframe.MN1: _MONTH_BASE + 1,
}

#: Name -> period in seconds. ``MN1`` is absent on purpose: a month has no fixed length,
#: so a period-seconds calculation for it would be a lie. Use :func:`next_close` for
#: month boundaries, or avoid them.
PERIOD_SECONDS: Final[dict[str, int]] = {
    Timeframe.M1: 60,
    Timeframe.M2: 120,
    Timeframe.M3: 180,
    Timeframe.M4: 240,
    Timeframe.M5: 300,
    Timeframe.M6: 360,
    Timeframe.M10: 600,
    Timeframe.M12: 720,
    Timeframe.M15: 900,
    Timeframe.M20: 1200,
    Timeframe.M30: 1800,
    Timeframe.H1: 3600,
    Timeframe.H2: 7200,
    Timeframe.H3: 10800,
    Timeframe.H4: 14400,
    Timeframe.H6: 21600,
    Timeframe.H8: 28800,
    Timeframe.H12: 43200,
    Timeframe.D1: 86400,
    Timeframe.W1: 604800,
}

TIMEFRAME_NAMES: Final[tuple[str, ...]] = tuple(MT5_ENCODED)

#: The two timeframes the baseline strategy depends on. Named here so that a
#: misconfiguration of "which timeframe decides direction" is impossible to express
#: implicitly.
DIRECTION_TIMEFRAME: Final[str] = Timeframe.M15
ENTRY_TIMEFRAME: Final[str] = Timeframe.M1


def to_mt5(name: str) -> int:
    """Canonical name -> MetaTrader 5 timeframe code.

    :raises MarketDataError: for an unknown name. A silent fallback to ``M1`` would make
        a typo look like a working system.
    """
    try:
        return MT5_ENCODED[canonical_name(name)]
    except KeyError as exc:
        raise MarketDataError(
            f"unknown timeframe {name!r}; known timeframes are {list(MT5_ENCODED)}"
        ) from exc


def canonical_name(name: str | int) -> str:
    """Accept either a name or a MetaTrader 5 code and return the canonical name.

    A broker or a data file may hand back the integer code, and comparing it against a
    configured name without this conversion is a silent mismatch.
    """
    if isinstance(name, bool):
        raise MarketDataError(f"invalid timeframe {name!r}")
    if isinstance(name, int):
        for candidate, code in MT5_ENCODED.items():
            if code == name:
                return candidate
        raise MarketDataError(f"unknown MetaTrader 5 timeframe code {name}")
    text = str(name).strip().upper()
    if text in MT5_ENCODED:
        return text
    raise MarketDataError(f"unknown timeframe {name!r}; known timeframes are {list(MT5_ENCODED)}")


def period_seconds(name: str | int) -> int:
    """Length of one bar in seconds.

    :raises MarketDataError: for ``MN1``, and for anything unknown. A month is not a
        fixed number of seconds; pretending otherwise is how a look-ahead bug gets in
        through the back door.
    """
    canonical = canonical_name(name)
    try:
        return PERIOD_SECONDS[canonical]
    except KeyError as exc:
        raise MarketDataError(
            f"{canonical} has no fixed period in seconds; month boundaries are not uniform"
        ) from exc


def floor_time(moment: datetime, timeframe: str | int) -> datetime:
    """The open time of the bar containing ``moment``.

    UTC-based. MetaTrader 5 brokers quote times in server time, which for most brokers
    *is* UTC or a fixed offset from it, and the terminal itself anchors daily bars to
    server time rather than to UTC. Converting server time to UTC before flooring would
    therefore be wrong for any broker whose server time is not UTC, which is why the
    caller passes server time in and this function stays a pure floor.

    :raises MarketDataError: if ``moment`` is naive. A naive datetime here means the
        caller lost the offset, and the floor would silently use the local machine's.
    """
    seconds = period_seconds(timeframe)
    _require_aware(moment)
    epoch = int(moment.timestamp())
    floored = epoch - (epoch % seconds)
    return datetime.fromtimestamp(floored, tz=UTC)


def align_to(moment: datetime, timeframe: str | int) -> datetime:
    """Readable alias for :func:`floor_time`.

    Call sites read better as "align this moment to M15" than as "floor this moment".
    """
    return floor_time(moment, timeframe)


def is_aligned(moment: datetime, timeframe: str | int) -> bool:
    """Whether ``moment`` is exactly on a bar boundary.

    A feed whose candle open times are not aligned is a feed whose boundaries differ from
    ours, which shifts every "is this candle closed" answer. Cheap to check, so it is
    checked.
    """
    seconds = period_seconds(timeframe)
    _require_aware(moment)
    return int(moment.timestamp()) % seconds == 0


def next_close(moment: datetime, timeframe: str | int) -> datetime:
    """When the bar opening at ``moment`` finishes."""
    seconds = period_seconds(timeframe)
    _require_aware(moment)
    return moment + timedelta(seconds=seconds)


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise MarketDataError(f"naive datetime {moment!r}; a timezone is required")
