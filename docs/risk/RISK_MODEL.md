# Risk Model

How a position is sized, where the stop and target go, and what makes the engine refuse.

Implementation: `src/stop_order_scalp/risk/`. Tests: `tests/risk/`.

> **Every number below uses the *assumed* `US30` specification** from
> [`../mt5/SYMBOL_SPECIFICATIONS.md`](../mt5/SYMBOL_SPECIFICATIONS.md). Point `0.1`, tick
> value `1.0`/lot, volume step `0.1`. These are assumptions until Phase 11 captures the
> real values, and the arithmetic scales with them — the *structure* does not.

---

## 1. The three figures

The specification requires price risk, commission and total risk to be separately
visible, and they are three separate fields on `TradePlan` and `RiskAssessment`:

| Figure | What it is |
| --- | --- |
| `price_risk` | entry to stop, **commission excluded** |
| `commission` | estimated cost of the whole planned trade |
| `total_risk` | `price_risk + commission` |

**`0.5 %` is compared against `total_risk`.** Not `price_risk`. §3 works through why
that distinction is worth 6 % on every trade.

## 2. Sizing

```
ticks_per_lot  = (entry - stop) / tick_size
price_per_lot  = ticks_per_lot * tick_value
commission_lot = lots * commission_per_lot * mode.sides
total_per_lot  = price_per_lot + commission_lot

volume         = budget / total_per_lot          # then FLOORED onto volume_step
```

Two modes, and the budget is `balance * percent / 100` in `percent_balance`:

| Mode | Volume |
| --- | --- |
| `percent_balance` | `budget / total_per_lot`, floored |
| `fixed_lot` | `fixed_lot`, floored |

Three rules that are load-bearing:

**Balance, never equity.** Sizing from equity would silently increase risk after a losing
streak — exactly when risk should not grow.

**Every point conversion goes through `SymbolSpecification`.** No function in `risk/`
multiplies a point count by a price. This is what makes *"do not assume 1 point = $1"*
structural rather than a comment, and `test_points_are_not_dollars` changes `point`,
`tick_size` and `tick_value` to prove the answer moves with them.

**Rounding is always down.** A size rounded up is larger than the engine approved, which
defeats the point of a percentage of balance.

## 3. The worked example

0.5 % of a $10,000 account, a 100-point stop, $6/lot commission.

```
risk budget     = 10000 x 0.005                = $50.00

100 points x 0.1 (point)                       = 10.0 price units
10.0 / 0.1 (tick_size)                         = 100 ticks
100 x $1.00 (tick_value)                       = $100.00 price risk per lot
1 x $6.00 commission (round trip)              = $6.00 commission per lot
                                                  ------------
                                                  $106.00 total per lot

volume          = 50 / 106                     = 0.4717 lots
floored to 0.1 step                            = 0.4 lots

  price risk    = 0.4 x $100.00                = $40.00
  commission    = 0.4 x $6.00                  = $2.40
  total risk    = $42.40                       = 0.424 % of balance
```

**The naive calculation overshoots.** Sizing on `price_risk` alone gives `50 / 100 = 0.5`
lots, which carries `$50.00` of price risk *plus* `$3.00` of commission = **`$53.00`**, over
a `$50.00` budget. That is a 6 % overshoot on every single trade, and nothing raises.

This is the whole reason `total_risk` is a separate field and the reason the sizer divides
by it.

## 4. Refusing rather than rounding up

When the budget supports fewer lots than the broker's minimum, the engine **refuses**:

```
budget $50, but the smallest tradable position costs $106
  → 50/106 = 0.4717 lots < volume_min 0.1?  No — it floors to 0.4, which is fine.
```

and with a smaller budget:

```
budget $2.50 (on a $500 account)
  → 2.50/106 = 0.0236 lots, below volume_min 0.1
  → REFUSED, code "below_min_volume"
```

Rounding up to 0.1 lots would carry `$10.60` — **four times the budget**. The whole point of
a risk limit is that it binds, so `refuse_below_min_volume` (default `true`) refuses.
Setting it to `false` allows the clamp, and the result is flagged
`refused_below_minimum` so the journal shows it happened.

## 5. Commission interpretation

One configured number, two meanings, differing by a factor of two:

| `commission_mode` | $6/lot means | Cost per lot |
| --- | --- | --- |
| `per_lot_round_trip` | opening **and** closing | `$6.00` |
| `per_lot_per_side` | one side, doubled | `$12.00` |

The mode is read from configuration every time and **never inferred**, because reading it
wrong produces a position twice the intended size without raising anything. On the worked
example, per-side makes one lot cost `$112` and the size falls from 0.4 to 0.3 lots.

## 6. Stop loss

The baseline is `target.stop_loss_points` from entry — 100 points.

Rounding is asymmetric, and this is the part that matters:

* a **BUY** stop sits *below* entry, so away from entry means rounding **down**
* a **SELL** stop sits *above* entry, so away from entry means rounding **up**

A stop rounded toward entry by one tick is tighter protection than the risk engine
approved, which means the approved size is too large for the protection actually in place.
The real risk becomes a different number from the one that was checked.

This is `SymbolSpecification.round_stop_price`, anchored on the **entry**. It is *not*
`round_entry_price`, which is anchored on the candle extreme and rounds the other way.
Phase 3 found that mixing them up moves every level a tick toward the market; a test in
`test_stop_loss_take_profit.py` pins the difference.

## 7. Take profit and the precedence rule

Three sources are legitimate, and they are ranked rather than raced:

```
signal_defined  >  risk_reward  >  fixed_points
```

1. a target the **signal** carried — deliberately provided by whatever produced it;
2. a **ratio-derived** target, when a stop is known to measure against;
3. **fixed points** — the baseline, and the honest fallback.

The configured mode is a *preference*, not an override of a better answer. That is what
`TargetMode` means by it, and `resolve_target` implements exactly that order.

The baseline is **1000 points against a 100-point stop, i.e. 10:1** — not 1:1.
`risk_reward: 1.0` gives 1:1 instead, a 100-point target with that stop. Which one runs is
visible on every `TargetLevel.source` and in `describe_precedence()`.

## 8. Rejection codes

A rejection that only says "too risky" forces every caller to parse prose. A code can be
counted, alerted on, and compared across runs.

| Code | Meaning |
| --- | --- |
| `ok` | accepted |
| `account_not_tradeable` | the account reports trading disabled |
| `balance_not_positive` | nothing to size against |
| `no_stop_loss` | entry and stop coincide, so there is no risk |
| `stop_on_wrong_side` | the supplied stop would not protect the position |
| `stop_too_close` | closer to market than the broker's `stops_level` |
| `below_min_volume` | the budget cannot support the broker's minimum |
| `above_max_total_risk` | total risk exceeds the configured ceiling |
| `volume_above_broker_max` | the size exceeds `volume_max` |
| `specification_unusable` | the broker's numbers cannot support the arithmetic |
| `zero_cost_per_lot` | no price risk and no commission: nothing to size against |

**These strings are an interface.** Renaming one breaks every alert built on it, so add a
code rather than reword one. `tests/risk/test_risk_manager.py` asserts they are unique and
snake_case, and that every refusal names a known one.

## 9. Two independent brakes

`risk.percent` is the intent. `risk.max_total_risk_fraction` is a **second, independent
ceiling** on total risk, so a mistake in the first cannot widen it. It is configured as a
*fraction* (`0 < x <= 1`); the default `1.0` means disabled.

```yaml
risk:
  percent: 0.5
  max_total_risk_fraction: 1.0   # 1.0 = no ceiling
```

## 10. The property

> **the chosen volume never carries more total risk than the budget allowed**

Stated as a `hypothesis` property over generated specifications, balances, percentages,
commission rates and distances — because an example only proves the function worked on the
case somebody thought of. Alongside it:

* the size is always on the broker's volume step
* the size is always within the broker's min/max
* rounding never increases the size
* `total_risk == price_risk + commission`
* the same input always gives the same size

The headline one is also stated as `raw_volume` in the manager tests, because a fixture
that happened to divide exactly would not catch a rounding-up regression.

## 11. What this phase does not do

* **No order validation against live broker state.** `stops_level` is checked from the
  specification; the actual send-time rejection handling is Phase 5.
* **No duplicate detection, margin check, or spread check.** The roadmap lists these under
  `risk_manager`; the broker-facing ones belong to `execution/` in Phase 5, where live
  account state is available.
* **No profitability claim.** Every figure here is arithmetic on assumed inputs. Nothing
  has been run against market data.
