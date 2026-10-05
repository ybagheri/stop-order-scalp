"""Ask the terminal why it would refuse a pending order. Sends nothing.

    python scripts/diagnose_order.py --symbol BITCOIN --offset 500

It builds one BUY STOP a little above the market, exactly the shape the project sends, and
passes it to ``order_check`` -- the terminal's "would you accept this?" -- once for each filling
mode. ``order_check`` places no order and changes nothing on the account.

The first real run ended in ``retcode -1 ()``: ``order_send`` returned ``None``, which means the
request was refused before it left the terminal, and the reason is in ``last_error()``. This
prints that reason, plus the terminal switches that most often cause it.

``--offset`` is in points of the symbol (``point`` is read from the terminal).
"""

from __future__ import annotations

import argparse
import sys

#: The comment the project sent on the first real run: 31 characters, with '|' and '/'.
PROJECT_COMMENT = "de023fb22e2799f28867|SOS/m15_m1"


def _matrix(mt5: object, info: object, args: argparse.Namespace, price: float, stop: float,
            target: float) -> None:  # fmt: skip
    """Vary one thing at a time and show which change turns a refusal into an acceptance."""
    print("-- matrix (RETURN filling): volume x comment")
    volumes = sorted({float(info.volume_min), 0.1, 0.15, 0.5})  # type: ignore[attr-defined]
    comments = {
        "short": "diag",
        "project-style (31 chars, | and /)": PROJECT_COMMENT,
        "same, no | or /": PROJECT_COMMENT.replace("|", "_").replace("/", "_"),
        "empty": "",
    }
    for volume in volumes:
        for label, comment in comments.items():
            request = {
                "action": mt5.TRADE_ACTION_PENDING,  # type: ignore[attr-defined]
                "symbol": args.symbol,
                "type": mt5.ORDER_TYPE_BUY_STOP,  # type: ignore[attr-defined]
                "volume": volume,
                "price": price,
                "sl": stop,
                "tp": target,
                "deviation": 200,
                "magic": args.magic,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,  # type: ignore[attr-defined]
                "type_filling": mt5.ORDER_FILLING_RETURN,  # type: ignore[attr-defined]
            }
            result = mt5.order_check(request)  # type: ignore[attr-defined]
            if result is None:
                verdict = f"None; last_error {mt5.last_error()}"  # type: ignore[attr-defined]
            else:
                verdict = f"{'ACCEPTED' if result.retcode == 0 else 'REFUSED '} retcode {result.retcode} {result.comment!r}"
            print(f"volume {volume:<5} comment {label:35s} {verdict}")


def _comment_scan(mt5: object, info: object, args: argparse.Namespace, price: float,
                  stop: float, target: float) -> None:  # fmt: skip
    """Find the longest accepted comment, 1 to 40 characters."""
    print("-- comment length scan (RETURN filling)")
    longest = 0
    for length in range(1, 41):
        request = {
            "action": mt5.TRADE_ACTION_PENDING,  # type: ignore[attr-defined]
            "symbol": args.symbol,
            "type": mt5.ORDER_TYPE_BUY_STOP,  # type: ignore[attr-defined]
            "volume": float(info.volume_min),  # type: ignore[attr-defined]
            "price": price,
            "sl": stop,
            "tp": target,
            "deviation": 200,
            "magic": args.magic,
            "comment": "a" * length,
            "type_time": mt5.ORDER_TIME_GTC,  # type: ignore[attr-defined]
            "type_filling": mt5.ORDER_FILLING_RETURN,  # type: ignore[attr-defined]
        }
        result = mt5.order_check(request)  # type: ignore[attr-defined]
        accepted = result is not None and result.retcode == 0
        if accepted:
            longest = length
        elif result is None:
            print(f"length {length:2d}: refused ({mt5.last_error()})")  # type: ignore[attr-defined]
            break
        else:
            print(f"length {length:2d}: refused (retcode {result.retcode} {result.comment!r})")
            break
    print(f"the longest accepted comment is {longest} characters")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--offset", type=int, default=500, help="points above the ask")
    parser.add_argument("--stop", type=int, default=10000, help="stop distance in points")
    parser.add_argument("--target", type=int, default=30000, help="target distance in points")
    parser.add_argument("--magic", type=int, default=20260930)
    parser.add_argument(
        "--matrix",
        action="store_true",
        help="also try the volumes and comment styles the project sends, one variable at a time",
    )
    parser.add_argument(
        "--comment-scan",
        action="store_true",
        help="find the longest comment this terminal accepts (order_check only)",
    )
    args = parser.parse_args()

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("the MetaTrader5 package is not installed")
        return 1

    if not mt5.initialize():
        print(f"cannot attach to the terminal: {mt5.last_error()}")
        return 1
    try:
        terminal = mt5.terminal_info()
        account = mt5.account_info()
        print("-- switches")
        print(f"terminal.trade_allowed (Algo Trading button): {terminal.trade_allowed}")
        print(f"terminal.tradeapi_disabled: {getattr(terminal, 'tradeapi_disabled', 'n/a')}")
        print(f"account.trade_allowed: {account.trade_allowed}")
        print(f"account.trade_expert (automated trading allowed): {getattr(account, 'trade_expert', 'n/a')}")
        print(f"account.trade_mode (0 demo): {account.trade_mode}   server: {account.server}")

        mt5.symbol_select(args.symbol, True)
        info = mt5.symbol_info(args.symbol)
        tick = mt5.symbol_info_tick(args.symbol)
        if info is None or tick is None:
            print(f"no data for {args.symbol}: {mt5.last_error()}")
            return 1
        print("-- symbol")
        print(f"trade_mode {info.trade_mode} (4 = full), execution mode {info.trade_exemode}, "
              f"filling flags {info.filling_mode} (1 FOK, 2 IOC), stops level {info.trade_stops_level}")
        print(f"bid {tick.bid}  ask {tick.ask}  point {info.point}  digits {info.digits}")

        digits = info.digits
        price = round(tick.ask + args.offset * info.point, digits)
        stop = round(price - args.stop * info.point, digits)
        target = round(price + args.target * info.point, digits)
        print(f"-- request: BUY STOP {info.volume_min} lot at {price}, stop {stop}, target {target}")

        any_ok = False
        for name in ("RETURN", "IOC", "FOK"):
            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": args.symbol,
                "type": mt5.ORDER_TYPE_BUY_STOP,
                "volume": float(info.volume_min),
                "price": price,
                "sl": stop,
                "tp": target,
                "deviation": 200,
                "magic": args.magic,
                "comment": "diag",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": getattr(mt5, f"ORDER_FILLING_{name}"),
            }
            result = mt5.order_check(request)
            if result is None:
                print(f"{name:7s} order_check returned None; last_error = {mt5.last_error()}")
                continue
            ok = result.retcode == 0
            any_ok = any_ok or ok
            print(f"{name:7s} {'ACCEPTED' if ok else 'REFUSED '}  retcode {result.retcode}  "
                  f"comment {result.comment!r}")
        if args.matrix:
            _matrix(mt5, info, args, price, stop, target)
        if args.comment_scan:
            _comment_scan(mt5, info, args, price, stop, target)
        print()
        print("at least one filling mode is accepted" if any_ok else "no filling mode was accepted")
        return 0 if any_ok else 1
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    sys.exit(main())
