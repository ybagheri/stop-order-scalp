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

from stop_order_scalp.domain.value_objects import SymbolSpecification

__all__ = ["MEASURED_FROM", "MEASURED_ON", "us30_specification"]


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


#: As read from the terminal. The assumed values, for comparison, are in ``docs/mt5/``.
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


def us30_specification(symbol: str = "US30") -> SymbolSpecification:
    """The measured US30 specification as the domain object the venue needs.

    Named ``us30_...`` rather than ``assumed_...`` because it is no longer an assumption. The
    name change is deliberate: the old name is what let the value sit in two modules
    unchallenged for ten phases, and a reader seeing "assumed" in a money path learns to
    discount it.
    """
    return SymbolSpecification(
        name=symbol,
        digits=MEASURED.digits,
        point=MEASURED.point,
        tick_size=MEASURED.tick_size,
        tick_value=MEASURED.tick_value,
        contract_size=MEASURED.contract_size,
        volume_min=MEASURED.volume_min,
        volume_max=MEASURED.volume_max,
        volume_step=MEASURED.volume_step,
        stops_level=MEASURED.stops_level,
        freeze_level=MEASURED.freeze_level,
    )
