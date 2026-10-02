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

## Phase 4 — Risk Engine — `complete`

Implement:

* `risk/commission.py`: `per_lot_round_trip` vs `per_lot_per_side` — **done**
* `risk/position_sizer.py`: fixed-lot mode and percent-balance mode using tick value /
  tick size / contract size, never `1 point = $1` — **done**
* `risk/stop_loss.py` + `risk/take_profit.py`: `StopLossProvider` / `TakeProfitProvider`
  interfaces; fixed-points, risk-reward, signal-defined implementations — **done**
* `risk/risk_manager.py`: order validation inputs — volume bounds, stop distance, freeze
  level, margin, duplicate detection — **done**
* `1:1` R:R support and the precedence rule against the fixed 1000-point TP — **done**
* `docs/risk/RISK_MODEL.md` — **done**

Delivered:

* `risk/commission.py` — the mode is read from configuration every time and never
  inferred, because reading it wrong doubles the cost and therefore halves the size without
  raising anything.
* `risk/position_sizer.py` — the budget is compared against **total** risk; the size always
  floors onto `volume_step`; a budget too small for the broker minimum is refused.
* `risk/stop_loss.py`, `risk/take_profit.py` — three providers each, and
  `signal_defined > risk_reward > fixed_points` precedence in `resolve_target`.
* `risk/risk_manager.py` — `RiskAssessment` with 11 stable rejection codes.
* `docs/risk/RISK_MODEL.md`.
* 140 tests in `tests/risk/`.

Quality gate: **met** — 683 tests pass (1 skipped: the optional `albrooks` cross-check),
`ruff` clean, `mypy --strict` clean over `src` and `tests`, architecture reports 38 modules
and no violations.

### The property

> **the chosen volume never carries more total risk than the budget allowed**

Stated as a `hypothesis` property over generated specifications, balances, percentages,
commission rates and distances. Alongside it: the size is always on the broker's step, always
within the min/max, rounding never increases it, `total_risk == price_risk + commission`,
and the same input always gives the same size.

### Scope note

The roadmap also listed margin and duplicate detection under `risk_manager`. Both are
deliberately **not** here: they need live broker state, so they belong to `execution/` in
Phase 5. `stops_level` is checked from the specification now; the send-time rejection
handling is Phase 5's.

### Decisions made in Phase 4

1. **The budget is divided by `total_per_lot`, not `price_per_lot`.** Sizing on price risk
   alone gives 0.5 lots carrying $53.00 of a $50.00 budget — a 6 % overshoot on every trade,
   invisible. Worked through in `docs/risk/RISK_MODEL.md` §3.
2. **A budget below the broker minimum is refused, not clamped.** `refuse_below_min_volume`
   defaults to `true`; rounding up to `volume_min` would carry 4× the budget on the
   project's own numbers. A risk limit that does not bind is not a limit.
3. **Rejections return a stable `code`, not prose.** Tested for uniqueness, snake_case, and
   that every refusal names a known code.
4. **A malformed target falls back rather than refusing.** The stop is what bounds risk, so a
   bad take profit must not block a trade that is already safe. A malformed *stop* refuses.
5. **`max_total_risk_fraction` is a second, independent brake**, configured as a fraction
   with `1.0` meaning disabled, so a mistake in `percent` cannot widen it.
6. **The stop is resolved before the size, never after.** A tighter stop is cheaper and
   therefore buys a larger position — 0.8 lots at 50 points versus 0.4 at 100 — so deriving
   the size first would silently under- or over-state the risk.

---

## Phase 5 — Order Execution — `complete`

Implement:

* `execution/order_manager.py`: build, validate, place pending orders; **re-read broker
  state before every send** (idempotency) — **done**
* `execution/mt5_broker.py`: `Broker` protocol over the native MT5 API — **done**
* `execution/simulated_broker.py`: full in-memory broker (dry-run, paper, backtest) — **done**
* Magic number + comment + client tag strategy identity — **done**
* Bounded exponential backoff for safe reads; **no resend after an unknown send outcome** —
  **done**, `execution/retry.py`
* Broker error classification (retryable vs terminal) — **done**, plus a third bucket for
  the codes that mean neither

Gate: execution tests including duplicate-prevention and rejection handling, commit, push.

### Decisions taken

1. **The retcode table has three buckets, not two.** "Retryable" versus "terminal" cannot
   classify `10012` (timeout) or `10031` (connection lost), because the request may already
   have reached the venue. Those become `ExecutionUnknownError`, which is never retryable; the
   caller re-observes broker state. An **unrecognised** retcode is treated as unknown rather
   than terminal — wrong pessimistically costs a re-read, wrong optimistically costs a
   duplicate.
2. **No layer retries a send.** Not the broker, not the manager, not `retry.py`.
   `retry.py` re-raises `ExecutionUnknownError` immediately, so a placement mistakenly passed
   in as a "read" cannot be ridden out. A test asserts both the attempt count and that no
   sleep occurs.
3. **The duplicate check runs last, immediately before the send.** The gate and risk checks
   come first so a refused trade costs no broker round trip; the book read cannot then be
   stale by the time it matters.
4. **Duplicates match on the client tag, never on price.** A second implementation of the tag
   derivation would eventually disagree with the one that builds the order, so the derivation
   lives in exactly one place, `OrderIntent.client_tag_for(plan)`, and a test compares the two.
5. **The identity tag is packed into the terminal's comment, tag first.** MT5 has no
   client-order-id, and it truncates comments at 31 characters. A 20-character tag leaves
   about ten for prose — a documented trade-off, and identity is worth more than prose.
6. **`MT5Api` grew the four execution methods.** The dependency surface stays described in
   one module rather than gaining a second, private description in the execution layer.
   `AccountReader` likewise gained `specification()`, because the risk engine needs the
   broker's own contract numbers.
7. **A cancellation asks the book, not the retcode.** Its goal is that the order is not on the
   book, so an ambiguous cancel response is resolved by re-reading rather than retried.
8. **Floating P/L is gross; commission is charged once, at close.** Charging it in both places
   would show less equity than a live account has.
9. **`sleep()` moved into `infrastructure/clock.py`.** The architecture check already refused
   `import time` elsewhere; making the one legal home explicit keeps retry testable without a
   real wait.

### Known limitation

`OrderManager.place(settings=None)` skips the gate check. That is correct for `DRY_RUN` and
`PAPER`, where the venue is the simulator, but it means a caller that hands it a real broker
and omits `settings` would route orders ungated. Phase 7's composition root should make the
environment explicit rather than optional. Not a defect in any current call path.

### Not verified

The `metatrader5` package is installed but no terminal has been connected, so the wire
values, the retcode
table and the tag encoding have never met a real terminal. `test-connection` correctly exits 3
with `package_installed: false`. No live order has been placed.

---

## Phase 6 — Position Management — `complete`

Implement:

* `trailing/break_even.py`: configurable trigger, broker minimum distance, freeze level,
  spread awareness, idempotent (no repeated modify on every tick) — **done**
* `trailing/trailing_stop.py`: BUY `bid − d`, SELL `ask + d`, **monotonic** — **done**
* `execution/position_manager.py`: BE → trailing — **done**; close detection is Phase 7, which
  owns the loop that notices a position is gone
* Property tests over generated price sequences:
  BUY `sl_new >= sl_old`, SELL `sl_new <= sl_old` — **done**, over both `hypothesis` walks and
  adversarial run-up-then-reversal paths

Gate: BE/trailing tests + property tests, commit, push.

### Decisions taken

1. **Monotonicity is structural.** The provider returns no proposal when the level is not an
   improvement, rather than clamping a backwards move after the fact. There is no code path
   that can emit one, so the guarantee does not depend on a check being reached.
2. **The trigger is measured on the exit side** — `bid` for a BUY, `ask` for a SELL. Measuring
   on the mid arms break-even while the position is still underwater by the spread, and a stop
   at entry is then hit immediately: a flat trade becomes a certain loss. Arithmetic, not a
   filter.
3. **Break-even wins outright; trailing is only consulted when it proposed nothing.** Both are
   computed against the same position, so applying both in one tick applies a trailing level
   derived from a stop that has already moved. The property test found this, not review.
4. **`stops_level` and `freeze_level` are checked separately**, because they constrain
   different things — where a stop may be placed, versus whether a position may be modified at
   all — and satisfying the first does not satisfy the second.
5. **Floating point never appears.** With `point = 0.1`, ten points is one price unit, so the
   100-point trailing distance is `10.0` in price terms. Every conversion goes through
   `points_to_price`.
6. **A stop modification is never retried**, on the same reasoning as an order placement: the
   stop may have moved, and a second request is interpreted against a state that no longer
   exists. Reads are still retried.

### Defects found and fixed during Phase 6

Both were silent — every outcome still carried a plausible reason:

* `BreakEvenRefusal.INSIDE_STOPS_LEVEL` did not exist. The class named it
  `VIOLATES_STOPS_LEVEL`, so **every** break-even modification past the trigger would have
  raised `AttributeError`. Caught by mypy.
* The broker-limit helpers returned `bool | None` and were tested with `is not None`, so the
  "false" case and the "not applicable" case were the same value and **every** modification was
  refused. Caught by a test asserting that a modification *succeeds* on a normal
  specification — the assertion that turns out to be the load-bearing one.

### Known limitation

`PositionManager` has no close detection. It evaluates the positions it is handed, so it
notices nothing about a position the venue has closed. That belongs with the lifecycle, which
already owns "position closed → place a replacement" and has the state to tell a close from a
disappearance. Tracked in `HANDOFF.md`.

### Not verified

No terminal has been connected, so the `stops_level` and
`freeze_level` semantics encode documented broker behaviour that has never been observed. A
broker that behaves differently would show up as refusals where a modification was expected —
the safe direction to fail, but unproven.

---

## Phase 7 — Lifecycle and Recovery — `complete`

Implement:

* `lifecycle/state_machine.py`: table-driven transitions with a listener — **done**
* `lifecycle/trade_lifecycle.py`: `WAITING_FOR_SIGNAL` → … → `POSITION_CLOSED` → replacement
  order — **done**; the replacement is placed by the *same* code path as a first entry
* `infrastructure/persistence.py`: write-intent-before-act ledger, atomic writes,
  refuse-to-overwrite guard — **done**
* Restart recovery: reconcile local state against live broker state (broker authoritative) —
  **done**, `lifecycle/recovery.py`
* Automatic replacement order with full re-evaluation — **done**
* Duplicate protection across duplicate ticks, reconnects, restarts, retries — **done**; four
  distinct guards, each catching a different route to the same catastrophe

Gate: lifecycle + recovery tests, commit, push.

### Decisions taken

1. **The gate is chosen by the venue; the environment is always stated.** Phase 5's
   `place(settings=None)` skipped the gate, which was right for `DRY_RUN`/`PAPER` and wrong
   everywhere else. `place()` now requires `settings`, and *which* gate applies is decided once
   at construction. The new `SimulatedGate` is **not** an open gate — it refuses `LIVE`, so a
   composition mistake wiring it to a real broker fails closed.
2. **`VERIFYING` cannot reach `VALIDATING`.** Asserted as a property of the table rather than
   as a code path: nothing may be sent from the state that exists *because* a send's outcome is
   unknown, until broker state has been read.
3. **The ledger never starts fresh.** `load` raises on an unreadable ledger rather than
   returning an empty one. An absent file is a genuine first run; an unreadable file is a lost
   idempotency record, and those are not the same thing.
4. **`awaiting_confirmation()` is wider than `unresolved()`.** An ambiguous send is recorded
   `UNKNOWN`, which *is* an outcome. Treating it as settled would leave the lifecycle in
   `VERIFYING` with nothing to verify — so those entries are re-observed and *upgraded* to
   `placed` when the broker confirms them. The one case where a recorded outcome may be
   replaced, and it is safe precisely because `unknown` is a statement of ignorance rather
   than a fact about the venue.
5. **A "placed" entry with an empty book is not a contradiction.** It is the ordinary
   filled-then-closed case. An earlier version raised `StaleStateError` for it, which would
   have halted the system on every take-profit after every restart. What *does* halt is
   **unattributable exposure**: a position or working order whose identity matches no ledger
   entry.
6. **The replacement is the same path as a first entry.** `_on_position_closed` journals and
   stops; `POSITION_CLOSED` is quiet, so the next tick's decision is placed by `_enter` through
   the ordinary `place_order` — same ledger write, same book re-read, same gate. Two paths
   would have to agree; one cannot drift.
7. **Close detection is a comparison, not an event,** and deliberately does not distinguish a
   stop-out from a take-profit. Recovery settles what happened.
8. **`_adopt_open_position` walks real edges rather than resetting,** because the machine must
   always be able to describe the exposure it actually holds.

### Defects found and fixed during Phase 7

All five were found by a gate or a test, and four were silent:

* `TransitionListener` never inherited `Protocol`, making it a *nominal* base class. No
  duck-typed listener could satisfy it — the type checker caught it.
* `_adopt_open_position` returned after the first reachable step, so it never reached
  `POSITION_OPEN` from a resting state.
* The lifecycle recorded an intent and *then* asked the ledger whether it had been sent,
  finding the entry it had just written and refusing every placement as a duplicate of itself.
* `settled_intents` consulted only `unresolved()`, so an entry already recorded `UNKNOWN` was
  never re-observed and `VERIFYING` had nothing to verify.
* A recovery test failed because the contradiction rule fired on an ordinary
  filled-then-closed order; the rule was wrong, not the test.

### Known limitation

The lifecycle has no clock of its own and requires one to be injected — refusing rather than
falling back to `datetime.now()`, which the architecture gate forbids in this layer. A caller
that forgets is told so explicitly.

### Not verified

No order has been placed, so the ledger has never been written by a real crash. Atomicity and
refuse-to-overwrite are proved against an injected `OSError`, not a power cut.

---

## Phase 8 — Al Brooks Integration — `removed`

**Removed, with the reason recorded rather than quietly deleted.**

The Phase 0 audit found two unrelated projects sitting in sibling directories on the
development machine — `auto-trade` and `al-brooks-price-action-engine` — and treated them as
"sibling projects" of this one. Phase 8 was then planned to integrate the second. Nobody asked
for it: the request was a simple system that places pending orders, and a third-party signal
source is a decision about *strategy*, not about plumbing.

The whole phase was built, then reverted. What survives is the seam it needed — the strategy
is a façade over frozen settings, so another source could be added later without touching the
layers below. The Al Brooks code itself, its tests and its documentation are gone.

Mixing in another method is a **future** intent, not this phase. When it happens it should be
a deliberate, measured decision with the baseline still available to compare against — not a
default that quietly takes over.

---

## Phase 9 — Make It Run — `complete`

The one deliverable that matters: **`run --dry-run` actually runs.**

After seven phases the system had 1082 passing tests and could not start. Every layer was
built and none of them were wired together, so the honest state was "a large pile of
well-tested parts and no program". That is what this phase fixes.

Delivered:

* `application/service.py` — `build_service(...)`, the composition root the CLI expected
  since Phase 1. Wires the venue, strategy, risk, order manager, position manager and
  lifecycle, and runs a bounded number of cycles.
* `infrastructure/persistence.py` — `JsonStateLedger`, the name the CLI expected, with
  `.journal(limit=)` and a context manager.
* `run --dry-run` against `SimulatedBroker` with no MetaTrader 5 installed, printing what
  it did on every cycle. `run --candles PATH` reads real history;
  `run --m15 PATH` supplies the direction series separately.
* `run --live` is refused **by name**, with the reason, rather than failing obscurely.
* **The rule stays configurable.** Every number lives in `config/default.yaml`.

`run` and `journal` now exit 0. `status`, `diagnostics` and `backtest` still exit 4 and say so.

### What running found that testing did not

Four bugs, all invisible to 1082 passing component tests:

* **Every order was born expired.** `OrderManager.intent_for(plan, now=...)` passed `now`
  through as the *expiration*, so an order created at time *t* expired at *t*. The unit tests
  all called it without `now`, so that branch was never taken. The expiry now comes from
  `order.lifetime_seconds`.
* **The lifecycle was moved into the wrong enum.** `PositionManager` returns a
  `PositionState`; the machine expects `LifecycleState`. Both are `StrEnum`, so the wrong one
  was stored silently and raised `AttributeError` three cycles later. There is now an
  explicit translation table.
* **The strategy→risk bridge did not exist.** The strategy produces a `TradeDecision`; the
  order manager needs a sized `TradePlan`. Nothing connected them, so every decision counted
  as "not a plan" and **no order was ever placed**. It lives in the composition root, because
  neither layer may depend on the other.
* **The report showed the plan beside the action**, so a `trailed` line quoted the intended
  level rather than the stop the broker actually held. It is now read back from the broker.

Two tests also asserted *about the machine* rather than about the code — "MetaTrader5 is
genuinely not installed in the test environment" — which stopped being true when
`metatrader5 5.0.6231` appeared. Both now force the import to fail.

**The lesson, recorded because it is the whole point of this phase:** a green suite over
unconnected modules is a statement about the modules. 1082 tests passed while the program
could not start.

---

## Phase 10 — Backtest and Statistics — `complete`

Replay recorded candles through the *same* trading stack and report the distribution rather
than a single number.

Built as `backtest/replay.py`, `backtest/statistics.py` and `backtest/runner.py`, plus
`backtest --data PATH`. On the committed sample: 19 closed trades, 9 winners, 10 losers,
+462.00 on 10 000, of which 45.60 was commission.

### Two things it refused to do

**Generate candles.** `backtest` with no `--data` exits 2 and says so. A command that invents
a price series and prints a profit figure produces a number with no provenance, which is the
one artefact this project exists not to create. `run --dry-run` with no `--candles` is the
command that proves the wiring; it says on its face that it is not evidence.

**Report an equity-curve statistic.** No Sharpe ratio, no volatility, no "expected value per
trade" in the abstract. Each needs a sampling model for the *untraded* periods, and a sampled
curve is a claim about the future wearing the costume of a measurement. What is reported —
trade count, win rate, profit factor, drawdown, exits by reason, and the number of trades still
open at the end — is all countable from what the venue actually did.

`win_rate` and `profit_factor` are `null` rather than `0` when undefined. "No trades" and
"every trade lost" are different facts.

### Three bugs the replay found, none of them in the replay

**The venue was not charging commission.** `configure_specification` takes a `commission`
argument and neither the dry run nor the replay passed it, so the simulated venue charged
nothing. The risk engine sizes the position *net* of `commission_per_lot`; a free venue then
books the full gross as profit. Every P/L figure in the project overstated the result by
exactly the cost the sizing had already accounted for — in the direction that flatters the
strategy. Found because a backtest reported `total_commission: 0.0` while the config said 6.0.

**The venue's clock never moved.** `SimulatedBroker` defaults to a frozen 2026-01-01 and only
advances when something calls `set_time`. The replay did not, so every `opened_at` and
`closed_at` was that date and every trade duration read as 0 seconds. The report looked
complete. `set_time` is called per bar now, and two tests pin it.

**A closed trade had no record.** `close()` popped the position and adjusted the balance, so
the only way to learn what had been traded was to diff the open-position list between ticks —
which cannot tell a stop-out from a take-profit, cannot recover the exit price, and
misattributes a closure to whichever tick noticed it. `SimulatedBroker.history()` and the
immutable `ClosedTrade` now exist, written at the moment of closure while the levels that
triggered it were still known. Every statistic is computed from that and nothing else.

### One structural constraint worth knowing

`backtest` sits *inside* `application` in the layer order, so it cannot import
`TradingService`. The walk-forward loop is written again over the same collaborators rather
than reaching across the boundary, and `load_candles_csv` / `aggregate` moved from
`application/service.py` to `market_data/candles.py` so both layers can reach them. The
strategy→risk bridge is therefore duplicated in two places — which is a weaker guarantee than
sharing it, and is called out in the code rather than hidden.

### A fixture, and why its numbers look like that

`tests/fixtures/us30_m1_sample.csv` is committed, with `scripts/make_sample_candles.py` and a
fixed seed beside it, and the file's first two lines say it is synthetic. The regime length
was tuned, and the tuning is recorded in the script: a per-bar random walk produces **zero**
trades (the M15 filter correctly rejects a series with no direction), which teaches a reader
nothing about whether the command works. Forty-five-bar regimes give 19 trades with a win rate
*below* 50% — credible rather than flattering, and it exercises both the trailing stop and the
take profit.

---

## Phase 11 — Demo Validation — `pending`

A real MetaTrader 5 **demo** account. The `metatrader5` package (5.0.6231) is now installed
on the development machine, but no terminal has been connected, so every wire value, retcode
and specification this project uses is still unverified against reality — the classification table and the broker-limit semantics in particular.

Implement:

* capture the real `US30` specification and replace the assumed one
* verify the retcode table against actual failures
* run the dry run against a demo terminal and compare it to the backtest

Nothing in this project should touch a **live** account until Phase 11 has run for long
enough to mean something. `LIVE` is gated three times over and that is not decoration.

---

## Later — combining with another method

Recorded because it is a stated intent, not because it is planned. When it happens:

* the baseline must remain available, so the two can be compared rather than assumed additive;
* a signal source may **veto**, and it may not quietly become the only source;
* every switch defaults to off, and a switch that is off changes nothing at all.

`docs/architecture/ARCHITECTURE.md` §2 keeps `integrations` as a layer for exactly this, and
`domain/interfaces.py` already declares the protocols a new source would implement.

---

## Out of scope (documented, not forbidden)

* Multi-instrument trading — the instrument policy is US30-only by specification
* MQL5 EA implementation — only if a future phase proves the native Python API is
  unreliable for order placement (§5.1 of the audit)
* Tick-by-tick latency arbitrage