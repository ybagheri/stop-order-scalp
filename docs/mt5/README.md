# MetaTrader 5

| Document | Phase | Covers |
| --- | --- | --- |
| [`SETUP.md`](SETUP.md) | 2 | Installing the terminal package, configuring `.env`, **which clock decides a candle is closed**, troubleshooting |
| [`SYMBOL_SPECIFICATIONS.md`](SYMBOL_SPECIFICATIONS.md) | 2 / 11 | The assumed `US30` figures, what they imply, and the checklist to capture the real ones in Phase 11 |

## Current status of the MT5 dependency

The feed is implemented and tested. The terminal is configured on this machine but
**nothing has been executed against it**.

* `MetaTrader5` is **not installed here**, and no module imports it at module scope.
* `SOS_MT5_PATH` is set in `.env` (git-ignored) to
  `C:\Program Files\Alpari MT5_2\terminal64.exe`.
* The terminal's data directory is derived by MetaTrader 5 from that path; it is not
  configured.
* `python -m stop_order_scalp test-connection` runs and reports
  `"package_installed": false` with the exact remedy, exiting **3**. It is read-only and
  never raises.

## The boundary

`MetaTrader5` may be named by exactly three modules, and only inside a function body:

| Module | Role |
| --- | --- |
| `market_data/mt5_module.py` | **owns the `import MetaTrader5` statement**, plus the typed protocols and the pure row→domain converters |
| `market_data/mt5_feed.py` | reaches the terminal through `MT5Module`; connection, symbols, candles, ticks, `ServerClock`, `probe_connection` |
| `execution/mt5_broker.py` | Phase 5 — order placement |

`scripts/check_architecture.py` fails the build if that changes, and
`tests/unit/test_architecture.py` asserts both that the allowlist is right *and* that the
import statement lives in only one market-data module.

## What is assumed and what is not

Every risk calculation in the test suite runs against a **synthetic** specification
(point `0.1`, tick `0.1`, tick value `1.0`/lot, contract size `1.0`, volume step `0.1`).
These are assumptions. On those numbers a 100-point move costs **$100 per lot**, not $100
per point — see [`SYMBOL_SPECIFICATIONS.md`](SYMBOL_SPECIFICATIONS.md) §2.

The real values are captured in **Phase 11**, and will differ. Nothing about the strategy
depends on the particular numbers: only the sizing arithmetic does, and it goes through
`SymbolSpecification` for exactly that reason.
