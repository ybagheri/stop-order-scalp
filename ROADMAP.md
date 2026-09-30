# Roadmap

Status legend: `pending` · `in progress` · `complete` · `blocked`

**Read [`HANDOFF.md`](HANDOFF.md) before touching this file.**

---

## Phase 0 — Repository Audit — `complete`

Deliverables:

* [`docs/architecture/PHASE0_AUDIT.md`](docs/architecture/PHASE0_AUDIT.md)
  * Current state of `E:\stop-order-scalp` (empty, zero commits, clean tree)
  * `auto-trade` audit: reusable ideas adopted, project-specific code rejected, with
    reasons
  * `al-brooks-price-action-engine` audit: exact `TradePlan` / `Decision` / `Bar` shapes,
    the absence of a forming-bar flag, and the `frozen_bars` requirement
  * Key architectural decisions (native MT5 API over MQL5 over GUI automation; domain is
    MT5-free; `Decimal` prices; write-intent-before-act; broker is authoritative)
  * Implementation plan
* Baseline strategy table frozen
* `README.md`, `README.fa.md`, `ROADMAP.md`, `HANDOFF.md`, `CHANGELOG.md`

Quality gate: **met** — document committed and pushed.

---

## Phase 1 — Project Foundation — `pending`

Implement:

* `pyproject.toml` (setuptools, src layout, Python `>=3.11`, dev extras, `ruff`,
  `mypy --strict`, `pytest`)
* Package skeleton: `domain/`, `strategy/`, `risk/`, `execution/`, `trailing/`,
  `lifecycle/`, `infrastructure/`, `integrations/`, `application/`, `market_data/`
* `domain/value_objects/`: `Price`, `Volume`, `SymbolSpecification`, `Money`
* `domain/enums.py`, `domain/exceptions.py`
* `domain/interfaces.py`: `Broker`, `MarketDataProvider`, `Clock`, `AuditSink` protocols
* `infrastructure/config.py`: YAML + env layering, validated, no secrets in YAML
* `infrastructure/logging.py`: structured JSONL audit log, rotating, redaction by design
* `infrastructure/clock.py`: injectable clock, tz-aware only
* `cli/main.py` skeleton with `validate-config` and `--version`
* `.env.example`, `.gitignore`, `LICENSE`, `CONTRIBUTING.md`
* `tests/conftest.py` (pinned `TZ`, clean env), `tests/unit/` foundation tests
* `scripts/check_architecture.py` — architecture boundary enforcement
* Documentation: `docs/architecture/ARCHITECTURE.md`

Gate: tests pass, `ruff` clean, `mypy` clean, docs updated, commit, push.

---

## Phase 2 — Market Data — `pending`

Implement:

* `market_data/timeframes.py`: `M1`…`MN1` with period seconds and boundary math
* `market_data/mt5_module.py`: lazy `import MetaTrader5`, typed protocol for the slice used
* `market_data/mt5_feed.py`: connect, symbol select, ticks, candles, closed-candle freeze
* `domain/value_objects/price.py`: `points_to_price`, `price_to_points`, `normalize_price`,
  `normalize_volume` — all broker-spec driven
* `market_data/candles.py`: `Candle` domain object, `closed_only()` filter with an
  explicit freeze report, tz-aware timestamps
* Broker server-time vs UTC vs local-time handling, documented
* `docs/mt5/SETUP.md`, `docs/mt5/SYMBOL_SPECIFICATIONS.md`

Gate: tests pass (incl. timezone and boundary tests), lint/type clean, commit, push.

---

## Phase 3 — Core Strategy — `pending`

Implement:

* `strategy/candle_direction.py`: M15 candle direction with an explicit, configurable
  selection rule and no look-ahead
* `strategy/entry_rules.py`: BUY STOP = `M1.high + offset`, SELL STOP = `M1.low − offset`
* `strategy/signal.py` + `strategy/strategy.py`: produce a `TradeSignal` or `NoTrade`
* US30-only restriction enforced by `AllowedInstruments` policy
* Look-ahead bias tests, including the property that adding future candles cannot change a
  decision taken at time *t*

Gate: strategy tests + property tests, commit, push.

---

## Phase 4 — Risk Engine — `pending`

Implement:

* `risk/commission.py`: `per_lot_round_trip` vs `per_lot_per_side`
* `risk/position_sizer.py`: fixed-lot mode and percent-balance mode using tick value /
  tick size / contract size, never `1 point = $1`
* `risk/stop_loss.py` + `risk/take_profit.py`: `StopLossProvider` / `TakeProfitProvider`
  interfaces; fixed-points, risk-reward, signal-defined implementations
* `risk/risk_manager.py`: order validation inputs — volume bounds, stop distance, freeze
  level, margin, duplicate detection
* `1:1` R:R support and the precedence rule against the fixed 1000-point TP
* `docs/risk/RISK_MODEL.md`

Gate: risk tests (balances, commission, rounding, min/max/step, invalid specs), commit, push.

---

## Phase 5 — Order Execution — `pending`

Implement:

* `execution/order_manager.py`: build, validate, place pending orders; **re-read broker
  state before every send** (idempotency)
* `execution/mt5_broker.py`: `Broker` protocol over the native MT5 API
* `execution/simulated_broker.py`: full in-memory broker (dry-run, paper, backtest)
* Magic number + comment + client tag strategy identity
* Bounded exponential backoff for safe reads; **no resend after an unknown send outcome**
* Broker error classification (retryable vs terminal)

Gate: execution tests including duplicate-prevention and rejection handling, commit, push.

---

## Phase 6 — Position Management — `pending`

Implement:

* `trailing/break_even.py`: configurable trigger, broker minimum distance, freeze level,
  spread awareness, idempotent (no repeated modify on every tick)
* `trailing/trailing_stop.py`: BUY `bid − d`, SELL `ask + d`, **monotonic**
* `execution/position_manager.py`: BE → trailing → close detection
* Property tests over generated price sequences:
  BUY `sl_new >= sl_old`, SELL `sl_new <= sl_old`

Gate: BE/trailing tests + property tests, commit, push.

---

## Phase 7 — Lifecycle and Recovery — `pending`

Implement:

* `lifecycle/state_machine.py`: table-driven transitions with a listener
* `lifecycle/trade_lifecycle.py`: `WAITING_FOR_SIGNAL` → … → `POSITION_CLOSED` →
  `NEW_PENDING_ORDER`
* `infrastructure/persistence.py`: write-intent-before-act ledger, atomic writes,
  refuse-to-overwrite guard
* Restart recovery: reconcile local state against live MT5 state (broker authoritative)
* Automatic replacement order with full re-evaluation per §45
* Duplicate protection across duplicate ticks, reconnects, restarts, retries

Gate: lifecycle + recovery tests, commit, push.

---

## Phase 8 — Al Brooks Integration — `pending`

Implement:

* `integrations/al_brooks_adapter.py`: map `Decision.action` + `Decision.plan` to a
  domain signal
* `integrations/al_brooks_signal_provider.py`
* Optional extra `[albrooks]`; the only module importing `albrooks`
* **Disabled by default.** The M15/M1 baseline rules remain authoritative unless
  `integrations.al_brooks.enabled` is explicitly true
* Never invent signals; a `WAIT`/`NO_TRADE` decision is not a signal

Gate: adapter tests using a fake engine, commit, push.

---

## Phase 9 — Backtesting / Simulation — `pending`

Implement:

* `backtest/data.py`: historical candle loading
* `backtest/engine.py`: replay against `SimulatedBroker` with bid/ask, spread, slippage
* SL/TP, break-even, trailing, commission, equity curve
* `backtest/statistics.py`: trade count, win rate, average R, expectancy, profit factor,
  max drawdown, consecutive losses, Sharpe/Sortino, commission impact, slippage sensitivity
* Walk-forward / train / validation / out-of-sample split support
* Live execution and simulation stay fully separate

Gate: simulation tests, statistics tests, commit, push.

---

## Phase 10 — Observability — `pending`

Implement:

* `status` command output
* `journal` command — closed-trade journal from persistence
* `diagnostics` command — environment, config, MT5 reachability bundle
* Structured logging coverage for every field listed in the specification

Gate: CLI contract tests, commit, push.

---

## Phase 11 — Demo Validation — `pending`

* Run read-only validation against the user's MT5 demo terminal
* Capture the real `US30` symbol specification into `docs/mt5/SYMBOL_SPECIFICATIONS.md`
* Confirm dry-run → paper → demo progression end to end
* **Never** switch to live automatically
* Document results in `docs/operations/DEMO_VALIDATION.md`

Gate: report committed, no live trading occurred, commit, push.

---

## Phase 12 — Research / Optimization Layer — `pending`

Isolated, **disabled by default**, and must not modify the baseline:

* `research/filters.py`: spread, ATR volatility, M1 candle quality, M15 candle strength,
  session, pending-order expiration, one-trade-per-candle, optional news
* `research/optimize.py`: parameter sweeps over the backtest engine
* `research/report.py`: train / validation / out-of-sample / walk-forward reporting
* `docs/research/` with findings, including negative results

Gate: research tests; baseline still reproducible from `config/default.yaml` alone; commit, push.

---

## Out of scope (documented, not forbidden)

* Multi-instrument trading — the instrument policy is US30-only by specification
* MQL5 EA implementation — only if a future phase proves the native Python API is
  unreliable for order placement (§5.1 of the audit)
* Tick-by-tick latency arbitrage