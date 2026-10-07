# Stop Order Scalp

[نسخه فارسی](README.fa.md)

**US30 stop-order scalping system — M15 direction filter, M1 stop entries, MT5 execution.**

> **Status: under active development.** See [`ROADMAP.md`](ROADMAP.md) for what exists
> today and [`HANDOFF.md`](HANDOFF.md) for the state of the last completed phase.
>
> **Phases 1–7 complete, and it runs.** The domain layer, configuration, logging, the
> MetaTrader 5 feed, the closed-candle freeze, the M15/M1 baseline decision, the position
> sizer, the idempotent order manager, the monotonic trailing stop and the write-intent
> lifecycle, and the backtester are implemented, **wired together**, and tested — **1178
> tests, no broker required**. `run --dry-run` executes the whole pipeline against a
> simulated venue and prints what it did on every cycle; `backtest --data PATH` replays
> recorded candles through that same code and reports the distribution of outcomes.
>
> **All seven CLI commands are built**, and the symbol specification is **measured** from a
> real Alpari MT5 terminal rather than assumed — which mattered more than anything else in
> the project so far. `diagnostics` reports what it found, including whether the terminal
> still agrees with the recorded values.
>
> **On 30 000 bars of real US30 data the rule loses money**: 9 trades, −219.82 on 10 000,
> profit factor 0.21, and commission nearly three times the gross profit. That is a real
> result and it is negative — see [`docs/backtest/README.md`](docs/backtest/README.md).
> Nothing here is a validated edge.
>
> **The rule is configuration, not code.** Direction timeframe, entry offset, stop, target,
> risk percentage and commission all live in
> [`config/default.yaml`](config/default.yaml) — change them there, not in the code.
>
> **No profitability is claimed.** See [`docs/strategy/BASELINE.md`](docs/strategy/BASELINE.md).
>
> **Risk warning.** Automated trading carries substantial risk of loss. Start in
> `DRY_RUN`, then `PAPER`, then a demo account. `LIVE` is not implemented and is gated
> three times over; read [`docs/operations/`](docs/operations/) before going anywhere near
> it.
---

## Purpose

A modular, testable, object-oriented trading application that implements exactly one
strategy on one instrument:

* Trade **US30 only**.
* The **15-minute candle** decides the allowed direction.
* A **1-minute candle's high/low** places a **pending stop order**, 10 points beyond it.
* Size positions from **0.5 % of account balance**, commission-aware.
* **Break-even** at a configurable trigger, then a **100-point trailing stop** that never
  moves backwards.
* **Immediately** place the next pending order when a position closes.

Everything about the strategy lives in configuration. Nothing about MetaTrader 5 leaks
into the strategy, risk, or lifecycle code.

## Documentation map

| Document | Purpose |
| --- | --- |
| [`ROADMAP.md`](ROADMAP.md) | Phased plan and per-phase status |
| [`HANDOFF.md`](HANDOFF.md) | Mandatory state file — read this first |
| [`CHANGELOG.md`](CHANGELOG.md) | Release-style change log |
| [`docs/architecture/`](docs/architecture/) | Architecture, decisions, audit |
| [`docs/strategy/BASELINE.md`](docs/strategy/BASELINE.md) | The exact strategy rules |
| [`docs/risk/`](docs/risk/) | Sizing, commission, SL/TP semantics |
| [`docs/execution/`](docs/execution/) | Order identity, idempotency, error classification |
| [`docs/trailing/`](docs/trailing/) | Break-even, the trailing rule, monotonicity |
| [`docs/lifecycle/`](docs/lifecycle/) | Write-intent ledger, transition table, restart recovery |
| [`docs/mt5/`](docs/mt5/) | MT5 setup, symbol specifications, dry-run |
| [`docs/testing/`](docs/testing/) | Test layout, property tests, how to run |
| [`docs/operations/`](docs/operations/) | Deployment, troubleshooting, recovery |
| [`docs/research/`](docs/research/) | Optional, disabled-by-default research layer |

## Quick start

```bash
git clone git@github.com:ybagheri/stop-order-scalp.git
cd stop-order-scalp
python -m venv .venv
# Windows: .venv\Scripts\activate   |   Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # Windows: copy .env.example .env
python -m stop_order_scalp validate-config
python -m stop_order_scalp run --dry-run --max-cycles 30
```

Read [`docs/operations/RUNNING.md`](docs/operations/RUNNING.md) for how to interpret that
output. Repeating the identical command gives the identical result — see
[Repeating a run](docs/operations/RUNNING.md#repeating-a-run) for why that needed fixing.

To replay recorded candles instead:

```bash
python -m stop_order_scalp backtest --data tests/fixtures/us30_m1_sample.csv
```

A synthetic sample file is committed so the command runs with no setup; the file says in its
own first two lines that it is invented. Read
[`docs/backtest/README.md`](docs/backtest/README.md) before quoting any number it prints —
in particular, that the symbol specification is a guess that every money figure scales with.

## CLI

```bash
python -m stop_order_scalp run                # live trading (refuses unless explicitly enabled)
python -m stop_order_scalp run --dry-run      # full pipeline, zero broker writes
python -m stop_order_scalp status             # connection, account, symbol, state, risk
python -m stop_order_scalp validate-config    # validate config/default.yaml + .env
python -m stop_order_scalp backtest --data PATH   # replay candles, report the distribution
python -m stop_order_scalp test-connection    # MT5 reachability, read-only
python -m stop_order_scalp journal            # trade journal
python -m stop_order_scalp diagnostics        # environment + config bundle
```

On a **demo account** (terminal signed in, `SOS_ENVIRONMENT=DEMO`):

```bash
python -m stop_order_scalp run --demo                      # real market, observe only
python -m stop_order_scalp run --demo --place-orders       # real orders, demo account only
python -m stop_order_scalp book                            # what the strategy has on the account
python -m stop_order_scalp history --days 7                # how closed trades ended, net result
python -m stop_order_scalp cancel [--ticket N] [--yes]     # remove resting orders (preview without --yes)
python -m stop_order_scalp close  [--ticket N] [--yes]     # close positions at market
python -m stop_order_scalp flatten [--yes]                 # cancel everything, then close everything
```

How a trade opens and closes, and what to type to stop one:
[`docs/operations/TRADING_GUIDE.md`](docs/operations/TRADING_GUIDE.md) ·
[فارسی](docs/operations/TRADING_GUIDE.fa.md).

Execution modes (`SOS_ENVIRONMENT`): `DRY_RUN` (default) → `PAPER` → `DEMO` → `LIVE`.
Each account-changing operation has its **own** opt-in switch, and `LIVE` additionally
requires `SOS_ALLOW_LIVE=true`. There is no path that reaches live trading by accident.

### Exit codes

Part of the contract, because a supervisor process depends on them.

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Ran and failed (broker refused, order rejected) |
| 2 | Refused before doing anything (bad config, safety gate) |
| 3 | Not connected to MetaTrader 5 |
| 4 | The command exists, but the phase implementing it has not been built yet |

## Development

```bash
python -m pytest                             # 1178 tests
python -m ruff check .                       # lint
python -m mypy                               # types, strict, src + tests
python scripts/check_architecture.py         # architecture boundaries
```

`pytest` covers all four gates, but run them individually while working so the output is
legible. See [`docs/testing/`](docs/testing/) and [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Configuration

Strategy parameters: [`config/default.yaml`](config/default.yaml).
Machine-specific values (MT5 path, login, server, magic number, mode): `.env`
(see [`.env.example`](.env.example)).

```yaml
symbol: US30
symbol_aliases: [US30, US30.cash, US30m, DJ30]   # exact, case-insensitive; no fuzzy match

entry:
  timeframe: M1
  direction_timeframe: M15
  offset_points: 10                            # BUY STOP = high+10, SELL STOP = low-10
  candle_selection: last_closed                # never the forming candle: no look-ahead

risk:
  mode: percent_balance                         # or: fixed_lot
  percent: 0.5
  fixed_lot: 0.10
  commission_per_lot: 6.0
  commission_mode: per_lot_round_trip

target:
  mode: fixed_points                            # or: risk_reward
  take_profit_points: 1000
  risk_reward: 1.0
  stop_loss_points: 100                         # distance from entry when a mode needs an SL

break_even:
  enabled: true
  trigger_points: 50
  mode: entry                                   # or: commission_aware

trailing:
  enabled: true
  distance_points: 100
  min_step_points: 1                            # no modify request on every tick
```

An unknown key anywhere in this file is a **hard error naming the section**, not a
warning. A typo in a risk parameter that is silently ignored is the most expensive kind of
configuration bug there is.

## Architecture

```
CLI  →  Application (TradingService)  →  Orchestrator
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              ▼                         ▼                         ▼
         Strategy                  Risk Engine              Lifecycle
   (M15 direction, M1          (sizing, commission,      (state machine,
    stop-entry rules)            SL/TP, validation)       replacement)
              │                         │                         │
              └─────────────────────────┴─────────────────────────┘
                                        ▼
                          Execution  ──►  Broker (Protocol)
                                        │
                     ┌──────────────────┴──────────────────┐
                     ▼                                     ▼
              MetaTrader5Broker                    SimulatedBroker
                (native API)                        (dry-run / paper / backtest)
```

* The **domain** is free of MetaTrader 5, free of floats for money, and free of I/O.
* `strategy`, `risk`, `trailing` and `lifecycle` never import `MetaTrader5`.
  `scripts/check_architecture.py` enforces this in CI.
* `simulated_broker` is a first-class implementation, not a test double — it is what
  `DRY_RUN` and the backtester run on.

### The four rules that matter most

**A candle must be closed before it can be read.** `market_data/candles.py` is the only
route candles take to a decision. It drops every bar that had not finished at an explicitly
supplied reference moment, and returns a report naming what it withheld. A `hypothesis`
property proves that appending future bars cannot change a decision taken at time *t* —
proved once for the freeze and again for the whole strategy decision, because a correct
freeze can still be fed the wrong candles. `test-connection` aside, nothing can reach a
forming bar except by asking for it by name.

**Not trading is a result.** Most of the time the correct answer is to do nothing: the M15
candle is a doji, the feed has no closed bar yet, the M1 candle has not closed. That is
modelled as a first-class `NoTrade` carrying a named reason, not as an exception or a null
signal — otherwise every caller has to re-derive *why* nothing happened, and they will
eventually disagree.

**A risk limit that does not bind is not a limit.** The position sizer divides the budget by
*total* risk — price risk **plus** commission — and always rounds the size **down**. On the
project's own numbers, sizing on price risk alone would buy 0.5 lots carrying $53.00 of a
$50.00 budget: a 6 % overshoot on every trade, visible in no log and raised by nothing. A
budget too small for the broker's minimum position is **refused**, not rounded up. See
[`docs/risk/RISK_MODEL.md`](docs/risk/RISK_MODEL.md).

**Broker server time decides when a candle is closed.** MetaTrader 5 anchors bars and
sessions to server time, which is usually not UTC, so the local clock would be hours wrong —
and would look like a working strategy. `ServerClock` reads the terminal's own clock and
reports when it has had to fall back. See [`docs/mt5/SETUP.md`](docs/mt5/SETUP.md) §4.

**An ambiguous send is not a failed send.** MetaTrader 5 reports some failures as a code that
means only "I cannot say" — a timeout, most obviously, where the request may already have
reached the venue. Those become `ExecutionUnknownError` and are **never** retried; the only
correct response is to re-read broker state and find out. Assuming otherwise and resending is
how one order becomes two positions. Broker state is therefore re-read immediately before
every send, and identity is a deterministic tag that survives the terminal's 31-character
comment field, so duplicate detection still works after a restart. See
[`docs/execution/EXECUTION.md`](docs/execution/EXECUTION.md).

**A trailing stop that moves backwards is a bug, not a setting.** A BUY stop that follows price
down walks through the entry and out the other side, closing a trade that was well in profit
minutes earlier at a loss. The providers here return *no proposal* when a level would be worse
than the stop already in place, so no code path can emit one — and monotonicity is proved over
generated and adversarial price sequences rather than asserted on one example. Break-even's
trigger is measured on the price a close would actually get (the bid for a BUY), so it cannot
arm while the position is still underwater by the spread. See
[`docs/trailing/TRAILING_MODEL.md`](docs/trailing/TRAILING_MODEL.md).

**An intent is written down before it is sent.** Every order is recorded in a durable ledger
before the send that could create it, and settled afterwards. If the process dies in between,
recovery finds an intent whose outcome is unknown and knows to ask the broker — there is no
interval in which an order could exist at the venue and be unknown locally. The ledger never
"starts fresh": an unreadable state file raises rather than yielding an empty one, because an
empty ledger is exactly the condition under which duplicate orders appear. See
[`docs/lifecycle/LIFECYCLE.md`](docs/lifecycle/LIFECYCLE.md).

See [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md),
[`docs/strategy/ENTRY_RULES.md`](docs/strategy/ENTRY_RULES.md),
[`docs/risk/RISK_MODEL.md`](docs/risk/RISK_MODEL.md),
[`docs/execution/EXECUTION.md`](docs/execution/EXECUTION.md),
[`docs/trailing/TRAILING_MODEL.md`](docs/trailing/TRAILING_MODEL.md) and
[`docs/lifecycle/LIFECYCLE.md`](docs/lifecycle/LIFECYCLE.md).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Every phase must end with: tests passing,
documentation updated, `ROADMAP.md` / `HANDOFF.md` / `CHANGELOG.md` refreshed, git
working tree clean, commit created, push succeeded.

## Security

* No credentials in git. `.env` is git-ignored and root-anchored.
* The audit/journal logs are structural: no secret field exists on the log record model,
  so a token cannot be logged by mistake.
* `LIVE` requires two independent opt-ins plus a demo-only interlock.

## License

Proprietary. See [`LICENSE`](LICENSE).

## Disclaimer

Provided for **research and educational purposes only**. It is not financial,
investment, or trading advice. Trading leveraged instruments can lose you more than your
deposit.