"""Replaying recorded candles through the real trading stack.

This module exists to answer one question honestly: *what would this rule have done on this
data?* Everything about how it is built follows from wanting the answer to be worth having.

The same code trades the replay and the dry run
----------------------------------------------
The venue is a :class:`~stop_order_scalp.execution.simulated_broker.SimulatedBroker`, the
decisions come from the same :class:`~stop_order_scalp.strategy.strategy.StopOrderStrategy`,
and every send goes through the same
:meth:`~stop_order_scalp.lifecycle.trade_lifecycle.TradeLifecycle.place_order`. There is no
second, simplified path. A backtest that takes a shortcut past ``place_order`` would skip the
write-intent record, the duplicate check and the retcode classification -- so it would not be
measuring this system, it would be measuring a different one that happens to share a strategy.

That is also why this cannot reuse
:class:`~stop_order_scalp.application.service.TradingService`: ``backtest`` sits *inside*
``application`` in the layer order and may not import it. The walk-forward loop is therefore
written again here, over the same collaborators, rather than reaching across a boundary.

No look-ahead, by construction
------------------------------
The only thing that reaches the strategy is
:func:`~stop_order_scalp.market_data.candles.freeze_closed_bars`, evaluated against a reference
that advances one bar per cycle. Cycle *n* sees the first *n* bars and nothing after them.

There is deliberately no "current forming bar" concept here. The temptation is to hand the
strategy the bar in progress, because that is what a live feed does -- but in a replay the bar
in progress is *already known*, so feeding it is handing over the future. A backtest that does
that reports a profit shaped exactly like competence. The freeze decides what is closed, and
that rule is the same one the dry run uses.

What a replay cannot tell you
-----------------------------
A replay over a CSV says what this rule did on *that file*. It does not say the rule works,
because the file is one sample of one market, chosen by whoever selected it. Every report this
module produces therefore names the file it read, and says in words that a number without a
provenance is not a result.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from stop_order_scalp.backtest.statistics import BacktestStatistics, EquityPoint, summarise
from stop_order_scalp.domain.enums import Environment
from stop_order_scalp.domain.exceptions import RiskError
from stop_order_scalp.domain.models import (
    Candle,
    EnvironmentSettings,
    OrderRecord,
    TradePlan,
)
from stop_order_scalp.domain.value_objects import SymbolSpecification
from stop_order_scalp.execution.gates import LiveInterlock, SimulatedGate
from stop_order_scalp.execution.order_manager import OrderManager
from stop_order_scalp.execution.position_manager import PositionManager
from stop_order_scalp.execution.simulated_broker import SimulatedBroker
from stop_order_scalp.infrastructure.config import AppConfig
from stop_order_scalp.infrastructure.persistence import StateLedger
from stop_order_scalp.lifecycle.trade_lifecycle import TradeLifecycle
from stop_order_scalp.market_data.candles import aggregate, freeze_closed_bars
from stop_order_scalp.market_data.symbols import us30_specification
from stop_order_scalp.market_data.timeframes import period_seconds
from stop_order_scalp.risk.risk_manager import RiskManager, RiskRequest
from stop_order_scalp.strategy.strategy import StopOrderStrategy, StrategyContext

__all__ = ["ReplayResult", "ReplayStep", "replay"]

#: The point value and balance a replay starts from when the configuration does not say.
#:
#: Both are *assumptions*, and both are reported in the result. A replay whose arithmetic
#: depends on a guessed tick value must not be quotable without the guess attached, so
#: ``ReplayResult.to_dict`` carries them into the output rather than leaving them in the code.
ASSUMED_BALANCE = Decimal("10000")

#: How a bar's price path is played into the venue.
#:
#: ``close``  only the close is published (legacy). Optimistic for stops -- an intrabar
#:            excursion through a trailing stop is never seen -- and pessimistic for entries,
#:            because a stop order fills only if the *close* is beyond it.
#: ``auto``   by the bar's colour: a bar that closed up is played open -> low -> high -> close,
#:            one that closed down open -> high -> low -> close. The usual heuristic, and the
#:            only one of the three that does not manufacture whipsaws: a bar that closed on
#:            its high almost never travelled to the high, then to the low, then back.
#: ``ohlc``   open -> high -> low -> close.
#: ``olhc``   open -> low -> high -> close.
#:
#: OHLC bars do not record which extreme came first, so neither ``ohlc`` nor ``olhc`` is
#: "the" answer. Both are plausible paths, and **the pair is a bracket**: a conclusion that
#: only holds under one ordering is not a conclusion. Tick data is what removes the ambiguity.
INTRABAR_MODES = frozenset({"close", "auto", "ohlc", "olhc"})


@dataclass(frozen=True, slots=True)
class ReplayStep:
    """What happened on one bar, as the venue and the lifecycle reported it."""

    index: int
    moment: datetime
    action: str
    state: str
    detail: str
    price: str
    balance: str
    equity: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "moment": self.moment.isoformat(),
            "action": self.action,
            "state": self.state,
            "detail": self.detail,
            "price": self.price,
            "balance": self.balance,
            "equity": self.equity,
        }


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """A replay, its numbers, and the provenance those numbers need to be believed."""

    symbol: str
    source: Path | None
    statistics: BacktestStatistics
    steps: tuple[ReplayStep, ...] = ()
    specification: SymbolSpecification | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self, *, include_steps: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": self.symbol,
            "source": None if self.source is None else str(self.source),
            "bars": len(self.steps),
            "specification_source": "assumed (no broker connected)",
            "specification": (
                None
                if self.specification is None
                else {
                    "point": str(self.specification.point),
                    "tick_size": str(self.specification.tick_size),
                    "tick_value": str(self.specification.tick_value),
                    "contract_size": str(self.specification.contract_size),
                }
            ),
            "statistics": self.statistics.to_dict(),
            "notes": list(self.notes),
        }
        if include_steps:
            payload["steps"] = [step.to_dict() for step in self.steps]
        return payload


class _StepClock:
    """A clock that follows the data, one second per reading.

    Not the wall clock. A ledger timestamped from the wall clock makes a replay depend on when
    it was run, which is the one thing a replay must not be.
    """

    __slots__ = ("_moment",)

    def __init__(self, start: datetime) -> None:
        self._moment = start

    def now(self) -> datetime:
        moment = self._moment
        self._moment = moment + timedelta(seconds=1)
        return moment


def replay(
    config: AppConfig,
    candles: Sequence[Candle],
    *,
    m15_candles: Sequence[Candle] | None = None,
    source: Path | None = None,
    max_cycles: int | None = None,
    starting_balance: Decimal = ASSUMED_BALANCE,
    slippage_points: Decimal | int | float = 0,
    spread_points: Decimal | int | float | None = None,
    intrabar: str = "close",
) -> ReplayResult:
    """Replay ``candles`` through the assembled trading stack and summarise the result.

    ``m15_candles`` defaults to M1 bars aggregated up to the configured direction timeframe.
    Aggregating rather than passing the M1 series through matters: the direction filter refuses
    a timeframe mismatch, and a replay that quietly fed it the wrong timeframe *and was
    accepted* would be a look-ahead bug wearing the costume of a result.
    """
    if intrabar not in INTRABAR_MODES:
        raise RiskError(f"intrabar must be one of {sorted(INTRABAR_MODES)}, got {intrabar!r}")
    symbol = config.strategy.symbol
    settings = EnvironmentSettings(
        environment=Environment.DRY_RUN, allow_live=False, allow_order=False
    )
    specification = us30_specification(symbol)
    _venue(specification, config)

    slip = Decimal(str(slippage_points))
    spread = Decimal(1) if spread_points is None else Decimal(str(spread_points))
    broker = SimulatedBroker(
        settings,
        balance=starting_balance,
        slippage=specification.points_to_price(slip) if slip else Decimal(0),
        continuous_path=intrabar != "close",
    )
    broker.connect()

    # **Always in memory, even when `state.path` names a file.**
    #
    # A replay's venue is built fresh on every call and remembers nothing, so a durable ledger
    # beside it would claim an order had reached a venue that had never heard of it -- and the
    # second replay would find the first one's intents and decline to place, which is how a
    # backtest becomes non-repeatable. The ledger's lifetime has to be the venue's lifetime.
    #
    # This is stated rather than left implicit because an operator who sets `state.path` and
    # then runs a backtest will reasonably expect a file to appear. It will not, and
    # ``ReplayResult.to_dict`` says so in its notes. Every rule still applies to the in-memory
    # ledger -- record before send, refuse a repeated tag, settle once -- so the write-intent
    # path is genuinely exercised rather than stubbed out.
    ledger = StateLedger.in_memory()
    risk_manager = RiskManager(config.strategy.risk, config.strategy.target)
    order_manager = OrderManager(
        config.execution,
        config.strategy.order,
        risk_manager,
        gate=SimulatedGate(),
        interlock=LiveInterlock.from_settings(settings),
    )
    position_manager = PositionManager(config.strategy.break_even, config.strategy.trailing)
    lifecycle = TradeLifecycle(
        broker,
        ledger,
        order_manager,
        clock=_StepClock(_first_reference(candles, config)),
        position_manager=position_manager,
        magic_number=config.execution.magic_number,
        symbol=symbol,
        refresh_pending=config.strategy.entry.refresh_pending,
    )
    lifecycle.recover()

    strategy = StopOrderStrategy(config.strategy, specification)
    series_m1 = list(candles)
    series_m15 = list(
        m15_candles
        if m15_candles is not None
        else aggregate(
            series_m1,
            config.strategy.entry.timeframe,
            config.strategy.entry.direction_timeframe,
        )
    )
    # Validate both series in full, once, before the first cycle slices into them. The
    # per-cycle path only shows the freeze part of the data, so this is what keeps a
    # duplicated timestamp at the end of a 30 000-bar file an error rather than silence.
    _validate_series(series_m1)
    _validate_series(series_m15)

    # Open times, once. `_prefix` bisects into these on every cycle.
    times_m1 = [candle.open_time for candle in series_m1]
    times_m15 = [candle.open_time for candle in series_m15]

    steps: list[ReplayStep] = []
    curve: list[EquityPoint] = []
    lookback = config.strategy.entry.lookback
    step_seconds = period_seconds(config.strategy.entry.timeframe)
    reference = _first_reference(series_m1, config) - timedelta(seconds=step_seconds)
    horizon = (
        series_m1[-1].open_time + timedelta(seconds=step_seconds) if series_m1 else reference
    )
    limit = max_cycles if max_cycles is not None else max(1, len(series_m1))

    for index in range(1, limit + 1):
        reference += timedelta(seconds=step_seconds)
        if reference > horizon:
            break
        window_m1 = _closed_window(series_m1, times_m1, reference, lookback)
        window_m15 = _closed_window(series_m15, times_m15, reference, lookback)
        if not window_m1 or not window_m15:
            break

        if intrabar != "close":
            # Play the bar's path *before* the decision it informs: a pending order placed at
            # the end of the previous cycle faces this bar's whole range, not just its close.
            # Positions are managed between sub-ticks, as a live loop polling every second
            # would manage them.
            broker.set_time(reference)
            for price in _intrabar_path(window_m1[-1], intrabar)[:-1]:
                _publish_price(broker, symbol, price, specification, spread)
                lifecycle.attach_market(broker.tick(symbol), specification)
                lifecycle.observe_fills()
                lifecycle.tick(None)

        _publish(broker, symbol, window_m1[-1], specification, spread)
        # **Move the venue's clock to the data.** SimulatedBroker has `set_time` for exactly
        # this and it is not optional: its default is a frozen 2026-01-01, so without this
        # every `placed_at`, `opened_at` and `closed_at` is the same instant. That does not
        # look like a crash -- the replay produces a full report -- but every trade duration
        # comes out as 0, the equity curve's timestamps are fiction, and order expiry is
        # compared against a date the candles have nothing to do with. Silent, and wrong in
        # exactly the fields a reader would use to judge the result.
        broker.set_time(reference)

        decision = strategy.evaluate(
            StrategyContext(
                symbol=symbol,
                m15_candles=window_m15,
                m1_candles=window_m1,
                reference=reference,
                specification=specification,
            )
        )
        lifecycle.attach_market(broker.tick(symbol), specification)
        plan, reason = _size(risk_manager, decision, broker, specification)
        step = lifecycle.tick(plan if plan is not None else decision)
        lifecycle.observe_fills()

        account = broker.account()
        steps.append(
            ReplayStep(
                index=index,
                moment=reference,
                action=step.actions[0] if step.actions else "hold",
                state=str(lifecycle.state),
                detail=_detail(step, broker, reason),
                price=str(window_m1[-1].close),
                balance=str(account.balance.amount),
                equity=str(account.equity.amount),
            )
        )
        curve.append(
            EquityPoint(
                moment=reference,
                balance=account.balance.amount,
                equity=account.equity.amount,
            )
        )

    notes = [
        "A replay measures this rule on this file. It does not measure the rule.",
        f"Symbol specification is assumed: point {specification.point}, tick value "
        f"{specification.tick_value} per lot. On a real US30 contract these differ, and every "
        "money figure here scales with them.",
        "This replay's ledger is in memory and nothing was written. `state.path` applies to "
        "the trading path, not to a replay: a replay's venue is rebuilt on every call, so a "
        "persisted ledger would outlive the venue it describes and make the second run of an "
        "identical command refuse to trade.",
    ]
    notes.append(
        f"Spread modelled: {spread} points. Adverse slippage modelled: {slip} points. "
        f"Price path within a bar: {intrabar}."
    )
    if intrabar == "close":
        notes.append(
            "Only each bar's close was published, so protective stops never saw an intrabar "
            "extreme and a stop-order entry filled only if the close was beyond it. Run with "
            "--intrabar auto, and with ohlc and olhc to see how much the ordering matters."
        )
    notes.append(_cost_note(config, specification, spread))
    if source is None:
        notes.append(
            "No source file: the candles were generated, not read from history. Nothing here "
            "is evidence about a market."
        )

    account = broker.account()
    statistics = summarise(
        broker.history(magic_number=config.execution.magic_number),
        curve,
        symbol=symbol,
        starting_balance=starting_balance,
        ending_balance=account.balance.amount,
        open_at_end=len(broker.positions(magic_number=config.execution.magic_number)),
        notes=notes,
    )
    return ReplayResult(
        symbol=symbol,
        source=source,
        statistics=statistics,
        steps=tuple(steps),
        specification=specification,
        notes=tuple(notes),
    )


def _intrabar_path(candle: Candle, mode: str) -> list[Decimal]:
    """The prices a bar passes through, in order, ending on its close."""
    high_first = (
        candle.close.value < candle.open.value if mode == "auto" else mode == "ohlc"
    )
    first, second = (
        (candle.high.value, candle.low.value)
        if high_first
        else (candle.low.value, candle.high.value)
    )
    return [candle.open.value, first, second, candle.close.value]


def _publish_price(
    broker: SimulatedBroker,
    symbol: str,
    price: Decimal,
    specification: SymbolSpecification,
    spread_points: Decimal,
) -> None:
    broker.publish(
        symbol,
        price,
        price + specification.point * spread_points,
        digits=specification.digits,
    )


def _cost_note(config: AppConfig, specification: SymbolSpecification, spread: Decimal) -> str:
    """What one round trip costs *relative to what the stop risks*, in one line.

    Commission and spread are paid on every trade whether it wins or not, so the number that
    decides whether a rule can work at all is their size against the stop distance. It is
    stated in the report because it is invisible in the configuration: ``6.0`` and ``100``
    look unrelated until the tick value turns them into dollars.
    """
    per_point = specification.tick_value / specification.tick_size * specification.point
    stop_points = Decimal(config.strategy.target.stop_loss_points)
    risk_lot = stop_points * per_point
    risk_cfg = config.strategy.risk
    commission_lot = risk_cfg.commission_per_lot * Decimal(risk_cfg.commission_mode.sides)
    spread_lot = spread * per_point
    if risk_lot <= 0:
        return "Cost note unavailable: the stop distance is not positive."
    total = commission_lot + spread_lot
    return (
        f"Round-trip cost per lot: commission {commission_lot} + spread {spread_lot} = {total}, "
        f"against {risk_lot} of price risk at a {stop_points}-point stop "
        f"({(total / risk_lot * 100):.0f}% of the stop distance, paid on every trade)."
    )


def _first_reference(candles: Sequence[Candle], config: AppConfig) -> datetime:
    """The first decision moment: the close of the first *direction* candle.

    Not the first M1 bar. The direction filter is consulted first, and a reference before its
    first close leaves it with nothing to read -- so every early cycle would decline and the
    replay would look broken when it is merely early.
    """
    if not candles:
        return datetime(2026, 1, 1, tzinfo=UTC)
    return candles[0].open_time + timedelta(
        seconds=period_seconds(config.strategy.entry.direction_timeframe)
    )


def _closed_window(
    candles: Sequence[Candle],
    open_times: Sequence[datetime],
    reference: datetime,
    lookback: int,
) -> list[Candle]:
    """The last ``lookback`` bars closed at ``reference``. Nothing after it.

    The freeze is the only thing permitted to decide what is closed. That is what makes this a
    walk forward rather than a look-ahead: there is no path in this module by which a bar later
    than ``reference`` can reach the strategy.

    **The freeze is still what decides** -- ``_prefix`` only bounds how much of the series it is
    asked to look at, and any bar it cut off would have been dropped by the freeze anyway. It is
    a performance hint, not a second copy of the closure rule, and the authority stays where it
    is. Handing the freeze the whole 30 000-bar series on every one of 30 000 cycles is
    O(n^2): it sorts and re-validates each time, and a one-month replay took over twenty-five
    minutes. The series is validated in full once, in :func:`_validate_series`, so nothing is
    skipped -- only repeated.
    """
    closed, _ = freeze_closed_bars(
        _prefix(candles, open_times, reference, lookback), reference=reference
    )
    return list(closed[-lookback:])


def _prefix(
    candles: Sequence[Candle],
    open_times: Sequence[datetime],
    reference: datetime,
    lookback: int,
) -> Sequence[Candle]:
    """The only bars worth handing the freeze at this reference.

    A bar is closed at ``reference`` only if its own period had elapsed, so on an ascending
    series every closed bar starts at or before ``reference - period``. Everything from index 0
    to there is irrelevant: the caller keeps the last ``lookback`` closed bars, and the strategy
    only ever sees those. So the window is ``lookback`` bars before the cutoff, plus two after
    it so the freeze still sees -- and still reports -- the bar in progress. A cut that hid the
    forming bar would make ``FreezeReport`` claim nothing was forming.

    Both bounds matter, and the first one is the one that is easy to miss. Returning
    ``candles[:index + 2]`` is already a large improvement on handing over the whole series, and
    it is still O(n^2): the prefix grows with the reference, so a 3 000-cycle replay called
    ``is_closed_at`` 4.6 million times. Cutting the *start* as well makes it O(n · lookback).

    ``open_times`` is passed in rather than computed here, because it is the same on every
    cycle. Rebuilding it per call is O(n) per cycle and undoes the whole thing: on a one-month
    file that is the difference between seconds and minutes.
    """
    period = timedelta(seconds=period_seconds(_timeframe_of(candles)))
    cutoff = reference - period
    index = bisect.bisect_right(open_times, cutoff)
    return candles[max(0, index - lookback) : index + 2]


def _timeframe_of(candles: Sequence[Candle]) -> str:
    return candles[0].timeframe if candles else "M1"


def _validate_series(candles: Sequence[Candle]) -> None:
    """Check the whole series once, up front.

    The per-cycle path slices, so the duplicate and mixed-timeframe checks in the freeze only
    ever see part of the data. Running the freeze once across everything means the series is
    still validated in full -- a duplicated timestamp in bar 29 000 is still an error -- instead
    of being silently out of scope.
    """
    if candles:
        freeze_closed_bars(candles, reference=datetime.max.replace(tzinfo=UTC))


def _publish(
    broker: SimulatedBroker,
    symbol: str,
    candle: Candle,
    specification: SymbolSpecification,
    spread_points: Decimal = Decimal(1),
) -> None:
    """Push the newest closed price into the venue, with a spread of ``spread_points``.

    A venue with no spread fills a stop at exactly its level, and the strategy would look
    better than it is. The legacy default is one point -- the assumed minimum tick -- but
    the spread measured on the Alpari US30 demo was 18 points (``docs/mt5/MEASURED_US30.json``),
    so pass ``--spread-points`` for anything that is going to be quoted. The value used is
    reported in the result's notes.
    """
    close = candle.close.value
    broker.publish(
        symbol,
        close,
        close + specification.point * spread_points,
        digits=specification.digits,
    )


def _size(
    risk_manager: RiskManager,
    decision: Any,
    broker: SimulatedBroker,
    specification: SymbolSpecification,
) -> tuple[TradePlan | None, str]:
    """Turn a decision into a sized plan, or say why not.

    Duplicated from the service's equivalent on purpose rather than shared, because sharing it
    would mean ``backtest`` importing ``application``. The two are kept honest by a test that
    asserts they accept and reject the same decisions -- which is a weaker guarantee than
    sharing the code, and the honest way to say so.
    """
    signal = getattr(decision, "signal", None)
    if signal is None:
        reason = getattr(decision, "reason", None)
        detail = getattr(decision, "detail", "")
        text = f"{reason}: {detail}" if reason and detail else (str(reason) if reason else "no signal")
        return None, text
    try:
        plan = risk_manager.plan_for(
            RiskRequest(
                signal=signal,
                account=broker.account(),
                specification=specification,
                risk=risk_manager.risk_settings,
                target=risk_manager.target_settings,
            )
        )
    except RiskError as exc:
        return None, f"risk_refused: {exc}"
    return plan, f"planned {plan.volume.lots} lots at {plan.entry}"


def _detail(step: Any, broker: SimulatedBroker, fallback: str) -> str:
    """One line describing what actually happened, read back from the venue.

    Never from the plan. A statistic computed from intended exits is a different number that
    looks like the right one, and this project has already shipped that bug once in a report.
    """
    action = step.actions[0] if step.actions else "hold"
    if action in ("trailed", "break_even_armed"):
        positions = broker.positions()
        if positions:
            position = positions[0]
            return (
                f"{action}: stop now {position.stop_loss}, entry {position.entry}, "
                f"{position.side} {position.volume.lots} lots"
            )
    if action == "placed":
        orders = broker.orders()
        if orders:
            record: OrderRecord = orders[0]
            return (
                f"placed {record.kind} ticket {record.ticket} at {record.entry} "
                f"sl {record.stop_loss} tp {record.take_profit}"
            )
    if action == "hold":
        positions = broker.positions()
        if positions:
            position = positions[0]
            return (
                f"holding {position.side} {position.volume.lots} lots, entry {position.entry}, "
                f"stop {position.stop_loss}"
            )
        orders = broker.orders()
        if orders:
            resting = ", ".join(
                f"{record.kind} ticket {record.ticket} at {record.entry}" for record in orders
            )
            return f"order already resting: {resting}"
    notes = getattr(step, "notes", ())
    if notes:
        return str(notes[0])
    return fallback


def _venue(specification: SymbolSpecification, config: AppConfig) -> None:
    """Apply the measured specification *and* the configured commission to the venue.

    Both halves, because they fail in the same direction. A venue that keeps a stale
    specification reports a profit figure scaled by the wrong tick value, and a venue that
    charges no commission while the risk engine sizes positions net of commission reports one
    that is too high by exactly the cost already accounted for. Both flatter the strategy, which
    is the direction this project must never be wrong in.
    """
    from stop_order_scalp.execution.simulated_broker import configure_specification

    configure_specification(
        specification,
        commission=config.strategy.risk.commission_per_lot,
        commission_mode=config.strategy.risk.commission_mode,
    )
