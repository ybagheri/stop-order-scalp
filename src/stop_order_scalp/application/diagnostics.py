"""`status` and `diagnostics`: what the system is doing, and what it is about to do.

Two commands with different jobs, and the difference matters.

**`diagnostics`** answers *"can this system run here?"* It resolves configuration, attempts a
read-only connection, and reports what it found. It never trades, never resumes, and never
reconciles -- it opens no orders even in ``VERIFYING``, because its whole purpose is to be safe
to run on a machine that is not ready.

**`status`** answers *"what is it doing right now?"* It runs the lifecycle's recovery pass, so
it reports the state the system would resume into, and any open positions or working orders
the venue holds. It is the command to look at before pressing go on a demo account.

Why they are separate
---------------------
Because the safe one is the one people reach for when something is wrong, and the one that
can change state is the one people reach for when they are about to trade. Merging them would
mean a diagnostic accidentally reconciles an order. They are two commands because "is this
machine ready" and "what is my account doing" are different questions with different risks.
"""

from __future__ import annotations

from typing import Any

from stop_order_scalp.domain.enums import Environment
from stop_order_scalp.infrastructure.config import AppConfig
from stop_order_scalp.market_data.symbols import MEASURED, MEASURED_FROM, MEASURED_ON

__all__ = ["build_diagnostics"]


def build_diagnostics(config: AppConfig) -> dict[str, Any]:
    """Everything needed to judge whether this machine can trade, in one JSON object.

    Read-only. It connects if it can, reports what came back, and never sends anything.
    """
    environment = config.environment

    report: dict[str, Any] = {
        "environment": {
            "environment": str(environment.environment),
            "allow_live": environment.allow_live,
            "allow_order": environment.allow_order,
            "allow_close": environment.allow_close,
            "live_reachable": (
                environment.environment is Environment.LIVE
                and environment.allow_live
                and environment.allow_order
            ),
        },
        "instrument": {
            "symbol": config.strategy.symbol,
            "aliases": list(config.strategy.symbol_aliases),
        },
        "rule": {
            "direction_timeframe": config.strategy.entry.direction_timeframe,
            "entry_timeframe": config.strategy.entry.timeframe,
            "offset_points": config.strategy.entry.offset_points,
            "lookback": config.strategy.entry.lookback,
            "stop_loss_points": config.strategy.target.stop_loss_points,
            "take_profit_points": config.strategy.target.take_profit_points,
            "break_even_trigger_points": config.strategy.break_even.trigger_points,
            "trailing_distance_points": config.strategy.trailing.distance_points,
        },
        "risk": {
            "mode": str(config.strategy.risk.mode),
            "percent": str(config.strategy.risk.percent),
            "commission_per_lot": str(config.strategy.risk.commission_per_lot),
            "commission_mode": str(config.strategy.risk.commission_mode),
            "refuse_below_min_volume": config.strategy.risk.refuse_below_min_volume,
        },
        "specification": {
            "source": f"measured {MEASURED_ON} from {MEASURED_FROM}",
            "values": MEASURED.as_dict(),
            "caveat": (
                "Broker values, not universal truth. A different account or broker would "
                "differ, and a CFD's tick value can move with the underlying index. Re-measure "
                "rather than edit this if the numbers stop matching the terminal."
            ),
        },
        "paths": {
            "config": str(config.paths.config_file),
            "state": str(config.paths.state_file),
            "log_directory": str(config.paths.log_directory),
        },
        "connection": _connection(config),
    }
    return report


def _connection(config: AppConfig) -> dict[str, Any]:
    """Attach to the terminal, read what it will tell us, and let go.

    A failure here is data, not an error. ``diagnostics`` reporting "the terminal refused"
    *is* the answer to the question it was asked.
    """
    from stop_order_scalp.domain.exceptions import TerminalError
    from stop_order_scalp.market_data.mt5_feed import MT5Feed

    settings = config.environment
    result: dict[str, Any] = {
        "terminal_path_configured": bool(settings.mt5_path),
        "account_login_configured": bool(settings.mt5_login),
        "account_server_configured": bool(settings.mt5_server),
        "connected": False,
        "password": (
            "not reported and not readable here. EnvironmentSettings carries no password "
            "field by design -- a secret reduced to a presence flag at load time, which no "
            "downstream code can read. An already-signed-in terminal needs none."
        ),
    }
    if not settings.mt5_path:
        result["error"] = "SOS_MT5_PATH is not set, so no terminal can be started"
        return result

    feed = MT5Feed()
    try:
        feed.connect(settings)
    except TerminalError as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    try:
        result["connected"] = True
        account = feed.account()
        result["account"] = {
            "login": account.login,
            "server": account.server,
            "currency": account.currency,
            "balance": str(account.balance.amount),
            "equity": str(account.equity.amount),
            "leverage": account.leverage,
            "is_demo": account.is_demo,
            "trade_allowed": account.trade_allowed,
        }
        result["server_time"] = feed.server_time().isoformat()
        symbol = feed.resolve_symbol(
            config.strategy.symbol, config.strategy.symbol_aliases
        )
        result["symbol_resolved_to"] = symbol
        result["symbol_available"] = feed.symbol_available(symbol)
        live = feed.specification(symbol)
        result["specification_from_terminal"] = {
            "point": str(live.point),
            "tick_size": str(live.tick_size),
            "tick_value": str(live.tick_value),
            "contract_size": str(live.contract_size),
            "volume_min": str(live.volume_min),
            "volume_max": str(live.volume_max),
            "volume_step": str(live.volume_step),
            "stops_level": live.stops_level,
        }
        result["specification_matches_recorded"] = _matches(live)
        if not result["specification_matches_recorded"]:
            result["specification_warning"] = (
                "The terminal's values differ from the ones recorded in "
                "market_data/symbols.py. Every money figure scales with the tick value, so this "
                "is worth resolving before trusting any result."
            )
        tick = feed.tick(symbol)
        if tick is not None:
            result["tick"] = {
                "bid": str(tick.bid.value),
                "ask": str(tick.ask.value),
                "at": tick.moment.isoformat(),
            }
    except TerminalError as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        feed.shutdown()
    return result


def _matches(live: Any) -> bool:
    """Whether the terminal still agrees with the recorded specification.

    The point of re-reading it on every `diagnostics` run: a CFD's tick value can change with
    the underlying index, and a stale recorded value would silently keep every money figure
    wrong. A mismatch is a warning, not a failure -- the account is still usable, but the
    numbers are not.
    """
    agrees: bool = (
        live.point == MEASURED.point
        and live.tick_size == MEASURED.tick_size
        and live.tick_value == MEASURED.tick_value
        and live.contract_size == MEASURED.contract_size
    )
    return agrees
