"""A specification is a contract fact, and two brokers disagree by a factor of ten.

Measured on the same day, same index, two brokers:

                    point   digits   value per point per lot
    Alpari US30      0.1       1          0.10
    A Markets DJ30   1.0       0          1.00

Ten times apart, and they do not even agree on what a "point" is. Both were the *measured*
values -- the earlier one replaced a hand-written guess that was also out by ten, in the
opposite direction. So this file's job is to keep a broker's numbers from leaking into another
broker's replay, and to refuse a symbol nobody measured rather than falling back to a default.

A default here is the exact failure Phase 11 exists to prevent: a plausible specification for
an instrument nobody measured, which produces confident numbers about a contract that does not
exist.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from stop_order_scalp.domain.exceptions import InvalidSpecificationError
from stop_order_scalp.market_data.symbols import (
    MEASURED,
    MEASURED_BY_SYMBOL,
    MEASURED_DOWJONES30,
    us30_specification,
)


class TestTheTwoBrokersAreNotInterchangeable:
    def test_they_really_do_differ(self) -> None:
        """Asserted as a difference, so a well-meaning "simplification" cannot unify them."""
        assert MEASURED.value_per_point_per_lot == Decimal("0.1")
        assert MEASURED_DOWJONES30.value_per_point_per_lot == Decimal("1.0")
        assert MEASURED.digits == 1
        assert MEASURED_DOWJONES30.digits == 0
        assert MEASURED.point != MEASURED_DOWJONES30.point

    def test_each_symbol_gets_its_own(self) -> None:
        assert us30_specification("US30").tick_value == Decimal("0.1")
        assert us30_specification("DowJones30").tick_value == Decimal("1.0")
        assert us30_specification("US30").digits == 1
        assert us30_specification("DowJones30").digits == 0

    def test_the_lookup_is_exact(self) -> None:
        """No fuzzy matching: `us30mini` must not resolve to `US30`.

        The alias list in the configuration is matched exactly for the same reason -- a
        substring match could trade an instrument nobody approved.
        """
        with pytest.raises(InvalidSpecificationError):
            us30_specification("US30mini")
        with pytest.raises(InvalidSpecificationError):
            us30_specification("us30")  # case matters at the specification level


class TestAnUnmeasuredSymbolIsAnError:
    def test_it_raises_rather_than_defaulting(self) -> None:
        with pytest.raises(InvalidSpecificationError, match="no measured specification"):
            us30_specification("EURUSD")

    def test_the_error_says_what_to_do(self) -> None:
        """An operator needs to be told the fix, not just that something is wrong."""
        with pytest.raises(InvalidSpecificationError) as caught:
            us30_specification("SPX500")
        message = str(caught.value)
        assert "US30" in message and "DowJones30" in message, "does not list what is measured"
        assert "Re-measure" in message, "does not say how to fix it"


class TestTheMeasurementsAreLabelled:
    def test_every_entry_is_keyed_by_a_broker_symbol(self) -> None:
        assert set(MEASURED_BY_SYMBOL) == {"US30", "DowJones30"}

    def test_the_recorded_source_is_present(self) -> None:
        """A measured value without a date and a broker is a rumour."""
        from stop_order_scalp.market_data.symbols import MEASURED_FROM, MEASURED_ON

        assert MEASURED_ON  # a date
        assert "Alpari" in MEASURED_FROM  # and a source

    def test_volume_limits_are_within_the_broker_bounds(self) -> None:
        """A specification whose minimum exceeds its own maximum is nonsense."""
        for symbol, measured in MEASURED_BY_SYMBOL.items():
            assert measured.volume_min <= measured.volume_max, symbol
            assert measured.volume_step > 0, symbol


class TestTheConfigurationAgrees:
    def test_every_configured_alias_has_a_measurement(self) -> None:
        """An alias with no measured specification would resolve and then fail at build time.

        Better to catch it in a test, where the message can name the alias, than at the moment
        somebody tries to trade.
        """
        from stop_order_scalp.infrastructure.config import load_config

        config = load_config()
        measured = set(MEASURED_BY_SYMBOL)
        for alias in config.strategy.symbol_aliases:
            # Alpari's naming variants all map onto the one measured US30 contract.
            if alias in {"US30", "US30.cash", "US30m", "DJ30"}:
                assert "US30" in measured, f"{alias} resolves to an unmeasured contract"
            else:
                assert alias in measured, (
                    f"{alias} is an approved alias but has no measured specification in "
                    "market_data/symbols.py"
                )
