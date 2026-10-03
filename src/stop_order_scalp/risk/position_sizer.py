"""How much to trade.

Two modes, and one rule that matters more than either of them: **the risk budget is
compared against ``total_risk`` -- price risk *plus* commission -- and the size rounds
down.**

============  ================================================================
``percent_balance``  a fraction of account *balance*, the baseline
``fixed_lot``        a fixed lot count
============  ================================================================

Sizing is ``budget / cost_per_lot``, where ``cost_per_lot`` is derived from the broker's
own numbers::

    ticks_per_lot  = (entry - stop) / tick_size
    price_per_lot  = ticks_per_lot * tick_value
    total_per_lot  = price_per_lot + commission_per_lot
    volume         = budget / total_per_lot

That is the whole reason ``SymbolSpecification`` exists. On the *measured* Alpari ``US30``
(point ``0.1``, tick ``0.1``, tick value ``0.1``/lot) a 100-point stop costs **$10 per lot**.
(Before the measurement the tick value was assumed to be 1.0, which made it $100.) An
implementation that multiplied the point count by a price would get this wrong by a factor of
100, and would not raise anything.

**Balance, never equity.** Sizing from equity would silently increase risk after a losing
streak -- exactly when risk should not grow.

**Rounding is always down.** A size rounded up is larger than the engine approved, which
defeats the point of a percentage of balance. And with ``refuse_below_min_volume``, a size
that floors below the broker's minimum is **refused**, not rounded up to it: on a small
account the honest answer is "cannot trade this", because the smallest tradable position
would breach the budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from stop_order_scalp.domain.enums import RiskMode
from stop_order_scalp.domain.exceptions import RiskError
from stop_order_scalp.domain.value_objects import Money, Price, SymbolSpecification, Volume
from stop_order_scalp.risk.commission import CommissionModel_

__all__ = [
    "SizingInput",
    "SizingResult",
    "cost_per_lot",
    "size_position",
]


@dataclass(frozen=True, slots=True)
class SizingInput:
    """Everything the sizer needs. Grouped so a field cannot be added by accident."""

    entry: Price
    stop: Price
    #: Account **balance**, not equity.
    balance: Money
    specification: SymbolSpecification
    mode: RiskMode = RiskMode.RISK_MODE_PERCENT_BALANCE
    percent: Decimal = Decimal("0.5")
    fixed_lot: Decimal = Decimal("0.10")
    commission: CommissionModel_ | None = None
    refuse_below_min_volume: bool = True


@dataclass(frozen=True, slots=True)
class SizingResult:
    """The chosen size and the arithmetic that produced it.

    Every intermediate figure is carried, not just the answer. "Why 0.4 lots" is a question
    that gets asked the first time something looks wrong, and reconstructing it from a log
    line is guesswork.
    """

    volume: Volume
    #: Budget in money. For ``percent_balance`` this is ``balance * percent / 100``.
    budget: Money
    #: What one lot would cost in price risk.
    price_risk_per_lot: Money
    #: What one lot would cost in commission, round trip.
    commission_per_lot: Money
    #: ``price_risk_per_lot + commission_per_lot`` -- the divisor.
    total_per_lot: Money
    #: The size before snapping to the broker's volume grid.
    raw_volume: Decimal
    #: What ``raw_volume / volume_step`` floored to. Below 1.0 means the budget could not
    #: support even the smallest tradable position.
    steps: Decimal
    #: What the budget supports as a fraction of balance, for reporting.
    risk_fraction: Decimal
    refused_below_minimum: bool = False

    @property
    def price_risk(self) -> Money:
        """Price risk at the chosen volume, commission excluded."""
        return self.price_risk_per_lot.scaled(self.volume.lots)

    @property
    def commission(self) -> Money:
        """Commission at the chosen volume."""
        return self.commission_per_lot.scaled(self.volume.lots)

    @property
    def total_risk(self) -> Money:
        """``price_risk + commission`` -- the figure the budget was compared against."""
        return self.total_per_lot.scaled(self.volume.lots)

    def as_dict(self) -> dict[str, object]:
        return {
            "volume": str(self.volume.lots),
            "budget": str(self.budget.amount),
            "price_risk_per_lot": str(self.price_risk_per_lot.amount),
            "commission_per_lot": str(self.commission_per_lot.amount),
            "total_per_lot": str(self.total_per_lot.amount),
            "raw_volume": str(self.raw_volume),
            "steps": str(self.steps),
            "risk_fraction": str(self.risk_fraction),
            "price_risk": str(self.price_risk.amount),
            "commission": str(self.commission.amount),
            "total_risk": str(self.total_risk.amount),
            "refused_below_minimum": self.refused_below_minimum,
        }

    def __str__(self) -> str:
        return (
            f"SizingResult({self.volume.lots} lots, budget {self.budget.amount}, "
            f"total risk {self.total_risk.amount}, fraction {self.risk_fraction}%)"
        )


def cost_per_lot(
    entry: Price,
    stop: Price,
    specification: SymbolSpecification,
    commission: CommissionModel_ | None = None,
) -> tuple[Money, Money, Money]:
    """``(price_risk, commission, total)`` for one lot.

    The price half is delegated to
    :meth:`SymbolSpecification.price_risk_for`, so the tick arithmetic lives in exactly one
    place. Nothing here multiplies a point count by a price.
    """
    one_lot = Decimal(1)
    price_risk = specification.price_risk_for(entry, stop, one_lot)
    fee = (
        commission.for_volume(one_lot)
        if commission is not None
        else Money(Decimal(0), price_risk.currency)
    )
    total = price_risk + fee
    return price_risk, fee, total


def _require_distinct(entry: Price, stop: Price) -> None:
    if entry == stop:
        raise RiskError(
            "entry and stop are the same price, so the position has no risk and no size "
            "could be derived from it"
        )


def _budget_for(data: SizingInput) -> Money:
    if data.balance.amount <= 0:
        raise RiskError(
            f"account balance is {data.balance.amount}, which cannot size a position"
        )
    if data.mode is RiskMode.RISK_MODE_FIXED_LOT:
        # A fixed-lot request expresses no budget; report the risk it implies instead, so
        # max_total_risk_fraction can still be checked against a real figure.
        return Money(Decimal(0), data.balance.currency)
    percent = data.percent
    if percent <= 0:
        raise RiskError(f"risk.percent must be positive, got {percent}")
    # ``scaled`` rather than ``*``: Money deliberately has no ``__mul__``, because a money
    # amount multiplied by a bare Decimal is exactly the kind of expression that ends up
    # meaning "dollars per point" somewhere downstream.
    return data.balance.scaled(percent / Decimal(100)).rounded()


def size_position(data: SizingInput) -> SizingResult:
    """Choose a volume.

    :raises RiskError: when the budget cannot support even the broker's minimum volume and
        ``refuse_below_min_volume`` is set. This is a refusal, not a clamp: rounding up to
        ``volume_min`` would produce a position larger than the budget, which is the one
        thing a risk limit exists to prevent.
    """
    _require_distinct(data.entry, data.stop)
    price_lot, commission_lot, total_lot = cost_per_lot(
        data.entry, data.stop, data.specification, data.commission
    )
    if total_lot.amount <= 0:
        raise RiskError(
            f"one lot costs nothing to lose on {data.specification.name}; "
            "check tick_value and the commission configuration"
        )

    if data.mode is RiskMode.RISK_MODE_FIXED_LOT:
        return _size_fixed(data, price_lot, commission_lot, total_lot)
    return _size_from_budget(data, price_lot, commission_lot, total_lot)


def _size_fixed(
    data: SizingInput,
    price_lot: Money,
    commission_lot: Money,
    total_lot: Money,
) -> SizingResult:
    requested = data.fixed_lot
    if requested <= 0:
        raise RiskError(f"risk.fixed_lot must be positive, got {requested}")
    volume = data.specification.normalize_volume(requested, rounding=ROUND_FLOOR)
    balance = data.balance.amount
    return SizingResult(
        volume=volume,
        budget=Money(Decimal(0), data.balance.currency),
        price_risk_per_lot=price_lot,
        commission_per_lot=commission_lot,
        total_per_lot=total_lot,
        raw_volume=requested,
        steps=(requested / data.specification.volume_step).to_integral_value(rounding=ROUND_FLOOR),
        risk_fraction=_fraction(total_lot.scaled(volume.lots).amount, balance),
    )


def _size_from_budget(
    data: SizingInput,
    price_lot: Money,
    commission_lot: Money,
    total_lot: Money,
) -> SizingResult:
    budget = _budget_for(data)
    raw = budget.amount / total_lot.amount
    steps = (raw / data.specification.volume_step).to_integral_value(rounding=ROUND_FLOOR)

    if steps < 1:
        if data.refuse_below_min_volume:
            raise RiskError(
                f"the {data.balance.amount} {budget.currency} budget supports "
                f"{_trim(raw)} lots, which is below the broker minimum "
                f"{data.specification.volume_min}; refusing rather than rounding up, "
                "because rounding up would exceed the budget"
            )
        volume = Volume.of(data.specification.volume_min)
        return _result(
            data, volume, budget, price_lot, commission_lot, total_lot, raw, steps,
            refused=True,
        )

    volume = Volume.of(steps * data.specification.volume_step)
    if volume.lots > data.specification.volume_max:
        volume = data.specification.normalize_volume(volume.lots)
    return _result(data, volume, budget, price_lot, commission_lot, total_lot, raw, steps)


def _result(
    data: SizingInput,
    volume: Volume,
    budget: Money,
    price_lot: Money,
    commission_lot: Money,
    total_lot: Money,
    raw: Decimal,
    steps: Decimal,
    *,
    refused: bool = False,
) -> SizingResult:
    return SizingResult(
        volume=volume,
        budget=budget,
        price_risk_per_lot=price_lot,
        commission_per_lot=commission_lot,
        total_per_lot=total_lot,
        raw_volume=raw,
        steps=steps,
        risk_fraction=_fraction(total_lot.scaled(volume.lots).amount, data.balance.amount),
        refused_below_minimum=refused,
    )


def _fraction(risk: Decimal, balance: Decimal) -> Decimal:
    if balance <= 0:
        return Decimal(0)
    return (risk / balance * Decimal(100)).quantize(Decimal("0.0001"))


def _trim(value: Decimal) -> str:
    """A short decimal for a message, without a trail of zeros."""
    return f"{value:.4f}"
