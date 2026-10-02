"""What a replay adds up to, computed from the venue's own records.

Every number here comes from :meth:`SimulatedBroker.history` -- the closed trades the venue
recorded when it closed them. None of it is reconstructed from the plans, and that is the
whole design of this module.

Why it matters
--------------
The dry-run report already had this bug once: it printed ``planned 0.4 lots at 40006.3`` beside
a stop that had actually moved to 40011.7, and nobody could tell which number the action
referred to. A statistic has the same failure mode with more decimal places. A win rate
computed from intended exits is not a slightly optimistic win rate, it is a different number
that looks like this one, and it will be quoted.

So: no trade enters these statistics unless the venue says it closed. No exit level is read
from a plan. No reason is inferred after the fact -- ``take_profit`` and ``stop_loss`` come from
:class:`~stop_order_scalp.execution.simulated_broker.ClosedTrade`, written at the moment of
closure while the levels that triggered it were still known.

What is deliberately absent
---------------------------
No equity curve, no Sharpe ratio, no "expected value per trade" in the abstract. Those need a
sampling model for the *untraded* periods, and a sampled curve is a claim about the future
wearing the costume of a measurement. The honest summary of a replay is: how many trades, how
many won, how much money moved, and how deep the worst point was. Anyone who wants a Sharpe
ratio can ask for one knowing exactly what it would assume.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from stop_order_scalp.execution.simulated_broker import ClosedTrade

__all__ = ["BacktestStatistics", "EquityPoint", "summarise"]


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """Balance at one moment, as the venue reported it.

    Marked-to-market, not closed-out: while a position is open, its floating P/L is part of
    what the account is worth, and a curve that ignored it would show a cliff at every entry
    and flatter every trade that was still running.
    """

    moment: datetime
    balance: Decimal
    equity: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "moment": self.moment.isoformat(),
            "balance": str(self.balance),
            "equity": str(self.equity),
        }


@dataclass(frozen=True, slots=True)
class BacktestStatistics:
    """The summary of one replay.

    Every field is derived from the venue's closed-trade history and its account snapshots.
    Fields that would require an assumption this project has not made -- a risk-free rate, a
    sampling model for untraded time -- are absent rather than zero, because a zero reads as a
    measurement.
    """

    symbol: str
    starting_balance: Decimal
    ending_balance: Decimal
    trades: tuple[ClosedTrade, ...]
    equity_curve: tuple[EquityPoint, ...] = ()
    #: The largest peak-to-trough fall in equity, and the money value at its worst point.
    max_drawdown: Decimal = Decimal(0)
    max_drawdown_equity: Decimal = Decimal(0)
    #: Trades still open when the data ran out. Counted, because a backtest that quietly
    #: drops an open position reports a result the account never had.
    open_at_end: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    # --- counts ----------------------------------------------------------

    @property
    def closed_count(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> tuple[ClosedTrade, ...]:
        return tuple(trade for trade in self.trades if trade.net.amount > 0)

    @property
    def losses(self) -> tuple[ClosedTrade, ...]:
        return tuple(trade for trade in self.trades if trade.net.amount < 0)

    @property
    def scratches(self) -> tuple[ClosedTrade, ...]:
        """Trades that closed for exactly nothing. Not a win and not a loss.

        Counted separately because folding them into either side quietly inflates one of the
        two numbers, and a strategy that trades a lot of break-even levels would report a
        suspiciously high win rate.
        """
        return tuple(trade for trade in self.trades if trade.net.amount == 0)

    @property
    def win_rate(self) -> Decimal | None:
        """Fraction of *decided* trades that won. ``None`` when nothing was decided.

        ``None`` rather than ``0`` because "no trades" and "every trade lost" are different
        facts and a zero would report the first as the second.
        """
        decided = self.wins + self.losses
        if not decided:
            return None
        return Decimal(len(self.wins)) / Decimal(len(decided))

    # --- money -----------------------------------------------------------

    @property
    def gross_profit(self) -> Decimal:
        return sum((trade.net.amount for trade in self.wins), Decimal(0))

    @property
    def gross_loss(self) -> Decimal:
        """A positive number. Losing money is not a negative loss."""
        return sum((-trade.net.amount for trade in self.losses), Decimal(0))

    @property
    def net_profit(self) -> Decimal:
        return sum((trade.net.amount for trade in self.trades), Decimal(0))

    @property
    def total_commission(self) -> Decimal:
        return sum((trade.commission.amount for trade in self.trades), Decimal(0))

    @property
    def profit_factor(self) -> Decimal | None:
        """Gross profit over gross loss. ``None`` when nothing lost.

        ``None`` because the ratio is undefined with no losses, and reporting ``inf`` would be
        a number this project cannot substantiate -- it would be an artefact of dividing by
        zero, dressed as a result.
        """
        loss = self.gross_loss
        if loss == 0:
            return None
        return self.gross_profit / loss

    @property
    def average_win(self) -> Decimal | None:
        wins = self.wins
        return None if not wins else self.gross_profit / Decimal(len(wins))

    @property
    def average_loss(self) -> Decimal | None:
        losses = self.losses
        return None if not losses else self.gross_loss / Decimal(len(losses))

    @property
    def largest_win(self) -> Decimal:
        return max((trade.net.amount for trade in self.wins), default=Decimal(0))

    @property
    def largest_loss(self) -> Decimal:
        """A positive number."""
        return max((-trade.net.amount for trade in self.losses), default=Decimal(0))

    @property
    def balance_change(self) -> Decimal:
        """What the account actually gained or lost.

        Reconciled against the trades rather than assumed equal to them: a fee the venue
        charged outside a close, or a starting balance this module was not told, would show up
        here as a difference. A statistics module that cannot be checked against the account
        is a statistics module making claims.
        """
        return self.ending_balance - self.starting_balance

    @property
    def return_percent(self) -> Decimal | None:
        if self.starting_balance == 0:
            return None
        return self.balance_change / self.starting_balance * Decimal(100)

    # --- exit reasons ----------------------------------------------------

    @property
    def exits_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for trade in self.trades:
            counts[trade.reason] = counts.get(trade.reason, 0) + 1
        return dict(sorted(counts.items()))

    # --- serialisation ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        win_rate = self.win_rate
        return {
            "symbol": self.symbol,
            "starting_balance": str(self.starting_balance),
            "ending_balance": str(self.ending_balance),
            "balance_change": str(self.balance_change),
            "return_percent": None if self.return_percent is None else str(
                self.return_percent.quantize(Decimal("0.0001"))
            ),
            "trades": {
                "closed": self.closed_count,
                "wins": len(self.wins),
                "losses": len(self.losses),
                "scratches": len(self.scratches),
                "win_rate": None if win_rate is None else str(
                    win_rate.quantize(Decimal("0.0001"))
                ),
                "open_at_end": self.open_at_end,
            },
            "money": {
                "gross_profit": str(self.gross_profit),
                "gross_loss": str(self.gross_loss),
                "net_profit": str(self.net_profit),
                "total_commission": str(self.total_commission),
                "profit_factor": (
                    None if self.profit_factor is None
                    else str(self.profit_factor.quantize(Decimal("0.0001")))
                ),
                "average_win": None if self.average_win is None else str(
                    self.average_win.quantize(Decimal("0.01"))
                ),
                "average_loss": None if self.average_loss is None else str(
                    self.average_loss.quantize(Decimal("0.01"))
                ),
                "largest_win": str(self.largest_win),
                "largest_loss": str(self.largest_loss),
            },
            "max_drawdown": str(self.max_drawdown),
            "max_drawdown_equity": str(self.max_drawdown_equity),
            "exits_by_reason": self.exits_by_reason,
            "trades_detail": [trade.to_dict() for trade in self.trades],
            "equity_curve": [point.to_dict() for point in self.equity_curve],
            "notes": list(self.notes),
        }


def _drawdown(curve: Sequence[EquityPoint]) -> tuple[Decimal, Decimal]:
    """The worst peak-to-trough fall in equity, and the equity at that point.

    Peak-to-trough rather than first-to-last, because a curve that ends lower than it started
    is not necessarily the one that suffered most on the way.
    """
    peak = Decimal(0)
    worst = Decimal(0)
    worst_at = Decimal(0)
    for point in curve:
        if point.equity > peak:
            peak = point.equity
        fall = peak - point.equity
        if fall > worst:
            worst = fall
            worst_at = point.equity
    return worst, worst_at


def summarise(
    trades: Sequence[ClosedTrade],
    equity_curve: Sequence[EquityPoint],
    *,
    symbol: str,
    starting_balance: Decimal,
    ending_balance: Decimal,
    open_at_end: int = 0,
    notes: Sequence[str] = (),
) -> BacktestStatistics:
    """Build the summary from the venue's records. The only way to make one.

    Takes the closed trades and the equity curve as arguments rather than reading a broker, so
    that the numbers can be computed from a fixture in a test and from a replay in production
    by the same code -- and so that nothing here can accidentally consult a *plan*.
    """
    drawdown, drawdown_at = _drawdown(equity_curve)
    return BacktestStatistics(
        symbol=symbol,
        starting_balance=starting_balance,
        ending_balance=ending_balance,
        trades=tuple(trades),
        equity_curve=tuple(equity_curve),
        max_drawdown=drawdown,
        max_drawdown_equity=drawdown_at,
        open_at_end=open_at_end,
        notes=tuple(notes),
    )
