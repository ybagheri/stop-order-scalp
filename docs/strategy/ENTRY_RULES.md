# Entry Rules and Candle Selection

How the baseline picks the M15 candle that decides direction, the M1 candle that places the
order, and what happens in every case where it cannot.

Implementation: `src/stop_order_scalp/strategy/`. Tests: `tests/strategy/`.

---

## 1. The whole rule, in one screen

```
                    ┌─────────────────────────────────────────────┐
  M15 candles ───►  │ last FULLY CLOSED M15 candle                │
                    │   close > open  ──────────────────►  BUY    │
                    │   close < open  ──────────────────►  SELL   │
                    │   close = open  ──────────────────►  NO TRADE│
                    └─────────────────────────────────────────────┘
                                        │ side
                                        ▼
                    ┌─────────────────────────────────────────────┐
  M1 candles  ───►  │ last FULLY CLOSED M1 candle                 │
                    │   BUY  STOP  = high + 10 points             │
                    │   SELL STOP  = low  - 10 points             │
                    └─────────────────────────────────────────────┘
```

Both "last fully closed" selections use broker server time, via the same
`freeze_closed_bars` the market-data layer uses. The strategy never reads a clock: the
reference moment is an argument to every function.

## 2. The direction filter

`strategy/candle_direction.py`. Five verdicts, of which **two** authorise a trade:

| Verdict | Means | Trades? |
| --- | --- | --- |
| `allow_buy` | the last closed M15 closed above its open | yes |
| `allow_sell` | it closed below its open | yes |
| `doji` | it closed exactly on its open | no |
| `no_candles` | no closed M15 candle was available | no |
| `timeframe_mismatch` | the series held no candles of this timeframe | no |

### A doji is not a weak signal

`close == open` is an absence of information, and the honest response to it is not to
trade. The tempting implementation — "use the last candle that had a direction" — is
excluded by a test (`test_a_doji_does_not_fall_back_to_the_previous_candle`), because it
silently turns a rule about the *most recent* candle into a rule about the *most recent
directional* one, and those are different strategies.

### A missing candle is not a neutral candle

"No closed M15 bar" and "the M15 bar says nothing" are different situations, and the
verdict distinguishes them. `is_indeterminate` separates the data-shortage verdicts from the
definitive ones, so an operator can tell a quiet market from a broken feed.

## 3. The entry rules

`strategy/entry_rules.py`. Two rules, both exact.

| Side | Price | Order kind |
| --- | --- | --- |
| BUY | `M1.high + offset_points` | `ORDER_KIND_BUY_STOP` |
| SELL | `M1.low - offset_points` | `ORDER_KIND_SELL_STOP` |

### Points are resolved, never assumed

The offset is in **points**, and a point is whatever `SymbolSpecification.point` says. On
this project's assumed `US30` that is `0.1`, so 10 points is 1.0 price unit:

```
M1.high 40014.0  +  10 points x 0.1  =  40015.0   BUY STOP
M1.low  39994.0  -  10 points x 0.1  =  39993.0   SELL STOP
```

Every conversion goes through `points_to_price`. No function in `strategy/` multiplies a
point count by a price, and there is no constant anywhere that says what a point is worth.
`test_points_are_not_dollars` in `tests/strategy/test_entry_rules.py` changes the point
size and asserts the *absolute* offset stays correct — a test that hard-coded "plus 1.0"
would pass on one broker and be wrong on every other.

### Entry rounding is not stop-loss rounding

A subtle bug found and fixed during Phase 3, worth writing down because it is easy to
repeat.

`SymbolSpecification` has two asymmetric rounding methods, and they are **not** mirror
images:

| Method | Anchored on | BUY rounds | SELL rounds |
| --- | --- | --- | --- |
| `round_stop_price` | the **entry** (a stop loss sits below a BUY entry) | down | up |
| `round_entry_price` | the **candle's extreme** (a BUY STOP entry sits above the high) | up | down |

Using the first for an entry moves every order a tick *toward* the market: the stop triggers
before the level the strategy specified, which is a different trade, and it also moves
closer to market where a broker's `stops_level` rejection lives. `entry_rules.py` uses
`round_entry_price` only, and a test asserts the two disagree.

Rounding is only ever non-trivial when a price arrives off the tick grid, which a
well-behaved broker will not do — so this is a correctness guarantee, not a workaround.

## 4. Not trading is a result

`strategy/signal.py`. The decision is a union:

```python
Decision = TradeDecision | NoTrade
```

A caller handles both arms in one place, so "no trade" cannot be forgotten:

```python
decision = strategy.evaluate(context)
if isinstance(decision, NoTrade):
    log.info(decision.reason, extra=decision.to_dict())
    return
plan = risk.assess(decision.signal)     # only a TradeSignal gets here
```

If "no trade" were an exception, a `None`, or a signal with a null side, every caller that
must distinguish *quiet* from *broken* would re-derive the reason — and would eventually
disagree with the strategy about why.

`NoTrade` carries its evidence, not just its verdict: the direction decision, the entry
candle if there was one, and the freeze report. A journal that recorded only "no trade"
would be useless for working out afterwards whether the strategy was quiet or broken.

| Reason | Indeterminate? | When |
| --- | --- | --- |
| `no_direction` | yes | no closed M15 candle, or it is still forming |
| `doji_direction` | no | the M15 candle closed on its open |
| `entry_candle_forming` | yes | direction fine, but no closed M1 candle yet |
| `no_entry_candle` | yes | no M1 candles at all |
| `instrument_not_allowed` | — | raised, not returned; see §6 |
| `instrument_quarantined` | — | raised, not returned |

## 5. The order of the checks

`evaluate()` checks in the order an operator would want to read in a log:

1. **Is the instrument permitted?** Raises. See §6.
2. **Is there a closed M15 candle, and does it permit a direction?** If not, no trade, and
   the reason names which of the four ways it failed.
3. **Is there a closed M1 candle to derive the entry from?** The entry level is a function
   of that candle's high or low, so without it there is nothing to place.
4. **Is it the right timeframe?** Checked after the data checks, because a wrong timeframe is
   a caller bug and should not be masked by a quiet market.

`test_the_direction_failure_is_reported_before_the_entry_failure` pins that order: when both
are broken, the reason names the direction.

## 6. US30 only, and why it raises

The specification says US30 only, and says not to silently trade something else.
`InstrumentPolicy` matches **exactly, after case folding**, so `US30` can never select
`US30mini` or `EURUSD30` — the specific failure the specification forbids.

A refusal is an **exception**, not a `NoTrade`. Reaching that check means the configuration
or the feed is wrong, not that the market is quiet, and a misconfigured run must fail loudly
rather than sit there declining to trade all day — which is the worst possible outcome,
because it looks like a strategy that is simply not triggering.

## 7. The closed-candle rule is structural

Three layers enforce it, and none of them relies on discipline:

1. `MT5Feed.candles()` returns only closed bars; the forming bar is reachable only by
   calling `forming_candle()` by name.
2. `freeze_closed_bars` drops anything not closed at the reference moment and returns a
   `FreezeReport` saying what it withheld.
3. The strategy calls `freeze_closed_bars` itself for the direction candle, and the entry
   selection goes through `select_entry_candle`. Neither reads a forming candle even if one
   is handed to it.

And the outcome is proved, not asserted:

> **appending future candles cannot change a decision taken at time *t***

That is a `hypothesis` property over generated market data —
`tests/strategy/test_signal.py::TestNoLookAhead` — because an example only proves the
function worked on the case somebody thought of. It varies the bar count, the reference
moment's position within the series, the M15 bodies, and the input order, because the
interesting cases are the ones where the reference falls inside a forming bar.

Three further properties come free with it: the same inputs always give the same decision;
input order never changes the decision; and a decision never uses a bar that had not closed
at the reference.

## 8. The selection knob

`entry.candle_selection` has two values:

| Value | Behaviour | Status |
| --- | --- | --- |
| `last_closed` | the last fully closed candle | **the baseline** |
| `current_forming` | the bar in progress; repaints | research only |

`StopOrderStrategy.uses_closed_candles` reports which is running, and `describe()` writes
the whole rule into the log. Stating the rule in full is what makes a journal entry months
later interpretable: *"US30, M15 direction, M1 entry, 10 points, last closed"* is a
complete statement of the strategy, and nothing shorter is.

Flipping this is a visible, deliberate act rather than a silent behaviour change.

## 9. What this phase does not do

* **No sizing, no stop loss, no take profit.** The signal deliberately carries no geometry;
  those come from configuration in the risk engine (Phase 4). A signal that invented its own
  would make it impossible to tell which signals carried geometry.
* **No filters.** Spread, ATR, session and candle-quality filters are Phase 12, disabled by
  default.
* **No Al Brooks.** Phase 8, optional, and it contributes geometry rather than direction.
* **No profitability claim.** Nothing here has been run against real market data.
