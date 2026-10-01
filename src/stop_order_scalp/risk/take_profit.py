"""Where the take profit goes, and the precedence rule when the answers disagree.

Three sources are legitimate, and they must be ranked rather than raced:

===========================  ====================================================
``signal_defined``           the signal carried a target
``risk_reward``              derived from the stop so the ratio is achieved
``fixed_points``             the baseline: ``take_profit_points`` from entry
===========================  ====================================================

The precedence is ``signal_defined`` > ``risk_reward`` > ``fixed_points``, and
:class:`~stop_order_scalp.domain.enums.TargetMode` documents the same order. The point is
that the mode is a *preference*, not an override of a better answer: a better-defined
target always wins, and a configured mode never overrides geometry a signal deliberately
supplied.

Rounding mirrors the stop: a target is rounded **away from entry**, so it is never easier
to reach than specified. That is
:meth:`SymbolSpecification.round_target_price` -- a BUY target is above entry and rounds
up, a SELL target is below and rounds down.

The baseline is 1000 points with a 100-point stop, which is 10:1. ``risk_reward: 1.0``
means 1:1 instead, i.e. a 100-point target with that stop. Which one runs is visible on
:func:`describe_precedence` and in every ``StopLevel``/``TargetLevel`` ``source`` field.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Protocol, runtime_checkable

from stop_order_scalp.domain.enums import Side, TargetMode
from stop_order_scalp.domain.exceptions import RiskError
from stop_order_scalp.domain.value_objects import Price, SymbolSpecification

__all__ = [
    "FixedPointsTargetProvider",
    "RiskRewardTargetProvider",
    "SignalTargetProvider",
    "TakeProfitProvider",
    "TargetLevel",
    "default_target_provider",
    "describe_precedence",
]


@dataclass(frozen=True, slots=True)
class TargetLevel:
    """A resolved target, and the distance it represents."""

    price: Price
    side: Side
    distance_points: Decimal
    source: str
    rounded: bool = False

    @property
    def mode(self) -> TargetMode:
        """Which configured mode produced this, for reporting."""
        if self.source.startswith("signal"):
            return TargetMode.TARGET_MODE_SIGNAL_DEFINED
        if self.source.startswith("risk_reward"):
            return TargetMode.TARGET_MODE_RISK_REWARD
        return TargetMode.TARGET_MODE_FIXED_POINTS

    def as_dict(self) -> dict[str, object]:
        return {
            "price": str(self.price),
            "side": str(self.side),
            "distance_points": str(self.distance_points),
            "source": self.source,
            "mode": str(self.mode),
            "rounded": self.rounded,
        }

    def __str__(self) -> str:
        return f"TargetLevel({self.price} at {self.distance_points} points, {self.source})"


@runtime_checkable
class TakeProfitProvider(Protocol):
    """Where the profit target goes."""

    def target_for(
        self, entry: Price, side: Side, specification: SymbolSpecification
    ) -> TargetLevel: ...


@dataclass(frozen=True, slots=True)
class FixedPointsTargetProvider:
    """The baseline: ``take_profit_points`` from the entry."""

    points: int

    def __post_init__(self) -> None:
        if self.points <= 0:
            raise RiskError(f"take_profit_points must be positive, got {self.points}")

    def target_for(
        self, entry: Price, side: Side, specification: SymbolSpecification
    ) -> TargetLevel:
        distance = specification.points_to_price(self.points)
        raw = entry.value + distance if side is Side.SIDE_BUY else entry.value - distance
        price = specification.round_target_price(raw, side)
        return TargetLevel(
            price=price,
            side=side,
            distance_points=specification.price_to_points(entry.absolute_distance_to(price)),
            source=f"fixed_points:{self.points}",
            rounded=price.value != raw,
        )

    def __str__(self) -> str:
        return f"FixedPointsTargetProvider({self.points} points)"


@dataclass(frozen=True, slots=True)
class RiskRewardTargetProvider:
    """A target distance that makes a configured ratio achievable against a known stop."""

    ratio: Decimal
    points: int

    def __post_init__(self) -> None:
        if self.ratio <= 0:
            raise RiskError(f"risk_reward must be positive, got {self.ratio}")
        if self.points <= 0:
            raise RiskError(f"take_profit_points must be positive, got {self.points}")

    def distance_for(
        self, entry: Price, stop: Price, specification: SymbolSpecification
    ) -> Decimal:
        """Target distance in points for ``ratio`` against an existing stop."""
        from stop_order_scalp.risk.stop_loss import stop_distance_points

        stop_points = stop_distance_points(
            entry, stop, Side.SIDE_BUY, specification
        )
        if stop_points <= 0:
            raise RiskError(
                "the stop coincides with entry, so no target distance can produce the "
                "configured ratio"
            )
        return (stop_points * self.ratio).to_integral_value(rounding=ROUND_CEILING)

    def target_for(
        self, entry: Price, side: Side, specification: SymbolSpecification
    ) -> TargetLevel:
        """Fall back to fixed points when no stop is known.

        Returning a *less specific* answer rather than a wrong one is deliberate: the
        caller that has a stop should call :meth:`target_from_stop`.
        """
        return FixedPointsTargetProvider(self.points).target_for(entry, side, specification)

    def target_from_stop(
        self, entry: Price, side: Side, stop: Price, specification: SymbolSpecification
    ) -> TargetLevel:
        """The ratio-derived target, given the stop it is measured against."""
        points = self.distance_for(entry, stop, specification)
        distance = specification.points_to_price(points)
        raw = entry.value + distance if side is Side.SIDE_BUY else entry.value - distance
        price = specification.round_target_price(raw, side)
        return TargetLevel(
            price=price,
            side=side,
            distance_points=specification.price_to_points(entry.absolute_distance_to(price)),
            source=f"risk_reward:{self.ratio}",
            rounded=price.value != raw,
        )

    def __str__(self) -> str:
        return f"RiskRewardTargetProvider(ratio={self.ratio}, fallback={self.points} points)"


@dataclass(frozen=True, slots=True)
class SignalTargetProvider:
    """A target the signal carried, falling back to fixed points."""

    points: int
    fallback: FixedPointsTargetProvider

    def target_for(
        self,
        entry: Price,
        side: Side,
        specification: SymbolSpecification,
        *,
        supplied: Price | None = None,
    ) -> TargetLevel:
        if supplied is None:
            return self.fallback.target_for(entry, side, specification)
        if supplied == entry:
            raise RiskError(
                "the signal's take profit coincides with the entry, which is not a target"
            )
        wrong_side = (side is Side.SIDE_BUY and supplied < entry) or (
            side is Side.SIDE_SELL and supplied > entry
        )
        if wrong_side:
            raise RiskError(
                f"the signal's target {supplied} is on the wrong side of a {side} entry "
                f"{entry}"
            )
        snapped = specification.round_target_price(supplied, side)
        return TargetLevel(
            price=snapped,
            side=side,
            distance_points=specification.price_to_points(entry.absolute_distance_to(snapped)),
            source="signal_defined",
            rounded=snapped.value != supplied.value,
        )

    def __str__(self) -> str:
        return f"SignalTargetProvider(fallback={self.fallback})"


def default_target_provider(
    mode: TargetMode,
    *,
    take_profit_points: int,
    risk_reward: Decimal,
) -> TakeProfitProvider:
    """The provider the configured mode describes.

    One place that maps a mode to an implementation, so a mode added later has exactly one
    place to be wired.
    """
    if mode is TargetMode.TARGET_MODE_SIGNAL_DEFINED:
        return SignalTargetProvider(take_profit_points, FixedPointsTargetProvider(take_profit_points))
    if mode is TargetMode.TARGET_MODE_RISK_REWARD:
        return RiskRewardTargetProvider(risk_reward, take_profit_points)
    return FixedPointsTargetProvider(take_profit_points)


def resolve_target(
    *,
    entry: Price,
    side: Side,
    stop: Price | None,
    specification: SymbolSpecification,
    configured: TakeProfitProvider,
    supplied: Price | None = None,
) -> TargetLevel:
    """Apply the precedence rule to whatever answers are available.

    The order, and why:

    1. a target the **signal** supplied -- it was deliberately provided by whatever
       produced the signal;
    2. a **ratio-derived** target, when a stop is known to measure against -- the
       configuration asked for a ratio and a concrete stop can deliver it;
    3. **fixed points** -- the baseline, and the honest fallback.

    A configured mode never overrides a better-defined answer. That is what
    ``TargetMode`` means by "the mode is a preference, not an override".
    """
    if supplied is not None:
        return SignalTargetProvider(
            _points_of(configured), FixedPointsTargetProvider(_points_of(configured))
        ).target_for(entry, side, specification, supplied=supplied)

    if isinstance(configured, RiskRewardTargetProvider) and stop is not None:
        return configured.target_from_stop(entry, side, stop, specification)

    return configured.target_for(entry, side, specification)


def _points_of(provider: TakeProfitProvider) -> int:
    for attribute in ("points",):
        value = getattr(provider, attribute, None)
        if isinstance(value, int):
            return value
    raise RiskError(f"{provider!r} does not expose a fixed-point fallback")


def describe_precedence() -> str:
    """The rule, in one line, for ``status`` output and the journal."""
    return "signal_defined > risk_reward > fixed_points"
