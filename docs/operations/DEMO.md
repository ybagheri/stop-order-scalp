# Trading on a demo account

> Looking for how to **close** a trade, cancel an order, or read the result? That is
> [`TRADING_GUIDE.md`](TRADING_GUIDE.md) (Persian: [`TRADING_GUIDE.fa.md`](TRADING_GUIDE.fa.md)).
> This page is the setup and the first run.

`run --dry-run` proves the pipeline against a simulator. This is the step after it: the same
pipeline reading the **real market** from your MetaTrader 5 terminal and, only if you ask,
sending **real orders to a demo account**.

**Nothing in this project had ever placed an order before this.** Everything here was checked
against the published MetaTrader5 reference and a test double that exposes only functions the
real package has -- which is a different thing from having worked. That is why the procedure
below starts by sending nothing.

## What has to be true

| Requirement | Why |
| --- | --- |
| Windows, the MT5 terminal **open and signed in to a demo account** | the `MetaTrader5` package talks to a running terminal |
| `pip install -e ".[mt5]"` | the package |
| `SOS_ENVIRONMENT=DEMO` in `.env` | the run refuses anything else |
| `SOS_MT5_LOGIN` and `SOS_MT5_PASSWORD` **empty** | the run attaches to the terminal's own session. Logging in from here is how three demo accounts were locked |
| the symbol is in `market_data/symbols.py` | a contract nobody measured is refused, and so is one that differs from the measurement |
| `SOS_ALLOW_LIVE=false` | a demo run refuses to start if live trading is enabled anywhere |

## Step 0 -- preflight (sends nothing)

```bash
python scripts/preflight_mt5.py --symbol US30
```

It asks the **actual package** whether the functions and constants the broker module uses
exist and have the expected values, whether the *Algo Trading* button is on, whether the account
is a demo account, and which filling modes the symbol allows. Fix every `PROBLEM` line first.

## Step 1 -- observe (sends nothing)

```bash
python -m stop_order_scalp run --demo --duration 900
```

It reads real candles, decides once per closed M1 candle, and prints what it **would** place:

```
14:07:01 [would_place] BUY STOP 0.5 lots, entry 100124.50, stop 100024.50, target 100424.50
```

Check that number against the terminal. Is the entry just above the last M1 high? Is the lot size
what 0.5 % of the balance means? If the market is closed it prints `[waiting]` and nothing else,
which is correct.

## Step 2 -- one order

In `.env`: `SOS_ALLOW_ORDER=true`. (Closing a position by command is a separate switch,
`SOS_ALLOW_CLOSE`; see the trading guide. Leave it `false` until you need it.) Then:

```bash
python -m stop_order_scalp run --demo --place-orders --max-orders 1 --duration 300
```

Four things must all hold or no order is sent -- the flag, `SOS_ENVIRONMENT=DEMO`,
`SOS_ALLOW_ORDER=true`, and the terminal itself confirming the account is a demo account.
Look in the terminal's *Trade* tab: there should be one pending stop order with a stop and a
target. When the run ends it removes a resting order (use `--keep-orders` to leave it).

If it is refused, the message names the cause:

| Retcode | Meaning | Fix |
| --- | --- | --- |
| 10027 | Algo Trading is off in the terminal | press the *Algo Trading* button; it must be green |
| 10030 | the symbol does not accept the filling mode | set `order.filling_policy` to `IOC` or `FOK` |
| 10018 | market closed | wait, or use a 24-hour symbol |
| 10016 | stop too close to the price | widen `entry.offset_points` / `target.stop_loss_points` |
| 10014 | invalid volume | the lot size does not fit the symbol's min/step |

## Step 3 -- let it manage a trade

`--max-orders 3` lets the run replace a stale stop as new candles close (it needs
`entry.refresh_pending: true`, otherwise the first order sits where it was placed). When an order
fills, break-even and trailing move the stop; the take-profit is left alone.

## What it will not do

* trade a live account -- `LIVE` is refused outright by the demo gate, and the terminal's own
  `trade_mode` must say demo;
* send more than `--max-orders` orders;
* trail a position after the run ends -- positions keep their stop and target, and the report
  warns that nothing is managing them any more.

## Read the ledger

The demo ledger is `state/state-demo.json`, separate from the simulator's and from the live one.
`python -m stop_order_scalp journal` reads the ledger of the configured environment (it used to
read the dry-run ledger whatever the environment was, so it showed nothing for a demo run). It
records what the strategy decided and sent, not profit and loss: for results use `history`.
