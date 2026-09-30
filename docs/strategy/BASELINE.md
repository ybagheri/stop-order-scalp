# Strategy Baseline

Status: **frozen.** This is the strategy the specification asks for, and it is not
changed by any later phase.

> **No profitability is claimed.** No backtest against real market data has been run.
> Nothing in this repository is evidence that this strategy makes money.

The canonical, machine-readable copy of every parameter below is
[`config/default.yaml`](../../config/default.yaml). This document explains the rules; the
YAML is what the code actually reads. If the two ever disagree, the YAML wins and this
document is a bug.

---

## 1. The rules

| Rule | Value | Config key |
| --- | --- | --- |
| Instrument | `US30` only | `symbol` |
| Direction timeframe | M15, **last fully closed candle** | `entry.direction_timeframe` |
| Entry timeframe | M1 | `entry.timeframe` |
| Candle selection | `last_closed` (never the forming candle) | `entry.candle_selection` |
| BUY STOP price | `M1.high + 10 points` | `entry.offset_points` |
| SELL STOP price | `M1.low − 10 points` | `entry.offset_points` |
| Risk | `0.5 %` of account balance | `risk.mode`, `risk.percent` |
| Commission | `$6 / lot`, round trip | `risk.commission_per_lot`, `risk.commission_mode` |
| Take profit | `1000 points` | `target.mode`, `target.take_profit_points` |
| Risk/reward | `1.0`, used only in `risk_reward` mode | `target.risk_reward` |
| Stop loss | `100 points` from entry, where a mode needs one | `target.stop_loss_points` |
| Break-even | armed at `50 points` | `break_even.enabled`, `break_even.trigger_points` |
| Trailing | `100 points`, monotonic | `trailing.enabled`, `trailing.distance_points` |
| Replacement order | placed immediately after a position closes | Phase 7 |
| Research filters | **off** | `filters_enabled: false` |

## 2. Why the closed-candle rule is the important one

`entry.candle_selection: last_closed` is the single setting that decides whether the
backtest means anything.

A forming M1 candle's high and low move until the candle closes. If the entry price is
computed from a forming candle, the system is reading a number that will change, and in a
historical replay it is reading the *future*: the recorded high of a bar is the high the
bar eventually reached, not the high that was knowable when the decision was taken. That is
look-ahead bias, and it makes results look far better than reality.

Phase 3 proves the property formally: **appending future candles cannot change a decision
taken at time *t*.** That test is a release gate, not a nicety.

The same rule applies to the M15 direction filter. The direction comes from the last
*closed* M15 candle, never the one in progress.

## 3. Why points are never assumed to be dollars

The specification says explicitly: *do not assume 1 point = $1*.

Every point-to-price and price-to-money conversion goes through `SymbolSpecification`,
which is built from the broker's own `symbol_info`. A point is a price increment, not a
money amount; the money value of a move depends on `tick_size`, `tick_value` and
`contract_size`, which only the broker knows.

The test suite's synthetic `US30` specification makes the distinction concrete: point
`0.1`, tick `0.1`, tick value `1.0` per lot, contract size `1.0`. On those numbers, 10
points is 1.0 price units and a 100-point move costs **$1000 per lot**, not $100. The real
values for the user's broker are unknown until Phase 11 captures them from the live
terminal, and until then every risk figure in the tests is stated against this synthetic
object and labelled as assumed.

## 4. The three risk figures are kept distinct

The specification requires price risk, commission and total risk to be separately
visible, and they are three separate fields on `TradePlan`:

| Figure | Meaning |
| --- | --- |
| `price_risk` | Entry to stop, commission excluded |
| `commission` | Estimated commission for the whole planned life of the trade |
| `total_risk` | `price_risk + commission` |

`0.5 %` is compared against **`total_risk`**. Sizing against price risk alone would
systematically over-size every position by the cost of the commission, which on a scalping
strategy with a $6/lot commission and a 1000-point target is not a rounding error.

## 5. Exit management

1. **Take profit** at 1000 points, or at `1:1` risk/reward in `risk_reward` mode.
2. **Break-even** once price has moved `50 points` in favour: the stop moves to entry.
3. **Trailing** at 100 points, **monotonic** — for a BUY the stop only ever rises, for a
   SELL it only ever falls.

Monotonicity is not a preference. A trailing stop that moves backwards converts a winning
trade into a losing one on spread alone. Phase 6 proves it as a property over generated
price sequences, including sequences designed to tempt it.

## 6. What is deliberately not here

* **Multi-instrument trading.** The instrument policy is `US30`-only by specification. An
  `InstrumentPolicy` enforces it rather than trusting configuration.
* **Al Brooks as a signal source.** Optional, disabled by default, and when enabled it is a
  *geometry suggestion*, never a signal the system trusts blindly. A `WAIT` or `NO_TRADE`
  decision is not a signal. See `docs/research/` and Phase 8.
* **Filters.** Spread, volatility, session and candle-quality filters all exist in the
  research layer (Phase 12), disabled by default. The baseline must stay reproducible from
  `config/default.yaml` alone.

Any enhancement must be configurable, documented, independently testable, and **disabled
by default**.
