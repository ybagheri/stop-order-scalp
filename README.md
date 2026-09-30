# Stop Order Scalp

[نسخه فارسی](README.fa.md)

**US30 stop-order scalping system — M15 direction filter, M1 stop entries, MT5 execution.**

> **Status: under active development.** See [`ROADMAP.md`](ROADMAP.md) for what exists
> today and [`HANDOFF.md`](HANDOFF.md) for the state of the last completed phase.
>
> **Phases 1–3 complete** (foundation, market data, core strategy). The domain layer,
> configuration, logging, clock, the MetaTrader 5 feed, the closed-candle freeze and the
> M15/M1 baseline decision are implemented and tested — **533 tests, no broker required**.
> Of the seven CLI commands below, `validate-config` and `test-connection` work; the rest
> exit **4** and say which phase has not landed. The command surface is fixed ahead of its
> implementations on purpose, so the contract is testable now.
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
python -m stop_order_scalp test-connection
python -m stop_order_scalp run --dry-run
```

## CLI

```bash
python -m stop_order_scalp run                # live trading (refuses unless explicitly enabled)
python -m stop_order_scalp run --dry-run      # full pipeline, zero broker writes
python -m stop_order_scalp status             # connection, account, symbol, state, risk
python -m stop_order_scalp validate-config    # validate config/default.yaml + .env
python -m stop_order_scalp backtest --help    # historical replay
python -m stop_order_scalp test-connection    # MT5 reachability, read-only
python -m stop_order_scalp journal            # trade journal
python -m stop_order_scalp diagnostics        # environment + config bundle
```

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
python -m pytest                             # 533 tests
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

### The three rules that matter most

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

**Broker server time decides when a candle is closed.** MetaTrader 5 anchors bars and
sessions to server time, which is usually not UTC, so the local clock would be hours wrong —
and would look like a working strategy. `ServerClock` reads the terminal's own clock and
reports when it has had to fall back. See [`docs/mt5/SETUP.md`](docs/mt5/SETUP.md) §4.

See [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md) and
[`docs/strategy/ENTRY_RULES.md`](docs/strategy/ENTRY_RULES.md).

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