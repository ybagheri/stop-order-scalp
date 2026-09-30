# MetaTrader 5 Setup

Covers installing the terminal's Python package, configuring this project for a given
machine, and — the part that is easy to get wrong — **which clock decides that a candle has
closed**.

---

## 1. Install the package

`MetaTrader5` is a broker-supplied binary wheel. It is **not** on PyPI, so
`pip install MetaTrader5` will not find it. It comes from your broker:

| Broker | Where to get it |
| --- | --- |
| Alpari / Exness / IC Markets / RoboForex and most MT4/MT5 white-labels | your broker's client-area download page, or `pip install MetaTrader5` if your mirror carries it |
| MetaQuotes demo terminal | bundled with the terminal, under `MQL5/Packages` |

Install it as the optional extra, so the project's own dependency list stays honest:

```bash
pip install -e ".[mt5]"
```

Confirm it imports:

```bash
python -c "import MetaTrader5; print(MetaTrader5.version())"
```

**The core project does not need it.** `pip install -e ".[dev]"` is enough to run the
entire test suite — 406 tests, no terminal, no broker. That is deliberate: a test suite
that needs a running terminal is a test suite nobody runs.

## 2. Configure `.env`

Copy `.env.example` to `.env` and fill it in. `.env` is git-ignored and may hold
credentials; `config/default.yaml` may not.

```ini
SOS_MT5_PATH=C:\Program Files\Alpari MT5_2\terminal64.exe
SOS_MT5_LOGIN=12345678
SOS_MT5_PASSWORD=your-password
SOS_MT5_SERVER=Alpari-Express-Demo
SOS_MT5_TIMEOUT_MS=60000
```

`SOS_MT5_PATH` is the only genuinely machine-specific value, and the only one that cannot
be discovered. It is never written into application code.

The terminal's data directory is derived by MetaTrader 5 from the executable path; do not
configure it. For reference, on the machine this was developed on it resolves to:

```
C:\Users\<user>\AppData\Roaming\MetaQuotes\Terminal\AF19ECCF568F855DF9D3196BBF8BF315
```

### Credentials are optional

If the terminal is already logged in, `initialize()` attaches to the saved session with no
credentials at all. That is the normal case. Supplying a login and password is what lets
the project attach to an account the terminal is *not* currently logged into.

Note what this means for `test-connection`: a successful probe proves the terminal is
reachable, **not** that these credentials are correct. Check the `credentials_configured`
field for the latter.

## 3. Check reachability

```bash
python -m stop_order_scalp test-connection
```

Read-only. Never places, modifies or cancels anything. Exit **0** connected, **3** not.

The report distinguishes failure modes, because "it didn't work" is not a diagnostic:

| Field | Meaning |
| --- | --- |
| `package_installed` | is the `MetaTrader5` wheel importable? |
| `terminal_path_configured` | is `SOS_MT5_PATH` set? |
| `credentials_configured` | are login **and** password present? |
| `terminal_running` | did `initialize()` succeed? |
| `version`, `terminal_build` | terminal build numbers |
| `account_login`, `account_server`, `is_demo` | which account we landed on |
| `error`, `last_error` | the failure, with the terminal's own code |

On a machine with no package installed:

```json
{
  "connected": false,
  "package_installed": false,
  "error": "the MetaTrader5 package is not installed; install the broker-supplied package with: pip install -e \".[mt5]\""
}
```

## 4. Which clock decides a candle is closed

This is the part of the MT5 integration most likely to be got wrong, and the reason
`ServerClock` exists as a separate class.

### The three clocks

| Clock | Source | Used for |
| --- | --- | --- |
| **Broker server time** | the terminal's `time_current()` | **deciding whether a bar has closed** |
| UTC | `datetime.now(UTC)` | timestamps, logging, deadlines |
| Local machine time | the PC's zone | nothing that affects a decision |

### Why server time decides closure

MetaTrader 5 anchors daily bars, session boundaries and symbol trading hours to **server**
time. For most brokers that is UTC+2 or UTC+3, not UTC.

A bar stamped `12:00` server time closes at `12:00` server time. On a machine running UTC,
local time does not reach that instant until `14:00`. Every "is this bar closed" answer
computed against the local clock would therefore be **up to three hours late**, and on an
hourly or daily timeframe it would be wrong for hours at a stretch.

The failure is silent and looks like a strategy that "works". That is exactly why it is
handled structurally:

* `ServerClock` implements the same `Clock` protocol as the system clock, so it is injected
  the same way and there is no second code path to remember.
* `MT5Feed.candles()` judges closure against `ServerClock`, never against local time.
* Nothing in `market_data/` reads a clock directly. `scripts/check_architecture.py` fails
  the build if one starts to.

### What UTC is used for, and the one limit worth knowing

Bar and tick timestamps arrive as Unix epoch seconds, which are absolute instants, so they
are converted with `datetime.fromtimestamp(value, tz=UTC)` and no broker offset is applied.
Server time enters only through `ServerClock`, as the reference a bar is compared *against*.

`floor_time()` floors an instant in UTC. MetaTrader 5 floors in server time. **These agree
for M1 and M15 — the only timeframes this strategy uses — for every common broker offset**,
because UTC, UTC+1, +2, +3, +4, +5, +6 and +8 are all whole multiples of 15 minutes.

They do **not** agree for daily and larger timeframes at an offset that is not a whole
number of hours. A broker at UTC+5:30 has a server day that starts at 18:30 UTC the
previous day, so UTC-flooring misplaces a `D1` bar by 5.5 hours.

`tests/unit/test_candles.py::TestTimezoneHandling` states both halves of this: a
parametrised test over the common offsets, and a test that asserts the daily case is *not*
aligned. If anyone reaches for a daily timeframe, the boundary is already written down.

### When the clock degrades

`ServerClock` falls back to the injected clock if the terminal is unreachable, or if
`time_current()` returns zero (which happens straight after a connect, before the first
tick). It sets `degraded` when it does. The fallback is visible rather than silent, because
an invisible substitution of the wrong clock is precisely the failure this design exists to
prevent.

## 5. Symbol resolution

Brokers expose the same index under different names: `US30`, `US30.cash`, `US30m`, `DJ30`.
`config/default.yaml` lists them:

```yaml
symbol: US30
symbol_aliases: [US30, US30.cash, US30m, DJ30]
```

Matching is **exact and case-insensitive**, over the configured name first and then the
aliases in order. There is no substring or fuzzy match, so `US30` can never select
`US30mini` or `EURUSD30` by accident. If none of the names is available, `resolve_symbol`
raises `SymbolNotFoundError` naming every name it tried rather than silently trading
something else.

`US30` is a broker-specific name. Alpari, for instance, commonly offers `US30` and
`US30.cash`. Whatever your broker's exact spelling is, add it to `symbol_aliases` and
nothing else needs to change.

## 6. Symbol specifications

The broker's own `symbol_info` is converted to a `SymbolSpecification` at runtime: point,
tick size, tick value, contract size, volume limits and step, stops level, freeze level.
Nothing is hard-coded.

If the broker omits a field without which no arithmetic is possible — `trade_tick_value`,
`trade_contract_size` — the instrument is **refused** with a message naming the field,
rather than defaulted. A guessed tick value would silently mis-size every position.

The **real** values for this broker's `US30` are recorded in
[`SYMBOL_SPECIFICATIONS.md`](SYMBOL_SPECIFICATIONS.md). Until Phase 11 captures them from a
live terminal, every risk figure in the test suite rests on the synthetic specification in
`tests/conftest.py` and is labelled as assumed.

## 7. The closed-candle rule

`copy_rates_from_pos` returns the **newest** rows first, and the newest row is the bar in
progress. So:

* `MT5Feed.freeze()` requests `count + 1` rows and lets `freeze_closed_bars` drop the
  forming one. Requesting exactly `count` and trusting the feed to exclude it would make
  the freeze depend on terminal behaviour this project does not control.
* `MT5Feed.candles()` returns only closed bars, oldest first. Forming bars are reachable
  **only** by calling `forming_candle()` by name.
* Every freeze returns a `FreezeReport` recording what was withheld. "Which bars did the
  decision actually see" is the first question asked when a result looks wrong.
* The reference moment is an argument. `candles(..., before=...)` overrides the server
  clock, which is what makes historical replay independent of when the call happened.

`freeze_closed_bars` is pure: no I/O, no clock. Its properties are proved with `hypothesis`
in `tests/unit/test_candles.py`, including **appending future bars cannot change a decision
taken at time *t***.

## 8. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `package_installed: false` | wheel not installed, or wrong Python | `pip install -e ".[mt5]"`, confirm the interpreter matches |
| `error: terminal is not running` (code `-1`) | `SOS_MT5_PATH` wrong, or the terminal is already running under another user | check the path; close other MT5 instances |
| `connected: true` but `account_login: null` | attached to a terminal with no session | log in to the terminal, or supply credentials |
| `SymbolNotFoundError` | broker uses a different name | add it to `symbol_aliases` |
| `no 'time' field` / `mid price is not acceptable` | the row shape is not what the converter expects | check the terminal build against the field names in `mt5_module.py` |
| `copy_rates_from_pos ... failed` | symbol not selected, or the market is closed | resolve the symbol first; confirm the terminal is logged in |
| `ServerClock.degraded` is true | terminal unreachable or not yet ticking | connect first; do not trade while degraded |

## 9. What Phase 2 does not do

* It does not place, modify or cancel anything. `MT5Feed` has no order methods, and
  `tests/unit/test_mt5.py` asserts they are absent. Order placement is Phase 5.
* It does not validate the broker's numbers against reality. That is Phase 11, against a
  live terminal.
* It does not claim the feed is correct against a real broker. Everything in
  `tests/unit/test_mt5.py` runs against a hand-written fake that models the terminal's
  documented awkwardness — newest-first rates, `None` on failure, `False` from
  `initialize` — but a fake cannot prove the terminal behaves as documented. Only Phase 11
  can.
