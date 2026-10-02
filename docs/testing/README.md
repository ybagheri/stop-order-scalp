# Testing

**`docs/testing/TESTING.md` does not exist yet** — this directory is a placeholder so the
links in [`README.md`](../../README.md) resolve. The full document lands with Phase 9.

This page is the short version, and it is accurate today.

## Running everything

```bash
python -m pip install -e ".[dev]"

python -m pytest                             # 1116 tests, 1 skipped
python -m ruff check .                       # lint
python -m mypy                               # types, strict, src + tests
python scripts/check_architecture.py         # architecture boundaries
```

`mypy` and the architecture script also run as part of `pytest`, so a single
`python -m pytest` covers all four gates. Prefer `python -m …` over console scripts.

## Current suite

| File | Tests | Covers |
| --- | --- | --- |
| `tests/backtest/test_backtest.py` | 27 | Replay end to end: walk-forward, no look-ahead, statistics from the venue's records, commission charged, the command's refusals, and replaying twice |
| `tests/application/test_service.py` | 42 | The assembled system run for real, and what a *second* run sees |
| `tests/unit/test_interfaces.py` | 108 | The protocols; every module imports; every `__all__` entry resolves |
| `tests/unit/test_mt5.py` | 80 | The MT5 boundary against a fake terminal: lazy import, converters, feed, `ServerClock`, `probe_connection`, the execution surface |
| `tests/unit/test_config.py` | 55 | Three-layer config, unknown-key rejection, `.env` precedence, secret handling |
| `tests/risk/test_position_sizer.py` | 50 | The worked example, points-vs-dollars, rounding, broker bounds, sizing properties |
| `tests/unit/test_value_objects.py` | 46 | `Price`, `Money`, `Volume`, `SymbolSpecification`, point/price arithmetic |
| `tests/unit/test_candles.py` | 45 | The freeze, the report, timezone handling, the no-look-ahead properties |
| `tests/unit/test_state_machine.py` | 44 | The transition table as data: no traps, halt absorbing, `VERIFYING` sealed |
| `tests/execution/test_mt5_classification.py` | 42 | Retcode buckets, and that no ambiguous code is ever retryable |
| `tests/risk/test_stop_loss_take_profit.py` | 39 | Stop and target providers, rounding asymmetry, target precedence |
| `tests/strategy/test_signal.py` | 37 | The decision, the no-trade reasons, the instrument policy, and the no-look-ahead properties |
| `tests/unit/test_trade_lifecycle.py` | 36 | The loop: record, re-read, send once, settle; duplicate protection |
| `tests/unit/test_architecture.py` | 35 | The architecture gate itself, including deliberately broken source |
| `tests/execution/test_mt5_broker.py` | 34 | One `order_send` per placement, wire values, comment encoding |
| `tests/unit/test_persistence.py` | 34 | Atomic writes, refuse-to-overwrite, never start fresh |
| `tests/strategy/test_candle_direction.py` | 33 | The five verdicts, closed-candle enforcement, the doji case |
| `tests/risk/test_risk_manager.py` | 30 | Sizing end to end, and every rejection code |
| `tests/trailing/test_position_manager.py` | 27 | Break-even-then-trailing ordering, idempotency, one request per change |
| `tests/unit/test_timeframes.py` | 27 | Period seconds, boundary math, candle closure, timezone handling |
| `tests/execution/test_simulated_broker.py` | 26 | Bid/ask fills, stops, commission, the injected clock |
| `tests/unit/test_cli.py` | 26 | Parser surface, exit codes, commands whose phase has not landed |
| `tests/unit/test_recovery.py` | 26 | Broker authoritative; unattributable exposure stops the system |
| `tests/strategy/test_entry_rules.py` | 25 | The two entry rules, points resolution, entry-vs-stop rounding |
| `tests/unit/test_logging.py` | 25 | JSONL audit records, rotation, structural absence of secrets |
| `tests/execution/test_gates.py` | 24 | The three-fold interlock, and that both gates default closed |
| `tests/trailing/test_break_even.py` | 24 | The trigger, commission-aware targets, broker limits, idempotency |
| `tests/strategy/test_strategy.py` | 24 | Construction refusals, dispatch, the self-describing rule |
| `tests/trailing/test_trailing_stop.py` | 23 | `bid − d` / `ask + d`, arming, minimum step, monotonicity |
| `tests/risk/test_commission.py` | 21 | Round-trip versus per-side, and that they differ by exactly two |
| `tests/trailing/test_property_monotonic.py` | 13 | Monotonicity and idempotency over generated and adversarial paths |
| `tests/execution/test_order_manager.py` | 13 | Duplicate prevention, and that an unknown outcome is never resent |
| `tests/execution/test_retry.py` | 11 | Bounded backoff, and that it never rides out an ambiguous send |
| **Total** | **1083** (1 skipped) | |

The one skip is the cross-check of `freeze_closed_bars` against the independent Al Brooks
implementation, which needs the optional `albrooks` extra. It is the only skip in the suite
and is acceptable only because that extra is genuinely optional.

## Principles

**Isolation is by construction.** Protocols plus hand-written fakes. There is no mocking
framework in this project, and no `skip` marker standing in for a missing dependency. If a
test needs a broker it gets a `SimulatedBroker`, which is a first-class implementation and
not a test double.

**There is no MetaTrader 5 in the unit suite at all.** The test suite passes on a machine
with no terminal installed, which is what makes it a real gate.

**A gate that cannot fail is worse than no gate.** `tests/unit/test_architecture.py`
therefore feeds deliberately broken source through the checker and asserts each rule
fires. Without those negative cases, a refactor of the checker could stop it checking
anything while the suite stayed green. That is not hypothetical — it is exactly what
happened in Phase 1, and the negative tests are why it was caught.

**Every module must import, and every `__all__` entry must resolve.**
`tests/unit/test_interfaces.py` asserts both across the whole package. This is how the
Phase 1 stabilisation found that `domain/interfaces.py` — the file declaring the project's
four core protocols — raised `TypeError` on import because `Broker` listed `Protocol`
before protocols that already inherit from it. The suite had been green, because nothing
imported the module.

**Determinism comes from injection.** An injected `Clock`, a pinned `TZ`, no `time.sleep`
in unit tests. `tests/conftest.py` also clears every `SOS_*` variable before each test, so
no test inherits another's configuration.

### Watch out for `.env`

`load_config(env_file=None)` auto-discovers a `.env` in the **current working directory**
and merges it into `os.environ`. That is intended production behaviour, and it means a
test that relies on the default will read the developer's real machine settings. The
helpers in `tests/unit/test_config.py` pass an explicit non-existent path to switch the
environment layer off; two regression tests in that file pin both halves of this
behaviour.

## Property tests

`hypothesis` is used where an invariant must hold for *all* inputs rather than for the
examples anyone thought to write:

* point ↔ price conversion never drifts
* normalized volume never exceeds the request
* **appending future candles cannot change a decision taken at time *t*** — the headline
  no-look-ahead property, proved once for the freeze (`test_candles.py`) and again for the
  whole strategy decision (`test_signal.py`), because a freeze that is correct can still be
  fed the wrong candles
* moving the reference time forward never *removes* a bar (monotonicity)
* the same input always produces the same output
* input order never changes the decision
* a decision never uses a bar that had not closed at the reference
* **the chosen position size never carries more total risk than the budget allowed**
* a position size is always on the broker's volume step and within its min/max
* rounding a size down never increases it
* **Phase 6:** a trailing stop is monotonic — BUY `sl_new >= sl_old`, SELL
  `sl_new <= sl_old`

The no-look-ahead property deserves a note. It is stated as a property over generated bar
series rather than as one worked example, because an example only proves the function
behaved on the case somebody thought of. The generators vary the bar count, the offset of
the reference moment within the series, and the timeframe step, because the interesting
cases are the ones where the reference falls in the middle of a forming bar.

The sizing property is stated the same way, over generated specifications, balances,
percentages, commission rates and distances — the cases where the numbers only line up
neatly are the cases where a rounding bug is least likely to show.

**A property test that fails is not always a bug in the code.** When
`test_appending_future_bars_cannot_change_the_decision` first failed, the correct answer
turned out to be that the strategy was right and the *test helper* was wrong: it generated
"future" bars anchored on the last existing bar rather than on the reference moment, so it
sometimes produced bars the reference could legitimately see. Fixing the helper rather than
the strategy was the right call — but only after checking, which is the whole reason to
write the property rather than an example.

**Test fixtures have to be honest too.** Writing the risk tests surfaced several cases
where a fixture would have passed for the wrong reason: bars with identical highs that
could not distinguish which one the rule selected, a `commission_model=None` sentinel that
silently meant "the baseline" rather than "no commission", and a rounding case built on a
specification the sizer correctly refuses. Each is now pinned by a test that would fail if
the fixture were weakened.

## Testing the MT5 boundary without a terminal

There is no `MetaTrader5` in this suite and no skip marker pretending otherwise. A fake
implementing `MT5Api` is enough, because the whole point of the protocol is that the rest of
the project never sees the real module.

The fake deliberately models the terminal's **awkwardness** rather than an idealised API:

* `copy_rates_from_pos` returns the **newest** rows first, and the newest is the bar in
  progress — which is why the feed asks for one bar more than it needs
* `initialize` returns `False` and populates `last_error` rather than raising
* `copy_rates_*` returns `None` on failure rather than raising
* rows are namedtuples, reached by attribute access
* `symbol_select` matches case-insensitively

Writing the fake this way paid for itself immediately: it exposed that the feed was
normalising ordering correctly while the test's own expectations were built on the wrong
assumption about which end of the table was newest.

**What this cannot prove:** that the real terminal behaves as documented. Only Phase 11,
against a live terminal, can do that.

## Testing a stateful system: run it twice

The most valuable test in this project asserts nothing about a single run. It runs the system
twice against the same state file and compares.

It exists because of a specific failure. Phase 9 shipped with the idempotency guarantee dead:
`build_service` constructed the ledger instead of loading it, so recovery was blind and the
first write erased every prior record. **1116 tests passed.** Every one of them built a fresh
`tmp_path` and ran once. The defect was invisible in isolation and obvious in sequence, and
the suite only ever tested one run.

So the rule this project now holds itself to:

> A test that exercises state must exercise it **twice, against the same file**.

`TestAcrossTwoRuns` (application) and `TestReplayedTwice` (backtest) are that rule made
permanent. When you add state to a component, add the second run to its test.

## A regression test that cannot fail

Phase 9 also produced the opposite failure: tests written for real bugs that passed against
those bugs. Four of the first eight, recorded because each is an ordinary mistake:

| How it lied | Why it passed |
| --- | --- |
| compared the on-disk ledger to the in-memory one | those two agree even when both are wrong |
| compared entry *counts* across two runs | run two re-records the same client tag, so the counts match even though the row was replaced |
| `if not detail.startswith(...): continue` | with the bug present, every detail was skipped, so the loop body never ran |
| passed a `--config` flag the CLI does not have | the command read a different ledger than the test had populated |

And one in Phase 10: a "duration is real" assertion that passed with the clock fix reverted,
because it looked at a trade list that was empty in the reverted state.

**So the check is part of writing the test, not a formality afterwards:** revert the fix, run
the test, confirm it fails, restore. If it passes, the test is a comment with a runtime cost.

## Checking a number against a second number

Phase 10 built the first thing here that reports numbers, and three of them were wrong in the
direction that flatters a strategy:

- the venue charged **no commission** while the risk engine sized positions net of it, so
  every P/L figure was too high by exactly the cost already accounted for
- the venue's **clock never moved**, so every trade duration read as 0 seconds
- **closed trades were never recorded**, so exits had to be guessed from price action

Each was found by reconciling two independent numbers, not by reading one:

| Reported | Checked against |
| --- | --- |
| `total_commission` | `commission_per_lot` in the config |
| `net_profit` | `ending_balance - starting_balance` |
| one trade's charge | `rate × lots` under the configured mode |
| an exit reason | whether the P&L sign agrees with `take_profit` / `stop_loss` |

The same instinct applies to the M15 filter: a replay that reports *zero* trades is a result
to be explained, not a shrug. On this rule and this data, zero meant the direction filter was
correctly rejecting a series with no direction — which is a fact about the fixture, and the
fixture was rebuilt to make it visible.

