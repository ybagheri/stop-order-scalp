# Running It

The shortest path from "it is built" to "I saw it do something".

## The one command

```bash
python -m stop_order_scalp run --dry-run --max-cycles 30
```

No MetaTrader 5 needed. No account, no credentials, no money. It runs the whole pipeline —
strategy, risk sizing, the gate, the idempotency ledger, the simulated venue's bid/ask fills,
break-even, the trailing stop — and prints what it did on every cycle.

Start with:

```bash
python -m stop_order_scalp run --dry-run --max-cycles 20
```

## Reading the output

Each cycle reports what it decided, in order:

| `action` | Meaning |
| --- | --- |
| `placed` | a pending order went out; the detail names the ticket, entry, stop and target |
| `trailed` | the stop moved to follow price; the detail is read back from the broker |
| `break_even_armed` | price moved far enough in favour; the stop went to entry |
| `no_trade` | the strategy declined, and the detail says **why** in its own words |
| `hold` | nothing to do; a resting order or an open position is being managed |

A real excerpt from the synthetic series:

```
  1 00:15 placed   placed BUY_STOP ticket 1 at 40005.7 sl 39995.7 tp 40105.7
  6 00:20 trailed  trailed: stop now 39996.0, entry 40005.7, BUY 0.4 lots
 22 00:36 break_even_armed  break_even: stop now 40005.7, entry 40005.7
```

The summary at the end carries the balance, the open positions and the final state.

## What a dry run does **not** tell you

It proves the wiring, not the strategy. The default series is a synthetic trend, and a
result computed from it says nothing about whether this system makes money. The output says
so in its own `note` field.

Point it at real history before drawing any conclusion:

```bash
python -m stop_order_scalp run --dry-run --candles us30_m1.csv --m15 us30_m15.csv
```

The CSV is `time,open,high,low,close`, one row per candle, oldest first. `time` may be epoch
seconds, ISO-8601, or `YYYY-MM-DD HH:MM:SS` — a naive timestamp is read as UTC, never as
machine-local time. Rows are sorted by time, and blank lines are skipped.

Without `--m15`, the M15 direction series is **aggregated from the M1 file**. That is
deliberate: the direction filter refuses a timeframe mismatch rather than trying to
interpret M1 bars as M15, and feeding it the wrong timeframe and being *accepted* would be a
look-ahead-shaped bug.

## Changing the rule

Every number is in [`config/default.yaml`](../../config/default.yaml). Nothing below needs a
code change.

| What | Key | Baseline |
| --- | --- | --- |
| which candle decides direction | `entry.direction_timeframe` | `M15` |
| which candle places the entry | `entry.timeframe` | `M1` |
| how far beyond the M1 extreme | `entry.offset_points` | `10` |
| stop distance | `target.stop_loss_points` | `100` |
| take profit | `target.take_profit_points` | `1000` |
| position size | `risk.percent` | `0.5` (% of balance) |
| commission | `risk.commission_per_lot` | `6.0` |
| break-even trigger | `break_even.trigger_points` | `50` |
| trailing distance | `trailing.distance_points` | `100` |

Then check it:

```bash
python -m stop_order_scalp validate-config
```

An unknown key is an **error**, not a warning. A typo in a risk parameter that is silently
ignored is the most expensive kind of configuration bug there is.

## What is refused, and why

| Command | Result |
| --- | --- |
| `run --live` | exit 4, with the reason: every wire value and retcode here is still unverified against a real terminal |
| `run` in any other mode | runs against the simulator, never a broker |
| `status`, `diagnostics`, `backtest` | exit 4, naming the phase that has not landed |

`LIVE` needs three independent switches — `SOS_ENVIRONMENT=LIVE`, `SOS_ALLOW_LIVE=true` and
`SOS_ALLOW_ORDER=true` — and that is not decoration. It is a belt-and-braces refusal for a
system whose broker behaviour has not been observed once.
