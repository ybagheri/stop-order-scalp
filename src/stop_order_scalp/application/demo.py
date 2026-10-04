"""The demo run: real market data, a real broker, and a gate that stays shut until asked.

This is the first composition root that reaches a venue that can fill an order, so it is
written to be hard to misuse rather than convenient:

* **Observe is the default.** ``run --demo`` reads the real market and prints every decision --
  the pending order it *would* place, with its size -- and sends nothing. Placing needs
  ``--place-orders`` **and** ``SOS_ENVIRONMENT=DEMO`` **and** ``SOS_ALLOW_ORDER=true`` **and** a
  terminal that says the account is a demo account (:class:`DemoOrderGate`).
* **A live account cannot be reached from here.** ``DEMO`` is required, ``SOS_ALLOW_LIVE`` must be
  false, the gate refuses ``LIVE`` outright, and the account's own ``trade_mode`` is read back
  from the terminal. A configuration that says DEMO over a signed-in real account is stopped by
  that last check, which no setting can fake.
* **The specification is the terminal's, and must match the one that was measured.** A different
  contract size or tick value mis-scales every position, so a mismatch refuses to start.
* **Limited by default.** One order (``--max-orders``), then the run ends once flat. Resting
  orders are cancelled on exit; positions are left, and the report says so.

What this does *not* do: it has never run against a real terminal. The broker's wire format was
checked against the published MetaTrader5 reference and a test double that exposes only real
functions, which is a different thing from having worked. Observe mode first; one order second.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from stop_order_scalp.application.service import size_decision
from stop_order_scalp.domain.enums import Environment, Side
from stop_order_scalp.domain.exceptions import (
    BrokerError,
    BrokerNotConnectedError,
    MarketDataError,
    RetryableError,
)
from stop_order_scalp.domain.models import Candle, EnvironmentSettings, Tick
from stop_order_scalp.domain.value_objects import SymbolSpecification
from stop_order_scalp.execution.gates import DemoOrderGate, LiveInterlock
from stop_order_scalp.execution.mt5_broker import MetaTrader5Broker
from stop_order_scalp.execution.order_manager import OrderManager
from stop_order_scalp.execution.position_manager import PositionManager
from stop_order_scalp.infrastructure.clock import SystemClock, utc_now
from stop_order_scalp.infrastructure.clock import sleep as _sleep
from stop_order_scalp.infrastructure.config import AppConfig
from stop_order_scalp.infrastructure.persistence import StateLedger
from stop_order_scalp.lifecycle.trade_lifecycle import TradeLifecycle
from stop_order_scalp.market_data.mt5_feed import MT5Feed
from stop_order_scalp.market_data.mt5_module import MT5Module
from stop_order_scalp.market_data.symbols import us30_specification
from stop_order_scalp.market_data.timeframes import period_seconds
from stop_order_scalp.risk.risk_manager import RiskManager
from stop_order_scalp.strategy.strategy import StopOrderStrategy, StrategyContext

__all__ = ["DemoService", "DemoUnavailable", "build_demo_service", "compare_specifications"]

#: A poll that fails this many times in a row ends the run. One failure is a hiccup; thirty
#: seconds of them is a terminal that is gone, and carrying on would be deciding blind.
MAX_CONSECUTIVE_ERRORS = 30

#: Rejections after which the run stops. Each is a definitive "no" from the server for a plan
#: on a *new* candle, so one can be a price that moved; three in a row is a setting that is
#: wrong, and a loop that kept sending would only repeat it every minute.
MAX_REJECTIONS = 3

#: What a poll may raise and the loop survive: the terminal or the network, not a logic error.
_TRANSIENT = (BrokerNotConnectedError, RetryableError, MarketDataError)

#: Events kept in the report. Bounded, because a run can last hours.
_EVENT_LIMIT = 200


class DemoUnavailable(RuntimeError):
    """The demo run cannot start, and the message says what to change.

    A distinct type so the CLI reports it as "not available" with the reason, not as a crash.
    """


class _Feed(Protocol):
    """What the loop reads. :class:`MT5Feed` satisfies it; a test supplies its own."""

    def server_time(self) -> datetime: ...

    def candles(
        self, symbol: str, timeframe: str, count: int, *, before: datetime | None = None
    ) -> Sequence[Candle]: ...


class _ServerClock:
    """The lifecycle's clock: broker server time, so ledger stamps agree with the candles."""

    __slots__ = ("_feed",)

    def __init__(self, feed: _Feed) -> None:
        self._feed = feed

    def now(self) -> datetime:
        return self._feed.server_time()


@dataclass
class DemoService:
    """Runs the decision loop against a live terminal and remembers what happened."""

    config: AppConfig
    settings: EnvironmentSettings
    strategy: StopOrderStrategy
    risk_manager: RiskManager
    lifecycle: TradeLifecycle
    broker: Any
    feed: _Feed
    specification: SymbolSpecification
    symbol: str
    place_orders: bool = False
    max_orders: int = 1
    cancel_on_exit: bool = True
    log: Callable[[str], None] = field(default=lambda _line: None)
    events: list[dict[str, Any]] = field(default_factory=list)
    decisions: int = 0
    orders_placed: int = 0
    rejections: int = 0
    warnings: list[str] = field(default_factory=list)
    _last_boundary: datetime | None = None
    _stale_reported_for: datetime | None = None

    # --- the loop ---------------------------------------------------------

    def run(
        self,
        *,
        max_polls: int | None = None,
        duration_seconds: float | None = None,
        sleep: Callable[[float], None] = _sleep,
        monotonic: Callable[[], float] = SystemClock().monotonic,
    ) -> dict[str, Any]:
        """Poll until a limit is reached, the test completes, or the operator interrupts.

        Returns the report. Never raises for an operator interrupt, and always runs the
        shutdown, so a resting order is not left behind by Ctrl-C.
        """
        interval = float(self.config.execution.poll_interval_seconds)
        started = monotonic()
        polls = 0
        errors = 0
        reason = "max_polls"
        self._announce()
        try:
            while True:
                if max_polls is not None and polls >= max_polls:
                    reason = "max_polls"
                    break
                if duration_seconds is not None and monotonic() - started >= duration_seconds:
                    reason = "duration"
                    break
                polls += 1
                try:
                    self._poll()
                    errors = 0
                except _TRANSIENT as exc:
                    errors += 1
                    self._event("error", f"{type(exc).__name__}: {exc}")
                    if errors >= MAX_CONSECUTIVE_ERRORS:
                        reason = "too_many_errors"
                        break
                if self.rejections >= MAX_REJECTIONS:
                    reason = "rejected_repeatedly"
                    break
                if self._complete():
                    reason = "test_complete"
                    break
                sleep(interval)
        except KeyboardInterrupt:
            reason = "interrupted"
            self._event("interrupted", "operator interrupt")
        finally:
            self._shutdown()
        return self.report(reason, polls)

    def _announce(self) -> None:
        mode = "PLACING ORDERS on the demo account" if self.place_orders else "OBSERVING only"
        self.log(f"demo run: {mode}; symbol {self.symbol}; max orders {self.max_orders}")

    # --- one poll ---------------------------------------------------------

    def _poll(self) -> None:
        now = self.feed.server_time()
        step = period_seconds(self.config.strategy.entry.timeframe)
        boundary = _floor(now, step)
        decided = False
        if boundary != self._last_boundary:
            decided = self._decide(now, boundary, step)
        if self.place_orders and not decided:
            self._manage()

    def _decide(self, now: datetime, boundary: datetime, step: int) -> bool:
        """Decide on the candle that has just closed, once. ``True`` if a decision was made."""
        lookback = self.config.strategy.entry.lookback
        entry_tf = self.config.strategy.entry.timeframe
        direction_tf = self.config.strategy.entry.direction_timeframe
        m1 = self.feed.candles(self.symbol, entry_tf, lookback, before=now)
        m15 = self.feed.candles(self.symbol, direction_tf, lookback, before=now)
        newest_closed = m1[-1].open_time if m1 else None
        if not m1 or not m15 or newest_closed is None or newest_closed + timedelta(seconds=step) != boundary:
            # The candle that should just have closed is not there: the market is closed, or
            # the terminal has not published it yet. Retry next poll, and say so once.
            if self._stale_reported_for != boundary:
                self._stale_reported_for = boundary
                seen = newest_closed.isoformat() if newest_closed else "none"
                self._event(
                    "waiting",
                    f"no newly closed {entry_tf} candle for {boundary.isoformat()} "
                    f"(newest closed: {seen}); is the market open?",
                )
            return False

        self._last_boundary = boundary
        self.decisions += 1
        decision = self.strategy.evaluate(
            StrategyContext(
                symbol=self.symbol,
                m15_candles=m15,
                m1_candles=m1,
                reference=now,
                specification=self.specification,
            )
        )
        plan, why = size_decision(
            self.risk_manager, self.broker.account(), self.specification, decision
        )
        if not self.place_orders:
            self._observe(plan, why)
            return True
        return self._act(decision, plan, why)

    def _observe(self, plan: Any, why: str) -> None:
        if plan is None:
            self._event("no_trade", why)
            return
        self._event("would_place", _describe(plan))

    def _act(self, decision: Any, plan: Any, why: str) -> bool:
        """Offer the decision to the lifecycle, within the order limit."""
        self._bind_market()
        offered: Any = plan if plan is not None else decision
        if plan is not None and self.orders_placed >= self.max_orders:
            self._event(
                "limit", f"order limit ({self.max_orders}) reached; not placing: {_describe(plan)}"
            )
            offered = None
        step = self.lifecycle.tick(offered)
        self.lifecycle.observe_fills()
        for action in step.actions:
            if action == "placed":
                self.orders_placed += 1
                self._event("placed", _describe(plan))
            elif action == "rejected":
                self.rejections += 1
                self._event("rejected", "; ".join(step.notes) or "the broker refused the order")
            elif action in {"gate_refused", "busy", "duplicate", "execution_unknown"}:
                self._event(action, "; ".join(step.notes) or why)
            elif action == "no_trade":
                self._event("no_trade", why)
        return True

    def _manage(self) -> None:
        """Between decisions: follow fills, break-even and trailing."""
        self._bind_market()
        self.lifecycle.tick(None)
        self.lifecycle.observe_fills()

    def _bind_market(self) -> None:
        tick: Tick | None = self.broker.tick(self.symbol)
        if tick is not None:
            self.lifecycle.attach_market(tick, self.specification)

    # --- ending -----------------------------------------------------------

    def _complete(self) -> bool:
        """A limited test is over when its orders are placed and nothing is left on the book."""
        if not self.place_orders or self.orders_placed < self.max_orders:
            return False
        return not self._orders() and not self._positions()

    def _orders(self) -> list[Any]:
        return list(self.broker.orders(magic_number=self.settings.magic_number, symbol=self.symbol))

    def _positions(self) -> list[Any]:
        return list(
            self.broker.positions(magic_number=self.settings.magic_number, symbol=self.symbol)
        )

    def _shutdown(self) -> None:
        """Take resting orders off the book. Positions stay, and the report says so."""
        if not self.place_orders:
            return
        try:
            resting = self._orders()
            positions = self._positions()
        except _TRANSIENT as exc:
            self.warnings.append(f"could not read the book on exit: {exc}")
            return
        if resting and self.cancel_on_exit:
            for order in resting:
                try:
                    self.lifecycle.cancel_pending(order.ticket)
                    self._event("cancelled", f"resting order {order.ticket} removed on exit")
                except (BrokerError, *_TRANSIENT) as exc:
                    self.warnings.append(f"could not cancel order {order.ticket}: {exc}")
        elif resting:
            self.warnings.append(f"{len(resting)} order(s) left resting on the account by request")
        if positions:
            self.warnings.append(
                f"{len(positions)} open position(s) remain with their stop and target, but "
                "nothing is trailing them any more"
            )

    # --- reporting --------------------------------------------------------

    def _event(self, kind: str, detail: str) -> None:
        self.log(f"[{kind}] {detail}")
        self.events.append({"kind": kind, "detail": detail})
        del self.events[:-_EVENT_LIMIT]

    def report(self, stop_reason: str, polls: int) -> dict[str, Any]:
        account = self.broker.account()
        return {
            "mode": "place_orders" if self.place_orders else "observe",
            "environment": str(self.settings.environment),
            "broker": type(self.broker).__name__,
            "account": {
                "server": account.server,
                "is_demo": account.is_demo,
                "currency": account.currency,
                "balance": str(account.balance.amount),
            },
            "symbol": self.symbol,
            "specification_source": "terminal (live), matches the recorded measurement",
            "rule": self.strategy.describe(),
            "stop_reason": stop_reason,
            "polls": polls,
            "decisions": self.decisions,
            "orders_placed": self.orders_placed,
            "rejections": self.rejections,
            "max_orders": self.max_orders,
            "orders_on_book": len(self._orders()),
            "positions": len(self._positions()),
            "state": str(self.lifecycle.state),
            "warnings": self.warnings,
            "events": self.events,
            "note": (
                "Observe mode sends nothing. Place mode sends real orders to the demo account."
                if not self.place_orders
                else "Orders were sent to the demo account. Check them in the terminal."
            ),
        }


def _describe(plan: Any) -> str:
    """One line for a sized plan: side, size, and the three prices an operator checks."""
    side = "BUY STOP" if plan.side is Side.SIDE_BUY else "SELL STOP"
    return (
        f"{side} {plan.volume.lots} lots, entry {plan.entry}, "
        f"stop {plan.stop_loss}, target {plan.take_profit}"
    )


def _floor(moment: datetime, step: int) -> datetime:
    """The start of the ``step``-second candle containing ``moment``."""
    seconds = int(moment.timestamp())
    return datetime.fromtimestamp(seconds - seconds % step, UTC)


def compare_specifications(
    live: SymbolSpecification, recorded: SymbolSpecification
) -> list[str]:
    """Every way the terminal's contract differs from the one that was measured, or ``[]``.

    Exact for the fields that fix a position's size and a stop's distance. ``tick_value`` is
    allowed to differ by 1 % because for an instrument not quoted in the account currency it
    moves with the exchange rate, and a refusal on every tick of EURUSD would make the check
    useless -- but not by more, because a factor of ten is exactly the error that made an
    earlier result twelve times too flattering.
    """
    problems: list[str] = []
    for name in ("digits", "point", "tick_size", "contract_size", "volume_min", "volume_max",
                 "volume_step", "stops_level"):  # fmt: skip
        got, expected = getattr(live, name), getattr(recorded, name)
        if got != expected:
            problems.append(f"{name}: terminal says {got}, recorded {expected}")
    if recorded.tick_value:
        drift = abs(live.tick_value - recorded.tick_value) / recorded.tick_value
        if drift > Decimal("0.01"):
            problems.append(
                f"tick_value: terminal says {live.tick_value}, recorded {recorded.tick_value}"
            )
    return problems


def build_demo_service(
    config: AppConfig,
    *,
    place_orders: bool = False,
    max_orders: int = 1,
    cancel_on_exit: bool = True,
    module: MT5Module | None = None,
    log: Callable[[str], None] | None = None,
) -> DemoService:
    """Wire the demo run, refusing to start unless every precondition holds.

    :raises DemoUnavailable: with the reason and the fix, for every refusal here.
    """
    settings = config.environment
    if settings.environment is not Environment.DEMO:
        raise DemoUnavailable(
            f"SOS_ENVIRONMENT is {settings.environment}; set SOS_ENVIRONMENT=DEMO in .env to "
            "use --demo. DRY_RUN is the simulated venue and has its own command."
        )
    if settings.allow_live:
        raise DemoUnavailable(
            "SOS_ALLOW_LIVE is true. A demo run refuses to start with live trading enabled "
            "anywhere in the configuration; set it to false."
        )
    if place_orders and not settings.allow_order:
        raise DemoUnavailable(
            "--place-orders needs SOS_ALLOW_ORDER=true in .env as well: two independent "
            "switches, so one forgotten flag cannot send an order."
        )
    if max_orders < 1:
        raise DemoUnavailable("--max-orders must be at least 1")

    shared = module if module is not None else MT5Module()
    feed = MT5Feed(shared)
    broker = MetaTrader5Broker(
        shared, settings, filling=config.strategy.order.filling_policy
    )
    try:
        feed.connect(settings)
        broker.connect()
    except BrokerError as exc:
        raise DemoUnavailable(f"cannot attach to the terminal: {exc}") from exc

    if not broker.account_is_confirmed_demo():
        broker.shutdown()
        raise DemoUnavailable(
            "the terminal did not confirm this is a DEMO account (trade_mode). Refusing to go "
            "further: sign the terminal in to a demo account and try again."
        )

    symbol = feed.resolve_symbol(config.strategy.symbol, config.strategy.symbol_aliases)
    live = feed.specification(symbol)
    recorded = us30_specification(symbol)  # raises, naming what is measured, if it is not
    problems = compare_specifications(live, recorded)
    if problems:
        broker.shutdown()
        raise DemoUnavailable(
            f"the terminal's contract for {symbol} differs from the one that was measured: "
            + "; ".join(problems)
            + ". Re-measure and update market_data/symbols.py before trading it."
        )

    risk_manager = RiskManager(config.strategy.risk, config.strategy.target)
    gate = DemoOrderGate(enabled=place_orders, account_confirmed_demo=True)
    order_manager = OrderManager(
        config.execution,
        config.strategy.order,
        risk_manager,
        gate=gate,
        interlock=LiveInterlock.from_settings(settings),
    )
    position_manager = PositionManager(config.strategy.break_even, config.strategy.trailing)
    ledger = StateLedger.load(config.paths.state_file_for(Environment.DEMO))
    lifecycle = TradeLifecycle(
        broker,
        ledger,
        order_manager,
        clock=_ServerClock(feed),
        position_manager=position_manager,
        magic_number=config.execution.magic_number,
        symbol=symbol,
        refresh_pending=config.strategy.entry.refresh_pending,
        settings=settings,
    )
    lifecycle.recover()

    return DemoService(
        config=config,
        settings=settings,
        strategy=StopOrderStrategy(config.strategy, live),
        risk_manager=risk_manager,
        lifecycle=lifecycle,
        broker=broker,
        feed=feed,
        specification=live,
        symbol=symbol,
        place_orders=place_orders,
        max_orders=max_orders,
        cancel_on_exit=cancel_on_exit,
        log=log if log is not None else _stderr,
    )


def _stderr(line: str) -> None:
    stamp = utc_now().strftime("%H:%M:%S")
    sys.stderr.write(f"{stamp} {line}\n")
    sys.stderr.flush()
