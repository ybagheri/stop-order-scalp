# Lifecycle and Recovery

| Document | Covers |
| --- | --- |
| [`LIFECYCLE.md`](LIFECYCLE.md) | Write-intent-before-act, the transition table, and restart recovery |

## What exists

| Module | State |
| --- | --- |
| `infrastructure/persistence.py` | Complete — the state ledger: atomic writes, refuse-to-overwrite |
| `lifecycle/state_machine.py` | Complete — the transition table, a machine, and listeners |
| `lifecycle/recovery.py` | Complete — reconcile local state against the broker's book |
| `lifecycle/trade_lifecycle.py` | Complete — the loop: decide, act, record |

140 tests in `tests/unit/`: 44 for the transition table, 36 for the loop, 34 for the ledger,
26 for recovery.

## The one ordering that matters

```python
0. ask the gate                      # before anything is written
1. record the intent in the ledger   # durable, before anything can be sent
2. re-read the broker's book         # idempotency, immediately before the send
3. send, exactly once
4. settle the ledger entry
```

Step 1 before step 3 closes the window a **crash** would open. Step 2 immediately before
step 3 closes the window a **duplicate tick** or a **reconnect** would open. Step 0 before
step 1 means a gate refusal leaves no ledger trace — a recorded intent for a trade that never
existed would later be "resolved" by recovery as a phantom question.

## Four mechanisms, one catastrophe

The same duplicate order can arrive by four routes, so there are four guards. They are not
redundant:

| Cause | Guard | Reported as |
| --- | --- | --- |
| duplicate tick, mid-session | a resting order blocks any send | `busy` |
| restart, or a prior cancelled attempt | the ledger already holds the identity | `already_recorded` |
| a resting order with no ledger record | the book is re-read before the send | `duplicate` |
| an unknown send outcome | `VERIFYING`, which cannot reach a placement | `execution_unknown` |

## The broker is authoritative

Recovery compares local state against the broker's book and adopts the broker's answer. It is
**read-only by construction** — the reconciler has no write methods — so it is safe to run at
startup before any gate is open.

Exposure that cannot be *attributed* stops the system with `StaleStateError`. A position or a
working order carrying this magic number whose identity matches no ledger entry may become
exposure nobody is managing, and guessing is how that happens.

An intent recorded `placed` with an empty book is **not** a contradiction: that is the ordinary
filled-then-closed case, and treating it as one would halt the system on every take-profit
after every restart.

## Not yet true

No order has been placed, so the ledger has never been written by a real crash. The atomicity
and refuse-to-overwrite properties are proved against a fake filesystem and an injected
failure, not against a power cut. Phase 11 is where the recovery path gets exercised against a
real terminal.