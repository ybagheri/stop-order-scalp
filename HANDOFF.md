# Handoff

> **Mandatory.** Every AI agent or human continuing this project must read this file,
> then `ROADMAP.md`, then `README.md`, **before** changing anything.
>
> Read order: **`HANDOFF.md`** → `ROADMAP.md` → `README.md` →
> `docs/architecture/PHASE0_AUDIT.md` → this file again.

---

## Current Phase

**Phase 0 — Repository Audit — COMPLETE.**

Next phase to execute: **Phase 1 — Project Foundation.**

---

## Completed

* Audited `E:\stop-order-scalp` — it contains only `.git/`, branch `main`, **zero commits**,
  remote `git@github.com:ybagheri/stop-order-scalp.git`. Greenfield.
* Audited `E:\auto-trade` (GUI-automation executor for MT5) — extracted reusable
  architecture ideas, rejected its UI-automation execution model.
* Audited `E:\al-brooks-price-action-engine` — established the exact `TradePlan` /
  `Decision` / `Bar` shapes and the closed-candle freeze requirement.
* Wrote the audit, the architecture diagram, both READMEs, the roadmap, this handoff, and
  the changelog.

## Files Added

```
docs/architecture/PHASE0_AUDIT.md
README.md
README.fa.md
ROADMAP.md
HANDOFF.md
CHANGELOG.md
```

## Files Modified

None — the repository was empty.

## Tests

Not yet. No code exists. Phase 1 introduces `tests/conftest.py` and the first unit tests.

## Test Results

N/A.

## Git Commit

`docs(audit): phase 0 repository audit, architecture decisions and roadmap`

## Git Push

Pushed to `origin/main` (see `git log --oneline -1` after the commit).

---

## Current Architecture

Design only — nothing implemented yet. The intended shape:

```
src/stop_order_scalp/
    domain/          value objects, enums, exceptions, protocols. No I/O, no MT5, no floats for money
    market_data/     timeframes, candles, ticks, MT5 feed, point/price math
    strategy/        M15 direction filter, M1 stop-entry rules, signal generation
    risk/            commission, position sizing, SL providers, TP providers, validation
    trailing/        break-even, trailing stop (monotonic)
    execution/       Broker protocol, MT5 broker, simulated broker, order manager, position manager, gates
    lifecycle/       state machine, trade lifecycle, reconciliation
    infrastructure/  config, logging, persistence, clock
    integrations/    optional: al_brooks adapter
    application/     TradingService (composition + orchestration)
    backtest/        Phase 9
    research/        Phase 12
    cli/             argparse CLI
tests/               unit, integration, strategy, risk, execution, lifecycle, property
config/default.yaml  strategy defaults
```

Dependency direction is strictly inward. `strategy`, `risk`, `trailing`, `lifecycle` and
`domain` must not import `MetaTrader5`. `scripts/check_architecture.py` enforces this.

## Remaining Work

Phases 1 through 12, exactly as listed in `ROADMAP.md`. In order:

1. Project foundation
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

* **`git` is not on `PATH`** in the default PowerShell shell on this machine. It lives at
  `C:\Users\bagheri\AppData\Local\Programs\Git\cmd\git.exe`. Prepend it before any git
  command. This is a machine fact — it must not appear in application code or `.env`.
* **Git's bundled MSYS2 `ssh` is broken on this machine.** `$env:HOME` is empty, so the
  bundled `ssh` resolves `/home/bagheri/.ssh`, finds no key and no `known_hosts`, and every
  remote operation fails with `Host key verification failed`. Fix for every git command:

  ```powershell
  $env:PATH  = "C:\Users\bagheri\AppData\Local\Programs\Git\cmd;" + $env:PATH
  $env:GIT_SSH = "C:\Windows\System32\OpenSSH\ssh.exe"
  ```

  With `GIT_SSH` pointed at the Windows OpenSSH client, authentication to GitHub works
  (`ssh -T git@github.com` → `Hi ybagheri!`). Port 22 to GitHub is intermittently slow —
  a push can time out on the first attempt and succeed on the retry. Retry before
  concluding the remote is unreachable.
  The key `C:\Users\bagheri\.ssh\id_ed25519` is already registered with GitHub.
* Python 3.13.12 at `C:\Users\bagheri\Downloads\python-3.13.12-embed-amd64` (embedded
  distribution). `python -m pip` works; console scripts are not on `PATH`. Use
  `python -m ...` invocations.
* `hypothesis` was installed during Phase 0 for the property tests in Phases 3 and 6.
* The real MT5 `US30` symbol specification (tick size, tick value, volume step, stops
  level) is **unknown** and unvalidated. Tests use synthetic specifications. Phase 11
  captures the real values.
* `albrooks 0.1.0` is already installed globally, so the Phase 8 adapter can be
  exercised without extra setup.

## Decisions Made

1. **Python application + native MetaTrader5 Python API for execution.** Not MQL5, not
   GUI automation. `order_send` is atomic, server-validated, ticket-returning and
   observable; GUI automation is not testable and cannot express a pending stop order.
   MQL5 is documented as the justified fallback if the native API is ever proven
   unreliable — and only `execution/mt5_broker.py` would change.
2. **Domain is MT5-free.** Boundary enforced by a CI script, mirroring the mechanism
   already proven in `al-brooks-price-action-engine`
   (`scripts/check_no_mt5_dependency.py`).
3. **`Decimal` for all money, volume and price.** Floats exist only at the MT5 boundary.
   Point↔price conversion always goes through `SymbolSpecification`; the specification's
   "do not assume 1 point = $1" rule is therefore structural, not a comment.
4. **Write intent before act.** The order intent is persisted *before* `order_send`. An
   unknown send outcome is never retried — the system re-reads broker state instead.
   Adopted from `auto-trade`'s ledger, which is the most valuable single idea inherited
   from that project.
5. **The broker is authoritative on restart.** Local state is reconciled against live
   MT5 state filtered by magic number; positions and orders found on the broker are
   adopted, not recreated.
6. **Retry only what is safe to retry.** Bounded exponential backoff for reads. Sends are
   never blindly retried.
7. **Separate opt-in gate per account-changing operation** (`OrderGate`, `CloseGate`),
   `enabled=False` by default, `LIVE` needs a second independent switch. Inherited shape
   from `auto-trade`.
8. **Secrets are structurally unloggable.** No secret field exists on the log event
   model, so there is no filter that could be forgotten.
9. **Configuration layering:** `config/default.yaml` (strategy) → `.env` (machine) →
   real environment variables (`SOS_*`), where a real env var beats `.env`.
10. **Al Brooks is optional and disabled by default.** It is a geometry suggestion, not a
    signal the system trusts blindly; `Decision.to_dict()` explicitly states
    `is_recommendation: False`.
11. **Two independent candle implementations must agree** — this project's
    `market_data/candles.py` and al-brooks' `freeze_closed_bars`. Cross-tested.
12. **No absolute paths in application code.** Only `.env` / relative resolution.

## Configuration

Strategy defaults: `config/default.yaml` (created in Phase 1).
Machine settings: `.env` from `.env.example` (created in Phase 1).

Environment variable prefix: **`SOS_`**.

Planned keys: `SOS_ENVIRONMENT`, `SOS_ALLOW_LIVE`, `SOS_ALLOW_ORDER`, `SOS_ALLOW_CLOSE`,
`SOS_MT5_PATH`, `SOS_MT5_LOGIN`, `SOS_MT5_PASSWORD`, `SOS_MT5_SERVER`, `SOS_MT5_TIMEOUT_MS`,
`SOS_SYMBOL`, `SOS_MAGIC_NUMBER`, `SOS_CONFIG_PATH`, `SOS_STATE_PATH`, `SOS_LOG_DIR`,
`SOS_ENV_FILE`.

**`SOS_MT5_PASSWORD` is a secret. It lives only in `.env`, which is git-ignored. Never
commit it, never log it.**

## Next Recommended Phase

**Phase 1 — Project Foundation.** Start at
`ROADMAP.md` §"Phase 1", and use
`docs/architecture/PHASE0_AUDIT.md` §2.2 (reusable ideas to adopt) and §5 (decisions to
honour) as the design input.

Concretely, in order:

1. `pyproject.toml`, `.gitignore`, `.env.example`, `LICENSE`, `CONTRIBUTING.md`
2. `domain/enums.py`, `domain/exceptions.py`
3. `domain/value_objects/price.py` — `Price`, `Volume`, `SymbolSpecification`
4. `domain/interfaces.py` — `Broker`, `MarketDataProvider`, `Clock`, `AuditSink`
5. `infrastructure/clock.py`, `infrastructure/config.py`, `infrastructure/logging.py`
6. `market_data/timeframes.py` (needed by the config validator)
7. `cli/main.py` with `validate-config` + `--version`
8. `tests/conftest.py` + foundation unit tests
9. `scripts/check_architecture.py`
10. `ruff check .`, `mypy src`, `pytest`
11. Update `README.md`, `README.fa.md`, `ROADMAP.md`, `HANDOFF.md`, `CHANGELOG.md`
12. Commit and push

## Important Notes For The Next AI Agent

* **Do not change the baseline strategy.** It is frozen in `ROADMAP.md` §"Out of scope"
  and `docs/architecture/PHASE0_AUDIT.md` §6. Any enhancement must be configurable,
  documented, independently testable, and **disabled by default**.
* **Do not claim profitability.** No backtest against real data has been run.
* **`git` is not on `PATH`.** Run
  `& "C:\Users\bagheri\AppData\Local\Programs\Git\cmd\git.exe" ...` or prepend
  `$env:PATH = "C:\Users\bagheri\AppData\Local\Programs\Git\cmd;" + $env:PATH`.
  That path is **this machine only** — never write it into code, config, or docs.
* The working directory for this project is `E:\stop-order-scalp`, **not** the shell's
  default directory. Pass it as `workdir`.
* Prefer `python -m pytest` / `python -m stop_order_scalp` over console scripts, because
  the embedded Python does not put its `Scripts` directory on `PATH`.
* Only `execution/mt5_broker.py` and `market_data/mt5_feed.py` may import `MetaTrader5`,
  and only lazily. Only `integrations/al_brooks_adapter.py` may import `albrooks`.
* Test isolation is by **construction** (protocols + hand-written fakes), not by mocking
  and not by skip markers. There is no MT5 in the unit test suite at all.
* Use `Decimal` everywhere for money/price/volume. `float` at the MT5 boundary only.
* Never use a naive `datetime`. All timestamps must be timezone-aware UTC.
* Every phase ends with: `pytest` → fix → docs → `ROADMAP.md` → `HANDOFF.md` →
  `CHANGELOG.md` → review `git diff` → commit → push.