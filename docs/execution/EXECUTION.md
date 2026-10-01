# Execution Design

How a validated `TradePlan` becomes an order on a venue, and why the dangerous parts are
written the way they are.

---

## 1. The one question

MetaTrader 5 reports every failure as a numeric `retcode`. Behind every handling decision
sits a single question:

> **Did the order reach the venue or not?**

Answer it optimistically — assume an unfamiliar code means "not sent", so resend — and one
order becomes two positions. Answer it pessimistically for a code that actually means
"delivered" and a trade is missed until the next signal. So `execution/mt5_broker.py`
classifies into three buckets, and the middle one is the reason the module exists.

| Bucket | Meaning | Raised as | Retried? |
| --- | --- | --- | --- |
| accepted | executed, placed, or partially filled | — | n/a |
| terminal | refused on its merits | `BrokerRejectedError` | no — a resend is refused identically |
| retryable | "not now": market closed, requote, busy | `RetryableError` | by the caller, later |
| **unknown** | **the terminal cannot say** | **`ExecutionUnknownError`** | **never** |

The unknown bucket is `10011` (error, no detail), `10012` (timeout), `10031` (connection
lost), `10028`/`10029` (context busy or frozen), and `-1` (internal). A timeout is the
classic case: the request may well have been processed, and the terminal cannot tell us.

**An unrecognised retcode is treated as unknown, not terminal.** Being wrong pessimistically
costs one re-read. Being wrong optimistically costs a duplicate.

`tests/execution/test_mt5_classification.py` asserts this by enumeration rather than leaving
it to the reader: for every code in `_UNKNOWN`, the raised error is `ExecutionUnknownError`
and *not* `RetryableError`.

---

## 2. No send is ever retried

Not in the broker, not in the manager, not in `retry.py`.

```python
# execution/mt5_broker.py
result = api.order_send(request)
classify(int(field(result, "retcode", -1) or 0), ..., context="placing ...")
# exactly one call. classify() raises or returns; there is no loop.
```

`tests/execution/test_mt5_broker.py::TestNoRetry` asserts `len(terminal.sent) == 1` after an
ambiguous failure, for five different retcodes.

### What happens instead

An unknown outcome leaves the state machine in `VERIFYING` (Phase 7) and the caller
re-observes broker state. `SimulatedBroker.fail_next_send(outcome="unknown")` models the real
case honestly — the order **is** on the book and the caller was never told:

```python
broker.fail_next_send(outcome="unknown")
outcome = manager.place(broker, plan)

assert outcome.code == "execution_unknown"
assert len(broker.orders()) == 1        # it really is there
assert len(broker.send_attempts) == 1   # and was not sent twice
```

The next cycle re-reads the book, finds the order, recognises the tag and refuses as a
duplicate. That recovery path is tested
(`test_re_observation_finds_the_order_so_the_next_attempt_is_a_duplicate`) because without
it the caller would be forced to choose between a phantom order and a real duplicate.

### Reads are different

Reading the book twice returns the same answer, so `execution/retry.py` retries reads with
bounded exponential backoff. `ExecutionUnknownError` propagates immediately rather than being
ridden out — a test asserts both the attempt count and that no sleep occurs.

The bound matters as much as the schedule: unbounded retry against a wedged terminal becomes
a hot loop against a socket that will never answer.

---

## 3. Idempotency

Broker state is re-read **immediately before every send**:

```python
# execution/order_manager.py, in this order:
gate.check(settings)      # cheap, local
assess(plan, broker)      # re-run risk against live balance
has_position(broker, plan)
is_duplicate(broker, plan)  # <-- the read that must not be stale
broker.place_order(intent)
```

The local checks come first so an obviously-refused trade costs no broker round trip. The
book read comes **last**, immediately before the send, so it cannot be stale by the time it
matters.

A cached read from earlier in the cycle is correct until the process is interrupted — and an
interruption between "decided to place" and "read the book" is precisely the case that
duplicates a position.

### Matching on tag, not price

The duplicate check matches on the **client tag**, never on price. Price matching would treat
a genuinely new setup at the same level as a duplicate, and would miss a duplicate whose
price moved with the tick.

The tag is derived in exactly one place — `OrderIntent.client_tag_for(plan)` — from the plan
id, both timeframes, both candle open times and the side:

```python
f"{plan.plan_id}|{signal.direction_timeframe}@{...direction_candle_open_time}"
f"|{signal.timeframe}@{...source_candle_open_time}|{signal.side}"
```

A second derivation would eventually disagree with the one that builds the order, and at that
moment duplicate protection silently stops working. A test compares the two.

Because identity includes the authorising candle, a new candle produces a different tag —
which is what lets Phase 7 replace a stale pending order instead of adopting it.

---

## 4. Identity across the terminal boundary

MetaTrader 5 has no client-order-id field, so the **comment** is the only channel by which
identity survives the round trip. `encode_comment` packs the tag first and never truncates
it; the prose is what gets cut:

```python
encode_comment("0123456789abcdef0123", "buy stop US30")   # 31 chars
decode_comment(...)  # ("0123456789abcdef0123", "buy stop U")
```

A documented consequence: with a 20-character tag, the human-readable half has about ten
characters. Identity is worth more than prose, so the tag wins. The budget is small enough
to be a test rather than a surprise.

An `sl` or `tp` the terminal reports as `0` means *no stop set*, and reads back as `None` —
not as a stop at price zero.

---

## 5. The gates

Three independent switches. Any two are not enough:

| # | Switch | Source |
| --- | --- | --- |
| 1 | environment is `LIVE` | `SOS_ENVIRONMENT` |
| 2 | `allow_live` | `SOS_ALLOW_LIVE` |
| 3 | the operation's own switch | `SOS_ALLOW_ORDER` / `SOS_ALLOW_CLOSE` |

Both gates default to **closed**. `require_configured` rejects `LIVE` without `allow_live` at
load time, so an impossible combination is reported before it matters.

Closing is a separate switch from opening because the risks differ: an operator content to
open positions is not automatically content to have them closed automatically, and on a
losing streak automatic closing is exactly when it would fire.

Checks run cheapest-first and most-fundamental-first, and the **first** failure is returned,
so the reason reported is the one to act on.

---

## 6. `OrderManager.place`

Refusals are **values**, not exceptions. Refusing a duplicate is correct behaviour that
happens routinely — a tick-driven loop asks many times and is told "already there" most of
them — so raising would make the normal path exceptional.

```python
class RefusalCode:
    NOT_ASSESSED        = "risk_not_accepted"
    GATE_CLOSED         = "order_gate_closed"
    DUPLICATE_ORDER     = "duplicate_pending_order"
    ALREADY_IN_POSITION = "already_in_position"
```

`PlacementOutcome.require_order()` exists for a caller that genuinely cannot continue without
an order.

### Known limitation

`place(settings=None)` skips the gate check entirely. That is deliberate for `DRY_RUN` and
`PAPER`, where the venue is the simulator and no money is at stake — but it means a caller
that passes a *real* broker and forgets `settings` would route orders ungated. Phase 7
wires the composition root and should make the environment explicit rather than optional.
Tracked as a follow-up, not a defect in the current call paths.

---

## 7. `SimulatedBroker`

Not a test double. It is what `DRY_RUN` and `PAPER` run against, which is the only reason a
dry run proves anything.

Modelled, because a real venue does it:

* bid/ask — a buy fills at the ask, a sell at the bid;
* pending stops resting on the book until price crosses them;
* SL and TP checked on every price move, on the correct side;
* commission charged on close, so a flat trade is a small loss;
* magic numbers, so two strategies can share an account.

Deliberately **not** modelled, because inventing it would be worse than its absence: slippage
beyond the spread, partial fills, requotes, latency, margin constraints.

Floating P/L is **gross**, matching what a live terminal shows; commission is charged once,
at close. Charging it in both places would show a live account less equity than it has.

Every method takes an injected clock. Nothing reads a wall clock, which is what makes a
replay reproducible.

---

## 8. What is not verified

`MetaTrader5` is not installed on the development machine. Everything above is proven against
fakes and the simulator; the wire values, the retcode table and the tag encoding have never
met a real terminal. `test-connection` correctly exits 3 and reports `package_installed:
false`.

No live order has been placed. `DRY_RUN` is the only mode exercised end to end.