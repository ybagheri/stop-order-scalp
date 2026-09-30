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
        "--live",
        action="store_true",
        help="place real orders (refused unless SOS_ALLOW_LIVE=true and SOS_ALLOW_ORDER=true)",
    )
    run.add_argument("--max-cycles", type=int, default=None, help="stop after this many decision cycles")
    run.add_argument("--interval", type=float, default=None, help="override execution.poll_interval_seconds")

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
    backtest.add_argument("--output", type=Path, default=None, metavar="PATH", help="write the report to a file")

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
    return EXIT_OK if report["reachable"] or report["environment"] == "DRY_RUN" else EXIT_NOT_CONNECTED


def _cmd_test_connection(args: argparse.Namespace, config: AppConfig, out: TextIO, err: TextIO) -> int:
    probe_connection = _resolve("stop_order_scalp.market_data.mt5_feed", "probe_connection")

    report = probe_connection(config)
    _emit(out, report)
    return EXIT_OK if report["connected"] else EXIT_NOT_CONNECTED


def _cmd_run(args: argparse.Namespace, config: AppConfig, out: TextIO, err: TextIO) -> int:
    build_service = _resolve("stop_order_scalp.application.service", "build_service")

    service = build_service(config, dry_run=args.dry_run, paper=args.paper, live=args.live)
    cycles = service.run(max_cycles=args.max_cycles, interval=args.interval)
    _emit(out, {"command": "run", "cycles": cycles, **service.last_report()})
    return EXIT_OK


def _cmd_journal(args: argparse.Namespace, config: AppConfig, out: TextIO, err: TextIO) -> int:
    ledger_type = _resolve("stop_order_scalp.infrastructure.persistence", "JsonStateLedger")

    with ledger_type(config.paths.state_file) as ledger:
        entries = ledger.journal(limit=config.state.journal_limit)
        _emit(out, {"count": len(entries), "entries": entries})
    return EXIT_OK


def _cmd_diagnostics(args: argparse.Namespace, config: AppConfig, out: TextIO, err: TextIO) -> int:
    build_diagnostics = _resolve("stop_order_scalp.application.diagnostics", "build_diagnostics")

    _emit(out, build_diagnostics(config))
    return EXIT_OK


def _cmd_backtest(args: argparse.Namespace, config: AppConfig, out: TextIO, err: TextIO) -> int:
    run_backtest_from_args = _resolve("stop_order_scalp.backtest.runner", "run_backtest_from_args")

    report = run_backtest_from_args(args, config)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
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
