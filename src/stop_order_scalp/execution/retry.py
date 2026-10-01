"""Bounded exponential backoff for reads that are safe to repeat.

The distinction this module exists to make, in one line:

    **a read can be retried; a send cannot.**

Reading the order book twice returns the same answer, so a transient connection failure
costs nothing to ride out. Sending an order twice may open a second position, so it may
never be retried -- not here, not in the broker, not anywhere. :func:`retry_read` therefore
accepts a read and refuses to accept anything else.

The bound matters as much as the backoff. An unbounded retry against a terminal that is
merely wedged turns into a hot loop against a socket that will never answer, which is how a
brief broker outage becomes a process that has to be killed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from stop_order_scalp.domain.exceptions import BrokerError, ExecutionUnknownError, RetryableError
from stop_order_scalp.infrastructure.clock import sleep as real_sleep
from stop_order_scalp.infrastructure.config import RetrySettings

__all__ = ["ReadOutcome", "retry_read"]

T = TypeVar("T")

#: Failures that mean "ask again shortly". Everything else propagates.
_RETRYABLE: tuple[type[Exception], ...] = (RetryableError, BrokerError)


class ReadOutcome:
    """How a retried read ended. Returned so a caller can log the attempt count."""

    __slots__ = ("attempts", "value")

    def __init__(self, value: T, attempts: int) -> None:
        self.value = value
        #: How many times the read was actually performed. ``1`` means it worked first time.
        self.attempts = attempts

    def __repr__(self) -> str:
        return f"ReadOutcome(attempts={self.attempts}, value={self.value!r})"


def retry_read(
    operation: Callable[[], T],
    settings: RetrySettings,
    *,
    sleep: Callable[[float], None] | None = None,
    context: str = "read",
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> ReadOutcome:
    """Call ``operation`` until it succeeds or the attempts run out.

    Only for operations that are **idempotent reads**: ``orders``, ``positions``,
    ``account``, ``specification``, ``server_time``. Passing a placement here would defeat
    the entire design, which is why the parameter is documented as a read and why the
    unknown-outcome branch refuses rather than retrying.

    :param sleep: injected so a test does not wait, and so the delay is observable.
    :param on_retry: called before each sleep with ``(attempt, delay, error)``.
    :raises RetryableError: when every attempt failed, carrying the last error.
    """
    nap = sleep if sleep is not None else real_sleep
    last: Exception | None = None
    for attempt in range(1, settings.max_attempts + 1):
        try:
            return ReadOutcome(operation(), attempt)
        except ExecutionUnknownError:
            # An ambiguous *send* is not a read failure and must never be ridden out.
            # Reached only if a caller wrongly passes a placement in here; propagating
            # immediately is the safe response to that mistake.
            raise
        except _RETRYABLE as error:
            last = error
            if attempt == settings.max_attempts:
                break
            delay = settings.delay_for(attempt)
            if on_retry is not None:
                on_retry(attempt, delay, error)
            nap(delay)

    assert last is not None  # the loop exits only via break or return, and break sets last
    raise RetryableError(
        f"{context}: gave up after {settings.max_attempts} attempts; last error was {last}"
    ) from last
