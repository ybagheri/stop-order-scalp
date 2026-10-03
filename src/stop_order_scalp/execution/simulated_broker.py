"""An in-memory venue with real bid/ask, fills, stops and commission.

**This is not a test double.** It is what ``DRY_RUN`` and ``PAPER`` run against, and it is
what the Phase 9 backtester replays through. That is the whole reason it exists rather than
a mock in the test suite: dry-run has to exercise the same code path live trading does, or
it proves nothing.

What it models, because each of these is something the real venue does:

* **bid/ask.** A buy fills at the ask, a sell at the bid. Collapsing them to a mid price is
  how a backtest stops matching live fills.
* **pending stops.** A BUY STOP rests on the book and only fills when price crosses it.
* **stop loss and take profit** attached to a position, checked on every price move.
* **commission** charged on fill, so sizing and P&L both see the cost.
* **magic numbers**, so several strategies can share one account without colliding.

What it deliberately does **not** model, because inventing it would be worse than its
absence: slippage beyond the spread, partial fills, requotes, latency, or margin
constraints. Phase 9 adds what it needs for statistics, and each addition is a documented
assumption rather than a silent fiction.

Every method takes an injected clock. Nothing here reads a wall clock, so a replay is
reproducible.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from stop_order_scalp.domain.enums import CommissionMode, Side
from stop_order_scalp.domain.exceptions import (
    BrokerError,
    BrokerNotConnectedError,
    BrokerRejectedError,
    ExecutionUnknownError,
    OrderValidationError,
    PositionNotFoundError,
    SymbolNotFoundError,
)
from stop_order_scalp.domain.interfaces import Clock
from stop_order_scalp.domain.models import (
    AccountSnapshot,
    EnvironmentSettings,
    OrderIntent,
    OrderRecord,
    PositionRecord,
    Tick,
)
from stop_order_scalp.domain.value_objects import Money, Price, SymbolSpecification, Volume
from stop_order_scalp.infrastructure.clock import FixedClock

__all__ = ["ClosedTrade", "SimulatedBroker", "SimulatedTick"]

#: MetaTrader 5 symbol suffixes used to distinguish bid and ask.
_BID = "bid"
_ASK = "ask"


@dataclass(frozen=True, slots=True)
class SimulatedTick:
    """One price observation for the simulated venue."""

    bid: Price
    ask: Price

    def price_for(self, side: Side) -> Price:
        return self.ask if side is Side.SIDE_BUY else self.bid


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """One position the venue closed, as the venue recorded it.

    ``reason`` is the venue's classification at the moment of closure, not a guess made
    afterwards by comparing levels. A backtest that reports "stopped out" for a trade that
    actually reached its target is worse than one that reports nothing, because it looks like
    evidence.
    """

    ticket: int
    symbol: str
    side: Side
    volume: Volume
    entry: Price
    exit_price: Price
    opened_at: datetime
    closed_at: datetime
    gross: Money
    commission: Money
    net: Money
    magic_number: int
    client_tag: str
    comment: str
    reason: str

    @property
    def is_win(self) -> bool:
        return self.net.amount > 0

    @property
    def duration(self) -> timedelta:
        return self.closed_at - self.opened_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "symbol": self.symbol,
            "side": str(self.side),
            "volume": str(self.volume.lots),
            "entry": str(self.entry),
            "exit": str(self.exit_price),
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat(),
            "duration_seconds": int(self.duration.total_seconds()),
            "gross": str(self.gross.amount),
            "commission": str(self.commission.amount),
            "net": str(self.net.amount),
            "reason": self.reason,
        }


def _exit_reason(position: _Position, exit_price: Price) -> str:
    """Why the venue closed this position, decided from the levels it held.

    Decided here, at closure, because afterwards the stop has usually been trailed forward and
    the original level is gone. Three outcomes, and no fourth: if neither protective level is
    within reach the closure was something else, and it is labelled as such rather than being
    forced into "stop" or "target".
    """
    stop = position.stop_loss
    target = position.take_profit
    if stop is None and target is None:
        return "closed"
    if stop is not None:
        if position.side is Side.SIDE_BUY and exit_price.value <= stop.value:
            return "stop_loss"
        if position.side is Side.SIDE_SELL and exit_price.value >= stop.value:
            return "stop_loss"
    if target is not None:
        if position.side is Side.SIDE_BUY and exit_price.value >= target.value:
            return "take_profit"
        if position.side is Side.SIDE_SELL and exit_price.value <= target.value:
            return "take_profit"
    return "closed"


@dataclass
class _Position:
    """An open position at the venue. Mutable, because a stop is trailed by assignment.

    Not frozen, unlike :class:`ClosedTrade`: :meth:`SimulatedBroker.modify_position` rewrites
    ``stop_loss`` in place on every trailing step, and freezing this would mean rebuilding the
    position -- and, worse, would tempt an implementation into recording the trailed level as
    the one the position was opened with. The immutable record of what happened is
    :class:`ClosedTrade`, written once at closure.
    """

    ticket: int
    symbol: str
    side: Side
    volume: Volume
    entry: Price
    stop_loss: Price | None
    take_profit: Price | None
    opened_at: datetime
    client_tag: str
    comment: str
    magic_number: int
    source_order_ticket: int | None = None

    def to_record(self, tick: SimulatedTick | None) -> PositionRecord:
        return PositionRecord(
            ticket=self.ticket,
            symbol=self.symbol,
            side=self.side,
            volume=self.volume,
            entry=self.entry,
            stop_loss=self.stop_loss,
            take_profit=self.take_profit,
            magic_number=self.magic_number,
            comment=self.comment,
            opened_at=self.opened_at,
            profit=self.profit(tick),
            source_order_ticket=self.source_order_ticket,
            client_tag=self.client_tag,
        )

    def profit(self, tick: SimulatedTick | None) -> Money:
        """Floating profit at ``tick``.

        Gross, not net: commission is charged when the position is *closed*, so adding it
        here would deduct it twice. A broker's floating P/L is gross too, which is what
        keeps this comparable with the live account screen.

        Converted with the **same** tick arithmetic as :meth:`SimulatedBroker.close`, because a
        position whose floating P/L is computed one way and whose realised P/L is computed
        another will show a jump at the moment of closure, and a jump that big is always a bug
        wearing a plausible number. This used to be ``delta * contract_size``, which ignores
        ``tick_value`` and ``tick_size`` entirely -- correct on Alpari US30 only because
        ``tick_size == point == 0.1`` makes the two conversions coincide, and wrong on any
        instrument where they do not.
        """
        if tick is None:
            return Money.of(0, _CURRENCY)
        spec = _SPECS.get(self.symbol)
        exit_price = tick.price_for(self.side.opposite)
        delta = exit_price.value - self.entry.value
        if self.side is Side.SIDE_SELL:
            delta = -delta
        return Money.of(_value_of_move(spec, delta) * self.volume.lots, _CURRENCY)

    def stop_hit(self, tick: SimulatedTick) -> bool:
        """Whether it is the *stop loss* (not the target) that has been reached."""
        if self.stop_loss is None:
            return False
        worst = tick.price_for(self.side.opposite)
        if self.side is Side.SIDE_BUY:
            return worst <= self.stop_loss
        return worst >= self.stop_loss

    def protective_hit(self, tick: SimulatedTick) -> bool:
        """Whether a protective level would have triggered at this price."""
        if self.stop_loss is None and self.take_profit is None:
            return False
        # Stops trigger on the adverse side, targets on the favourable one.
        worst = tick.price_for(self.side.opposite)
        # A long closes by selling at the bid and a short by buying at the ask, so the target
        # is judged against that same price. It used to be judged against the *entry* side's
        # price, which let a target trigger one spread early.
        best = worst
        if self.stop_loss is not None:
            if self.side is Side.SIDE_BUY and worst <= self.stop_loss:
                return True
            if self.side is Side.SIDE_SELL and worst >= self.stop_loss:
                return True
        if self.take_profit is not None:
            if self.side is Side.SIDE_BUY and best >= self.take_profit:
                return True
            if self.side is Side.SIDE_SELL and best <= self.take_profit:
                return True
        return False


#: Venue-level configuration. Module-level because the simulated venue is a singleton in
#: spirit: the same instrument specification applies to every instance, and threading it
#: through every constructor would be noise. Tests that need a different one call
#: :func:`configure_specification`.
_SPECS: dict[str, SymbolSpecification] = {}
_CURRENCY: str = "USD"
_TICK_VALUE: Decimal = Decimal("1.0")
_COMMISSION: tuple[Decimal, CommissionMode] = (Decimal("0"), CommissionMode.PER_LOT_ROUND_TRIP)


def configure_specification(
    specification: SymbolSpecification,
    *,
    tick_value: Decimal | None = None,
    commission: Decimal | None = None,
    commission_mode: CommissionMode = CommissionMode.PER_LOT_ROUND_TRIP,
    currency: str = "USD",
) -> None:
    """Set the venue's economics. Called by the application composition root."""
    global _CURRENCY, _TICK_VALUE, _COMMISSION
    _SPECS[specification.name] = specification
    _TICK_VALUE = tick_value if tick_value is not None else specification.tick_value
    _COMMISSION = (commission or Decimal("0"), commission_mode)
    _CURRENCY = currency


def reset_venue() -> None:
    """Forget every instrument and the economics. For test isolation."""
    _SPECS.clear()
    global _CURRENCY, _TICK_VALUE, _COMMISSION
    _CURRENCY = "USD"
    _TICK_VALUE = Decimal("1.0")
    _COMMISSION = (Decimal("0"), CommissionMode.PER_LOT_ROUND_TRIP)


class SimulatedBroker:
    """A venue that behaves like a broker without being one.

    Implements the read/write halves of
    :class:`~stop_order_scalp.domain.interfaces.Broker`, and refuses anything it does not
    model rather than approximating it.
    """

    def __init__(
        self,
        settings: EnvironmentSettings,
        *,
        clock: Clock | None = None,
        balance: Decimal = Decimal("10000"),
        leverage: int = 100,
        slippage: Decimal = Decimal(0),
        continuous_path: bool = False,
    ) -> None:
        if slippage < 0:
            raise BrokerError(
                f"slippage must be a non-negative price distance, got {slippage}; a negative "
                "value would make every fill better than the level"
            )
        #: Adverse slippage as a **price distance**, applied to stop-order entry fills and to
        #: stop-loss exits. Never to take-profit exits, which fill at the level or better.
        self._slippage = slippage
        #: ``True`` when published quotes are *samples of a continuous path* (an OHLC bar's
        #: extremes), not the only prices that existed. A protective level crossed between two
        #: samples then fills **at the level**, as a real stop does, rather than at the next
        #: sample -- which can be a whole bar's range away and manufactures a loss on every
        #: stop-out. ``False`` keeps the legacy behaviour (fill at the published quote).
        self._continuous_path = continuous_path
        self._settings = settings
        self._clock = clock or FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
        self._balance = balance
        self._equity = balance
        self._leverage = leverage
        self._orders: dict[int, OrderRecord] = {}
        self._positions: dict[int, _Position] = {}
        self._ticks: dict[str, SimulatedTick] = {}
        self._next_ticket = 1
        self._connected = False
        #: One-shot failure armed by :meth:`fail_next_send`.
        self._pending_failure: str | None = None
        #: Number of ``place_order`` calls that returned an unknown outcome. A test asserts
        #: this is never followed by a resend.
        self.unknown_outcomes = 0
        #: Every intent this venue was ever asked to accept, successful or not.
        self.send_attempts: list[str] = []
        #: Every position this venue has closed, oldest first. See :meth:`history`.
        self._history: list[ClosedTrade] = []

    # --- connection ------------------------------------------------------

    def connect(self) -> None:
        self._connected = True

    def shutdown(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _require(self) -> None:
        if not self._connected:
            raise BrokerNotConnectedError(
                "SimulatedBroker is not connected; call connect() first"
            )

    # --- clock seam ------------------------------------------------------

    def set_time(self, moment: datetime) -> None:
        """Jump the venue's clock. The only way time moves, so replay is reproducible."""
        setter = getattr(self._clock, "set", None)
        if setter is None:  # pragma: no cover - defensive; the default is a FixedClock
            raise BrokerError("this venue's clock is not settable")
        setter(moment)

    @property
    def now(self) -> datetime:
        return self._clock.now()

    def advance(self, seconds: float) -> None:
        advancer = getattr(self._clock, "advance", None)
        if advancer is not None:
            advancer(seconds)

    # --- prices ----------------------------------------------------------

    def publish(self, symbol: str, bid: Decimal, ask: Decimal, *, digits: int = 1) -> None:
        """Set the venue's current prices, which is what pending orders trigger against."""
        self._require()
        if ask < bid:
            raise BrokerError(f"ask {ask} is below bid {bid}")
        self._ticks[symbol] = SimulatedTick(
            bid=Price(bid, digits), ask=Price(ask, digits)
        )
        self._settle(symbol)

    def tick(self, symbol: str) -> Tick | None:
        self._require()
        quote = self._ticks.get(symbol)
        if quote is None:
            return None
        return Tick(moment=self.now, bid=quote.bid, ask=quote.ask)

    def stream_ticks(self, symbol: str) -> Iterator[Tick]:
        """Yield the last published quote for ``symbol``, then wait for the next change.

        Bounded by the caller: this is a generator over explicit :meth:`publish` calls, not
        a background thread. A PAPER run drives it from the same price feed the live run
        would read.
        """
        self._require()
        seen = 0
        while self._connected:
            quote = self._ticks.get(symbol)
            if quote is None:
                return
            seen += 1
            yield Tick(moment=self.now, bid=quote.bid, ask=quote.ask)
            if seen > 1000:  # pragma: no cover - runaway guard
                raise BrokerError(f"simulated tick stream for {symbol} did not terminate")

    # --- reads -----------------------------------------------------------

    def account(self) -> AccountSnapshot:
        self._require()
        self._mark_to_market()
        return AccountSnapshot(
            login=1,
            server="Simulated",
            currency=_CURRENCY,
            balance=Money.of(self._balance, _CURRENCY),
            equity=Money.of(self._equity, _CURRENCY),
            margin_used=Money.of(0, _CURRENCY),
            margin_free=Money.of(self._balance, _CURRENCY),
            leverage=self._leverage,
            is_demo=True,
            trade_allowed=True,
        )

    def server_time(self) -> datetime:
        self._require()
        return self.now

    def symbol_available(self, symbol: str) -> bool:
        return symbol in _SPECS

    def specification(self, symbol: str) -> SymbolSpecification:
        spec = _SPECS.get(symbol)
        if spec is None:
            raise SymbolNotFoundError(f"the simulated venue does not model {symbol!r}")
        return spec

    def orders(
        self, *, magic_number: int | None = None, symbol: str | None = None
    ) -> list[OrderRecord]:
        """Working orders, oldest first."""
        self._require()
        found = [
            record
            for record in self._orders.values()
            if record.is_active
            and (magic_number is None or record.magic_number == magic_number)
            and (symbol is None or record.symbol == symbol)
        ]
        return sorted(found, key=lambda record: record.ticket)

    def positions(
        self, *, magic_number: int | None = None, symbol: str | None = None
    ) -> list[PositionRecord]:
        self._require()
        found = [
            position.to_record(self._ticks.get(position.symbol))
            for position in self._positions.values()
            if (magic_number is None or position.magic_number == magic_number)
            and (symbol is None or position.symbol == symbol)
        ]
        return sorted(found, key=lambda record: record.ticket)

    def position_by_ticket(self, ticket: int) -> PositionRecord | None:
        self._require()
        position = self._positions.get(ticket)
        if position is None:
            return None
        return position.to_record(self._ticks.get(position.symbol))

    def leverage_for(self, symbol: str) -> Decimal:
        del symbol
        return Decimal(self._leverage)

    # --- writes ----------------------------------------------------------

    def place_order(self, intent: OrderIntent) -> OrderRecord:
        """Validate and rest a pending stop order.

        Validates what the real venue validates, so a dry run catches a rejected order
        before it is sent rather than after.

        An armed :meth:`fail_next_send` with ``outcome='unknown'`` performs the placement and
        *then* raises :class:`ExecutionUnknownError`. That models the dangerous case
        honestly: the venue has the order, the caller never heard back. The caller must
        therefore re-read the book rather than resend -- which it can do, because the order
        really is there.
        """
        self._require()
        self._validate_intent(intent)
        self.send_attempts.append(intent.client_tag)
        ticket = self._take_ticket()
        record = OrderRecord(
            ticket=ticket,
            client_tag=intent.client_tag,
            symbol=intent.symbol,
            kind=intent.kind,
            volume=intent.volume,
            entry=intent.entry,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            magic_number=intent.magic_number,
            comment=intent.comment,
            placed_at=self.now,
            state="PLACED",
            expires_at=intent.expiration,
        )
        self._orders[ticket] = record
        # A pending stop may already be through the level the moment it rests.
        self._settle(intent.symbol)
        current = self._orders.get(ticket)
        settled = current if current is not None else record

        outcome = self._pending_failure
        if outcome is not None:
            self._pending_failure = None
            if outcome == "unknown":
                self.unknown_outcomes += 1
                raise ExecutionUnknownError(
                    f"simulated unknown outcome placing {intent.kind} on {intent.symbol}: "
                    f"the venue accepted order {ticket} but the caller was not told. The "
                    "order IS on the book -- re-read it, do not resend."
                )
            raise BrokerError(f"simulated failure placing {intent.symbol}: {outcome}")

        return settled

    def cancel_order(self, ticket: int) -> bool:
        """Remove a working order. ``True`` if it is gone afterwards, including if absent.

        ``OrderRecord`` is frozen, so the state change replaces the stored record rather
        than mutating it -- the same immutability the domain guarantees everywhere else.
        """
        self._require()
        record = self._orders.get(ticket)
        if record is None or not record.is_active:
            return True
        self._orders[ticket] = replace(record, state="CANCELLED")
        return True

    def modify_position(
        self,
        ticket: int,
        *,
        stop_loss: Any = None,
        take_profit: Any = None,
    ) -> bool:
        """Change protective levels. Idempotent: a no-op change is success."""
        self._require()
        position = self._positions.get(ticket)
        if position is None:
            raise PositionNotFoundError(f"no simulated position with ticket {ticket}")
        if stop_loss is not None:
            position.stop_loss = _as_price(stop_loss)
        if take_profit is not None:
            position.take_profit = _as_price(take_profit)
        return True

    def close(self, ticket: int, *, at: Decimal | None = None) -> Money:
        """Close a position at the venue's price and return the realised P&L.

        The closure is appended to :meth:`history` before the balance moves. A venue that
        forgets a closed trade cannot answer "what happened", and a backtest that has to
        reconstruct that by diffing the open-position list between ticks gets it subtly wrong
        the moment two positions overlap -- which is exactly the case a trailing stop creates.
        """
        self._require()
        position = self._positions.pop(ticket, None)
        if position is None:
            raise PositionNotFoundError(f"no simulated position with ticket {ticket}")
        quote = self._ticks.get(position.symbol)
        exit_price = (
            Price(at, position.entry.digits) if at is not None
            else quote.price_for(position.side.opposite) if quote
            else position.entry
        )
        spec = _SPECS.get(position.symbol)
        delta = exit_price.value - position.entry.value
        if position.side is Side.SIDE_SELL:
            delta = -delta
        gross = _value_of_move(spec, delta) * position.volume.lots
        fee = _commission(position.volume)
        net = Money(gross - fee, _CURRENCY)
        self._balance += net.amount
        self._mark_to_market()
        self._history.append(
            ClosedTrade(
                ticket=ticket,
                symbol=position.symbol,
                side=position.side,
                volume=position.volume,
                entry=position.entry,
                exit_price=exit_price,
                opened_at=position.opened_at,
                closed_at=self.now,
                gross=Money(gross, _CURRENCY),
                commission=Money(fee, _CURRENCY),
                net=net,
                magic_number=position.magic_number,
                client_tag=position.client_tag,
                comment=position.comment,
                reason=_exit_reason(position, exit_price),
            )
        )
        return net

    def history(
        self, *, magic_number: int | None = None, symbol: str | None = None
    ) -> tuple[ClosedTrade, ...]:
        """Every position this venue has closed, oldest first.

        The venue's own record, which is the only one worth reading. Reconstructing closed
        trades by comparing the open-position list across ticks is the alternative, and it
        fails quietly: it cannot tell a stop-out from a take-profit, it cannot recover the
        exit price, and it attributes a closure to whichever tick happened to notice it.
        """
        return tuple(
            trade
            for trade in self._history
            if (magic_number is None or trade.magic_number == magic_number)
            and (symbol is None or trade.symbol == symbol)
        )

    # --- internals -------------------------------------------------------

    def _take_ticket(self) -> int:
        ticket = self._next_ticket
        self._next_ticket += 1
        return ticket

    def _validate_intent(self, intent: OrderIntent) -> None:
        spec = _SPECS.get(intent.symbol)
        if spec is None:
            raise SymbolNotFoundError(
                f"the simulated venue does not model {intent.symbol!r}; call "
                "configure_specification() before trading it"
            )
        if intent.volume.lots < spec.volume_min or intent.volume.lots > spec.volume_max:
            raise BrokerRejectedError(
                f"{intent.volume.lots} lots is outside the venue's "
                f"[{spec.volume_min}, {spec.volume_max}]"
            )
        if intent.kind.side is Side.SIDE_BUY and intent.stop_loss >= intent.entry:
            raise OrderValidationError(
                f"a BUY stop at {intent.stop_loss} does not protect an entry at "
                f"{intent.entry}"
            )
        if intent.kind.side is Side.SIDE_SELL and intent.stop_loss <= intent.entry:
            raise OrderValidationError(
                f"a SELL stop at {intent.stop_loss} does not protect an entry at "
                f"{intent.entry}"
            )

    def _settle(self, symbol: str) -> None:
        """Advance the venue one step: fill what triggered, close what was protected."""
        quote = self._ticks.get(symbol)
        if quote is None:
            return
        self._fill_pending(symbol, quote)
        self._check_protective(symbol, quote)
        self._expire(symbol)
        self._mark_to_market()

    def _fill_pending(self, symbol: str, quote: SimulatedTick) -> None:
        for ticket in list(self._orders):
            record = self._orders[ticket]
            if record.symbol != symbol or not record.is_active:
                continue
            side = record.kind.side
            trigger_price = quote.price_for(side)
            if side is Side.SIDE_BUY and trigger_price < record.entry:
                continue
            if side is Side.SIDE_SELL and trigger_price > record.entry:
                continue
            fill = (
                record.entry
                if not self._slippage
                else Price(
                    record.entry.value + (self._slippage if side is Side.SIDE_BUY else -self._slippage),
                    record.entry.digits,
                )
            )
            position = _Position(
                ticket=self._take_ticket(),
                symbol=symbol,
                side=side,
                volume=record.volume,
                entry=fill,
                stop_loss=record.stop_loss,
                take_profit=record.take_profit,
                opened_at=self.now,
                client_tag=record.client_tag,
                comment=record.comment,
                magic_number=record.magic_number,
                source_order_ticket=ticket,
            )
            self._positions[position.ticket] = position
            self._orders[ticket] = replace(
                self._orders[ticket], state="FILLED", position_id=position.ticket
            )

    def _check_protective(self, symbol: str, quote: SimulatedTick) -> None:
        for ticket in list(self._positions):
            position = self._positions[ticket]
            if position.symbol != symbol:
                continue
            if position.protective_hit(quote):
                exit_at: Decimal | None = None
                stopped = position.stop_hit(quote)
                sign = Decimal(-1) if position.side is Side.SIDE_BUY else Decimal(1)
                if self._continuous_path:
                    level = position.stop_loss if stopped else position.take_profit
                    if level is not None:
                        exit_at = level.value + (sign * self._slippage if stopped else Decimal(0))
                elif self._slippage and stopped:
                    base = quote.price_for(position.side.opposite).value
                    exit_at = base + sign * self._slippage
                self.close(ticket, at=exit_at)

    def _expire(self, symbol: str) -> None:
        moment = self.now
        for ticket in list(self._orders):
            record = self._orders[ticket]
            if record.symbol != symbol or not record.is_active:
                continue
            if record.expires_at is not None and record.expires_at <= moment:
                self._orders[ticket] = replace(record, state="EXPIRED")

    def _mark_to_market(self) -> None:
        self._equity = self._balance + sum(
            (position.profit(self._ticks.get(position.symbol)).amount
             for position in self._positions.values()),
            Decimal(0),
        )

    def fail_next_send(self, *, outcome: str = "unknown") -> None:
        """Arm a one-shot failure on the next :meth:`place_order`.

        ``outcome='unknown'`` is the important one: the order lands on the book and then
        :class:`ExecutionUnknownError` is raised, which is precisely the case where a
        resend would open a second position. Phase 5's tests use it to prove the manager
        re-reads instead.
        """
        self._pending_failure = outcome

    def as_dict(self) -> dict[str, Any]:
        return {
            "orders": len(self.orders(magic_number=None)),
            "positions": len(self.positions(magic_number=None)),
            "balance": str(self._balance),
            "equity": str(self._equity),
            "unknown_outcomes": self.unknown_outcomes,
        }

    def __repr__(self) -> str:
        return (
            f"SimulatedBroker({self._settings.environment}, connected={self._connected}, "
            f"orders={len(self._orders)}, positions={len(self._positions)})"
        )


def _value_of_move(spec: SymbolSpecification | None, delta: Decimal) -> Decimal:
    """What a price move of ``delta`` is worth, per lot.

    The one place this venue converts price to money, used by **both** the floating profit and
    the realised P/L. One conversion, deliberately: when two code paths price a position and
    disagree, the account shows a jump at the moment of closure, and a jump that size is always
    a bug wearing a plausible number.

        value = delta / tick_size  x  tick_value  x  contract_size

    A ``tick_value`` is what one *tick* is worth, and a tick is ``tick_size`` of price, so
    dividing by ``tick_size`` and multiplying by ``tick_value`` converts price to money
    directly. Multiplying by ``delta * contract_size`` instead -- which is what the floating
    profit used to do -- ignores ``tick_value`` completely, and happens to agree on Alpari US30
    only because ``tick_size == tick_value == 0.1`` there.

    Returns a Decimal; the caller applies the volume.
    """
    if spec is None:
        return delta
    ticks = delta / spec.tick_size if spec.tick_size else delta
    return ticks * spec.tick_value * spec.contract_size


def _commission(volume: Volume) -> Decimal:
    rate, mode = _COMMISSION
    return rate * Decimal(mode.sides) * volume.lots


def _as_price(value: Any) -> Price:
    return value if isinstance(value, Price) else Price.parse(str(value), 1)
