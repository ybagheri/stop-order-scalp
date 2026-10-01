"""``classify`` decides whether a retcode means "not sent" or "maybe sent".

The whole test file exists for one assertion: an ambiguous retcode must **never** be
classified as retryable. That single property is what stops one order becoming two, so it is
checked explicitly and by enumeration rather than left to the reader to verify.
"""

from __future__ import annotations

import pytest

from stop_order_scalp.domain.exceptions import (
    BrokerRejectedError,
    ExecutionUnknownError,
    RetryableError,
)
from stop_order_scalp.execution.mt5_broker import _ACCEPTED, _UNKNOWN, classify


class TestAccepted:
    @pytest.mark.parametrize("retcode", sorted(_ACCEPTED))
    def test_an_accepted_code_does_not_raise(self, retcode: int) -> None:
        classify(retcode, "request placed", context="placing a BUY_STOP")


class TestUnknown:
    """Ambiguous codes. The dangerous ones, and the reason this module exists."""

    @pytest.mark.parametrize("retcode", sorted(_UNKNOWN))
    def test_an_ambiguous_code_raises_execution_unknown(self, retcode: int) -> None:
        with pytest.raises(ExecutionUnknownError):
            classify(retcode, "no detail", context="placing a BUY_STOP")

    @pytest.mark.parametrize("retcode", sorted(_UNKNOWN))
    def test_an_ambiguous_code_is_never_retryable(self, retcode: int) -> None:
        """The one property that must hold for every ambiguous code.

        ``RetryableError`` invites a resend. If any ambiguous code reached it, the caller
        would send the order again having no idea whether the first one landed.
        """
        with pytest.raises(ExecutionUnknownError) as caught:
            classify(retcode, "no detail", context="placing a BUY_STOP")
        assert not isinstance(caught.value, RetryableError)

    @pytest.mark.parametrize("retcode", sorted(_UNKNOWN))
    def test_the_message_tells_the_caller_what_to_do(self, retcode: int) -> None:
        """A 3am operator needs the instruction, not just the classification."""
        with pytest.raises(ExecutionUnknownError, match="Do NOT resend"):
            classify(retcode, "no detail", context="placing a BUY_STOP")

    def test_timeout_is_ambiguous(self) -> None:
        """A timeout is the classic case: the request may well have been processed."""
        with pytest.raises(ExecutionUnknownError):
            classify(10012, "timeout", context="placing a BUY_STOP")


class TestTerminal:
    @pytest.mark.parametrize("retcode", [10006, 10013, 10014, 10015, 10016, 10017, 10019])
    def test_a_refusal_on_merits_is_not_retryable(self, retcode: int) -> None:
        """A resend would be refused identically, so retrying wastes the chance something changed."""
        with pytest.raises(BrokerRejectedError):
            classify(retcode, "invalid stops", context="placing a BUY_STOP")

    def test_invalid_stops_is_terminal(self) -> None:
        """The most common Phase 5 rejection, and a permanent one until the plan changes."""
        with pytest.raises(BrokerRejectedError):
            classify(10016, "invalid stops", context="placing a BUY_STOP")


class TestRetryable:
    @pytest.mark.parametrize("retcode", [10018, 10020, 10021, 10024])
    def test_a_temporary_failure_is_retryable(self, retcode: int) -> None:
        """These genuinely mean "not now": the order certainly did not reach the venue."""
        with pytest.raises(RetryableError):
            classify(retcode, "market closed", context="placing a BUY_STOP")

    def test_market_closed_is_retryable(self) -> None:
        with pytest.raises(RetryableError):
            classify(10018, "market closed", context="placing a BUY_STOP")


class TestUnrecognised:
    def test_an_unknown_code_is_treated_as_ambiguous_not_terminal(self) -> None:
        """Being wrong pessimistically costs a re-read. Being wrong optimistically costs a duplicate."""
        with pytest.raises(ExecutionUnknownError, match="unrecognised"):
            classify(31337, "who knows", context="placing a BUY_STOP")

    @pytest.mark.parametrize("retcode", [-999, 0, 1, 99999])
    def test_no_unrecognised_code_is_retryable(self, retcode: int) -> None:
        with pytest.raises(ExecutionUnknownError):
            classify(retcode, "?", context="placing a BUY_STOP")


class TestMessages:
    def test_the_context_reaches_the_operator(self) -> None:
        with pytest.raises(BrokerRejectedError, match="placing a BUY_STOP on US30"):
            classify(10015, "invalid price", context="placing a BUY_STOP on US30")

    def test_the_retcode_is_reported(self) -> None:
        with pytest.raises(BrokerRejectedError, match="10019"):
            classify(10019, "no money", context="placing a BUY_STOP on US30")
