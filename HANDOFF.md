# Handoff

> **Mandatory.** Every AI agent or human continuing this project must read this file,
> then `ROADMAP.md`, then `README.md`, **before** changing anything.
>
> Read order: **`HANDOFF.md`** → `ROADMAP.md` → `README.md` →
> `docs/architecture/ARCHITECTURE.md` → `docs/strategy/BASELINE.md` → this file again.

---

## Current Phase

**Phase 2 — Market Data — COMPLETE and green.**

Next phase to execute: **Phase 3 — Core Strategy.**

---

## Completed

### Phase 0 — Repository Audit

* Audited the repository — greenfield, and wrote the audit, the READMEs, the roadmap, this
  handoff and the changelog. See `docs/architecture/PHASE0_AUDIT.md`.

### Phase 1 — Project Foundation

* Full domain layer: `domain/enums.py` (14 enums), `domain/exceptions.py` (22 types),
  `domain/value_objects/price.py` (`Price`, `Money`, `Volume`, `SymbolSpecification`),
  `domain/models.py` (the domain records), `domain/interfaces.py` (the four protocols).
* `infrastructure/config.py` — three-layer configuration with unknown-key rejection and
  secret reduction to a presence flag.
* `infrastructure/logging.py` — JSONL rotating audit log.
* `infrastructure/clock.py` — injectable, timezone-aware only.
* `market_data/timeframes.py` — period seconds and boundary math.
* `cli/main.py` — the complete command surface and exit-code contract.
* `scripts/check_architecture.py` — six boundary rules, enforced.
* `config/default.yaml`, `.env.example`, `.gitignore`, `LICENSE`, `CONTRIBUTING.md`.
* Documentation: `docs/architecture/ARCHITECTURE.md`, `docs/strategy/BASELINE.md`, and
  placeholder `README.md` in `docs/{risk,mt5,testing,operations,research}/`.

### Phase 1 stabilisation

The first Phase 1 commit did not satisfy its own quality gate. It has been repaired. This
matters for two reasons: the defects were real runtime bugs, and **four of the six
architecture rules had never once fired**. The worst single find was
`domain/interfaces.py` — the file declaring the project's four core protocols — which
**could not be imported at all** because `Broker` listed `Protocol` before protocols that
already inherit from it, producing an inconsistent MRO. The 212-test suite was green
throughout, because nothing imported it.

See `ROADMAP.md` §"Defects found and fixed during Phase 1 stabilisation" for the full
table.

### Phase 2 — Market Data

* `market_data/candles.py` — `freeze_closed_bars` with a `FreezeReport`, `select_closed`,
  and `require_closed_only`. Pure: no I/O, no clock. The reference moment is an argument.
* `market_data/mt5_module.py` — the project's single `import MetaTrader5`, a `Protocol` for
  the API slice used, and pure row→domain converters. numpy is not imported.
* `market_data/mt5_feed.py` — `MT5Feed`, `ServerClock`, `probe_connection`.
* `docs/mt5/SETUP.md` and `docs/mt5/SYMBOL_SPECIFICATIONS.md`.
* `tests/unit/test_candles.py` and `tests/unit/test_mt5.py`, including the no-look-ahead
  property.

## Files Added

```
docs/architecture/ARCHITECTURE.md
docs/strategy/BASELINE.md
docs/risk/README.md
docs/mt5/README.md
docs/mt5/SETUP.md
docs/mt5/SYMBOL_SPECIFICATIONS.md
docs/testing/README.md
docs/operations/README.md
docs/research/README.md
src/stop_order_scalp/market_data/candles.py
src/stop_order_scalp/market_data/mt5_module.py
src/stop_order_scalp/market_data/mt5_feed.py
tests/unit/test_architecture.py
tests/unit/test_cli.py
tests/unit/test_interfaces.py
tests/unit/test_candles.py
tests/unit/test_mt5.py
```

## Files Modified

Phase 2:

```
scripts/check_architecture.py       MT5_ALLOWED now names three modules
src/stop_order_scalp/cli/main.py    test-connection resolves market_data.mt5_feed
tests/unit/test_architecture.py     updated allowlist assertion, + the single-import test
tests/unit/test_cli.py              test-connection left DEFERRED_COMMANDS, + real tests
```

Phase 1 stabilisation:

```
.gitignore                                    .hypothesis/ added
pyproject.toml                                scripts on the pytest + mypy path, CLI per-file ignores
scripts/check_architecture.py                 6 bugs; 4 rules were inert
src/stop_order_scalp/cli/main.py              dynamic component resolution, exit code 4
src/stop_order_scalp/domain/enums.py          added the missing LifecycleState.is_terminal
src/stop_order_scalp/domain/exceptions.py     added ComponentNotAvailableError
src/stop_order_scalp/domain/models.py         RiskAssessment.reject is now a classmethod
src/stop_order_scalp/domain/value_objects/price.py   price_risk_for accepts a bare Decimal
src/stop_order_scalp/infrastructure/config.py  removed the phantom PathSettings export; _coerce is generic
src/stop_order_scalp/infrastructure/logging.py keeps the concrete FileHandler
11 x __init__.py                              UTF-8 BOM removed
tests/conftest.py, tests/unit/*.py            annotations, hermetic .env
```

## Tests

406 passing, 1 skipped.

| File | Tests |
| --- | --- |
| `tests/unit/test_mt5.py` | 72 |
| `tests/unit/test_interfaces.py` | 62 |
| `tests/unit/test_config.py` | 55 |
| `tests/unit/test_value_objects.py` | 46 |
| `tests/unit/test_candles.py` | 43 |
| `tests/unit/test_architecture.py` | 34 |
| `tests/unit/test_timeframes.py` | 27 |
| `tests/unit/test_cli.py` | 25 |
| `tests/unit/test_logging.py` | 25 |

The single skip is the `albrooks` cross-check of `freeze_closed_bars`, which needs the
optional extra. It is the only skip in the suite, and is acceptable only because that extra
is genuinely optional.

## Test Results

```
python -m pytest                             406 passed, 1 skipped
python -m ruff check .                       All checks passed!
python -m mypy                               Success: no issues found in 41 source files
python scripts/check_architecture.py         architecture OK: 29 modules checked
python -m stop_order_scalp validate-config   exit 0
python -m stop_order_scalp test-connection   exit 3, reports package_installed: false
```

## Git Commit

`feat(market-data): MetaTrader 5 feed, closed-candle freeze, broker server clock`

## Git Push

Pushed to `origin/main`.

---

## Current Architecture

```
src/stop_order_scalp/
    domain/          IMPLEMENTED — value objects, enums, models, exceptions, protocols
    infrastructure/  IMPLEMENTED — config, logging, clock
    market_data/     IMPLEMENTED — timeframes, candles/freeze, mt5_module, mt5_feed
    strategy/        empty      Phase 3
    risk/            empty      Phase 4
    execution/       empty      Phases 5, 6
    trailing/        empty      Phase 6
    lifecycle/       empty      Phase 7
    integrations/    empty      Phase 8
    backtest/        empty      Phase 9
    research/        empty      Phase 12
    application/     empty      composition + orchestration
    cli/             IMPLEMENTED — contract + validate-config + test-connection
tests/               unit (406). integration/ exists but is empty.
config/default.yaml  strategy defaults
scripts/             check_architecture.py
```

Dependency direction is strictly inward. `strategy`, `risk`, `trailing`, `lifecycle` and
`domain` must not import `MetaTrader5`. `scripts/check_architecture.py` enforces this and
`tests/unit/test_architecture.py` tests that the enforcement still works.

**The empty packages are intentional.** They exist so the boundary is real from day one
rather than being retrofitted.

## The MT5 boundary

`MetaTrader5` may be named by three modules, and only inside a function body:

| Module | Role |
| --- | --- |
| `market_data/mt5_module.py` | **owns the `import MetaTrader5`**, plus the `MT5Api` protocol and the pure converters |
| `market_data/mt5_feed.py` | reaches the terminal through `MT5Module`; `MT5Feed`, `ServerClock`, `probe_connection` |
| `execution/mt5_broker.py` | Phase 5 — order placement |

A test asserts that only `mt5_module.py` actually contains the import statement, so the
count cannot grow quietly.

`MT5Feed` has **no order methods**, and a test asserts they are absent. A strategy must not
be able to bypass the execution gate through the market-data layer.

## The clock decision, settled in Phase 2

| Question | Clock |
| --- | --- |
| Has this bar closed? | **broker server time**, from the terminal's `time_current()` |
| When did this bar/tick happen? | UTC, from absolute epoch seconds |
| Scheduling, deadlines, log stamps | local/UTC, via `SystemClock` |

`ServerClock` implements the same `Clock` protocol as the system clock, so it is injected
identically. It sets `degraded` when it falls back to the injected clock, because an
invisible substitution of the wrong clock is the exact failure this design prevents.

**Known limit, documented and tested:** `floor_time` floors in UTC; MetaTrader 5 floors in
server time. These agree for M1 and M15 — the only timeframes the strategy uses — for every
common broker offset, but **not** for `D1` at a non-whole-hour offset such as UTC+5:30. See
`docs/mt5/SETUP.md` §4 and `TestTimezoneHandling` in `tests/unit/test_candles.py`.

## The CLI contract is ahead of its implementations

`cli/main.py` fixes the whole command surface in Phase 1. Only `validate-config` is
implemented. The other six commands exit **4** with a named message, because
`ComponentNotAvailableError` is raised rather than an `ImportError` traceback reaching the
operator:

| Command | Implemented in | Currently |
| --- | --- | --- |
| `validate-config` | Phase 1 | works |
| `test-connection` | Phase 2 | works (read-only; exits 3 with the MT5 package absent) |
| `run`, `status` | Phases 5, 10 | exit 4 |
| `journal`, `diagnostics` | Phases 7, 10 | exit 4 |
| `backtest` | Phase 9 | exit 4 |

When a phase lands, replace the `_resolve("…", "…")` call in the relevant `_cmd_*` function
with a normal import and delete the now-unnecessary indirection.

## Remaining Work

Phases 2 through 12, exactly as listed in `ROADMAP.md`. In order:

1. Project foundation — **done**
2. Market data — **done**
3. Core strategy
4. Risk engine
5. Order execution
6. Position management
7. Lifecycle and recovery
8. Al Brooks integration
9. Backtesting / simulation
10. Observability
11. Demo validation
12. Research / optimization

## Known Issues

* **Machine facts for this checkout.** Git is on `PATH` at `C:\Program Files\Git`, so no
  prefixing is needed — this differs from the other machine this project lives on, where
  git lives at `%LOCALAPPDATA%\Programs\Git\cmd` and is *not* on `PATH`. Check before
  assuming either. Python here is **3.12.9**, not the 3.13.12 embedded build. These are
  machine facts and must never appear in application code, config, or docs.
* **The project root is `D:\Projects\stop-order-scalp`**, not the `E:\stop-order-scalp`
  recorded in the Phase 0 audit. Pass it as the shell's working directory.
* **`.env` exists and is git-ignored.** It sets `SOS_MT5_PATH` to the Alpari terminal.
  `SOS_MT5_LOGIN`, `SOS_MT5_PASSWORD` and `SOS_MT5_SERVER` are **blank** — fill them in
  before `test-connection` can report an account.
* **`MetaTrader5` is not installed here.** It is a broker-supplied package, not on PyPI —
  get it from your broker, then `pip install -e ".[mt5]"`. See
  [`docs/mt5/SETUP.md`](docs/mt5/SETUP.md) §1. Nothing in the repository imports it at
  module scope, and the whole suite passes without it. `test-connection` currently reports
  `package_installed: false` and exits 3, which is the correct behaviour, not a failure.
* **A `.env` in the working directory leaks into tests.** `load_config(env_file=None)`
  auto-discovers it. The test helpers pass an explicit non-existent path; if you add a
  test that calls `load_config` directly, do the same or it will depend on the developer's
  machine. Two regression tests in `test_config.py` pin this.
* **The real MT5 `US30` symbol specification is unknown and unvalidated.** Tests use a
  synthetic specification (point `0.1`, tick `0.1`, tick value `1.0`/lot, contract size
  `1.0`, volume step `0.1`). On those numbers a 100-point move costs **$100 per lot**, not
  $100. Phase 11 captures the real values; see
  [`docs/mt5/SYMBOL_SPECIFICATIONS.md`](docs/mt5/SYMBOL_SPECIFICATIONS.md).
* **`albrooks` is not installed here**, so the `freeze_closed_bars` cross-check skips.
  Phase 8's adapter needs `pip install -e ".[albrooks]"`. This is the **only** skip in the
  suite.
* **No profitability is claimed and none has been tested.** No backtest has been run
  against real data.
* `docs/{risk,testing,operations,research}/` contain a `README.md` only — useful, but not
  finished reference documentation. Do not mistake them for complete.

## Decisions Made

1. **Python application + native MetaTrader5 Python API for execution.** Not MQL5, not
   GUI automation. `order_send` is atomic, server-validated, ticket-returning and
   observable. MQL5 is the documented fallback if the native API proves unreliable, and
   only `execution/mt5_broker.py` would change.
2. **Domain is MT5-free.** Enforced by `scripts/check_architecture.py`, mirroring the
   mechanism already proven in the sibling project.
3. **`Decimal` for all money, volume and price.** Floats exist only at the MT5 boundary.
   Point↔price conversion always goes through `SymbolSpecification`, so "do not assume
   1 point = $1" is structural rather than a comment.
4. **Write intent before act.** The order intent is persisted *before* `order_send`. An
   unknown send outcome is never retried — broker state is re-read instead.
5. **The broker is authoritative on restart.** Local state is reconciled against live MT5
   state filtered by magic number; what the broker holds is adopted, not recreated.
6. **Retry only what is safe to retry.** Bounded exponential backoff for reads; sends are
   never blindly retried.
7. **Separate opt-in gate per account-changing operation**, `enabled=False` by default.
   `LIVE` needs a second independent switch.
8. **Secrets are structurally unloggable.** No secret field exists on the log event model,
   so there is no filter that could be forgotten.
9. **Configuration layering:** `config/default.yaml` → `.env` → real `SOS_*` environment
   variables, where a real environment variable beats `.env`.
10. **Al Brooks is optional and disabled by default.** A geometry suggestion, not a signal.
11. **Two independent candle implementations must agree** — this project's
    `market_data/candles.py` and the sibling engine's `freeze_closed_bars`. Cross-tested.
12. **No absolute paths in application code.** Only `.env` and relative resolution.
13. **The CLI surface is fixed before its implementations.** An unbuilt command raises
    `ComponentNotAvailableError` and exits **4**, never an `ImportError` traceback. Added
    during Phase 1 stabilisation; the alternative was six `mypy` errors against modules
    that do not exist yet.
14. **Every gate is itself tested.** `tests/unit/test_architecture.py` feeds deliberately
    broken source through the architecture checker, and
    `tests/unit/test_interfaces.py` asserts that every module in the package imports and
    every `__all__` entry resolves. This is not ceremony: it is the only reason the inert
    rules, the phantom export, and the un-importable `interfaces` module were caught.
15. **A forming candle is unreachable except by name.** `MT5Feed.candles` returns only
    closed bars, and `forming_candle()` is the sole route to a bar in progress. The feed
    requests one bar more than it needs and lets the freeze drop the forming one, rather than
    trusting the terminal to exclude it.
16. **A missing broker field is refused, not defaulted.** An absent `trade_tick_value` makes
    the instrument unusable, and a guessed value would mis-size every position. A missing
    `trade_mode` defaults to *demo*, so a misdetected live account fails closed.
17. **Symbol matching is exact and case-insensitive, never fuzzy.** `US30` can never select
    `US30mini` or `EURUSD30` by accident.

## Configuration

Strategy defaults: `config/default.yaml`.
Machine settings: `.env` from `.env.example`.

Environment variable prefix: **`SOS_`**.

Planned keys: `SOS_ENVIRONMENT`, `SOS_ALLOW_LIVE`, `SOS_ALLOW_ORDER`, `SOS_ALLOW_CLOSE`,
`SOS_MT5_PATH`, `SOS_MT5_LOGIN`, `SOS_MT5_PASSWORD`, `SOS_MT5_SERVER`, `SOS_MT5_TIMEOUT_MS`,
`SOS_SYMBOL`, `SOS_MAGIC_NUMBER`, `SOS_CONFIG_PATH`, `SOS_STATE_PATH`, `SOS_LOG_DIR`,
`SOS_ENV_FILE`, `SOS_AL_BROOKS_ENABLED`.

**`SOS_MT5_PASSWORD` is a secret. It lives only in `.env`, which is git-ignored. Never
commit it, never log it.** It is reduced to a presence flag at load time and no downstream
code reads the variable.

## Next Recommended Phase

**Phase 3 — Core Strategy.** Start at `ROADMAP.md` §"Phase 3", with
`docs/strategy/BASELINE.md` and `docs/architecture/ARCHITECTURE.md` §5–§6 as the design
input.

The pieces Phase 3 needs all exist:

| What Phase 3 needs | Where it is |
| --- | --- |
| `Candle` with `is_closed_at`, `direction`, `is_doji` | `domain/models.py` |
| `freeze_closed_bars` + `FreezeReport` | `market_data/candles.py` |
| `Timeframe` names, `period_seconds`, `floor_time` | `market_data/timeframes.py` |
| `Side`, `TimeframeSelection`, `InstrumentPolicy` | `domain/enums.py`, `domain/models.py` |
| `TradeSignal` | `domain/models.py` |
| `SymbolSpecification.points_to_price` | `domain/value_objects/price.py` |
| `MarketDataProvider` | `domain/interfaces.py` |

Concretely, in order:

1. `strategy/candle_direction.py` — the M15 direction filter, reading the **last closed**
   M15 candle. Handle the doji case explicitly: `Candle.direction` already returns `None`,
   and a doji must not be turned into a trade.
2. `strategy/entry_rules.py` — BUY STOP = `M1.high + offset_points`, SELL STOP =
   `M1.low − offset_points`, converted through `SymbolSpecification.points_to_price`.
   **Never multiply a point count by a price.**
3. `strategy/signal.py` and `strategy/strategy.py` — produce a `TradeSignal` or a
   `NoTrade`. The no-trade path must be a first-class result, not an exception.
4. Enforce US30-only via `InstrumentPolicy`.
5. The no-look-ahead property test, extending the one already in
   `tests/unit/test_candles.py::TestNoLookAhead` to the full decision, not just the freeze:
   **appending future candles cannot change a decision taken at time *t***. This is the
   project's headline correctness property and it is a release gate.
6. `docs/strategy/` — document the selection rule and why the closed candle is mandatory.
7. Gates, then `ROADMAP.md` → `HANDOFF.md` → `CHANGELOG.md` → `README.md` → `README.fa.md`
   → commit → push.

**Note on the strategy contract:** `entry.candle_selection` already supports
`current_forming` for research. The baseline is `last_closed`. Do not let the research mode
leak into the baseline path, and keep `filters_enabled: false`.

## Important Notes For The Next AI Agent

* **Do not change the baseline strategy.** It is frozen in
  `docs/strategy/BASELINE.md` and `ROADMAP.md` §"Out of scope". Any enhancement must be
  configurable, documented, independently testable, and **disabled by default**.
* **Do not claim profitability.** No backtest against real data has been run.
* **Run all four gates before committing**: `python -m pytest`, `python -m ruff check .`,
  `python -m mypy`, `python scripts/check_architecture.py`. The first three are also
  covered by `pytest`, but run them individually so the output is legible.
* **`mypy` checks `src` *and* `tests`** — the config sets `files = ["src", "tests"]` with
  `strict = true`. Adding an unannotated test helper will fail the build.
* **Prefer `python -m pytest` / `python -m stop_order_scalp`** over console scripts.
* **Only `execution/mt5_broker.py` and `market_data/mt5_feed.py` may import
  `MetaTrader5`**, and only inside a function body. Only
  `integrations/al_brooks_adapter.py` may import `albrooks`.
* **Test isolation is by construction** — protocols plus hand-written fakes. No mocking
  framework, no skip markers, and no MT5 in the unit suite at all.
* **Use `Decimal` everywhere** for money, price and volume. `float` at the MT5 boundary
  only.
* **Never use a naive `datetime`.** All timestamps are timezone-aware UTC, and business code
  takes a `Clock`.
* **When you add a rule to `scripts/check_architecture.py`, add the negative test to
  `tests/unit/test_architecture.py` in the same commit.** A rule with no negative test has
  not been shown to work, and four of the six original rules had never fired.
* **When you add a module, check it imports.** `tests/unit/test_interfaces.py` walks the
  package and imports everything, so this is automatic — but note that a module nothing
  imports can otherwise break silently. An inconsistent MRO among `Protocol` bases is a
  `TypeError` at import time; `Protocol` must be listed **last**.
* **Every phase ends with:** all four gates → fix → docs → `ROADMAP.md` → `HANDOFF.md` →
  `CHANGELOG.md` → review `git diff` → commit → push.
