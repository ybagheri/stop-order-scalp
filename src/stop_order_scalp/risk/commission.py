"""Commission, and the ambiguity that must never be guessed.

One configured number, ``risk.commission_per_lot``, and two possible meanings:

* ``per_lot_round_trip`` -- the figure covers opening **and** closing. The baseline.
* ``per_lot_per_side`` -- the figure covers one side, so a round trip costs twice it.

The two differ by exactly a factor of two, and therefore by a factor of two in the
position size that a risk budget can support. Reading the wrong one does not produce an
error; it produces a position twice the intended size. So the mode is read from
configuration every time, and there is no default path that could guess.

Commission is kept **separate from price risk** throughout, because the strategy
specification requires the three figures to be distinguishable:

===================  =========================================================
``price_risk``       entry to stop, commission excluded
``commission``        the estimated cost of the whole planned trade
``total_risk``        ``price_risk + commission`` -- what ``0.5 %`` is compared against
===================  =========================================================

Sizing on ``price_risk`` alone would over-size every position by the cost of the
commission. On the numbers in ``docs/mt5/SYMBOL_SPECIFICATIONS.md`` §2 that is 6 % --
small, systematic, and invisible unless the arithmetic is written out.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from stop_order_scalp.domain.enums import CommissionMode
from stop_order_scalp.domain.value_objects import Money, Volume

__all__ = [
    "CommissionModel_",
    "commission_for",
    "per_lot_cost",
    "round_trip_cost",
]


def per_lot_cost(rate: Decimal, mode: CommissionMode) -> Decimal:
    """The round-trip cost of one lot, given a rate and its interpretation.

    :param mode: read from configuration, never inferred. ``PER_LOT_PER_SIDE`` doubles the
        rate; ``PER_LOT_ROUND_TRIP`` uses it as-is.
    """
    if rate < 0:
        raise ValueError(f"commission rate must be non-negative, got {rate}")
    return rate * Decimal(mode.sides)


def round_trip_cost(
    volume: Volume | Decimal,
    rate: Decimal,
    mode: CommissionMode,
    currency: str = "USD",
) -> Money:
    """Estimated commission for a whole trade at ``volume``.

    For a position held for minutes the entry and exit rates are the same, so one
    round-trip figure is an honest estimate. It is an *estimate* rather than a promise:
    Phase 11 records the broker's real schedule, and a swap or a tiered rate would change
    this number.
    """
    lots = volume.lots if isinstance(volume, Volume) else Decimal(str(volume))
    return Money(lots * per_lot_cost(rate, mode), currency)


def commission_for(
    volume: Volume | Decimal,
    rate: Decimal,
    mode: CommissionMode,
    currency: str = "USD",
) -> Money:
    """Alias for :func:`round_trip_cost`, named for how the risk engine reads it."""
    return round_trip_cost(volume, rate, mode, currency)


@dataclass(frozen=True, slots=True)
class CommissionModel_:
    """A resolved commission rate, with its interpretation fixed.

    Preferred over passing ``(rate, mode)`` as two loose arguments, because the pair is
    what a caller has to get right and a single frozen object cannot be assembled with one
    of them missing.

    Named with a trailing underscore because :class:`~stop_order_scalp.domain.models.CommissionModel`
    is the domain record this will eventually become. Until then the two coexist and this
    one is clearly the transient.
    """

    rate: Decimal
    mode: CommissionMode
    currency: str = "USD"

    def __post_init__(self) -> None:
        if self.rate < 0:
            raise ValueError(f"commission rate must be non-negative, got {self.rate}")

    @property
    def per_lot_round_trip(self) -> Decimal:
        return per_lot_cost(self.rate, self.mode)

    def for_volume(self, volume: Volume | Decimal) -> Money:
        lots = volume.lots if isinstance(volume, Volume) else Decimal(str(volume))
        return Money(lots * self.per_lot_round_trip, self.currency)

    def as_dict(self) -> dict[str, object]:
        return {
            "rate": str(self.rate),
            "mode": str(self.mode),
            "sides": self.mode.sides,
            "per_lot_round_trip": str(self.per_lot_round_trip),
            "currency": self.currency,
        }

    def __str__(self) -> str:
        return (
            f"CommissionModel_({self.rate} {self.mode} "
            f"= {self.per_lot_round_trip} per lot round trip)"
        )
