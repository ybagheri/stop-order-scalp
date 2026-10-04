"""Read-only preflight: does the real MetaTrader5 package and terminal match what the code assumes?

Run this on the machine that has the terminal, signed in to the DEMO account, **before**
``run --demo --place-orders``::

    python scripts/preflight_mt5.py
    python scripts/preflight_mt5.py --symbol BITCOIN

It sends nothing and changes nothing. It prints a line per check, ``OK`` or ``PROBLEM``, and
exits non-zero if anything is a problem. Nothing in the test suite can replace this: the tests
use a double built from the published reference, and this asks the actual package.
"""

from __future__ import annotations

import argparse
import sys

#: Functions the project calls (see tests/unit/test_mt5_surface.py for the full official list).
USED = (
    "initialize", "shutdown", "last_error", "account_info", "terminal_info", "symbol_info",
    "symbol_info_tick", "symbol_select", "copy_rates_from_pos", "orders_get", "positions_get",
    "order_send",
)  # fmt: skip

#: Names the project once called and the package does not have.
ABSENT = ("order_get", "time_current")

#: What the broker module sends on the wire, as published.
EXPECTED_CONSTANTS = {
    "TRADE_ACTION_DEAL": 1,
    "TRADE_ACTION_PENDING": 5,
    "TRADE_ACTION_SLTP": 6,
    "TRADE_ACTION_MODIFY": 7,
    "TRADE_ACTION_REMOVE": 8,
    "ORDER_TYPE_BUY_STOP": 4,
    "ORDER_TYPE_SELL_STOP": 5,
    "ORDER_FILLING_FOK": 0,
    "ORDER_FILLING_IOC": 1,
    "ORDER_FILLING_RETURN": 2,
    "ORDER_TIME_GTC": 0,
    "TIMEFRAME_M1": 1,
    "TIMEFRAME_M15": 15,
    "ACCOUNT_TRADE_MODE_DEMO": 0,
}

problems = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global problems
    if not ok:
        problems += 1
    print(f"{'OK     ' if ok else 'PROBLEM'}  {label}" + (f"  -- {detail}" if detail else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", default=None, help="also inspect this symbol")
    args = parser.parse_args()

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("PROBLEM  the MetaTrader5 package is not installed (pip install MetaTrader5)")
        return 1

    print("-- the package")
    for name in USED:
        check(hasattr(mt5, name), f"function {name} exists")
    for name in ABSENT:
        check(not hasattr(mt5, name), f"{name} is NOT in the package (the code must not call it)")
    for name, expected in EXPECTED_CONSTANTS.items():
        got = getattr(mt5, name, None)
        check(got == expected, f"{name} == {expected}", f"package says {got}")

    print("-- the terminal")
    if not mt5.initialize():
        check(False, "attach to the terminal", f"{mt5.last_error()}; is it open and signed in?")
        return 1
    try:
        terminal = mt5.terminal_info()
        account = mt5.account_info()
        check(terminal is not None and account is not None, "terminal and account readable")
        if terminal is None or account is None:
            return 1
        check(
            bool(terminal.trade_allowed),
            "AutoTrading / Algo Trading is ON",
            "press the 'Algo Trading' button in the terminal toolbar (it must be green)",
        )
        check(
            not getattr(terminal, "tradeapi_disabled", False),
            "the terminal allows its Python API to trade",
        )
        check(bool(account.trade_allowed), "trading is allowed on this account")
        check(
            bool(getattr(account, "trade_expert", True)),
            "automated trading is allowed on this account",
        )
        check(
            account.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO,
            "the account is a DEMO account",
            f"trade_mode is {account.trade_mode} (0 demo, 1 contest, 2 real); server {account.server}",
        )
        print(f"        account {account.login} on {account.server}, balance {account.balance}")

        if args.symbol:
            print(f"-- the symbol {args.symbol}")
            check(bool(mt5.symbol_select(args.symbol, True)), "symbol can be selected")
            info = mt5.symbol_info(args.symbol)
            check(info is not None, "symbol_info is available")
            if info is not None:
                print(
                    f"        digits {info.digits}, point {info.point}, "
                    f"tick size {info.trade_tick_size}, tick value {info.trade_tick_value}, "
                    f"contract {info.trade_contract_size}"
                )
                print(
                    f"        volume {info.volume_min} .. {info.volume_max} step {info.volume_step}, "
                    f"stops level {info.trade_stops_level}, freeze level {info.trade_freeze_level}"
                )
                modes = []
                if info.filling_mode & 1:
                    modes.append("FOK")
                if info.filling_mode & 2:
                    modes.append("IOC")
                print(
                    f"        symbol filling flags: {modes or 'none'} (RETURN is separate; the "
                    f"project defaults to RETURN for pending orders); execution mode {info.trade_exemode}"
                )
                check(info.trade_mode == getattr(mt5, "SYMBOL_TRADE_MODE_FULL", 4),
                      "the symbol is fully tradable right now",
                      f"trade_mode {info.trade_mode}; a closed market or a close-only symbol refuses orders")  # fmt: skip
            tick = mt5.symbol_info_tick(args.symbol)
            check(tick is not None, "a tick is available (the server clock is read from it)")
            if tick is not None:
                print(f"        newest tick time (server clock): {tick.time}, bid {tick.bid}, ask {tick.ask}")
    finally:
        mt5.shutdown()

    print()
    print("everything matches" if not problems else f"{problems} problem(s): fix them before trading")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
