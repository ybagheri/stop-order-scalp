# MetaTrader 5

**Nothing here yet.** This directory is a placeholder so that the links in
[`README.md`](../../README.md) and [`ROADMAP.md`](../../ROADMAP.md) resolve.

The MT5 gateway lands in **Phase 2** and Phase 5, and will produce:

| Document | Phase | Covers |
| --- | --- | --- |
| `SETUP.md` | 2 | Installing the terminal, the Python API package, and configuring `.env` for a given machine |
| `SYMBOL_SPECIFICATIONS.md` | 11 | The **real** `US30` figures — point, tick size, tick value, contract size, volume step and limits, stops level, freeze level |

## Current status of the MT5 dependency

The terminal is configured on this machine but **nothing has been executed against it**.
Specifically:

* `MetaTrader5` is not installed in this environment, and no module imports it.
* The terminal path is set in `.env` (git-ignored):
  `C:\Program Files\Alpari MT5_2\terminal64.exe`.
* The terminal's data directory is derived by MetaTrader 5 from that executable path; it
  does not need to be configured.
* `python -m stop_order_scalp test-connection` currently exits **4** — the command is in
  the contract, but the module that implements it belongs to Phase 5.

## The one thing to be careful about

`MetaTrader5` may be imported by exactly two modules, and only lazily inside a function
body:

* `market_data/mt5_feed.py`
* `execution/mt5_broker.py`

`scripts/check_architecture.py` fails the build if that ever changes, and
`tests/unit/test_architecture.py` tests that the rule still fires.

## The `US30` specification is unknown

Every risk calculation in the test suite runs against a **synthetic** specification
(point `0.1`, tick `0.1`, tick value `1.0`/lot, contract size `1.0`, volume step `0.1`).
These are assumptions, chosen to make the "1 point ≠ $1" rule concrete. The real values
are captured in Phase 11 and will differ. Nothing about the strategy depends on the
particular numbers — only the sizing arithmetic does, and it goes through
`SymbolSpecification` for exactly that reason.
