# Opening and closing trades on a demo account

This is the guide for the person at the keyboard. It answers four questions: **how does a trade
open, who manages it, how does it close, and what do I type if I want it to stop.**

Persian version: [`TRADING_GUIDE.fa.md`](TRADING_GUIDE.fa.md). Setup and first run:
[`DEMO.md`](DEMO.md).

> **Demo accounts only.** Everything here refuses a live account, four independent ways.
> Nothing in this project has traded real money.

## 1. The life of one trade

```
 new M1 candle closes
        |
        v
  strategy decides  --- no signal ---> nothing happens, wait for the next candle
        |
   BUY or SELL signal
        v
  size = 0.5 % of balance / (stop distance + commission), rounded DOWN
        |
        v
  PENDING STOP ORDER placed a little beyond the candle's high (buy) or low (sell)
  with a stop-loss and a take-profit attached              <- you see it in the Trade tab
        |
        +-- price never reaches it ----> with entry.refresh_pending: true a newer candle
        |                                 REPLACES it; otherwise it waits where it was placed.
        |                                 Either way it is cancelled when the run ends
        v
  price crosses the entry  ==>  the order FILLS and becomes a POSITION
        |
        v
  while `run` is alive:  break-even, then trailing stop, move the stop-loss only
        |
        v
  the position CLOSES, by whichever comes first:
     - take-profit reached               (reason: take_profit)
     - stop-loss reached, incl. a trailed one   (reason: stop_loss)
     - you run `close` or `flatten`      (reason: program)
     - you close it by hand in the terminal     (reason: manual)
```

**You do not open trades by hand.** The strategy opens them, one pending order at a time, when
`run --demo --place-orders` is running. There is deliberately no `buy` command: a manual market
order would bypass the sizing, the ledger and the gates. If you want a hand-made trade, make it
in the terminal; this project will ignore it (see *Only this strategy's trades*, below).

**Who manages an open position?** Only a *running* `run --demo --place-orders`. Break-even and
trailing happen inside that process. If you stop it, **the position stays open** with whatever
stop-loss and take-profit it had at that moment, and nothing moves them any more. The report says
so. To end a test cleanly, `close` the position (section 4) before or after stopping.

## 2. Open trades: the three steps

Do them in order. Each one adds a way to lose a (demo) dollar, so do not skip ahead.

| Step | Command | What can happen |
| --- | --- | --- |
| 0. Check the terminal | `python scripts/preflight_mt5.py --symbol BITCOIN` | nothing sent |
| 1. Observe | `python -m stop_order_scalp run --demo --duration 900` | nothing sent; prints what it *would* place |
| 2. One real order | `python -m stop_order_scalp run --demo --place-orders --max-orders 1 --duration 300` | **one pending order** on the demo account |
| 3. Let it run | `... --place-orders --max-orders 3 --duration 1800` | up to three orders; fills are managed |

`.env` for step 2 onwards: `SOS_ENVIRONMENT=DEMO`, `SOS_ALLOW_ORDER=true`, `SOS_ALLOW_LIVE=false`,
and `SOS_MT5_LOGIN` / `SOS_MT5_PASSWORD` **empty** (the terminal must already be signed in).

Four conditions must all hold for an order to be sent; if any one fails, nothing is sent and the
message names it: `--place-orders`, `SOS_ENVIRONMENT=DEMO`, `SOS_ALLOW_ORDER=true`, and the
terminal itself reporting a demo account.

`--max-orders N` counts orders **placed**. With `refresh_pending: true` a replaced stop counts as
a new order, so for a run meant to last use `--max-orders 3` or more.

## 3. See what is happening

| I want to know... | Command |
| --- | --- |
| what the strategy has on the account right now | `python -m stop_order_scalp book` |
| how closed trades ended, and the net result | `python -m stop_order_scalp history --days 7` |
| what the strategy decided and sent | `python -m stop_order_scalp journal` |
| whether the terminal is reachable | `python -m stop_order_scalp test-connection` |

All four only read.

`book` lists the **resting pending orders** and **open positions** carrying this strategy's magic
number, with the current bid/ask, each position's profit and the total floating profit.

`history` lists every deal of this strategy and sums them. `net` is profit **plus commission plus
swap**, so it includes what the trades cost. `how_trades_ended` counts `take_profit`, `stop_loss`,
`program` (a close command) and `manual`. A position that has closed is no longer in `book`; this
is the only place its result is.

`journal` is the ledger of what the strategy *decided and sent*. It is not profit and loss.

## 4. Close trades

Every action command is a **preview unless you add `--yes`**. A preview sends nothing and tells
you what it would do and whether it is permitted.

| I want to... | Preview | Do it |
| --- | --- | --- |
| remove resting orders (not yet filled) | `cancel` | `cancel --yes` |
| remove one order | `cancel --ticket 123456` | `cancel --ticket 123456 --yes` |
| close open positions at market | `close` | `close --yes` |
| close one position | `close --ticket 123456` | `close --ticket 123456 --yes` |
| **emergency: cancel every order, then close every position** | `flatten` | `flatten --yes` |

Prefix each with `python -m stop_order_scalp`.

`close` needs its own switch: **`SOS_ALLOW_CLOSE=true`** in `.env`. `cancel` needs
`SOS_ALLOW_ORDER=true`. They are separate on purpose; being happy to let the strategy open trades
is not agreement to let a command close them.

`flatten` cancels orders **first**, so a pending stop cannot fill into a fresh position between
the two steps, then closes whatever is open, then prints the book as it stands.

Example, closing everything after a test:

```bash
python -m stop_order_scalp flatten            # look at what it would do
python -m stop_order_scalp flatten --yes      # do it
python -m stop_order_scalp book               # confirm nothing is left
```

### Only this strategy's trades

Orders and positions are selected by the strategy's **magic number** and symbol. A trade you
opened by hand in the terminal has a different magic number and is never listed, never cancelled,
never closed. Asking for such a ticket by number is refused by name:

```
ticket 777 is not one of this strategy's closeable items (magic 20260930, BITCOIN);
the strategy has: 123, 124. Trades opened by hand are never touched.
```

### What the outcomes mean

| `outcome` | Meaning | What to do |
| --- | --- | --- |
| `cancelled` / `closed` | done | nothing |
| `already_gone` | it closed or filled between your look and your command (a stop-loss, say) | run `book` |
| `refused` | the terminal said no, and the reason is in `detail` | read it; nothing changed |
| `unknown` | the terminal could not say whether it went through | run `book`, **do not repeat the command blindly** |
| `error` | the terminal was unreachable | check the terminal, then `book` |

A command never retries. One attempt per ticket, because a close that "failed" but actually
happened, repeated, would act on whatever is there next.

## 5. Stopping `run`

Press **Ctrl-C**. It always runs its shutdown:

- a **resting order is cancelled** (add `--keep-orders` to leave it);
- an **open position is left** with its current stop-loss and take-profit, and the report warns
  that nothing is trailing it any more.

`run` also ends on its own when: `--duration` elapses, `--max-cycles` polls are done, the order
limit is reached and the account is flat (`test_complete`), three orders have been refused
(`rejected_repeatedly`), or an order's outcome is unknown (`execution_unknown`; open the Trade tab
before doing anything else).

After `cancel`, `close` or a stop-loss, the next `run --demo` start **reconciles** its ledger with
the account, so there is nothing for you to repair by hand.

## 6. When something goes wrong

| You see | Likely cause | Fix |
| --- | --- | --- |
| `[rejected] ... 10027 ... Algo Trading` | the terminal's *Algo Trading* button is off | press it; it must be green |
| `[rejected] ... 10030` | the symbol does not take that filling mode | set `order.filling_policy` to `IOC` or `FOK` |
| `[rejected] ... 10015 Invalid price` | price moved past the entry before the order was sent | normal; the next candle tries again |
| `[rejected] ... 10018` | market closed | wait, or trade a 24-hour symbol |
| `Invalid "comment" argument` | an order comment over the terminal's limit | already handled (limit 24); report it if it recurs |
| `[waiting] no newly closed M1 candle` | the market is closed or the terminal has not published the bar | nothing to fix |
| `risk_refused: ... below the broker minimum` | 0.5 % of the balance buys less than the minimum lot | raise `percent`, use a smaller stop, or `fixed_lot` |
| `[execution_unknown]` | the terminal could not say whether the order went out | **open the Trade tab**, then `book` |
| `the terminal did not confirm this is a DEMO account` | the terminal is signed in to a real account | sign in to a demo account |
| `SOS_ALLOW_CLOSE is false` | closing has its own switch | set `SOS_ALLOW_CLOSE=true` in `.env` |

Two diagnostic scripts send nothing and ask the terminal directly:

```bash
python scripts/preflight_mt5.py --symbol BITCOIN        # the package, the switches, the symbol
python scripts/diagnose_order.py --symbol BITCOIN --matrix   # would it accept this order, and why not
```

## 7. How big is the trade?

`risk.percent` of the balance, divided by what one lot loses if the stop is hit (stop distance
plus commission), **rounded down** to the lot step:

```
lots = (balance x percent) / (stop_loss_in_money_per_lot + commission_per_lot)
```

If the result is under the broker's minimum lot, the trade is **refused**, not rounded up, because
rounding up would risk more than the budget. For a fixed size, set `risk.mode: fixed_lot` and
`risk.fixed_lot`.

Example: balance 3 117.44, 0.5 % = 15.59; a 10 000-point stop on a symbol worth 0.01 per point
per lot = 100 per lot; no commission: 15.59 / 100 = 0.156, so **0.15 lots**. A wider stop makes
the trade smaller and keeps the risk the same.

## 8. What is still unproven

Be honest with yourself about what a demo test has and has not shown.

- A pending order is placed, validated by the terminal (`order_check`) and cancelled: shown only
  once you have done it on your terminal. Until then it is checked against the published API and
  a test double, not the real thing.
- Fill, break-even, trailing and close on a **real** terminal have not been observed by this
  project at the time of writing. Treat the first of each as an experiment and watch the Trade tab.
- Nothing here says the **strategy** makes money. The backtest harness cannot separate a real
  edge from the order of high and low inside a candle, and costs are large against a 10-point
  stop. See [`docs/backtest/README.md`](../backtest/README.md) before believing any number.
