"""The null-market test: what does this harness report on a market with no edge?

A random walk has, by construction, nothing for any rule to find. Whatever a backtest reports
on one is therefore *the harness's own bias plus the costs* -- and that is the number to hold
any real result against. A rule that "makes" +10 % on real data under a setting that makes
+30 % on pure noise has shown nothing.

Usage::

    python scripts/null_market.py                 # 20 sessions, sigma 6 points/minute
    python scripts/null_market.py --days 40 --seed 3

It prints one row per (commission, spread, price path). Nothing here touches a broker.
"""

from __future__ import annotations

import argparse
import math
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from stop_order_scalp.backtest.replay import replay
from stop_order_scalp.domain.models import Candle
from stop_order_scalp.domain.value_objects import Price
from stop_order_scalp.infrastructure.config import AppConfig, load_config
from stop_order_scalp.market_data.candles import aggregate

SESSION_MINUTES = 390


def random_walk(days: int, sigma: float, seed: int, start: float = 51200.0) -> list[Candle]:
    """M1 bars from a driftless walk with six sub-steps per bar, so highs and lows are real."""
    rng = random.Random(seed)
    price = start
    moment = datetime(2026, 9, 1, 13, 30, tzinfo=UTC)
    bars: list[Candle] = []
    for _ in range(days):
        for _ in range(SESSION_MINUTES):
            path = [price]
            for _ in range(6):
                path.append(path[-1] + rng.gauss(0, sigma / math.sqrt(6)))
            o, c = path[0], path[-1]
            bars.append(
                Candle(
                    open_time=moment,
                    open=Price(Decimal(f"{o:.1f}"), 1),
                    high=Price(Decimal(f"{max(path):.1f}"), 1),
                    low=Price(Decimal(f"{min(path):.1f}"), 1),
                    close=Price(Decimal(f"{c:.1f}"), 1),
                    timeframe_seconds=60,
                    timeframe="M1",
                    volume=Decimal(1),
                    tick_volume=1,
                    is_confirmed=True,
                )
            )
            price = c
            moment += timedelta(minutes=1)
        moment = (moment + timedelta(days=1)).replace(hour=13, minute=30)
    return bars


def with_options(config: AppConfig, *, commission: Decimal, refresh: bool) -> AppConfig:
    risk = replace(config.strategy.risk, commission_per_lot=commission)
    entry = replace(config.strategy.entry, refresh_pending=refresh)
    return replace(config, strategy=replace(config.strategy, risk=risk, entry=entry))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=20)
    parser.add_argument("--sigma", type=float, default=6.0, help="price points per minute")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--spreads", type=float, nargs="+", default=[1.0, 18.0])
    args = parser.parse_args()

    base = load_config()
    bars = random_walk(args.days, args.sigma, args.seed)
    m15 = aggregate(bars, "M1", "M15")
    configured = base.strategy.risk.commission_per_lot
    print(f"{len(bars)} bars, no edge by construction. Net result on 10 000 starting balance.\n")
    print(f"{'commission':>10} {'spread':>7} {'path':>6} {'trades':>7} {'win':>6} {'net':>10} {'PF':>6}")
    for commission in (Decimal(0), configured):
        config = with_options(base, commission=commission, refresh=True)
        for spread in args.spreads:
            for path in ("close", "auto", "ohlc", "olhc"):
                result = replay(config, bars, m15_candles=m15, spread_points=spread, intrabar=path)
                stats = result.statistics.to_dict()
                trades = stats["trades"]
                net = Decimal(stats["balance_change"])
                profit_factor = stats["money"].get("profit_factor")
                print(
                    f"{commission!s:>10} {spread:>7} {path:>6} {trades['closed']:>7} "
                    f"{trades['win_rate'] or '-':>6} {net:>10.1f} {profit_factor or '-':>6}"
                )
        print()


if __name__ == "__main__":
    main()
