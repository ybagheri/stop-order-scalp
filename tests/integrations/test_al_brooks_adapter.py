"""The adapter maps an engine decision, and never invents one.

Driven by a fake engine, because the roadmap's gate for this phase is "adapter tests using a
fake engine" and because ``albrooks`` is not installed here. The fake mirrors the shape the
Phase 0 audit recorded: ``Decision(action=..., plan={...})`` with ``direction`` an int.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from stop_order_scalp.domain.enums import OrderKind, Side
from stop_order_scalp.domain.value_objects import Price, SymbolSpecification, Volume
from stop_order_scalp.infrastructure.config import AlBrooksSettings
from stop_order_scalp.integrations.al_brooks_adapter import (
    ACTION_BUY,
    ACTION_NO_TRADE,
    ACTION_SELL,
    ACTION_WAIT,
    AlBrooksAdapter,
    AlBrooksRefusal,
    import_engine,
)

NOW = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)


class FakeDecision:
    """The engine's decision shape, as recorded in the Phase 0 audit.

    A plain object with the two documented attributes, so the adapter is exercised against
    the *contract* rather than against whatever version happens to be installed. ``plan`` is
    typed loosely because the engine really does return either a dict or an object with
    ``to_dict`` -- the adapter handles both, so the fake has to be able to produce both.
    """

    def __init__(self, action: str, plan: Any = None) -> None:
        self.action = action
        self.plan = plan


class FakePlan:
    """A plan object rather than a dict, to exercise the ``to_dict`` path."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, Any]:
        return dict(self._payload)


def plan_for(action: str, *, direction: int = 1) -> dict[str, Any]:
    return {
        "direction": direction,
        "entry": 40000.0,
        "stop": 39990.0,
        "target": 40020.0,
        "reward_to_risk": 2.0,
    }


def adapter(**settings: Any) -> AlBrooksAdapter:
    return AlBrooksAdapter(AlBrooksSettings(**settings), "US30", digits=1)


def enabled(**settings: Any) -> AlBrooksAdapter:
    return AlBrooksAdapter(AlBrooksSettings(enabled=True, **settings), "US30", digits=1)


class TestDisabledByDefault:
    def test_the_default_configuration_is_disabled(self) -> None:
        """The whole point of the extra being optional: nothing changes without opting in."""
        assert AlBrooksSettings().enabled is False

    def test_a_disabled_adapter_produces_no_signal_even_for_a_buy(self) -> None:
        outcome = adapter().adapt(FakeDecision(ACTION_BUY, plan_for(ACTION_BUY)),
                                  source_candle_open_time=NOW)

        assert outcome.is_declined
        assert outcome.code == AlBrooksRefusal.DISABLED

    def test_a_disabled_adapter_still_records_the_action(self) -> None:
        """So a journal shows what the engine said even while it is inert."""
        outcome = adapter().adapt(FakeDecision(ACTION_BUY, plan_for(ACTION_BUY)),
                                  source_candle_open_time=NOW)

        assert outcome.action == ACTION_BUY


class TestActionMapping:
    def test_a_buy_becomes_a_buy_stop(self) -> None:
        outcome = enabled().adapt(
            FakeDecision(ACTION_BUY, plan_for(ACTION_BUY, direction=1)),
            source_candle_open_time=NOW,
        )

        signal = outcome.require_signal()
        assert signal.side is Side.SIDE_BUY
        assert signal.order_kind is OrderKind.ORDER_KIND_BUY_STOP

    def test_a_sell_becomes_a_sell_stop(self) -> None:
        outcome = enabled().adapt(
            FakeDecision(ACTION_SELL, plan_for(ACTION_SELL, direction=-1)),
            source_candle_open_time=NOW,
        )

        signal = outcome.require_signal()
        assert signal.side is Side.SIDE_SELL
        assert signal.order_kind is OrderKind.ORDER_KIND_SELL_STOP

    def test_the_signal_is_marked_as_coming_from_the_engine(self) -> None:
        """A journal reader must be able to tell which rule produced it."""
        outcome = enabled().adapt(
            FakeDecision(ACTION_BUY, plan_for(ACTION_BUY)), source_candle_open_time=NOW
        )

        assert outcome.require_signal().source == "al_brooks"

    def test_the_upstream_is_recommendation_flag_is_recorded_false(self) -> None:
        """The engine hard-codes ``is_recommendation: False``; the journal should say so."""
        outcome = enabled().adapt(
            FakeDecision(ACTION_BUY, plan_for(ACTION_BUY)), source_candle_open_time=NOW
        )

        assert outcome.require_signal().context["al_brooks_is_recommendation"] is False

    def test_a_plan_object_with_to_dict_is_accepted(self) -> None:
        outcome = enabled().adapt(
            FakeDecision(ACTION_BUY, FakePlan(plan_for(ACTION_BUY))),
            source_candle_open_time=NOW,
        )

        assert outcome.is_signal


class TestDecliningActionsAreNeverSignals:
    """The rule that keeps the adapter a filter rather than a source of trades."""

    @pytest.mark.parametrize("action", [ACTION_WAIT, ACTION_NO_TRADE])
    def test_a_declining_action_produces_no_signal(self, action: str) -> None:
        outcome = enabled().adapt(FakeDecision(action, {}), source_candle_open_time=NOW)

        assert outcome.is_declined

    def test_a_wait_and_a_no_trade_are_distinguishable(self) -> None:
        """Different upstream answers, so different journal entries."""
        wait = enabled().adapt(FakeDecision(ACTION_WAIT, {}), source_candle_open_time=NOW)
        no_trade = enabled().adapt(
            FakeDecision(ACTION_NO_TRADE, {}), source_candle_open_time=NOW
        )

        assert wait.code == AlBrooksRefusal.WAIT
        assert no_trade.code == AlBrooksRefusal.NO_TRADE

    def test_a_declining_action_converts_to_a_no_trade(self) -> None:
        outcome = enabled().adapt(FakeDecision(ACTION_WAIT, {}), source_candle_open_time=NOW)

        no_trade = outcome.to_no_trade(reference=NOW, symbol="US30")

        assert no_trade.reason == AlBrooksRefusal.WAIT
        assert "US30" in no_trade.detail or no_trade.symbol == "US30"


class TestNeverInvents:
    def test_an_unknown_action_is_refused_not_guessed(self) -> None:
        """A new upstream action must fail closed rather than default to some side."""
        outcome = enabled().adapt(
            FakeDecision("SHORT", plan_for(ACTION_BUY)), source_candle_open_time=NOW
        )

        assert outcome.code == AlBrooksRefusal.UNKNOWN_ACTION

    def test_a_missing_action_is_refused(self) -> None:
        outcome = enabled().adapt(FakeDecision("", {}), source_candle_open_time=NOW)

        assert outcome.is_declined

    def test_a_buy_whose_direction_says_short_is_refused(self) -> None:
        """The engine carries direction twice; a contradiction is a broken decision."""
        outcome = enabled().adapt(
            FakeDecision(ACTION_BUY, plan_for(ACTION_BUY, direction=-1)),
            source_candle_open_time=NOW,
        )

        assert outcome.code == AlBrooksRefusal.DIRECTION_CONFLICTS_ACTION

    def test_a_sell_whose_direction_says_long_is_refused(self) -> None:
        outcome = enabled().adapt(
            FakeDecision(ACTION_SELL, plan_for(ACTION_SELL, direction=1)),
            source_candle_open_time=NOW,
        )

        assert outcome.code == AlBrooksRefusal.DIRECTION_CONFLICTS_ACTION

    def test_a_trade_with_a_zero_direction_is_refused(self) -> None:
        outcome = enabled().adapt(
            FakeDecision(ACTION_BUY, plan_for(ACTION_BUY, direction=0)),
            source_candle_open_time=NOW,
        )

        assert outcome.code == AlBrooksRefusal.DIRECTION_MISSING

    def test_a_non_numeric_direction_is_refused(self) -> None:
        payload = plan_for(ACTION_BUY)
        payload["direction"] = "up"
        outcome = enabled().adapt(
            FakeDecision(ACTION_BUY, payload), source_candle_open_time=NOW
        )

        assert outcome.code == AlBrooksRefusal.DIRECTION_MISSING

    def test_a_missing_entry_is_refused(self) -> None:
        outcome = enabled().adapt(
            FakeDecision(ACTION_BUY, {"direction": 1}), source_candle_open_time=NOW
        )

        assert outcome.code == AlBrooksRefusal.GEOMETRY_UNUSABLE

    def test_an_unparseable_entry_is_refused(self) -> None:
        payload = plan_for(ACTION_BUY)
        payload["entry"] = "not a number"
        outcome = enabled().adapt(
            FakeDecision(ACTION_BUY, payload), source_candle_open_time=NOW
        )

        assert outcome.code == AlBrooksRefusal.GEOMETRY_UNUSABLE


class TestGeometryIsSeparate:
    def test_geometry_is_off_by_default(self) -> None:
        assert AlBrooksSettings().allow_geometry is False

    def test_an_enabled_engine_without_geometry_sends_no_levels(self) -> None:
        """One traceable source for the level an order actually uses."""
        outcome = enabled().adapt(
            FakeDecision(ACTION_BUY, plan_for(ACTION_BUY)), source_candle_open_time=NOW
        )

        signal = outcome.require_signal()
        assert signal.stop_loss is None
        assert signal.take_profit is None

    def test_the_geometry_source_is_recorded_in_the_context(self) -> None:
        outcome = enabled().adapt(
            FakeDecision(ACTION_BUY, plan_for(ACTION_BUY)), source_candle_open_time=NOW
        )

        assert (
            outcome.require_signal().context["geometry_source"] == "m15_m1_baseline"
        )

    def test_allow_geometry_carries_the_engines_own_levels(self) -> None:
        outcome = enabled(allow_geometry=True).adapt(
            FakeDecision(ACTION_BUY, plan_for(ACTION_BUY)), source_candle_open_time=NOW
        )

        signal = outcome.require_signal()
        assert signal.stop_loss == Price.parse("39990.0", 1)
        assert signal.take_profit == Price.parse("40020.0", 1)

    def test_the_geometry_source_changes_when_allowed(self) -> None:
        outcome = enabled(allow_geometry=True).adapt(
            FakeDecision(ACTION_BUY, plan_for(ACTION_BUY)), source_candle_open_time=NOW
        )

        assert outcome.require_signal().context["geometry_source"] == "al_brooks"

    def test_a_plan_without_a_stop_still_produces_a_direction(self) -> None:
        """A missing optional level must not throw away a usable direction."""
        payload = plan_for(ACTION_BUY)
        del payload["stop"]
        outcome = enabled(allow_geometry=True).adapt(
            FakeDecision(ACTION_BUY, payload), source_candle_open_time=NOW
        )

        assert outcome.is_signal
        assert outcome.require_signal().stop_loss is None


class TestDigits:
    def test_the_entry_is_parsed_at_the_specifications_precision(self) -> None:
        adapter = AlBrooksAdapter(
            AlBrooksSettings(enabled=True), "US30", digits=2
        )

        outcome = adapter.adapt(
            FakeDecision(ACTION_BUY, plan_for(ACTION_BUY)), source_candle_open_time=NOW
        )

        assert outcome.require_signal().reference_price.digits == 2


class TestDecisionContract:
    def test_require_signal_raises_when_declined(self) -> None:
        outcome = enabled().adapt(FakeDecision(ACTION_WAIT, {}), source_candle_open_time=NOW)

        with pytest.raises(ValueError, match="no signal"):
            outcome.require_signal()

    def test_to_dict_is_serialisable(self) -> None:
        import json

        outcome = enabled().adapt(
            FakeDecision(ACTION_BUY, plan_for(ACTION_BUY)), source_candle_open_time=NOW
        )

        assert json.loads(json.dumps(outcome.to_dict()))["has_signal"] is True

    def test_a_signal_and_a_refusal_are_mutually_exclusive(self) -> None:
        """Never both, and never neither: a caller must not have to guess."""
        for decision in (FakeDecision(ACTION_BUY, plan_for(ACTION_BUY)), FakeDecision("", {})):
            outcome = enabled().adapt(decision, source_candle_open_time=NOW)
            assert outcome.is_signal is not outcome.is_declined

    def test_every_refusal_code_is_distinct(self) -> None:
        codes = [
            value
            for name, value in vars(AlBrooksRefusal).items()
            if not name.startswith("_") and isinstance(value, str)
        ]

        assert len(codes) == len(set(codes))
        assert len(codes) >= 6


class TestOptionalImport:
    def test_import_engine_explains_how_to_install(self) -> None:
        """The message is the only thing a user sees if the extra is missing."""
        from stop_order_scalp.domain.exceptions import ComponentNotAvailableError

        try:
            engine = import_engine()
        except ComponentNotAvailableError as exc:
            assert "albrooks" in str(exc)
            assert "pip install" in str(exc)
        else:
            assert engine is not None

    def test_importing_the_adapter_does_not_import_the_engine(self) -> None:
        """Otherwise the optional extra would be required to import the package at all."""
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import stop_order_scalp.integrations.al_brooks_adapter as m;"
                "import sys;"
                "print('albrooks' in sys.modules)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout.strip() == "False"


def test_the_adapter_never_names_the_package_outside_the_one_module() -> None:
    """The architecture rule restated as a test, so it cannot rot quietly."""
    from pathlib import Path

    offenders: list[str] = []
    for path in Path("src").rglob("*.py"):
        if path.name == "al_brooks_adapter.py":
            continue
        if "import albrooks" in path.read_text(encoding="utf-8"):
            offenders.append(str(path))

    assert offenders == [], f"only al_brooks_adapter may import albrooks: {offenders}"


def test_the_provider_does_not_name_the_package_either() -> None:
    """The provider reaches the engine through a callable, not an import."""
    from pathlib import Path

    source = Path(
        "src/stop_order_scalp/integrations/al_brooks_signal_provider.py"
    ).read_text(encoding="utf-8")

    assert "import albrooks" not in source


def test_unused_price_and_volume_are_importable() -> None:
    """Guards the value objects the adapter's price parsing relies on."""
    assert Volume.of(Decimal("0.1")).lots == Decimal("0.1")
    assert SymbolSpecification is not None
