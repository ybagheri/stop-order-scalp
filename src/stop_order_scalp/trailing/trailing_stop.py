"""A stop that follows price and never moves backwards.

The rule from the specification, in one line each:

* a **BUY** stop sits ``bid - distance_points``
* a **SELL** stop sits ``ask + distance_points``

and both are **monotonic** -- a BUY's stop only ever rises, a SELL's only ever falls.

Why monotonicity is not a preference
-----------------------------------

A trailing stop that follows price down converts a winning trade into a losing one. If the
BUY stop tracks ``bid - d`` unconditionally, a pullback walks the stop down through the
entry and then below it, and the trade closes at a loss having been well in profit minutes
earlier. The strategy specification calls this out by name, and
``tests/trailing/test_property_monotonic.py`` proves it over generated price sequences
rather than asserting it on one hand-picked example.

Monotonicity here is **structural**, not a clamp applied afterwards: the provider returns no
proposal at all when the trailing level would be worse than the stop already in place. So
there is no code path that can emit a backwards move, and the property holds because of the
shape of the decision rather than because a later check caught it.

Spread and the tick grid
------------------------

The BUY reference is the **bid** and the SELL reference is the **ask**, because that is where
the position would be closed. Using the mid would place the stop half a spread closer to the
market than reality, which the broker may reject and which understates the locked profit
regardless.

The proposed level is rounded onto the tick grid by :meth:`SymbolSpecification.round_stop_price`,
which rounds *away from entry*. Rounding the other way would make the stop tighter than the
configured distance, which for a trailing stop means the real trailing distance is not the
configured one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from stop_order_scalp.domain.enums import Side
from stop_order_scalp.domain.models import ManagedStop, PositionRecord, Tick
from stop_order_scalp.domain.value_objects import Price, SymbolSpecification
from stop_order_scalp.infrastructure.config import TrailingSettings
from stop_order_scalp.trailing.break_even import exit_price_for

__all__ = [
    "TrailingDecision",
    "TrailingProvider",
    "TrailingRefusal",
    "TrailingStopProvider",
]


class TrailingRefusal:
    """Stable reasons the trailing stop does not move.

    ``NO_CHANGE`` and ``NOT_MONOTONIC`` are the interesting pair. Both mean "leave the stop
    alone", but they are different operational facts -- one is ordinary waiting, the other
    is the guard that kept a pullback from walking the stop back through entry -- so they
    are not collapsed into one code.
    """

    DISABLED: str = "trailing_disabled"
    NOT_ARMED: str = "trailing_not_armed"
    NO_STOP_IN_PLACE: str = "trailing_no_stop_in_place"
    NOT_MONOTONIC: str = "trailing_not_monotonic"
    BELOW_MIN_STEP: str = "trailing_below_min_step"
    VIOLATES_STOPS_LEVEL: str = "trailing_violates_stops_level"
    INSIDE_FREEZE_LEVEL: str = "trailing_inside_freeze_level"


@dataclass(frozen=True, slots=True)
class TrailingDecision:
    """Whether the trailing stop proposes a new level, and why not when it does not."""

    proposal: ManagedStop | None
    code: str = "ok"
    reason: str = ""
    #: The level the trailing rule computed, whether or not it may be used.
    trailing_level: Price | None = None
    #: How far price has travelled in favour, in points.
    favourable_points: Decimal = Decimal(0)

    @property
    def proposed(self) -> bool:
        return self.proposal is not None

    @property
    def armed(self) -> bool:
        """Whether the rule got far enough to have an opinion about the stop.

        False only for :data:`TrailingRefusal.DISABLED`,
        :data:`TrailingRefusal.NO_STOP_IN_PLACE` and
        :data:`TrailingRefusal.NOT_ARMED` -- that is, before any arithmetic was done. Every
        other refusal is a real opinion about a real stop, which is what
        :attr:`BreakEvenDecision.armed` means on the break-even side and what the position
        manager relies on when it reports *why* nothing was sent.
        """
        return self.code not in (
            TrailingRefusal.DISABLED,
            TrailingRefusal.NO_STOP_IN_PLACE,
            TrailingRefusal.NOT_ARMED,
        )

    def require(self) -> ManagedStop:
        if self.proposal is None:
            raise ValueError(f"trailing proposed nothing: {self.code}: {self.reason}")
        return self.proposal

    def __str__(self) -> str:
        if self.proposal is not None:
            return f"TrailingDecision({self.proposal})"
        return f"TrailingDecision({self.code}: {self.reason})"


@runtime_checkable
class TrailingProvider(Protocol):
    """Where a trailing stop should sit."""

    def evaluate(
        self, position: PositionRecord, tick: Tick, specification: SymbolSpecification
    ) -> TrailingDecision: ...


@dataclass(frozen=True, slots=True)
class TrailingStopProvider:
    """The specification's rule: ``bid - d`` for a BUY, ``ask + d`` for a SELL.

    :param min_step_points: refuse a move smaller than this. The second half of
        idempotency -- break-even is idempotent because its target is fixed, but a
        trailing stop's target moves on every tick, so without a minimum step it would
        send an order per tick forever. The default of 1 point is the smallest move the
        broker could distinguish at all.
    """

    settings: TrailingSettings

    def evaluate(
        self, position: PositionRecord, tick: Tick, specification: SymbolSpecification
    ) -> TrailingDecision:
        side = position.side
        entry = position.entry
        current = position.stop_loss

        exit_price = exit_price_for(tick, side)
        favourable = _favourable_points(specification, exit_price, entry, side)

        if not self.settings.enabled:
            return TrailingDecision(
                None,
                TrailingRefusal.DISABLED,
                "trailing is disabled in configuration",
                favourable_points=favourable,
            )

        if current is None:
            return TrailingDecision(
                None,
                TrailingRefusal.NO_STOP_IN_PLACE,
                "the position carries no stop loss, so there is nothing to trail",
                favourable_points=favourable,
            )

        level = self.trailing_level(specification, tick, side)

        if favourable < Decimal(self.settings.arm_after_points):
            return TrailingDecision(
                None,
                TrailingRefusal.NOT_ARMED,
                f"price has moved {favourable} points in favour, "
                f"{self.settings.arm_after_points} required before trailing arms",
                level,
                favourable,
            )

        # Monotonicity, structurally: a level that is not better than the stop in place
        # produces no proposal, so no backwards move can ever reach a broker.
        if not _is_improvement(level, current, side):
            return TrailingDecision(
                None,
                TrailingRefusal.NOT_MONOTONIC,
                f"the trailing level {level} would move the {side} stop backwards from "
                f"{current}; the stop stays where it is",
                level,
                favourable,
            )

        step = abs(specification.distance_points(current, level))
        if step < Decimal(self.settings.min_step_points):
            return TrailingDecision(
                None,
                TrailingRefusal.BELOW_MIN_STEP,
                f"the move to {level} is {step} points, below the configured minimum of "
                f"{self.settings.min_step_points}",
                level,
                favourable,
            )

        frozen = _inside_freeze_level(specification, exit_price, current, side)
        if frozen:
            return TrailingDecision(
                None,
                TrailingRefusal.INSIDE_FREEZE_LEVEL,
                f"price is within the broker's freeze level of {current}; the position "
                "cannot be modified",
                level,
                favourable,
            )

        too_close = _inside_stops_level(specification, exit_price, level, side)
        if too_close:
            return TrailingDecision(
                None,
                TrailingRefusal.VIOLATES_STOPS_LEVEL,
                f"a stop at {level} is closer to the market than the broker's stops_level; "
                "it would be rejected",
                level,
                favourable,
            )

        proposal = ManagedStop(
            position_ticket=position.ticket,
            side=side,
            current_stop=current,
            proposed_stop=level,
            reason=f"trailing_{favourable}pts",
            distance_points=favourable,
        )
        # Belt and braces: the check above already guarantees this, and the property is
        # important enough to assert rather than infer.
        if not proposal.is_monotonic():  # pragma: no cover - unreachable by construction
            return TrailingDecision(
                None,
                TrailingRefusal.NOT_MONOTONIC,
                f"moving to {level} from {current} would move a {side} stop backwards",
                level,
                favourable,
            )
        return TrailingDecision(proposal, "ok", "", level, favourable)

    def trailing_level(
        self, specification: SymbolSpecification, tick: Tick, side: Side
    ) -> Price:
        """``bid - d`` for a BUY, ``ask + d`` for a SELL, on the tick grid.

        Public because the level is worth reporting on its own -- ``status`` and the journal
        both want to show where the trailing rule currently points even when the stop has
        not moved.
        """
        distance = specification.points_to_price(self.settings.distance_points)
        raw = (
            tick.bid.value - distance
            if side is Side.SIDE_BUY
            else tick.ask.value + distance
        )
        return specification.round_stop_price(raw, side)

    def __str__(self) -> str:
        return (
            f"TrailingStopProvider(enabled={self.settings.enabled}, "
            f"distance={self.settings.distance_points})"
        )


def _is_improvement(level: Price, current: Price, side: Side) -> bool:
    """Whether ``level`` is a strictly better stop than ``current`` for ``side``.

    "Better" means in the money: higher for a BUY, lower for a SELL. A level equal to the
    current stop is not an improvement, and proposing it would be a modify that changes
    nothing.
    """
    if side is Side.SIDE_BUY:
        return level > current
    return level < current


def _favourable_points(
    specification: SymbolSpecification, exit_price: Price, entry: Price, side: Side
) -> Decimal:
    delta = exit_price.value - entry.value
    if side is Side.SIDE_SELL:
        delta = -delta
    return specification.price_to_points(delta)


def _inside_stops_level(
    specification: SymbolSpecification, market: Price, proposed: Price, side: Side
) -> bool:
    required = specification.points_to_price(specification.stops_level)
    if side is Side.SIDE_BUY:
        return market.value - proposed.value < required
    return proposed.value - market.value < required


def _inside_freeze_level(
    specification: SymbolSpecification, market: Price, current_stop: Price, side: Side
) -> bool:
    required = specification.points_to_price(specification.freeze_level)
    if required <= 0:
        return False
    if side is Side.SIDE_BUY:
        return market.value - current_stop.value < required
    return current_stop.value - market.value < required
