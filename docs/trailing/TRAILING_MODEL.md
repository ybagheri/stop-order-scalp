# Trailing and Break-Even

How a position's stop moves once it is in profit, and why a trailing stop that follows price
down is a bug rather than a setting.

---

## 1. The rule

From `docs/strategy/BASELINE.md` §5:

1. **Take profit** at 1000 points, or `1:1` risk/reward in `risk_reward` mode.
2. **Break-even** once price has moved **50 points** in favour: the stop moves to entry.
3. **Trailing** at **100 points**, **monotonic** — for a BUY the stop only ever rises, for a
   SELL it only ever falls.

With the project's US30 specification (`point = 0.1`), ten points is one price unit. A 100-point
trailing distance is therefore **10.0** in price terms. Every conversion goes through
`SymbolSpecification.points_to_price`, never a literal.

---

## 2. Monotonicity

> Monotonicity is not a preference. A trailing stop that moves backwards converts a winning
> trade into a losing one on spread alone. — `BASELINE.md` §5

The naive implementation follows price: a BUY stop sits at `bid − d`, unconditionally. A
pullback then walks that stop down through the entry and out the other side, closing a trade
that was well in profit minutes earlier at a loss.

**It is prevented structurally.** The provider returns *no proposal at all* when the computed
level is not an improvement on the stop already in place:

```python
if not _is_improvement(level, current, side):
    return TrailingDecision(None, TrailingRefusal.NOT_MONOTONIC, ...)
```

So there is no clamp applied afterwards and no code path that can emit a backwards move. The
invariant holds because of the *shape of the decision*, not because a later check caught it.

### Proved as a property

`tests/trailing/test_property_monotonic.py` runs generated price paths through both rules and
asserts over every stop that was ever in place:

* `hypothesis` walks over arbitrary paths, 200 examples;
* an **adversarial** generator: a run-up of up to 400 points followed by a reversal of up to
  400 — precisely the shape that tempts the stop backwards;
* hand-written V-shaped, flat and reversal paths, which a random walk rarely produces but
  which fail most clearly.

Plus a property for idempotency: the same stop level is never proposed twice along a path.

---

## 3. Break-even: the trigger is on the exit side

A BUY's profit is realised by selling at the **bid**. A SELL's, by buying at the **ask**. So
the trigger compares that price against the configured points:

```python
def exit_price_for(tick: Tick, side: Side) -> Price:
    return tick.bid if side is Side.SIDE_BUY else tick.ask
```

Measuring on the mid, or on the ask for a BUY, arms break-even while the position is still
underwater by the spread — and a stop at entry is then hit immediately. This is the spread
awareness the roadmap requires, and it is arithmetic rather than a filter.

### Commission-aware mode

`BREAK_EVEN_MODE_COMMISSION_AWARE` pushes the stop past entry by the estimated round-trip
cost, so a close at break-even is not a net loss:

| Mode | BUY target | SELL target |
| --- | --- | --- |
| `entry` | `entry` | `entry` |
| `commission_aware` (+30 points) | `entry + 3.0` | `entry − 3.0` |

The offset is **ignored entirely in `entry` mode**, so a configured `commission_points` cannot
silently apply to a mode that does not use it. A test asserts this.

---

## 4. Idempotency

A proposal that would not change the stop is refused, not sent:

| Situation | Code |
| --- | --- |
| Stop already at or beyond entry | `break_even_already_applied` |
| Trailing level would move the stop backwards | `trailing_not_monotonic` |
| Trailing level equals the current stop | `trailing_not_monotonic` |
| Move smaller than `min_step_points` | `trailing_below_min_step` |

The distinction between `no_change` and `not_monotonic` matters less than it looks: both mean
"leave the stop alone", but one is ordinary waiting and the other is the guard that just kept
a pullback from walking the stop back through entry. They are separate codes so an alert
distinguishes them.

Break-even is idempotent because its target is fixed. **Trailing is not**, so `min_step_points`
does that job there — the default of 1 point is the smallest move the broker could distinguish.

Without these, a market drifting a tick at a time produces one modify request per tick for
the lifetime of a position.

---

## 5. Broker limits

Two different constraints, often conflated:

| | Constrains | Refusal code |
| --- | --- | --- |
| `stops_level` | where a stop may be **placed** | `*_violates_stops_level` |
| `freeze_level` | whether an existing position can be **modified** at all | `*_inside_freeze_level` |

A request satisfying the first can still be refused by the second. Checking them separately is
what keeps a modification from being sent and then rejected with retcode `10016`.

Both are measured on the side the stop protects: a BUY's stop is below the market, so the
distance is `bid − stop`; a SELL's is above, so `stop − ask`.

> Both checks originally returned `bool | None` and were tested with `is not None`, which
> refused **every** modification — the "false" case and the "not applicable" case were the same
> value. The bug was silent: every outcome still carried a plausible-looking reason. It was
> caught by writing a test that asserted a modification *succeeds* on a normal specification,
> which is the assertion that turns out to be the load-bearing one.

---

## 6. Ordering, and why it is load-bearing

`PositionManager` evaluates break-even first. If it proposes, that proposal wins and trailing is
not consulted.

Both proposals are computed against the **same** position. Applying both in one tick therefore
applies a trailing level derived from a stop that the first has already moved. For a SELL whose
stop has just been pulled down to entry, `ask + d` lands *above* it — a backwards move.

This was found by the property test, not by inspection, and is now asserted directly in
`test_property_monotonic.py` with the reasoning in the comment.

Once break-even has been applied, subsequent ticks report `already_applied` (which counts as
*armed*), so trailing gets its turn from the next real move.

---

## 7. No send is ever retried

`PositionManager._apply` calls `modify_position` exactly once and lets exceptions propagate:

* `ExecutionUnknownError` — the stop may have moved; the caller re-reads broker state.
* `BrokerRejectedError` — the reason reaches the journal instead of becoming a silent no-op.

Retrying a stop modification is as wrong as retrying an order placement: the second request
either moves the stop again or is interpreted against a state that no longer exists.

Reads *are* retried, with bounded backoff, because reading the book twice returns the same
answer. Positions are read once per update.

---

## 8. What is not verified

`MetaTrader5` is not installed on the development machine. The `stops_level` and `freeze_level`
semantics encode documented broker behaviour that has not been observed. A broker that differs
would appear as refusals where a modification was expected — the safe direction to fail, but it
is unproven. Phase 11 is where that gets checked against a real terminal.