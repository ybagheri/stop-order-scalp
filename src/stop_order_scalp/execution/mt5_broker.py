"""``Broker`` over the native MetaTrader 5 API, and the error classification that matters.

One of only three modules permitted to name ``MetaTrader5``, and even here the ``import``
belongs to :mod:`stop_order_scalp.market_data.mt5_module` -- this module reaches the
terminal through :class:`~stop_order_scalp.market_data.mt5_module.MT5Module` rather than
importing the package again, so there is exactly one place that knows how to load it.

The most important function in this module is :func:`classify`, because everything else is
mechanical. MetaTrader 5 reports every failure as a numeric ``retcode``, and the single
question that matters is:

    **did the order reach the venue or not?**

Get that wrong in the optimistic direction -- assume a retcode means "not sent" and resend
-- and one order becomes two. Wrong in the pessimistic direction and a position is missed
until the next signal. So the ambiguous codes become
:class:`~stop_order_scalp.domain.exceptions.ExecutionUnknownError`, which the caller handles
by re-reading broker state. It is never retried.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from stop_order_scalp.domain.enums import OrderKind, Side
from stop_order_scalp.domain.exceptions import (
    BrokerError,
    BrokerNotConnectedError,
    BrokerRejectedError,
    ExecutionUnknownError,
    RetryableError,
    SymbolNotFoundError,
)
from stop_order_scalp.domain.models import (
    AccountSnapshot,
    EnvironmentSettings,
    OrderIntent,
    OrderRecord,
    PositionRecord,
    Tick,
)
from stop_order_scalp.domain.value_objects import Money, Price, SymbolSpecification, Volume
from stop_order_scalp.market_data.mt5_module import (
    MT5Api,
    MT5Module,
    account_info_to_snapshot,
    epoch_to_datetime,
    field,
    server_seconds,
    symbol_info_to_specification,
    tick_info_to_tick,
)

__all__ = ["MetaTrader5Broker", "classify"]

# =============================================================================
# Return codes, from the terminal's own TRADE_RETCODE_* enumeration
# =============================================================================
#
# Only the codes this project can actually produce, or must recognise, are listed. The full
# enumeration belongs to the terminal; copying all of it would be a maintenance liability
# with no benefit. Anything unrecognised is treated as *ambiguous*, which is the safe
# direction -- see :func:`classify`.

#: Accepted: fully executed.
RETCODE_DONE: Final[int] = 10009
#: Accepted: resting as a pending order.
RETCODE_PLACED: Final[int] = 10008
#: Accepted: partially executed.
RETCODE_PARTIAL: Final[int] = 10010
#: Refused on its merits.
RETCODE_REJECTED: Final[int] = 10006
#: Generic "invalid request" -- the one a malformed trade request produces.
RETCODE_INVALID: Final[int] = 10013
RETCODE_INVALID_VOLUME: Final[int] = 10014
RETCODE_INVALID_PRICE: Final[int] = 10015
#: Stops too close to the market, or on the wrong side of the entry. The single most
#: common Phase 5 rejection, and the reason ``stops_level`` is checked before sending.
RETCODE_INVALID_STOPS: Final[int] = 10016
#: Trading disabled for the account.
RETCODE_TRADE_DISABLED: Final[int] = 10017
#: Market closed. "Not now", not "no".
RETCODE_MARKET_CLOSED: Final[int] = 10018
#: Not enough free margin. Terminal for this attempt; the caller must re-size.
RETCODE_NO_MONEY: Final[int] = 10019
#: Price moved. Retryable.
RETCODE_PRICE_CHANGED: Final[int] = 10020
#: Price is off the quotes. Retryable.
RETCODE_PRICE_OFF: Final[int] = 10021
#: Terminal busy. Retryable.
RETCODE_TOO_BUSY: Final[int] = 10024
#: The expiration is not acceptable to the venue.
RETCODE_INVALID_EXPIRATION: Final[int] = 10022
#: "No changes": a modify that asked for what is already true. Success for an idempotent
#: ``modify_position``; a trailing stop that has not moved sends exactly this.
RETCODE_NO_CHANGES: Final[int] = 10025
#: AutoTrading is switched off -- by the server, or by the terminal's *Algo Trading* button.
#: The most common reason a first order is refused, and one the operator fixes in a click.
RETCODE_AUTOTRADING_SERVER: Final[int] = 10026
RETCODE_AUTOTRADING_CLIENT: Final[int] = 10027
#: The symbol does not accept this ``type_filling``. Refused before reaching the book, so
#: nothing exists and nothing is ambiguous; ``order.filling_policy`` is the setting to change.
RETCODE_INVALID_FILL: Final[int] = 10030
#: Ambiguous: a failure with no detail.
RETCODE_ERROR: Final[int] = 10011
#: Ambiguous: the request timed out, so it may have been processed.
RETCODE_TIMEOUT: Final[int] = 10012
#: Ambiguous: connection dropped mid-request, so it may have been processed.
RETCODE_CONNECTION: Final[int] = 10031
#: Ambiguous: trade context is busy, which can mean the request is still in flight.
RETCODE_CONTEXT_BUSY: Final[int] = 10028
#: Ambiguous: trade context is frozen while a deal is being processed.
RETCODE_CONTEXT_FROZEN: Final[int] = 10029
#: Ambiguous: internal error.
RETCODE_INTERNAL_ERROR: Final[int] = -1

#: Accepted, in some form.
_ACCEPTED: Final[frozenset[int]] = frozenset(
    {RETCODE_DONE, RETCODE_PLACED, RETCODE_PARTIAL, RETCODE_NO_CHANGES}
)

#: What to do about the refusals an operator meets first, appended to the terminal's own words.
_HINTS: Final[dict[int, str]] = {
    10026: "enable AutoTrading on the server side for this account",
    10027: "press the terminal's 'Algo Trading' button (it must be green) and, in "
    "Tools > Options > Expert Advisors, allow algorithmic trading",
    10030: "this symbol does not accept the configured filling mode; set "
    "order.filling_policy to IOC or RETURN (or FOK) in the config and try again",
    10017: "trading is disabled for this account or symbol",
    10018: "the market is closed for this symbol",
}

#: Refused on its merits. A resend will be refused identically.
_TERMINAL: Final[frozenset[int]] = frozenset(
    {
        RETCODE_REJECTED,
        RETCODE_INVALID,
        RETCODE_INVALID_VOLUME,
        RETCODE_INVALID_PRICE,
        RETCODE_INVALID_STOPS,
        RETCODE_TRADE_DISABLED,
        RETCODE_NO_MONEY,
        RETCODE_INVALID_EXPIRATION,
        RETCODE_AUTOTRADING_SERVER,
        RETCODE_AUTOTRADING_CLIENT,
        RETCODE_INVALID_FILL,
    }
)

#: "Not now"; trying again later may work.
_RETRYABLE: Final[frozenset[int]] = frozenset(
    {RETCODE_MARKET_CLOSED, RETCODE_PRICE_CHANGED, RETCODE_PRICE_OFF, RETCODE_TOO_BUSY}
)

#: The request may or may not have been processed. **Never resend these.**
#:
#: Each of these is a case where the terminal could not tell us, which means the order may
#: exist on the venue. The only correct response is to re-read broker state and find out.
_UNKNOWN: Final[frozenset[int]] = frozenset(
    {
        RETCODE_ERROR,
        RETCODE_TIMEOUT,
        RETCODE_CONNECTION,
        RETCODE_CONTEXT_BUSY,
        RETCODE_CONTEXT_FROZEN,
        RETCODE_INTERNAL_ERROR,
    }
)


def classify(retcode: int, message: str, *, context: str) -> None:
    """Raise the right exception for a terminal ``retcode``.

    :raises ExecutionUnknownError: for an ambiguous outcome. Never retryable, and the
        single most important branch in the project: the request may have reached the
        venue, and resending is how one order becomes two.
    :raises BrokerRejectedError: for a refusal on its merits. A resend will be refused
        identically, so retrying wastes the chance that something changed.
    :raises RetryableError: for "not now".
    """
    if retcode in _ACCEPTED:
        return
    detail = f"{context}: retcode {retcode} ({message})"
    if retcode in _HINTS:
        detail = f"{detail}; {_HINTS[retcode]}"
    if retcode in _UNKNOWN:
        raise ExecutionUnknownError(
            f"{detail} -- the terminal cannot say whether the request reached the venue. "
            "Do NOT resend; re-read broker state for this magic number."
        )
    if retcode in _TERMINAL:
        raise BrokerRejectedError(f"{detail} -- refused on its merits")
    if retcode in _RETRYABLE:
        raise RetryableError(f"{detail} -- not now, try again later")
    # An unrecognised code is treated as ambiguous rather than terminal. Being wrong in the
    # pessimistic direction costs one re-read; being wrong in the optimistic one costs a
    # duplicate.
    raise ExecutionUnknownError(
        f"{detail} -- unrecognised retcode, treated as an unknown outcome. Do NOT resend."
    )


#: ``DEAL_ENTRY_*``: whether a deal opened a position, closed one, or both.
_DEAL_ENTRY: Final[dict[int, str]] = {0: "open", 1: "close", 2: "reverse", 3: "close"}

#: ``DEAL_REASON_*``: what caused a deal. This is how a closed trade says how it ended.
_DEAL_REASON: Final[dict[int, str]] = {
    0: "manual",
    1: "manual",
    2: "manual",
    3: "program",
    4: "stop_loss",
    5: "take_profit",
    6: "stop_out",
}

#: ``last_error()`` codes that mean the terminal refused the call **before anything left it**:
#: -2 invalid arguments, -4 not found, -5 invalid version, -6 authorisation, -7 unsupported,
#: -8 auto-trading disabled. Nothing was sent, so nothing is ambiguous.
_NOT_SENT_ERRORS: Final[frozenset[int]] = frozenset({-2, -4, -5, -6, -7, -8})

_LAST_ERROR_HINTS: Final[dict[int, str]] = {
    -2: "the request has an argument of the wrong type or range",
    -8: "AutoTrading is disabled: press the terminal's 'Algo Trading' button (it must be green)",
    -6: "the terminal is not authorised on the account",
}

#: What ``order_check`` answers when the request would be accepted.
_CHECK_OK: Final[frozenset[int]] = frozenset({0, RETCODE_DONE})


def _last_error(api: Any) -> tuple[int, str]:
    """The terminal's own explanation for a call that returned nothing."""
    try:
        code, text = api.last_error()
        return int(code), str(text)
    except (AttributeError, TypeError, ValueError):
        return 0, "last_error() is unavailable"


def _check_request(api: Any, request: dict[str, Any], context: str) -> None:
    """Ask the server whether it would accept ``request``. Sends nothing.

    Any refusal here is a :class:`BrokerRejectedError`, whatever its retcode would have meant
    from ``order_send``: nothing was transmitted, so there is no ambiguity to preserve, and a
    plain "no, because ..." is what lets the lifecycle settle the attempt and carry on.
    """
    checked = api.order_check(request)
    if checked is None:
        code, text = _last_error(api)
        detail = f"{context}: the terminal could not evaluate the request: error {code} ({text})"
        if code in _LAST_ERROR_HINTS:
            detail = f"{detail}; {_LAST_ERROR_HINTS[code]}"
        raise BrokerRejectedError(f"{detail} -- nothing was sent")
    code = int(field(checked, "retcode", -1))
    if code in _CHECK_OK:
        return
    message = str(field(checked, "comment", "") or "")
    try:
        classify(code, message, context=f"checking {context}")
    except (BrokerRejectedError, ExecutionUnknownError, RetryableError) as exc:
        raise BrokerRejectedError(f"{exc} (checked before sending; nothing was sent)") from exc


def _dispatch(api: Any, request: dict[str, Any], context: str) -> Any:
    """``order_send`` once. A ``None`` answer is explained by ``last_error()``, not guessed at."""
    result = api.order_send(request)
    if result is not None:
        return result
    code, text = _last_error(api)
    detail = f"{context}: order_send returned nothing; terminal error {code} ({text})"
    if code in _LAST_ERROR_HINTS:
        detail = f"{detail}; {_LAST_ERROR_HINTS[code]}"
    if code in _NOT_SENT_ERRORS:
        raise BrokerRejectedError(f"{detail} -- refused before it left the terminal; nothing was sent")
    raise ExecutionUnknownError(
        f"{detail} -- the terminal cannot say whether the request reached the venue. "
        "Do NOT resend; re-read broker state for this magic number."
    )


class MetaTrader5Broker:
    """``Broker`` implemented over the native MetaTrader 5 Python API.

    Places and cancels orders, modifies positions, and reads account, book and server time.
    It **never retries a send internally** -- that is the caller's decision, made through
    the lifecycle state machine.

    Credentials: the password is read from the environment at connect time and is never
    stored on this object. ``EnvironmentSettings`` holds only a presence flag, by design.
    """

    __slots__ = ("_connected", "_deviation", "_filling", "_module", "_settings")

    def __init__(
        self,
        module: MT5Module | None = None,
        settings: EnvironmentSettings | None = None,
        *,
        filling: str = "RETURN",
        deviation_points: int = 200,
    ) -> None:
        if filling not in _FILLING_VALUES:
            raise BrokerError(f"filling must be one of {sorted(_FILLING_VALUES)}, got {filling!r}")
        self._module = module if module is not None else MT5Module()
        self._settings = settings
        #: ``RETURN`` is what MetaQuotes' own pending-order example uses and the one most
        #: brokers accept for a stop order. Which one a symbol allows is the broker's choice;
        #: a refusal (retcode 10030) says so, and ``order.filling_policy`` is the setting.
        self._filling = filling
        #: Slippage tolerated when closing at market, in points of the symbol.
        self._deviation = deviation_points
        self._connected = False

    # --- connection ------------------------------------------------------

    def connect(self) -> None:
        """Start or attach to the terminal. Idempotent.

        ``Broker.connect`` takes no arguments, so ``EnvironmentSettings`` is supplied at
        construction. Password comes from ``SOS_MT5_PASSWORD`` and is never retained.
        """
        settings = self._settings
        if settings is None:
            raise BrokerError("MetaTrader5Broker needs EnvironmentSettings before connect()")
        if self._connected:
            return
        password = os.environ.get("SOS_MT5_PASSWORD") or None
        if password:
            ok = self._module.api().initialize(
                path=settings.mt5_path,
                login=settings.mt5_login,
                password=password,
                server=settings.mt5_server,
                timeout=settings.mt5_timeout_ms,
                portable=False,
            )
        else:
            # Attach to the session the terminal already has. A login with no password is
            # answered with error -2 and is how this project locked three demo accounts
            # (HANDOFF.md). ``MT5Feed.connect`` has always done this.
            ok = self._module.api().initialize(
                path=settings.mt5_path,
                timeout=settings.mt5_timeout_ms,
                portable=False,
            )
        if not ok:
            code, message = self._module.describe_last_error()
            raise BrokerNotConnectedError(
                f"MetaTrader 5 refused to initialize (code {code}): {message}"
            )
        self._connected = True

    def shutdown(self) -> None:
        """Tear the session down. Idempotent, and safe when never connected."""
        if self._connected:
            self._module.api().shutdown()
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _api(self) -> MT5Api:
        if not self._connected:
            raise BrokerNotConnectedError("not connected to MetaTrader 5; call connect()")
        return self._module.api()

    # --- account and symbol reads ---------------------------------------

    def account(self) -> AccountSnapshot:
        info = self._api().account_info()
        if info is None:
            raise BrokerNotConnectedError("the terminal returned no account information")
        return account_info_to_snapshot(info)

    def account_is_confirmed_demo(self) -> bool:
        """``True`` only if the terminal *says* this is a demo account.

        ``account()`` reports ``is_demo`` and defaults it to ``True`` when the terminal does
        not say, which is the safe default for a reader and the wrong one for a gate: a gate
        that opens when it cannot tell has opened for a live account. This reads the raw
        ``trade_mode`` and requires ``ACCOUNT_TRADE_MODE_DEMO`` (0) -- a contest account (1) or
        a real one (2) is refused, and so is an answer that is missing.
        """
        info = self._api().account_info()
        if info is None:
            return False
        mode = field(info, "trade_mode")
        return mode is not None and int(mode) == 0

    def server_time(self) -> datetime:
        """Broker server time. The only clock that may decide whether a candle has closed."""
        seconds = server_seconds(
            self._api(), self._settings.symbol if self._settings is not None else None
        )
        if not seconds:
            raise BrokerNotConnectedError("the terminal reported no server time")
        return epoch_to_datetime(seconds)

    def symbol_available(self, symbol: str) -> bool:
        return bool(self._api().symbol_info(symbol))

    def specification(self, symbol: str) -> SymbolSpecification:
        info = self._api().symbol_info(symbol)
        if info is None:
            raise SymbolNotFoundError(
                f"the terminal does not know a symbol called {symbol!r}"
            )
        return symbol_info_to_specification(info)

    def leverage_for(self, symbol: str) -> Decimal:
        info = self._api().symbol_info(symbol)
        if info is None:
            raise SymbolNotFoundError(f"the terminal does not know a symbol called {symbol!r}")
        return Decimal(str(field(info, "leverage", 100) or 100))

    # --- prices ----------------------------------------------------------

    def tick(self, symbol: str) -> Tick | None:
        info = self._api().symbol_info_tick(symbol)
        if info is None:
            return None
        return tick_info_to_tick(info, digits=self.specification(symbol).digits)

    def stream_ticks(self, symbol: str) -> Iterator[Tick]:
        """Yield ticks as they arrive. Used by ``PAPER``, which writes nothing.

        Polls rather than subscribing: the terminal's own streaming API is not available to
        every build, and a PAPER mode that follows prices a second late is still following
        them. The trade-off is documented rather than hidden.
        """
        api = self._api()
        last = 0
        while self._connected:
            info = api.symbol_info_tick(symbol)
            if info is not None:
                moment = int(field(info, "time_ms", 0) or 0) // 1000
                if moment and moment != last:
                    last = moment
                    yield tick_info_to_tick(
                        info, digits=self.specification(symbol).digits
                    )
            else:
                code, message = self._module.describe_last_error()
                raise BrokerNotConnectedError(f"tick stream for {symbol} failed: {code} {message}")

    # --- order book ------------------------------------------------------

    def orders(
        self, *, magic_number: int | None = None, symbol: str | None = None
    ) -> list[OrderRecord]:
        """Working orders for this magic number.

        Read immediately before every send. A cached copy is correct until the process is
        interrupted, and an interruption between "decided to place" and "read the book" is
        exactly the case that produces a duplicate position.
        """
        api = self._api()
        rows = api.orders_get(symbol=symbol) if symbol else api.orders_get()
        digits = self.specification(symbol).digits if symbol else 2
        found = [
            _order_from(row, digits)
            for row in rows or ()
            if magic_number is None or int(field(row, "magic", 0) or 0) == magic_number
        ]
        return sorted(found, key=lambda record: record.ticket)

    def positions(
        self, *, magic_number: int | None = None, symbol: str | None = None
    ) -> list[PositionRecord]:
        api = self._api()
        rows = api.positions_get(symbol=symbol) if symbol else api.positions_get()
        digits = self.specification(symbol).digits if symbol else 2
        found = [
            _position_from(row, digits)
            for row in rows or ()
            if magic_number is None or int(field(row, "magic", 0) or 0) == magic_number
        ]
        return sorted(found, key=lambda record: record.ticket)

    def position_by_ticket(self, ticket: int) -> PositionRecord | None:
        api = self._api()
        rows = api.positions_get(ticket=ticket)
        if not rows:
            return None
        row = rows[0]
        name = str(field(row, "symbol", "") or "")
        digits = self.specification(name).digits if name else 2
        return _position_from(row, digits)

    # --- writes ----------------------------------------------------------

    def place_order(self, intent: OrderIntent) -> OrderRecord:
        """Send one pending order. Exactly one ``order_send``, and never a second.

        A failure is classified and raised. The only correct response to an
        :class:`ExecutionUnknownError` is to re-read broker state.
        """
        api = self._api()
        request: dict[str, Any] = {
            "action": _const(api, "TRADE_ACTION_PENDING", _ACTION_PENDING),
            "symbol": intent.symbol,
            "type": _MT5_TYPE[intent.kind],
            "volume": float(intent.volume.lots),
            "price": float(intent.entry.value),
            "sl": float(intent.stop_loss.value),
            "tp": float(intent.take_profit.value),
            "deviation": intent.deviation_points,
            "magic": intent.magic_number,
            "comment": encode_comment(intent.client_tag, intent.comment),
            "type_time": _const(api, "ORDER_TIME_GTC", _TIME_GTC),
            "type_filling": _const(
                api, f"ORDER_FILLING_{self._filling}", _FILLING_VALUES[self._filling]
            ),
        }
        if intent.expiration is not None:
            request["type_time"] = _const(api, "ORDER_TIME_SPECIFIED", _TIME_SPECIFIED)
            request["expiration"] = int(intent.expiration.timestamp())
        context = f"placing {intent.kind} on {intent.symbol}"
        _check_request(api, request, context)
        result = _dispatch(api, request, context)
        classify(
            int(field(result, "retcode", -1) or 0),
            str(field(result, "comment", "") or ""),
            context=context,
        )
        ticket = int(field(result, "order", 0) or 0)
        rows = api.orders_get(ticket=ticket) if ticket else None
        placed = rows[0] if rows else None
        if placed is None:
            # Accepted, but not yet readable. The send succeeded; observing it is a
            # separate concern that belongs to the lifecycle, not here.
            return _intent_record(intent, ticket)
        return _order_from(placed, intent.entry.digits)

    def cancel_order(self, ticket: int) -> bool:
        """Delete a working order. ``True`` if it is gone afterwards, including if absent.

        Implemented by *asking the book*, not by trusting the retcode. A cancellation whose
        response is ambiguous is not a failure to be retried -- it is a question, and the
        order book is where the answer lives.
        """
        api = self._api()
        try:
            result = _dispatch(
                api,
                {"action": _const(api, "TRADE_ACTION_REMOVE", _ACTION_REMOVE), "order": ticket},
                f"cancelling order {ticket}",
            )
        except ExecutionUnknownError:
            # No answer at all. Whether it reached the venue is exactly what the book says.
            return not api.orders_get(ticket=ticket)
        retcode = int(field(result, "retcode", -1) or 0)
        if retcode not in _ACCEPTED:
            if retcode in _UNKNOWN:
                # The terminal lost track of the outcome. Falling through to the re-read
                # below is correct: what matters is only whether the order is still there.
                pass
            else:
                classify(
                    retcode,
                    str(field(result, "comment", "") or ""),
                    context=f"cancelling order {ticket}",
                )
        return not api.orders_get(ticket=ticket)

    def deals(
        self, *, days: int = 7, magic_number: int | None = None, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        """Executed buy/sell deals of the last ``days`` days, oldest first.

        This is where the result of a trade lives: a position that has closed is gone from
        ``positions()``, and its profit, its commission and *why it closed* -- stop-loss,
        take-profit, or someone pressing close -- are only in the deal history.
        """
        api = self._api()
        now = server_seconds(api, symbol or (self._settings.symbol if self._settings else None))
        if not now:
            raise BrokerNotConnectedError("the terminal reported no server time for the history")
        rows = api.history_deals_get(
            epoch_to_datetime(now - days * 86400), epoch_to_datetime(now + 86400)
        )
        found: list[dict[str, Any]] = []
        for row in rows or ():
            kind = int(field(row, "type", -1) or 0)
            if kind not in (0, 1):  # balance, credit and similar are not trades
                continue
            if magic_number is not None and int(field(row, "magic", 0) or 0) != magic_number:
                continue
            if symbol is not None and str(field(row, "symbol", "")) != symbol:
                continue
            found.append(
                {
                    "ticket": int(field(row, "ticket", 0) or 0),
                    "position": int(field(row, "position_id", 0) or 0),
                    "time": epoch_to_datetime(int(field(row, "time", 0) or 0)).isoformat(),
                    "side": "BUY" if kind == 0 else "SELL",
                    "leg": _DEAL_ENTRY.get(int(field(row, "entry", -1) or 0), "other"),
                    "volume": float(field(row, "volume", 0.0) or 0.0),
                    "price": float(field(row, "price", 0.0) or 0.0),
                    "profit": float(field(row, "profit", 0.0) or 0.0),
                    "commission": float(field(row, "commission", 0.0) or 0.0),
                    "swap": float(field(row, "swap", 0.0) or 0.0),
                    "reason": _DEAL_REASON.get(int(field(row, "reason", -1) or 0), "other"),
                    "comment": str(field(row, "comment", "") or ""),
                }
            )
        found.sort(key=lambda deal: (deal["time"], deal["ticket"]))
        return found

    def close_position(self, ticket: int) -> bool:
        """Close one position at market. ``True`` when the terminal accepted the close.

        A *market* order, so unlike a pending one it needs a filling mode the symbol allows for
        market execution -- which on the first real terminal was FOK only, while pending orders
        took RETURN. The mode is therefore read from the symbol, not from ``order.filling_policy``.

        Never retried: if the answer is ambiguous the position may or may not be closed, and a
        second close request on a position that *did* close would be refused, but one on a
        position that was *reopened* by something else would not. The caller re-reads the book.
        """
        api = self._api()
        rows = api.positions_get(ticket=ticket)
        row = rows[0] if rows else None
        if row is None:
            raise BrokerRejectedError(
                f"closing position {ticket}: no open position with that ticket"
            )
        symbol = str(field(row, "symbol", ""))
        is_long = int(field(row, "type", 0) or 0) == 0
        tick = api.symbol_info_tick(symbol)
        if tick is None:
            raise BrokerNotConnectedError(f"closing position {ticket}: no price for {symbol}")
        # A long is closed by selling at the bid; a short by buying at the ask.
        price = float(field(tick, "bid" if is_long else "ask"))
        request: dict[str, Any] = {
            "action": _const(api, "TRADE_ACTION_DEAL", _ACTION_DEAL),
            "symbol": symbol,
            "volume": float(field(row, "volume")),
            "type": _const(
                api,
                "ORDER_TYPE_SELL" if is_long else "ORDER_TYPE_BUY",
                1 if is_long else 0,
            ),
            "position": ticket,
            "price": price,
            "deviation": self._deviation,
            "magic": int(field(row, "magic", 0) or 0),
            "comment": "SOS close",
            "type_time": _const(api, "ORDER_TIME_GTC", _TIME_GTC),
            "type_filling": self._market_filling(api, symbol),
        }
        context = f"closing position {ticket}"
        _check_request(api, request, context)
        result = _dispatch(api, request, context)
        classify(
            int(field(result, "retcode", -1) or 0),
            str(field(result, "comment", "") or ""),
            context=context,
        )
        return True

    @staticmethod
    def _market_filling(api: Any, symbol: str) -> int:
        """A filling mode the symbol allows for a market order: FOK, else IOC, else RETURN."""
        info = api.symbol_info(symbol)
        flags = int(field(info, "filling_mode", 0) or 0) if info is not None else 0
        if flags & 1:
            return _const(api, "ORDER_FILLING_FOK", _FILLING_VALUES["FOK"])
        if flags & 2:
            return _const(api, "ORDER_FILLING_IOC", _FILLING_VALUES["IOC"])
        return _const(api, "ORDER_FILLING_RETURN", _FILLING_VALUES["RETURN"])

    def modify_position(
        self, ticket: int, *, stop_loss: Any = None, take_profit: Any = None
    ) -> bool:
        """Change a position's protective levels. Idempotent: a no-op change is success.

        ``TRADE_ACTION_SLTP`` rather than a deal request: this never fills anything, it only
        moves the stops on a position that already exists.

        **A level that is not being changed is re-sent at its current value.** In this action a
        zero means *remove*, not *leave alone* -- so sending only the new stop used to send
        ``tp=0`` and delete the position's take-profit on the first trailing step. (The
        simulated venue treats an omitted level as unchanged, which is why nothing caught it.)
        """
        api = self._api()
        rows = api.positions_get(ticket=ticket)
        current = rows[0] if rows else None
        if current is None:
            raise BrokerRejectedError(
                f"modifying position {ticket}: no open position with that ticket; nothing to modify"
            )

        def level(given: Any, name: str) -> float:
            if isinstance(given, Price):
                return float(given.value)
            return float(field(current, name, _UNSET_FLOAT) or _UNSET_FLOAT)

        request: dict[str, Any] = {
            "action": _const(api, "TRADE_ACTION_SLTP", _ACTION_SLTP),
            "position": ticket,
            "sl": level(stop_loss, "sl"),
            "tp": level(take_profit, "tp"),
        }
        context = f"modifying position {ticket}"
        result = _dispatch(api, request, context)
        classify(
            int(field(result, "retcode", -1) or 0),
            str(field(result, "comment", "") or ""),
            context=context,
        )
        return True

    def __repr__(self) -> str:
        return f"MetaTrader5Broker(connected={self._connected})"


# =============================================================================
# Wire constants and row converters
# =============================================================================

#: ``TRADE_REQUEST_ACTIONS``.
#:
#: These are the **documented** values, used only as a fallback. They were 0/1/2/3 here for ten
#: phases -- which is ``DEAL`` for nothing, ``DEAL`` for a pending order and two undefined
#: actions -- and the test double asserted ``== 1`` for "pending", so it agreed. The real
#: package is asked first (see :func:`_const`), so a wrong number here cannot reach the wire
#: unless the package itself is missing.
_ACTION_DEAL: Final[int] = 1
_ACTION_PENDING: Final[int] = 5
_ACTION_SLTP: Final[int] = 6
_ACTION_MODIFY: Final[int] = 7
_ACTION_REMOVE: Final[int] = 8

#: ``ORDER_FILLING_*`` and ``ORDER_TIME_*``, again as documented fallbacks.
_FILLING_VALUES: Final[dict[str, int]] = {"FOK": 0, "IOC": 1, "RETURN": 2}
_TIME_GTC: Final[int] = 0
_TIME_SPECIFIED: Final[int] = 2


def _const(api: Any, name: str, fallback: int) -> int:
    """A numeric constant from the terminal package, or the documented value.

    Asking the package removes the dependency on anyone's memory of the numbers, mine included.
    A test double that does not define the name gets the documented fallback.
    """
    value = getattr(api, name, None)
    return int(value) if isinstance(value, int) else fallback

#: ``ORDER_TYPE_*``. Distinct values, and the inverse map is keyed off the same table.
_TYPE_BUY: Final[int] = 0
_TYPE_SELL: Final[int] = 1
_TYPE_BUY_LIMIT: Final[int] = 2
_TYPE_SELL_LIMIT: Final[int] = 3
_TYPE_BUY_STOP: Final[int] = 4
_TYPE_SELL_STOP: Final[int] = 5

#: Our ``OrderKind`` to the terminal's order type.
_MT5_TYPE: Final[dict[OrderKind, int]] = {
    OrderKind.ORDER_KIND_BUY_STOP: _TYPE_BUY_STOP,
    OrderKind.ORDER_KIND_SELL_STOP: _TYPE_SELL_STOP,
    OrderKind.ORDER_KIND_BUY_LIMIT: _TYPE_BUY_LIMIT,
    OrderKind.ORDER_KIND_SELL_LIMIT: _TYPE_SELL_LIMIT,
    OrderKind.ORDER_KIND_MARKET_BUY: _TYPE_BUY,
    OrderKind.ORDER_KIND_MARKET_SELL: _TYPE_SELL,
}

#: The terminal's order type back to ours.
_ORDER_TYPE: Final[dict[int, OrderKind]] = {
    _TYPE_BUY_STOP: OrderKind.ORDER_KIND_BUY_STOP,
    _TYPE_SELL_STOP: OrderKind.ORDER_KIND_SELL_STOP,
    _TYPE_BUY_LIMIT: OrderKind.ORDER_KIND_BUY_LIMIT,
    _TYPE_SELL_LIMIT: OrderKind.ORDER_KIND_SELL_LIMIT,
    _TYPE_BUY: OrderKind.ORDER_KIND_MARKET_BUY,
    _TYPE_SELL: OrderKind.ORDER_KIND_MARKET_SELL,
}

#: The longest comment this project will send.
#:
#: MetaTrader 5's *documentation* says a comment is cut at 31 characters. The Python package does
#: not cut it: on the first real run a 31-character comment made ``order_check`` and
#: ``order_send`` fail with ``Invalid "comment" argument`` (``last_error`` -2), while "diag" and an
#: empty comment were accepted. The true ceiling is somewhere below 31, and it is not yet
#: measured (``scripts/diagnose_order.py --comment-scan`` measures it). Until it is, 24 keeps the
#: 20-character identity tag, the separator and three characters of prose -- and the tag is the
#: only part anything depends on.
_COMMENT_LIMIT: Final[int] = 24


#: A level the terminal reports as ``0`` has no stop or target. NOT "leave unchanged": see
#: :meth:`MetaTrader5Broker.modify_position`.
_UNSET_FLOAT: Final[float] = 0.0


def encode_comment(client_tag: str, comment: str) -> str:
    """Pack the identity tag and the human comment into the terminal's short comment field.

    MetaTrader 5 has no client-order-id field, so the comment is the only channel by which
    identity survives the round trip -- and identity is what makes "did I already place
    this?" answerable after a restart. The tag goes **first** and is never truncated,
    because a tag that loses characters stops matching and duplicate protection silently
    stops working; the prose is what gets cut.

    The separator is a character the digest cannot contain, so decoding is unambiguous.
    """
    head = f"{client_tag}|"
    if len(head) >= _COMMENT_LIMIT:
        # Cannot happen with a 20-character blake2b digest, but a longer tag must fail
        # loudly here rather than produce a comment that decodes to nothing.
        raise BrokerError(
            f"client tag {client_tag!r} leaves no room in the {_COMMENT_LIMIT}-character "
            "terminal comment; identity would not survive the round trip"
        )
    return f"{head}{comment}"[:_COMMENT_LIMIT]


def decode_comment(raw: str) -> tuple[str, str]:
    """Inverse of :func:`encode_comment`. Returns ``(client_tag, comment)``."""
    if "|" not in raw:
        return "", raw
    tag, _, rest = raw.partition("|")
    return tag, rest


def _intent_record(intent: OrderIntent, ticket: int) -> OrderRecord:
    """An :class:`OrderRecord` built from what we sent, before the terminal confirms it."""
    return OrderRecord(
        ticket=ticket,
        client_tag=intent.client_tag,
        symbol=intent.symbol,
        kind=intent.kind,
        volume=intent.volume,
        entry=intent.entry,
        stop_loss=intent.stop_loss,
        take_profit=intent.take_profit,
        magic_number=intent.magic_number,
        comment=intent.comment,
        placed_at=datetime.now(UTC),
        expires_at=intent.expiration,
    )


def _order_from(row: Any, digits: int) -> OrderRecord:
    comment = str(field(row, "comment", "") or "")
    tag, prose = decode_comment(comment)
    volume = Decimal(str(field(row, "volume_current", None) or field(row, "volume_initial", 0) or 0))
    setup = int(field(row, "time_setup", 0) or 0)
    expiration = int(field(row, "time_expiration", 0) or 0)
    return OrderRecord(
        ticket=int(field(row, "ticket", 0) or 0),
        client_tag=tag,
        symbol=str(field(row, "symbol", "") or ""),
        kind=_ORDER_TYPE.get(int(field(row, "type", 0) or 0), OrderKind.ORDER_KIND_BUY_STOP),
        volume=Volume.of(volume),
        entry=Price.parse(str(field(row, "price_open", None) or field(row, "price", 0) or 0), digits),
        stop_loss=_optional_price(field(row, "sl"), digits),
        take_profit=_optional_price(field(row, "tp"), digits),
        magic_number=int(field(row, "magic", 0) or 0),
        comment=prose,
        placed_at=epoch_to_datetime(setup) if setup else datetime.now(UTC),
        expires_at=epoch_to_datetime(expiration) if expiration else None,
    )


def _position_from(row: Any, digits: int) -> PositionRecord:
    comment = str(field(row, "comment", "") or "")
    tag, prose = decode_comment(comment)
    opened = int(field(row, "time", 0) or 0)
    currency = str(field(row, "currency", "USD") or "USD")
    return PositionRecord(
        ticket=int(field(row, "ticket", 0) or 0),
        symbol=str(field(row, "symbol", "") or ""),
        side=Side.SIDE_BUY if int(field(row, "type", 0) or 0) == _TYPE_BUY else Side.SIDE_SELL,
        volume=Volume.of(Decimal(str(field(row, "volume", 0) or 0))),
        entry=Price.parse(str(field(row, "price_open", 0) or 0), digits),
        stop_loss=_optional_price(field(row, "sl"), digits),
        take_profit=_optional_price(field(row, "tp"), digits),
        magic_number=int(field(row, "magic", 0) or 0),
        comment=prose,
        opened_at=epoch_to_datetime(opened) if opened else datetime.now(UTC),
        profit=Money.of(Decimal(str(field(row, "profit", 0) or 0)), currency),
        client_tag=tag,
    )


def _optional_price(value: Any, digits: int) -> Price | None:
    """A stop or target the terminal reports as ``0`` means "none", not "at zero"."""
    if value is None:
        return None
    raw = Decimal(str(value))
    return None if raw == 0 else Price(raw, digits)
