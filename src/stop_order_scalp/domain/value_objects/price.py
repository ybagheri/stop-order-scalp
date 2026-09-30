"""Price, volume and money as immutable :class:`~decimal.Decimal` value objects.

Why this exists
---------------
The strategy specification is explicit that ``1 point`` must not be assumed to equal
``$1``, and that US30 contract specifications vary between brokers. That single
requirement dictates the shape of this module:

* Prices are :class:`~decimal.Decimal`, never ``float``. ``0.1 + 0.2 != 0.3`` is
  unacceptable when the difference is a stop loss.
* Every conversion between *points* and *price* goes through
  :class:`SymbolSpecification`, which is built from the broker's own reported values.
  There is no arithmetic shortcut anywhere in the codebase.
* Rounding is always *away from the trader*, never toward them. A stop loss is rounded
  away from entry so it is never closer than intended; a take profit is rounded toward
  the target for the same reason; a volume is always rounded **down** to the broker's
  volume step, so the position can never be larger than the risk engine approved.

Floats appear only at the MetaTrader 5 boundary, in ``execution.mt5_broker``, and are
converted to ``Decimal`` from their string form on the way in.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final, Self

from stop_order_scalp.domain.enums import Side
from stop_order_scalp.domain.exceptions import DomainError, InvalidSpecificationError

__all__ = [
    "DEFAULT_DIGITS",
    "Money",
    "Price",
    "SymbolSpecification",
    "Volume",
    "zero",
]

#: Number of decimal places used when a broker reports a symbol with no explicit
#: digit count. Never used to derive a price; only as a formatting fallback.
DEFAULT_DIGITS: Final[int] = 8

#: Canonical decimal zero. Constructed once so that identity and equality checks on
#: derived values never depend on how the zero was spelled.
_ZERO: Final[Decimal] = Decimal(0)


def zero(digits: int = DEFAULT_DIGITS) -> Decimal:
    """Return a decimal zero quantised to ``digits`` places."""
    return _ZERO.quantize(Decimal(1).scaleb(-digits))


def _to_decimal(value: Decimal | int | str, name: str) -> Decimal:
    """Coerce to ``Decimal`` without ever passing through ``float``."""
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, bool):
        # bool is an int subclass; accepting it here would hide a real bug.
        raise DomainError(f"{name} must be a Decimal, int or str, not bool")
    elif isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, str):
        try:
            candidate = Decimal(value)
        except InvalidOperation as exc:
            raise DomainError(f"{name} is not a valid decimal: {value!r}") from exc
    else:
        raise DomainError(f"{name} must be a Decimal, int or str, not {type(value).__name__}")

    if not candidate.is_finite():
        raise DomainError(f"{name} must be finite, got {candidate}")
    return candidate


@dataclass(frozen=True, slots=True, order=False)
class Price:
    """An absolute price for one symbol.

    Not a free-floating number: it carries the symbol's ``digits`` so that
    normalisation is deterministic, and it refuses non-finite values up front rather
    than letting ``NaN`` propagate into a stop loss.

    Comparison and arithmetic yield ``Decimal``, not ``Price``, because a bare
    ``Decimal`` result cannot know the digits of the symbol it came from. Call
    :meth:`at_digits` to come back to a ``Price`` before sending anything to a broker.
    """

    value: Decimal
    digits: int = DEFAULT_DIGITS

    def __post_init__(self) -> None:
        if self.digits < 0:
            raise DomainError(f"digits must be non-negative, got {self.digits}")
        object.__setattr__(self, "value", _to_decimal(self.value, "price"))

    # --- construction ---------------------------------------------------

    @classmethod
    def parse(cls, text: str, digits: int = DEFAULT_DIGITS) -> Self:
        """Parse from text. Strings are how the MetaTrader 5 boundary delivers prices."""
        return cls(_to_decimal(text, "price"), digits)

    @classmethod
    def of(cls, value: Decimal | int | str, digits: int = DEFAULT_DIGITS) -> Self:
        return cls(_to_decimal(value, "price"), digits)

    # --- comparison -----------------------------------------------------

    def __lt__(self, other: Self | Decimal | int) -> bool:
        return self.value < _coerce_operand(other)

    def __le__(self, other: Self | Decimal | int) -> bool:
        return self.value <= _coerce_operand(other)

    def __gt__(self, other: Self | Decimal | int) -> bool:
        return self.value > _coerce_operand(other)

    def __ge__(self, other: Self | Decimal | int) -> bool:
        return self.value >= _coerce_operand(other)

    # --- arithmetic -----------------------------------------------------

    def __add__(self, other: Self | Decimal | int) -> Decimal:
        return self.value + _coerce_operand(other)

    def __sub__(self, other: Self | Decimal | int) -> Decimal:
        return self.value - _coerce_operand(other)

    def __neg__(self) -> Decimal:
        return -self.value

    def distance_to(self, other: Self) -> Decimal:
        """Signed distance ``other - self``. Positive means ``other`` is above ``self``."""
        return other.value - self.value

    def absolute_distance_to(self, other: Self) -> Decimal:
        """Unsigned price distance. This is the quantity the risk engine needs."""
        return abs(other.value - self.value)

    # --- normalisation --------------------------------------------------

    def at_digits(self, digits: int) -> Price:
        """Re-quantise to ``digits`` places, rounding half away from zero."""
        return Price(self.value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP), digits)

    def __str__(self) -> str:
        return f"{self.value:.{self.digits}f}"


def _coerce_operand(other: Price | Decimal | int) -> Decimal:
    """Accept a :class:`Price`, ``Decimal`` or ``int`` wherever arithmetic happens."""
    if isinstance(other, Price):
        return other.value
    if isinstance(other, bool):
        raise DomainError("bool is not a valid price operand")
    if isinstance(other, (Decimal, int)):
        return _to_decimal(other, "operand")
    raise DomainError(f"cannot compare Price with {type(other).__name__}")


@dataclass(frozen=True, slots=True)
class Money:
    """An amount in account currency.

    Separate from :class:`Price` on purpose. Conflating them is how a bug that treats a
    price as a dollar amount becomes invisible in review. ``Money`` never gets compared
    to a ``Price``; the type system refuses.
    """

    amount: Decimal
    currency: str = "USD"

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", _to_decimal(self.amount, "money"))
        if not self.currency or not self.currency.isalpha():
            raise DomainError(f"currency must be alphabetic, got {self.currency!r}")

    @classmethod
    def of(cls, amount: Decimal | int | str, currency: str = "USD") -> Money:
        return cls(_to_decimal(amount, "money"), currency)

    @property
    def is_positive(self) -> bool:
        return self.amount > 0

    def __add__(self, other: Money) -> Money:
        self._require_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._require_same_currency(other)
        return Money(self.amount - other.amount, self.currency)

    def scaled(self, factor: Decimal) -> Money:
        return Money(self.amount * _to_decimal(factor, "factor"), self.currency)

    def rounded(self, places: int = 2) -> Money:
        return Money(self.amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), self.currency)

    def _require_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise DomainError(f"cannot combine {self.currency} with {other.currency}")

    def __str__(self) -> str:
        return f"{self.amount:.2f} {self.currency}"


@dataclass(frozen=True, slots=True)
class Volume:
    """An order or position size in lots.

    Carries no normalisation of its own. Normalisation is the broker's business and
    lives in :meth:`SymbolSpecification.normalize_volume`, because only the
    specification knows the volume step and the limits.
    """

    lots: Decimal

    def __post_init__(self) -> None:
        lots = _to_decimal(self.lots, "lots")
        if lots <= 0:
            raise DomainError(f"volume must be positive, got {lots}")
        object.__setattr__(self, "lots", lots)

    @classmethod
    def of(cls, lots: Decimal | int | str) -> Volume:
        return cls(_to_decimal(lots, "lots"))

    def __str__(self) -> str:
        return f"{self.lots:f}"


@dataclass(frozen=True, slots=True)
class SymbolSpecification:
    """The broker's own description of a tradable instrument.

    This is the *only* place points are converted to price, and it is built from
    ``symbol_info`` fields reported by MetaTrader 5. A specification is validated on
    construction: an instrument whose numbers cannot support the arithmetic is
    rejected at the boundary rather than producing a wrong order later.
    """

    #: Broker symbol name, e.g. ``US30``, ``US30.cash``, ``US30m``, ``DJ30``.
    name: str
    #: Decimal places in a price.
    digits: int
    #: Smallest price increment, e.g. ``0.01``. One "point" is this value.
    point: Decimal
    #: Price increment used for trading: the smallest legal price change.
    tick_size: Decimal
    #: Value of one :attr:`tick_size` move, in account currency, for one lot.
    tick_value: Decimal
    #: Units of the base instrument in one lot. 100 for US30-style indices.
    contract_size: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    #: Minimum distance in points between the current price and a stop level.
    stops_level: int = 0
    #: Distance in points within which a stop level is frozen from modification.
    freeze_level: int = 0
    #: Commission charged per lot per side, as the broker reports it.
    #: ``None`` means the broker reported no value; the configured assumption is used.
    commission_per_lot: Decimal | None = None
    #: Currency of :attr:`tick_value`.
    currency: str = "USD"

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise InvalidSpecificationError("symbol name must not be empty")
        object.__setattr__(self, "name", self.name.strip())

        if self.digits < 0:
            raise InvalidSpecificationError(f"{self.name}: digits must be non-negative")
        if self.point <= 0:
            raise InvalidSpecificationError(f"{self.name}: point must be positive, got {self.point}")
        if self.tick_size <= 0:
            raise InvalidSpecificationError(
                f"{self.name}: tick_size must be positive, got {self.tick_size}"
            )
        if self.tick_value < 0:
            raise InvalidSpecificationError(
                f"{self.name}: tick_value must be non-negative, got {self.tick_value}"
            )
        if self.contract_size <= 0:
            raise InvalidSpecificationError(
                f"{self.name}: contract_size must be positive, got {self.contract_size}"
            )
        if self.volume_min <= 0:
            raise InvalidSpecificationError(
                f"{self.name}: volume_min must be positive, got {self.volume_min}"
            )
        if self.volume_max < self.volume_min:
            raise InvalidSpecificationError(
                f"{self.name}: volume_max {self.volume_max} is below volume_min {self.volume_min}"
            )
        if self.volume_step <= 0:
            raise InvalidSpecificationError(
                f"{self.name}: volume_step must be positive, got {self.volume_step}"
            )
        if self.stops_level < 0:
            raise InvalidSpecificationError(f"{self.name}: stops_level must be non-negative")
        if self.freeze_level < 0:
            raise InvalidSpecificationError(f"{self.name}: freeze_level must be non-negative")

        if self.tick_size > self.point:
            # A tick *smaller* than a point is ordinary (sub-point quoting: point 0.01,
            # tick 0.001). A tick *larger* than a point means the broker's own fields
            # disagree, so a "100 point" stop distance could not be represented at all and
            # every point conversion from here on would be silently wrong.
            raise InvalidSpecificationError(
                f"{self.name}: tick_size {self.tick_size} is larger than point {self.point}; "
                "a point-denominated stop distance cannot be represented, so the "
                "specification is inconsistent"
            )

    # --- price normalisation --------------------------------------------

    @property
    def tick_decimal(self) -> Decimal:
        """How many decimal places a price must have to represent one tick exactly."""
        exponent = self.tick_size.normalize().as_tuple().exponent
        return Decimal(exponent) if isinstance(exponent, int) else Decimal(0)

    @property
    def tick_digits(self) -> int:
        """Decimal places required to express :attr:`tick_size` without loss."""
        return max(self.digits, int(-self.tick_decimal))

    def normalize_price(self, price: Price | Decimal | int) -> Price:
        """Snap a price onto the broker's tick grid and its digit count.

        Rounding is *away from* ``price`` in the conservative direction decided by
        :meth:`round_stop_price` / :meth:`round_target_price`; this method is the plain
        half-up snap used for entry prices, where the exact level is what the strategy
        specified.
        """
        raw = price.value if isinstance(price, Price) else _to_decimal(price, "price")
        return Price(raw.quantize(self.tick_size, rounding=ROUND_HALF_UP), self.digits)

    def round_stop_price(self, price: Price | Decimal | int, side: Side) -> Price:
        """Round a stop loss *away from entry*, so it is never tighter than intended.

        Direction matters, and getting it wrong inverts the sign of the protection:

        * a BUY stop sits **below** entry, so away from entry means rounding **down**
        * a SELL stop sits **above** entry, so away from entry means rounding **up**

        A stop rounded toward entry by one tick is a materially different trade from the
        one the risk engine approved, so this rounding is deliberately asymmetric.
        """
        raw = price.value if isinstance(price, Price) else _to_decimal(price, "price")
        rounding = ROUND_FLOOR if side is Side.SIDE_BUY else ROUND_CEILING
        return Price(raw.quantize(self.tick_size, rounding=rounding), self.digits)

    def round_target_price(self, price: Price | Decimal | int, side: Side) -> Price:
        """Round a take profit *away from entry*, so it is never easier to reach.

        The mirror image of :meth:`round_stop_price`: a BUY target is above entry and
        rounds up, a SELL target is below entry and rounds down.
        """
        raw = price.value if isinstance(price, Price) else _to_decimal(price, "price")
        rounding = ROUND_CEILING if side is Side.SIDE_BUY else ROUND_FLOOR
        return Price(raw.quantize(self.tick_size, rounding=rounding), self.digits)

    def is_on_tick_grid(self, price: Price | Decimal | int) -> bool:
        """Whether a price is exactly representable on the broker's tick grid."""
        raw = price.value if isinstance(price, Price) else _to_decimal(price, "price")
        return (raw / self.tick_size) == (raw / self.tick_size).to_integral_value()

    # --- points <-> price ------------------------------------------------

    def points_to_price(self, points: Decimal | int) -> Decimal:
        """Convert points to a *price distance*.

        A point is the broker's :attr:`point`, not a pip and not a price unit. For
        US30-style indices this is commonly ``0.1``, so ten points is one price unit --
        but that is a consequence of this broker's specification, not an assumption.
        """
        return _to_decimal(points, "points") * self.point

    def price_to_points(self, distance: Decimal | int) -> Decimal:
        """Convert a *price distance* to points.

        Returns a non-integral result when the distance is not a whole number of
        points. Callers that need whole points must decide explicitly what to do with
        the remainder; silently truncating is how a trailing distance quietly becomes a
        different number of points.
        """
        return _to_decimal(distance, "distance") / self.point

    def offset(self, price: Price, points: Decimal | int) -> Price:
        """Move ``price`` by a signed number of points and land on the tick grid."""
        return self.normalize_price(price.value + self.points_to_price(points))

    def distance_points(self, a: Price, b: Price) -> Decimal:
        """Signed distance from ``a`` to ``b``, expressed in points."""
        return self.price_to_points(b.value - a.value)

    # --- volume ----------------------------------------------------------

    def normalize_volume(self, lots: Decimal | int, *, rounding: str = ROUND_FLOOR) -> Volume:
        """Snap a lot count onto the broker's volume grid and clamp to its limits.

        ``rounding`` defaults to :data:`~decimal.ROUND_FLOOR`, which is the only safe
        default: rounding up would produce a position larger than the risk engine
        approved, which defeats the entire point of sizing from a percentage of
        balance. Callers that want a volume to be *at least* something must say so.
        """
        raw = _to_decimal(lots, "lots")
        steps = (raw / self.volume_step).to_integral_value(rounding=rounding)
        snapped = steps * self.volume_step
        if snapped < self.volume_min:
            raise InvalidSpecificationError(
                f"{self.name}: volume {raw} rounds to {snapped}, below the broker minimum "
                f"{self.volume_min}"
            )
        if snapped > self.volume_max:
            # Clamping downward is correct: the risk engine computed an upper bound and
            # the broker's ceiling is a second, tighter upper bound.
            snapped = self.volume_max - (self.volume_max % self.volume_step)
            if snapped < self.volume_min:
                raise InvalidSpecificationError(
                    f"{self.name}: volume_max {self.volume_max} is not on the volume step "
                    f"{self.volume_step}"
                )
        return Volume(snapped)

    def is_volume_on_step(self, lots: Decimal | int) -> bool:
        raw = _to_decimal(lots, "lots")
        return (raw / self.volume_step) == (raw / self.volume_step).to_integral_value()

    # --- monetary conversion ---------------------------------------------

    def tick_count_for(self, points: Decimal | int) -> Decimal:
        """How many ticks a distance in points spans.

        This is the bridge between the strategy's language (points) and the broker's
        (ticks), and the step that makes "1 point is not $1" structurally true.
        """
        return self.points_to_price(points) / self.tick_size

    def price_risk_for(self, entry: Price, stop: Price, volume: Volume | Decimal) -> Money:
        """Theoretical price risk of moving ``entry`` to ``stop`` at ``volume``.

        Commission is deliberately excluded. The specification requires the three
        figures to be distinguishable: theoretical price risk, estimated commission,
        and total estimated monetary risk. Use :mod:`stop_order_scalp.risk.commission`
        for the other two.
        """
        distance = entry.absolute_distance_to(stop)
        ticks = distance / self.tick_size
        lots = volume.lots if isinstance(volume, Volume) else _to_decimal(volume, "volume")
        return Money(ticks * self.tick_value * lots, self.currency)

    def __str__(self) -> str:
        return (
            f"SymbolSpecification({self.name}, digits={self.digits}, point={self.point}, "
            f"tick_size={self.tick_size}, tick_value={self.tick_value}, "
            f"contract_size={self.contract_size}, volume=[{self.volume_min}..{self.volume_max}]"
            f"/{self.volume_step}, stops_level={self.stops_level}, "
            f"freeze_level={self.freeze_level})"
        )
