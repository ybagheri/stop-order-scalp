"""The code may only call functions the real MetaTrader5 package has.

For ten phases ``MT5Api`` declared ``order_get`` and ``time_current``, the broker called them,
and the test doubles implemented them -- so everything passed against an API that was never
there. On a real terminal ``place_order`` would have sent the order and then raised
``AttributeError`` looking for the ticket, and ``ServerClock`` would have fallen back to the
local clock without anyone noticing.

A double can only prove the code agrees with the double. This test compares the code with the
*published function list of the package* instead.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from stop_order_scalp.market_data.mt5_module import MT5Api, server_seconds

#: The complete function list of the ``MetaTrader5`` Python package, from MetaQuotes'
#: "Integration with Python" reference. ``order_get`` and ``time_current`` are not in it.
OFFICIAL_FUNCTIONS = frozenset(
    {
        "initialize", "login", "shutdown", "version", "last_error",
        "account_info", "terminal_info",
        "symbols_total", "symbols_get", "symbol_info", "symbol_info_tick", "symbol_select",
        "market_book_add", "market_book_get", "market_book_release",
        "copy_rates_from", "copy_rates_from_pos", "copy_rates_range",
        "copy_ticks_from", "copy_ticks_range",
        "orders_total", "orders_get", "order_calc_margin", "order_calc_profit",
        "order_check", "order_send",
        "positions_total", "positions_get",
        "history_orders_total", "history_orders_get",
        "history_deals_total", "history_deals_get",
    }
)  # fmt: skip

SOURCE = Path(__file__).resolve().parents[2] / "src" / "stop_order_scalp"

#: ``api.x(``, ``self._api().x(`` and ``module.api().x(`` -- the three ways the code reaches
#: the package.
_CALL = re.compile(r"(?:\bapi|_api\(\)|\.api\(\))\.([a-z_]+)\(")


def _called_names() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in SOURCE.rglob("*.py"):
        for name in _CALL.findall(path.read_text(encoding="utf-8")):
            found.setdefault(name, set()).add(path.name)
    return found


class TestTheCodeOnlyCallsRealFunctions:
    def test_every_call_through_the_api_is_a_function_the_package_has(self) -> None:
        unknown = {n: sorted(f) for n, f in _called_names().items() if n not in OFFICIAL_FUNCTIONS}

        assert not unknown, f"calls to functions MetaTrader5 does not have: {unknown}"

    def test_the_protocol_declares_only_real_functions(self) -> None:
        declared = {
            name
            for name, member in vars(MT5Api).items()
            if callable(member) and not name.startswith("_")
        }

        assert declared <= OFFICIAL_FUNCTIONS, sorted(declared - OFFICIAL_FUNCTIONS)

    def test_the_scan_would_notice_a_bad_name(self) -> None:
        """The check is only worth having if it can fail."""
        assert "order_get" not in OFFICIAL_FUNCTIONS
        assert "time_current" not in OFFICIAL_FUNCTIONS
        assert _CALL.findall("x = self._api().order_get(ticket=1)") == ["order_get"]


class TestServerTimeWithoutTimeCurrent:
    def test_it_is_read_from_the_newest_tick(self) -> None:
        class Package:  # exposes only real functions
            @staticmethod
            def symbol_info_tick(name: str) -> SimpleNamespace:
                assert name == "BITCOIN"
                return SimpleNamespace(time=1_781_000_000)

        assert server_seconds(Package(), "BITCOIN") == 1_781_000_000

    def test_no_symbol_means_unknown_not_a_guess(self) -> None:
        assert server_seconds(SimpleNamespace(), None) == 0

    def test_a_silent_symbol_means_unknown(self) -> None:
        package = SimpleNamespace(symbol_info_tick=lambda _name: None)

        assert server_seconds(package, "BITCOIN") == 0
