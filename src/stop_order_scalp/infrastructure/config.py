"""Validated configuration.

Three layers, resolved in this order (later wins):

1. ``config/default.yaml`` -- strategy parameters. Committed. Never a secret.
2. ``.env`` -- machine-local settings. Git-ignored. May hold credentials.
3. real environment variables -- always beat ``.env``.

Layer 3 beating layer 2 is achieved with ``os.environ.setdefault`` when loading ``.env``,
which means a value exported by a shell or a service manager wins over the file without
this module having to know where either came from.

Design commitments:

* **Unknown keys are errors.** A misspelled ``trailing.distance_point`` that is silently
  ignored is a risk parameter that is not what the operator thinks it is.
* **Validated at construction.** Invalid values raise
  :class:`~stop_order_scalp.domain.exceptions.ConfigError` at load, not at first use.
* **No absolute paths in the repository.** Paths resolve against the project root, which
  is discovered by walking up for ``pyproject.toml`` -- the same technique the sibling
  ``auto-trade`` project uses for ``.env`` discovery.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Final

import yaml

from stop_order_scalp.domain.enums import (
    BreakEvenMode,
    CommissionMode,
    Environment,
    RiskMode,
    TargetMode,
    TimeframeSelection,
)
from stop_order_scalp.domain.exceptions import ConfigError
from stop_order_scalp.domain.models import (
    EnvironmentSettings,
    InstrumentPolicy,
    set_password_present,
)

__all__ = [
    "CONFIG_ENV_PREFIX",
    "DEFAULT_CONFIG_RELATIVE",
    "AppConfig",
    "BreakEvenSettings",
    "EntrySettings",
    "ExecutionSettings",
    "FilterSettings",
    "IntegrationSettings",
    "LoggingSettings",
    "OrderSettings",
    "PathSettings",
    "ProjectPaths",
    "RetrySettings",
    "RiskSettings",
    "StateSettings",
    "StrategySettings",
    "TargetSettings",
    "TrailingSettings",
    "load_config",
    "load_env_file",
]

CONFIG_ENV_PREFIX: Final[str] = "SOS_"

#: The strategy defaults shipped with the repository.
DEFAULT_CONFIG_RELATIVE: Final[str] = "config/default.yaml"

#: Environment variables that hold credentials. Their values are read exactly once, at
#: configuration load, and reduced immediately to a presence flag. Nothing else in the
#: codebase ever reads them, so there is no code path that could put one in a log.
_SECRET_ENV_KEYS: Final[frozenset[str]] = frozenset({"SOS_MT5_PASSWORD"})


# =============================================================================
# Project root discovery
# =============================================================================


def find_project_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` looking for ``pyproject.toml``.

    Falls back to the current working directory. A missing project root is not fatal --
    a system-wide install has no ``pyproject.toml`` above it, and refusing to run would
    be worse than resolving paths against the working directory.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return current


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    """Every filesystem root the application uses, resolved.

    Held in one object so that no other module constructs a path, and so that a test can
    redirect all of them with one substitution.
    """

    root: Path
    config_file: Path
    state_file: Path
    log_directory: Path

    @classmethod
    def resolve(
        cls,
        *,
        root: Path | None = None,
        config_file: Path | None = None,
        state_file: str | Path | None = None,
        log_directory: str | Path | None = None,
    ) -> ProjectPaths:
        base = (root or find_project_root()).resolve()
        return cls(
            root=base,
            config_file=_resolve(base, config_file, DEFAULT_CONFIG_RELATIVE),
            state_file=_resolve(base, state_file, "state/state.json"),
            log_directory=_resolve(base, log_directory, "logs"),
        )


def _resolve(base: Path, value: str | Path | None, fallback: str) -> Path:
    """Absolute paths are honoured; relative paths resolve against ``base``."""
    if value is None:
        return (base / fallback).resolve()
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (base / candidate).resolve()


# =============================================================================
# `.env` loading
# =============================================================================


def load_env_file(path: Path | str | None = None) -> tuple[dict[str, str], Path | None]:
    """Load ``.env`` into ``os.environ`` without overwriting real environment variables.

    Returns the parsed values and the path actually used, so the caller can report which
    file influenced the configuration.

    Resolution order:

    * explicit ``path``
    * ``$SOS_ENV_FILE``
    * ``.env`` in the current working directory
    * ``.env`` at the project root

    Not finding a file is **not** an error: running with pure defaults and pure
    environment variables is a legitimate and common configuration.
    """
    resolved = _resolve_env_file(path)
    if resolved is None:
        return {}, None

    values = _parse_env(resolved)
    for key, value in values.items():
        # setdefault, not assignment: a real environment variable always wins.
        os.environ.setdefault(key, value)

    # Reduce secrets to a flag now. Nothing downstream reads the variable.
    for key in _SECRET_ENV_KEYS:
        if key in values:
            set_password_present(bool(values[key]))

    return values, resolved


def _resolve_env_file(explicit: Path | str | None) -> Path | None:
    if explicit is not None:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None

    from_env = os.environ.get(f"{CONFIG_ENV_PREFIX}ENV_FILE")
    if from_env:
        candidate = Path(from_env).expanduser()
        return candidate if candidate.is_file() else None

    for candidate in (Path.cwd() / ".env", find_project_root() / ".env"):
        if candidate.is_file():
            return candidate.resolve()
    return None


def _parse_env(path: Path) -> dict[str, str]:
    """Parse a minimal ``.env``.

    Supports ``#`` comments, blank lines, an optional ``export`` prefix, ``KEY=VALUE``,
    and matching single or double quotes. Windows paths can therefore be written
    verbatim inside quotes without escaping backslashes.

    Errors carry a line number. A malformed ``.env`` that raises a bare
    ``ValueError`` with no position is indistinguishable from a bug.
    """
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            raise ConfigError(f"{path}:{number}: expected KEY=VALUE, got {raw!r}")
        key = key.strip()
        if not key.isidentifier():
            raise ConfigError(f"{path}:{number}: {key!r} is not a valid variable name")
        values[key] = _unquote(value.strip())
    return values


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value.replace("\\\\", "\\")


# =============================================================================
# Typed settings
# =============================================================================


@dataclass(frozen=True, slots=True)
class EntrySettings:
    timeframe: str = "M1"
    direction_timeframe: str = "M15"
    offset_points: int = 10
    candle_selection: TimeframeSelection = TimeframeSelection.LAST_CLOSED
    lookback: int = 5
    expiry_candles: int | None = None

    def __post_init__(self) -> None:
        if self.timeframe == self.direction_timeframe:
            raise ConfigError(
                f"entry.timeframe and entry.direction_timeframe are both {self.timeframe}; "
                "the strategy needs two distinct timeframes"
            )
        if self.offset_points < 0:
            raise ConfigError(f"entry.offset_points must be non-negative, got {self.offset_points}")
        if self.lookback < 2:
            raise ConfigError(f"entry.lookback must be at least 2, got {self.lookback}")
        if self.expiry_candles is not None and self.expiry_candles < 1:
            raise ConfigError(f"entry.expiry_candles must be null or at least 1, got {self.expiry_candles}")

    @property
    def uses_closed_candles(self) -> bool:
        return self.candle_selection is TimeframeSelection.LAST_CLOSED


@dataclass(frozen=True, slots=True)
class RiskSettings:
    mode: RiskMode = RiskMode.RISK_MODE_PERCENT_BALANCE
    percent: Decimal = Decimal("0.5")
    fixed_lot: Decimal = Decimal("0.10")
    commission_per_lot: Decimal = Decimal("6.0")
    commission_mode: CommissionMode = CommissionMode.PER_LOT_ROUND_TRIP
    refuse_below_min_volume: bool = True
    max_total_risk_fraction: Decimal = Decimal("1.0")

    def __post_init__(self) -> None:
        if self.percent <= 0:
            raise ConfigError(f"risk.percent must be positive, got {self.percent}")
        if self.percent > 100:
            raise ConfigError(f"risk.percent must not exceed 100, got {self.percent}")
        if self.fixed_lot <= 0:
            raise ConfigError(f"risk.fixed_lot must be positive, got {self.fixed_lot}")
        if self.commission_per_lot < 0:
            raise ConfigError("risk.commission_per_lot must be non-negative")
        if not 0 < self.max_total_risk_fraction <= 1:
            raise ConfigError(
                f"risk.max_total_risk_fraction must be in (0, 1], got {self.max_total_risk_fraction}"
            )


@dataclass(frozen=True, slots=True)
class TargetSettings:
    mode: TargetMode = TargetMode.TARGET_MODE_FIXED_POINTS
    take_profit_points: int = 1000
    risk_reward: Decimal = Decimal("1.0")
    stop_loss_points: int = 100

    def __post_init__(self) -> None:
        if self.take_profit_points <= 0:
            raise ConfigError(f"target.take_profit_points must be positive, got {self.take_profit_points}")
        if self.risk_reward <= 0:
            raise ConfigError(f"target.risk_reward must be positive, got {self.risk_reward}")
        if self.stop_loss_points <= 0:
            raise ConfigError(f"target.stop_loss_points must be positive, got {self.stop_loss_points}")


@dataclass(frozen=True, slots=True)
class BreakEvenSettings:
    enabled: bool = True
    trigger_points: int = 50
    mode: BreakEvenMode = BreakEvenMode.BREAK_EVEN_MODE_ENTRY
    commission_points: int = 0

    def __post_init__(self) -> None:
        if self.trigger_points < 0:
            raise ConfigError(f"break_even.trigger_points must be non-negative, got {self.trigger_points}")
        if self.commission_points < 0:
            raise ConfigError("break_even.commission_points must be non-negative")
        if self.enabled and self.trigger_points == 0:
            # Legal, but it means the stop moves on the tick the position opens, which the
            # specification explicitly warns against. Refuse rather than guess.
            raise ConfigError(
                "break_even.trigger_points is 0 with break_even.enabled true; that moves the "
                "stop to entry the instant the position opens. Set a positive trigger or "
                "disable break-even."
            )


@dataclass(frozen=True, slots=True)
class TrailingSettings:
    enabled: bool = True
    distance_points: int = 100
    arm_after_points: int = 0
    min_step_points: int = 1

    def __post_init__(self) -> None:
        if self.distance_points <= 0:
            raise ConfigError(f"trailing.distance_points must be positive, got {self.distance_points}")
        if self.arm_after_points < 0:
            raise ConfigError("trailing.arm_after_points must be non-negative")
        if self.min_step_points < 0:
            raise ConfigError("trailing.min_step_points must be non-negative")


@dataclass(frozen=True, slots=True)
class OrderSettings:
    lifetime_seconds: int | None = None
    deviation_points: int = 20
    filling_policy: str = "FOK"
    min_stop_points: int = 0

    def __post_init__(self) -> None:
        if self.lifetime_seconds is not None and self.lifetime_seconds <= 0:
            raise ConfigError("order.lifetime_seconds must be null or positive")
        if self.deviation_points < 0:
            raise ConfigError("order.deviation_points must be non-negative")
        if self.min_stop_points < 0:
            raise ConfigError("order.min_stop_points must be non-negative")
        if self.filling_policy not in ("FOK", "IOC", "RETURN"):
            raise ConfigError(f"order.filling_policy must be FOK, IOC or RETURN, got {self.filling_policy!r}")

    @property
    def lifetime(self) -> timedelta | None:
        return None if self.lifetime_seconds is None else timedelta(seconds=self.lifetime_seconds)


@dataclass(frozen=True, slots=True)
class RetrySettings:
    """Bounded exponential backoff, for *safe* operations only.

    An order send whose outcome is unknown is not a retryable operation. It goes to
    :class:`~stop_order_scalp.domain.exceptions.ExecutionUnknownError` and is handled by
    re-observing broker state.
    """

    max_attempts: int = 5
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0
    multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ConfigError(f"execution.retry.max_attempts must be at least 1, got {self.max_attempts}")
        if self.base_delay_seconds <= 0:
            raise ConfigError("execution.retry.base_delay_seconds must be positive")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ConfigError("execution.retry.max_delay_seconds must be >= base_delay_seconds")
        if self.multiplier < 1:
            raise ConfigError("execution.retry.multiplier must be >= 1")

    def delay_for(self, attempt: int) -> float:
        """Delay before attempt number ``attempt`` (1-based), capped at the maximum.

        No jitter. The delays here apply to a single-threaded poll loop against one
        broker, so spreading them would add randomness without reducing contention.
        """
        if attempt < 1:
            raise ConfigError(f"attempt must be at least 1, got {attempt}")
        raw = self.base_delay_seconds * (self.multiplier ** (attempt - 1))
        return min(raw, self.max_delay_seconds)


@dataclass(frozen=True, slots=True)
class ExecutionSettings:
    magic_number: int = 20260930
    poll_interval_seconds: float = 1.0
    retry: RetrySettings = field(default_factory=RetrySettings)

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ConfigError("execution.poll_interval_seconds must be positive")
        if not 0 <= self.magic_number < 2**32:
            raise ConfigError(f"execution.magic_number must fit in 32 bits, got {self.magic_number}")


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    level: str = "INFO"
    directory: str = "logs"

    def __post_init__(self) -> None:
        if self.level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError(f"logging.level is not a valid level, got {self.level!r}")


@dataclass(frozen=True, slots=True)
class StateSettings:
    path: str = "state/state.json"
    journal_limit: int = 500

    def __post_init__(self) -> None:
        if self.journal_limit < 1:
            raise ConfigError("state.journal_limit must be at least 1")


@dataclass(frozen=True, slots=True)
class AlBrooksSettings:
    enabled: bool = False
    allow_geometry: bool = False

    def __post_init__(self) -> None:
        if self.allow_geometry and not self.enabled:
            raise ConfigError(
                "integrations.al_brooks.allow_geometry requires integrations.al_brooks.enabled"
            )


@dataclass(frozen=True, slots=True)
class IntegrationSettings:
    al_brooks: AlBrooksSettings = field(default_factory=AlBrooksSettings)


@dataclass(frozen=True, slots=True)
class FilterSettings:
    """Optional, disabled-by-default filters.

    Every field is a "no" by default. With all of them at their defaults the strategy is
    byte-for-byte the baseline, which is what makes Phase 12 research additive rather
    than a silent alteration of the original rules.
    """

    max_spread_points: int = 0
    min_atr_points: int = 0
    max_atr_points: int = 0
    min_m1_range_points: int = 0
    max_m1_range_points: int = 0
    min_m15_body_ratio: Decimal = Decimal("0")
    sessions: tuple[str, ...] = ()
    one_trade_per_candle: bool = False

    def __post_init__(self) -> None:
        for name in (
            "max_spread_points",
            "min_atr_points",
            "max_atr_points",
            "min_m1_range_points",
            "max_m1_range_points",
        ):
            if getattr(self, name) < 0:
                raise ConfigError(f"filters.{name} must be non-negative")
        if self.min_atr_points and self.max_atr_points and self.min_atr_points > self.max_atr_points:
            raise ConfigError("filters.min_atr_points must not exceed filters.max_atr_points")
        if (
            self.min_m1_range_points
            and self.max_m1_range_points
            and self.min_m1_range_points > self.max_m1_range_points
        ):
            raise ConfigError("filters.min_m1_range_points must not exceed filters.max_m1_range_points")
        if not 0 <= self.min_m15_body_ratio <= 1:
            raise ConfigError("filters.min_m15_body_ratio must be within [0, 1]")
        for session in self.sessions:
            if session not in ("london", "new_york", "us_cash_open", "us_late", "asian"):
                raise ConfigError(
                    f"filters.sessions contains an unknown session {session!r}; "
                    "valid values are london, new_york, us_cash_open, us_late, asian"
                )

    @property
    def any_enabled(self) -> bool:
        """Whether any filter would change behaviour. Reported by ``status``."""
        return (
            self.max_spread_points > 0
            or self.min_atr_points > 0
            or self.max_atr_points > 0
            or self.min_m1_range_points > 0
            or self.max_m1_range_points > 0
            or self.min_m15_body_ratio > 0
            or bool(self.sessions)
            or self.one_trade_per_candle
        )


@dataclass(frozen=True, slots=True)
class StrategySettings:
    """The complete strategy parameter set.

    Grouped into one frozen object so a component can be handed exactly the settings it
    needs and nothing more, and so a test can substitute a whole alternative strategy
    configuration with one argument.
    """

    symbol: str = "US30"
    symbol_aliases: tuple[str, ...] = ("US30",)
    symbol_quarantine: tuple[str, ...] = ()
    entry: EntrySettings = field(default_factory=EntrySettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    target: TargetSettings = field(default_factory=TargetSettings)
    stop_loss_mode: str = "fixed_points"
    break_even: BreakEvenSettings = field(default_factory=BreakEvenSettings)
    trailing: TrailingSettings = field(default_factory=TrailingSettings)
    order: OrderSettings = field(default_factory=OrderSettings)
    filters: FilterSettings = field(default_factory=FilterSettings)

    def __post_init__(self) -> None:
        if not self.symbol or not self.symbol.strip():
            raise ConfigError("symbol must not be empty")
        if self.stop_loss_mode not in ("fixed_points", "risk_reward", "signal_defined"):
            raise ConfigError(
                f"stop_loss.mode must be fixed_points, risk_reward or signal_defined, "
                f"got {self.stop_loss_mode!r}"
            )
        if not self.symbol_aliases:
            raise ConfigError("symbol_aliases must not be empty")
        overlap = {a.upper() for a in self.symbol_aliases} & {q.upper() for q in self.symbol_quarantine}
        if overlap:
            raise ConfigError(f"symbols appear in both aliases and quarantine: {sorted(overlap)}")

    @property
    def instrument_policy(self) -> InstrumentPolicy:
        return InstrumentPolicy(
            logical_symbol=self.symbol,
            accepted_names=frozenset(self.symbol_aliases),
            quarantine=frozenset(self.symbol_quarantine),
        )

    def __str__(self) -> str:
        return (
            f"StrategySettings({self.symbol} {self.entry.direction_timeframe}->"
            f"{self.entry.timeframe} offset={self.entry.offset_points} "
            f"risk={self.risk.mode} target={self.target.mode})"
        )


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Everything the application needs, fully resolved.

    :attr:`strategy` is the part that is safe to log and to diff.
    :attr:`environment` is the part that is not, and whose ``to_dict`` reports only
    presence flags.
    """

    strategy: StrategySettings
    environment: EnvironmentSettings
    execution: ExecutionSettings
    logging: LoggingSettings
    state: StateSettings
    integrations: IntegrationSettings
    paths: ProjectPaths
    #: Which files contributed, for ``validate-config`` and ``diagnostics``.
    sources: tuple[Path, ...] = ()

    @property
    def magic_number(self) -> int:
        return self.execution.magic_number

    def with_strategy(self, strategy: StrategySettings) -> AppConfig:
        """Return a copy with a different strategy. Used by tests and research sweeps."""
        return replace(self, strategy=strategy)

    def summary(self) -> dict[str, Any]:
        """Log-safe summary. Contains no credential, by construction."""
        return {
            "strategy": {
                "symbol": self.strategy.symbol,
                "symbol_aliases": list(self.strategy.symbol_aliases),
                "entry": {
                    "timeframe": self.strategy.entry.timeframe,
                    "direction_timeframe": self.strategy.entry.direction_timeframe,
                    "offset_points": self.strategy.entry.offset_points,
                    "candle_selection": str(self.strategy.entry.candle_selection),
                },
                "risk": {
                    "mode": str(self.strategy.risk.mode),
                    "percent": str(self.strategy.risk.percent),
                    "commission_per_lot": str(self.strategy.risk.commission_per_lot),
                    "commission_mode": str(self.strategy.risk.commission_mode),
                },
                "target": {
                    "mode": str(self.strategy.target.mode),
                    "take_profit_points": self.strategy.target.take_profit_points,
                    "risk_reward": str(self.strategy.target.risk_reward),
                },
                "trailing": {
                    "enabled": self.strategy.trailing.enabled,
                    "distance_points": self.strategy.trailing.distance_points,
                },
                "break_even": {
                    "enabled": self.strategy.break_even.enabled,
                    "trigger_points": self.strategy.break_even.trigger_points,
                },
                "filters_enabled": self.strategy.filters.any_enabled,
            },
            "environment": self.environment.to_dict(),
            "execution": {
                "magic_number": self.execution.magic_number,
                "poll_interval_seconds": self.execution.poll_interval_seconds,
                "retry_max_attempts": self.execution.retry.max_attempts,
            },
            "sources": [str(source) for source in self.sources],
        }


# =============================================================================
# Loading
# =============================================================================


def load_config(
    *,
    config_path: Path | str | None = None,
    env_file: Path | str | None = None,
    root: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> AppConfig:
    """Resolve the full configuration.

    :param config_path: strategy YAML. ``None`` means the project default.
    :param env_file: ``.env`` location. ``None`` means auto-discover.
    :param root: project root for relative path resolution. ``None`` means auto-discover.
    :param overrides: environment-variable overrides, as ``{"RISK.PERCENT": "0.25"}``.
        Applied last, after ``.env``. Used by tests and by the CLI's ``--set`` flag.
    :raises ConfigError: on a missing, malformed, or semantically invalid configuration.
    """
    discovered_root = root or find_project_root()
    strategy_yaml = _resolve(discovered_root, config_path, DEFAULT_CONFIG_RELATIVE)

    if not strategy_yaml.is_file():
        raise ConfigError(
            f"strategy configuration not found at {strategy_yaml}. "
            "Pass --config or SOS_CONFIG_PATH, or restore config/default.yaml."
        )

    raw = _read_yaml(strategy_yaml)
    _reject_unknown_keys(raw, strategy_yaml)

    env_values, env_path = load_env_file(env_file)

    # ``load_env_file`` used ``setdefault``, so ``os.environ`` now holds the merge of the
    # file and the real environment, with the real environment winning. Reading it back is
    # therefore the authoritative view of this layer -- reading ``env_values`` alone would
    # silently ignore every variable exported by a shell or a service manager.
    layer: dict[str, Any] = {
        key: value for key, value in os.environ.items() if key.startswith(CONFIG_ENV_PREFIX)
    }
    if overrides:
        layer.update(overrides)

    _apply_env_overrides(raw, layer, strategy_yaml)

    sources = [strategy_yaml] + ([env_path] if env_path else [])

    strategy = _build_strategy(raw)
    logging_settings = _coerce(LoggingSettings, dict(raw.get("logging") or {}), "logging")
    state_settings = _coerce(StateSettings, dict(raw.get("state") or {}), "state")
    integrations = _build_integrations(raw)

    paths = ProjectPaths.resolve(
        root=discovered_root,
        config_file=strategy_yaml,
        state_file=state_settings.path,
        log_directory=logging_settings.directory,
    )

    environment = _build_environment(
        layer, default_magic=int((raw.get("execution") or {}).get("magic_number", 20260930))
    )
    execution = _build_execution(raw, environment)

    return AppConfig(
        strategy=strategy,
        environment=environment,
        execution=execution,
        logging=logging_settings,
        state=state_settings,
        integrations=integrations,
        paths=paths,
        sources=tuple(sources),
    )


def _build_execution(raw: Mapping[str, Any], environment: EnvironmentSettings) -> ExecutionSettings:
    """Build execution settings.

    The magic number is deliberately single-sourced from the environment layer, and the
    environment's own default is the YAML's value. A value that disagrees between the two
    files *and* has ``SOS_MAGIC_NUMBER`` set is refused outright, because orders placed
    under one magic number are invisible to a system looking for the other -- and that is
    the most common cause of "it cannot find its own orders after a restart".
    """
    payload = dict(raw.get("execution") or {})
    retry_payload = dict(payload.pop("retry", None) or {})

    yaml_magic = payload.pop("magic_number", None)
    if yaml_magic is not None and _env_supplied("MAGIC_NUMBER"):
        if int(yaml_magic) != environment.magic_number:
            raise ConfigError(
                f"magic_number disagrees: execution.magic_number={yaml_magic} but "
                f"{CONFIG_ENV_PREFIX}MAGIC_NUMBER={environment.magic_number}. Orders placed "
                "under one magic number are invisible to the other; pick one."
            )

    payload["magic_number"] = environment.magic_number
    return _coerce(
        ExecutionSettings, {**payload, "retry": _coerce(RetrySettings, retry_payload, "execution.retry")},
        "execution",
    )


def _env_supplied(name: str) -> bool:
    return f"{CONFIG_ENV_PREFIX}{name}" in os.environ


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot be read: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path}: top level must be a mapping, got {type(loaded).__name__}")
    return loaded


def _reject_unknown_keys(raw: Mapping[str, Any], path: Path) -> None:
    """Fail on any key the loader does not know.

    A silently ignored misspelled risk parameter is the worst class of configuration
    bug: the system runs, trades, and is not doing what the file says.
    """
    allowed: set[str] = {
        "symbol",
        "symbol_aliases",
        "symbol_quarantine",
        "entry",
        "risk",
        "target",
        "stop_loss",
        "break_even",
        "trailing",
        "order",
        "execution",
        "logging",
        "state",
        "integrations",
        "filters",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"{path}: unknown configuration keys {unknown}. Fix or remove them.")

    nested_allowed: dict[str, set[str]] = {
        "entry": {"timeframe", "direction_timeframe", "offset_points", "candle_selection", "lookback", "expiry_candles"},
        "risk": {
            "mode",
            "percent",
            "fixed_lot",
            "commission_per_lot",
            "commission_mode",
            "refuse_below_min_volume",
            "max_total_risk_fraction",
        },
        "target": {"mode", "take_profit_points", "risk_reward", "stop_loss_points"},
        "stop_loss": {"mode"},
        "break_even": {"enabled", "trigger_points", "mode", "commission_points"},
        "trailing": {"enabled", "distance_points", "arm_after_points", "min_step_points"},
        "order": {"lifetime_seconds", "deviation_points", "filling_policy", "min_stop_points"},
        "execution": {"magic_number", "poll_interval_seconds", "retry"},
        "logging": {"level", "directory"},
        "state": {"path", "journal_limit"},
        "integrations": {"al_brooks"},
        "filters": {
            "max_spread_points",
            "min_atr_points",
            "max_atr_points",
            "min_m1_range_points",
            "max_m1_range_points",
            "min_m15_body_ratio",
            "sessions",
            "one_trade_per_candle",
        },
    }
    for section, keys in nested_allowed.items():
        value = raw.get(section)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: '{section}' must be a mapping, got {type(value).__name__}")
        unknown_nested = sorted(set(value) - keys)
        if unknown_nested:
            raise ConfigError(f"{path}: unknown keys under '{section}': {unknown_nested}")

    integrations_raw = raw.get("integrations")
    if isinstance(integrations_raw, dict):
        al_brooks = integrations_raw.get("al_brooks")
        if al_brooks is not None:
            if not isinstance(al_brooks, dict):
                raise ConfigError(f"{path}: 'integrations.al_brooks' must be a mapping")
            unknown_al = sorted(set(al_brooks) - {"enabled", "allow_geometry"})
            if unknown_al:
                raise ConfigError(f"{path}: unknown keys under 'integrations.al_brooks': {unknown_al}")

    execution_raw = raw.get("execution")
    if isinstance(execution_raw, dict):
        retry = execution_raw.get("retry")
        if retry is not None:
            if not isinstance(retry, dict):
                raise ConfigError(f"{path}: 'execution.retry' must be a mapping")
            unknown_retry = sorted(
                set(retry) - {"max_attempts", "base_delay_seconds", "max_delay_seconds", "multiplier"}
            )
            if unknown_retry:
                raise ConfigError(f"{path}: unknown keys under 'execution.retry': {unknown_retry}")


def _apply_env_overrides(raw: dict[str, Any], values: Mapping[str, Any], path: Path) -> None:
    """Apply ``SOS_*`` overrides onto the parsed YAML, in place.

    Recognised shapes:

    * ``SOS_SYMBOL`` -> ``symbol`` (and adds the name to ``symbol_aliases``)
    * ``SOS_MAGIC_NUMBER`` -> ``execution.magic_number``
    * ``SOS_RISK_PERCENT`` -> ``risk.percent``

    A dotted form also works (``SOS_STRATEGY_RISK.PERCENT``), and is tried first.
    Overrides are walked against the *existing* tree, so an override can never introduce
    a key the schema does not know -- a typo in an env var is a no-op rather than a
    silent corruption. Unknown ``SOS_`` keys that do not correspond to a strategy
    parameter are machine-local settings, which is the majority of them.
    """
    for key, value in sorted(values.items()):
        if not key.startswith(CONFIG_ENV_PREFIX):
            continue
        if key in _SECRET_ENV_KEYS:
            continue
        for dotted in _override_candidates(key[len(CONFIG_ENV_PREFIX) :], raw):
            if _set_dotted(raw, dotted, value):
                break

    # Symbol aliases are structural, not scalar, so they get dedicated handling.
    aliases = values.get(f"{CONFIG_ENV_PREFIX}SYMBOL_ALIASES")
    if aliases is not None:
        raw["symbol_aliases"] = [part.strip() for part in str(aliases).split(",") if part.strip()]
    quarantine = values.get(f"{CONFIG_ENV_PREFIX}SYMBOL_QUARANTINE")
    if quarantine is not None:
        raw["symbol_quarantine"] = [part.strip() for part in str(quarantine).split(",") if part.strip()]

    symbol = values.get(f"{CONFIG_ENV_PREFIX}SYMBOL")
    if symbol is not None:
        configured = str(symbol).strip()
        raw["symbol"] = configured
        # The broker's own name is always an accepted alias. Without this, pointing
        # SOS_SYMBOL at the broker's spelling would select an instrument that the
        # instrument policy then refuses to trade.
        existing = [str(item) for item in raw.get("symbol_aliases", [])]
        if configured.upper() not in {item.upper() for item in existing}:
            existing.append(configured)
        raw["symbol_aliases"] = existing
        # An approved name can never also be quarantined.
        raw["symbol_quarantine"] = [
            item for item in raw.get("symbol_quarantine", []) if str(item).upper() != configured.upper()
        ]


def _override_candidates(remainder: str, raw: Mapping[str, Any]) -> list[str]:
    """Dotted paths a ``SOS_`` remainder might address, most specific first.

    Two spellings are supported:

    * ``SOS_TRAILING_DISTANCE_POINTS`` -> ``trailing.distance_points``
    * ``SOS_EXECUTION_RETRY_MAX_ATTEMPTS`` -> ``execution.retry.max_attempts``

    A naive ``_`` -> ``.`` replacement cannot work, because a parameter name may itself
    contain an underscore (``distance_points``). So the candidate paths are built from the
    keys that actually exist in the document: the index knows that ``trailing`` has a
    child called ``distance_points``, and therefore that ``TRAILING_DISTANCE_POINTS``
    addresses it. The resolution is derived from the schema rather than guessed, so an
    env var either matches a real parameter exactly or matches nothing.
    """
    candidates: list[str] = []
    for key in (remainder, remainder.removeprefix("STRATEGY_")):
        if not key:
            continue
        dotted = _resolve_against_index(key, _leaf_index(raw))
        if dotted is not None:
            candidates.append(dotted)
    # Last resort, so an explicit dotted spelling still works even for a path the index
    # has not seen (for example a section that exists only via an earlier override).
    candidates.append(remainder.lower())
    return candidates


def _leaf_index(raw: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """Map ``"TRAILING.DISTANCE_POINTS"`` to ``["trailing", "distance_points"]``.

    Keys are upper-cased with the path separators removed, so lookups are
    case-insensitive and separator-agnostic. Shallower paths win on collision, because a
    top-level scalar is a more specific target than a nested one.
    """
    index: dict[str, list[str]] = {}
    for key, value in raw.items():
        path = (*prefix, key)
        flat = "_".join(part.upper() for part in path)
        existing = index.get(flat)
        if existing is None or len(path) < len(existing):
            index[flat] = list(path)
        if isinstance(value, dict):
            for nested_key, nested in _leaf_index(value, path).items():
                if nested_key not in index or len(index[nested_key]) > len(nested):
                    index[nested_key] = nested
    return index


def _resolve_against_index(key: str, index: Mapping[str, list[str]]) -> str | None:
    for spelling in (key, key.replace(".", "_"), key.replace("_", ".")):
        match = index.get(spelling.upper())
        if match is not None:
            return ".".join(match)
    return None


def _set_dotted(raw: dict[str, Any], dotted: str, value: Any) -> bool:
    """Set ``raw["a"]["b"]`` from ``"a.b"``. Returns ``False`` if no such path exists.

    Walking the existing tree rather than creating it means an override can never
    introduce a key the schema does not know, so a typo in an env var is a no-op rather
    than a silent corruption. Key lookup is case-insensitive because environment variable
    names are.
    """
    parts = [part for part in dotted.split(".") if part]
    if not parts:
        return False
    node: Any = raw
    for part in parts[:-1]:
        if not isinstance(node, dict):
            return False
        match = _lookup(node, part)
        if match is None:
            return False
        node = node[match]
    if not isinstance(node, dict):
        return False
    leaf = _lookup(node, parts[-1])
    if leaf is None:
        return False
    node[leaf] = value
    return True


def _lookup(node: dict[str, Any], name: str) -> str | None:
    """Case-insensitive key lookup. Returns the actual key, or ``None``."""
    if name in node:
        return name
    lowered = name.lower()
    for key in node:
        if key.lower() == lowered:
            return key
    return None


def _build_strategy(raw: Mapping[str, Any]) -> StrategySettings:
    return StrategySettings(
        symbol=str(raw.get("symbol", "US30")),
        symbol_aliases=tuple(str(item) for item in raw.get("symbol_aliases", ("US30",))),
        symbol_quarantine=tuple(str(item) for item in raw.get("symbol_quarantine", ())),
        entry=_coerce(EntrySettings, dict(raw.get("entry") or {}), "entry"),
        risk=_coerce(RiskSettings, dict(raw.get("risk") or {}), "risk"),
        target=_coerce(TargetSettings, dict(raw.get("target") or {}), "target"),
        stop_loss_mode=str((raw.get("stop_loss") or {}).get("mode", "fixed_points")),
        break_even=_coerce(BreakEvenSettings, dict(raw.get("break_even") or {}), "break_even"),
        trailing=_coerce(TrailingSettings, dict(raw.get("trailing") or {}), "trailing"),
        order=_coerce(OrderSettings, dict(raw.get("order") or {}), "order"),
        filters=_coerce(FilterSettings, dict(raw.get("filters") or {}), "filters"),
    )


def _build_integrations(raw: Mapping[str, Any]) -> IntegrationSettings:
    al_brooks = _coerce(
        AlBrooksSettings, dict((raw.get("integrations") or {}).get("al_brooks") or {}), "integrations.al_brooks"
    )
    return IntegrationSettings(al_brooks=al_brooks)


def _build_environment(values: Mapping[str, Any], *, default_magic: int) -> EnvironmentSettings:
    return EnvironmentSettings(
        environment=_enum(values.get(f"{CONFIG_ENV_PREFIX}ENVIRONMENT"), Environment, Environment.DRY_RUN),
        allow_live=_flag(values.get(f"{CONFIG_ENV_PREFIX}ALLOW_LIVE")),
        allow_order=_flag(values.get(f"{CONFIG_ENV_PREFIX}ALLOW_ORDER")),
        allow_close=_flag(values.get(f"{CONFIG_ENV_PREFIX}ALLOW_CLOSE")),
        symbol=str(values.get(f"{CONFIG_ENV_PREFIX}SYMBOL", "US30")),
        magic_number=_int(values.get(f"{CONFIG_ENV_PREFIX}MAGIC_NUMBER"), default_magic),
        mt5_path=_optional_str(values.get(f"{CONFIG_ENV_PREFIX}MT5_PATH")),
        mt5_login=_optional_int(values.get(f"{CONFIG_ENV_PREFIX}MT5_LOGIN")),
        mt5_server=_optional_str(values.get(f"{CONFIG_ENV_PREFIX}MT5_SERVER")),
        mt5_timeout_ms=_int(values.get(f"{CONFIG_ENV_PREFIX}MT5_TIMEOUT_MS"), 60000),
    )


# =============================================================================
# Coercion helpers
# =============================================================================

_TRUE: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def _flag(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


def _int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"expected an integer, got {value!r}") from exc


def _optional_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return _int(value, 0)


def _optional_str(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return str(value).strip()


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _enum(value: Any, enum_type: type, default: Any) -> Any:
    """Parse an enum member from text, case-insensitively.

    Every enum in this project uses upper-case values, so upper-casing the input is a
    lossless convenience rather than a guess: ``SOS_ENVIRONMENT=demo`` is unambiguous.
    """
    if value is None or value == "":
        return default
    text = str(value).strip().upper()
    for member in enum_type:  # type: ignore[attr-defined]
        if str(member.value).upper() == text:
            return member
    options = ", ".join(str(member.value) for member in enum_type)  # type: ignore[attr-defined]
    raise ConfigError(f"expected one of [{options}], got {value!r}")


def _coerce(factory: type, payload: Mapping[str, Any], section: str) -> Any:
    """Build a settings dataclass from raw YAML, coercing types and reporting cleanly.

    Every failure names the section, because "invalid literal for int()" with a bare
    traceback is not an operator-friendly error message. Types are resolved through
    :func:`typing.get_type_hints` so that ``from __future__ import annotations`` -- which
    turns annotations into strings -- does not defeat the coercion.
    """
    import typing

    hints = typing.get_type_hints(factory)
    kwargs: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in hints:
            raise ConfigError(f"unknown key {key!r} under '{section}'")
        kwargs[key] = _coerce_value(key, value, hints[key], section)
    try:
        return factory(**kwargs)
    except ConfigError:
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid configuration under '{section}': {exc}") from exc


def _coerce_value(key: str, value: Any, annotation: Any, section: str) -> Any:
    import typing

    if annotation is not bool and value is None:
        return None
    if typing.get_origin(annotation) in (tuple, list, set) or annotation in (tuple, list, set):
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",") if part.strip()]
        elif isinstance(value, (list, tuple, set)):
            parts = list(value)
        else:
            raise ConfigError(f"{section}.{key} must be a list, got {value!r}")
        return tuple(parts)
    if annotation is int:
        try:
            return int(str(value).strip())
        except ValueError as exc:
            raise ConfigError(f"{section}.{key} must be an integer, got {value!r}") from exc
    if annotation is float:
        try:
            return float(str(value))
        except ValueError as exc:
            raise ConfigError(f"{section}.{key} must be a number, got {value!r}") from exc
    if annotation is Decimal:
        try:
            return _decimal(value)
        except Exception as exc:  # noqa: BLE001 - decimal raises several unrelated types
            raise ConfigError(f"{section}.{key} must be a decimal, got {value!r}") from exc
    if annotation is bool:
        return _flag(value)
    if annotation is str:
        return str(value)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return _enum(value, annotation, None)
    if annotation is dict:
        if not isinstance(value, dict):
            raise ConfigError(f"{section}.{key} must be a mapping, got {value!r}")
        return value
    return value