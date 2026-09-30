# Strategy

| Document | Covers |
| --- | --- |
| [`BASELINE.md`](BASELINE.md) | The frozen strategy, and why the closed-candle and points-are-not-dollars rules matter |
| [`ENTRY_RULES.md`](ENTRY_RULES.md) | How the M15 direction candle and the M1 entry candle are selected, every no-trade reason, and the no-look-ahead property |

## What exists

| Module | Phase | State |
| --- | --- | --- |
| `strategy/candle_direction.py` | 3 | Complete — the M15 direction filter and its five verdicts |
| `strategy/entry_rules.py` | 3 | Complete — BUY STOP / SELL STOP placement |
| `strategy/signal.py` | 3 | Complete — `TradeDecision` or `NoTrade`, the instrument policy check |
| `strategy/strategy.py` | 3 | Complete — the `StopOrderStrategy` façade |

All four are **pure**: no clock, no I/O, no broker. The reference moment is an argument.
That is what makes the no-look-ahead property testable and what lets the whole layer be
tested without a terminal.

## The two rules, restated

* The **last fully closed M15 candle** decides the direction. Close above its open permits
  BUY, below permits SELL, exactly equal does not trade.
* The **last fully closed M1 candle** places the order: `high + 10 points` for a BUY STOP,
  `low - 10 points` for a SELL STOP, with the point resolved through `SymbolSpecification`.

See [`ENTRY_RULES.md`](ENTRY_RULES.md) for the full rule, the no-trade reasons, and the
proof obligations.

## Testing

`tests/strategy/` holds 119 tests:

| File | Tests | Covers |
| --- | --- | --- |
| `test_signal.py` | 37 | The decision, the no-trade reasons, the instrument policy, and the no-look-ahead properties |
| `test_candle_direction.py` | 33 | The five verdicts, closed-candle enforcement, the doji case |
| `test_entry_rules.py` | 25 | The two rules, points resolution, entry-vs-stop rounding |
| `test_strategy.py` | 24 | Construction refusals, dispatch, the self-describing rule |

The headline test is `test_appending_future_bars_cannot_change_the_decision`, a
`hypothesis` property over generated market data.

## Not trading

`NoTrade` is a first-class result, not an exception and not a null signal. Most of the time
the correct answer is to do nothing, and the reason is recorded so a quiet market can be
told apart from a broken feed.
