"""Configuration loading and layering.

The behaviours under test are the ones that make a misconfiguration *visible*:
unknown keys rejected, env overrides applied predictably, and the environment variable
beating ``.env``.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from stop_order_scalp.domain.enums import (
    CommissionMode,
    Environment,
    RiskMode,
    TargetMode,
    TimeframeSelection,
)
from stop_order_scalp.domain.exceptions import ConfigError
from stop_order_scalp.infrastructure.config import (
    AppConfig,
    RetrySettings,
    load_config,
    load_env_file,
)


@pytest.fixture
def config_path(project_root: Path) -> Path:
    return project_root / "config" / "default.yaml"


@pytest.fixture
def baseline(config: AppConfig) -> AppConfig:
    """The shipped configuration, loaded with no environment influence."""
    return config


class TestShippedDefaults:
    """The baseline strategy must be exactly what the specification states."""

    def test_symbol_is_us30(self, baseline: AppConfig) -> None:
        assert baseline.strategy.symbol == "US30"

    def test_direction_timeframe_is_m15_and_entry_timeframe_is_m1(self, baseline: AppConfig) -> None:
        assert baseline.strategy.entry.direction_timeframe == "M15"
        assert baseline.strategy.entry.timeframe == "M1"

    def test_entry_offset_is_ten_points(self, baseline: AppConfig) -> None:
        assert baseline.strategy.entry.offset_points == 10

    def test_only_closed_candles_are_used(self, baseline: AppConfig) -> None:
        # The baseline must never read the forming candle. Look-ahead bias is the single
        # most damaging defect this project could ship.
        assert baseline.strategy.entry.candle_selection is TimeframeSelection.LAST_CLOSED
        assert baseline.strategy.entry.uses_closed_candles

    def test_risk_is_half_a_percent_of_balance(self, baseline: AppConfig) -> None:
        assert baseline.strategy.risk.mode is RiskMode.RISK_MODE_PERCENT_BALANCE
        assert baseline.strategy.risk.percent == Decimal("0.5")

    def test_commission_is_six_per_lot_round_trip(self, baseline: AppConfig) -> None:
        assert baseline.strategy.risk.commission_per_lot == Decimal("6.0")
        assert baseline.strategy.risk.commission_mode is CommissionMode.PER_LOT_ROUND_TRIP

    def test_take_profit_is_one_thousand_points_in_fixed_mode(self, baseline: AppConfig) -> None:
        assert baseline.strategy.target.mode is TargetMode.TARGET_MODE_FIXED_POINTS
        assert baseline.strategy.target.take_profit_points == 1000

    def test_risk_reward_is_one_to_one(self, baseline: AppConfig) -> None:
        assert baseline.strategy.target.risk_reward == Decimal("1.0")

    def test_trailing_is_enabled_at_one_hundred_points(self, baseline: AppConfig) -> None:
        assert baseline.strategy.trailing.enabled
        assert baseline.strategy.trailing.distance_points == 100

    def test_break_even_is_enabled_with_a_positive_trigger(self, baseline: AppConfig) -> None:
        assert baseline.strategy.break_even.enabled
        assert baseline.strategy.break_even.trigger_points > 0

    def test_no_research_filter_is_enabled_by_default(self, baseline: AppConfig) -> None:
        # Phase 12 research must be additive. If a filter is on in the shipped file, the
        # baseline is no longer reproducible from this file alone.
        assert not baseline.strategy.filters.any_enabled

    def test_al_brooks_integration_is_disabled_by_default(self, baseline: AppConfig) -> None:
        assert not baseline.integrations.al_brooks.enabled

    def test_default_environment_is_dry_run(self, baseline: AppConfig) -> None:
        assert baseline.environment.environment is Environment.DRY_RUN
        assert not baseline.environment.allow_live
        assert not baseline.environment.allow_order

    def test_no_credential_reaches_the_serialisable_summary(self, baseline: AppConfig) -> None:
        import json

        rendered = json.dumps(baseline.summary(), default=str)
        assert "password" not in rendered.lower() or "password_configured" in rendered
        assert baseline.environment.to_dict()["mt5_password_configured"] is False


class TestValidation:
    def test_unknown_top_level_key_is_an_error(self, tmp_path: Path) -> None:
        _write(tmp_path, "symbol: US30\nmystery: 1\n")
        with pytest.raises(ConfigError, match="unknown configuration keys"):
            _load(tmp_path)

    def test_unknown_nested_key_is_an_error(self, tmp_path: Path) -> None:
        _write(tmp_path, "trailing:\n  distance_point: 100\n")
        with pytest.raises(ConfigError, match="unknown keys under 'trailing'"):
            _load(tmp_path)

    def test_unknown_retry_key_is_an_error(self, tmp_path: Path) -> None:
        _write(tmp_path, "execution:\n  retry:\n    tries: 3\n")
        with pytest.raises(ConfigError, match=r"unknown keys under 'execution\.retry'"):
            _load(tmp_path)

    def test_missing_file_is_an_error_that_names_the_path(self, tmp_path: Path) -> None:
        from stop_order_scalp.infrastructure.config import load_config

        with pytest.raises(ConfigError, match="strategy configuration not found"):
            load_config(config_path=tmp_path / "nope.yaml", root=tmp_path)

    def test_malformed_yaml_is_reported_as_such(self, tmp_path: Path) -> None:
        _write(tmp_path, "symbol: [unclosed\n")
        with pytest.raises(ConfigError, match="invalid YAML"):
            _load(tmp_path)

    def test_non_mapping_top_level_is_rejected(self, tmp_path: Path) -> None:
        _write(tmp_path, "- US30\n- EURUSD\n")
        with pytest.raises(ConfigError, match="must be a mapping"):
            _load(tmp_path)

    def test_negative_offset_is_rejected(self, tmp_path: Path) -> None:
        _write(tmp_path, "entry:\n  offset_points: -1\n")
        with pytest.raises(ConfigError, match="offset_points"):
            _load(tmp_path)

    def test_identical_timeframes_are_rejected(self, tmp_path: Path) -> None:
        # Direction and entry must be distinguishable, otherwise the M15/M1 rules collapse.
        _write(tmp_path, "entry:\n  timeframe: M15\n  direction_timeframe: M15\n")
        with pytest.raises(ConfigError, match="two distinct timeframes"):
            _load(tmp_path)

    def test_zero_percent_risk_is_rejected(self, tmp_path: Path) -> None:
        _write(tmp_path, "risk:\n  percent: 0\n")
        with pytest.raises(ConfigError, match="percent must be positive"):
            _load(tmp_path)

    def test_break_even_enabled_with_zero_trigger_is_rejected(self, tmp_path: Path) -> None:
        # The specification explicitly warns against assuming the stop should move to entry
        # the moment the position opens. Refusing the configuration is better than honouring
        # a reading of the spec that the spec disclaims.
        _write(tmp_path, "break_even:\n  enabled: true\n  trigger_points: 0\n")
        with pytest.raises(ConfigError, match="instant the position opens"):
            _load(tmp_path)

    def test_break_even_disabled_with_zero_trigger_is_allowed(self, tmp_path: Path) -> None:
        _write(tmp_path, "break_even:\n  enabled: false\n  trigger_points: 0\n")
        assert _load(tmp_path).strategy.break_even.trigger_points == 0

    def test_zero_trailing_distance_is_rejected(self, tmp_path: Path) -> None:
        _write(tmp_path, "trailing:\n  distance_points: 0\n")
        with pytest.raises(ConfigError, match="distance_points must be positive"):
            _load(tmp_path)

    def test_negative_take_profit_is_rejected(self, tmp_path: Path) -> None:
        _write(tmp_path, "target:\n  take_profit_points: -100\n")
        with pytest.raises(ConfigError, match="take_profit_points"):
            _load(tmp_path)

    def test_unknown_risk_mode_names_the_valid_options(self, tmp_path: Path) -> None:
        _write(tmp_path, "risk:\n  mode: hand_waving\n")
        with pytest.raises(ConfigError, match="expected one of"):
            _load(tmp_path)

    def test_unknown_session_name_is_rejected(self, tmp_path: Path) -> None:
        _write(tmp_path, "filters:\n  sessions:\n    - tokyo\n")
        with pytest.raises(ConfigError, match="unknown session"):
            _load(tmp_path)

    def test_geometry_without_enabling_the_integration_is_rejected(self, tmp_path: Path) -> None:
        _write(tmp_path, "integrations:\n  al_brooks:\n    enabled: false\n    allow_geometry: true\n")
        with pytest.raises(ConfigError, match="requires"):
            _load(tmp_path)

    def test_a_symbol_cannot_be_both_approved_and_quarantined(self, tmp_path: Path) -> None:
        _write(tmp_path, "symbol_aliases: [US30]\nsymbol_quarantine: [us30]\n")
        with pytest.raises(ConfigError, match="both aliases and quarantine"):
            _load(tmp_path)

    def test_empty_alias_list_is_rejected(self, tmp_path: Path) -> None:
        _write(tmp_path, "symbol_aliases: []\n")
        with pytest.raises(ConfigError, match="must not be empty"):
            _load(tmp_path)


class TestEnvironmentLayering:
    def test_a_dot_env_in_the_working_directory_cannot_reach_a_test(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression. ``load_config(env_file=None)`` auto-discovers a ``.env`` in the cwd
        # and merges it into os.environ, so a developer who has a real ``.env`` used to
        # make the validation tests pass or fail depending on their machine. The helpers
        # pass an explicit non-existent path, which is the documented way to have no
        # environment layer at all.
        hostile = tmp_path / "cwd"
        hostile.mkdir()
        (hostile / ".env").write_text("SOS_SYMBOL=US30\nSOS_RISK_PERCENT=99\n", encoding="utf-8")
        monkeypatch.chdir(hostile)

        _write(tmp_path, "risk:\n  percent: 0.75\n")
        assert _load(tmp_path).strategy.risk.percent == Decimal("0.75")

    def test_auto_discovery_still_finds_a_dot_env_in_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The counterpart: the production behaviour is intentional and must not regress
        # into "always ignore .env". Only the *tests* are insulated from it.
        hostile = tmp_path / "cwd"
        hostile.mkdir()
        (hostile / ".env").write_text("SOS_RISK_PERCENT=1.25\n", encoding="utf-8")
        monkeypatch.chdir(hostile)
        _write(tmp_path, "risk:\n  percent: 0.75\n")

        loaded = load_config(
            config_path=tmp_path / "config" / "default.yaml",
            env_file=None,
            root=tmp_path,
        )
        assert loaded.strategy.risk.percent == Decimal("1.25")

    def test_a_real_environment_variable_beats_the_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("SOS_RISK_PERCENT=1.5\n", encoding="utf-8")
        monkeypatch.setenv("SOS_RISK_PERCENT", "0.25")
        _write(tmp_path, "risk:\n  percent: 0.5\n")

        config = _load(tmp_path, env_file=env_file)
        assert config.strategy.risk.percent == Decimal("0.25")

    def test_the_env_file_beats_the_yaml(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("SOS_RISK_PERCENT=1.5\n", encoding="utf-8")
        _write(tmp_path, "risk:\n  percent: 0.5\n")

        config = _load(tmp_path, env_file=env_file)
        assert config.strategy.risk.percent == Decimal("1.5")

    def test_yaml_is_the_lowest_layer(self, tmp_path: Path) -> None:
        _write(tmp_path, "risk:\n  percent: 0.75\n")
        assert _load(tmp_path).strategy.risk.percent == Decimal("0.75")

    def test_underscore_override_resolves_against_the_section_anchor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DISTANCE_POINTS contains an underscore, so a blanket "_" -> "." replacement
        # would produce trailing.distance.points and silently do nothing.
        monkeypatch.setenv("SOS_TRAILING_DISTANCE_POINTS", "250")
        _write(tmp_path, "trailing:\n  distance_points: 100\n")
        assert _load(tmp_path).strategy.trailing.distance_points == 250

    def test_nested_underscore_override_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOS_ENTRY_OFFSET_POINTS", "25")
        _write(tmp_path, "entry:\n  offset_points: 10\n")
        assert _load(tmp_path).strategy.entry.offset_points == 25

    def test_override_is_case_insensitive_in_the_variable_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Environment variable names are conventionally upper-case; the override must not
        # depend on the operator typing it that way.
        monkeypatch.setenv("SOS_trailing_distance_points", "250")
        _write(tmp_path, "trailing:\n  distance_points: 100\n")
        assert _load(tmp_path).strategy.trailing.distance_points == 250

    def test_nested_override_addresses_a_leaf_two_levels_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOS_EXECUTION_RETRY_MAX_ATTEMPTS", "9")
        _write(tmp_path, "execution:\n  retry:\n    max_attempts: 5\n")
        assert _load(tmp_path).execution.retry.max_attempts == 9

    def test_an_override_cannot_introduce_an_unknown_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An env var that does not correspond to a real parameter is a machine-local
        # setting, not a strategy override. It must be ignored, not written into the tree.
        monkeypatch.setenv("SOS_MT5_PATH", r"C:\terminals\terminal64.exe")
        _write(tmp_path, "symbol: US30\n")
        assert _load(tmp_path).strategy.symbol == "US30"

    def test_symbol_env_var_adds_the_broker_name_to_the_aliases(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Otherwise SOS_SYMBOL would select an instrument the policy then refuses.
        monkeypatch.setenv("SOS_SYMBOL", "US30.cash")
        _write(tmp_path, "symbol: US30\nsymbol_aliases: [US30]\n")
        config = _load(tmp_path)
        assert config.environment.symbol == "US30.cash"
        assert config.strategy.instrument_policy.allows("US30.cash")

    def test_symbol_env_var_removes_the_name_from_quarantine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOS_SYMBOL", "US30.cash")
        _write(tmp_path, "symbol: US30\nsymbol_aliases: [US30]\nsymbol_quarantine: [US30.cash]\n")
        assert _load(tmp_path).strategy.instrument_policy.allows("US30.cash")

    def test_environment_flag_parsing_is_case_insensitive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOS_ENVIRONMENT", "demo")
        _write(tmp_path, "symbol: US30\n")
        assert _load(tmp_path).environment.environment is Environment.DEMO

    def test_an_absent_env_file_is_not_an_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write(tmp_path, "symbol: US30\n")
        values, path = load_env_file(tmp_path / "does-not-exist")
        assert values == {}
        assert path is None
        assert _load(tmp_path).strategy.symbol == "US30"


class TestEnvFileParsing:
    def test_parses_comments_blanks_and_export(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# a comment\n\n  \nexport SOS_SYMBOL=US30m\nSOS_MAGIC_NUMBER=7\n",
            encoding="utf-8",
        )
        values, path = load_env_file(env_file)
        assert path == env_file
        assert values == {"SOS_SYMBOL": "US30m", "SOS_MAGIC_NUMBER": "7"}

    def test_strips_matching_quotes(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text('SOS_MT5_PATH="C:\\Program Files\\MT5\\terminal64.exe"\n', encoding="utf-8")
        values, _ = load_env_file(env_file)
        assert values["SOS_MT5_PATH"] == "C:\\Program Files\\MT5\\terminal64.exe"

    def test_error_carries_a_line_number(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("SOS_A=1\nthis is not an assignment\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=r":2:"):
            load_env_file(env_file)

    def test_rejects_an_invalid_variable_name(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("not-a-name=1\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="not a valid variable name"):
            load_env_file(env_file)

    def test_a_password_is_reduced_to_a_presence_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from stop_order_scalp.domain.models import set_password_present

        set_password_present(False)
        env_file = tmp_path / ".env"
        env_file.write_text("SOS_MT5_PASSWORD=hunter2\nSOS_MT5_LOGIN=99\n", encoding="utf-8")
        config = _load(tmp_path, env_file=env_file)
        assert config.environment.password_present is True
        assert config.environment.to_dict()["mt5_password_configured"] is True
        # The value must not be reachable from the settings object at all.
        assert "hunter2" not in repr(config.environment)


class TestRetryBackoff:
    def test_delay_grows_geometrically(self) -> None:
        retry = RetrySettings(base_delay_seconds=1, multiplier=2, max_delay_seconds=100)
        assert [retry.delay_for(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]

    def test_delay_is_capped(self) -> None:
        retry = RetrySettings(base_delay_seconds=1, multiplier=10, max_delay_seconds=5)
        assert retry.delay_for(5) == 5.0

    def test_attempt_below_one_is_rejected(self) -> None:
        with pytest.raises(ConfigError):
            RetrySettings().delay_for(0)

    def test_multiplier_below_one_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="multiplier"):
            RetrySettings(multiplier=0.5)


def _write(directory: Path, content: str) -> Path:
    (directory / "config").mkdir(parents=True, exist_ok=True)
    target = directory / "config" / "default.yaml"
    target.write_text(content, encoding="utf-8")
    return target


#: An ``.env`` path that deliberately does not exist.
#:
#: ``load_config(env_file=None)`` auto-discovers a ``.env`` in the *current working
#: directory* and then merges it into ``os.environ`` with ``setdefault``. A test suite
#: that relies on the default therefore reads the developer's real machine settings, and
#: a validation test silently stops validating anything as soon as the developer has a
#: ``.env``. Passing this path explicitly switches auto-discovery off, because a missing
#: explicit ``.env`` is not an error -- it simply contributes no layer.
NO_ENV_FILE = "this-file-deliberately-does-not-exist.env"


def _load(directory: Path, *, env_file: Path | str | None = NO_ENV_FILE) -> AppConfig:
    config_path = directory / "config" / "default.yaml"
    if not config_path.is_file():
        _write(directory, "symbol: US30\n")
    return load_config(config_path=config_path, env_file=env_file, root=directory)


@pytest.fixture
def config() -> AppConfig:
    """Load the shipped configuration from the repository, isolated from the environment."""
    from stop_order_scalp.infrastructure.config import find_project_root

    root = find_project_root()
    return load_config(
        config_path=root / "config" / "default.yaml", env_file=NO_ENV_FILE, root=root
    )
