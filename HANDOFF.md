# Handoff

> **Mandatory.** Every AI agent or human continuing this project must read this file,
> then `ROADMAP.md`, then `README.md`, **before** changing anything.
>
> Read order: **`HANDOFF.md`** → `ROADMAP.md` → `README.md` →
> `docs/architecture/ARCHITECTURE.md` → `docs/strategy/BASELINE.md` → this file again.

---

## Current Phase

**Phase 11 — Demo Validation — in progress. The specification is measured; the account is
not currently valid.**

Two facts to read before anything else:

1. **`tick_value` was wrong by a factor of ten for ten phases.** It was hand-written as `1.0`;
   Alpari's US30 is `0.1`. Every reported P/L figure was ten times too large and every
   position was a tenth of the intended risk. The specification is now measured, lives in
   `market_data/symbols.py` in one place with its capture date, and is pinned by a test.
2. **Demo account 53137121 is no longer valid.** The terminal authorised at 12:16 and has
   failed with `Invalid account` since 13:37, across a restart. A working account is needed to
   continue; no code change will fix this one.

### The first real backtest: +16%, and the number has been wrong in both directions

30 022 real US30 M1 bars, one month: **29 trades, 14 winners, +1600.82 on 10 000 (+16.0%), profit
factor 3.86.** Twenty-eight exits via the trailing stop, one take profit.

**The first run on the same data reported −219.82 and 9 trades.** That was not the strategy — it
was `aggregate()` grouping M1 bars by position instead of by timestamp. Alpari's export starts
at 11:42 and has weekend and daily-session gaps, so 1 822 of 2 001 "M15" candles were not on a
15-minute boundary and 49 spanned a session break. The direction filter had been reading
candles that never existed. Fixed, verified against the terminal's own M15 bars, all 2 009 now
agree exactly.

**Do not treat +16% as a result — and now there is a measurement.** The replay publishes only
each candle's close, so the trailing stop never sees an intrabar extreme. Measured both ways on
the same data:

| | trades | net | profit factor |
| --- | --- | --- | --- |
| close only | 22 | +1859.46 | 6.57 |
| high and low published | 3 | -65.96 | 0.30 |

**The entire result sits inside that bracket, so this harness cannot currently measure the
strategy's edge.** Neither end is right: close-only is optimistic, and publishing the extremes
lets a position's own entry bar trigger the stop from a move that preceded the fill. OHLC bars
do not record which came first.

The fix is tick data — the terminal exports it, and then the ambiguity is gone. Until then,
report the bracket, never the optimistic end alone.

**Do not change the rule yet.** Two results in opposite directions from the same code means the
candle layer, not the strategy, is what has been moving.

### What Phase 11 must still do

1. **Get a valid demo account**, put `SOS_MT5_LOGIN` and `SOS_MT5_SERVER` in `.env`, and run
   `diagnostics`. Confirm `specification_matches_recorded: true`. A CFD's tick value can move
   with the underlying index, so this is a live check, not a formality.
2. **No password is needed, and none is stored.** An already-signed-in terminal attaches to
   its own session; `EnvironmentSettings` has no password field by design, and reading a symbol
   specification must not require a broker credential on disk.
3. **Nothing has ever placed an order.** The retcode table was written from documentation, and
   the whole execution path is exercised only against a simulator.

## What the last three phases cost, and why

| Phase | What it found |
| --- | --- |
| 9 | the idempotency guarantee was dead code, and 1116 tests passed |
| 10 | the venue charged no commission; the clock never moved; closed trades were never recorded |
| 11 | the tick value was 10x wrong, and the rule loses money on real data |

Every one of them was found by *running* the thing rather than by reading it, and two of the
three were invisible to a large green suite. The recurring lesson is in
[Testing rules](#testing-rules-this-project-has-now-learned-twice) below and it has now cost
three phases: **check a number against a second, independent number.** `total_commission`
against the config; `net_profit` against the balance change; the reported P/L against the
specification the terminal actually reports.

## Read this before Phase 11

Phase 10 found three bugs by building a thing that reports numbers. All three were in code
that had been "done" for phases, and all three made results look **better** than they were.
That direction is the one to worry about: a bug that loses money gets noticed.

### The venue was not charging commission

`configure_specification` takes a `commission` argument. Neither the dry run nor the replay
passed it, so `SimulatedBroker` charged nothing. The risk engine sizes the position *net* of
`commission_per_lot` (6.0 per lot, round trip) — so a free venue books the full gross as
profit, and every P/L figure in the project overstated the result by exactly the cost the
sizing had already accounted for.

Found because a backtest printed `total_commission: 0.0` while the config said 6.0. **Check
this class of thing by reconciling two independent numbers**, not by reading one: the test
now asserts `net_profit == ending_balance - starting_balance` and that each trade's charge
equals `rate × lots` under the configured mode.

### The venue's clock never moved

`SimulatedBroker`'s clock defaults to a frozen `2026-01-01` and only advances when something
calls `set_time`. The replay did not, so every `opened_at` and `closed_at` was that date and
every trade duration read as **0 seconds**. Nothing crashed; the report looked complete.

The replay calls `set_time(reference)` per bar. Two tests pin it, and the second one only
exists because the first was too weak — see the vacuous-test note below.

### A closed trade had no record at all

`SimulatedBroker.close()` popped the position and adjusted the balance. The only way to learn
what had been traded was to diff the open-position list between ticks, which cannot tell a
stop-out from a take-profit, cannot recover the exit price, and attributes a closure to
whichever tick noticed it.

Now: `SimulatedBroker.history()` and the frozen `ClosedTrade`, written at the moment of
closure while the levels that triggered it were still known. **Every statistic in
`backtest/statistics.py` is computed from that and nothing else** — no plan, no intended exit,
no inference. If you add a statistic, it reads `ClosedTrade` or it does not go in.

`_exit_reason` classifies at closure time, not afterwards, because by then a trailed stop has
moved and the original level is gone. It returns `stop_loss`, `take_profit`, or `closed` — and
never guesses when neither level explains the exit.

## What Phase 11 must preserve

1. **Never resolve an intent by assumption.** An unresolved entry is a question for the broker.
   Assuming it was not sent is how a duplicate order is created.
2. **The retcode table has never met a real failure.** Every entry in it was written from
   documentation. Capture the actual retcodes a demo account produces and correct the table
   against them; do not add entries from docs alone.
3. **Replace the assumed specification with the measured one.** Point `0.1`, tick value `1.0`
   per lot, contract size `1.0` are guesses, and every money figure in a replay scales with
   the tick value. A backtest that looks profitable on assumed numbers may be a different
   number entirely on the real contract. Capture it from the terminal and re-run the sample
   fixture before drawing any conclusion.
4. **Re-run the sample fixture after the specification changes**, and expect the money figures
   to move. The trade *count* should not change; if it does, something else is wrong.
5. **A demo run is a second run over a venue that persists.** That is the shape in which both
   the Phase 9 and the Phase 10 defects lived. Test the restart path deliberately.

## Testing rules this project has now learned twice

**A test that exercises state must exercise it twice, against the same file.** 1116 tests
passed while the idempotency guarantee was dead code, because every test built a fresh
`tmp_path` and ran once. `TestAcrossTwoRuns` and `TestReplayedTwice` exist for this.

**A regression test that cannot fail is worse than no test.** Four of the first eight written
for Phase 9 passed against the bugs they were written for:

| How it lied | Why it passed |
| --- | --- |
| compared the on-disk ledger to the in-memory one | those agree even when both are wrong |
| compared entry *counts* across two runs | run two re-records the same client tag, so the counts match |
| `if not detail.startswith(...): continue` | with the bug, every detail was skipped and the loop body never ran |
| passed a `--config` flag the CLI does not have | the command read a different ledger than the test populated |

And one in Phase 10: a "duration is real" test that passed with the clock fix reverted,
because the assertion was on a trade list that was empty in the reverted state.

**So: revert the fix, run the test, confirm it fails.** That check is in the handoff rather
than done once, and it should be repeated for every new regression test. The Phase 9 and
Phase 10 checks are both re-runnable from the recorded fix/test pairs.

## Layering note for whoever adds to the backtest

`backtest` sits *inside* `application` in `scripts/check_architecture.py`'s layer order, so it
**cannot import `TradingService`**. The walk-forward loop in `backtest/replay.py` is therefore
a second implementation over the same collaborators, and the strategy→risk bridge
(`_size`) is duplicated in two modules.

That is a real cost and it is stated in the code rather than hidden. `load_candles_csv` and
`aggregate` were moved to `market_data/candles.py` specifically so both layers could reach
them without crossing the boundary. If a third copy of the bridge appears, extract it into a
layer both can import instead.

## How Phase 9 shipped a broken guarantee, and how it was found

Kept because the failure mode is more useful than the fix. The idempotency guarantee was dead
code for the whole of Phase 9, and 1116 tests passed while it was.

**What went wrong.** `build_service` built the ledger with `StateLedger(path)` — the
constructor — instead of `StateLedger.load(path)`. The constructor takes a path and starts
empty; it never reads the file. So on every run `recover()` was handed a blank ledger and
found nothing to verify, and the first `record` flushed an empty file over every intent
already on disk. Phase 7's entire restart-recovery path was dead code in the only place that
called it.

**Why the suite missed it.** Every Phase 7 and Phase 9 test built its own ledger in a fresh
`tmp_path` and ran once. Nothing in 1116 tests ever had two runs share a file, which is the
only shape in which the defect is visible. It is correct in isolation and wrong in sequence,
and the suite only ever tested one run.

**Found by** running the same command twice, after the Phase 9 commit was pushed and the demo
had been shown to work. Not by a test.

### The ledger's lifetime must match the venue's lifetime

The fix raised a second question the first one hid: with `load()`, a dry run correctly read
what the last run wrote, and three identical dry runs gave `placed`, then `already_recorded`.

The answer was not to weaken the duplicate check. `SimulatedBroker` is constructed fresh every
run and remembers nothing, so a durable ledger beside it is a lie — it claims an identity
reached a venue that has never heard of it, and `was_sent()` would then answer *yes* for an
order a live run never sent. The system would refuse to trade, silently. A ledger's value is
that it and the venue agree on what was sent, so their lifetimes have to match.

- `DRY_RUN` gets an in-memory ledger. Every rule still holds — record before send, refuse a
  repeated tag, settle once. Only `flush` stops writing.
- The environment is otherwise part of the path: `state-dry_run.json`, `state-paper.json`, and
  unsuffixed `state/state.json` for `LIVE`, which a dry run never writes.
- An **explicitly configured** `state.path` is honoured verbatim and *is* persisted, because
  that is how restart recovery gets exercised: two runs pointed at one file must share it, and
  the second must decline to re-send.

### `journal` could delete the journal

`_cmd_journal` had the same construct-instead-of-load defect, and its context manager flushed
on exit. So *asking what had happened* replaced the file with an empty one. It loads now, and
reports which ledger it read — `state/state.json` is the LIVE ledger, so a journal that
silently showed nothing would read as "no trades" rather than "wrong file".

A later fix in Phase 10: that same context manager was creating a ledger it had just reported
as absent, because it flushed unconditionally. It now flushes only when something was written.

### `config/default.yaml` stating the default is still the default

`state.path` is written in the shipped YAML, so "was this configured?" could not be answered
with `path is not None` — that classified the project's own default as an operator override
and left every ledger unscoped. It is compared against `DEFAULT_STATE_RELATIVE` now. A test
caught this one: the first version of the fix passed every unit test and did nothing in
practice.

### Two smaller ones

- The report synthesised `"hold"` when a step carried no actions, while `_describe` read the
  same emptiness as `""`. So a line could read `hold` beside `planned 0.4 lots at 40006.3`
  while the venue held an order at 40005.7. One `_action_of` decides now, and a `hold` names
  the resting order or the open position.
- `synthetic_candles`' docstring described a 30/15/30/15 series the code did not build. It
  builds 40 up / 5 down, and both numbers are load-bearing; the docstring says why now.

## How Phase 10 found three more, none of them in the backtest

Phase 10 built the first thing in this project that reports *numbers*, and the numbers were
wrong in the direction that flatters a strategy. That is the direction to worry about: a bug
that loses money gets noticed.

**The venue was not charging commission.** `configure_specification` takes a `commission`
argument and neither the dry run nor the replay passed it, so the simulated venue charged
nothing. The risk engine sizes the position *net* of `commission_per_lot`, so a free venue
books the full gross as profit, and every P/L figure overstated the result by exactly the cost
the sizing had already accounted for. Found because a backtest printed
`total_commission: 0.0` while the config said 6.0.

**The venue's clock never moved.** `SimulatedBroker` defaults to a frozen 2026-01-01 and only
advances when something calls `set_time`. The replay did not, so every `opened_at` and
`closed_at` was that date and every trade duration read as 0 seconds. Nothing crashed; the
report looked complete.

**A closed trade had no record.** `close()` popped the position and adjusted the balance, so
the only way to learn what had been traded was to diff the open-position list between ticks —
which cannot tell a stop-out from a take-profit, cannot recover the exit price, and attributes
a closure to whichever tick noticed it. `SimulatedBroker.history()` and the frozen
`ClosedTrade` exist now, written at the moment of closure while the levels that triggered it
were still known.

**The lesson, which is the same one twice:** check a number against a second, independent
number. `total_commission` against the config; `net_profit` against
`ending_balance - starting_balance`; a trade's charge against `rate × lots`. Reading one
number and believing it is how a plausible wrong answer survives.

## Phase 8 — Al Brooks Integration: removed

Built, then reverted. The Phase 0 audit found two unrelated projects in sibling directories on
the development machine and planned an integration with one of them; nobody asked for it. A
third-party *signal source* is a decision about strategy, not about plumbing. The code, its
tests and its documentation are gone.

The seam it needed survives — the strategy is a façade over frozen settings, so another source
could be added later without touching anything below.

## What Phase 10 was asked to preserve, and whether it held

The list Phase 9 left behind, checked against what was built. Kept because a rule that was
written down and then quietly dropped is worse than one that was never written.

1. **Every send goes through `place_order`'s ordering** — held. `backtest/replay.py` drives
   the same `TradeLifecycle` against the same `SimulatedBroker`; there is no second path.
2. **`VERIFYING` must never reach a placement without a broker read** — held, untouched.
3. **Unresolved *and* unknown intents are both re-observed** — held, untouched.
4. **The gate is chosen by the venue and `settings` is always passed** — held;
   `SimulatedGate` refuses `LIVE`.
5. **The replay must not gain a forming-bar concept** — held. The only thing that reaches the
   strategy is `freeze_closed_bars` against a reference that advances one bar per cycle, and
   the strongest test of it is that replaying a 100-bar prefix gives the *same* 100 steps as
   replaying 400 bars.
6. **A backtest is a second run, so it inherits every bug above** — held, and it is why
   `TestReplayedTwice` exists.
7. **Statistics must come from the venue's own records, not the plans** — held, and it forced
   `SimulatedBroker.history()` into existence, which is the right outcome.
8. **The synthetic series is not evidence** — held. `backtest` refuses to run without
   `--data`, and the committed fixture says in its own first two lines that it is invented.

---

## Phase 9 — Make It Run — COMPLETE

* `application/service.py` — `build_service`, `TradingService`, the strategy→risk→plan
  bridge, `load_candles_csv`, `synthetic_candles`, `aggregate`.
* `infrastructure/persistence.py` — `JsonStateLedger`, the name the CLI expected since
  Phase 1, with `.journal(limit=)` and a context manager.
* `cli/main.py` — `run --candles PATH` and `run --m15 PATH`; `--live` refused by name.
* `tests/application/test_service.py` — 31 tests, all by *running* the assembled system.
* `run` and `journal` now exit 0. `status`, `diagnostics`, `backtest` still exit 4 honestly.

### Bugs only running could find

All four were invisible to 1082 passing component tests:

* **Every order was born expired.** `OrderManager.intent_for(plan, now=...)` passed `now`
  straight through as the *expiration*, so an order created at time *t* expired at *t*. The
  unit tests all called it without `now`, so the branch was never taken. The expiry now comes
  from `order.lifetime_seconds`, with `now` only anchoring it.
* **The lifecycle was moved into the wrong enum.** `PositionManager` returns a
  `PositionState`; the lifecycle stored it in a machine that expects `LifecycleState`. Both
  are `StrEnum`, so it was accepted silently and raised `AttributeError` three cycles later
  on `quiet`. There is now an explicit `PositionState → LifecycleState` table.
* **The strategy→risk bridge did not exist.** The strategy produces a `TradeDecision`; the
  order manager needs a sized `TradePlan`. Nothing connected them, so every decision was
  treated as "not a plan" and no order was ever placed. It lives in the composition root
  because neither layer may depend on the other.
* **The report showed the plan next to the action.** "planned 0.4 lots at 40007.2" printed
  beside a stop that had moved to 40011.7. The detail is now read back from the broker.

### Also fixed

Two tests asserted *about the machine* rather than about the code: "MetaTrader5 is genuinely
not installed in the test environment". True when written, false once `metatrader5
5.0.6231` was installed — and the tests then failed while saying nothing useful. Both now
force the import to fail, so they exercise the path on every machine.

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
  property for the freeze.

### Phase 3 — Core Strategy

* `strategy/candle_direction.py` — the M15 direction filter: five verdicts, two of which
  authorise a trade. A doji has no direction; a missing candle is not a neutral candle.
* `strategy/entry_rules.py` — `BUY STOP = M1.high + offset_points`,
  `SELL STOP = M1.low - offset_points`, every point conversion through the specification.
* `strategy/signal.py` — `Decision = TradeDecision | NoTrade`, plus the US30-only check.
* `strategy/strategy.py` — the stateless `StopOrderStrategy` façade.
* `docs/strategy/ENTRY_RULES.md` and `docs/strategy/README.md`.
* 119 tests in `tests/strategy/`, including the headline no-look-ahead property proved on
  the **whole decision** rather than only on the freeze.

### Phase 4 — Risk Engine

* `risk/commission.py` — `per_lot_round_trip` versus `per_lot_per_side`, read from
  configuration and never inferred.
* `risk/position_sizer.py` — `percent_balance` and `fixed_lot`. The budget is compared
  against **total** risk, the size always floors onto `volume_step`, and a budget below the
  broker minimum is refused rather than rounded up.
* `risk/stop_loss.py` / `risk/take_profit.py` — `StopLossProvider` / `TakeProfitProvider`
  with fixed-points, risk-reward and signal-defined implementations, and the
  `signal_defined > risk_reward > fixed_points` precedence rule.
* `risk/risk_manager.py` — `RiskAssessment` with stable, machine-readable rejection codes.
* `docs/risk/RISK_MODEL.md`.
* 140 tests in `tests/risk/`, including **total risk never exceeds the budget** as a
  `hypothesis` property.

### Phase 5 — Order Execution

* `execution/gates.py` — `OrderGate`, `CloseGate`, `LiveInterlock`, and
  `describe_safety` / `require_configured`. Both gates default to **closed**; `LIVE` needs
  three independent switches and any two are not enough.
* `execution/order_manager.py` — idempotent placement. Refusals are **values**
  (`PlacementOutcome`, `RefusalCode`), not exceptions, because refusing a duplicate is
  correct behaviour that happens routinely.
* `execution/mt5_broker.py` — the native MT5 `Broker`, `classify()`, and the comment
  encode/decode that carries the identity tag through a 31-character field.
* `execution/simulated_broker.py` — the in-memory venue: real bid/ask, pending stops, SL/TP
  settlement, magic numbers, commission. Not a test double; it is what `DRY_RUN` runs on.
* `execution/retry.py` — bounded exponential backoff for **safe reads only**.
* `docs/execution/README.md` and `docs/execution/EXECUTION.md`.
* 145 tests in `tests/execution/`, including the two headline properties: *the same plan
  placed twice yields one order*, and *an unknown send outcome is never resent*.

### Phase 6 — Position Management

* `trailing/break_even.py` — `ConfiguredBreakEvenProvider` plus `exit_price_for`. The trigger
  is measured on the side a close would actually get, so break-even cannot arm while the
  position is still underwater by the spread.
* `trailing/trailing_stop.py` — `TrailingStopProvider`: `bid − d` for a BUY, `ask + d` for a
  SELL, on the tick grid, with `min_step_points` for idempotency.
* `execution/position_manager.py` — break-even then trailing, one `modify_position` per real
  change, `ActionKind` and `PositionAction` as the journal-facing vocabulary.
* `docs/trailing/README.md` and `docs/trailing/TRAILING_MODEL.md
docs/lifecycle/README.md
docs/lifecycle/LIFECYCLE.md`.
* 87 tests in `tests/trailing/`, including monotonicity proved over `hypothesis` walks **and**
  adversarial run-up-then-reversal paths.

### What Phase 7 must preserve

Three invariants, each of which a phase-7 loop could plausibly break:

1. **`ExecutionUnknownError` is never retryable**, including for a *stop modification*. The
   stop may have moved; a second request is interpreted against a state that no longer exists.
   `PositionManager` deliberately calls `modify_position` once and lets exceptions propagate.
2. **Break-even wins outright.** Both rules are computed against the same position, so applying
   both in one tick applies a trailing level derived from an already-moved stop — for a SELL
   just pulled down to entry, that lands *above* it. The property test found this.
3. **An ordinary tick must cost no broker write.** Both providers are idempotent; do not add a
   path that re-proposes a level already in place.

### Carried into Phase 7 — both now closed

Two limitations were carried forward from Phases 5 and 6. Both are closed:

* `OrderManager.place(settings=None)` skipped the gate. **Closed**: `settings` is now a
  required keyword argument, and which gate applies is decided once at construction by the
  venue — `OrderGate` for real, the new `SimulatedGate` for simulated. `SimulatedGate` refuses
  `LIVE`, so a composition mistake wiring it to a real broker fails closed.
* `PositionManager` had no close detection. **Closed**: `TradeLifecycle._detect_closes`
  compares the book against the last observation, and Phase 7 owns the replacement path.

### Phase 7 — Lifecycle and Recovery

* `infrastructure/persistence.py` — `StateLedger`, `LedgerEntry`, `atomic_write_json`.
  Write-intent-before-act, atomic replace with `fsync`, refuse-to-overwrite, never start fresh.
* `lifecycle/state_machine.py` — `TRANSITIONS`, `LifecycleMachine`, `TransitionListener`,
  `describe_table`. The reachable set is data, so it is printed, reviewed and asserted.
* `lifecycle/recovery.py` — `Reconciler`, `Reconciliation`, `settled_intents`. Read-only by
  construction, so it is safe to run at startup before any gate is open.
* `lifecycle/trade_lifecycle.py` — `TradeLifecycle`, `LifecycleStep`. The loop, plus the
  four-step placement ordering.
* `docs/lifecycle/README.md` and `docs/lifecycle/LIFECYCLE.md`.
* 140 tests across `tests/unit/test_persistence.py`, `test_state_machine.py`,
  `test_recovery.py` and `test_trade_lifecycle.py`.

### What Phase 8 must preserve

Four invariants, each of which a later phase could plausibly break by adding a new send path:

1. **Any new write goes through `place_order`'s ordering** — gate, record, re-read, send once,
   settle. A new account-changing operation (Phase 8's adapter reaches no broker, but a future
   exit or re-entry would) needs the ledger write before its send, or the crash window reopens.
2. **`VERIFYING` must never reach a placement without a broker read.** If a state is added,
   check that it cannot reach `PENDING_ORDER_PLACED` without passing through `VERIFYING` or
   `RECONCILING`.
3. **Unresolved *and* unknown intents are both re-observed.** `awaiting_confirmation()` is the
   query; `unresolved()` alone is not enough, and using it leaves `VERIFYING` with nothing to
   verify.
4. **The gate is chosen by the venue and the environment is always passed.** Any new entry
   point must take `settings` explicitly rather than defaulting it.

`ExecutionUnknownError` is **never** retryable, anywhere. `retry.py` re-raises it
immediately rather than riding it out, precisely so that a placement mistakenly passed in as a
read cannot be resent. Any new code in Phase 6 that loops over broker calls must keep that
branch, and any new send path — break-even modification, trailing modification, close — must
classify its retcode the same way `place_order` does. A stop that moved is still an
account-changing operation; an ambiguous outcome there is a stop that may or may not have
moved.

## Files Added

```
docs/architecture/ARCHITECTURE.md
docs/strategy/BASELINE.md
docs/strategy/ENTRY_RULES.md
docs/strategy/README.md
docs/risk/README.md
docs/risk/RISK_MODEL.md
docs/mt5/README.md
docs/mt5/SETUP.md
docs/mt5/SYMBOL_SPECIFICATIONS.md
docs/testing/README.md
docs/operations/README.md
docs/research/README.md
docs/execution/README.md
docs/execution/EXECUTION.md
docs/trailing/README.md
docs/trailing/TRAILING_MODEL.md
docs/lifecycle/README.md
docs/lifecycle/LIFECYCLE.md
docs/backtest/README.md
docs/operations/RUNNING.md
src/stop_order_scalp/application/service.py
src/stop_order_scalp/backtest/replay.py
src/stop_order_scalp/backtest/statistics.py
src/stop_order_scalp/backtest/runner.py
scripts/make_sample_candles.py
scripts/check_architecture.py
tests/fixtures/us30_m1_sample.csv
src/stop_order_scalp/market_data/candles.py
src/stop_order_scalp/market_data/mt5_module.py
src/stop_order_scalp/market_data/mt5_feed.py
src/stop_order_scalp/strategy/candle_direction.py
src/stop_order_scalp/strategy/entry_rules.py
src/stop_order_scalp/strategy/signal.py
src/stop_order_scalp/strategy/strategy.py
src/stop_order_scalp/risk/commission.py
src/stop_order_scalp/risk/position_sizer.py
src/stop_order_scalp/risk/stop_loss.py
src/stop_order_scalp/risk/take_profit.py
src/stop_order_scalp/risk/risk_manager.py
src/stop_order_scalp/execution/gates.py
src/stop_order_scalp/execution/order_manager.py
src/stop_order_scalp/execution/mt5_broker.py
src/stop_order_scalp/execution/simulated_broker.py
src/stop_order_scalp/execution/retry.py
src/stop_order_scalp/execution/position_manager.py
src/stop_order_scalp/trailing/break_even.py
src/stop_order_scalp/trailing/trailing_stop.py
src/stop_order_scalp/infrastructure/persistence.py
src/stop_order_scalp/lifecycle/state_machine.py
src/stop_order_scalp/lifecycle/recovery.py
src/stop_order_scalp/lifecycle/trade_lifecycle.py
tests/strategy/conftest.py
tests/strategy/test_candle_direction.py
tests/strategy/test_entry_rules.py
tests/strategy/test_signal.py
tests/strategy/test_strategy.py
tests/risk/conftest.py
tests/risk/test_commission.py
tests/risk/test_position_sizer.py
tests/risk/test_risk_manager.py
tests/risk/test_stop_loss_take_profit.py
tests/execution/conftest.py
tests/execution/test_gates.py
tests/execution/test_order_manager.py
tests/execution/test_mt5_broker.py
tests/execution/test_mt5_classification.py
tests/execution/test_simulated_broker.py
tests/execution/test_retry.py
tests/trailing/conftest.py
tests/trailing/test_break_even.py
tests/trailing/test_trailing_stop.py
tests/trailing/test_position_manager.py
tests/trailing/test_property_monotonic.py
tests/unit/test_persistence.py
tests/unit/test_state_machine.py
tests/unit/test_recovery.py
tests/unit/test_trade_lifecycle.py
tests/unit/test_architecture.py
tests/unit/test_cli.py
tests/unit/test_interfaces.py
tests/unit/test_candles.py
tests/unit/test_mt5.py
```

## Files Modified

Phase 7:

```
src/stop_order_scalp/execution/gates.py        SimulatedGate added -- open for DRY_RUN/PAPER,
                                               refuses LIVE so it fails closed if miswired
src/stop_order_scalp/execution/order_manager.py place() now REQUIRES settings; gate typed to the
                                               Gate protocol and chosen at construction
src/stop_order_scalp/domain/interfaces.py       unchanged
tests/execution/conftest.py                    the manager fixture names its gate
docs/lifecycle/*                               new
ROADMAP.md, HANDOFF.md, README.md, README.fa.md, docs/architecture/ARCHITECTURE.md,
CHANGELOG.md, docs/testing/README.md
```

Phase 6:

```
src/stop_order_scalp/execution/retry.py    ReadOutcome made Generic, so a retried read keeps
                                           its type rather than degrading to Any
docs/trailing/*                           new
ROADMAP.md, HANDOFF.md
```

Phase 5:

```
src/stop_order_scalp/domain/models.py       OrderIntent.client_tag_for() -- the single
                                           place the identity tag is derived
src/stop_order_scalp/domain/interfaces.py   AccountReader gained specification(); the risk
                                           engine needs the broker's own contract numbers
src/stop_order_scalp/market_data/mt5_module.py  MT5Api gained order_send/order_get/
                                           orders_get/positions_get
src/stop_order_scalp/infrastructure/clock.py    sleep() added, so import time has one home
src/stop_order_scalp/risk/risk_manager.py   risk_settings/target_settings exposed
tests/unit/test_mt5.py                      FakeTerminal grew the execution surface
tests/unit/test_interfaces.py               FakeBroker grew specification()
docs/execution/*                            new
ROADMAP.md, README.md, docs/architecture/ARCHITECTURE.md, CHANGELOG.md
```

Phase 4:

```
src/stop_order_scalp/risk/*.py            the risk layer, new
docs/risk/RISK_MODEL.md                    the sizing derivation and the worked example
docs/risk/README.md                        rewritten: no longer a placeholder
tests/risk/*                               140 tests
HANDOFF.md                                 corrected 4 stale statements (see below)
```

**Corrections made to this file in Phase 4.** Reading it and checking every claim against
the repository found four that had gone stale as earlier phases landed. Left uncorrected,
they would have misled the next agent:

* "Only `validate-config` is implemented" contradicted the table immediately below it;
  `test-connection` landed in Phase 2.
* "Only `execution/mt5_broker.py` and `market_data/mt5_feed.py` may import `MetaTrader5`"
  — Phase 2 added `mt5_module.py`, which owns the import.
* "Phases 2 through 12" in Remaining Work, when 2 and 3 were done.
* `docs/{risk,testing,operations,research}/` described as placeholder-only; `docs/risk/`
  now has a real model.

Phase 3:

```
src/stop_order_scalp/domain/value_objects/price.py   added round_entry_price
src/stop_order_scalp/strategy/*.py                    the strategy layer, new
tests/strategy/*                                       119 tests
docs/strategy/*                                        ENTRY_RULES.md, README.md
```

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

1159 passing, 1 skipped.

| File | Tests |
| --- | --- |
| `tests/unit/test_interfaces.py` | 108 |
| `tests/unit/test_mt5.py` | 80 |
| `tests/unit/test_config.py` | 55 |
| `tests/risk/test_position_sizer.py` | 50 |
| `tests/unit/test_value_objects.py` | 46 |
| `tests/unit/test_candles.py` | 45 |
| `tests/unit/test_state_machine.py` | 44 |
| `tests/execution/test_mt5_classification.py` | 42 |
| `tests/risk/test_stop_loss_take_profit.py` | 39 |
| `tests/strategy/test_signal.py` | 37 |
| `tests/unit/test_trade_lifecycle.py` | 36 |
| `tests/unit/test_architecture.py` | 35 |
| `tests/execution/test_mt5_broker.py` | 34 |
| `tests/unit/test_persistence.py` | 34 |
| `tests/strategy/test_candle_direction.py` | 33 |
| `tests/risk/test_risk_manager.py` | 30 |
| `tests/trailing/test_position_manager.py` | 27 |
| `tests/unit/test_timeframes.py` | 27 |
| `tests/execution/test_simulated_broker.py` | 26 |
| `tests/unit/test_cli.py` | 26 |
| `tests/unit/test_recovery.py` | 26 |
| `tests/strategy/test_entry_rules.py` | 25 |
| `tests/unit/test_logging.py` | 25 |
| `tests/trailing/test_break_even.py` | 24 |
| `tests/strategy/test_strategy.py` | 24 |
| `tests/trailing/test_trailing_stop.py` | 23 |
| `tests/risk/test_commission.py` | 21 |
| `tests/execution/test_gates.py` | 24 |
| `tests/trailing/test_property_monotonic.py
tests/unit/test_persistence.py
tests/unit/test_state_machine.py
tests/unit/test_recovery.py
tests/unit/test_trade_lifecycle.py` | 13 |
| `tests/execution/test_order_manager.py` | 13 |
| `tests/execution/test_retry.py` | 11 |

The single skip is the `albrooks` cross-check of `freeze_closed_bars`, which needs the
optional extra. It is the only skip in the suite, and is acceptable only because that
extra is genuinely optional. The skip is about the *candle* agreement, not about the
integration that was removed in Phase 8.

## Test Results

```
python -m pytest                             1159 passed, 1 skipped
python -m ruff check .                       All checks passed!
python -m mypy                               Success: no issues found in 95 source files
python scripts/check_architecture.py         architecture OK: 51 modules checked
python -m stop_order_scalp validate-config   exit 0
python -m stop_order_scalp test-connection   exit 3, reports package_installed: false
```

## Git Commit

`feat(lifecycle): write-intent ledger, transition table, and restart recovery`

## Git Push

Pushed to `origin/main`.

---

## Current Architecture

```
src/stop_order_scalp/
    domain/          IMPLEMENTED — value objects, enums, models, exceptions, protocols
    infrastructure/  IMPLEMENTED — config, logging, clock
    market_data/     IMPLEMENTED — timeframes, candles/freeze, mt5_module, mt5_feed
    strategy/        IMPLEMENTED — candle_direction, entry_rules, signal, strategy
    risk/            IMPLEMENTED — commission, position_sizer, stop_loss, take_profit, manager
    execution/       IMPLEMENTED — gates, order_manager, mt5_broker, simulated_broker,
                                 retry, position_manager
    trailing/        IMPLEMENTED — break_even, trailing_stop
    lifecycle/       IMPLEMENTED — state_machine, recovery, trade_lifecycle
    integrations/    empty      reserved for a future signal source
    backtest/        empty      Phase 10
    research/        empty      reserved
    application/     IMPLEMENTED — service.py, the composition root
    cli/             IMPLEMENTED — contract + validate-config, test-connection, run, journal
    tests/              unit + strategy + risk + execution + trailing + application
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

`cli/main.py` fixes the whole command surface in Phase 1. Two commands are implemented;
the rest exit **4** with a named message, because `ComponentNotAvailableError` is raised
rather than an `ImportError` traceback reaching the operator:

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

In order, as listed in `ROADMAP.md`:

1. Project foundation — **done**
2. Market data — **done**
3. Core strategy — **done**
4. Risk engine — **done**
5. Order execution — **done**
6. Position management — **done**
7. Lifecycle and recovery — **done**
8. Al Brooks integration — **removed**, see above
9. Make it run — **done**
10. Backtest and statistics — **done**
11. Demo validation — **not started**, and it needs a human with a demo account

Two CLI commands are still unbuilt and still exit 4 honestly: `status` and `diagnostics`.
Neither blocks trading. `diagnostics` in particular would be the natural way to watch a demo
account, so it is the first thing to add when Phase 11 starts.

## Known Issues

* **The assumed symbol specification is the largest open item in the project.** Point `0.1`,
  tick value `1.0` per lot, contract size `1.0`, written by hand in
  `docs/mt5/SYMBOL_SPECIFICATIONS.md`. **Every money figure the project produces scales with
  the tick value**, so a replay on these numbers is a different measurement from the same
  replay on a real US30 contract. `backtest` reports the assumption in its output for exactly
  this reason. Phase 11 exists to replace it.
* **A backtest on one CSV is not a result.** The committed fixture is invented, and a real one
  would still be a single sample chosen by whoever picked it. The project reports trade count,
  win rate, profit factor, drawdown and exits-by-reason because they are countable from what
  the venue did — and deliberately reports **no** Sharpe ratio, no volatility and no
  equity-curve statistic, because each needs a sampling model for the untraded periods and a
  sampled curve is a claim about the future wearing the costume of a measurement.
* **Fills are optimistic in one specific way.** A stop order fills at its level, on the bar
  that crossed it. Real venues fill with slippage and gaps; `--slippage-points` models the
  first and is adverse-only by design.
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
* **No MetaTrader 5 terminal has ever been connected.** The `metatrader5` package
  (5.0.6231) is installed, so imports resolve, but nothing has talked to a broker. Every
  wire value, retcode and broker limit in this project is still unverified. It is a
  get it from your broker, then `pip install -e ".[mt5]"`. See
  [`docs/mt5/SETUP.md`](docs/mt5/SETUP.md) §1. Nothing in the repository imports it at
  module scope, and the whole suite passes without it. `test-connection` currently reports
  `package_installed: false` and exits 3, which is the correct behaviour, not a failure.
* **Never edit a Persian or accented `.md` file with PowerShell `Set-Content`.** It writes
  in the ANSI codepage and destroys the text. This happened to `README.fa.md` during Phase 3
  and was caught only by scanning every file for invalid UTF-8; use the editor tools, which
  write UTF-8. `python -c` with an explicit `encoding="utf-8"` is the safe way to check.
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
  It needs `pip install -e ".[albrooks]"`. This is the **only** skip in the suite.
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
10. **Two independent candle implementations must agree** — this project's
    `market_data/candles.py` and the sibling engine's `freeze_closed_bars`. Cross-tested.
11. **No absolute paths in application code.** Only `.env` and relative resolution.
12. **The CLI surface is fixed before its implementations.** An unbuilt command raises
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
18. **Not trading is a value, not a failure.** The decision is `TradeDecision | NoTrade`,
    and `NoTrade` carries a named reason plus its evidence. Most of the time the correct
    answer is to do nothing; modelling that as an exception or a null signal would push
    the work of telling a quiet market from a broken feed out to every caller.
19. **An instrument-policy refusal raises rather than returning `NoTrade`.** Reaching that
    check means the configuration or the feed is wrong, and a run that silently declines to
    trade all day is the worst possible outcome — it looks like a strategy that simply is
    not triggering.
20. **A doji is never traded and never falls back to an earlier directional candle.** "Use
    the last candle that had a direction" changes the rule from *most recent* to *most recent
    directional*, which is a different strategy.
21. **Entry rounding and stop rounding are different methods with opposite signs.**
    `round_entry_price` is anchored on the candle's extreme; `round_stop_price` is anchored
    on the entry. Mixing them up moves every order a tick toward the market.
22. **The risk budget is compared against `total_risk`, never `price_risk`.** Sizing on
    price risk alone ignores the commission and over-sizes every position — 6 % on the
    project's own numbers, silently. The three figures stay separate on `TradePlan` for
    exactly this reason.
23. **A size always floors onto the broker's volume step.** Rounding up would exceed the
    approved budget. With `refuse_below_min_volume`, a budget too small for the broker's
    minimum is **refused**, not rounded up to it — a limit that does not bind is not a limit.
24. **Rejections carry stable, machine-readable codes.** Prose forces every caller to parse
    English; a code can be counted, alerted on and compared across runs. The strings in
    `RejectionCode` are an interface — add one rather than reword one.
25. **A bad target must not stop a trade whose risk is already bounded.** The stop is what
    bounds risk, so a malformed take profit falls back to the configured target. A
    malformed *stop*, by contrast, refuses the trade.
26. **A tighter stop buys a larger position.** Which is why the stop must be fixed before
    the size, never derived from it. `tests/risk/test_risk_manager.py` pins it.

## Configuration

Strategy defaults: `config/default.yaml`.
Machine settings: `.env` from `.env.example`.

Environment variable prefix: **`SOS_`**.

Planned keys: `SOS_ENVIRONMENT`, `SOS_ALLOW_LIVE`, `SOS_ALLOW_ORDER`, `SOS_ALLOW_CLOSE`,
`SOS_MT5_PATH`, `SOS_MT5_LOGIN`, `SOS_MT5_PASSWORD`, `SOS_MT5_SERVER`, `SOS_MT5_TIMEOUT_MS`,
`SOS_SYMBOL`, `SOS_MAGIC_NUMBER`, `SOS_CONFIG_PATH`, `SOS_STATE_PATH`, `SOS_LOG_DIR`,
`SOS_ENV_FILE`.

**`SOS_MT5_PASSWORD` is a secret. It lives only in `.env`, which is git-ignored. Never
commit it, never log it.** It is reduced to a presence flag at load time and no downstream
code reads the variable.

## Next Recommended Phase

**Phase 11 — Demo Validation.** Start at `ROADMAP.md` §"Phase 11", with
`docs/mt5/SYMBOL_SPECIFICATIONS.md` and `docs/operations/RUNNING.md` as the design input.

This is the first phase that touches a real venue, and it cannot be done without a human
holding a demo account. It is also the phase that makes the assumed numbers real.

| What Phase 11 needs | Where it is written down |
| --- | --- |
| the real `US30` specification, replacing the assumed one | `docs/mt5/SYMBOL_SPECIFICATIONS.md` |
| the retcode table, corrected against actual failures | `docs/execution/EXECUTION.md` |
| the dry run driven against a demo terminal | `docs/operations/RUNNING.md` |
| a comparison of that run against `backtest` on the same data | `docs/backtest/README.md` |

Order matters. **Capture the specification before anything else**, because every money figure
in a replay scales with the tick value and the contract size. A backtest run on assumed
numbers is a different measurement from the same replay on real ones, and comparing the two
is meaningless until they share a specification.

**Expect the sample fixture's money figures to change** once the specification is real. The
trade *count* should not — if it does, something else is wrong.

### Also unbuilt, and honestly optional

`status` and `diagnostics` still exit 4. Neither blocks trading; both would be genuinely
useful for watching a demo account. They are the cheapest remaining work in the project.

---

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
* **Only `market_data/mt5_module.py`, `market_data/mt5_feed.py` and
  `execution/mt5_broker.py` may import `MetaTrader5`**, and only inside a function body —
  and only `mt5_module.py` actually contains the `import`.
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
