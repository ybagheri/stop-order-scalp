# Architecture

Status: **Phase 1 implemented.** This document describes the shape the project is built
to; several layers are still empty by design and are filled in by later phases.

The authoritative statement of *why* each boundary exists is
[`PHASE0_AUDIT.md`](PHASE0_AUDIT.md) §5. This document describes *what* the boundary is.

---

## 1. The one-paragraph version

A strategy decides **what** to trade, a risk engine decides **how much**, an execution
layer decides **how to send it**, and a lifecycle decides **what happens next**. Each of
those talks to the outside world only through a `Protocol` declared in `domain/`, so the
business rules can be tested with no broker, no terminal and no network. MetaTrader 5 is
confined to two modules, and a CI script fails the build if it escapes.

## 2. Layers

Dependencies point strictly inward. A layer may import only from layers to its left.

```
cli            composition root, argument parsing, exit codes
application    TradingService: wires everything together, owns no rules
research       optional, disabled by default (Phase 12)
backtest       historical replay (Phase 9)
lifecycle      state machine, replacement, recovery (Phase 7)
execution      order and position management, gates (Phase 5, 6)
trailing       break-even, trailing stop (Phase 6)
risk           sizing, commission, SL/TP, validation (Phase 4)
strategy       M15 direction, M1 stop entries (Phase 3)
integrations   optional third-party adapters (Phase 8)
market_data    timeframes, candles, ticks, MT5 feed (Phase 2)
infrastructure config, logging, persistence, clock
domain         value objects, enums, models, exceptions, protocols
```

`domain`, `strategy`, `risk`, `trailing` and `lifecycle` are **broker-free**: they may not
import `MetaTrader5`, and they may not import the execution layer. Everything they need
from the outside arrives as an argument.

## 3. What is implemented today

| Module | Phase | State |
| --- | --- | --- |
| `domain/enums.py` | 1 | Complete — every enum the specification names |
| `domain/exceptions.py` | 1 | Complete — 22 types rooted at `StopOrderScalpError` |
| `domain/value_objects/price.py` | 1 | Complete — `Price`, `Money`, `Volume`, `SymbolSpecification` |
| `domain/models.py` | 1 | Complete — `Candle`, `Tick`, `TradePlan`, `OrderIntent`, `RiskAssessment`, … |
| `domain/interfaces.py` | 1 | Complete — `Broker`, `MarketDataProvider`, `Clock`, `AuditSink` |
| `infrastructure/config.py` | 1 | Complete — three-layer config, validated |
| `infrastructure/logging.py` | 1 | Complete — JSONL audit log, rotating |
| `infrastructure/clock.py` | 1 | Complete — injectable, tz-aware only |
| `market_data/timeframes.py` | 1 | Period seconds and boundary math |
| `market_data/candles.py` | 2 | Complete — the closed-candle freeze and its report |
| `market_data/mt5_module.py` | 2 | Complete — the one `import MetaTrader5`, the API protocol, pure converters |
| `market_data/mt5_feed.py` | 2 | Complete — `MT5Feed`, `ServerClock`, `probe_connection` |
| `cli/main.py` | 1/2 | Contract complete; `validate-config` and `test-connection` implemented |
| `strategy/`, `risk/`, `trailing/`, `execution/`, `lifecycle/`, `application/`, `backtest/`, `research/`, `integrations/` | 3–12 | Empty packages, present so the boundary is real from day one |

## 4. The protocols

`domain/interfaces.py` declares every outward boundary. This is what makes the rest of the
system testable without a broker.

| Protocol | Replaced in production by | Replaced in tests by |
| --- | --- | --- |
| `Clock` | `infrastructure.clock.SystemClock`, `market_data.mt5_feed.ServerClock` | `FixedClock` |
| `AuditSink` | `infrastructure.logging.AuditLogger` | `MemoryAuditSink` |
| `MarketDataProvider` | `market_data.mt5_feed.MT5Feed` | hand-written fake |
| `Broker` | `execution.mt5_broker.MetaTrader5Broker` | `execution.simulated_broker.SimulatedBroker` |

`SimulatedBroker` is **not** a test double. It is a first-class implementation and it is
what `DRY_RUN` and the backtester run on, so dry-run exercises the same code path that
live trading does.

## 5. Numbers are `Decimal`

`float` appears only at the MetaTrader 5 boundary and is converted on the way in.

* `Price` carries a `Decimal` **and** the symbol's digit count, so a `Price` cannot be
  rendered at the wrong precision.
* Arithmetic on `Price` yields a plain `Decimal`, because a bare `Decimal` does not know
  the digit count and must not be silently promoted back into a `Price`.
* Point ↔ price conversion only ever happens inside `SymbolSpecification`, which is built
  from the broker's own `symbol_info` fields. This is how *"do not assume 1 point = $1"*
  becomes structural instead of a comment: there is no code path that multiplies a point
  count by a price.
* `SymbolSpecification` validates itself on construction. An instrument whose numbers
  cannot support the arithmetic is rejected at the boundary.

## 6. Time is timezone-aware and injected

* No naive `datetime` anywhere. Every timestamp is timezone-aware UTC.
* Business code never calls `datetime.now()`. It takes a `Clock`.
* `domain.models.utc_now()` is the one sanctioned seam, and the architecture script allows
  it precisely because its result is aware.
* A candle is closed when `open_time + period_seconds <= now`. `Candle.is_closed_at()`
  takes the reference moment as an argument, so the same candle always gives the same
  answer — a candle whose answer could change because the machine's clock moved would be
  untestable.

## 7. Configuration

Three layers, highest priority last:

```
config/default.yaml   the strategy, committed, machine-independent
        ↓ overridden by
.env                  this machine: terminal path, login, magic number, mode
        ↓ overridden by
real SOS_* variables  a shell, a service manager, CI
```

* An unknown key anywhere is a hard error naming the section. A typo is never ignored.
* `config/default.yaml` may not hold a credential. `.env` is git-ignored and root-anchored.
* `SOS_MT5_PASSWORD` is reduced to a **presence flag** at load time. No downstream code
  reads the variable, so there is no code path that could log it.
* `python -m stop_order_scalp validate-config` prints the resolved value of everything,
  which is the point: a silently-defaulted risk percentage is the failure mode the
  command exists to make obvious.

## 8. Secrets are structurally unloggable

The audit log event is a mapping with no secret field, and no field named like a
credential is accepted. This is stronger than a redaction filter, because a filter can be
forgotten, misconfigured, or bypassed by a new call site. There is nothing to forget.

## 9. The architecture gate

`scripts/check_architecture.py` turns the rules above into a build failure, and
`tests/unit/test_architecture.py` tests the gate itself — including deliberately broken
source, because a gate that cannot fail is worse than no gate.

| # | Rule |
| --- | --- |
| 1 | `MetaTrader5` only in `market_data/mt5_module.py` (which owns the import), `market_data/mt5_feed.py` and `execution/mt5_broker.py`, and only inside a function body |
| 2 | `albrooks` only in `integrations/al_brooks_adapter.py` |
| 3 | Layer direction; `domain` imports nothing but itself |
| 4 | No `float` annotation on a money-shaped name; no `float()` cast of one |
| 5 | No absolute machine paths in application source |
| 6 | No naive `datetime.now()` / `utcnow()`, and no direct `time` import, outside the three modules allowed to read the wall clock |

Run it with `python scripts/check_architecture.py`; it is also part of `pytest`.

> **Note.** All six rules were silently inert until Phase 1 stabilisation. The allowlists
> held unqualified module names while the checker compared fully qualified ones, the
> `float`-money pattern required a leading colon that `ast.unparse` never emits, and three
> `ast.unparse` calls crashed the script outright. The negative tests in
> `tests/unit/test_architecture.py` exist so this cannot recur silently.

> **Note.** Rule 1 now names three modules, but only `mt5_module.py` contains an
> `import MetaTrader5` statement; the other two reach the terminal through it. A test
> asserts that, so the count cannot grow quietly.

## 9a. Which clock decides what

The single most consequential thing in the market-data layer, and the reason
`ServerClock` is a separate class rather than a line inside the feed.

MetaTrader 5 anchors bars, sessions and trading hours to **broker server time**, which for
most brokers is UTC+2 or UTC+3. A bar stamped `12:00` server time closes at `12:00` server
time; on a machine running UTC, local time does not reach that instant for two or three
more hours. Judging closure against the local clock would therefore be *late* by hours —
and on an hourly or daily timeframe, wrong for hours at a stretch, while looking like a
working strategy.

So:

| Question | Clock |
| --- | --- |
| Has this bar closed? | **broker server time**, from `time_current()` |
| When did this bar/tick happen? | UTC, from the terminal's absolute epoch seconds |
| Scheduling, deadlines, log stamps | local / UTC, via `SystemClock` |

`ServerClock` implements the same `Clock` protocol as the system clock, so it is injected
identically and there is no second code path to remember. Nothing in `market_data/` reads a
clock directly — rule 6 would fail the build. When `ServerClock` falls back to the injected
clock because the terminal is unreachable, it sets `degraded`, because an invisible
substitution of the wrong clock is the exact failure this design exists to prevent.

One limit is documented and tested rather than left implicit: `floor_time` floors in UTC,
MetaTrader 5 floors in server time, and the two agree for M1 and M15 — the only timeframes
this strategy uses — for every common broker offset, but not for `D1` at a non-whole-hour
offset such as UTC+5:30. See `docs/mt5/SETUP.md` §4.

## 10. Exit codes

Part of the CLI contract, because a supervisor depends on them.

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Ran and failed (broker refused, order rejected) |
| 2 | Refused before doing anything (bad config, safety gate) |
| 3 | Not connected to MetaTrader 5 |
| 4 | Command is in the contract but its phase is not built yet |

Code 4 exists because Phase 1 fixes the command surface ahead of the components behind it.
A command whose phase has not landed reports that by name, rather than raising an
`ImportError` traceback at the operator.

## 11. Safety interlocks

Execution modes: `DRY_RUN` → `PAPER` → `DEMO` → `LIVE`, defaulting to `DRY_RUN`.

Each account-changing operation has its **own** opt-in (`SOS_ALLOW_ORDER`,
`SOS_ALLOW_CLOSE`), and `LIVE` additionally requires `SOS_ALLOW_LIVE=true`. These gates
land in Phase 5. The design intent is that there is no sequence of configuration mistakes
that reaches a live account, and no code path that upgrades itself.

## 12. Testing philosophy

Isolation is **by construction** — protocols plus hand-written fakes. There is no mocking
framework, no `skip` marker standing in for a missing dependency, and no MetaTrader 5 in
the unit suite at all. If a test needs a broker, it gets a `SimulatedBroker`.

Determinism comes from an injected `Clock`, a pinned `TZ`, and no `time.sleep`. Property
tests use `hypothesis` for the invariants that must hold for *all* inputs: point/price
round-tripping, trailing-stop monotonicity, and no-look-ahead.
