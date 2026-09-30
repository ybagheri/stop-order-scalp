"""The strategy: one object that answers "should I place an order right now, and where?"

This is the module the rest of the system talks to. It composes the three lower layers and
holds the configuration, so that a caller needs one object rather than a settings dict, a
specification and a policy:

* :mod:`~stop_order_scalp.strategy.candle_direction` — which M15 candle decides, and what
* :mod:`~stop_order_scalp.strategy.entry_rules` — where the pending stop goes
* :mod:`~stop_order_scalp.strategy.signal` — the decision, as a value

It owns no rules of its own. That is a deliberate constraint: the composition root should
be boring, because every rule it added would be a rule with no dedicated tests.

It is a **stateless** function-style façade over frozen settings. Holding mutable state
here would make "what did the strategy believe at time *t*?" unanswerable, which is exactly
the question the no-look-ahead tests ask.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from stop_order_scalp.domain.enums import TimeframeSelection
from stop_order_scalp.domain.exceptions import ConfigError
from stop_order_scalp.domain.models import Candle, InstrumentPolicy
from stop_order_scalp.domain.value_objects import SymbolSpecification
from stop_order_scalp.infrastructure.config import EntrySettings, StrategySettings
from stop_order_scalp.strategy.candle_direction import (
    DirectionDecision,
    decide_direction,
    select_direction_candle,
)
from stop_order_scalp.strategy.entry_rules import EntryLevel
from stop_order_scalp.strategy.signal import Decision, evaluate

__all__ = [
    "StopOrderStrategy",
    "StrategyContext",
    "entry_geometry",
]


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Everything the strategy needs for one decision.

    Grouped so that the strategy's inputs are visible in one signature and cannot grow by
    accident. A caller that finds itself wanting to add a field here is usually about to
    introduce state the decision did not need.
    """

    symbol: str
    m15_candles: Sequence[Candle]
    m1_candles: Sequence[Candle]
    #: Broker server time. The only clock the strategy is allowed to consult.
    reference: datetime
    specification: SymbolSpecification
    policy: InstrumentPolicy | None = None


class StopOrderStrategy:
    """The M15-direction / M1-stop-entry baseline.

    Frozen at construction: the settings and the specification are captured once, so a
    decision cannot be taken against a configuration that changed halfway through.
    """

    __slots__ = ("_entry", "_policy", "_specification", "_symbol")

    def __init__(
        self,
        settings: StrategySettings,
        specification: SymbolSpecification,
        policy: InstrumentPolicy | None = None,
    ) -> None:
        entry = settings.entry
        _require_known_timeframes(entry)
        _require_permitted_instrument(settings, specification)
        self._entry = entry
        self._specification = specification
        self._symbol = settings.symbol
        self._policy = policy

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def entry_settings(self) -> EntrySettings:
        return self._entry

    @property
    def specification(self) -> SymbolSpecification:
        return self._specification

    @property
    def policy(self) -> InstrumentPolicy | None:
        return self._policy

    @property
    def uses_closed_candles(self) -> bool:
        """Whether the baseline rule is in force.

        ``True`` is the only correct value for the baseline. The property exists so that a
        caller, a log line or a test can state which rule is running rather than assuming
        it, and so that flipping it in configuration is a visible act.
        """
        return self._entry.candle_selection is TimeframeSelection.LAST_CLOSED

    def evaluate(self, context: StrategyContext) -> Decision:
        """The decision, as a :class:`TradeSignal` or a :class:`NoTrade`.

        Pure. The same context always produces the same decision, which is what the
        no-look-ahead property test relies on.
        """
        return evaluate(
            symbol=context.symbol,
            m15_candles=context.m15_candles,
            m1_candles=context.m1_candles,
            reference=context.reference,
            entry=self._entry,
            specification=self._specification,
            policy=context.policy if context.policy is not None else self._policy,
        )

    def direction(self, context: StrategyContext) -> DirectionDecision:
        """Just the direction verdict, without the entry geometry."""
        return decide_direction(
            context.m15_candles,
            reference=context.reference,
            timeframe=self._entry.direction_timeframe,
        )

    def direction_candle(self, context: StrategyContext) -> Any:
        """The M15 candle the direction would be read from, or ``None``."""
        return select_direction_candle(
            context.m15_candles,
            reference=context.reference,
            timeframe=self._entry.direction_timeframe,
        )

    def describe(self) -> dict[str, Any]:
        """A JSON-friendly description of the running rule, for ``status`` and the log.

        Stating the rule in full is what makes a journal entry months later interpretable:
        "US30, M15 direction, M1 entry, 10 points, last closed" is a complete statement of
        the strategy, and nothing shorter is.
        """
        return {
            "symbol": self._symbol,
            "direction_timeframe": self._entry.direction_timeframe,
            "entry_timeframe": self._entry.timeframe,
            "offset_points": self._entry.offset_points,
            "candle_selection": str(self._entry.candle_selection),
            "uses_closed_candles": self.uses_closed_candles,
            "lookback": self._entry.lookback,
            "specification": {
                "name": self._specification.name,
                "point": str(self._specification.point),
                "tick_size": str(self._specification.tick_size),
                "tick_value": str(self._specification.tick_value),
                "stops_level": self._specification.stops_level,
                "volume_min": str(self._specification.volume_min),
                "volume_step": str(self._specification.volume_step),
            },
            "policy": None if self._policy is None else str(self._policy),
        }

    def __repr__(self) -> str:
        return (
            f"StopOrderStrategy({self._symbol}, {self._entry.direction_timeframe}->"
            f"{self._entry.timeframe}, {self._entry.offset_points}pt, "
            f"closed={self.uses_closed_candles})"
        )


def _require_permitted_instrument(
    settings: StrategySettings, specification: SymbolSpecification
) -> None:
    """Refuse a specification for an instrument this strategy is not permitted to trade.

    The check is against the logical symbol *and every configured alias*, not the logical
    symbol alone. The broker's own name for a permitted instrument is frequently a variant
    -- ``US30.cash``, ``US30m``, ``DJ30`` -- and requiring an exact match would make the
    strategy impossible to construct on a broker that names it differently, which is the
    common case rather than the exotic one.

    What it still catches is the mistake that matters: a specification for a *different*
    instrument, whose ``point`` and ``tick_value`` would make every price and money
    conversion wrong. That has to be refused at construction, where the message can name
    both sides, rather than discovered later in a decision.
    """
    permitted = {name.strip().upper() for name in (settings.symbol, *settings.symbol_aliases)}
    actual = specification.name.strip().upper()
    if actual not in permitted:
        raise ConfigError(
            f"the specification is for {specification.name!r}, which is not a permitted "
            f"name for {settings.symbol!r}; permitted names are {sorted(permitted)}. "
            "A specification for a different instrument makes every point and money "
            "conversion wrong, so it is refused here rather than used."
        )


def _require_known_timeframes(entry: EntrySettings) -> None:
    """Refuse a timeframe this project cannot compute boundaries for.

    ``MN1`` is the interesting case: a month has no fixed number of seconds, and
    ``period_seconds`` raises for it. Catching that here means the error names the
    configuration key, rather than surfacing as a stack trace from deep inside the freeze.
    """
    from stop_order_scalp.market_data.timeframes import period_seconds

    for label, name in (
        ("entry.timeframe", entry.timeframe),
        ("entry.direction_timeframe", entry.direction_timeframe),
    ):
        try:
            period_seconds(name)
        except Exception as exc:
            raise ConfigError(f"{label}: {exc}") from exc


def entry_geometry(level: EntryLevel) -> dict[str, Any]:
    """A journal-friendly view of an entry level."""
    return {
        "side": str(level.side),
        "order_kind": str(level.order_kind),
        "price": str(level.price),
        "anchor": str(level.anchor),
        "offset_points": level.offset_points,
        "offset_distance": str(level.offset_distance),
        "rounded": level.rounded,
    }
