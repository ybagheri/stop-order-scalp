# Position Management

| Document | Covers |
| --- | --- |
| [`TRAILING_MODEL.md`](TRAILING_MODEL.md) | Break-even arming, the trailing rule, and monotonicity as a proved property |

## What exists

| Module | State |
| --- | --- |
| `trailing/break_even.py` | Complete — trigger, commission-aware target, broker limits |
| `trailing/trailing_stop.py` | Complete — `bid − d` / `ask + d`, monotonic, minimum step |
| `execution/position_manager.py` | Complete — break-even then trailing, one request per change |

87 tests in `tests/trailing/`.

## The three ideas

**1. Monotonicity is structural, not a clamp.** The providers return *no proposal* when a
level would be worse than the stop already in place, so there is no code path that can emit a
backwards move. Proved as a `hypothesis` property over generated and adversarial price
sequences, not asserted on one example.

**2. The trigger is measured on the exit side.** A BUY's profit is realised selling at the
bid, so break-even compares `bid − entry` against the trigger. Measuring on the mid arms it
while the position is still underwater by the spread, and a stop at entry is then hit
immediately — a trade that was merely flat becomes a certain loss.

**3. Both rules are idempotent, so an ordinary tick costs no broker write.** Break-even's
target is fixed; trailing's `min_step_points` refuses moves too small to matter. Without the
second, a market drifting a tick at a time would produce one modify request per tick for the
lifetime of a position.

## Ordering is load-bearing

Break-even is evaluated first and wins outright; trailing is only consulted when break-even
proposed nothing. Both proposals are computed against the *same* position, so applying both
in one tick would apply a trailing level derived from a stop that had already moved — which,
for a SELL just pulled down to entry, lands *above* it and walks the stop backwards. The
property test was the first thing to find this.

## Not yet true

Nothing here has met a real terminal, and no position has been managed against one. The
`stops_level` and `freeze_level` refusals encode documented MetaTrader 5 semantics that have
not been observed in practice; a broker that behaves differently would show up as refusals
where a modification was expected, which is the safe direction to fail.

Phase 7 replaces the per-call evaluation here with the lifecycle's loop, and owns close
detection plus the replacement order.