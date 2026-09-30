# Testing

**`docs/testing/TESTING.md` does not exist yet** — this directory is a placeholder so the
links in [`README.md`](../../README.md) resolve. The full document lands with Phase 9.

This page is the short version, and it is accurate today.

## Running everything

```bash
python -m pip install -e ".[dev]"

python -m pytest                             # 406 tests, 1 skipped
python -m ruff check .                       # lint
python -m mypy                               # types, strict, src + tests
python scripts/check_architecture.py         # architecture boundaries
```

`mypy` and the architecture script also run as part of `pytest`, so a single
`python -m pytest` covers all four gates. Prefer `python -m …` over console scripts.

## Current suite

| File | Tests | Covers |
| --- | --- | --- |
| `tests/unit/test_mt5.py` | 72 | The MT5 boundary against a fake terminal: lazy import, converters, feed, `ServerClock`, `probe_connection` |
| `tests/unit/test_config.py` | 55 | Three-layer config, unknown-key rejection, `.env` precedence, secret handling |
| `tests/unit/test_value_objects.py` | 46 | `Price`, `Money`, `Volume`, `SymbolSpecification`, point/price arithmetic |
| `tests/unit/test_candles.py` | 43 | The freeze, the report, timezone handling, and the no-look-ahead properties |
| `tests/unit/test_architecture.py` | 34 | The architecture gate itself, including deliberately broken source |
| `tests/unit/test_timeframes.py` | 27 | Period seconds, boundary math, candle closure, timezone handling |
| `tests/unit/test_cli.py` | 25 | Parser surface, exit codes, commands whose phase has not landed |
| `tests/unit/test_logging.py` | 25 | JSONL audit records, rotation, structural absence of secrets |
| `tests/unit/test_interfaces.py` | 62 | The four protocols; every module imports; every `__all__` entry resolves |
| **Total** | **406** (1 skipped) | |

The one skip is the cross-check of `freeze_closed_bars` against the independent Al Brooks
implementation, which needs the optional `albrooks` extra. It is the only skip in the suite
and is acceptable only because that extra is genuinely optional.

## Principles

**Isolation is by construction.** Protocols plus hand-written fakes. There is no mocking
framework in this project, and no `skip` marker standing in for a missing dependency. If a
test needs a broker it gets a `SimulatedBroker`, which is a first-class implementation and
not a test double.

**There is no MetaTrader 5 in the unit suite at all.** The test suite passes on a machine
with no terminal installed, which is what makes it a real gate.

**A gate that cannot fail is worse than no gate.** `tests/unit/test_architecture.py`
therefore feeds deliberately broken source through the checker and asserts each rule
fires. Without those negative cases, a refactor of the checker could stop it checking
anything while the suite stayed green. That is not hypothetical — it is exactly what
happened in Phase 1, and the negative tests are why it was caught.

**Every module must import, and every `__all__` entry must resolve.**
`tests/unit/test_interfaces.py` asserts both across the whole package. This is how the
Phase 1 stabilisation found that `domain/interfaces.py` — the file declaring the project's
four core protocols — raised `TypeError` on import because `Broker` listed `Protocol`
before protocols that already inherit from it. The suite had been green, because nothing
imported the module.

**Determinism comes from injection.** An injected `Clock`, a pinned `TZ`, no `time.sleep`
in unit tests. `tests/conftest.py` also clears every `SOS_*` variable before each test, so
no test inherits another's configuration.

### Watch out for `.env`

`load_config(env_file=None)` auto-discovers a `.env` in the **current working directory**
and merges it into `os.environ`. That is intended production behaviour, and it means a
test that relies on the default will read the developer's real machine settings. The
helpers in `tests/unit/test_config.py` pass an explicit non-existent path to switch the
environment layer off; two regression tests in that file pin both halves of this
behaviour.

## Property tests

`hypothesis` is used where an invariant must hold for *all* inputs rather than for the
examples anyone thought to write:

* point ↔ price conversion never drifts
* normalized volume never exceeds the request
* **appending future candles cannot change a decision taken at time *t*** — the
  no-look-ahead property, and the single most important test in the project
* moving the reference time forward never *removes* a bar (monotonicity)
* the same input always produces the same output
* **Phase 6:** a trailing stop is monotonic — BUY `sl_new >= sl_old`, SELL
  `sl_new <= sl_old`

The no-look-ahead property deserves a note. It is stated as a property over generated bar
series rather than as one worked example, because an example only proves the function
behaved on the case somebody thought of. The generators vary the bar count, the offset of
the reference moment within the series, and the timeframe step, because the interesting
cases are the ones where the reference falls in the middle of a forming bar.

## Testing the MT5 boundary without a terminal

There is no `MetaTrader5` in this suite and no skip marker pretending otherwise. A fake
implementing `MT5Api` is enough, because the whole point of the protocol is that the rest of
the project never sees the real module.

The fake deliberately models the terminal's **awkwardness** rather than an idealised API:

* `copy_rates_from_pos` returns the **newest** rows first, and the newest is the bar in
  progress — which is why the feed asks for one bar more than it needs
* `initialize` returns `False` and populates `last_error` rather than raising
* `copy_rates_*` returns `None` on failure rather than raising
* rows are namedtuples, reached by attribute access
* `symbol_select` matches case-insensitively

Writing the fake this way paid for itself immediately: it exposed that the feed was
normalising ordering correctly while the test's own expectations were built on the wrong
assumption about which end of the table was newest.

**What this cannot prove:** that the real terminal behaves as documented. Only Phase 11,
against a live terminal, can do that.
