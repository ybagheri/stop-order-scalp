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

## Phase 1 — Project Foundation — `complete`

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

Delivered:

* `domain/` — enums (14), exceptions (22), `Price` / `Money` / `Volume` /
  `SymbolSpecification`, domain models, and the four outward protocols
* `infrastructure/` — three-layer config with unknown-key rejection and secret reduction,
  JSONL rotating audit log, injectable clock
* `market_data/timeframes.py` — period seconds and boundary math
* `cli/main.py` — the full command surface and exit-code contract. Only `validate-config`
  is implemented; the other six exit **4** by name until their phase lands
* `docs/architecture/ARCHITECTURE.md`, `docs/strategy/BASELINE.md`, and placeholder
  `README.md` in `docs/{risk,mt5,testing,operations,research}/`

Quality gate: **met** — 274 tests pass, `ruff` clean, `mypy --strict` clean over
`src` and `tests`, `scripts/check_architecture.py` reports 26 modules and no violations.

### Defects found and fixed during Phase 1 stabilisation

The first Phase 1 commit did not satisfy its own gate. Fixing it surfaced real bugs, not
just lint noise:

| Defect | Consequence |
| --- | --- |
| `config.__all__` exported a non-existent `PathSettings` | `import *` from the module raised `AttributeError` |
| `LifecycleState.is_recoverable` called an undefined `is_terminal` | `AttributeError` on every call |
| `SymbolSpecification.price_risk_for` called `.lots` on a bare `Decimal` | `AttributeError` for any non-`Volume` argument |
| `RiskAssessment.reject` declared no `self` | `code` bound to `self`; the classmethod-style call failed |
| `AuditLogger.file_path` read `baseFilename` off the `Handler` base class | `AttributeError`; the attribute is on `FileHandler` |
| `TradePlan.__post_init__` compared a `Volume` to `0` | `TypeError`; and the check was unreachable anyway |
| `Broker(Protocol, AccountReader, …)` — `Protocol` listed first | inconsistent MRO; `domain/interfaces.py` could not be **imported at all**, and nothing tested it |
| `check_architecture.py` crashed on un-annotated parameters | the whole gate failed to run |
| `check_architecture.py` crashed on `ast.arg.arg` (a `str`, not a node) | ditto |
| `_CLOCK_ALLOWED` held unqualified module names | no module was ever exempt; `datetime.now(UTC)` was flagged |
| `MT5_ALLOWED` / `AL_BROOKS_ALLOWED` held unqualified module names | rules 1 and 2 rejected the two permitted modules and permitted nothing |
| `_FLOAT_ANNOTATION` required a leading colon `ast.unparse` never emits | rule 4 could never match; entirely inert |
| the `time` rule only matched `from time import …`, not `import time` | rule 6 had a hole |
| 11 `__init__.py` files carried a UTF-8 BOM | `check_architecture.py` could not parse them |
| `cli/main.py` statically imported six modules from Phases 5–10 | `mypy` failed; a traceback at run time |

Two further findings were test defects rather than code defects, and both are now pinned by
regression tests:

* `load_config(env_file=None)` auto-discovers a `.env` in the **working directory**, so the
  config tests read the developer's real machine settings and silently stopped validating
  anything once a `.env` existed. The helpers now pass an explicit non-existent path.
* The architecture gate had never been tested. `tests/unit/test_architecture.py` now feeds
  deliberately broken source through it, which is how the four inert rules above were
  found. `tests/unit/test_interfaces.py` adds the same treatment for the package surface:
  every module must import, and every `__all__` entry must resolve.

---

## Phase 2 — Market Data — `complete`

Start here: `market_data/timeframes.py` and the point/price helpers in
`domain/value_objects/price.py` were already implemented in Phase 1, and `Candle` already
lived in `domain/models.py` with `is_closed_at()` and `close_time_is_floor()`. Phase 2
built the MT5 side on top of them rather than re-implementing them.

Implement:

* `market_data/timeframes.py`: `M1`…`MN1` with period seconds and boundary math
  — **done in Phase 1**
* `market_data/mt5_module.py`: lazy `import MetaTrader5`, typed protocol for the slice used
  — **done**; it owns the import statement
* `market_data/mt5_feed.py`: connect, symbol select, ticks, candles, closed-candle freeze
  — **done**
* `domain/value_objects/price.py`: `points_to_price`, `price_to_points`, `normalize_price`,
  `normalize_volume` — all broker-spec driven — **done in Phase 1**
* `market_data/candles.py`: `Candle` domain object, `closed_only()` filter with an
  explicit freeze report, tz-aware timestamps — **done**; `Candle` was in
  `domain/models.py`, the freeze lives here
* Broker server-time vs UTC vs local-time handling, documented — **done**;
  `docs/mt5/SETUP.md` §4
* `docs/mt5/SETUP.md`, `docs/mt5/SYMBOL_SPECIFICATIONS.md` — **done**

Delivered:

* `market_data/candles.py` — `freeze_closed_bars` with a `FreezeReport` recording exactly
  what was withheld, `select_closed`, and `require_closed_only` as a guard. Pure: no I/O, no
  clock. The reference moment is an argument, so the same input always gives the same
  output.
* `market_data/mt5_module.py` — the one `import MetaTrader5` in the project, a `Protocol`
  for the API slice used, and pure converters from terminal rows to domain objects. numpy is
  **not** imported: the rates table is described structurally, so the project stays stdlib
  plus a YAML parser.
* `market_data/mt5_feed.py` — `MT5Feed` (read-only; no order methods), `ServerClock`, and
  `probe_connection`.
* `docs/mt5/SETUP.md` and `docs/mt5/SYMBOL_SPECIFICATIONS.md`.

Quality gate: **met** — 406 tests pass (1 skipped: the optional `albrooks` cross-check),
`ruff` clean, `mypy --strict` clean over `src` and `tests`, architecture reports 29 modules
and no violations.

### Decisions made in Phase 2

1. **`mt5_module.py` owns the import; `mt5_feed.py` reaches the terminal through it.** The
   architecture allowlist now names three modules, but only one of them contains an
   `import MetaTrader5` statement. A test asserts that, so a second import cannot creep in.
2. **`probe_connection` lives in `market_data`, not `execution`.** A read-only reachability
   check is a market-data concern. The CLI was re-pointed accordingly, which makes
   `test-connection` work as of Phase 2 instead of Phase 5.
3. **The feed requests `count + 1` bars and lets the freeze drop the forming one**, rather
   than requesting `count` and trusting the terminal to exclude it.
4. **A missing broker field is refused, not defaulted.** `trade_tick_value` absent means the
   instrument is rejected by name, because a guessed tick value mis-sizes every position.
5. **A missing `trade_mode` defaults to "demo".** Fail closed: a misdetected live account
   must not be assumed live.
6. **Symbol matching is exact and case-insensitive, never fuzzy**, so `US30` cannot select
   `US30mini`.

### The clock question, settled

Broker **server** time decides candle closure, via the terminal's `time_current()`. Bar and
tick timestamps arrive as absolute epoch seconds and are converted in UTC. `ServerClock`
implements the same `Clock` protocol as the system clock, so it is injected identically and
there is no second code path. When it falls back to the injected clock it sets `degraded`,
because an invisible substitution of the wrong clock is the failure this design exists to
prevent.

One limit is documented and tested rather than left implicit: `floor_time` floors in UTC,
MetaTrader 5 floors in server time, and these agree for M1 and M15 — the only timeframes
the strategy uses — for every common broker offset, but **not** for `D1` at a non-whole-hour
offset such as UTC+5:30. `tests/unit/test_candles.py::TestTimezoneHandling` states both
halves.

---

## Phase 3 — Core Strategy — `complete`

Implement:

* `strategy/candle_direction.py`: M15 candle direction with an explicit, configurable
  selection rule and no look-ahead — **done**
* `strategy/entry_rules.py`: BUY STOP = `M1.high + offset`, SELL STOP = `M1.low − offset`
  — **done**
* `strategy/signal.py` + `strategy/strategy.py`: produce a `TradeSignal` or `NoTrade`
  — **done**
* US30-only restriction enforced by `AllowedInstruments` policy — **done**; uses the
  existing `InstrumentPolicy`
* Look-ahead bias tests, including the property that adding future candles cannot change a
  decision taken at time *t* — **done**

Delivered:

* `strategy/candle_direction.py` — five verdicts, of which two authorise a trade. A doji has
  no direction and is not traded; a missing candle is not a neutral candle, and
  `is_indeterminate` separates "could not tell" from "told, and the answer is no".
* `strategy/entry_rules.py` — the two exact rules, with every point conversion through
  `SymbolSpecification`. No function multiplies a point count by a price.
* `strategy/signal.py` — `Decision = TradeDecision | NoTrade`. "Not trading" is a
  first-class value with a named reason and its evidence attached, never an exception and
  never a null signal.
* `strategy/strategy.py` — the `StopOrderStrategy` façade. Stateless, so "what did the
  strategy believe at time *t*?" stays answerable.
* `docs/strategy/ENTRY_RULES.md` and `docs/strategy/README.md`.
* 119 tests in `tests/strategy/`.

Quality gate: **met** — 533 tests pass (1 skipped: the optional `albrooks` cross-check),
`ruff` clean, `mypy --strict` clean over `src` and `tests`, architecture reports 33 modules
and no violations.

### The headline property

> **appending future candles cannot change a decision taken at time *t***

Stated as a `hypothesis` property over generated market data rather than as one worked
example, because an example only proves the function behaved on the case somebody thought
of. The generators vary the bar count, the reference moment's position within the series,
the M15 bodies (bull/bear/doji) and the input order, because the interesting cases are the
ones where the reference falls inside a forming bar.

Three more fall out of it: the same inputs always give the same decision; input order never
changes the decision; and a decision never uses a bar that had not closed at the reference.

### A bug found while writing this phase

The first implementation of `entry_rules.py` reused
`SymbolSpecification.round_stop_price` for entry prices. It is the wrong method, and
inverted the rounding.

| Method | Anchored on | BUY rounds | SELL rounds |
| --- | --- | --- | --- |
| `round_stop_price` | the **entry** (a stop loss sits below a BUY entry) | down | up |
| `round_entry_price` | the **candle's extreme** (a BUY STOP sits above the high) | up | down |

Using the first for an entry moved every order a tick *toward* the market: the stop would
trigger before the level the strategy specified, which is a different trade, and it also
moves closer to market where a broker's `stops_level` rejection lives.

`SymbolSpecification.round_entry_price` was added, `entry_rules.py` uses only that, and a
test asserts the two disagree. Rounding is only non-trivial when a price arrives off the
tick grid, so this is a correctness guarantee rather than a workaround.

### Decisions made in Phase 3

1. **A doji is never traded, and never falls back to an earlier candle.** "Use the last
   candle that had a direction" silently changes the rule from *most recent* to *most recent
   directional*, which is a different strategy. Excluded by a test.
2. **A missing candle is not a neutral candle.** Separate verdicts, and `is_indeterminate`
   so an operator can tell a quiet market from a broken feed.
3. **An instrument-policy refusal raises rather than returning `NoTrade`.** Reaching that
   check means the configuration or feed is wrong, and a run that silently declines to
   trade all day is the worst possible outcome — it looks like a strategy that just is not
   triggering.
4. **The strategy carries no stop loss or take profit.** Those come from configuration in
   the risk engine, so it stays possible to tell which signals carried geometry.
5. **The check order is direction, then entry, then timeframe.** When both the direction and
   the entry are unusable, the reason names the direction, because that is what has to be
   fixed first. Pinned by a test.
6. **`StopOrderStrategy` accepts a specification whose name is any configured alias**, not
   only the logical symbol. A broker naming the permitted instrument `US30.cash` is the
   common case, and requiring an exact match would make the strategy impossible to
   construct on most brokers.

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