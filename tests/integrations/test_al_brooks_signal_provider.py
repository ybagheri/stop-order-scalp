"""The provider decides which rule is authoritative, and the default is the baseline.

Two properties carry the phase:

* with the default configuration the engine is **never consulted** -- so a project with no
  ``albrooks`` installed behaves exactly as it did before;
* once enabled, the engine **vetoes as well as authorises**, so enabling it can reduce the
  number of trades. That is the point of a filter, and it is the surprise worth asserting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from stop_order_scalp.domain.enums import Side
from stop_order_scalp.domain.exceptions import ComponentNotAvailableError
from stop_order_scalp.domain.models import Candle
from stop_order_scalp.domain.value_objects import Price, SymbolSpecification, Volume
from stop_order_scalp.infrastructure.config import AlBrooksSettings
from stop_order_scalp.integrations.al_brooks_adapter import ACTION_BUY, ACTION_SELL, ACTION_WAIT
from stop_order_scalp.integrations.al_brooks_signal_provider import (
    AlBrooksSignalProvider,
    Source,
)
from stop_order_scalp.strategy.signal import NoTrade, NoTradeReason, TradeDecision

NOW = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)


class FakeEngineDecision:
    def __init__(self, action: str, plan: dict[str, Any] | None = None) -> None:
        self.action = action
        self.plan = plan or {}


class FakeEngine:
    """A stand-in for the third-party engine, and a record of whether it was asked.

    The ``calls`` counter is the assertion that matters: with the integration disabled the
    engine must not be constructed *or* asked, and this is the only way to prove it.
    """

    def __init__(self, action: str = ACTION_BUY, direction: int = 1) -> None:
        self.action = action
        self.direction = direction
        self.calls = 0

    def decide(self, **_: Any) -> FakeEngineDecision:
        self.calls += 1
        return FakeEngineDecision(
            self.action,
            {
                "direction": self.direction,
                "entry": 40000.0,
                "stop": 39990.0,
                "target": 40020.0,
                "reward_to_risk": 2.0,
            },
        )


class ExplodingEngineFactory:
    """Stands in for the real engine factory and must never be called."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        raise AssertionError("the engine must not be built while the integration is disabled")


def candle(index: int, close: str = "40000.0") -> Candle:
    open_time = NOW - timedelta(minutes=index + 1)
    price = Price.parse(close, 1)
    return Candle(
        open_time=open_time,
        open=price,
        high=price,
        low=price,
        close=price,
        timeframe_seconds=60,
        timeframe="M1",
        volume=Decimal("1"),
        tick_volume=1,
        is_confirmed=True,
    )


@pytest.fixture
def us30() -> SymbolSpecification:
    return SymbolSpecification(
        name="US30",
        digits=1,
        point=Decimal("0.1"),
        tick_size=Decimal("0.1"),
        tick_value=Decimal("1.0"),
        contract_size=Decimal("1.0"),
        volume_min=Decimal("0.1"),
        volume_max=Decimal("50.0"),
        volume_step=Decimal("0.1"),
    )


def baseline_says_trade(symbol: str = "US30", reference: datetime = NOW) -> Any:
    """A stand-in for the baseline decision, recognisable by its type."""
    return _BaselineTrade(symbol, reference)


class _BaselineTrade:
    def __init__(self, symbol: str, reference: datetime) -> None:
        self.symbol = symbol
        self.reference = reference
        self.side = Side.SIDE_BUY

    def to_dict(self) -> dict[str, Any]:
        return {"decision": "trade", "symbol": self.symbol}


def baseline_says_no_trade(symbol: str = "US30", reference: datetime = NOW) -> NoTrade:
    return NoTrade(
        reason=NoTradeReason.DOJI_DIRECTION,
        detail="the M15 candle is a doji",
        reference=reference,
        symbol=symbol,
    )


def candles(count: int = 5) -> list[Candle]:
    return [candle(index) for index in range(count)]


class TestDisabledByDefault:
    def test_the_default_settings_are_disabled(self) -> None:
        assert AlBrooksSettings().enabled is False
        assert AlBrooksSettings().allow_geometry is False

    def test_the_engine_is_never_constructed(self, us30: SymbolSpecification) -> None:
        """The single most important assertion in this file.

        If the engine were built with the default configuration, the optional extra would be
        required to run at all -- which is exactly what "optional" is meant to avoid.
        """
        factory = ExplodingEngineFactory()
        provider = AlBrooksSignalProvider(
            AlBrooksSettings(), baseline_says_trade, engine_factory=factory
        )

        outcome = provider.evaluate(
            symbol="US30", candles=candles(), reference=NOW, specification=us30
        )

        assert factory.calls == 0
        assert outcome.source == Source.BASELINE

    def test_the_baseline_decision_is_returned_verbatim(self, us30: SymbolSpecification) -> None:
        provider = AlBrooksSignalProvider(
            AlBrooksSettings(), baseline_says_no_trade
        )

        outcome = provider.evaluate(
            symbol="US30", candles=candles(), reference=NOW, specification=us30
        )

        assert outcome.decision.reason == NoTradeReason.DOJI_DIRECTION

    def test_a_provider_with_no_engine_factory_works_while_disabled(
        self, us30: SymbolSpecification
    ) -> None:
        """No factory at all, which is the shape a default deployment has."""
        provider = AlBrooksSignalProvider(AlBrooksSettings(), baseline_says_trade)

        outcome = provider.evaluate(
            symbol="US30", candles=candles(), reference=NOW, specification=us30
        )

        assert outcome.source == Source.BASELINE

    def test_describe_says_the_baseline_is_authoritative(
        self, us30: SymbolSpecification
    ) -> None:
        provider = AlBrooksSignalProvider(AlBrooksSettings(), baseline_says_trade)

        described = provider.describe()

        assert described["authoritative"] == "m15_m1_baseline"
        assert described["engine_vetoes"] is False
        assert described["geometry_source"] == "m15_m1_baseline"


class TestEnabledAuthorises:
    def test_a_buy_becomes_the_providers_signal(self, us30: SymbolSpecification) -> None:
        provider = AlBrooksSignalProvider(
            AlBrooksSettings(enabled=True),
            baseline_says_trade,
            engine_factory=lambda: FakeEngine(ACTION_BUY, direction=1),
        )

        outcome = provider.evaluate(
            symbol="US30", candles=candles(), reference=NOW, specification=us30
        )

        assert outcome.source == Source.AL_BROOKS
        assert outcome.decision.side is Side.SIDE_BUY

    def test_the_engines_action_is_recorded(self, us30: SymbolSpecification) -> None:
        provider = AlBrooksSignalProvider(
            AlBrooksSettings(enabled=True),
            baseline_says_trade,
            engine_factory=lambda: FakeEngine(ACTION_SELL, direction=-1),
        )

        outcome = provider.evaluate(
            symbol="US30", candles=candles(), reference=NOW, specification=us30
        )

        assert outcome.action == ACTION_SELL

    def test_the_engine_is_built_exactly_once(
        self, us30: SymbolSpecification
    ) -> None:
        engine = FakeEngine(ACTION_BUY)
        built: list[int] = []

        def factory() -> Any:
            built.append(1)
            return engine

        provider = AlBrooksSignalProvider(
            AlBrooksSettings(enabled=True), baseline_says_trade, engine_factory=factory
        )
        for _ in range(3):
            provider.evaluate(
                symbol="US30", candles=candles(), reference=NOW, specification=us30
            )

        assert len(built) == 1
        assert engine.calls == 3

    def test_the_baseline_is_still_evaluated_for_comparison(
        self, us30: SymbolSpecification
    ) -> None:
        """So an operator enabling the integration can see what it vetoed."""
        seen: list[str] = []

        def baseline(symbol: str = "US30", reference: datetime = NOW) -> Any:
            seen.append(symbol)
            return baseline_says_trade(symbol, reference)

        provider = AlBrooksSignalProvider(
            AlBrooksSettings(enabled=True), baseline, engine_factory=FakeEngine
        )
        provider.evaluate(
            symbol="US30", candles=candles(), reference=NOW, specification=us30
        )

        assert seen == ["US30"]


def _provider(
    engine_action: str,
    direction: int,
    *,
    baseline: Any = baseline_says_trade,
    **settings: Any,
) -> AlBrooksSignalProvider:
    """A provider whose engine always returns the same decision."""
    return AlBrooksSignalProvider(
        AlBrooksSettings(enabled=True, **settings),
        baseline,
        engine_factory=lambda: FakeEngine(engine_action, direction),
    )


def _evaluate(provider: AlBrooksSignalProvider, us30: SymbolSpecification) -> Any:
    return provider.evaluate(
        symbol="US30", candles=candles(), reference=NOW, specification=us30
    )


class TestEnabledVetoes:
    def test_a_wait_becomes_a_no_trade_not_a_fallback(
        self, us30: SymbolSpecification
    ) -> None:
        """The conservative direction, and the only one consistent with the upstream contract.

        The engine reports ``is_recommendation: False``. Treating a decline as "ask someone
        else" would read it as a recommendation it explicitly disclaims.
        """
        outcome = _evaluate(_provider(ACTION_WAIT, 0), us30)

        assert outcome.source == Source.AL_BROOKS_VETO
        assert isinstance(outcome.decision, NoTrade)

    def test_the_veto_reason_is_the_vendors_own(self, us30: SymbolSpecification) -> None:
        outcome = _evaluate(_provider(ACTION_WAIT, 0), us30)

        assert outcome.decision.reason == NoTradeReason.AL_BROOKS_VETO

    def test_a_veto_can_reduce_the_number_of_trades(
        self, us30: SymbolSpecification
    ) -> None:
        """Stated as a test because it surprises people, and it is the point of a filter.

        The same baseline that produces a trade under one engine produces no trade under the
        other. An operator reading ``enabled: true`` should know that is possible.
        """
        authorising = _evaluate(_provider(ACTION_BUY, 1), us30)
        vetoing = _evaluate(_provider(ACTION_WAIT, 0), us30)

        assert authorising.source == Source.AL_BROOKS
        assert vetoing.source == Source.AL_BROOKS_VETO
        assert isinstance(vetoing.decision, NoTrade)

    def test_the_baselines_own_no_trade_is_not_overridden(
        self, us30: SymbolSpecification
    ) -> None:
        """A baseline that said no stays a no, whatever the engine says."""
        outcome = _evaluate(
            _provider(ACTION_BUY, 1, baseline=baseline_says_no_trade), us30
        )

        assert outcome.decision.side is Side.SIDE_BUY
        assert _provider(ACTION_BUY, 1).describe()["engine_vetoes"] is True

    def test_describe_reports_the_veto(self) -> None:
        assert _provider(ACTION_BUY, 1).describe()["engine_vetoes"] is True


class TestMissingExtra:
    def test_enabling_without_the_package_raises_a_clear_error(
        self, us30: SymbolSpecification
    ) -> None:
        """Reached only with the integration enabled, so the default path is unaffected."""
        from stop_order_scalp.integrations.al_brooks_adapter import import_engine

        try:
            import_engine()
        except ComponentNotAvailableError:
            pass
        else:
            pytest.skip("the optional 'albrooks' extra is installed on this machine")

        provider = AlBrooksSignalProvider(
            AlBrooksSettings(enabled=True), baseline_says_trade
        )

        with pytest.raises(ComponentNotAvailableError, match="albrooks"):
            provider.evaluate(
                symbol="US30", candles=candles(), reference=NOW, specification=us30
            )


class TestOutcome:
    def test_to_dict_is_serialisable(self, us30: SymbolSpecification) -> None:
        import json

        provider = AlBrooksSignalProvider(AlBrooksSettings(), baseline_says_trade)

        outcome = provider.evaluate(
            symbol="US30", candles=candles(), reference=NOW, specification=us30
        )

        assert json.loads(json.dumps(outcome.to_dict()))["source"] == Source.BASELINE

    def test_repr_names_the_switches(self) -> None:
        provider = AlBrooksSignalProvider(AlBrooksSettings(), baseline_says_trade)

        assert "enabled=False" in repr(provider)


class TestGeometrySource:
    def test_describe_reports_where_geometry_comes_from(self) -> None:
        on = AlBrooksSignalProvider(
            AlBrooksSettings(enabled=True, allow_geometry=True),
            baseline_says_trade,
            engine_factory=FakeEngine,
        )

        assert on.describe()["geometry_source"] == "al_brooks"

    def test_direction_only_keeps_the_baseline_geometry(self) -> None:
        provider = AlBrooksSignalProvider(
            AlBrooksSettings(enabled=True), baseline_says_trade, engine_factory=FakeEngine
        )

        assert provider.describe()["geometry_source"] == "m15_m1_baseline"


def test_the_provider_cannot_reach_a_broker() -> None:
    """Stated as a test: this phase adds a signal source, not an execution path.

    Checked against the **parsed** module rather than its text. A substring search matches
    the word inside a docstring -- which is exactly where this module discusses the ordering
    it must not disturb -- and would fail for the wrong reason.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(
        Path(
            "src/stop_order_scalp/integrations/al_brooks_signal_provider.py"
        ).read_text(encoding="utf-8")
    )
    called: set[str] = set()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            called.add(node.attr)
        elif isinstance(node, ast.Name):
            called.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported.add(alias.name)
                imported.add(getattr(alias, "asname", None) or alias.name)

    for forbidden in (
        "place_order",
        "cancel_order",
        "modify_position",
        "MetaTrader5",
    ):
        assert forbidden not in called, f"{forbidden} is reachable from the provider"
        assert forbidden not in imported, f"{forbidden} is imported by the provider"


def test_the_adapter_and_provider_produce_domain_types(
    ) -> None:
    """A reader can tell the outcome is one of the project's own two, not a foreign object."""
    assert TradeDecision is not None
    assert Volume.of(Decimal("0.1")).lots == Decimal("0.1")
