"""When a position's stop moves to entry, and why it does not move on every tick.

Break-even is the first managed exit: once price has travelled far enough in favour, the
stop moves to the entry price so the trade can no longer lose. Three things make that
deceptively easy to get wrong, and each has a named refusal below.

**The trigger is measured on the exit side, not the mid.** A BUY's profit is realised by
selling at the **bid**, so the trigger compares ``bid - entry`` against the configured
points. Measuring on the mid, or on the ask, arms break-even while the position is still
underwater by the spread -- and a stop at entry is then hit immediately, converting a trade
that was merely flat into a certain loss. This is the spread-awareness requirement, and it
is arithmetic rather than a filter.

**Break-even is idempotent.** Once the stop is at entry, every subsequent tick proposes the
same price. Modifying on each of those would send an order per tick forever. A proposal
that does not change the stop is refused with :data:`BreakEvenRefusal.ALREADY_APPLIED`, so
the caller has nothing to do.

**The broker can refuse the modification.** ``stops_level`` requires the requested stop to
sit a minimum distance from the market, and ``freeze_level`` locks the position entirely when
price comes close to the existing stop. Both are checked here rather than discovered as a
retcode 10016 in production.

This module *proposes*; it never modifies. Sending is :mod:`stop_order_scalp.execution.position_manager`'s
job, which keeps the monotonicity and distance guarantees testable without a broker.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from stop_order_scalp.domain.enums import BreakEvenMode, Side
from stop_order_scalp.domain.models import ManagedStop, PositionRecord, Tick
from stop_order_scalp.domain.value_objects import Price, SymbolSpecification
from stop_order_scalp.infrastructure.config import BreakEvenSettings

__all__ = [
    "BreakEvenDecision",
    "BreakEvenProvider",
    "BreakEvenRefusal",
    "exit_price_for",
]


class BreakEvenRefusal:
    """Stable reasons break-even does not move a stop.

    An interface, like ``risk/risk_manager.RejectionCode``: a refusal that only says "not
    yet" forces every caller to parse prose, and an alert built on one of these must not
    silently change meaning.
    """

    DISABLED: str = "break_even_disabled"
    NOT_ARMED: str = "break_even_not_armed"
    ALREADY_APPLIED: str = "break_even_already_applied"
    NOT_MONOTONIC: str = "break_even_not_monotonic"
    VIOLATES_STOPS_LEVEL: str = "break_even_violates_stops_level"
    INSIDE_FREEZE_LEVEL: str = "break_even_inside_freeze_level"
    NO_STOP_IN_PLACE: str = "break_even_no_stop_in_place"


def exit_price_for(tick: Tick, side: Side) -> Price:
    """The price at which ``side`` would actually be closed.

    A BUY closes by selling at the bid, a SELL by buying at the ask. Everything about
    break-even is measured against this, because everything about break-even is about the
    profit a close would actually realise.
    """
    return tick.bid if side is Side.SIDE_BUY else tick.ask


@dataclass(frozen=True, slots=True)
class BreakEvenDecision:
    """Whether break-even proposes a new stop, and why not when it does not."""

    proposal: ManagedStop | None
    code: str = "ok"
    reason: str = ""
    #: How far price has travelled in favour, in points, measured on the exit side.
    favourable_points: Decimal = Decimal(0)
    #: Where the stop would go.
    target: Price | None = None

    @property
    def proposed(self) -> bool:
        return self.proposal is not None

    @property
    def armed(self) -> bool:
        """Whether the trigger has been reached, whatever else was refused."""
        return self.code not in (
            BreakEvenRefusal.DISABLED,
            BreakEvenRefusal.NOT_ARMED,
            BreakEvenRefusal.NO_STOP_IN_PLACE,
        )

    def require(self) -> ManagedStop:
        if self.proposal is None:
            raise ValueError(f"break-even proposed nothing: {self.code}: {self.reason}")
        return self.proposal

    def __str__(self) -> str:
        if self.proposal is not None:
            return f"BreakEvenDecision({self.proposal})"
        return f"BreakEvenDecision({self.code}: {self.reason})"


@runtime_checkable
class BreakEvenProvider(Protocol):
    """When a stop should move to entry."""

    def evaluate(
        self, position: PositionRecord, tick: Tick, specification: SymbolSpecification
    ) -> BreakEvenDecision: ...


@dataclass(frozen=True, slots=True)
class ConfiguredBreakEvenProvider:
    """Break-even driven by :class:`~stop_order_scalp.infrastructure.config.BreakEvenSettings`.

    Stateless. The trigger, mode and commission offset all arrive in configuration, so this
    is the only implementation the baseline needs; the protocol exists so a test or a
    future mode can substitute another.
    """

    settings: BreakEvenSettings

    def evaluate(
        self, position: PositionRecord, tick: Tick, specification: SymbolSpecification
    ) -> BreakEvenDecision:
        side = position.side
        entry = position.entry
        exit_price = exit_price_for(tick, side)
        favourable = _favourable_points(specification, exit_price, entry, side)

        if not self.settings.enabled:
            return BreakEvenDecision(
                None,
                BreakEvenRefusal.DISABLED,
                "break-even is disabled in configuration",
                favourable,
            )

        if position.stop_loss is None:
            return BreakEvenDecision(
                None,
                BreakEvenRefusal.NO_STOP_IN_PLACE,
                "the position carries no stop loss, so there is nothing to move to entry",
                favourable,
            )

        if favourable < Decimal(self.settings.trigger_points):
            return BreakEvenDecision(
                None,
                BreakEvenRefusal.NOT_ARMED,
                f"price has moved {favourable} points in favour, "
                f"{self.settings.trigger_points} required",
                favourable,
            )

        target = self._target_price(specification, entry, side)
        current = position.stop_loss

        # Idempotency. Checked before the broker's limits, because a stop already at entry
        # is not going to be moved by a distance check either.
        if current >= target if side is Side.SIDE_BUY else current <= target:
            return BreakEvenDecision(
                None,
                BreakEvenRefusal.ALREADY_APPLIED,
                f"the stop is already at or beyond entry ({current})",
                favourable,
                target,
            )

        frozen = _inside_freeze_level(specification, exit_price, current, side)
        if frozen:
            return BreakEvenDecision(
                None,
                BreakEvenRefusal.INSIDE_FREEZE_LEVEL,
                f"price is within the broker's freeze level of {current}; the position "
                "cannot be modified",
                favourable,
                target,
            )

        too_close = _inside_stops_level(specification, exit_price, target, side)
        if too_close:
            return BreakEvenDecision(
                None,
                BreakEvenRefusal.VIOLATES_STOPS_LEVEL,
                f"a stop at {target} is closer to the market than the broker's "
                f"stops_level; it would be rejected",
                favourable,
                target,
            )

        proposal = ManagedStop(
            position_ticket=position.ticket,
            side=side,
            current_stop=current,
            proposed_stop=target,
            reason=f"break_even_{favourable}pts",
            distance_points=favourable,
        )
        if not proposal.is_monotonic():
            # Unreachable given the ALREADY_APPLIED check above, and kept as a guard
            # because the invariant is the thing that must never be violated.
            return BreakEvenDecision(
                None,
                BreakEvenRefusal.NOT_MONOTONIC,
                f"moving to {target} from {current} would move a {side} stop backwards",
                favourable,
                target,
            )
        return BreakEvenDecision(proposal, "ok", "", favourable, target)

    def _target_price(
        self, specification: SymbolSpecification, entry: Price, side: Side
    ) -> Price:
        """Where the stop goes, rounded onto the tick grid.

        ``COMMISSION_AWARE`` pushes the stop past entry by the estimated round-trip cost, so
        a close at break-even is not a net loss. For a BUY that means *above* entry, which
        is also the monotonic direction; for a SELL, below.
        """
        offset = Decimal(self.settings.commission_points)
        if self.settings.mode is BreakEvenMode.BREAK_EVEN_MODE_COMMISSION_AWARE and offset:
            offset = offset if side is Side.SIDE_BUY else -offset
        else:
            offset = Decimal(0)
        return specification.round_stop_price(
            entry.value + specification.points_to_price(offset), side
        )

    def __str__(self) -> str:
        return (
            f"ConfiguredBreakEvenProvider(enabled={self.settings.enabled}, "
            f"trigger={self.settings.trigger_points})"
        )


def _favourable_points(
    specification: SymbolSpecification, exit_price: Price, entry: Price, side: Side
) -> Decimal:
    """Points in favour, measured on the price a close would actually get."""
    delta = exit_price.value - entry.value
    if side is Side.SIDE_SELL:
        delta = -delta
    return specification.price_to_points(delta)


def _inside_stops_level(
    specification: SymbolSpecification, market: Price, proposed: Price, side: Side
) -> bool:
    """Whether a stop at ``proposed`` is too close to the market for the broker to accept.

    The distance is measured on the side the stop protects: a BUY's stop is below the
    market, so ``bid - stop``; a SELL's is above, so ``stop - ask``.

    Plain ``bool``, and the callers test it with ``if``. An earlier version returned
    ``bool | None`` and tested ``is not None``, which refused *every* modification -- the
    false case and the "not applicable" case were the same value. The bug was silent: every
    outcome still carried a sensible-looking reason.
    """
    required = specification.points_to_price(specification.stops_level)
    if side is Side.SIDE_BUY:
        return market.value - proposed.value < required
    return proposed.value - market.value < required


def _inside_freeze_level(
    specification: SymbolSpecification, market: Price, current_stop: Price, side: Side
    ) -> bool:
    """Whether the position is frozen and cannot be modified at all.

    Separate from ``stops_level``: ``stops_level`` constrains where a stop may be *placed*,
    while ``freeze_level`` locks an *existing* position once price comes close to it. A
    request that satisfies the first can still be refused by the second.
    """
    required = specification.points_to_price(specification.freeze_level)
    if required <= 0:
        return False
    if side is Side.SIDE_BUY:
        return market.value - current_stop.value < required
    return current_stop.value - market.value < required
