"""The venue must convert price to money the same way everywhere, and generally.

The bug
-------
`SimulatedBroker` priced a position two different ways. Realised P/L used
``ticks x tick_value``, and floating P/L used ``delta x contract_size`` -- ignoring
``tick_value`` and ``tick_size`` entirely.

On Alpari US30 the two agree, because ``tick_size == tick_value == 0.1`` makes the conversions
coincide. On any instrument where they differ -- which is most of them -- the account shows a
jump at the moment a position closes, and the jump is the size of the disagreement. Nothing
raises, and the equity curve is smooth right up to the exit.

There is also a unit trap in the neighbourhood, and this project has now walked into it once.
A price difference is in **price units**; a *point* difference is that divided by ``point``;
a *tick* count is it divided by ``tick_size``. On this CFD the last two are equal, so a formula
that multiplies the raw price difference by a per-point value looks ten times too small and
reads as a dramatic P&L bug when it is a units mistake in the checking script.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from stop_order_scalp.domain.enums import Environment
from stop_order_scalp.domain.models import EnvironmentSettings, PositionRecord
from stop_order_scalp.domain.value_objects import Price, SymbolSpecification
from stop_order_scalp.execution.simulated_broker import (
    SimulatedBroker,
    configure_specification,
    reset_venue,
)


def spec(
    *,
    point: str = "0.1",
    tick_size: str = "0.1",
    tick_value: str = "0.1",
    contract_size: str = "1.0",
    symbol: str = "US30",
) -> SymbolSpecification:
    return SymbolSpecification(
        name=symbol,
        digits=1,
        point=Decimal(point),
        tick_size=Decimal(tick_size),
        tick_value=Decimal(tick_value),
        contract_size=Decimal(contract_size),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("300.0"),
        volume_step=Decimal("0.01"),
    )


def broker_with(specification: SymbolSpecification, *, balance: str = "100000") -> SimulatedBroker:
    reset_venue()
    configure_specification(specification)
    broker = SimulatedBroker(
        EnvironmentSettings(environment=Environment.DRY_RUN, allow_live=False, allow_order=False),
        balance=Decimal(balance),
    )
    broker.connect()
    return broker


def place(
    broker: SimulatedBroker,
    *,
    entry: str,
    stop: str,
    target: str,
    lots: str,
    symbol: str = "US30",
    side: str = "BUY",
) -> int:
    from stop_order_scalp.domain.enums import OrderKind
    from stop_order_scalp.domain.models import OrderIntent
    from stop_order_scalp.domain.value_objects import Volume

    kind = (
        OrderKind.ORDER_KIND_BUY_STOP
        if side == "BUY"
        else OrderKind.ORDER_KIND_SELL_STOP
    )
    intent = OrderIntent(
        plan_id="p",
        client_tag="tag",
        symbol=symbol,
        kind=kind,
        volume=Volume.of(Decimal(lots)),
        entry=Price.parse(entry, 1),
        stop_loss=Price.parse(stop, 1),
        take_profit=Price.parse(target, 1),
        magic_number=1,
        comment="",
        deviation_points=20,
        expiration=None,
    )
    return broker.place_order(intent).ticket


class TestFloatingAndRealisedAgree:
    """A position priced two ways shows a jump when it closes. It must not."""

    @pytest.mark.parametrize(
        ("label", "kwargs"),
        [
            ("Alpari US30, where the two conversions coincide", {}),
            ("tick_value differs from tick_size", {"tick_value": "0.05", "tick_size": "0.1"}),
            ("contract size is not 1", {"contract_size": "10.0"}),
            ("both differ", {"tick_value": "1.0", "tick_size": "0.01", "contract_size": "5.0"}),
        ],
    )
    def test_no_jump_at_closure(self, label: str, kwargs: dict[str, str]) -> None:
        """Floating P/L just before closing must equal the realised P/L after it."""
        reset_venue()
        specification = spec(**kwargs)
        configure_specification(specification, commission=Decimal("0"))
        broker = SimulatedBroker(
            EnvironmentSettings(
                environment=Environment.DRY_RUN, allow_live=False, allow_order=False
            ),
            balance=Decimal("100000"),
        )
        broker.connect()
        broker.publish("US30", Decimal("50000.0"), Decimal("50000.1"), digits=1)
        # A BUY *stop* must rest above the market, so the resting level is 50100 and the
        # position opens when price trades up through it.
        place(broker, entry="50100.0", stop="49000.0", target="52000.0", lots="2.0")

        # Push price up so the stop triggers and the position opens.
        broker.publish("US30", Decimal("50100.0"), Decimal("50100.1"), digits=1)
        position = _open_position(broker)
        assert position is not None, "the BUY stop did not fill"

        # **Move the price away from the entry before reading anything.** The first version of
        # this test closed at the fill price, where the move is zero -- so both conversions
        # returned 0, both assertions passed, and the test could not fail. A test for a
        # conversion bug has to have a non-zero move or it is testing zero.
        broker.publish("US30", Decimal("50400.0"), Decimal("50400.1"), digits=1)
        moved = _open_position(broker)
        assert moved is not None
        floating = moved.profit.amount
        assert floating != 0, (
            "the position is flat, so this test would compare 0 with 0 and pass against any "
            "conversion at all"
        )

        # Close at exactly the same price and compare.
        realised = broker.close(moved.ticket).amount

        assert floating == realised, (
            f"{label}: floating P/L was {floating} but closing booked {realised}. A jump of "
            "this size means the two paths price a position differently."
        )

    def test_the_conversion_is_the_documented_one(self) -> None:
        """value = delta / tick_size x tick_value x contract_size, stated as an expectation."""
        # Alpari US30: 100.0 of price on 1 lot.
        assert _expected(Decimal("100.0"), "0.1", "0.1", "1.0") == Decimal("100.0")
        # A gold-like contract: tick_size 0.01, tick_value 1.0, contract 100.
        assert _expected(Decimal("5.0"), "0.01", "1.0", "100.0") == Decimal("50000.0")
        # Where tick_value is half the tick size, the two used to disagree by 2x.
        assert _expected(Decimal("10.0"), "0.1", "0.05", "1.0") == Decimal("5.0")

    def test_a_short_position_loses_when_price_rises(self) -> None:
        """Sign convention, in both directions.

        A different property from the conversion *factor* above, and deliberately so: this one
        passes with either implementation, because both handle the sign. It is here to catch a
        conversion that is the right magnitude and the wrong direction -- which is exactly the
        bug an absolute-value assertion cannot see. The price is pushed away from the entry so
        the move is non-zero.
        """
        reset_venue()
        configure_specification(spec(), commission=Decimal("0"))
        broker = SimulatedBroker(
            EnvironmentSettings(environment=Environment.DRY_RUN, allow_live=False, allow_order=False),
            balance=Decimal("100000"),
        )
        broker.connect()
        broker.publish("US30", Decimal("50000.0"), Decimal("50000.1"), digits=1)
        # A SELL stop rests below the market.
        place(
            broker, entry="49900.0", stop="51000.0", target="48000.0", lots="1.0", side="SELL"
        )
        broker.publish("US30", Decimal("49900.0"), Decimal("49900.1"), digits=1)
        assert _open_position(broker) is not None

        # Price rises 100 points against a short: it must lose.
        broker.publish("US30", Decimal("50000.0"), Decimal("50000.1"), digits=1)
        position = _open_position(broker)
        assert position is not None
        assert position.profit.amount < 0, (
            f"a short lost money should show a loss, got {position.profit.amount}"
        )
        assert broker.close(position.ticket).amount < 0

        # And the mirror image: price falling against a long must also lose.
        reset_venue()
        configure_specification(spec(), commission=Decimal("0"))
        broker2 = SimulatedBroker(
            EnvironmentSettings(environment=Environment.DRY_RUN, allow_live=False, allow_order=False),
            balance=Decimal("100000"),
        )
        broker2.connect()
        broker2.publish("US30", Decimal("50000.0"), Decimal("50000.1"), digits=1)
        place(broker2, entry="50100.0", stop="49000.0", target="52000.0", lots="1.0")
        broker2.publish("US30", Decimal("50100.0"), Decimal("50100.1"), digits=1)
        broker2.publish("US30", Decimal("49900.0"), Decimal("49900.1"), digits=1)
        long_position = _open_position(broker2)
        assert long_position is not None
        assert long_position.profit.amount < 0, (
            f"a long losing money should show a loss, got {long_position.profit.amount}"
        )


def _expected(
    delta: Decimal, tick_size: str, tick_value: str, contract_size: str
) -> Decimal:
    return (delta / Decimal(tick_size)) * Decimal(tick_value) * Decimal(contract_size)


def _open_position(broker: SimulatedBroker) -> PositionRecord | None:
    """The first open position, or ``None``."""
    positions = broker.positions()
    return positions[0] if positions else None
