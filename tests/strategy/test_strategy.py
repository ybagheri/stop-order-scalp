"""The strategy façade: construction, dispatch, and the self-describing rule.

The interesting tests here are the *refusals at construction*. A strategy built against a
mismatched specification, or one configured with a timeframe whose boundaries cannot be
computed, would otherwise fail later and deeper -- inside a decision, with a stack trace,
having already looked like a working system.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from stop_order_scalp.domain.enums import Environment, Side, TimeframeSelection
from stop_order_scalp.domain.exceptions import ConfigError
from stop_order_scalp.domain.models import Candle, InstrumentPolicy
from stop_order_scalp.domain.value_objects import SymbolSpecification
from stop_order_scalp.infrastructure.config import EntrySettings, StrategySettings
from stop_order_scalp.strategy.signal import NoTrade, TradeDecision
from stop_order_scalp.strategy.strategy import (
    StopOrderStrategy,
    StrategyContext,
    entry_geometry,
)
from strategy.conftest import (
    BASE,
    M1,
    M15,
    at,
    bearish_m15,
    bullish_m15,
    doji_m15,
    last_m15_close,
    m1_series,
    m15_series,
)


def _context(
    strategy: StopOrderStrategy,
    *,
    m15: list[Candle] | None = None,
    m1: list[Candle] | None = None,
    reference: datetime | None = None,
) -> StrategyContext:
    return StrategyContext(
        symbol=strategy.symbol,
        m15_candles=m15 if m15 is not None else m15_series(1),
        m1_candles=m1 if m1 is not None else m1_series(5),
        reference=reference if reference is not None else last_m15_close(),
        specification=strategy.specification,
        policy=strategy.policy,
    )


@pytest.fixture
def strategy(settings: StrategySettings, spec: SymbolSpecification) -> StopOrderStrategy:
    return StopOrderStrategy(settings, spec)


class TestConstruction:
    def test_it_holds_the_configured_symbol(
        self, strategy: StopOrderStrategy, settings: StrategySettings
    ) -> None:
        assert strategy.symbol == settings.symbol
        assert "US30" in repr(strategy)

    def test_a_mismatched_specification_is_refused(
        self, settings: StrategySettings
    ) -> None:
        from decimal import Decimal

        wrong = SymbolSpecification(
            name="EURUSD", digits=5, point=Decimal("0.00001"),
            tick_size=Decimal("0.00001"), tick_value=Decimal("1.0"),
            contract_size=Decimal("100000"), volume_min=Decimal("0.01"),
            volume_max=Decimal("100"), volume_step=Decimal("0.01"),
        )
        with pytest.raises(ConfigError, match="EURUSD"):
            StopOrderStrategy(settings, wrong)

    def test_a_broker_variant_name_is_accepted(
        self, settings: StrategySettings
    ) -> None:
        from decimal import Decimal

        # The broker calls it US30.cash, which is an accepted alias. Requiring an exact
        # match with the logical symbol would make the strategy impossible to construct on
        # a broker that names it differently -- which is the common case, not the exotic one.
        variant = SymbolSpecification(
            name="US30.cash", digits=1, point=Decimal("0.1"), tick_size=Decimal("0.1"),
            tick_value=Decimal("1.0"), contract_size=Decimal("1.0"),
            volume_min=Decimal("0.1"), volume_max=Decimal("50.0"),
            volume_step=Decimal("0.1"),
        )
        settings = StrategySettings(
            symbol="US30", symbol_aliases=("US30", "US30.cash", "US30m")
        )
        assert StopOrderStrategy(settings, variant).symbol == "US30"

    def test_the_mismatch_error_names_both(self, settings: StrategySettings) -> None:
        from decimal import Decimal

        foreign = SymbolSpecification(
            name="EURUSD", digits=5, point=Decimal("0.00001"), tick_size=Decimal("0.00001"),
            tick_value=Decimal("1.0"), contract_size=Decimal("100000"),
            volume_min=Decimal("0.01"), volume_max=Decimal("100"),
            volume_step=Decimal("0.01"),
        )
        with pytest.raises(ConfigError, match="EURUSD") as caught:
            StopOrderStrategy(settings, foreign)
        assert "US30" in str(caught.value)

    def test_a_monthly_timeframe_is_refused(self, spec: SymbolSpecification) -> None:
        settings = StrategySettings(
            symbol="US30",
            entry=EntrySettings(timeframe="M1", direction_timeframe="MN1"),
        )
        with pytest.raises(ConfigError, match="direction_timeframe"):
            StopOrderStrategy(settings, spec)

    def test_an_unknown_timeframe_is_refused(self, spec: SymbolSpecification) -> None:
        with pytest.raises(ConfigError):
            StopOrderStrategy(
                StrategySettings(symbol="US30", entry=EntrySettings(timeframe="M99")),
                spec,
            )

    def test_the_timeframe_error_names_the_config_key(
        self, spec: SymbolSpecification
    ) -> None:
        with pytest.raises(ConfigError, match=r"entry\.timeframe"):
            StopOrderStrategy(
                StrategySettings(symbol="US30", entry=EntrySettings(timeframe="NOPE")),
                spec,
            )


class TestTheClosedCandleRule:
    def test_the_baseline_uses_closed_candles(
        self, strategy: StopOrderStrategy
    ) -> None:
        assert strategy.uses_closed_candles is True
        assert strategy.entry_settings.candle_selection is TimeframeSelection.LAST_CLOSED

    def test_the_property_follows_configuration(
        self, settings: StrategySettings, spec: SymbolSpecification
    ) -> None:
        # Flipping it is a visible, deliberate act rather than a silent behaviour change.
        research = StrategySettings(
            symbol="US30",
            entry=EntrySettings(candle_selection=TimeframeSelection.CURRENT_FORMING),
        )
        assert StopOrderStrategy(research, spec).uses_closed_candles is False

    def test_describe_states_the_whole_rule(self, strategy: StopOrderStrategy) -> None:
        payload = strategy.describe()
        assert payload["symbol"] == "US30"
        assert payload["direction_timeframe"] == M15
        assert payload["entry_timeframe"] == M1
        assert payload["offset_points"] == 10
        assert payload["candle_selection"] == "last_closed"
        assert payload["uses_closed_candles"] is True

    def test_describe_includes_the_specification(
        self, strategy: StopOrderStrategy
    ) -> None:
        # A journal entry months later needs the numbers the decision was made with.
        spec = strategy.describe()["specification"]
        assert spec["point"] == "0.1"
        assert spec["tick_value"] == "1.0"
        assert spec["stops_level"] == 10

    def test_describe_serialises_to_json(self, strategy: StopOrderStrategy) -> None:
        import json

        json.dumps(strategy.describe())


class TestEvaluation:
    def test_a_bullish_m15_trades(
        self, strategy: StopOrderStrategy
    ) -> None:
        decision = strategy.evaluate(_context(strategy))
        assert isinstance(decision, TradeDecision)
        assert decision.side is Side.SIDE_BUY

    def test_a_bearish_m15_trades_the_other_way(
        self, strategy: StopOrderStrategy
    ) -> None:
        decision = strategy.evaluate(_context(strategy, m15=[bearish_m15(0)]))
        assert isinstance(decision, TradeDecision)
        assert decision.side is Side.SIDE_SELL

    def test_a_doji_does_not_trade(self, strategy: StopOrderStrategy) -> None:
        decision = strategy.evaluate(_context(strategy, m15=[doji_m15(0)]))
        assert isinstance(decision, NoTrade)

    def test_the_direction_accessor_agrees_with_the_decision(
        self, strategy: StopOrderStrategy
    ) -> None:
        context = _context(strategy)
        verdict = strategy.direction(context)
        decision = strategy.evaluate(context)
        assert isinstance(decision, TradeDecision)
        assert verdict.side == decision.side
        assert verdict.allows_trading

    def test_the_direction_accessor_works_when_there_is_no_trade(
        self, strategy: StopOrderStrategy
    ) -> None:
        context = _context(strategy, m15=[doji_m15(0)])
        assert strategy.direction(context).verdict == "doji"
        assert isinstance(strategy.evaluate(context), NoTrade)

    def test_the_direction_candle_is_the_one_that_decided(
        self, strategy: StopOrderStrategy
    ) -> None:
        context = _context(strategy, m15=[bullish_m15(0), bearish_m15(1)],
                           reference=last_m15_close(index=1))
        assert strategy.direction_candle(context).open_time == BASE + timedelta(seconds=900)

    def test_evaluation_is_pure(self, strategy: StopOrderStrategy) -> None:
        context = _context(strategy)
        first = strategy.evaluate(context)
        second = strategy.evaluate(context)
        assert first.to_dict() == second.to_dict()

    def test_a_context_policy_overrides_the_strategy_policy(
        self, settings: StrategySettings, spec: SymbolSpecification
    ) -> None:
        strict = InstrumentPolicy(
            logical_symbol="US30", accepted_names=frozenset({"US30.cash"})
        )
        built = StopOrderStrategy(settings, spec, policy=strict)
        context = _context(built)
        from stop_order_scalp.domain.exceptions import InstrumentNotAllowedError

        with pytest.raises(InstrumentNotAllowedError):
            built.evaluate(context)


class TestGeometryHelper:
    def test_it_serialises_an_entry_level(self, spec: SymbolSpecification) -> None:
        from stop_order_scalp.strategy.entry_rules import buy_stop_price
        from strategy.conftest import m1

        level = buy_stop_price(m1(0), 10, spec)
        payload = entry_geometry(level)
        assert payload["order_kind"] == "BUY_STOP"
        assert payload["offset_points"] == 10
        assert payload["rounded"] is False


class TestContext:
    def test_it_requires_a_symbol_and_candles(
        self, spec: SymbolSpecification
    ) -> None:
        context = StrategyContext(
            symbol="US30",
            m15_candles=[],
            m1_candles=[],
            reference=last_m15_close(),
            specification=spec,
        )
        assert context.symbol == "US30"
        assert context.policy is None

    def test_the_environment_enum_is_unused_here(
        self, settings: StrategySettings, spec: SymbolSpecification
    ) -> None:
        # The strategy must not know the execution mode. DRY_RUN, PAPER, DEMO and LIVE all
        # run the same decision code; only the broker differs.
        built = StopOrderStrategy(settings, spec)
        assert not hasattr(built, "environment")
        assert Environment.DRY_RUN.value == "DRY_RUN"


def test_at_and_base_are_distinct_times() -> None:
    # Guards the shared helpers: if at() silently returned BASE, several tests above would
    # pass for the wrong reason.
    assert at(minutes=15) != BASE
    assert at(minutes=15) == last_m15_close()
