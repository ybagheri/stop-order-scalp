"""Exception hierarchy.

Rooted at :class:`StopOrderScalpError` so a caller can catch everything this project
raises deliberately without swallowing programming errors such as ``TypeError``.

Two distinctions matter operationally:

* :class:`RetryableError` versus :class:`TerminalError`. Only a retryable failure may
  ever be retried. Anything else must stop the cycle and be logged.
* :class:`ExecutionUnknownError`. The send may or may not have reached the trade server.
  The correct response is to re-read broker state, never to resend. This is the single
  most important failure mode in the whole system, so it has its own type.
"""

from __future__ import annotations

__all__ = [
    "BrokerError",
    "BrokerNotConnectedError",
    "BrokerRejectedError",
    "ComponentNotAvailableError",
    "ConfigError",
    "DomainError",
    "ExecutionUnknownError",
    "IllegalTransitionError",
    "IncompatibleBrokerError",
    "InstrumentNotAllowedError",
    "InvalidSpecificationError",
    "LookAheadViolationError",
    "MarginInsufficientError",
    "MarketDataError",
    "OrderValidationError",
    "PersistenceError",
    "PositionNotFoundError",
    "RetryableError",
    "RiskError",
    "StaleStateError",
    "StopOrderScalpError",
    "SymbolNotFoundError",
    "TerminalError",
]


class StopOrderScalpError(Exception):
    """Root of the hierarchy for every deliberate failure in this project."""


class DomainError(StopOrderScalpError):
    """A value object invariant was violated."""


class ConfigError(StopOrderScalpError):
    """Configuration is missing, malformed, or internally inconsistent.

    Raised during load and during validation. Never swallowed: a system running on
    half-understood parameters is worse than a system that refuses to start.
    """


class ComponentNotAvailableError(StopOrderScalpError):
    """A command was invoked before the phase that implements it has been built.

    The CLI contract is defined in Phase 1, before the market-data, execution, lifecycle
    and backtest components behind most subcommands exist. Invoking one of those
    subcommands today must produce a named, exit-coded failure rather than an
    ``ImportError`` traceback, so the contract is testable now and each implementation
    drops in later without the dispatcher changing.
    """


class InvalidSpecificationError(DomainError):
    """A symbol specification is internally inconsistent or unusable.

    For example a zero tick size, a tick value with no corresponding tick size, a
    volume step of zero, or a negative stops level. Position sizing cannot be done
    safely from such a specification, so the only correct behaviour is to refuse.
    """


class RiskError(StopOrderScalpError):
    """The risk engine refused to produce a volume or a monetary risk figure."""


class OrderValidationError(StopOrderScalpError):
    """A prepared order failed validation and was not sent.

    Carries the specific reason. An order that fails validation has not touched the
    broker, so this is a :class:`TerminalError` for that cycle.
    """

    def __init__(self, reason: str, code: str = "ORDER_VALIDATION_FAILED") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class InstrumentNotAllowedError(DomainError):
    """A symbol was requested that the instrument policy does not permit.

    The strategy trades US30 only. This error exists so that a configuration typo
    fails loudly instead of quietly trading something else.
    """


class LookAheadViolationError(DomainError):
    """A candle window contained information not available at decision time.

    This is a programming error, not a market condition. It exists so the no-look-ahead
    guarantee can be asserted mechanically rather than by review.
    """


class IllegalTransitionError(StopOrderScalpError):
    """A state machine edge that is not in the transition table was attempted."""

    def __init__(self, source: object, target: object) -> None:
        super().__init__(f"illegal transition {source} -> {target}")
        self.source = source
        self.target = target


class StaleStateError(StopOrderScalpError):
    """Local state disagrees with authoritative broker state.

    Raised by the reconciler when local and remote cannot be reconciled without an
    operator decision. The broker wins on positions and orders; this error records the
    contradiction rather than hiding it.
    """


class PersistenceError(StopOrderScalpError):
    """The state ledger could not be read, parsed, or written.

    Never downgraded to "start fresh". Losing the idempotency ledger is exactly the
    condition under which duplicate orders appear.
    """


# --- terminal / broker ----------------------------------------------------


class TerminalError(StopOrderScalpError):
    """A failure that must not be retried within the same decision cycle."""


class RetryableError(TerminalError):
    """A failure that is safe to retry with bounded backoff.

    Only idempotent reads belong here. A send whose outcome is unknown must raise
    :class:`ExecutionUnknownError` instead, because "retryable" would be a lie.
    """


class BrokerError(TerminalError):
    """The broker rejected a request or returned an inconsistent state.

    ``code`` and ``retcode`` carry the broker's own identifiers so an operator can
    look the failure up in the terminal's error log.
    """

    def __init__(self, message: str, *, code: str | None = None, retcode: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retcode = retcode


class BrokerNotConnectedError(RetryableError):
    """The MetaTrader 5 terminal is not reachable or has terminated."""


class BrokerRejectedError(BrokerError):
    """The trade server refused the request. Retrying the same request is pointless."""


class ExecutionUnknownError(BrokerError):
    """The outcome of a send is unknown and must be re-observed, never resent.

    Raised when ``order_send`` returns nothing, or returns a response that cannot be
    interpreted, or when the terminal disappeared mid-call. The order may or may not
    exist. The only safe action is to re-read broker state filtered by this strategy's
    magic number.
    """


class SymbolNotFoundError(RetryableError):
    """The broker does not expose the requested symbol."""


class PositionNotFoundError(BrokerError):
    """A referenced position no longer exists on the broker."""


class MarginInsufficientError(BrokerError):
    """The account cannot cover the order's margin requirement."""


class MarketDataError(StopOrderScalpError):
    """Market data could not be produced or was structurally unusable."""


class IncompatibleBrokerError(BrokerError):
    """The broker's symbol specification cannot support the strategy's arithmetic."""
