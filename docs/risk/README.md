# Risk

**Nothing here yet.** This directory is a placeholder so that the links in
[`README.md`](../../README.md) and [`ROADMAP.md`](../../ROADMAP.md) resolve.

The risk engine lands in **Phase 4** and will produce:

| Document | Covers |
| --- | --- |
| `RISK_MODEL.md` | Position sizing in both modes, the commission model, and why `0.5 %` is compared against *total* risk rather than price risk |
| `SYMBOL_SPECIFICATIONS.md` (in [`../mt5/`](../mt5/)) | The real `US30` figures, captured from the broker in Phase 11 |

What already exists and is worth reading today:

* The three distinct risk figures on `TradePlan` — see
  [`../strategy/BASELINE.md`](../strategy/BASELINE.md) §4.
* `SymbolSpecification.price_risk_for()` in
  `src/stop_order_scalp/domain/value_objects/price.py`, which is the only place a price
  distance becomes money, and which deliberately excludes commission.
* `tests/unit/test_value_objects.py`, which states every calculation against a synthetic
  `US30` specification.

**Until Phase 11 captures the real broker specification, every risk figure in this
repository rests on assumed values.** Treat them as such.
