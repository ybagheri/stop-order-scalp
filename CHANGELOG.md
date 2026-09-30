# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Phase 0 — Repository Audit
#### Added

- `docs/architecture/PHASE0_AUDIT.md` — audit of `stop-order-scalp`, `auto-trade` and
  `al-brooks-price-action-engine`; reuse analysis with explicit justification for every
  adoption and rejection; key architectural decisions; implementation plan.
- `README.md` and `README.fa.md` — bilingual documentation entry points.
- `ROADMAP.md` — 13 phases (0–12) with per-phase deliverables and quality gates.
- `HANDOFF.md` — mandatory state file for cross-laptop / cross-agent continuity.

#### Decisions

- Python application architecture with the **native MetaTrader 5 Python API** for
  execution. MQL5 and GUI automation rejected for now, with the conditions under which
  each would be reconsidered documented.
- Domain layer is MT5-free; the boundary is enforced by a CI script.
- `Decimal` for all money, price and volume; point↔price conversion always goes through
  the broker's `SymbolSpecification`.
- Order intent is persisted **before** the send; an unknown send outcome is never
  retried, only re-observed.
- The broker's live state is authoritative on restart.
- Secrets are structurally unloggable — no secret field exists on the log event model.

#### Notes

- No code exists yet. No tests have been run. No profitability claim is made.

### Phase 1 — Project Foundation

#### Added

- `domain/enums.py` — 14 enumerations covering side, order kind, risk and target modes,
  commission and break-even modes, environment, execution mode, candle selection, position
  and lifecycle state, and event kinds.
- `domain/exceptions.py` — 22 exception types rooted at `StopOrderScalpError`, separating
  the retryable from the terminal, with `ExecutionUnknownError` isolated because it is the
  single most important failure mode in the system.
- `domain/value_objects/price.py` — `Price`, `Money`, `Volume` and `SymbolSpecification`.
  `Price` carries the symbol's digit count so it cannot be rendered at the wrong precision;
  arithmetic yields a bare `Decimal` because a `Decimal` does not know the digit count.
  `SymbolSpecification` is the only place a point becomes a price or a price becomes money,
  which is what makes "do not assume 1 point = $1" structural rather than a comment.
- `domain/models.py` — the domain records: `Candle`, `Tick`, `AccountSnapshot`,
  `TradeSignal`, `TradePlan`, `OrderIntent`, `OrderRecord`, `PositionRecord`,
  `ManagedStop`, `RiskAssessment`, `InstrumentPolicy`, `CommissionModel`.
- `domain/interfaces.py` — the `Clock`, `MarketDataProvider`, `Broker` and `AuditSink`
  protocols. Every outward boundary in the system is one of these.
- `infrastructure/config.py` — three-layer configuration (`config/default.yaml` → `.env` →
  real `SOS_*` variables), with unknown keys rejected by name and `SOS_MT5_PASSWORD` reduced
  to a presence flag at load time.
- `infrastructure/logging.py` — JSON Lines audit log with rotation. No secret field exists
  on the event model, so a credential cannot be logged by mistake.
- `infrastructure/clock.py` — injectable clock; timezone-aware UTC only.
- `market_data/timeframes.py` — period seconds and boundary math for `M1`…`MN1`.
- `cli/main.py` — the complete command surface and exit-code contract.
- `config/default.yaml`, `.env.example`, `.gitignore`, `LICENSE`, `CONTRIBUTING.md`.
- `docs/architecture/ARCHITECTURE.md` — the layer model, the protocols, and the six
  enforced boundary rules.
- `docs/strategy/BASELINE.md` — the frozen strategy, and why the closed-candle rule and the
  points-are-not-dollars rule matter.
- Placeholder `README.md` in `docs/{risk,mt5,testing,operations,research}/` so the links in
  the top-level README resolve.
- 212 tests across six modules, including `hypothesis` property tests.

#### Fixed

Phase 1 stabilisation. The first Phase 1 commit did not satisfy its own quality gate. The
defects below were real, and several were guaranteed runtime failures.

- `config.__all__` exported a non-existent `PathSettings`; `import *` from the module raised
  `AttributeError`.
- `LifecycleState.is_recoverable` referenced an undefined `is_terminal`, so every call raised
  `AttributeError`. `is_terminal` is now defined — `STATE_HALTED` is the only absorbing
  state.
- `SymbolSpecification.price_risk_for` called `.lots` on a bare `Decimal`, raising
  `AttributeError` for any argument that was not a `Volume`, despite its own signature
  accepting one.
- `RiskAssessment.reject` declared no `self`, so `code` bound to `self` and the intended
  classmethod-style call failed. It is now a `@classmethod` returning `Self`.
- `AuditLogger.file_path` read `baseFilename` from the `logging.Handler` base class; the
  attribute is declared on `FileHandler`. The concrete handler is now retained.
- `TradePlan.__post_init__` compared a `Volume` to `0`, raising `TypeError` — and the check
  was unreachable anyway, since `Volume` already rejects a non-positive size on
  construction.
- `domain/interfaces.py` declared `Broker(Protocol, AccountReader, OrderBookReader, OrderExecutor)`.
  Naming `Protocol` before protocols that already inherit from it is an inconsistent MRO,
  so **the module could not be imported at all** — and the 212-test suite stayed green,
  because nothing imported it. `Protocol` now comes last. This was the file declaring the
  project's four core abstractions, so it was load-bearing and entirely non-functional.
- `cli/main.py` statically imported six modules belonging to Phases 5–10, which do not exist.
  Commands now resolve their components by name and raise `ComponentNotAvailableError`,
  exiting **4** with a named message instead of an `ImportError` traceback.
- `_fail()` accepted an exit code and discarded it; the code now appears in the message.
- 11 `__init__.py` files carried a UTF-8 BOM, which made `check_architecture.py` fail to
  parse them.

#### Fixed — in the architecture gate itself

`scripts/check_architecture.py` did not run, and the rules that did run were largely inert.
This is the most consequential part of the phase, because the gate is what makes the
architecture real rather than aspirational.

- Three `ast.unparse` calls crashed the script: on the `None` annotation of an un-annotated
  parameter, on `ast.arg.arg` (a plain `str`, not a node), and on any node reachable from
  either. The script aborted on nearly every module.
- `_CLOCK_ALLOWED`, `MT5_ALLOWED` and `AL_BROOKS_ALLOWED` held unqualified module names
  while the checker compares fully qualified ones, so **no module was ever exempt** and the
  two MT5 modules were rejected along with everything else.
- `_FLOAT_ANNOTATION` required a leading colon that `ast.unparse` never emits, so the
  float-money rule could never match.
- The wall-clock rule flagged `datetime.now(UTC)`, which is timezone-aware and therefore
  legal, including the project's own sanctioned `utc_now()` seam.
- The `time` rule matched only `from time import …`, leaving `import time` unchecked.

`tests/unit/test_architecture.py` was added to close this class of problem permanently. It
feeds deliberately broken source through the checker and asserts each rule fires, because a
gate that cannot fail is worse than no gate. It is how the four inert rules above were
found.

#### Fixed — test isolation

- `load_config(env_file=None)` auto-discovers a `.env` in the **current working directory**
  and merges it into `os.environ`. The configuration tests relied on that default, so a
  developer with a real `.env` silently stopped getting validation coverage. The helpers now
  pass an explicit non-existent path, and two regression tests pin both the isolation and
  the production behaviour it protects.

#### Added — the tests that would have caught the above

- `tests/unit/test_interfaces.py` (62 tests) — the protocols in `domain/interfaces.py`, plus
  two standing invariants over every module in the package: **every module imports**, and
  **every `__all__` entry resolves**. Those two are what expose the `Broker` MRO failure
  and the phantom `PathSettings` export respectively. It also pins that `Broker` composes
  its three narrow protocols, and that a hand-written fake satisfies it structurally with
  no inheritance at all.
- `tests/unit/test_architecture.py` (34 tests) — the architecture gate itself.
- `tests/unit/test_cli.py` (25 tests) — the parser surface, the exit codes, and the
  behaviour of a command whose phase has not landed.

#### Decisions

- The CLI surface is fixed in Phase 1, ahead of the components behind it. An unbuilt command
  reports itself by name and exits **4** rather than raising an `ImportError`.
- Every enforced rule must ship with a negative test in the same commit. A rule with no
  negative test has not been shown to work.
- `is_terminal` is defined as a property rather than an inline comparison, so adding an
  absorbing lifecycle state later is a one-line change.

#### Notes

- **No profitability is claimed.** No backtest against real data has been run.
- The real `US30` symbol specification is unknown. Every risk figure in the test suite is
  stated against a synthetic specification and labelled as assumed; Phase 11 captures the
  real values.
- Of the seven CLI commands, only `validate-config` is implemented. The other six exit **4**
  by design until their phase lands.