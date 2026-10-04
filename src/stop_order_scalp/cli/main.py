"""Command-line interface and composition root.

``argparse`` rather than a CLI framework. The command set is small and stable, the
interface has to work on a bare Python install, and every subcommand's contract is
"print JSON to stdout, print errors to stderr, return an exit code" -- which argparse
expresses in fewer lines than the configuration a framework would need.

Exit codes are part of the contract, because a supervisor process depends on them:

===== =========================================================================
Code  Meaning
===== =========================================================================
0     Success.
1     The operation ran and failed (broker refused, order rejected, assertion failed).
2     Refused before doing anything (bad configuration, safety gate, wrong environment).
3     Not connected to MetaTrader 5.
4     The command exists in the contract but the phase implementing it is not built yet.
===== =========================================================================

Every subcommand prints JSON to stdout so that output is machine-readable. Human-facing
text goes to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path
from typing import Any, Final, Protocol, TextIO

from stop_order_scalp.domain.exceptions import (
    ComponentNotAvailableError,
    ConfigError,
    StopOrderScalpError,
)
from stop_order_scalp.domain.version import __version__
from stop_order_scalp.infrastructure.config import AppConfig, load_config

__all__ = [
    "EXIT_CONFIG",
    "EXIT_FAILURE",
    "EXIT_NOT_CONNECTED",
    "EXIT_OK",
    "EXIT_UNAVAILABLE",
    "Handler",
    "build_parser",
    "main",
]

EXIT_OK: Final[int] = 0
EXIT_FAILURE: Final[int] = 1
EXIT_CONFIG: Final[int] = 2
EXIT_NOT_CONNECTED: Final[int] = 3
EXIT_UNAVAILABLE: Final[int] = 4


class Handler(Protocol):
    """The uniform shape every subcommand implements."""

    def __call__(
        self, args: argparse.Namespace, config: AppConfig, out: TextIO, err: TextIO
    ) -> int: ...


def _resolve(module: str, attribute: str) -> Any:
    """Import a component that a later phase owns, by name.

    Resolution is deliberately dynamic. The CLI contract is fixed in Phase 1 while the
    market-data, execution, lifecycle, persistence and backtest components behind most
    subcommands are built in Phases 2, 5, 7, 9 and 10. A static ``from ... import ...``
    would make the type checker demand modules that do not exist yet, and a bare
    ``import_module`` call would raise a bare ``ImportError`` traceback at run time. Going
    through this helper gives both: mypy sees a plain ``Any``, and the operator sees a
    named failure with a defined exit code.
    """
    try:
        return getattr(import_module(module), attribute)
    except (ImportError, AttributeError) as exc:
        raise ComponentNotAvailableError(
            f"{module}.{attribute} is not implemented yet; "
            f"'{_PROGRAM}' is at a phase where that command does not exist"
        ) from exc

_PROGRAM: Final[str] = "stop_order-scalp"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROGRAM,
        description=(
            "US30 stop-order scalping system. "
            "Default environment is DRY_RUN, which never writes to a broker."
        ),
        epilog=(
            "Run 'validate-config' first. Nothing here reaches live trading without "
            "SOS_ALLOW_LIVE=true, SOS_ALLOW_ORDER=true and an explicit --live flag."
        ),
    )
    parser.add_argument("--version", action="version", version=f"{_PROGRAM} {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="strategy configuration YAML (default: config/default.yaml)",
    )
    common.add_argument(
        "--env-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="machine-local .env (default: auto-discovered)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser(
        "run",
        parents=[common],
        help="run the trading loop",
        description="Run the decision loop against a broker or the simulator.",
    )
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="execute the full pipeline against the simulator")
    mode.add_argument("--paper", action="store_true", help="follow live prices, send nothing")
    mode.add_argument(
        "--demo",
        action="store_true",
        help="real market data from the terminal's DEMO account. Observes only unless "
        "--place-orders is also given",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="place real orders (refused unless SOS_ALLOW_LIVE=true and SOS_ALLOW_ORDER=true)",
    )
    run.add_argument(
        "--place-orders",
        action="store_true",
        help="with --demo: actually send orders to the demo account (also needs "
        "SOS_ALLOW_ORDER=true and SOS_ENVIRONMENT=DEMO)",
    )
    run.add_argument(
        "--max-orders",
        type=int,
        default=1,
        help="with --demo --place-orders: stop placing after this many orders (default 1)",
    )
    run.add_argument(
        "--duration",
        type=float,
        default=None,
        metavar="SECONDS",
        help="with --demo: stop after this long",
    )
    run.add_argument(
        "--keep-orders",
        action="store_true",
        help="with --demo: leave resting orders on the account when the run ends",
    )
    run.add_argument("--max-cycles", type=int, default=None, help="stop after this many decision cycles")
    run.add_argument("--interval", type=float, default=None, help="override execution.poll_interval_seconds")
    run.add_argument(
        "--candles",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "historical candles as CSV (time,open,high,low,close) for a dry run. "
            "Without it a deterministic synthetic series is used, which proves the wiring "
            "but says nothing about the strategy."
        ),
    )
    run.add_argument(
        "--m15",
        type=Path,
        default=None,
        metavar="PATH",
        help="separate M15 CSV for the direction filter; defaults to the --candles series",
    )

    subparsers.add_parser("status", parents=[common], help="report connection, account, symbol and state")
    subparsers.add_parser("validate-config", parents=[common], help="validate configuration and exit")
    subparsers.add_parser("test-connection", parents=[common], help="check MetaTrader 5 reachability, read-only")
    subparsers.add_parser("journal", parents=[common], help="print the trade journal")
    subparsers.add_parser("diagnostics", parents=[common], help="print an environment and configuration bundle")

    backtest = subparsers.add_parser("backtest", parents=[common], help="replay historical candles")
    backtest.add_argument("--data", type=Path, default=None, metavar="PATH", help="candle file (JSON or CSV)")
    backtest.add_argument("--symbol", default=None, help="override the configured symbol")
    backtest.add_argument("--from", dest="start", default=None, help="ISO-8601 start timestamp")
    backtest.add_argument("--to", dest="end", default=None, help="ISO-8601 end timestamp")
    backtest.add_argument("--slippage-points", type=float, default=0.0, help="adverse fill slippage, in points")
    backtest.add_argument(
        "--spread-points",
        type=float,
        default=None,
        help="bid/ask spread in points (default: 1, the legacy optimistic value; measured US30 on Alpari: 18)",
    )
    backtest.add_argument(
        "--intrabar",
        choices=("close", "auto", "ohlc", "olhc"),
        default="close",
        help="price path played inside each bar: close only (legacy); auto (by bar colour, "
        "recommended); or a fixed ohlc / olhc ordering to test sensitivity to it",
    )
    backtest.add_argument("--output", type=Path, default=None, metavar="PATH", help="write the report to a file")
    backtest.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="stop after this many bars; defaults to replaying the whole file",
    )

    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """Entry point. Returns an exit code; never raises for an expected failure."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        config = load_config(config_path=args.config, env_file=args.env_file)
    except ConfigError as exc:
        _fail(err, EXIT_CONFIG, f"configuration error: {exc}")
        return EXIT_CONFIG

    # ``required=True`` on the subparsers means argparse has already rejected any command
    # that is not registered, so every command reaching this line has a handler. A
    # KeyError here is therefore a wiring bug in this module, not an operator mistake,
    # and must not be swallowed into a polite exit code.
    handler = _HANDLERS[args.command]

    try:
        return handler(args, config, out, err)
    except ConfigError as exc:
        _fail(err, EXIT_CONFIG, f"configuration error: {exc}")
        return EXIT_CONFIG
    except ComponentNotAvailableError as exc:
        _fail(err, EXIT_UNAVAILABLE, str(exc))
        return EXIT_UNAVAILABLE
    except StopOrderScalpError as exc:
        _fail(err, EXIT_FAILURE, f"{type(exc).__name__}: {exc}")
        return EXIT_FAILURE
    except KeyboardInterrupt:
        _fail(err, EXIT_FAILURE, "interrupted")
        return EXIT_FAILURE


# =============================================================================
# Commands
# =============================================================================


def _cmd_validate_config(
    args: argparse.Namespace, config: AppConfig, out: TextIO, err: TextIO
) -> int:
    """Report what was resolved. A successful load *is* the validation.

    The value is in showing the operator exactly which file and which environment
    variable produced each value -- a silently-defaulted risk percentage is the failure
    mode this command exists to make obvious.
    """
    summary = config.summary()
    summary["sources"] = [str(source) for source in config.sources]
    summary["paths"] = {
        "root": str(config.paths.root),
        "config": str(config.paths.config_file),
        "state": str(config.paths.state_file),
        "log_directory": str(config.paths.log_directory),
    }
    summary["valid"] = True
    _emit(out, summary)
    return EXIT_OK


def _cmd_status(args: argparse.Namespace, config: AppConfig, out: TextIO, err: TextIO) -> int:
    collect_status = _resolve("stop_order_scalp.application.status", "collect_status")

    report = collect_status(config)
    _emit(out, report)
    # The contract this handler has always had, and which `collect_status` was written
    # against from the start: DRY_RUN is reachable by definition because its venue is
    # simulated, so the key is true even with no terminal anywhere near the machine. The old
    # handler raised KeyError on a report that did not carry it, which is a worse failure than
    # the exit code it was trying to produce.
    return EXIT_OK if report.get("reachable", True) else EXIT_NOT_CONNECTED


def _cmd_test_connection(args: argparse.Namespace, config: AppConfig, out: TextIO, err: TextIO) -> int:
    probe_connection = _resolve("stop_order_scalp.market_data.mt5_feed", "probe_connection")

    report = probe_connection(config)
    _emit(out, report)
    return EXIT_OK if report["connected"] else EXIT_NOT_CONNECTED


def _cmd_demo(args: argparse.Namespace, config: AppConfig, out: TextIO, err: TextIO) -> int:
    build_demo_service = _resolve("stop_order_scalp.application.demo", "build_demo_service")
    DemoUnavailable = _resolve("stop_order_scalp.application.demo", "DemoUnavailable")

    def log(line: str) -> None:
        err.write(line + "\n")
        err.flush()

    try:
        service = build_demo_service(
            config,
            place_orders=args.place_orders,
            max_orders=args.max_orders,
            cancel_on_exit=not args.keep_orders,
            log=log,
        )
    except DemoUnavailable as exc:
        _fail(err, EXIT_UNAVAILABLE, str(exc))
        return EXIT_UNAVAILABLE

    report = service.run(max_polls=args.max_cycles, duration_seconds=args.duration)
    _emit(out, {"command": "run --demo", **report})
    return EXIT_OK


def _cmd_run(args: argparse.Namespace, config: AppConfig, out: TextIO, err: TextIO) -> int:
    if args.demo:
        return _cmd_demo(args, config, out, err)
    if args.place_orders:
        _fail(err, EXIT_CONFIG, "--place-orders is only meaningful with --demo")
        return EXIT_CONFIG
    build_service = _resolve("stop_order_scalp.application.service", "build_service")
    load_candles_csv = _resolve(
        "stop_order_scalp.application.service", "load_candles_csv"
    )
    synthetic_candles = _resolve(
        "stop_order_scalp.application.service", "synthetic_candles"
    )
    aggregate = _resolve("stop_order_scalp.application.service", "aggregate")
    LiveTradingUnavailable = _resolve(
        "stop_order_scalp.application.service", "LiveTradingUnavailable"
    )

    entry_timeframe = config.strategy.entry.timeframe
    candles = (
        load_candles_csv(args.candles, timeframe=entry_timeframe)
        if args.candles is not None
        else synthetic_candles(120, timeframe=entry_timeframe)
    )
    m15 = (
        load_candles_csv(args.m15, timeframe=config.strategy.entry.direction_timeframe)
        if args.m15 is not None
        else aggregate(candles, entry_timeframe, config.strategy.entry.direction_timeframe)
    )

    try:
        service = build_service(
            config,
            dry_run=args.dry_run,
            paper=args.paper,
            live=args.live,
            candles=candles,
            m15_candles=m15,
        )
    except LiveTradingUnavailable as exc:
        _fail(err, EXIT_UNAVAILABLE, str(exc))
        return EXIT_UNAVAILABLE

    cycles = service.run(max_cycles=args.max_cycles or 10, interval=args.interval or 0.0)
    _emit(out, {"command": "run", "cycles": cycles, **service.last_report()})
    return EXIT_OK


def _cmd_journal(args: argparse.Namespace, config: AppConfig, out: TextIO, err: TextIO) -> int:
    ledger_type = _resolve("stop_order_scalp.infrastructure.persistence", "JsonStateLedger")

    # **Load, never construct** -- the same rule the trading path follows. Constructing here
    # would start from an empty ledger and then `__exit__` would flush that empty ledger over
    # the real file, so merely *asking what happened* would erase the record of what happened.
    #
    # **Scoped to the environment**, and the path is reported. `state/state.json` is the LIVE
    # ledger; a dry run's intents are not in it, and a journal that silently showed nothing
    # would read as "no trades" rather than as "you are looking at the wrong file".
    environment = _resolve("stop_order_scalp.domain.enums", "Environment")
    state_path = config.paths.state_file_for(environment.DRY_RUN)
    with ledger_type.load(state_path) as ledger:
        entries = ledger.journal(limit=config.state.journal_limit)
        _emit(
            out,
            {
                "ledger": str(state_path),
                "exists": state_path.exists(),
                "count": len(entries),
                "entries": entries,
                "note": (
                    "A default dry run keeps its ledger in memory and writes nothing, so this "
                    "is empty by design. Point state.path in config/default.yaml at a file to "
                    "persist a dry run's intents, or read the LIVE ledger at state/state.json."
                ),
            },
        )
    return EXIT_OK


def _cmd_diagnostics(args: argparse.Namespace, config: AppConfig, out: TextIO, err: TextIO) -> int:
    build_diagnostics = _resolve("stop_order_scalp.application.diagnostics", "build_diagnostics")

    _emit(out, build_diagnostics(config))
    return EXIT_OK


def _cmd_backtest(args: argparse.Namespace, config: AppConfig, out: TextIO, err: TextIO) -> int:
    run_backtest_from_args = _resolve("stop_order_scalp.backtest.runner", "run_backtest_from_args")

    # The runner writes --output itself, and it knows the path it wrote to. This handler used
    # to write it a second time from here, which meant the file was produced twice from two
    # places that could disagree about what it contains.
    report = run_backtest_from_args(args, config)
    _emit(out, report)
    return EXIT_OK


_HANDLERS: Final[dict[str, Handler]] = {
    "run": _cmd_run,
    "status": _cmd_status,
    "validate-config": _cmd_validate_config,
    "test-connection": _cmd_test_connection,
    "journal": _cmd_journal,
    "diagnostics": _cmd_diagnostics,
    "backtest": _cmd_backtest,
}


# =============================================================================
# Output
# =============================================================================


def _emit(stream: TextIO, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, indent=2, default=str, sort_keys=True) + "\n")
    stream.flush()


def _fail(stream: TextIO, code: int, message: str) -> None:
    stream.write(f"ERROR [{code}]: {message}\n")
    stream.flush()
