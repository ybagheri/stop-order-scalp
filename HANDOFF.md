# Handoff

> **Mandatory.** Every AI agent or human continuing this project must read this file,
> then `ROADMAP.md`, then `README.md`, **before** changing anything.
>
> Read order: **`HANDOFF.md`** → `ROADMAP.md` → `README.md` →
> `docs/architecture/ARCHITECTURE.md` → `docs/strategy/BASELINE.md` → this file again.

---

## Current Phase

**Phase 1 — Project Foundation — COMPLETE and green.**

Next phase to execute: **Phase 2 — Market Data.**

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

## Files Added

```
docs/architecture/ARCHITECTURE.md
docs/strategy/BASELINE.md
docs/risk/README.md
docs/mt5/README.md
docs/testing/README.md
docs/operations/README.md
docs/research/README.md
tests/unit/test_architecture.py
tests/unit/test_cli.py
tests/unit/test_interfaces.py
```

## Files Modified

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

274, all passing.

| File | Tests |
| --- | --- |
| `tests/unit/test_interfaces.py` | 62 |
| `tests/unit/test_config.py` | 55 |
| `tests/unit/test_value_objects.py` | 46 |
| `tests/unit/test_architecture.py` | 34 |
| `tests/unit/test_timeframes.py` | 27 |
| `tests/unit/test_cli.py` | 25 |
| `tests/unit/test_logging.py` | 25 |

## Test Results

```
python -m pytest                             274 passed
python -m ruff check .                       All checks passed!
python -m mypy                               Success: no issues found in 36 source files
python scripts/check_architecture.py         architecture OK: 26 modules checked
python -m stop_order_scalp validate-config   exit 0
```

## Git Commit

`fix(foundation): make Phase 1 satisfy its own quality gate`

## Git Push

Pushed to `origin/main`.

---

## Current Architecture

```
src/stop_order_scalp/
    domain/          IMPLEMENTED — value objects, enums, models, exceptions, protocols
    infrastructure/  IMPLEMENTED — config, logging, clock
    market_data/     PARTIAL    — timeframes done; mt5_feed is Phase 2
    strategy/        empty      Phase 3
    risk/            empty      Phase 4
    execution/       empty      Phases 5, 6
    trailing/        empty      Phase 6
    lifecycle/       empty      Phase 7
    integrations/    empty      Phase 8
    backtest/        empty      Phase 9
    research/        empty      Phase 12
    application/     empty      composition + orchestration
    cli/             IMPLEMENTED — contract only; see below
tests/               unit (212). integration/ exists but is empty.
config/default.yaml  strategy defaults
scripts/             check_architecture.py
```

Dependency direction is strictly inward. `strategy`, `risk`, `trailing`, `lifecycle` and
`domain` must not import `MetaTrader5`. `scripts/check_architecture.py` enforces this and
`tests/unit/test_architecture.py` tests that the enforcement still works.

**The empty packages are intentional.** They exist so the boundary is real from day one
rather than being retrofitted.

## The CLI contract is ahead of its implementations

`cli/main.py` fixes the whole command surface in Phase 1. Only `validate-config` is
implemented. The other six commands exit **4** with a named message, because
`ComponentNotAvailableError` is raised rather than an `ImportError` traceback reaching the
operator:

| Command | Implemented in | Currently |
| --- | --- | --- |
| `validate-config` | Phase 1 | works |
| `run`, `status` | Phases 5, 10 | exit 4 |
| `test-connection` | Phase 5 | exit 4 |
| `journal`, `diagnostics` | Phase 7, 10 | exit 4 |
| `backtest` | Phase 9 | exit 4 |

When a phase lands, replace the `_resolve("…", "…")` call in the relevant `_cmd_*` function
with a normal import and delete the now-unnecessary indirection.

## Remaining Work

Phases 2 through 12, exactly as listed in `ROADMAP.md`. In order:

1. Project foundation — **done**
2. Market data
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
  before `test-connection` can succeed in Phase 5.
* **`MetaTrader5` is not installed here.** It is a broker-supplied package
  (`pip install -e ".[mt5]"`). Nothing in the repository imports it yet, and the unit suite
  passes without it. That is deliberate.
* **A `.env` in the working directory leaks into tests.** `load_config(env_file=None)`
  auto-discovers it. The test helpers pass an explicit non-existent path; if you add a
  test that calls `load_config` directly, do the same or it will depend on the developer's
  machine. Two regression tests in `test_config.py` pin this.
* **The real MT5 `US30` symbol specification is unknown and unvalidated.** Tests use a
  synthetic specification (point `0.1`, tick `0.1`, tick value `1.0`/lot, contract size
  `1.0`, volume step `0.1`). Phase 11 captures the real values.
* **`albrooks` is not installed here.** Phase 8's adapter needs `pip install -e ".[albrooks]"`.
* **No profitability is claimed and none has been tested.** No backtest has been run
  against real data.
* `docs/{risk,mt5,testing,operations,research}/` contain placeholder `README.md` files
  only, so the links in the top-level `README.md` resolve. Do not mistake them for
  finished documentation.

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

**Phase 2 — Market Data.** Start at `ROADMAP.md` §"Phase 2", with
`docs/architecture/ARCHITECTURE.md` §2–§6 as the design input.

Note that three of its deliverables are already done in Phase 1 and are marked as such in
the roadmap: `market_data/timeframes.py`, the point/price helpers in
`domain/value_objects/price.py`, and the `Candle` object in `domain/models.py`. What
remains is the MT5 side.

Concretely, in order:

1. `market_data/mt5_module.py` — lazy `import MetaTrader5`, a typed protocol for the slice
   actually used, so the rest of the codebase never sees the raw module
2. `market_data/mt5_feed.py` — connect, symbol select, ticks, candles, and the closed-candle
   freeze. This is one of only two modules permitted to name `MetaTrader5`
3. `market_data/candles.py` — the `closed_only()` filter with an explicit freeze report,
   built on the existing `Candle`
4. Broker server time vs UTC vs local time, and how it is handled
5. `docs/mt5/SETUP.md`
6. Tests: timezone and boundary tests, including that a forming candle is never consumed
7. `scripts/check_architecture.py`, `ruff`, `mypy`, `pytest`
8. Update `README.md`, `README.fa.md`, `ROADMAP.md`, `HANDOFF.md`, `CHANGELOG.md`
9. Commit and push

`docs/mt5/SYMBOL_SPECIFICATIONS.md` is listed under Phase 2 in the roadmap but the *real*
values are only obtainable in Phase 11. Write the document in Phase 2 with the structure
and the assumed values clearly labelled as assumed; replace them in Phase 11.

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
