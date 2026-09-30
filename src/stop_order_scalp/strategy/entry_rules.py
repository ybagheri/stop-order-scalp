"""Where the pending stop order goes.

Two rules, and both are exact:

* **BUY STOP** at ``M1.high + offset_points``
* **SELL STOP** at ``M1.low - offset_points``

The offset is in **points**, and a point is whatever the broker's specification says it is.
For this project's assumed `US30` that is `0.1`, so 10 points is 1.0 price units — but that
is a consequence of the broker's numbers, not an assumption baked in here. Every conversion
goes through :meth:`SymbolSpecification.points_to_price`. No function in this module
multiplies a point count by a price, and there is no constant anywhere that says what a
point is worth.

Both prices are snapped onto the broker's tick grid, and the rounding direction is
deliberate. An entry price that rounds *toward* the candle makes the stop easier to reach
than the strategy specified, which changes the trade, and it moves the order closer to
market, which is where a broker's ``stops_level`` rejection lives. So both round **away
from the candle**: a BUY STOP rounds up, a SELL STOP rounds down.

That is the *opposite* sign to
:meth:`SymbolSpecification.round_stop_price`, and deliberately so. A stop loss is anchored
on the entry; an entry is anchored on the candle's extreme. See
:meth:`SymbolSpecification.round_entry_price`, which is the only rounding an entry may use.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from stop_order_scalp.domain.enums import OrderKind, Side
from stop_order_scalp.domain.exceptions import DomainError
from stop_order_scalp.domain.models import Candle
from stop_order_scalp.domain.value_objects import Price, SymbolSpecification

__all__ = [
    "EntryLevel",
    "buy_stop_price",
    "entry_level",
    "sell_stop_price",
    "validate_entry_candle",
]


@dataclass(frozen=True, slots=True)
class EntryLevel:
    """A resolved pending-order price and the geometry that produced it."""

    side: Side
    order_kind: OrderKind
    #: The price the broker will be asked to fill at.
    price: Price
    #: The candle high or low the level was derived from, before the offset.
    anchor: Price
    #: The offset in points that was applied.
    offset_points: int
    #: The offset as a price distance, resolved through the specification.
    offset_distance: Decimal
    #: Whether the price moved after rounding. Useful in a journal: a stop that rounded is
    #: a slightly different trade from the one that was specified, and that should be
    #: visible rather than discovered.
    rounded: bool

    @property
    def is_buy_stop(self) -> bool:
        return self.order_kind is OrderKind.ORDER_KIND_BUY_STOP

    def __str__(self) -> str:
        return f"EntryLevel({self.order_kind} at {self.price} from {self.anchor})"


def buy_stop_price(
    candle: Candle,
    offset_points: int,
    specification: SymbolSpecification,
) -> EntryLevel:
    """``M1.high + offset_points``, snapped onto the tick grid away from the candle."""
    _validate_offset(offset_points)
    distance = specification.points_to_price(offset_points)
    raw = candle.high.value + distance
    price = specification.round_entry_price(raw, Side.SIDE_BUY)
    return EntryLevel(
        side=Side.SIDE_BUY,
        order_kind=OrderKind.ORDER_KIND_BUY_STOP,
        price=price,
        anchor=candle.high,
        offset_points=offset_points,
        offset_distance=distance,
        rounded=price.value != raw,
    )


def sell_stop_price(
    candle: Candle,
    offset_points: int,
    specification: SymbolSpecification,
) -> EntryLevel:
    """``M1.low - offset_points``, snapped onto the tick grid away from the candle."""
    _validate_offset(offset_points)
    distance = specification.points_to_price(offset_points)
    raw = candle.low.value - distance
    price = specification.round_entry_price(raw, Side.SIDE_SELL)
    return EntryLevel(
        side=Side.SIDE_SELL,
        order_kind=OrderKind.ORDER_KIND_SELL_STOP,
        price=price,
        anchor=candle.low,
        offset_points=offset_points,
        offset_distance=distance,
        rounded=price.value != raw,
    )


def entry_level(
    candle: Candle,
    side: Side,
    offset_points: int,
    specification: SymbolSpecification,
) -> EntryLevel:
    """The entry level for ``side``, dispatching to the two rules above.

    One entry point rather than a ``match`` at every call site, so the two rules cannot
    drift apart.
    """
    if side is Side.SIDE_BUY:
        return buy_stop_price(candle, offset_points, specification)
    return sell_stop_price(candle, offset_points, specification)


def validate_entry_candle(candle: Candle, timeframe: str) -> None:
    """Refuse a candle that is not the entry timeframe.

    The direction filter reads M15 and the entry rule reads M1. Handing an M15 candle to
    the M1 rule would place a stop 10 points beyond a 15-minute high, which is not a
    subtly wrong trade — it is a completely different one, and it would look plausible.
    """
    if candle.timeframe != timeframe:
        raise DomainError(
            f"entry rule expected a {timeframe} candle but received a {candle.timeframe} "
            f"one opened at {candle.open_time.isoformat()}"
        )


def _validate_offset(offset_points: int) -> None:
    if offset_points < 0:
        raise DomainError(
            f"offset_points must be non-negative, got {offset_points}; a negative offset "
            "would place the stop order inside the candle it was derived from"
        )
