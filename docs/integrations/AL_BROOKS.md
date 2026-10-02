# The Al Brooks Integration

An optional, **disabled by default**, third-party signal source. It is a filter, not a
strategy, and it cannot place an order.

---

## 1. The upstream contract

`al-brooks-price-action-engine` — import name `albrooks`, stdlib only, MIT. It has no broker,
no orders and no sizing. It produces *geometry*.

| Fact | Value |
| --- | --- |
| Distribution / import name | `albrooks` (no underscore) |
| Candle type | `Bar(time: float, open, high, low, close, volume, index)`, epoch seconds, **bar open time** |
| Forming-bar concept | **None.** A bar is closed iff `time + period_seconds <= now` |
| Signal type | There is **no `Signal` class.** Geometry is `TradePlan`; the action is `Decision` |
| `Decision.action` | `"BUY"` \| `"SELL"` \| `"WAIT"` \| `"NO_TRADE"` |
| `Decision.plan` | a dict with `direction` (+1 / −1 / 0), `entry`, `stop`, `target`, `reward_to_risk` |

That table is transcribed from `docs/architecture/PHASE0_AUDIT.md` §3. It is a *record of what
was read*, not of what was observed at runtime — the package is not installed on this
machine.

`Decision.to_dict()` hard-codes `"is_recommendation": False`. The engine is explicit that its
output is not a recommendation, and the adapter is built around that.

---

## 2. One module, one import

`integrations/al_brooks_adapter.py` is the only module permitted to `import albrooks`, enforced
by `scripts/check_architecture.py` and restated as a test that walks `src/`.

The import is *inside a function* (`import_engine`), for the same reason `MetaTrader5` is:
a module-level import would make the optional extra required, which is the opposite of what an
optional extra is for. A test asserts that importing the adapter does not put `albrooks` in
`sys.modules`.

`integrations/al_brooks_signal_provider.py` reaches the engine through an injected
`engine_factory`, so it has no import of it at all. A test parses the module's AST — rather
than grepping its text, which would match the word inside a docstring — and asserts the
provider cannot reach `place_order`, `cancel_order`, `modify_position` or `MetaTrader5`.

---

## 3. Two independent switches

| Configuration | Direction comes from | Geometry comes from |
| --- | --- | --- |
| `enabled: false` — **default** | the M15/M1 baseline | the baseline |
| `enabled: true`, `allow_geometry: false` | the engine | the baseline |
| `enabled: true`, `allow_geometry: true` | the engine | the engine |

They are independent on purpose. The common case is wanting the engine's read on *direction*
while keeping this project's own levels: taking both at once would make it impossible to tell
afterwards which rule set the entry, and a level with an untraceable origin is a level nobody
can debug.

The geometry source is recorded in the signal's `context`, so a journal entry says which
produced the number.

### The default is inert, and that is tested

`AlBrooksSettings()` defaults to `enabled=False, allow_geometry=False`. With that
configuration the provider does not construct the engine, import it, or consult it — asserted
by injecting a factory that **raises** if called. A deployment with no `albrooks` installed
therefore behaves exactly as it did before this phase, which is the entire point of the extra
being optional.

---

## 4. A suggestion is not a signal

| Engine action | Outcome |
| --- | --- |
| `BUY` | a `TradeSignal`, side BUY, order kind BUY **STOP** |
| `SELL` | a `TradeSignal`, side SELL, order kind SELL **STOP** |
| `WAIT` | `NoTrade` — **never** a signal, **never** a fallback |
| `NO_TRADE` | `NoTrade` |
| anything else | refused: `al_brooks_unknown_action` |

A `WAIT` is an *answer*. Turning it into a trade — in this direction or the other — would make
the adapter a source of trades rather than a filter. It becomes `NoTrade` with
`NoTradeReason.AL_BROOKS_VETO`, a code distinct from every reason the baseline produces,
because it is the only one caused by a third party and an operator whose trades have quietly
stopped needs to name it.

### Nothing is ever invented

The engine carries direction **twice** — in the action and in the plan — and the adapter
refuses when the two disagree rather than picking a winner:

| Contradiction | Code |
| --- | --- |
| action `BUY`, `direction` −1 | `al_brooks_direction_conflicts_action` |
| action `SELL`, `direction` +1 | `al_brooks_direction_conflicts_action` |
| action trades, `direction` 0 | `al_brooks_direction_missing` |
| no usable `entry` | `al_brooks_geometry_unusable` |
| unrecognised action | `al_brooks_unknown_action` |

A missing optional level is *not* a refusal: a plan with a direction but no stop still produces
a signal, because throwing away the part that is good over a missing extra would be the wrong
trade.

### Enabling it can reduce trades

Worth stating plainly, because it surprises people: with the engine enabled, a `WAIT` produces
**no trade where the baseline would have produced one**. There is a test named for exactly
this. Read `enabled: true` as *"the engine now has a veto"*.

The baseline is still evaluated on every call even when the engine is enabled. It costs
nothing, and it means an operator can see what the engine vetoed rather than discovering it by
counting orders.

---

## 5. What this cannot do

The provider produces a `TradeDecision | NoTrade` and nothing else. It cannot reach a broker,
size a position, or place an order — asserted by an AST test. So enabling it does not disturb
the Phase 7 invariant that every send goes through `place_order`'s ordering (gate → record →
re-read → send once → settle). A third-party signal source that could send would be a much
larger change, and is not this.

---

## 6. Not verified

The adapter has never run against the real package. If a future version renames
`DecisionEngine` or changes `plan`'s shape, `_default_engine` raises
`ComponentNotAvailableError` naming both candidates and pointing here, and the adapter refuses
unrecognised actions and contradictory directions by name. The failure mode is a refusal, not a
wrong trade — but that is by construction rather than by observation.

Phase 12 is the research layer; the honest end state is that this integration stays off unless
someone installs the extra and can watch what it vetoes.