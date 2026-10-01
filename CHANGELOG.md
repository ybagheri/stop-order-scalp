# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Phase 5 — Order Execution

#### Added

- `execution/gates.py` — `OrderGate`, `CloseGate` and `LiveInterlock`. Both gates default
  to **closed**; `LIVE` requires the environment, `SOS_ALLOW_LIVE` and the operation's own
  switch, and any two are not enough. Closing positions has a switch separate from opening
  them: automatic closing on a losing streak is exactly when an operator wants it off.
  Refusals are ordered cheapest-first so the reason reported is the one to act on.
- `execution/order_manager.py` — idempotent placement. Broker state is re-read
  **immediately before every send**, not cached from earlier in the cycle: a cached read is
  correct until the process is interrupted, and an interruption between "decided to place"
  and "read the book" is exactly the case that duplicates a position. Refusals are values
  (`PlacementOutcome`, `RefusalCode`) rather than exceptions, because refusing a duplicate is
  correct behaviour that happens routinely.
- `execution/mt5_broker.py` — the native MT5 `Broker`, plus `classify()`. The retcode table
  has three buckets, not two: see Decisions.
- `execution/simulated_broker.py` — an in-memory venue with real bid/ask fills, pending
  stops resting on the book, SL/TP settlement on the correct side, magic numbers and
  commission charged on close. Not a test double: it is what `DRY_RUN` and `PAPER` run
  against, which is the only reason a dry run proves anything.
- `execution/retry.py` — bounded exponential backoff for **safe reads only**.
- `docs/execution/README.md` and `docs/execution/EXECUTION.md`.
- 145 tests in `tests/execution/`.

#### Decisions

- **The retcode table has three buckets, not two.** "Retryable" versus "terminal" cannot
  classify `10012` (timeout) or `10031` (connection lost), because the request may already
  have reached the venue. Those become `ExecutionUnknownError`, which is never retryable; the
  caller re-observes broker state instead. An **unrecognised** retcode is treated as unknown
  rather than terminal — being wrong pessimistically costs a re-read, being wrong
  optimistically costs a duplicate position.
- **No layer retries a send.** Not the broker, not the manager, not `retry.py`, which
  re-raises `ExecutionUnknownError` immediately precisely so that a placement mistakenly
  passed in as a read cannot be ridden out.
- **Duplicates match on the client tag, never on price.** Price matching would treat a new
  setup at the same level as a duplicate and would miss a duplicate whose price moved. The
  derivation lives in exactly one place, `OrderIntent.client_tag_for(plan)`, so the check and
  the sent order cannot disagree.
- **The identity tag is packed into the terminal's comment, tag first.** MT5 has no
  client-order-id and truncates comments at 31 characters; a 20-character tag leaves about
  ten for prose. A documented trade-off, and identity is worth more than prose.
- **A cancellation asks the order book, not the retcode.** Its goal is that the order is not
  on the book, so an ambiguous response is resolved by re-reading rather than retried.
- **Floating P/L is gross and commission is charged once, at close.** Charging it in both
  places would report less equity than a live account actually has.

#### Changed

- `domain/models.py` — `OrderIntent.client_tag_for()` extracted as the single public place
  the identity tag is derived.
- `domain/interfaces.py` — `AccountReader` gained `specification()`. The risk engine needs the
  **broker's own** contract numbers — tick size, tick value, contract size, stops level — and
  those come from the venue, not from a price series.
- `market_data/mt5_module.py` — `MT5Api` gained `order_send`, `order_get`, `orders_get` and
  `positions_get`. The dependency surface stays described in one module rather than gaining a
  second, private description in the execution layer.
- `infrastructure/clock.py` — `sleep()` added, so `import time` has exactly one home. The
  architecture check already refused it elsewhere; making the legal home explicit keeps retry
  testable without a real wait.
- `risk/risk_manager.py` — `risk_settings` and `target_settings` exposed, so a caller that
  re-assesses against live state uses the same settings the manager was built with rather
  than reconstructing defaults that could differ.

#### Known limitations

- `OrderManager.place(settings=None)` skips the gate check. Correct for `DRY_RUN` and
  `PAPER`, where the venue is the simulator, but a caller that hands it a real broker and
  omits `settings` would route orders ungated. Phase 7's composition root should make the
  environment explicit rather than optional. Not a defect in any current call path.
- `MetaTrader5` is not installed on the development machine, so the wire values, the retcode
  table and the tag encoding have never met a real terminal. `test-connection` correctly
  exits 3 with `package_installed: false`. No live order has been placed.

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

---

### Phase 2 — Market Data

#### Added

- `market_data/candles.py` — `freeze_closed_bars` keeps only the bars that had fully formed
  at an explicitly supplied reference moment, and returns a `FreezeReport` recording exactly
  what was withheld: how many forming bars were dropped, which one was in progress, whether
  the input arrived out of order, and the boundaries of what survived. Also `select_closed`
  and `require_closed_only`, a guard that turns a silent look-ahead bug into a loud failure
  at the point of misuse.
- `market_data/mt5_module.py` — the single `import MetaTrader5` in the project, a `Protocol`
  describing exactly the terminal functions used, and pure converters from terminal rows to
  domain objects. The converters are duck-typed over namedtuple, dataclass, mapping and
  object, so the test suite needs no numpy and no terminal.
- `market_data/mt5_feed.py` — `MT5Feed` (connection, exact-case-insensitive symbol
  resolution, cached `SymbolSpecification`, closed-only candles, forming candle by name,
  ticks, account snapshot), `ServerClock`, and `probe_connection`.
- `docs/mt5/SETUP.md` — installing the broker-supplied package, configuring `.env`, the
  clock question, and a troubleshooting table.
- `docs/mt5/SYMBOL_SPECIFICATIONS.md` — the assumed `US30` figures worked through by hand,
  and the checklist Phase 11 uses to replace them with measured values.
- 115 tests: `tests/unit/test_candles.py` (43) and `tests/unit/test_mt5.py` (72), against a
  hand-written fake that models the terminal's documented awkwardness.

#### Changed

- `scripts/check_architecture.py` — `MT5_ALLOWED` now names three modules, because
  `mt5_module.py` owns the import statement. A new test asserts that only that one of them
  actually contains `import MetaTrader5`.
- `cli/main.py` — `test-connection` now resolves `market_data.mt5_feed.probe_connection`
  rather than `execution.mt5_broker.probe_connection`. A read-only reachability check is a
  market-data concern, and this makes the command work in Phase 2 rather than Phase 5.

#### Fixed

- Nothing was broken; the phase added code. The two gate tests that failed on the
  `MT5_ALLOWED` and `test-connection` changes were updated deliberately, and each
  replacement asserts the new behaviour rather than merely accommodating it.

#### Decisions

- **Broker server time decides candle closure**, from the terminal's `time_current()`.
  MetaTrader 5 anchors bars and sessions to server time, which for most brokers is not UTC;
  judging closure against the local clock would be up to three hours wrong and would look
  like a working strategy. `ServerClock` implements the same `Clock` protocol as the system
  clock, so it is injected identically, and it reports `degraded` when it falls back rather
  than substituting silently.
- **The feed requests one bar more than it needs** and lets the freeze drop the forming one.
  Trusting the terminal to exclude it would make the freeze depend on behaviour this project
  does not control.
- **A missing broker field is refused, not defaulted.** An absent `trade_tick_value` or
  `trade_contract_size` makes the instrument unusable, and a guessed value would mis-size
  every position.
- **A missing `trade_mode` defaults to demo.** Fail closed.
- **The rates table is described structurally, not as a numpy array**, so numpy stays an
  optional `backtest` extra and the core remains importable with stdlib plus a YAML parser.
- **The forming candle is reachable only by calling `forming_candle()`**, and a test asserts
  that `MT5Feed` has no order methods at all, so a strategy cannot bypass the execution gate
  through the market-data layer.

#### Known limits, documented rather than hidden

- `floor_time` floors in UTC; MetaTrader 5 floors in server time. They agree for M1 and M15
  — the only timeframes this strategy uses — for every common broker offset, but **not** for
  `D1` at a non-whole-hour offset such as UTC+5:30. Both halves are stated in
  `tests/unit/test_candles.py::TestTimezoneHandling`.
- Every risk figure still rests on the synthetic specification. The real `US30` numbers are
  captured in Phase 11.
- Nothing has been executed against a real terminal. The fake models documented behaviour; a
  fake cannot prove the terminal behaves as documented.

#### Notes

- **No profitability is claimed.** No backtest has been run against real data.
- The `albrooks` cross-check of `freeze_closed_bars` is present and **skips cleanly**,
  because the extra is not installed. It is the only skip in the suite, and is acceptable
  only because `albrooks` is genuinely optional.

---

### Phase 3 — Core Strategy

#### Added

- `strategy/candle_direction.py` — the M15 direction filter, with five verdicts of which two
  authorise a trade. A doji has no direction and is not traded. A missing candle is not a
  neutral candle: `is_indeterminate` separates "could not tell" from "told, and the answer
  is no", so an operator can tell a quiet market from a broken feed.
- `strategy/entry_rules.py` — `BUY STOP = M1.high + offset_points` and
  `SELL STOP = M1.low - offset_points`, with every point conversion through
  `SymbolSpecification`. No function in `strategy/` multiplies a point count by a price.
- `strategy/signal.py` — `Decision = TradeDecision | NoTrade`. "Not trading" is a
  first-class value carrying a named reason *and* its evidence (the direction decision, the
  entry candle, the freeze report), never an exception and never a null signal.
  `check_instrument` enforces US30-only with exact, case-folded matching.
- `strategy/strategy.py` — the `StopOrderStrategy` façade. Stateless, so "what did the
  strategy believe at time *t*?" stays answerable, which is what the property test asks.
  `describe()` writes the whole running rule into the log.
- `docs/strategy/ENTRY_RULES.md` and `docs/strategy/README.md`.
- 119 tests in `tests/strategy/`, including the project's headline property.

#### Fixed

- **Entry prices were being rounded with the stop-loss rounding method, which inverts the
  sign.** `round_stop_price` is anchored on the *entry*; an entry is anchored on the
  *candle's extreme*. Reusing it moved every order a tick toward the market, so a stop would
  trigger before the level the strategy specified, and closer to market where a broker's
  `stops_level` rejection lives. `SymbolSpecification.round_entry_price` was added,
  `entry_rules.py` uses only that, and a test asserts the two disagree.
- `StopOrderStrategy` required `specification.name == settings.symbol`, which would have
  made the strategy impossible to construct on any broker that names the permitted
  instrument `US30.cash` — the common case, not the exotic one. It now accepts any
  configured alias and still refuses a genuinely different instrument.
- `NoTrade.is_indeterminate` omitted `entry_candle_forming`, which is the ordinary state of
  the world for the first second of every minute and is a shortage of information, not an
  answer.

#### The headline property

> **appending future candles cannot change a decision taken at time *t***

Stated as a `hypothesis` property over generated market data rather than as one worked
example, because an example only proves the function behaved on the case somebody thought
of. The generators vary the bar count, the reference moment's position within the series, the
M15 bodies and the input order.

Three more fall out of it: the same inputs always give the same decision; input order never
changes the decision; and a decision never uses a bar that had not closed at the reference.

Writing this property immediately earned its keep: the first version of the test helper
generated "future" bars anchored on the *last existing bar* rather than on the *reference
moment*, so it sometimes produced bars the reference could legitimately see. The property
failed, and the correct answer turned out to be that the strategy was right and the test was
wrong — which is exactly the kind of thing that is better to find here than in a backtest.

#### Decisions

- A doji is never traded, and never falls back to an earlier directional candle. "Use the
  last candle that had a direction" changes the rule from *most recent* to *most recent
  directional*, which is a different strategy. Excluded by a test.
- An instrument-policy refusal **raises** rather than returning `NoTrade`. Reaching that
  check means the configuration or the feed is wrong, and a run that silently declines to
  trade all day is the worst possible outcome.
- The strategy carries no stop loss and no take profit. Those come from configuration in the
  risk engine, so it stays possible to tell which signals carried geometry.
- The check order is instrument, direction, entry, timeframe — the order an operator would
  want to read in a log. Pinned by a test.
- `entry.candle_selection: current_forming` remains research-only. The façade exposes
  `uses_closed_candles` and `describe()` so flipping it is a visible act.

#### Notes

- **No profitability is claimed.** Nothing has been run against real market data.
- The strategy layer is pure: no clock, no I/O, no broker. All 533 tests pass with no
  terminal installed.

---

### Phase 4 — Risk Engine

#### Added

- `risk/commission.py` — `per_lot_round_trip` versus `per_lot_per_side`. The mode is read
  from configuration every time and never inferred: reading it wrong doubles the cost per
  lot and therefore halves the position size, without raising anything.
- `risk/position_sizer.py` — `percent_balance` and `fixed_lot`. The size is
  `budget / total_per_lot` and **always floors** onto the broker's volume step.
  `SizingResult` carries every intermediate figure, because "why 0.4 lots" is the first
  question asked when something looks wrong.
- `risk/stop_loss.py` / `risk/take_profit.py` — `StopLossProvider` and `TakeProfitProvider`
  protocols with fixed-points, risk-reward and signal-defined implementations, plus
  `resolve_target` implementing `signal_defined > risk_reward > fixed_points`.
- `risk/risk_manager.py` — `RiskAssessment` with 11 stable, machine-readable rejection
  codes, `stops_level` validation before sizing, and the independent
  `max_total_risk_fraction` ceiling.
- `docs/risk/RISK_MODEL.md` — the sizing derivation, the worked example, the rounding rules,
  and the full rejection-code table.
- 140 tests in `tests/risk/`.

#### The property

> **the chosen volume never carries more total risk than the budget allowed**

Stated as a `hypothesis` property over generated specifications, balances, percentages,
commission rates and distances. Alongside it: the size is always on the broker's step and
within the min/max, rounding never increases it, `total_risk == price_risk + commission`,
and the same input always gives the same size.

#### Decisions

- **The budget is divided by `total_per_lot`, not `price_per_lot`.** Sizing on price risk
  alone gives 0.5 lots carrying $53.00 of a $50.00 budget — a 6 % overshoot on every trade,
  visible in no log and raised by nothing. Worked through in `RISK_MODEL.md` §3.
- **A budget below the broker minimum is refused, not clamped.** `refuse_below_min_volume`
  defaults to `true`; rounding up to `volume_min` carries 4× the budget on these numbers.
  A risk limit that does not bind is not a limit.
- **Rejections return a stable `code`, not prose.** Prose forces every caller to parse
  English. Tested for uniqueness, snake_case, and that every refusal names a known code. The
  strings are an interface: add one rather than reword one.
- **A malformed target falls back; a malformed stop refuses.** The stop is what bounds risk,
  so a bad take profit must not block a trade that is already safe.
- **The stop is resolved before the size.** A tighter stop is cheaper and so buys a *larger*
  position — 0.8 lots at 50 points against 0.4 at 100. Deriving the size first would
  misstate the risk in whichever direction happened to be worse.
- **Margin and duplicate detection are deliberately not here.** Both need live broker state
  and belong to `execution/` in Phase 5.

#### Corrected — documentation

`HANDOFF.md` had drifted from the repository as earlier phases landed. Four claims were
stale and are corrected here, because that file is what the next agent trusts:

- "Only `validate-config` is implemented" contradicted the table directly beneath it;
  `test-connection` landed in Phase 2.
- The `MetaTrader5` allowlist was quoted as two modules; Phase 2 added `mt5_module.py`,
  which is the one that owns the `import`.
- "Phases 2 through 12" in Remaining Work, when 2 and 3 were done.
- `docs/risk/` was described as a placeholder; it now carries a real model.

#### Notes

- **No profitability is claimed.** Every figure here is arithmetic on *assumed* inputs. The
  real `US30` numbers arrive in Phase 11 and will rescale these results.
- `risk/` is pure: no clock, no I/O, no broker. All 683 tests pass with no terminal.