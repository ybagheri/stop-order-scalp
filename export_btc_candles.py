"""Export closed M1 candles of BITCOIN from the MT5 terminal to btc_m1.csv.

The terminal must be open and signed in. No login or password is used or needed.
"""

import csv
import datetime as dt

import MetaTrader5 as mt5

SYMBOL = "BITCOIN"
BARS = 20000  # about two weeks of a 24-hour market

if not mt5.initialize():
    raise SystemExit(f"cannot attach to the terminal: {mt5.last_error()}")
mt5.symbol_select(SYMBOL, True)
rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M1, 0, BARS)
if rates is None or len(rates) < 2:
    raise SystemExit(f"no candles for {SYMBOL}: {mt5.last_error()}")
rates = rates[:-1]  # the last bar is still forming

with open("btc_m1.csv", "w", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerow(["time", "open", "high", "low", "close"])
    for row in rates:
        moment = dt.datetime.fromtimestamp(int(row["time"]), dt.UTC)
        writer.writerow(
            [moment.strftime("%Y-%m-%dT%H:%M:%SZ"), row["open"], row["high"], row["low"], row["close"]]
        )

info = mt5.symbol_info(SYMBOL)
print(f"wrote {len(rates)} bars to btc_m1.csv")
print(f"current spread: {info.spread} points (= {info.spread * info.point:.2f} in price)")
print(f"last close: {rates[-1]['close']}")
mt5.shutdown()
