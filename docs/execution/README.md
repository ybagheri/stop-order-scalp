# Order Execution

| Document | Covers |
| --- | --- |
| [`EXECUTION.md`](EXECUTION.md) | Identity, idempotency, error classification, and why a send is never retried |

## What exists

| Module | State |
| --- | --- |
| `execution/gates.py` | Complete — `OrderGate`, `CloseGate`, `LiveInterlock`; all default closed |
| `execution/order_manager.py` | Complete — idempotent placement, duplicate and position checks |
| `execution/mt5_broker.py` | Complete — native MT5 `Broker` plus retcode classification |
| `execution/simulated_broker.py` | Complete — in-memory venue with bid/ask, stops, commission |
| `execution/retry.py` | Complete — bounded exponential backoff, safe reads only |

145 tests in `tests/execution/`.

## The three ideas

**1. A send is never retried.** A retcode the terminal cannot interpret becomes
`ExecutionUnknownError`, which is never retryable. The order may already be on the venue,
so the only correct response is to re-read broker state. Retrying is how one order becomes
two. Reads *are* retried, with bounded exponential backoff, because reading the book twice
returns the same answer.

**2. Broker state is re-read immediately before every send**, not cached from earlier in the
cycle. A cached read is correct until the process is interrupted — and an interruption
between "decided to place" and "read the book" is exactly the case that duplicates a
position.

**3. Identity is deterministic and survives the round trip.** The client tag is derived from
the plan and the candle that authorised it, and is packed into the terminal's 31-character
comment field so duplicate detection still works after a restart.

## The gates

Three independent switches stand between a configuration and a live order. Any two are not
enough:

1. `environment == LIVE`
2. `SOS_ALLOW_LIVE`
3. the operation's own switch — `SOS_ALLOW_ORDER` or `SOS_ALLOW_CLOSE`

Both gates default to **closed**, and closing positions has a switch separate from opening
them: automatic closing on a losing streak is exactly when an operator wants it off.

## Not yet true

The MT5 broker has never touched a terminal. `MetaTrader5` is not installed here, so
`test-connection` exits 3 and everything above is proven against fakes and the simulator.
The classification table, the wire values and the tag encoding are all unverified against
real retcodes. Phase 11 is where that happens.