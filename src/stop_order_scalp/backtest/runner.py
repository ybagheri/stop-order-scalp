"""The ``backtest`` command: read candles, replay them, report what happened.

Kept separate from :mod:`stop_order_scalp.backtest.replay` so that the replay engine takes
plain values -- candles in, a result out -- and knows nothing about argument parsing. The two
are separated so the engine can be driven from a test with a list of candles and no CLI
machinery in the way, which is how most of its tests run.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from stop_order_scalp.backtest.replay import replay
from stop_order_scalp.domain.exceptions import ConfigError
from stop_order_scalp.infrastructure.config import AppConfig
from stop_order_scalp.market_data.candles import aggregate, load_candles_csv
from stop_order_scalp.market_data.symbols import us30_specification

__all__ = ["run_backtest_from_args"]


def run_backtest_from_args(args: argparse.Namespace, config: AppConfig) -> dict[str, Any]:
    """Replay the requested candles and return the report as plain data."""
    if getattr(args, "symbol", None):
        config = _with_symbol(config, str(args.symbol))

    data = getattr(args, "data", None)
    timeframe = config.strategy.entry.timeframe
    if data is None:
        raise ConfigError(
            "backtest needs --data PATH. A replay without a file has nothing to say about a "
            "market, and this command will not generate candles and report the result as if "
            "it had. `run --dry-run` with no --candles is the command that proves the wiring."
        )

    path = Path(data)
    if not path.exists():
        raise ConfigError(f"no candle file at {path}")

    # Digits come from the *measured* specification for the symbol being replayed, not from a
    # constant. Replaying A Markets data while parsing at Alpari's precision would silently
    # round every price, and replaying a whole-number instrument at one decimal would invent
    # digits that are not there. `--symbol` selects which measurement applies.
    digits = us30_specification(config.strategy.symbol).digits
    candles = load_candles_csv(path, timeframe=timeframe, digits=digits)
    if not candles:
        raise ConfigError(f"{path} contained no usable rows")

    m15 = aggregate(candles, timeframe, config.strategy.entry.direction_timeframe)
    if not m15:
        raise ConfigError(
            f"{path} is too short to build even one "
            f"{config.strategy.entry.direction_timeframe} candle from "
            f"{timeframe} bars. The direction filter needs a closed higher-timeframe bar "
            "before it will read anything."
        )

    start = _parse_bound(getattr(args, "start", None), "--from")
    end = _parse_bound(getattr(args, "end", None), "--to")
    if start is not None or end is not None:
        candles = _clip(candles, start, end)
        m15 = aggregate(candles, timeframe, config.strategy.entry.direction_timeframe)
        if not candles or not m15:
            raise ConfigError("the --from/--to window contains no complete candle window")

    slippage = _slippage_points(getattr(args, "slippage_points", 0.0))
    spread = _spread_points(getattr(args, "spread_points", None))

    result = replay(
        config,
        candles,
        m15_candles=m15,
        source=path,
        max_cycles=getattr(args, "max_cycles", None),
        slippage_points=slippage,
        spread_points=spread,
        intrabar=str(getattr(args, "intrabar", "close") or "close"),
    )
    report = result.to_dict()
    report["window"] = {
        "from": candles[0].open_time.isoformat() if candles else None,
        "to": candles[-1].open_time.isoformat() if candles else None,
        "slippage_points": str(slippage),
        "spread_points": "1 (legacy default)" if spread is None else str(spread),
        "intrabar": str(getattr(args, "intrabar", "close") or "close"),
        "direction_timeframe": config.strategy.entry.direction_timeframe,
    }

    output = getattr(args, "output", None)
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        report["written_to"] = str(output)

    return report


def _parse_bound(value: Any, flag: str) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError(f"{flag} {value!r} is not an ISO-8601 timestamp: {exc}") from exc
    # A naive bound is read as UTC. Machine-local time would silently shift the window, and a
    # shifted window is a backtest of a different period than the one that was asked for.
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _clip(candles: list[Any], start: datetime | None, end: datetime | None) -> list[Any]:
    """Keep the bars inside ``[start, end]``, by open time, inclusive."""
    kept = []
    for candle in candles:
        if start is not None and candle.open_time < start:
            continue
        if end is not None and candle.open_time > end:
            continue
        kept.append(candle)
    return kept


def _slippage_points(value: Any) -> Decimal:
    if value is None:
        return Decimal(0)
    points = Decimal(str(value))
    if points < 0:
        raise ConfigError(
            f"--slippage-points cannot be negative (got {points}). Slippage in this project "
            "means adverse: it makes a fill worse than the level, never better. A negative "
            "value would be a quiet way to improve every result."
        )
    return points


def _spread_points(value: Any) -> Decimal | None:
    if value is None:
        return None
    points = Decimal(str(value))
    if points < 0:
        raise ConfigError(f"--spread-points cannot be negative (got {points})")
    return points


def _with_symbol(config: AppConfig, symbol: str) -> AppConfig:
    from dataclasses import replace

    return replace(config, strategy=replace(config.strategy, symbol=symbol))
