# Phase 0 — Repository Audit, Reuse Analysis and Implementation Plan

Status: **complete**
Date: 2026-09-30

This document is the Phase 0 deliverable. It records what exists on disk today, what
can be reused from the two sibling projects, what is deliberately *not* reused, and
the plan that drives Phases 1–12.

---

## 1. Current state of `stop-order-scalp`

| Item | Finding |
| --- | --- |
| `E:\stop-order-scalp` | Exists, contains **only** `.git/` |
| Branch | `main`, **zero commits** (`fatal: your current branch 'main' does not have any commits yet`) |
| Remote | `git@github.com:ybagheri/stop-order-scalp.git` (fetch/push) |
| Working tree | Clean |
| Tooling on this machine | Git 2.55.0 at `%LOCALAPPDATA%\Programs\Git\cmd` (**not on `PATH`**), Python 3.13.12 |

Consequences:

* There is no code, no configuration, no documentation and no test to preserve. Phase 0
  is a greenfield design exercise, not a migration.
* `git` is not on `PATH` in this shell. Every git invocation in this project must either
  use the absolute path or prepend `%LOCALAPPDATA%\Programs\Git\cmd`. This is a
  *machine* fact, never a *repository* fact — nothing about it is written into the code.

## 2. Audit of `auto-trade`

Read: `pyproject.toml`, `src/auto_trade/domain/{enums,models,protocols,exceptions}.py`,
`src/auto_trade/infrastructure/configuration/{config,env_file}.py`,
`src/auto_trade/infrastructure/logging/audit.py`,
`src/auto_trade/infrastructure/terminal/discovery.py`, `src/auto_trade/adapters/terminal.py`,
`src/auto_trade/application/{state_machine,risk,ledger,verification,workflow,kill_switch}.py`,
`src/auto_trade/infrastructure/automation/{execution,closing}.py`,
`src/auto_trade/cli/main.py`, `tests/**`, `.env.example`, `.gitignore`.

### 2.1 What `auto-trade` actually is

A **Windows GUI-automation** executor. It receives a `TradeSignal` from a file / HTTP /
pipe / websocket source and drives the MT5 desktop UI with `pywinauto`, clicking the real
Buy/Sell buttons. It has **no** `MetaTrader5` Python dependency at all — the test suite
does not import it, and the terminal is found by enumerating `terminal64.exe` through WMI.

That is a fundamentally different execution model from what this project needs. See §5.1.

### 2.2 Reusable ideas (adopted)

| Idea | Source | Adopted as |
| --- | --- | --- |
| Inward dependency rule, `Protocol` at every outward boundary | `domain/protocols.py` | `domain/interfaces/` — `Broker`, `MarketDataProvider`, `Clock`, `AuditSink` |
| Table-driven state machine with a listener callback; terminal states only reach `IDLE` | `application/state_machine.py:7-79` | `lifecycle/state_machine.py` — same shape, own transition table |
| **Write-intent-before-act ledger** | `application/ledger.py:39-48` | `infrastructure/persistence.py` — order intent recorded *before* the send |
| Refuse to overwrite a record whose `execution_id` differs | `application/ledger.py:52-54` | `infrastructure/persistence.py` — same guard |
| Atomic persistence via `.tmp` + `Path.replace()` | `application/ledger.py:130-137` | `infrastructure/persistence.py` |
| Unreadable ledger raises rather than silently resetting | `application/ledger.py:115-128` | Same |
| Pure, injected-clock `RiskEngine` returning a `RiskDecision(accepted, reason)` | `application/risk.py:10-48` | `risk/` — pure functions, no I/O, clock injected |
| Set-difference position verification (exactly one match, else not verified) | `application/verification.py:9-36` | `execution/position_manager.py` |
| `Decimal` for money/volume, tz-aware `datetime` required on parse | `domain/models.py:34-58` | `domain/value_objects/` — all prices/volumes are `Decimal` |
| `utc_now()` seam so tests are deterministic | `domain/models.py:34` | `infrastructure/clock.py` |
| Signal-id-as-filename pattern-restriction (path-traversal defence) | `domain/models.py:18-31` | `domain/models.py` — `strategy_id` / `client_tag` restricted |
| Secrets are **structurally** never passed to the logger | `infrastructure/logging/audit.py`, `diagnostics.py:25-29` | `infrastructure/logging.py` — no secret field exists on the event model |
| JSONL audit log + `RotatingFileHandler` + handler re-homing | `infrastructure/logging/audit.py:32-54` | Same |
| Separate opt-in gate per account-changing operation, `enabled=False` default, one ordered `refusal()` | `automation/execution.py:36-58`, `automation/closing.py:42-64` | `execution/gates.py` — `OrderGate`, `CloseGate` |
| `AUTO_TRADE_*` env-var prefix + `.env` with `os.environ.setdefault` so a real env var wins | `infrastructure/configuration/env_file.py:121` | `infrastructure/config.py`, prefix `SOS_*` |
| `safe_url_summary()` — never log a URL with credentials/query | `infrastructure/net.py:82` | Not needed (no HTTP in this project) |
| **No-retry-on-uncertainty** policy: bounded polling with a deadline, hard stop on `UNKNOWN` | `cli/main.py:850-925` | Adopted and extended (§5.4) |
| `.gitignore` root-anchored runtime paths (`/.env`, `/logs/`) | `.gitignore:13-15` | Same, for `SOS_*` paths |
| CLI is the composition root; heavy adapters imported function-locally | `cli/main.py` | `cli/main.py` |

### 2.3 Deliberately **not** reused

| Not reused | Why |
| --- | --- |
| `pywinauto` / Win32 automation | §5.1 — native MT5 API is strictly safer for order placement |
| `terminal64.exe` WMI discovery + `EnumWindows` | The native MT5 Python API takes a `path=` argument directly |
| Every control id (`10408`, `10409`, `10328`, `33033`, …) | UI-specific |
| Hardcoded Alpari/user paths in `configuration/config.py:46-57` | §5.6 — machine-specific absolute paths are forbidden |
| `config/default.yaml` in `auto-trade` | Nothing loads it; dead weight. Here the YAML config **is** loaded. |
| `audit` schema / `metrics.py` latency summary / `diagnostics.py` zip bundle | Tied to that audit schema |
| `domain/models.py` signal DTO shape | This project is not signal-driven; it is *rule-driven* |

## 3. Audit of `al-brooks-price-action-engine`

Read: `pyproject.toml`, `src/albrooks/**` public surface, `trade/plan.py`,
`decision/engine.py`, `adapters/mt5/*`, `core/bars.py`, `engine/pipeline.py`,
`engine/analyzer.py`, `README.md`, `tests/**`.

Key facts that constrain the integration:

| Fact | Value |
| --- | --- |
| Distribution / import name | `albrooks` (no underscore) — **already installed** on this machine (`albrooks 0.1.0`) |
| Runtime dependencies | none (stdlib only) |
| Python | `>=3.10` |
| License | MIT |
| Candle type | `Bar(time: float, open, high, low, close, volume, index)` — epoch seconds, **bar open time** |
| Forming-bar concept | **None.** A bar is closed iff `time + period_seconds <= now` |
| Freeze helper | `freeze_closed_bars(series, period_seconds, now=…, clock=…) -> (BarSeries, FreezeReport)` |
| Signal type | There is **no `Signal` class.** Geometry is `TradePlan`; the action is `Decision` |
| `Decision.action` | `"BUY" \| "SELL" \| "WAIT" \| "NO_TRADE"` |
| `Decision.plan` | `TradePlan.to_dict()` with `direction: int` (+1/−1/0), `entry`, `stop`, `target`, `reward_to_risk` |
| Execution / sizing | **Explicitly not built** — no broker, no orders, no sizing |
| MT5 dependency | Only `adapters/mt5/feed.py` may `import MetaTrader5` (CI-enforced by `scripts/check_no_mt5_dependency.py`) |
| MT5 feed API | `MT5Feed().connect(symbol, path=…)`, `.closed_bars(symbol, timeframe, count) -> FrozenSeries` |
| MT5 timeframe constants | `M1..M30`, `H1..H12`, `D1`, `W1`, `MN1` — MT5 encoding (`hours = 16384+h`) |

### 3.1 Consequences for the Al Brooks adapter

1. The adapter must map `Direction` → `Decision.action`, and must read SL/TP out of
   `Decision.plan`. It must **not** invent signals.
2. `Decision.to_dict()` hard-codes `"is_recommendation": False`. The adapter therefore
   treats an Al Brooks `BUY`/`SELL` as a *geometry suggestion* only. Whether it may
   override the M15/M1 baseline is a **configuration flag**, default **off**.
3. The engine has **no forming-bar flag**, so the adapter must call `freeze_closed_bars`
   itself (or use `MT5Feed.closed_bars`) before handing bars over. This project
   implements the same rule independently in `market_data/candles.py`; the two must agree.
4. The core is stdlib-only and CI-enforced against numpy/pandas. The adapter must
   therefore be an **optional extra** (`pip install ".[albrooks]"`), and must be the
   *only* module importing `albrooks`.

## 4. Implementation plan

Full detail in `ROADMAP.md`. Summary of the phases and their quality gates:

| Phase | Delivers | Gate |
| --- | --- | --- |
| 0 | Audit, reuse analysis, plan | this document committed |
| 1 | Packaging, config, logging, domain value objects, CLI skeleton | tests + ruff + mypy clean |
| 2 | MT5 gateway, symbol abstraction, candles, ticks, timeframes, point/price math, tz | tests + ruff + mypy clean |
| 3 | M15 direction filter, M1 stop entries, no-look-ahead proofs | strategy tests + property tests |
| 4 | Position sizing (fixed + 0.5 % risk), commission model, R:R, SL/TP providers | risk tests |
| 5 | Order validation, magic number, idempotency, bounded retry, broker error handling | execution tests |
| 6 | Break-even, trailing stop with monotonicity proof, TP modes | BE/trailing tests + property tests |
| 7 | State machine, automatic replacement, restart recovery, reconciliation | lifecycle + recovery tests |
| 8 | `albrooks` adapter (optional extra) | adapter tests with a fake engine |
| 9 | Historical replay, simulated fills, spread/slippage, SL/TP/BE/trailing, statistics | simulation tests |
| 10 | `status`, journal, diagnostics | CLI contract tests |
| 11 | Safe MT5 demo validation, documented | report committed, no live trading |
| 12 | Isolated research layer, disabled by default | research tests, baseline unchanged |

## 5. Key architectural decisions

### 5.1 Python application, MT5 Python API for execution (not MQL5, not GUI automation)

**Decision:** the application is Python. Order placement uses the **native MetaTrader5
Python API** (`MetaTrader5`), not `pywinauto` UI automation and not MQL5.

Rationale:

* `order_send` with `TRADE_ACTION_PENDING_DEAL` is atomic, validated by the trade server,
  returns a ticket, and is observable through `orders_get` / `positions_get`. GUI
  automation can silently mis-click, cannot express a pending stop order, and is
  untestable.
* MQL5 would be justified only if the strategy needed to live *inside* the terminal (for
  sub-second reaction, or when the PC is always on and Python restarts are unacceptable).
  Neither holds today. Should that change, only `execution/mt5_broker.py` would be
  replaced — the `Broker` protocol isolates it.
* Python keeps the testing, risk and backtest ecosystem the specification asks for.

**Consequence:** `metatrader5` is an optional runtime extra, imported **lazily** inside
`MetaTrader5Module._load()`. The core must be importable and fully testable with no
terminal installed.

### 5.2 Domain is MT5-free

`strategy/`, `risk/`, `trailing/` and `lifecycle/` import only from `domain/` and from
stdlib. A CI-checkable script (`scripts/check_architecture.py`) asserts that only
`execution/mt5_broker.py` and `market_data/mt5_feed.py` may import `MetaTrader5`, and
that no module outside `execution/` and `market_data/` may import `MetaTrader5` at all.
This mirrors the mechanism proven in the sibling project.

### 5.3 Prices are `Decimal`, points are broker-derived

The specification is explicit: *"Do not assume 1 point = $1."* Therefore:

* `domain/value_objects/price.py` holds `Price` (a `Decimal` with `digits`) and
  `SymbolSpecification` (point, tick_size, tick_value, contract_size, volume limits,
  stops_level, freeze_level, trade_mode).
* All point↔price conversions go through `SymbolSpecification`, never `* 10` or `* 0.1`.
* Money and volume are `Decimal` end to end; floats appear only at the MT5 boundary and
  are converted immediately.

### 5.4 Retry policy: bounded, and never on an uncertain send

Adopted from `auto-trade` and made explicit:

* **Safe to retry:** reads (account, symbol, candles, positions) — bounded exponential
  backoff, jitter, max attempts, then give up and log.
* **Never blindly retried:** `order_send`. The intent is written to the ledger *before*
  the call. After the call, the real broker state (`orders_get` filtered by magic) decides
  whether the order exists. If the send outcome is *unknown*, the system does **not**
  resend — it enters a `VERIFYING` state and re-reads broker state on the next cycle.
* All retries are idempotent operations by construction.

### 5.5 Restart recovery: the broker is authoritative

On startup the service runs `reconcile()`:

1. Read live MT5 state: positions and orders filtered by this strategy's **magic number**.
2. Read the local ledger.
3. Any open position with our magic → adopt it (`POSITION_OPEN`), regardless of what the
   ledger says.
4. Any pending order with our magic → adopt it (`WAITING_FOR_TRIGGER`).
5. Exactly one open position *and* one pending order for the same symbol → the pending
   order is a duplicate created by a race → cancel it.
6. Nothing on the broker and nothing pending → proceed to evaluate a fresh setup.

Blink-restart safety: before every send, `order_manager` re-reads broker orders for the
magic number and refuses to send if a matching pending order already exists.

### 5.6 Reproducibility

* `pyproject.toml` with pinned dev extras; Python `>=3.11`.
* `config/default.yaml` (strategy defaults) + `.env` (machine-local: MT5 path, login,
  server, magic number, environment mode).
* Environment variables `SOS_*` override file values; a real environment variable beats
  `.env` (`os.environ.setdefault` semantics, adopted from `auto-trade`).
* No absolute paths in application code. The only filesystem roots are relative to the
  current working directory or the project root, both resolved at runtime.
* Deterministic tests: injected `Clock`, no `time.sleep` in unit tests, `TZ` pinned in
  `tests/conftest.py`.

## 6. Strategy baseline (immutable)

The baseline is fixed and every parameter is configurable. Phase 12 may add
*optional, disabled-by-default* filters, but the baseline must remain reproducible from
`config/default.yaml` alone.

| Rule | Value |
| --- | --- |
| Instrument | `US30` (broker symbol name configurable) |
| Direction timeframe | M15, **last fully closed** candle |
| Entry timeframe | M1 |
| BUY STOP price | `M1.high + 10 points` |
| SELL STOP price | `M1.low − 10 points` |
| Risk | `0.5 %` of account balance (default mode) |
| Commission | `$6 / lot` |
| Take profit | `1000 points` (default mode `fixed_points`) |
| Risk/reward | `1.0` (used when mode is `risk_reward`) |
| Break-even | configurable trigger, move SL to entry |
| Trailing | `100 points`, monotonic |
| Automatic replacement | yes, after a position closes |

## 7. Known limitations at the end of Phase 0

* The MT5 Python API and the symbol specification for the user's actual broker can only
  be validated in Phase 11 against the real terminal. Until then, `US30` volume steps,
  tick values and stops levels are assumptions validated only against synthetic specs in
  tests.
* The `albrooks` integration is defined by this audit but not yet proven against the
  library's actual `Decision` payload shape.
* No profitability claim is made anywhere. Nothing has been tested against market data
  at this point.