# Lifecycle and Recovery

How one trade at a time gets from a signal to its replacement, and what happens when the
process dies in the middle.

---

## 1. Write intent before act

Every order is written to a durable ledger **before** it is sent.

If the process dies between the write and the send, recovery finds an intent with no outcome
and knows to ask the broker. If it dies after the send but before the outcome is recorded, it
finds exactly the same thing. There is no window in which an order could exist at the broker
and be unknown locally, because the intent that could produce it was already durable.

Three properties, each existing because its absence produces a duplicate order:

**Atomic writes.** The state file is rewritten in full every time, so a half-written file is
unparseable — and unparseable is indistinguishable from lost. The write goes to a temporary
file in the same directory, is flushed and `fsync`-ed, then `os.replace`-d over the target. On
Windows `os.replace` is atomic where delete-then-write is not, which is why there is no unlink
step. The temp file shares a directory with the target so the replace stays on one filesystem;
a temp file on another volume would make it a copy.

**Refuse to overwrite.** `Ledger.record` raises rather than replacing an entry with the same
client tag. Two intents with one tag means the tag derivation has regressed, and silently
keeping the second would discard the record that explains the duplicate.

**Never start fresh.** `StateLedger.load` raises `PersistenceError` on an unreadable or
unparseable ledger. It never returns an empty ledger for a file that *exists*, because losing
the idempotency record is the condition under which duplicates appear. An absent file is
different: that is a genuine first run.

The file is JSON rather than SQLite because it must be readable by a human at 3am with a text
editor, and because one account's state is a few kilobytes. Money is serialised as a string,
never a float — a state file that round-trips through `float` would quietly lose precision on
exactly the numbers a duplicate investigation needs.

### Intent and outcome are separate records

An entry with no outcome is an **unresolved intent**: written, possibly sent, possibly not.
Modelling the outcome as a nullable column on one row invites a query that filters it away, so
`unresolved()` and `awaiting_confirmation()` are named properties and recovery asks by name.

`awaiting_confirmation()` is wider than `unresolved()` on purpose. An ambiguous send is
recorded `UNKNOWN`, which **is** an outcome — and it is exactly those entries that recovery
exists to resolve. Treating them as settled would leave the lifecycle in `VERIFYING` with
nothing left to verify.

---

## 2. The transition table

`LifecycleState` says what the system is doing; `TRANSITIONS` says what may follow. Any edge
not in the table raises `IllegalTransitionError`.

The reachable set is *data*, so it can be printed, reviewed and asserted against. That is the
difference from an orchestration written as branching conditions: the conditions may each be
defensible while the set of reachable paths is implicit.

```text
blocked -> halted, idle, reconciling
break_even_armed -> blocked, halted, position_closed, reconciling, trailing
halted -> idle
idle -> halted, reconciling, verifying, waiting_for_signal
pending_order_placed -> blocked, halted, position_open, reconciling, verifying, waiting_for_trigger
position_closed -> halted, reconciling, validating, waiting_for_signal
position_open -> blocked, break_even_armed, halted, position_closed, reconciling, trailing
reconciling -> blocked, halted, idle, pending_order_placed, position_closed, position_open, verifying, waiting_for_signal
signal_detected -> blocked, halted, validating, waiting_for_signal
trailing -> blocked, halted, position_closed, reconciling
validating -> blocked, halted, pending_order_placed, verifying, waiting_for_signal
verifying -> blocked, halted, pending_order_placed, position_closed, position_open, reconciling, waiting_for_signal
waiting_for_signal -> blocked, halted, reconciling, signal_detected
waiting_for_trigger -> blocked, halted, position_open, reconciling, verifying, waiting_for_signal
```

Three properties are asserted of the table itself, not of code that happens to take an edge:

**`HALTED` is absorbing.** Its only successor is `IDLE`. Anything else would let a halted
system resume on its own, which is what a halt is for. `LifecycleState.is_terminal` says the
same thing and the two are checked against each other.

**`VERIFYING` cannot reach `VALIDATING`.** This is the reason the state exists. Nothing may be
sent from the state that exists *because* a send's outcome is unknown, until broker state has
been read. An edge to `VALIDATING` would be a path from "maybe an order is out there" straight
to sending another one.

**No traps.** Every state can reach `IDLE` and can reach `RECONCILING`, computed by
breadth-first search over the table rather than asserted as a fixed list.

---

## 3. The gate is chosen by the venue, and the environment is always stated

Phase 5 left a known limitation: `OrderManager.place(settings=None)` skipped the gate entirely.
That was correct for `DRY_RUN` and `PAPER`, and wrong everywhere else — a caller holding a
real broker and omitting the argument would route orders ungated.

It is now closed at both ends:

* `place()` takes `settings` as a **required** keyword argument, so it cannot be forgotten.
* *Which* gate applies is decided once at construction, by the venue:
  `OrderGate` for a real venue, the new `SimulatedGate` for a simulated one.

`SimulatedGate` is **not** an open gate. It refuses `LIVE`:

```python
if settings.environment is Environment.LIVE:
    return refusal(GateRefusal.NOT_LIVE, "a simulated gate refuses LIVE; ...")
return OPEN
```

so a composition mistake that wires it to a real broker fails closed rather than trading. An
`OrderManager` built without naming a gate defaults to the **closed** `OrderGate`.

---

## 4. The loop

`TradeLifecycle.tick(decision)` runs three things in a deliberate order:

1. **Managed exits** — break-even and trailing. First, because a stop that has moved is
   protection that should be in place before any new risk is taken.
2. **Close detection** — a comparison, because the venue closes at stops and targets without
   telling this system.
3. **A new entry**, if a decision was offered and the machine is quiet.

### Close detection is a comparison, not an event

`_detect_closes` compares the positions it now sees against the ones it saw last. A
disappearance is what a close *looks like*. It cannot distinguish a stop-out from a
take-profit and deliberately does not try.

### The replacement is the same code as a first entry

The specification says to place a replacement immediately after a position closes.
`_on_position_closed` journals the close and stops there. It does **not** re-enter, because
"the position closed" is not evidence that a new trade is warranted — only a fresh decision
from closed candles is.

`POSITION_CLOSED` is a quiet state, so the next `tick` may be offered a decision and `_enter`
places it through `place_order`: same ledger write, same book re-read, same gate, same
duplicate checks. The replacement path and the first-entry path are one path, which is why
they cannot drift apart.

### Adopting a position the broker filled

`POSITION_OPEN` is only reachable through a placement or a trigger, so a book that shows an
open position while the machine sits in `IDLE` has no direct edge to it. `_adopt_open_position`
walks the real route — resting order placed, then filled — rather than resetting. Resetting
would paper over a genuinely unreachable state, and this is the situation where that matters:
the machine must always be able to describe the exposure it actually holds.

---

## 5. Recovery

The broker is authoritative. Not as a preference between two sources, but because local state
can be missing, truncated or never written, while the venue's book is what determines whether
this account holds exposure.

`Reconciler` is **read-only by construction**: it is handed a `Broker` and calls only
`positions` and `orders`. That is what makes it safe to run at startup before any gate is
open — it cannot trade even by mistake. `tests/unit/test_recovery.py` asserts this by giving
it a fake book with no write methods at all.

### Unattributable exposure stops the system

A position or working order carrying this magic number whose identity matches no ledger entry
raises `StaleStateError`. It may have been placed by a previous build, by hand in the terminal,
or by another tool sharing the magic number — and a working order can still fill. Guessing
here is how a real position ends up unattended, so the system stops and asks.

### What is deliberately *not* a contradiction

A ledger entry recorded `placed` at ticket 555, with neither a matching working order nor a
matching position, is the **ordinary filled-then-closed case**. An earlier version treated it
as a contradiction, which would have halted the system on every take-profit after every
restart. It is a note now, and the distinction is asserted by a test whose name says so.

### An unmatched intent stays unknown

"`Not on the book`" is not proof of "never sent". An intent that recovery cannot match stays
`UNKNOWN`, and the lifecycle re-reads on the next cycle rather than acting on the difference.
An unresolved intent also counts as `was_sent`, because treating it as "not sent" is precisely
how a restart becomes a duplicate order.

---

## 6. What is not verified

No order has been placed, so the ledger has never been written by a real crash. Atomicity and
refuse-to-overwrite are proved against a fake filesystem and an injected `OSError`, not against
a power cut. The recovery path has never been exercised against a real terminal.

`MetaTrader5` is not installed on the development machine. Phase 11 is where both get checked
against reality.