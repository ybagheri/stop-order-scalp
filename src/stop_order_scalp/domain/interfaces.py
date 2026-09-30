"""Protocols at every outward boundary.

Dependency inversion lives here. Anything the application needs from the outside world
is declared as a :class:`typing.Protocol`, which means:

* no adapter has to import a base class, so a third-party integration cannot be forced
  into our inheritance hierarchy;
* tests substitute hand-written fakes that satisfy the shape, which is why the unit test
  suite never imports MetaTrader 5 at all;
* the direction of dependency is legible from this file alone.

The protocols are split by role so that a consumer depends on the narrowest one it
needs. A position manager that only reads ticks should not need a protocol that can
place orders.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from stop_order_scalp.domain.models import (
    AccountSnapshot,
    Candle,
    OrderIntent,
    OrderRecord,
    PositionRecord,
    Tick,
)
from stop_order_scalp.domain.value_objects import SymbolSpecification

__all__ = [
    "AccountReader",
    "AuditSink",
    "Broker",
    "Clock",
    "InstrumentResolver",
    "MarketDataProvider",
    "OrderBookReader",
    "OrderExecutor",
    "PositionManagerBroker",
]


@runtime_checkable
class Clock(Protocol):
    """Time as an injectable dependency.

    Every timestamp in the system comes from a ``Clock``. That is the only reason the
    test suite can assert on break-even triggers and candle boundaries deterministically,
    and the only reason a naive local time cannot leak into a decision.
    """

    def now(self) -> datetime:
        """Current time, always timezone-aware."""


@runtime_checkable
class MarketDataProvider(Protocol):
    """Candles and ticks, without a broker.

    Deliberately separate from :class:`Broker`: the strategy needs price history, not
    the ability to trade. This separation is what allows the backtester and the live
    system to run the same strategy code.
    """

    def specification(self, symbol: str) -> SymbolSpecification:
        """The instrument's contract details. Raises if the symbol is unknown."""

    def candles(
        self,
        symbol: str,
        timeframe: str,
        count: int,
        *,
        before: datetime | None = None,
    ) -> Sequence[Candle]:
        """Closed candles for ``timeframe``, oldest first.

        Implementations must return **only fully closed** candles unless the caller
        explicitly asks otherwise. Returning a forming bar from a method named
        ``candles`` is the single most dangerous way to introduce look-ahead bias, so
        forming bars are reachable only via :meth:`forming_candle`.
        """

    def forming_candle(self, symbol: str, timeframe: str) -> Candle | None:
        """The in-progress candle, if any. Never used by the baseline strategy."""

    def tick(self, symbol: str) -> Tick | None:
        """The latest bid/ask observation, if available."""


@runtime_checkable
class InstrumentResolver(Protocol):
    """Maps the logical instrument to the broker's symbol name."""

    def resolve(self, logical_symbol: str) -> str:
        """Return the broker symbol name, or raise if it cannot be determined."""


@runtime_checkable
class AccountReader(Protocol):
    """Account state and symbol availability."""

    def account(self) -> AccountSnapshot:
        """Current account. Raises :class:`BrokerNotConnectedError` if unreachable."""

    def symbol_available(self, symbol: str) -> bool:
        """Whether the symbol is selected and trading is permitted."""

    def server_time(self) -> datetime:
        """Broker server time as a timezone-aware datetime.

        Broker server time is not UTC and is not the local clock. Only this value may be
        compared against candle boundaries.
        """


@runtime_checkable
class OrderBookReader(Protocol):
    """Read-only view of positions and orders belonging to this strategy."""

    def positions(self, *, magic_number: int | None = None, symbol: str | None = None) -> Sequence[PositionRecord]:
        """Open positions, newest last."""

    def orders(self, *, magic_number: int | None = None, symbol: str | None = None) -> Sequence[OrderRecord]:
        """Working orders, including pending stops."""

    def position_by_ticket(self, ticket: int) -> PositionRecord | None:
        """A single position, or ``None``. Cheaper than filtering a full list."""


@runtime_checkable
class OrderExecutor(Protocol):
    """Writes to the broker."""

    def place_order(self, intent: OrderIntent) -> OrderRecord:
        """Place ``intent`` and return the broker's record.

        Implementations must not retry internally after an indeterminate outcome. That
        case raises
        :class:`~stop_order_scalp.domain.exceptions.ExecutionUnknownError`, and the
        caller re-observes broker state instead of resending.
        """

    def cancel_order(self, ticket: int) -> bool:
        """Delete a working order. ``True`` if it is gone afterwards."""

    def modify_position(
        self,
        ticket: int,
        *,
        stop_loss: Any = None,
        take_profit: Any = None,
    ) -> bool:
        """Change a position's protective levels. Idempotent: a no-op change is success."""


@runtime_checkable
class PositionManagerBroker(Protocol):
    """The narrow view the position manager needs."""

    def modify_position(
        self,
        ticket: int,
        *,
        stop_loss: Any = None,
        take_profit: Any = None,
    ) -> bool: ...


@runtime_checkable
class Broker(
    Protocol,
    AccountReader,
    OrderBookReader,
    OrderExecutor,
):
    """Everything the execution layer is allowed to do to a venue.

    Two implementations exist and both are first class:

    * :class:`stop_order_scalp.execution.mt5_broker.MT5Broker` -- the native MetaTrader 5
      Python API. Never imported by anything above this layer.
    * :class:`stop_order_scalp.execution.simulated_broker.SimulatedBroker` -- an in-memory
      venue with real bid/ask, fills, stops and commission. Used by ``DRY_RUN``, by
      ``PAPER``, by the backtester and by every unit test.

    Neither is a test double dressed up as an implementation. The simulator is the reason
    the whole pipeline can be exercised without a terminal.
    """

    def connect(self) -> None:
        """Establish a session. Idempotent."""

    def shutdown(self) -> None:
        """Tear the session down. Idempotent, and safe to call when never connected."""

    @property
    def is_connected(self) -> bool:
        """Whether the session is usable right now."""

    def stream_ticks(self, symbol: str) -> Iterator[Tick]:
        """Yield ticks. Used by ``PAPER`` mode, which follows live prices but writes
        nothing."""

    def leverage_for(self, symbol: str) -> Decimal:
        """Account leverage applicable to ``symbol``."""


@runtime_checkable
class AuditSink(Protocol):
    """Where structured events go.

    A journal, a file, or a list in a test. The interface carries no secret field, so a
    caller cannot leak a credential by forgetting to redact one.
    """

    def emit(self, event: dict[str, Any]) -> None:
        """Record one structured event."""

    def flush(self) -> None:
        """Force buffered events out. Called before the process exits."""