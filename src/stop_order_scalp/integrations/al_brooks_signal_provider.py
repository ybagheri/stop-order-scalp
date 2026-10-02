"""Where the optional engine meets the baseline strategy, and where it does not.

The rule this module exists to enforce: **the M15/M1 baseline is authoritative unless an
operator has explicitly enabled the integration.** Not "unless enabled and it has an opinion" —
*unless enabled*. With the default configuration the engine is never consulted, which is why a
project with no `albrooks` installed behaves exactly as it did before this phase.

Three positions, and they are not the same decision
--------------------------------------------------

.. code-block:: text

    enabled=False                       the baseline decides. Always.
    enabled=True,  allow_geometry=False  the engine decides the *direction*.
                                        Geometry still comes from the M1-extremum rule.
    enabled=True,  allow_geometry=True   the engine's entry/stop/target may be used as the
                                        order's geometry.

The two switches are independent on purpose. The common case is wanting the engine's read on
direction while keeping this project's own levels — taking both at once would make it
impossible to tell afterwards which rule set the entry, and a level with an untraceable origin
is a level nobody can debug.

When enabled, the engine **vetoes as well as authorises**
--------------------------------------------------------

An engine ``WAIT`` becomes a :class:`NoTrade`, not a fallback to the baseline. That is the
conservative direction, and it is the only one consistent with the engine's own contract: it
reports ``"is_recommendation": False``, so treating a decline as "ask someone else" would be
reading a decision as a recommendation it explicitly disclaims.

The consequence is worth stating plainly, because it surprises people: **enabling this
integration can reduce the number of trades.** That is the point of a filter. A reviewer
should read ``enabled: true`` as "the engine now has a veto", not as "the engine adds trades".

What this module cannot do
--------------------------

It cannot reach a broker, size a position or place an order. It produces a
:class:`~stop_order_scalp.strategy.signal.Decision` and nothing else, which is why enabling it
does not move the invariant that every send goes through ``place_order``'s ordering. The
adapter cannot even name ``albrooks``; that is
:mod:`stop_order_scalp.integrations.al_brooks_adapter`'s job alone.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from stop_order_scalp.domain.exceptions import ComponentNotAvailableError
from stop_order_scalp.domain.models import Candle
from stop_order_scalp.domain.value_objects import SymbolSpecification
from stop_order_scalp.infrastructure.config import AlBrooksSettings
from stop_order_scalp.integrations.al_brooks_adapter import (
    AdaptedDecision,
    AlBrooksAdapter,
    AlBrooksRefusal,
)
from stop_order_scalp.strategy.signal import NoTrade, NoTradeReason

__all__ = ["AlBrooksSignalProvider", "ProviderOutcome", "baseline_decider"]


class Source:
    """Which rule produced the decision. Stable strings, for the journal."""

    BASELINE: str = "baseline"
    AL_BROOKS: str = "al_brooks"
    #: The integration is enabled but the engine declined, and that is honoured.
    AL_BROOKS_VETO: str = "al_brooks_veto"


@dataclass(frozen=True, slots=True)
class ProviderOutcome:
    """The decision, and which rule produced it.

    ``source`` is not decoration. A journal entry that records only "no trade" cannot later
    distinguish a quiet strategy from one whose optional filter is vetoing everything, and
    that distinction is the first thing an operator needs when trades stop appearing.
    """

    decision: Any
    source: str
    #: The engine's verbatim action, when it was consulted. ``""`` when it was not.
    action: str = ""
    #: The adapter's refusal code, when the engine was consulted and declined.
    code: str = ""

    @property
    def is_signal(self) -> bool:
        return hasattr(self.decision, "signal")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source,
            "action": self.action,
            "code": self.code,
        }
        to_dict = getattr(self.decision, "to_dict", None)
        if callable(to_dict):
            payload["decision"] = to_dict()
        return payload

    def __str__(self) -> str:
        return f"ProviderOutcome({self.source}, {self.decision})"


#: A callable producing the baseline decision, so the provider does not import the strategy
#: layer's class and a test can substitute one. Type-aliased rather than a Protocol because a
#: single-method callable is already the protocol.
baseline_decider = Callable[..., Any]


class AlBrooksSignalProvider:
    """Chooses between the baseline strategy and the optional engine.

    Holds the engine behind a callable, so nothing here imports ``albrooks`` and a test can
    drive the whole provider with a fake. Constructing it does **not** require the extra: with
    the integration disabled -- the default -- the engine is never constructed and the class
    works with no third-party package installed at all.
    """

    __slots__ = ("_adapter", "_baseline", "_engine", "_engine_factory", "_settings")

    def __init__(
        self,
        settings: AlBrooksSettings,
        baseline: baseline_decider,
        *,
        adapter: AlBrooksAdapter | None = None,
        engine_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._baseline = baseline
        self._adapter = adapter
        #: Builds the engine on demand. Deferred so the optional import happens only if
        #: something actually asks for it, and only once.
        self._engine_factory = engine_factory
        #: Populated on first use by :meth:`_require_engine`. In ``__slots__`` and not a class
        #: attribute, because a class-level default would be shared by every instance.
        self._engine: Any = None

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    @property
    def allow_geometry(self) -> bool:
        return self._settings.allow_geometry

    def evaluate(
        self,
        *,
        symbol: str,
        candles: Sequence[Candle],
        reference: datetime,
        specification: SymbolSpecification,
        direction_candle_open_time: datetime | None = None,
    ) -> ProviderOutcome:
        """The decision for this tick, from whichever rule is authoritative.

        The baseline is always evaluated first, even when the engine is enabled. It costs
        nothing, and it means the outcome is available for comparison -- so an operator who
        enables the integration can see what the engine vetoed rather than discovering it by
        counting orders.
        """
        baseline = self._baseline(symbol=symbol, reference=reference)

        if not self._settings.enabled:
            # The default path. The engine is not constructed, not imported, not consulted.
            return ProviderOutcome(baseline, Source.BASELINE)

        adapted = self._consult(
            symbol=symbol,
            candles=candles,
            reference=reference,
            specification=specification,
            direction_candle_open_time=direction_candle_open_time,
        )
        if adapted.is_signal:
            return ProviderOutcome(adapted.require_signal(), Source.AL_BROOKS, adapted.action)
        return ProviderOutcome(
            _as_no_trade(adapted, reference=reference, symbol=symbol),
            Source.AL_BROOKS_VETO,
            adapted.action,
            adapted.code,
        )

    # --- internals -------------------------------------------------------

    def _consult(
        self,
        *,
        symbol: str,
        candles: Sequence[Candle],
        reference: datetime,
        specification: SymbolSpecification,
        direction_candle_open_time: datetime | None,
    ) -> AdaptedDecision:
        """Get one engine decision and translate it. Never raises for a declining engine."""
        adapter = self._require_adapter(symbol, specification)
        engine = self._require_engine()
        decision = engine.decide(
            bars=[_to_bar(candle) for candle in candles],
            symbol=symbol,
            now=reference.timestamp(),
        )
        return adapter.adapt(
            decision,
            source_candle_open_time=(
                direction_candle_open_time
                if direction_candle_open_time is not None
                # The newest closed candle is the honest fallback: it is the input the
                # baseline would have used, and inventing a timestamp would put a time in
                # the journal that no candle carried.
                else (candles[-1].open_time if candles else reference)
            ),
        )

    def _require_adapter(self, symbol: str, specification: Any) -> AlBrooksAdapter:
        """The adapter for this symbol, built once with the broker's own precision.

        Digits come from the specification rather than a default, because a price rendered at
        the wrong scale is rejected by the terminal as an invalid price -- and a mis-scaled
        level from a third-party engine is exactly the kind of bug that only appears live.
        """
        if self._adapter is None:
            self._adapter = AlBrooksAdapter(
                self._settings, symbol, digits=int(getattr(specification, "digits", 1))
            )
        return self._adapter

    def _require_engine(self) -> Any:
        """Build the engine once, on first use.

        :raises ComponentNotAvailableError: when the optional extra is missing. Reached only
            with the integration *enabled*, so the default path never needs the package.
        """
        if self._engine is None:
            if self._engine_factory is None:
                self._engine = _default_engine()
            else:
                self._engine = self._engine_factory()
        return self._engine

    def describe(self) -> dict[str, Any]:
        """A JSON-friendly description, for ``status``.

        States the rule in full: which source is authoritative and whether geometry follows
        it. A switch that is off and unexplained is a switch nobody dares turn on.
        """
        return {
            "enabled": self.enabled,
            "allow_geometry": self.allow_geometry,
            "authoritative": (
                "al_brooks" if self.enabled else "m15_m1_baseline"
            ),
            "engine_vetoes": self.enabled,
            "geometry_source": "al_brooks" if self.allow_geometry else "m15_m1_baseline",
        }

    def __repr__(self) -> str:
        return (
            f"AlBrooksSignalProvider(enabled={self.enabled}, "
            f"allow_geometry={self.allow_geometry})"
        )


def _as_no_trade(adapted: AdaptedDecision, *, reference: datetime, symbol: str) -> NoTrade:
    """The adapter's refusal as a :class:`NoTrade`.

    Folds the engine's reason into this project's vocabulary so a consumer that only knows
    :class:`NoTrade` handles it without learning that a third-party engine exists.
    """
    return NoTrade(
        reason=NoTradeReason.AL_BROOKS_VETO
        if adapted.code != AlBrooksRefusal.DISABLED
        else adapted.code,
        detail=(
            f"the Al Brooks engine returned {adapted.action!r}: {adapted.reason}"
            if adapted.reason
            else f"the Al Brooks engine returned {adapted.action!r}"
        ),
        reference=reference,
        symbol=symbol,
    )


def _to_bar(candle: Candle) -> Any:
    """A candle as the engine's ``Bar``.

    Built structurally rather than imported, so this module has no dependency on the extra
    even at the type level. The engine's ``Bar`` is a plain dataclass with these six fields
    and epoch-second ``time``; a duck-typed instance satisfies it.
    """
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Bar:
        time: float
        open: float
        high: float
        low: float
        close: float
        volume: int = 0
        index: int = 0

    return Bar(
        time=float(candle.open_time.timestamp()),
        open=float(candle.open.value),
        high=float(candle.high.value),
        low=float(candle.low.value),
        close=float(candle.close.value),
        volume=int(candle.tick_volume),
    )


def _default_engine() -> Any:
    """Build the real engine, or explain how to get it."""
    from stop_order_scalp.integrations.al_brooks_adapter import import_engine

    engine = import_engine()
    factory = getattr(engine, "DecisionEngine", None) or getattr(engine, "Engine", None)
    if factory is None:
        raise ComponentNotAvailableError(
            "the installed 'albrooks' package exposes neither DecisionEngine nor Engine. "
            "This project pins its expectations of that package in "
            "docs/integrations/AL_BROOKS.md; a version mismatch is the likely cause."
        )
    return factory()
