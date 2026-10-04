"""The composition root: everything built, and the one place they are wired together.

The CLI has expected ``application.service.build_service`` since Phase 1, while the layers
behind it were built one phase at a time. Until this module existed, ``run`` exited 4 and
printed that its phase had not landed — accurate, and the whole problem. Seven phases of
well-tested modules with nothing connecting them is a statement about the modules.

Everything below is wiring. **No rules live here.** A decision about *what to trade* belongs
to :mod:`stop_order_scalp.strategy`; a decision about *how big* to
:mod:`stop_order_scalp.risk`; a decision about *when to send* to
:mod:`stop_order_scalp.lifecycle`. This module only decides which object holds which
collaborator, because that is the one question nobody else can answer.

Dry run needs no broker
-----------------------

``--dry-run`` wires :class:`~stop_order_scalp.execution.simulated_broker.SimulatedBroker` and
:class:`~stop_order_scalp.market_data.candles.freeze_closed_bars`, so the whole pipeline —
strategy, risk, gates, idempotency, trailing, recovery — runs on a machine with no MetaTrader 5
installed. That is what makes it a real check rather than a mock: the same code paths a live
run would take, against a venue that fills at bid/ask and charges commission.

Price data
----------

``--candles PATH`` reads ``time,open,high,low,close`` CSV, so the strategy can be pointed at
real history. Without it, :func:`synthetic_candles` produces a deterministic zigzag. That is
enough to prove the wiring and **not** enough to learn anything about the strategy, which the
output says outright — a synthetic series has no opinion about whether this system makes
money.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from stop_order_scalp.domain.enums import Environment
from stop_order_scalp.domain.exceptions import RiskError
from stop_order_scalp.domain.models import Candle, EnvironmentSettings, TradePlan
from stop_order_scalp.domain.value_objects import Price, SymbolSpecification
from stop_order_scalp.execution.gates import LiveInterlock, SimulatedGate
from stop_order_scalp.execution.order_manager import OrderManager
from stop_order_scalp.execution.position_manager import PositionManager
from stop_order_scalp.execution.simulated_broker import (
    SimulatedBroker,
    configure_specification,
)
from stop_order_scalp.infrastructure.config import AppConfig
from stop_order_scalp.infrastructure.persistence import StateLedger
from stop_order_scalp.lifecycle.trade_lifecycle import TradeLifecycle
from stop_order_scalp.market_data.candles import (
    aggregate,
    freeze_closed_bars,
    load_candles_csv,
)
from stop_order_scalp.market_data.symbols import MEASURED, us30_specification
from stop_order_scalp.market_data.timeframes import period_seconds
from stop_order_scalp.risk.risk_manager import RiskManager, RiskRequest
from stop_order_scalp.strategy.strategy import StopOrderStrategy, StrategyContext

__all__ = [
    "TradingService",
    "aggregate",
    "build_service",
    "load_candles_csv",
    "size_decision",
    "synthetic_candles",
]

#: The specification used when no broker is connected. Deliberately labelled *assumed* --
#: Phase 11 replaces it with the real one. Every risk figure this project reports is stated
#: against these numbers and nothing else.

#: A starting balance for the dry run. Named ``assumed`` for the same reason.
ASSUMED_BALANCE = Decimal("10000")
#: The simulated venue's spread. One point, the measured minimum increment.
SPREAD_POINTS = Decimal("1")


@dataclass
class CycleReport:
    """What one pass of the loop decided. A value, so the CLI can print it verbatim."""

    index: int
    moment: datetime
    state: str
    action: str
    detail: str = ""
    price: str = ""
    placed: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "cycle": self.index,
            "time": self.moment.isoformat(timespec="seconds"),
            "state": self.state,
            "action": self.action,
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.price:
            payload["price"] = self.price
        if self.placed:
            payload["placed"] = True
        return payload


@dataclass
class TradingService:
    """Runs the decision loop and remembers what happened.

    Holds the wiring, plus a bounded report. It owns no rule: every decision is delegated, and
    the only judgement here is *when to ask*.
    """

    config: AppConfig
    strategy: StopOrderStrategy
    risk_manager: RiskManager
    order_manager: OrderManager
    lifecycle: TradeLifecycle
    broker: SimulatedBroker
    specification: SymbolSpecification
    settings: EnvironmentSettings
    candles_m15: Sequence[Candle]
    candles_m1: Sequence[Candle]
    position_manager: PositionManager
    cycles: list[CycleReport] = field(default_factory=list)
    _reference: datetime | None = None

    # --- the loop -------------------------------------------------------

    def run(self, max_cycles: int = 1, interval: float = 0.0) -> int:
        """Run up to ``max_cycles`` decision cycles. Returns how many ran.

        ``interval`` is honoured only between cycles, and the dry run defaults it to 0 so
        a hundred cycles finish instantly. Sleeping in a loop is the sort of thing that makes
        a dry run take ten minutes and look hung.
        """
        del interval  # No real waiting in a dry run; see the docstring.
        self.cycles = []
        step = timedelta(seconds=self._step_seconds())
        reference = self._first_reference() - step
        horizon = self._last_closed_moment()

        for index in range(1, max(1, max_cycles) + 1):
            reference += step
            if reference > horizon:
                # Past the end of the data: there is nothing new to decide on. Stopping is the
                # honest outcome -- continuing would re-decide on the last bar forever and
                # report it as fresh.
                break
            if not self._advance(reference, index):
                break
        return len(self.cycles)

    def last_report(self) -> dict[str, Any]:
        """The report the CLI prints. Plain data, so it serialises without ceremony."""
        return {
            "environment": str(self.settings.environment),
            "broker": type(self.broker).__name__,
            "symbol": self.strategy.symbol,
            "state": str(self.lifecycle.state),
            "specification_source": "assumed (no broker connected)",
            "rule": self.strategy.describe(),
            "cycles": [cycle.to_dict() for cycle in self.cycles],
            "orders_on_book": len(self.broker.orders()),
            "positions": len(self.broker.positions()),
            "balance": str(self.broker.account().balance.amount),
            "note": (
                "A dry run proves the wiring, not the strategy. Synthetic candles cannot say "
                "whether this system makes money; point --candles at real history for that."
            ),
        }

    # --- internals -------------------------------------------------------

    def _first_reference(self) -> datetime:
        """The first decision moment: the moment the first **direction** candle closes.

        Not the first M1 bar. The M15 direction filter is consulted first, and a reference
        before the first M15 close leaves it with nothing to read -- so every early cycle
        would decline and the run would look broken when it is merely early.

        Starting here and advancing one bar per cycle makes a dry run a **walk forward**
        through the data: cycle *n* sees the first *n* bars and nothing after them.
        """
        if self._reference is not None:
            return self._reference
        if self.candles_m1:
            self._reference = self.candles_m1[0].open_time + timedelta(
                seconds=period_seconds(
                    self.config.strategy.entry.direction_timeframe
                )
            )
        else:  # pragma: no cover - a run with no candles at all
            self._reference = datetime.now(UTC)
        return self._reference

    def _last_closed_moment(self) -> datetime:
        """When the final candle closes. Past this there is nothing left to decide on."""
        if not self.candles_m1:
            return self._first_reference()
        return self.candles_m1[-1].open_time + timedelta(seconds=self._step_seconds())

    def _step_seconds(self) -> int:
        """How far one cycle advances the reference: **one whole entry candle**.

        A second per cycle is wrong for minute data. Every cycle would re-decide on the same
        bars and the report would show one price repeated -- the loop running, and going
        nowhere. Advancing a full bar is what makes this a walk forward through the data
        rather than a repeated look at its last bar.
        """
        return period_seconds(self.config.strategy.entry.timeframe)

    def _advance(self, reference: datetime, index: int) -> bool:
        """One cycle. Returns ``False`` when there is nothing left to look at."""
        window_m1, _ = self._window(self.candles_m1, reference)
        window_m15, _ = self._window(self.candles_m15, reference)
        if not window_m1 or not window_m15:
            return False

        self._publish(window_m1[-1])

        decision = self.strategy.evaluate(
            StrategyContext(
                symbol=self.strategy.symbol,
                m15_candles=window_m15,
                m1_candles=window_m1,
                reference=reference,
                specification=self.specification,
            )
        )
        self.lifecycle.attach_market(
            self.broker.tick(self.strategy.symbol), self.specification
        )

        # The bridge between strategy and execution, and the reason it lives here and not in
        # either layer: the strategy decides *what* to trade and has no opinion about size,
        # while the order manager needs a fully sized plan. Neither layer may depend on the
        # other, so the composition root is the only place that can hold both.
        plan, reason = self._size(decision)
        step = self.lifecycle.tick(plan if plan is not None else decision)

        # Adopt anything that filled. The venue closes and opens without telling us, so the
        # state machine only learns about a position by being shown the book -- without this
        # the run would fill an order and then keep reporting "waiting for trigger" while
        # trailing a position that plainly existed.
        self.lifecycle.observe_fills()

        self.cycles.append(
            CycleReport(
                index=index,
                moment=reference,
                state=str(self.lifecycle.state),
                action=_action_of(step),
                detail=_describe(step, self, reason),
                price=str(window_m1[-1].close),
                placed="placed" in step.actions,
            )
        )
        return True

    def _size(self, decision: Any) -> tuple[TradePlan | None, str]:
        """Turn a strategy decision into a sized plan, or say why not.

        Returns ``(None, reason)`` rather than raising: declining is the expected outcome
        most of the time, and a report that cannot say *why* it declined is not a report.
        """
        return size_decision(
            self.risk_manager, self.broker.account(), self.specification, decision
        )

    def _window(
        self, candles: Sequence[Candle], reference: datetime
    ) -> tuple[list[Candle], Any]:
        lookback = self.config.strategy.entry.lookback
        closed, report = freeze_closed_bars(candles, reference=reference)
        return list(closed[-lookback:]), report

    def _publish(self, candle: Candle) -> None:
        """Push the newest closed price into the simulated venue.

        A one-tick spread, because a venue with no spread fills a stop at exactly its level
        and the strategy would look better than it is.
        """
        close = candle.close.value
        self.broker.publish(
            self.strategy.symbol,
            close,
            close + SPREAD_POINTS * MEASURED.point,
            digits=self.specification.digits,
        )


def size_decision(
    risk_manager: RiskManager,
    account: Any,
    specification: SymbolSpecification,
    decision: Any,
) -> tuple[TradePlan | None, str]:
    """Turn a strategy decision into a sized plan, or say why not.

    A function rather than a method so the dry run and the demo run size positions with the
    same code. Two copies of "how big" is how two runs disagree about risk.
    """
    signal = getattr(decision, "signal", None)
    if signal is None:
        return None, _reason(decision) or "no signal"
    try:
        plan = risk_manager.plan_for(
            RiskRequest(
                signal=signal,
                account=account,
                specification=specification,
                risk=risk_manager.risk_settings,
                target=risk_manager.target_settings,
            )
        )
    except RiskError as exc:
        # The risk engine's own verdict, in its own words. This is where a position smaller
        # than the broker minimum, or a risk budget that does not fit, shows up.
        return None, f"risk_refused: {exc}"
    return plan, f"planned {plan.volume.lots} lots at {plan.entry}"


def build_service(
    config: AppConfig,
    *,
    dry_run: bool = False,
    paper: bool = False,
    live: bool = False,
    candles: Sequence[Candle] | None = None,
    m15_candles: Sequence[Candle] | None = None,
) -> TradingService:
    """Wire the system and return it ready to run.

    ``--live`` is accepted by the CLI and refused here: reaching a real broker needs the
    specification, the magic number and the recovery path proven against a real terminal, and
    Phase 11 is where that happens. Refusing loudly beats half-wiring it.
    """
    if live:
        raise LiveTradingUnavailable(
            "--live is not available. Every wire value, retcode and broker limit this "
            "project uses is still unverified against a real terminal. Run --dry-run, and "
            "see ROADMAP.md Phase 11 for what must happen first."
        )
    del paper, dry_run  # A dry run is the only mode this module builds.

    symbol = config.strategy.symbol
    specification = us30_specification(symbol)
    # The venue must charge the commission the configuration states, or the two halves of the
    # system disagree about the cost of a trade: the risk engine sizes the position *net* of
    # `commission_per_lot`, and a venue charging nothing books the full gross as profit. Every
    # P/L figure in a dry run then overstates the result by exactly the commission the sizing
    # already accounted for. Silent, and it flatters the strategy.
    configure_specification(
        specification,
        commission=config.strategy.risk.commission_per_lot,
        commission_mode=config.strategy.risk.commission_mode,
    )

    settings = EnvironmentSettings(
        environment=Environment.DRY_RUN, allow_live=False, allow_order=False
    )
    broker = SimulatedBroker(settings, balance=ASSUMED_BALANCE)
    broker.connect()

    risk_manager = RiskManager(config.strategy.risk, config.strategy.target)
    order_manager = OrderManager(
        config.execution,
        config.strategy.order,
        risk_manager,
        # A simulated venue needs no LIVE interlock, and SimulatedGate refuses LIVE anyway --
        # so a misconfiguration fails closed rather than trading.
        gate=SimulatedGate(),
        interlock=LiveInterlock.from_settings(settings),
    )
    position_manager = PositionManager(
        config.strategy.break_even, config.strategy.trailing
    )
    # **Load, never construct.** The constructor takes a path and starts empty; only
    # `load` reads it. Constructing here would hand recovery a blank ledger, so
    # `recover()` would find nothing to verify, and the first `record` would flush an
    # empty file over the top of every intent already on disk -- silently deleting the
    # record whose entire purpose is to stop a duplicate order. Every Phase 7 test passed
    # because each built its own ledger; only a real second run shares the file.
    #
    # **A dry run's ledger is not persisted by default, and that is not a shortcut.**
    #
    # `SimulatedBroker` is constructed fresh on the line above and has no memory of any
    # previous run. A durable ledger beside it would be a lie: it would claim "this identity
    # reached a venue" about a venue that has never heard of it, and the duplicate guard would
    # then refuse a legitimate placement on the second run of an identical command. The
    # ledger's whole value is that it and the venue agree on what was sent, so the ledger's
    # lifetime must be the venue's lifetime -- which is also what makes a dry run
    # reproducible.
    #
    # **Unless the operator named a file.** An explicit `state_file` is a deliberate request
    # for that ledger to be used, and it is how restart recovery gets exercised at all: two
    # runs pointed at one path must share it, or there is nothing to recover. Overriding the
    # default is exactly the operator saying "I want this persisted".
    #
    # Every rule still applies either way -- record before send, refuse a repeated tag,
    # settle once. Only durability changes.
    if settings.environment is Environment.DRY_RUN and config.paths.state_file_is_default:
        ledger: StateLedger = StateLedger.in_memory()
    else:
        ledger = StateLedger.load(config.paths.state_file_for(settings.environment))

    lifecycle = TradeLifecycle(
        broker,
        ledger,
        order_manager,
        clock=_FixedStepClock(),
        position_manager=position_manager,
        magic_number=config.execution.magic_number,
        symbol=symbol,
        refresh_pending=config.strategy.entry.refresh_pending,
    )
    lifecycle.recover()

    series_m1 = list(candles or ())
    series_m15 = list(m15_candles or series_m1)
    return TradingService(
        config=config,
        strategy=StopOrderStrategy(config.strategy, specification),
        risk_manager=risk_manager,
        order_manager=order_manager,
        lifecycle=lifecycle,
        broker=broker,
        specification=specification,
        settings=settings,
        candles_m15=series_m15,
        candles_m1=series_m1,
        position_manager=position_manager,
    )


def _reason(decision: Any) -> str:
    """Why the strategy declined, in its own words.

    A report that says "no trade" without a reason is the least useful output a trading
    system can produce: it cannot distinguish a quiet market from a broken feed, a wrong
    timeframe, or a rule that is never satisfied. The strategy already carries the reason, so
    passing it through costs one line.
    """
    reason = getattr(decision, "reason", None)
    if not reason:
        return ""
    detail = getattr(decision, "detail", "")
    return f"{reason}: {detail}" if detail else str(reason)


def _action_of(step: Any) -> str:
    """This cycle's action, with one definition of "nothing happened".

    Previously the report synthesised ``"hold"`` when a step carried no actions while
    ``_describe`` read the same emptiness as ``""``. So the report labelled a cycle ``hold``
    and described it from a branch that a ``hold`` never reached -- which is why a line could
    read "hold" beside "planned 0.4 lots at 40006.3" while the venue held an order at
    40005.7. Two places deciding what "no action" means is how that happened.

    Now there is one function, and both the label and the description come from it.
    """
    actions = getattr(step, "actions", ()) or ()
    return str(actions[0]) if actions else "hold"


def _describe(step: Any, service: TradingService, fallback: str) -> str:
    """What actually happened this cycle, in one line.

    Read back from the **broker** rather than from the proposal. A report that shows the
    intended level next to a `trailed` action is worse than no report at all: it puts
    "planned 0.4 lots at 40007.2" beside a stop that moved to 40011.7 and leaves the reader
    guessing which number the action refers to. The venue's own state has no such ambiguity.
    """
    action = _action_of(step)
    if action in ("trailed", "break_even_armed"):
        positions = service.broker.positions()
        if positions:
            position = positions[0]
            return (
                f"{action}: stop now {position.stop_loss}, entry {position.entry}, "
                f"{position.side} {position.volume.lots} lots"
            )
    if action == "placed":
        orders = service.broker.orders()
        if orders:
            record = orders[0]
            return (
                f"placed {record.kind} ticket {record.ticket} at {record.entry} "
                f"sl {record.stop_loss} tp {record.take_profit}"
            )
    if action == "hold":
        # A `hold` means the strategy wanted to trade and the lifecycle declined to send,
        # usually because something is already resting or open. Reporting the *plan* here
        # is the same plan-versus-reality confusion as above, one level down: the reader
        # sees "planned 0.4 lots at 40006.3" while the venue is holding an order placed at
        # 40005.7, and reasonably concludes there are two orders. Name what the venue holds.
        return _describe_resting(service) or fallback
    notes = getattr(step, "notes", ())
    if notes:
        first: str = str(notes[0])
        return first
    return fallback


def _describe_resting(service: TradingService) -> str:
    """What the venue is actually holding, for a cycle that placed nothing.

    A position first: if one is open, that is what the reader needs to know, and the stop
    beside it is the number that matters.
    """
    positions = service.broker.positions()
    if positions:
        position = positions[0]
        return (
            f"holding {position.side} {position.volume.lots} lots, entry {position.entry}, "
            f"stop {position.stop_loss}"
        )
    orders = service.broker.orders()
    if orders:
        resting = ", ".join(
            f"{record.kind} ticket {record.ticket} at {record.entry}" for record in orders
        )
        return f"order already resting: {resting}"
    return ""


class LiveTradingUnavailable(RuntimeError):
    """``--live`` was asked for and is not available yet.

    A distinct type so the CLI can report it as "not available yet" rather than as a crash.
    """


class _FixedStepClock:
    """A clock that advances one second per call.

    A dry run has to have a clock that moves with the *data*, or the ledger timestamps and the
    freeze would disagree about what time it is. Using the wall clock here would make a
    replay depend on when it was run, which is exactly what the clock rule forbids.
    """

    __slots__ = ("_moment",)

    def __init__(self, start: datetime | None = None) -> None:
        self._moment = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        moment = self._moment
        self._moment = moment + timedelta(seconds=1)
        return moment


# =============================================================================
# Price data
# =============================================================================


def synthetic_candles(
    count: int = 90,
    *,
    start: datetime | None = None,
    timeframe: str = "M1",
    digits: int = 1,
    seed: Decimal = Decimal(40000),
) -> list[Candle]:
    """A deterministic trending series, so ``run --dry-run`` works with no data at all.

    **Not a market.** It exists to prove the wiring end to end with zero setup, and the report
    says so. A result computed from this series says nothing about whether the strategy works.

    It *trends* rather than oscillating, and that is not cosmetic. The M15 direction filter
    refuses a doji and a mixed body, so a zigzag produces a series where the correct behaviour
    is to never trade — which would demonstrate the wiring and look like a broken system. A
    trend lets a real order be placed, so the run exercises the whole path including risk,
    the gate and the ledger.

    The shape is **40 bars gently up, then 5 gently down, repeating**. Both numbers are
    load-bearing, and the docstring used to describe a different series than the code built,
    which is its own small warning:

    * **Up first, and for 40 bars.** The first decision happens at the 15-minute mark, so a
      pullback before then would leave a BUY STOP resting above the market that price never
      reached, and the run would end with a correct order and nothing else to show.
    * **Never longer than the entry offset.** Five down bars is a shallow retracement, so the
      5-bar M1 high always stays below the resting stop. A deeper one would fill the order
      immediately and the run would never reach the trailing logic that is the point of
      exercising it.

    A 120-bar run therefore contains both a BUY and a SELL, so a long enough run shows the
    system working in both directions rather than only the convenient one.
    """
    origin = start or datetime(2026, 3, 12, 0, 0, tzinfo=UTC)
    seconds = period_seconds(timeframe)
    candles: list[Candle] = []
    price = seed
    for index in range(count):
        # 40 bars gently up, 5 gently down. The proportions matter: the first decision
        # happens at the 15-minute mark, so a pattern that pulled back there would leave a
        # BUY STOP resting above the market that price never reached, and the run would end
        # with a correct order and nothing else to show. A slow uptrend lets the stop fill,
        # so the report can show the whole lifecycle.
        wave = Decimal("0.3") if (index % 45) < 40 else Decimal("-0.1")
        open_ = price
        close = price + wave
        high = max(open_, close) + Decimal("0.2")
        low = min(open_, close) - Decimal("0.2")
        candles.append(
            Candle(
                open_time=origin + timedelta(seconds=seconds * index),
                open=Price(open_, digits),
                high=Price(high, digits),
                low=Price(low, digits),
                close=Price(close, digits),
                timeframe_seconds=seconds,
                timeframe=timeframe,
                volume=Decimal("1"),
                tick_volume=1,
                is_confirmed=True,
            )
        )
        price = close
    return candles


