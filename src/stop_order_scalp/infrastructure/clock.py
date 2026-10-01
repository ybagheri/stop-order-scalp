"""Infrastructure implementations of the :class:`~stop_order_scalp.domain.interfaces.Clock`.

Two clocks, and the distinction is not cosmetic:

:class:`SystemClock`
    Local machine time, used only for scheduling and for logging when nothing better is
    available. Its absolute value is never used for a trading decision.
:class:`FixedClock`
    A deterministic clock for tests, the backtester and replay.

Broker server time is deliberately **not** implemented here. It comes from the broker
(:meth:`stop_order_scalp.domain.interfaces.AccountReader.server_time`) because it is
the only clock whose value is trustworthy for candle boundaries, and inventing a local
approximation of it would be exactly the kind of silent assumption this project exists to
avoid.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

__all__ = ["FixedClock", "SystemClock", "sleep", "utc_now"]


def utc_now() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(UTC)


def sleep(seconds: float) -> None:
    """Block for ``seconds``.

    Lives here so that ``import time`` appears in exactly one module. The architecture check
    refuses it anywhere else, which is what makes "no sleeping in the trading pipeline" a
    structural rule rather than a code-review habit -- a test injects its own recorder
    instead, and :mod:`stop_order_scalp.execution.retry` takes it as a parameter.
    """
    if seconds < 0:
        raise ValueError(f"cannot sleep for a negative duration, got {seconds}")
    import time

    time.sleep(seconds)


class SystemClock:
    """Local machine time, timezone-aware.

    Suitable for intervals, deadlines and log timestamps. Not suitable for deciding
    whether a candle has closed -- that comparison belongs to broker server time.
    """

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin, immune to clock adjustments.

        Used for deadlines and backoff so that a DST jump or an NTP correction cannot
        turn a bounded retry into an unbounded one.
        """
        import time

        return time.monotonic()

    def __repr__(self) -> str:
        return "SystemClock()"


class FixedClock:
    """A clock that only moves when told to.

    Deterministic tests and replay depend on this. Nothing in the trading pipeline is
    allowed to call :func:`time.time` or :func:`datetime.now` directly; passing
    ``FixedClock`` in is how that is enforced by convention.
    """

    __slots__ = ("_now", "_origin")

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None or start.tzinfo.utcoffset(start) is None:
            raise ValueError(f"FixedClock requires a timezone-aware datetime, got {start!r}")
        self._now = start.astimezone(UTC)
        self._origin = 0.0

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float | timedelta) -> datetime:
        """Move the clock forward. Returns the new time."""
        delta = seconds if isinstance(seconds, timedelta) else timedelta(seconds=seconds)
        if delta < timedelta(0):
            raise ValueError("FixedClock cannot move backwards")
        self._now = self._now + delta
        return self._now

    def set(self, moment: datetime) -> datetime:
        """Jump to an absolute moment. Must be timezone-aware and not before now."""
        if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
            raise ValueError(f"FixedClock requires a timezone-aware datetime, got {moment!r}")
        target = moment.astimezone(UTC)
        if target < self._now:
            raise ValueError("FixedClock cannot move backwards")
        self._now = target
        return self._now

    def monotonic(self) -> float:
        return self._origin

    def tick_monotonic(self, seconds: float) -> float:
        """Advance the monotonic reading without moving wall time.

        Exists so that retry-deadline tests can be written without sleeping.
        """
        self._origin += seconds
        return self._origin

    def __repr__(self) -> str:
        return f"FixedClock({self._now.isoformat()})"
