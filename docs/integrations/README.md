# Al Brooks Integration

| Document | Covers |
| --- | --- |
| [`AL_BROOKS.md`](AL_BROOKS.md) | The upstream contract, the two switches, and why declining is not a trade |

## What exists

| Module | State |
| --- | --- |
| `integrations/al_brooks_adapter.py` | Complete — the **only** module that may import `albrooks` |
| `integrations/al_brooks_signal_provider.py` | Complete — chooses between the baseline and the engine |

57 tests in `tests/integrations/`, all driven by a **fake engine**. `albrooks` is not
installed on the development machine, so nothing here has met the real package.

## The two switches, and the default

| Configuration | Authoritative | Geometry source |
| --- | --- | --- |
| `enabled: false` — **the default** | the M15/M1 baseline | the baseline |
| `enabled: true`, `allow_geometry: false` | the engine | the baseline |
| `enabled: true`, `allow_geometry: true` | the engine | the engine |

With the default configuration the engine is **never constructed and never consulted**, so a
project with no `albrooks` installed behaves exactly as it did before this phase. That is
asserted with a factory that raises if called.

## Enabling it can reduce the number of trades

An engine `WAIT` becomes a `NoTrade`, not a fallback to the baseline. The engine reports
`"is_recommendation": False` about its own output, so treating a decline as "ask someone
else" would read it as a recommendation it explicitly disclaims.

A reviewer should read `enabled: true` as **"the engine now has a veto"**, not as "the engine
adds trades". There is a test whose name says exactly that.

## Not yet true

The adapter has never been run against the real `albrooks` package. The mapping is written
against the contract recorded in `docs/architecture/PHASE0_AUDIT.md` §3, not against observed
behaviour. If a future version renames `DecisionEngine` or changes `plan`'s shape, the failure
mode is a refusal with a named code — not a wrong trade — but that is by construction rather
than by observation.