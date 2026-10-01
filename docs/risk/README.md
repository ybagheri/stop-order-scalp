# Risk

| Document | Covers |
| --- | --- |
| [`RISK_MODEL.md`](RISK_MODEL.md) | How a position is sized, where the stop and target go, and what makes the engine refuse |

## What exists

| Module | State |
| --- | --- |
| `risk/commission.py` | Complete — round-trip versus per-side, read from configuration |
| `risk/position_sizer.py` | Complete — `percent_balance` and `fixed_lot` |
| `risk/stop_loss.py` | Complete — fixed-points, risk-reward and signal-defined stops |
| `risk/take_profit.py` | Complete — three providers plus the precedence rule |
| `risk/risk_manager.py` | Complete — sizing, validation, and stable rejection codes |

Pure: no clock, no I/O, no broker. 140 tests in `tests/risk/`.

## The one number that matters

0.5 % of a $10,000 account, a 100-point stop, $6/lot commission:

```
one lot costs  $100.00 price risk + $6.00 commission = $106.00
budget        $50.00
volume        50 / 106 = 0.4717  ->  floored to 0.4 lots
total risk    0.4 x $106.00      =  $42.40  (0.424 % of balance)
```

Sizing on `price_risk` alone would give 0.5 lots carrying **$53.00 of a $50.00 budget** — a
6 % overshoot on every trade, silent. That is why the three figures are separate fields and
why `total_risk` is the divisor.

Full derivation in [`RISK_MODEL.md`](RISK_MODEL.md) §3.

## Still assumed

Every figure uses the **assumed** `US30` specification. Phase 11 captures the real one, and
the arithmetic will rescale with it — the structure will not.
