"""The US30 symbol specification, measured from a real Alpari demo terminal.

Single source of truth, and the reason it exists
-------------------------------------------------
Before Phase 11 this specification was hand-written from memory, and it was wrong by a factor
of ten in the one number that decides what every money figure in this project means.

    tick_value      measured 0.1      assumed 1.0
    volume_min      measured 0.01     assumed 0.1
    volume_max      measured 300.0    assumed 50.0
    volume_step     measured 0.01     assumed 0.1
    stops_level     measured 0        assumed 10

A tick value of 1.0 made a 100-point stop look like $100 per lot, so the position sizer chose
0.4 lots where the real answer is 4.0 -- **a tenth of the intended risk on every trade** --
while every reported P/L figure was ten times too large. Two errors, opposite directions,
which is the worst combination: the trades looked far more profitable than they were, and they
were much smaller than they should have been.

Captured 2026-10-02 from Alpari MT5 build 6230, account 53137121 on `Alpari-MT5-Demo`. The
full dump is in ``docs/mt5/MEASURED_US30.json``; this module is the part the code needs.

**These are broker values, not universal truth.** A different account or a different broker
would have different numbers, and a CFD's tick value can change with the underlying index. So
this is a recorded measurement with a date and a source, not a constant to trust forever --
`probe_symbol` re-reads the live values, and Phase 11's job is to compare them against this.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from stop_order_scalp.domain.exceptions import InvalidSpecificationError
from stop_order_scalp.domain.value_objects import SymbolSpecification

__all__ = [
    "MEASURED",
    "MEASURED_BITCOIN",
    "MEASURED_BY_SYMBOL",
    "MEASURED_DOWJONES30",
    "MEASURED_FROM",
    "MEASURED_ON",
    "us30_specification",
]


#: When these numbers were read, and from where. Kept next to the numbers because a
#: specification without a date is a rumour.
MEASURED_ON = "2026-10-02"
MEASURED_FROM = "Alpari MT5 build 6230, account 53137121, Alpari-MT5-Demo, symbol US30"


@dataclass(frozen=True, slots=True)
class MeasuredUS30:
    """The raw measured fields, so a comparison against the live venue is arithmetic."""

    point: Decimal
    tick_size: Decimal
    tick_value: Decimal
    contract_size: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    stops_level: int
    freeze_level: int
    digits: int

    @property
    def value_per_point_per_lot(self) -> Decimal:
        """What one point of price is worth on one lot.

        The number the whole project turns on. It is *not* ``tick_value``: that is the value
        of one ``tick_size`` increment, and confusing the two is how a 10x error hides inside
        a plausible-looking constant.
        """
        return self.tick_value * (Decimal(1) / self.tick_size) * self.point

    def as_dict(self) -> dict[str, str]:
        return {
            "point": str(self.point),
            "tick_size": str(self.tick_size),
            "tick_value": str(self.tick_value),
            "contract_size": str(self.contract_size),
            "volume_min": str(self.volume_min),
            "volume_max": str(self.volume_max),
            "volume_step": str(self.volume_step),
            "stops_level": str(self.stops_level),
            "freeze_level": str(self.freeze_level),
            "digits": str(self.digits),
            "value_per_point_per_lot": str(self.value_per_point_per_lot),
        }


#: As read from Alpari MT5. Superseded for A Markets -- see the table above.
MEASURED = MeasuredUS30(
    point=Decimal("0.1"),
    tick_size=Decimal("0.1"),
    tick_value=Decimal("0.1"),
    contract_size=Decimal("1.0"),
    volume_min=Decimal("0.01"),
    volume_max=Decimal("300.0"),
    volume_step=Decimal("0.01"),
    stops_level=0,
    freeze_level=0,
    digits=1,
)

#: A Markets `DowJones30`, measured 2026-10-02, account 8039744 on `AMarkets-Demo`.
#:
#: Quoted to **zero decimals**. `point` is 1.0, so one "point" in this project's configuration
#: is one whole index point, and a 100-point stop costs $100 a lot rather than $10. The position
#: sizer handles that on its own -- it is told the specification, not the number of lots -- but
#: it is worth naming because every offset, stop and target in `config/default.yaml` means
#: something ten times larger here than it did on Alpari.
MEASURED_DOWJONES30 = MeasuredUS30(
    point=Decimal("1.0"),
    tick_size=Decimal("1.0"),
    tick_value=Decimal("1.0"),
    contract_size=Decimal("1.0"),
    volume_min=Decimal("0.01"),
    volume_max=Decimal("100.0"),
    volume_step=Decimal("0.01"),
    stops_level=0,
    freeze_level=0,
    digits=0,
)

#: Alpari `BITCOIN`, measured 2026-10-03 on `Alpari-MT5-Demo` (``symbol_info``, read by hand).
#:
#: Added so the pipeline can be exercised on a 24-hour instrument while the index is closed.
#: One lot is one coin, `point` is one cent, so a point is worth **$0.01 per lot** and a 100-point
#: stop is a **$1** stop. The distances in ``config/default.yaml`` were chosen for an index near
#: 50 000 and mean almost nothing against a coin worth ten times as much; use a separate config
#: for this symbol. A wiring test, not a claim that this strategy suits the instrument.
MEASURED_BITCOIN = MeasuredUS30(
    point=Decimal("0.01"),
    tick_size=Decimal("0.01"),
    tick_value=Decimal("0.01"),
    contract_size=Decimal("1.0"),
    volume_min=Decimal("0.01"),
    volume_max=Decimal("300.0"),
    volume_step=Decimal("0.01"),
    stops_level=0,
    freeze_level=0,
    digits=2,
)

#: Keyed by the broker's own symbol name, because that is the identity the venue uses.
MEASURED_BY_SYMBOL: dict[str, MeasuredUS30] = {
    "US30": MEASURED,
    "DowJones30": MEASURED_DOWJONES30,
    "BITCOIN": MEASURED_BITCOIN,
}


def us30_specification(symbol: str = "US30") -> SymbolSpecification:
    """The measured specification for ``symbol``, or an error naming what is available.

    Per broker, because a specification is a *contract* fact and not a universal one. Measured
    from two brokers on the same day, the same index:

        Alpari US30        point 0.1    value per point per lot 0.10
        A Markets DowJones30  point 1.0  value per point per lot 1.00

    Ten times apart, and the two disagree about what a "point" even is -- Alpari quotes US30
    to one decimal, A Markets to none at all. Carrying one broker's numbers into the other
    silently mis-scales every position and every reported profit, which is precisely the error
    that made Phase 10's first result twelve times too flattering.

    An unknown symbol raises rather than falling back to a default. A default here would be the
    exact failure this function exists to prevent: a plausible specification for an instrument
    nobody measured.
    """
    measured = MEASURED_BY_SYMBOL.get(symbol)
    if measured is None:
        known = ", ".join(sorted(MEASURED_BY_SYMBOL))
        raise InvalidSpecificationError(
            f"no measured specification for {symbol!r}. Measured here: {known}. Re-measure with "
            "the terminal rather than guessing: a different broker's contract sizes, tick "
            "values and volume limits are not interchangeable, and using the wrong ones "
            "mis-scales every position and every reported profit."
        )
    return SymbolSpecification(
        name=symbol,
        digits=measured.digits,
        point=measured.point,
        tick_size=measured.tick_size,
        tick_value=measured.tick_value,
        contract_size=measured.contract_size,
        volume_min=measured.volume_min,
        volume_max=measured.volume_max,
        volume_step=measured.volume_step,
        stops_level=measured.stops_level,
        freeze_level=measured.freeze_level,
    )
