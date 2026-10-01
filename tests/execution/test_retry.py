"""Backoff is bounded, and only ever applied to reads.

The tests here pin down the two things that make retrying safe: it stops, and it never
applies to a send.
"""

from __future__ import annotations

from typing import NoReturn

import pytest

from stop_order_scalp.domain.exceptions import (
    BrokerError,
    ExecutionUnknownError,
    RetryableError,
)
from stop_order_scalp.execution.retry import retry_read
from stop_order_scalp.infrastructure.config import RetrySettings

SETTINGS = RetrySettings(max_attempts=4, base_delay_seconds=1.0, multiplier=2.0)


class Recorder:
    """Stands in for ``time.sleep`` so the backoff schedule is observable."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


class TestSucceedingImmediately:
    def test_a_first_time_success_does_not_sleep(self) -> None:
        sleeper = Recorder()

        outcome = retry_read(lambda: 42, SETTINGS, sleep=sleeper)

        assert outcome.value == 42
        assert outcome.attempts == 1
        assert sleeper.slept == []

    def test_the_attempt_count_is_reported(self) -> None:
        outcome = retry_read(lambda: "ok", SETTINGS, sleep=Recorder())

        assert outcome.value == "ok"
        assert outcome.attempts == 1


class TestBackoffSchedule:
    def test_delays_grow_exponentially(self) -> None:
        sleeper = Recorder()
        calls = {"n": 0}

        def flaky() -> NoReturn:
            calls["n"] += 1
            raise RetryableError("terminal busy")

        with pytest.raises(RetryableError):
            retry_read(flaky, SETTINGS, sleep=sleeper)

        assert sleeper.slept == [1.0, 2.0, 4.0]

    def test_the_delay_is_capped(self) -> None:
        """A long outage must not become an unbounded sleep."""
        settings = RetrySettings(
            max_attempts=6, base_delay_seconds=1.0, multiplier=10.0, max_delay_seconds=5.0
        )
        sleeper = Recorder()

        def always() -> NoReturn:
            raise RetryableError("still busy")

        with pytest.raises(RetryableError):
            retry_read(always, settings, sleep=sleeper)

        assert max(sleeper.slept) == 5.0


class TestBounded:
    def test_it_gives_up_after_max_attempts(self) -> None:
        calls: list[int] = []

        def always() -> NoReturn:
            calls.append(1)
            raise RetryableError("down")

        with pytest.raises(RetryableError, match="gave up after 4 attempts"):
            retry_read(always, SETTINGS, sleep=Recorder())

        assert len(calls) == 4

    def test_a_broker_error_is_also_retried(self) -> None:
        """A terminal that is merely disconnected is the canonical retryable read failure."""
        calls: list[int] = []

        def disconnected() -> str:
            calls.append(1)
            if len(calls) < 3:
                raise BrokerError("not connected")
            return "recovered"

        outcome = retry_read(disconnected, SETTINGS, sleep=Recorder())

        assert outcome.value == "recovered"
        assert outcome.attempts == 3

    def test_a_non_retryable_error_propagates_immediately(self) -> None:
        calls: list[int] = []

        def refused() -> NoReturn:
            calls.append(1)
            raise ValueError("bad argument")

        with pytest.raises(ValueError):
            retry_read(refused, SETTINGS, sleep=Recorder())

        assert len(calls) == 1, "a programming error must not be ridden out"


class TestNeverRetriesASend:
    def test_an_unknown_outcome_is_never_retried(self) -> None:
        """The whole point of the module, stated as a test.

        ``ExecutionUnknownError`` means the order may already be on the venue. Riding it out
        would place it again.
        """
        calls: list[int] = []

        def ambiguous() -> NoReturn:
            calls.append(1)
            raise ExecutionUnknownError("may or may not have reached the venue")

        with pytest.raises(ExecutionUnknownError):
            retry_read(ambiguous, SETTINGS, sleep=Recorder())

        assert len(calls) == 1, "an ambiguous send must not be retried"

    def test_an_unknown_outcome_never_sleeps(self) -> None:
        sleeper = Recorder()

        def ambiguous() -> NoReturn:
            raise ExecutionUnknownError("unknown")

        with pytest.raises(ExecutionUnknownError):
            retry_read(ambiguous, SETTINGS, sleep=sleeper)

        assert sleeper.slept == []


class TestObservability:
    def test_the_callback_sees_each_retry(self) -> None:
        seen: list[tuple[int, float, str]] = []
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise RetryableError("busy")
            return "done"

        retry_read(
            flaky,
            SETTINGS,
            sleep=Recorder(),
            on_retry=lambda attempt, delay, error: seen.append(
                (attempt, delay, str(error))
            ),
        )

        assert seen == [(1, 1.0, "busy"), (2, 2.0, "busy")]

    def test_the_context_reaches_the_failure_message(self) -> None:
        def always() -> NoReturn:
            raise RetryableError("down")

        with pytest.raises(RetryableError, match="reading the order book"):
            retry_read(always, SETTINGS, sleep=Recorder(), context="reading the order book")
