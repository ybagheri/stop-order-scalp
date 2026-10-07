"""What an operator does by hand: look at the account, cancel orders, close positions.

``run --demo`` opens trades (it places a pending stop that becomes a position when price crosses
it) and manages them (break-even, trailing). It does not close one on request, and it stops
managing the moment it stops running. These are the commands for everything else:

* ``book``     -- what the strategy has on the account right now. Reads only.
* ``cancel``   -- remove resting pending orders.
* ``close``    -- close open positions at market.
* ``flatten``  -- cancel every order, then close every position. The emergency stop.

Safety rules, all of them deliberate:

* **Preview first.** Without ``--yes`` nothing is sent; the command prints what it *would* do and
  whether it is permitted. Acting is a second, explicit step.
* **Only this strategy's trades.** Orders and positions are selected by the strategy's magic
  number and symbol. A ticket that belongs to anything else -- a trade opened by hand in the
  terminal -- is refused by name, not touched.
* **Demo only, four ways.** ``SOS_ENVIRONMENT=DEMO``, the operation's own switch
  (``SOS_ALLOW_ORDER`` for cancel, ``SOS_ALLOW_CLOSE`` for close), ``SOS_ALLOW_LIVE=false``, and
  the terminal itself confirming a demo account. The gates are :class:`DemoOrderGate` and
  :class:`DemoCloseGate`, which refuse ``LIVE`` outright.
* **One attempt per ticket.** An ambiguous answer is reported as ``unknown`` and never retried;
  run ``book`` to see what is actually there.

The demo ledger (``state/state-demo.json``) is not edited here. After a cancel or a close, the
next ``run --demo`` reconciles it against the account when it starts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from stop_order_scalp.domain.enums import Environment, Side
from stop_order_scalp.domain.exceptions import (
    BrokerError,
    BrokerNotConnectedError,
    BrokerRejectedError,
    ExecutionUnknownError,
)
from stop_order_scalp.domain.models import EnvironmentSettings
from stop_order_scalp.execution.gates import DemoCloseGate, DemoOrderGate
from stop_order_scalp.execution.mt5_broker import MetaTrader5Broker
from stop_order_scalp.infrastructure.config import AppConfig
from stop_order_scalp.market_data.mt5_feed import MT5Feed
from stop_order_scalp.market_data.mt5_module import MT5Module

__all__ = ["OperatorService", "OperatorUnavailable", "build_operator"]


class OperatorUnavailable(RuntimeError):
    """The operator tools cannot start, and the message says what to change."""


@dataclass
class OperatorService:
    """Acts on the strategy's own orders and positions on a confirmed demo account."""

    settings: EnvironmentSettings
    broker: Any
    symbol: str
    order_gate: DemoOrderGate
    close_gate: DemoCloseGate
    log: Callable[[str], None] = field(default=lambda _line: None)

    # --- reading ----------------------------------------------------------

    def _orders(self) -> list[Any]:
        return list(self.broker.orders(magic_number=self.settings.magic_number, symbol=self.symbol))

    def _positions(self) -> list[Any]:
        return list(
            self.broker.positions(magic_number=self.settings.magic_number, symbol=self.symbol)
        )

    def book(self) -> dict[str, Any]:
        """The strategy's orders and positions, with the account and the current price."""
        account = self.broker.account()
        tick = self.broker.tick(self.symbol)
        orders = self._orders()
        positions = self._positions()
        floating = sum((p.profit.amount for p in positions), start=Decimal(0))
        return {
            "symbol": self.symbol,
            "magic_number": self.settings.magic_number,
            "account": {
                "server": account.server,
                "is_demo": account.is_demo,
                "currency": account.currency,
                "balance": str(account.balance.amount),
                "equity": str(account.equity.amount),
            },
            "price": None if tick is None else {"bid": str(tick.bid), "ask": str(tick.ask)},
            "resting_orders": [_order_row(o) for o in orders],
            "positions": [_position_row(p) for p in positions],
            "floating_profit": str(floating),
            "note": (
                "Only orders and positions carrying this strategy's magic number are listed "
                "and acted on. Anything opened by hand in the terminal is left alone."
            ),
        }

    def history(self, *, days: int = 7) -> dict[str, Any]:
        """What the strategy's trades did: every deal, and the totals.

        A position that has closed is no longer in ``book``; its result is only here. ``net`` is
        profit plus commission plus swap over every deal, so it includes what the trades cost.
        """
        deals = list(
            self.broker.deals(
                days=days, magic_number=self.settings.magic_number, symbol=self.symbol
            )
        )
        closes = [d for d in deals if d["leg"] == "close"]
        net = sum((d["profit"] + d["commission"] + d["swap"] for d in deals), start=0.0)
        reasons: dict[str, int] = {}
        for deal in closes:
            reasons[deal["reason"]] = reasons.get(deal["reason"], 0) + 1
        return {
            "symbol": self.symbol,
            "days": days,
            "deals": deals,
            "summary": {
                "closed_trades": len(closes),
                "winning": sum(1 for d in closes if d["profit"] + d["commission"] + d["swap"] > 0),
                "net": round(net, 2),
                "how_trades_ended": reasons,
            },
            "note": (
                "net = profit + commission + swap over every deal of this strategy in the "
                "window. Positions that are still open are not included; see `book`."
            ),
        }

    # --- acting -----------------------------------------------------------

    def cancel(self, *, ticket: int | None = None, confirm: bool = False) -> dict[str, Any]:
        """Remove resting orders. ``ticket=None`` means every one of the strategy's."""
        return self._act(
            kind="cancel",
            rows=self._orders(),
            ticket=ticket,
            confirm=confirm,
            gate=self.order_gate,
            operation=self.broker.cancel_order,
            done="cancelled",
            describe=_order_row,
        )

    def close(self, *, ticket: int | None = None, confirm: bool = False) -> dict[str, Any]:
        """Close positions at market. ``ticket=None`` means every one of the strategy's."""
        return self._act(
            kind="close",
            rows=self._positions(),
            ticket=ticket,
            confirm=confirm,
            gate=self.close_gate,
            operation=self.broker.close_position,
            done="closed",
            describe=_position_row,
        )

    def flatten(self, *, confirm: bool = False) -> dict[str, Any]:
        """Cancel every order first, then close every position.

        Orders go first so a pending stop cannot fill into a new position between the two
        steps. The result carries both halves, and the book as it stands afterwards.
        """
        cancelled = self.cancel(confirm=confirm)
        closed = self.close(confirm=confirm)
        return {
            "preview": not confirm,
            "cancel": cancelled,
            "close": closed,
            "book_after": self.book() if confirm else None,
        }

    def _act(
        self,
        *,
        kind: str,
        rows: list[Any],
        ticket: int | None,
        confirm: bool,
        gate: Any,
        operation: Callable[[int], bool],
        done: str,
        describe: Callable[[Any], dict[str, Any]],
    ) -> dict[str, Any]:
        selected = rows
        if ticket is not None:
            selected = [row for row in rows if row.ticket == ticket]
            if not selected:
                known = ", ".join(str(r.ticket) for r in rows) or "none"
                raise OperatorUnavailable(
                    f"ticket {ticket} is not one of this strategy's {kind}able items "
                    f"(magic {self.settings.magic_number}, {self.symbol}); the strategy has: "
                    f"{known}. Trades opened by hand are never touched."
                )

        decision = gate.check(self.settings)
        report: dict[str, Any] = {
            "action": kind,
            "preview": not confirm,
            "permitted": decision.open,
            "matched": [describe(row) for row in selected],
            "results": [],
        }
        if not decision.open:
            report["why_not"] = decision.reason
        if not confirm:
            report["hint"] = (
                f"nothing was sent. Add --yes to {kind} the {len(selected)} item(s) above."
                if selected
                else f"nothing to {kind}."
            )
            return report
        if not decision.open:
            raise OperatorUnavailable(decision.reason)

        for row in selected:
            report["results"].append(self._one(row.ticket, operation, done))
        return report

    def _one(self, ticket: int, operation: Callable[[int], bool], done: str) -> dict[str, Any]:
        """One attempt on one ticket, and the outcome in words."""
        try:
            operation(ticket)
        except BrokerRejectedError as exc:
            text = str(exc)
            gone = "no open position" in text or "no open order" in text
            outcome = "already_gone" if gone else "refused"
            self.log(f"[{outcome}] {ticket}: {exc}")
            return {"ticket": ticket, "outcome": outcome, "detail": text}
        except ExecutionUnknownError as exc:
            self.log(f"[unknown] {ticket}: {exc}")
            return {
                "ticket": ticket,
                "outcome": "unknown",
                "detail": f"{exc} Run `book` to see what is actually on the account.",
            }
        except (BrokerError, BrokerNotConnectedError) as exc:
            self.log(f"[error] {ticket}: {exc}")
            return {"ticket": ticket, "outcome": "error", "detail": str(exc)}
        self.log(f"[{done}] {ticket}")
        return {"ticket": ticket, "outcome": done}


def _order_row(order: Any) -> dict[str, Any]:
    return {
        "ticket": order.ticket,
        "kind": str(order.kind),
        "volume": str(order.volume.lots),
        "entry": str(order.entry),
        "stop_loss": None if order.stop_loss is None else str(order.stop_loss),
        "take_profit": None if order.take_profit is None else str(order.take_profit),
    }


def _position_row(position: Any) -> dict[str, Any]:
    return {
        "ticket": position.ticket,
        "side": "BUY" if position.side is Side.SIDE_BUY else "SELL",
        "volume": str(position.volume.lots),
        "entry": str(position.entry),
        "stop_loss": None if position.stop_loss is None else str(position.stop_loss),
        "take_profit": None if position.take_profit is None else str(position.take_profit),
        "profit": str(position.profit.amount),
    }


def build_operator(
    config: AppConfig,
    *,
    module: MT5Module | None = None,
    log: Callable[[str], None] | None = None,
) -> OperatorService:
    """Attach to the terminal and refuse unless it is a confirmed demo account.

    :raises OperatorUnavailable: with the reason and the fix, for every refusal here.
    """
    settings = config.environment
    if settings.environment is not Environment.DEMO:
        raise OperatorUnavailable(
            f"SOS_ENVIRONMENT is {settings.environment}; these commands act on a demo account "
            "only. Set SOS_ENVIRONMENT=DEMO in .env."
        )
    if settings.allow_live:
        raise OperatorUnavailable(
            "SOS_ALLOW_LIVE is true. These commands refuse to run with live trading enabled "
            "anywhere in the configuration; set it to false."
        )

    shared = module if module is not None else MT5Module()
    feed = MT5Feed(shared)
    broker = MetaTrader5Broker(
        shared,
        settings,
        filling=config.strategy.order.filling_policy,
        deviation_points=config.strategy.order.deviation_points,
    )
    try:
        feed.connect(settings)
        broker.connect()
    except BrokerError as exc:
        raise OperatorUnavailable(f"cannot attach to the terminal: {exc}") from exc

    if not broker.account_is_confirmed_demo():
        broker.shutdown()
        raise OperatorUnavailable(
            "the terminal did not confirm this is a DEMO account (trade_mode). Sign the "
            "terminal in to a demo account and try again."
        )

    symbol = feed.resolve_symbol(config.strategy.symbol, config.strategy.symbol_aliases)
    return OperatorService(
        settings=settings,
        broker=broker,
        symbol=symbol,
        order_gate=DemoOrderGate(enabled=True, account_confirmed_demo=True),
        close_gate=DemoCloseGate(enabled=True, account_confirmed_demo=True),
        log=log if log is not None else _stderr,
    )


def _stderr(line: str) -> None:
    import sys

    sys.stderr.write(line + "\n")
    sys.stderr.flush()
