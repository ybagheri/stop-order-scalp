# US30 Symbol Specification

> **These numbers are assumed, not measured.** They are the project's working assumption
> until Phase 11 captures the real values from a live Alpari terminal. Every risk
> calculation in the test suite is stated against them, and none of them has been verified
> against a broker.
>
> The strategy does not depend on these particular values — only the position-sizing
> arithmetic does, and that goes through
> [`SymbolSpecification`](../../src/stop_order_scalp/domain/value_objects/price.py), which
> is built from the broker's own `symbol_info` at run time. So when the real numbers arrive,
> this document changes and no code does.

---

## 1. The assumed specification

This is the object in `tests/conftest.py::us30_spec`, chosen to make the "1 point ≠ $1"
rule concrete:

| Field | Assumed value | MT5 field it comes from |
| --- | --- | --- |
| `name` | `US30` | `name` |
| `digits` | `1` | `digits` |
| `point` | `0.1` | `point` |
| `tick_size` | `0.1` | `trade_tick_size` |
| `tick_value` | `1.0` per lot | `trade_tick_value` |
| `contract_size` | `1.0` | `trade_contract_size` |
| `volume_min` | `0.1` | `volume_min` |
| `volume_max` | `50.0` | `volume_max` |
| `volume_step` | `0.1` | `volume_step` |
| `stops_level` | `10` points | `trade_stops_level` |
| `freeze_level` | `0` points | `trade_freeze_level` |
| `commission_per_lot` | `6.0` | broker-dependent, configured in YAML |
| `currency` | `USD` | `currency` |

## 2. What these numbers imply

Worked through by hand, because this is the calculation the whole risk engine rests on.

**Ten points is 1.0 price units.**

```
10 points x point 0.1              = 1.0 price units
1.0 price units / tick_size 0.1    = 10 ticks
10 ticks x tick_value 1.0          = $10.00 per lot
```

**A 100-point move costs $1000 per lot.**

```
100 points x 0.1                   = 10.0 price units
10.0 / 0.1                         = 100 ticks
100 x 1.0                          = $100.00 per lot
```

**At 0.5 % risk on a $10,000 account, that is 0.4 lots.**

```
risk budget   = 10000 x 0.005                  = $50.00
per-lot risk  = $100.00
per-lot commission (round trip, $6/lot)        = $6.00
per-lot total                                 = $106.00
volume        = 50 / 106                      = 0.4717
floored to volume_step 0.1                    = 0.4 lots
```

Cross-check: 0.4 lots carries $40.00 of price risk plus $2.40 of commission = $42.40,
inside the $50 budget. The naive figure, `50 / 100 = 0.5 lots`, carries $50.00 of price
risk *plus* $3.00 of commission = $53.00, which is **over budget** — so sizing on price
risk alone would over-size every position by 6 %.

This is exactly why `0.5 %` is compared against *total* risk, and why the sizing code
refuses to round *up* to `volume_min` when the computed size falls below it: up-rounding
would exceed the budget, which is the one thing a risk limit is for.

If the real `tick_value` differs, every one of these figures scales. That is the point:
**"1 point = $1" is false here by a factor of 100**, and no code path in this project
multiplies a point count by a price.

## 3. What must be captured in Phase 11

Read-only, against a demo terminal, with no orders of any kind:

- [ ] `digits`, `point`, `trade_tick_size` — is sub-point quoting in use?
- [ ] `trade_tick_value` **for 1 lot and for the minimum lot** — some brokers quote tick
      value per lot, some per contract, and index CFDs frequently quote it oddly
- [ ] `trade_contract_size` — 1 for a cash index, 100 for a futures-style contract
- [ ] `volume_min`, `volume_max`, `volume_step` — the real tradeable range
- [ ] `trade_stops_level` — how far from market a stop must sit. **This directly affects
      whether a 100-point stop is placeable**
- [ ] `trade_freeze_level` — the distance within which stops cannot be modified. **This
      directly affects break-even and trailing**: a 100-point trailing stop with a
      non-zero freeze level will be rejected near the entry
- [ ] `swap_long` / `swap_short` — not used by the baseline (positions are held for
      minutes, not days) but worth recording
- [ ] the real commission, and whether the broker charges per side or round trip
- [ ] server time offset versus UTC, and the terminal build
- [ ] whether `US30` or `US30.cash` is the tradeable one on this account

## 4. Consequence if the assumptions are wrong

Nothing breaks, and that is by design:

* Point→price and price→money conversions read the runtime `SymbolSpecification`, so
  wrong assumptions here cannot produce a wrong order price.
* A `tick_value` that is wrong by a factor makes the *size* wrong, not the *price*. That is
  a real risk, and it is why Phase 11 exists before any demo trading.
* A `stops_level` larger than the configured 100-point stop distance would make the stop
  unplaceable. The risk engine validates against `stops_level` in Phase 4 and will refuse
  rather than send something the broker will reject.
* A non-zero `freeze_level` would cause break-even and trailing modifications to be
  rejected near entry. Phase 6 accounts for it; this document is where the real value goes.

## 5. How to fill this in

```bash
python -m stop_order_scalp test-connection      # proves reachability, read-only
```

Then, from a Python shell with `MetaTrader5` installed and the terminal logged in:

```python
import MetaTrader5 as mt5
mt5.initialize()
info = mt5.symbol_info("US30")
for field in ("name", "digits", "point", "trade_tick_size", "trade_tick_value",
              "trade_contract_size", "volume_min", "volume_max", "volume_step",
              "trade_stops_level", "trade_freeze_level", "currency", "leverage"):
    print(f"{field:24} {getattr(info, field, 'ABSENT')}")
print("server time", mt5.time_current(), "local", mt5.time_local(), "gmt", mt5.time_gmt())
```

Replace §1 with the output, note the server offset in §3, and cite the terminal build.
Nothing else in the repository needs to change.
