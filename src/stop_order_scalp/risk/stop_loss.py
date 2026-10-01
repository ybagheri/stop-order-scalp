"""Where the stop loss goes, and how it must be rounded.

The baseline places the stop ``target.stop_loss_points`` from the entry. A stop loss that
gets rounded *toward* entry is tighter protection than the risk engine approved, which
means the approved size is too large for the protection actually in place -- the risk
becomes a different number from the one that was checked.

So the rounding is asymmetric and fixed:

* a **BUY** stop sits *below* entry, so away from entry means rounding **down**
* a **SELL** stop sits *above* entry, so away from entry means rounding **up**

That is :meth:`SymbolSpecification.round_stop_price`, which is anchored on the **entry**.
It is deliberately *not* :meth:`SymbolSpecification.round_entry_price`, which is anchored on
the candle extreme and rounds the other way; Phase 3 found that mixing them up moves a
level by a tick in the wrong direction, and `tests/strategy/test_entry_rules.py` pins the
difference.

Three implementations, because three sources of a stop are legitimate:

``FixedPointsStopProvider``
    the baseline: N points from entry.

``RiskRewardStopProvider``
    derives the distance from the target, so that a configured ratio is actually achieved.

``SignalStopProvider``
    uses a stop the signal carried, falling back to fixed points when it carries none.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Protocol, runtime_checkable

from stop_order_scalp.domain.enums import Side
from stop_order_scalp.domain.exceptions import RiskError
from stop_order_scalp.domain.value_objects import Price, SymbolSpecification

__all__ = [
    "FixedPointsStopProvider",
    "RiskRewardStopProvider",
    "SignalStopProvider",
    "StopLevel",
    "StopLossProvider",
    "default_stop_provider",
]


@dataclass(frozen=True, slots=True)
class StopLevel:
    """A resolved stop, and the distance it represents."""

    price: Price
    side: Side
    #: Distance from entry in points. Always positive.
    distance_points: Decimal
    #: What produced it, for the journal.
    source: str
    #: Whether the snap to the tick grid moved the price.
    rounded: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "price": str(self.price),
            "side": str(self.side),
            "distance_points": str(self.distance_points),
            "source": self.source,
            "rounded": self.rounded,
        }

    def __str__(self) -> str:
        return f"StopLevel({self.price} at {self.distance_points} points, {self.source})"


@runtime_checkable
class StopLossProvider(Protocol):
    """Where the protective stop goes."""

    def stop_for(
        self, entry: Price, side: Side, specification: SymbolSpecification
    ) -> StopLevel: ...


@dataclass(frozen=True, slots=True)
class FixedPointsStopProvider:
    """The baseline: ``stop_loss_points`` from the entry.

    ``points`` is the configured distance. A provider is stateless and holds no
    configuration of its own beyond that number, so a test can construct exactly the
    provider the configuration describes.
    """

    points: int

    def __post_init__(self) -> None:
        if self.points <= 0:
            raise RiskError(f"stop_loss_points must be positive, got {self.points}")

    def stop_for(
        self, entry: Price, side: Side, specification: SymbolSpecification
    ) -> StopLevel:
        distance = specification.points_to_price(self.points)
        raw = entry.value - distance if side is Side.SIDE_BUY else entry.value + distance
        price = specification.round_stop_price(raw, side)
        return StopLevel(
            price=price,
            side=side,
            distance_points=specification.price_to_points(entry.absolute_distance_to(price)),
            source=f"fixed_points:{self.points}",
            rounded=price.value != raw,
        )

    def __str__(self) -> str:
        return f"FixedPointsStopProvider({self.points} points)"


@dataclass(frozen=True, slots=True)
class RiskRewardStopProvider:
    """A stop distance that makes a configured reward-to-risk ratio achievable.

    Used when the take profit comes from configuration and the stop has to be derived, or
    the reverse. Given the distance to the target, the stop is placed so that
    ``target_distance / stop_distance == ratio``.

    :meth:`stop_for` cannot derive a ratio without knowing the target, so it falls back to
    the configured fixed-point distance. A caller that has a target should call
    :meth:`distance_for` first; the fallback exists so this satisfies
    :class:`StopLossProvider` without pretending to a precision it does not have.
    """

    ratio: Decimal
    points: int

    def __post_init__(self) -> None:
        if self.ratio <= 0:
            raise RiskError(f"risk_reward must be positive, got {self.ratio}")
        if self.points <= 0:
            raise RiskError(f"stop_loss_points must be positive, got {self.points}")

    def stop_for(
        self, entry: Price, side: Side, specification: SymbolSpecification
    ) -> StopLevel:
        return FixedPointsStopProvider(self.points).stop_for(entry, side, specification)

    def distance_for(
        self, target: Price, entry: Price, side: Side, specification: SymbolSpecification
    ) -> Decimal:
        """Stop distance in points that achieves ``ratio`` against ``target``."""
        del side  # a distance is unsigned; the side does not change it
        target_points = specification.price_to_points(
            entry.absolute_distance_to(target)
        )
        if target_points <= 0:
            raise RiskError(
                "the take profit coincides with entry, so no stop distance can produce "
                "the configured ratio"
            )
        return (target_points / self.ratio).to_integral_value(rounding=ROUND_CEILING)

    def __str__(self) -> str:
        return f"RiskRewardStopProvider(ratio={self.ratio}, fallback={self.points} points)"


@dataclass(frozen=True, slots=True)
class SignalStopProvider:
    """A stop the signal carried, falling back to fixed points.

    The only provider that consults the signal. It exists so that a signal supplying its
    own stop remains possible without the *strategy* producing stops -- Phase 3 kept that
    separation deliberately, so it stays here in the risk layer.
    """

    points: int
    fallback: FixedPointsStopProvider

    def stop_for(
        self,
        entry: Price,
        side: Side,
        specification: SymbolSpecification,
        *,
        supplied: Price | None = None,
    ) -> StopLevel:
        if supplied is None:
            return self.fallback.stop_for(entry, side, specification)
        if supplied == entry:
            raise RiskError(
                "the signal's stop loss coincides with the entry, which gives no protection"
            )
        wrong_side = (side is Side.SIDE_BUY and supplied > entry) or (
            side is Side.SIDE_SELL and supplied < entry
        )
        if wrong_side:
            # Refusing rather than flipping the side: silently inverting a stop would turn
            # a protection into a target.
            raise RiskError(
                f"the signal's stop {supplied} is on the wrong side of a {side} entry "
                f"{entry}"
            )
        snapped = specification.round_stop_price(supplied, side)
        return StopLevel(
            price=snapped,
            side=side,
            distance_points=specification.price_to_points(entry.absolute_distance_to(snapped)),
            source="signal_defined",
            rounded=snapped.value != supplied.value,
        )

    def __str__(self) -> str:
        return f"SignalStopProvider(fallback={self.fallback})"


def default_stop_provider(stop_loss_points: int) -> StopLossProvider:
    """The baseline provider. One place to change if the default ever moves."""
    return FixedPointsStopProvider(stop_loss_points)


def stop_distance_points(
    entry: Price,
    stop: Price,
    side: Side,
    specification: SymbolSpecification,
) -> Decimal:
    """Unsigned entry-to-stop distance in points, refusing a stop on the wrong side."""
    wrong_side = (side is Side.SIDE_BUY and stop >= entry) or (
        side is Side.SIDE_SELL and stop <= entry
    )
    if wrong_side:
        raise RiskError(
            f"a {side} stop at {stop} is not protective against an entry at {entry}"
        )
    return specification.price_to_points(entry.absolute_distance_to(stop))
