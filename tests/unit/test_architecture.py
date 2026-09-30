"""Tests for the architecture boundary enforcer itself.

A gate that cannot fail is worse than no gate, because it is trusted. The positive case
-- the repository is currently clean -- is one assertion; the rest of this module feeds
deliberately broken source through the checker and asserts that each rule fires. Without
those, a refactor of ``scripts/check_architecture.py`` could quietly stop checking
anything while the suite stayed green.

The last three tests are regressions for defects found in Phase 1 stabilisation, each of
which silently disabled part of the gate:

* ``ast.unparse()`` cannot render the ``None`` annotation of an un-annotated parameter,
  so the float-money check used to raise ``AttributeError`` on almost every module and
  the whole script crashed.
* The wall-clock allowlist held unqualified module names while ``module_name()`` returns
  fully qualified ones, so no module was ever actually exempt.
* The naive-datetime rule matched ``datetime.now(UTC)``, which is timezone-aware and
  therefore legal, flagging the project's own sanctioned ``utc_now()`` seam.
"""

from __future__ import annotations

import ast
from pathlib import Path

import check_architecture as arch

# =============================================================================
# The repository must satisfy its own rules
# =============================================================================


class TestRepositoryIsClean:
    def test_no_violations_in_the_current_source_tree(self) -> None:
        assert arch.run() == []

    def test_the_enumerator_actually_finds_modules(self) -> None:
        # Guards the test above: if iter_sources ever returns nothing, run() is vacuous.
        assert len(list(arch.iter_sources())) > 20

    def test_the_script_runs_as_a_standalone_command(self) -> None:
        assert arch.main() == 0

    def test_module_names_are_fully_qualified(self) -> None:
        assert arch.module_name(arch.SRC / "stop_order_scalp" / "cli" / "main.py") == (
            "stop_order_scalp.cli.main"
        )

    def test_a_package_init_names_its_package_not_the_init_module(self) -> None:
        assert arch.module_name(arch.SRC / "stop_order_scalp" / "risk" / "__init__.py") == (
            "stop_order_scalp.risk"
        )


# =============================================================================
# Helpers for the negative cases
# =============================================================================


def _write(root: Path, layer: str, source: str, *, filename: str = "offender.py") -> Path:
    """Write ``source`` as a module inside ``layer`` and return its path.

    ``filename`` matters for the allowlist rules: the checker identifies a module by its
    path, so a synthetic file has to carry the permitted module's name to exercise the
    "allowed" branch rather than the "rejected" one.
    """
    directory = root / "stop_order_scalp" / layer
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(source, encoding="utf-8")
    return path


def _rules(path: Path, root: Path) -> set[str]:
    """The set of rule names ``check`` reports for one synthetic file."""
    return {violation.rule for violation in arch.check(path, root)}


def _clock_rules(source: str, module: str) -> set[str]:
    tree = ast.parse(source)
    return {violation.rule for violation in arch.check_clock_usage(Path("synthetic.py"), tree, module)}


# =============================================================================
# Rule 1: MetaTrader5 is confined to two modules, and imported lazily
# =============================================================================


class TestMetaTrader5Boundary:
    def test_mt5_import_in_a_third_party_module_is_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "strategy", "import MetaTrader5\n")
        assert "mt5-import" in _rules(path, tmp_path)

    def test_mt5_import_at_module_scope_is_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "execution", "import MetaTrader5\n", filename="mt5_broker.py")
        assert _rules(path, tmp_path) == {"mt5-eager-import"}

    def test_mt5_import_inside_a_function_is_allowed(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "execution",
            "def load():\n    import MetaTrader5\n    return MetaTrader5\n",
            filename="mt5_broker.py",
        )
        assert arch.check(path, tmp_path) == []

    def test_only_the_named_modules_are_permitted(self) -> None:
        # Regression: the allowlist must be spelled the way module_name() returns it, or
        # the rule rejects the permitted modules as well and permits nothing.
        # mt5_module is the one that owns the `import MetaTrader5` statement; the other
        # two reach the terminal through it.
        assert frozenset(
            {
                "stop_order_scalp.market_data.mt5_module",
                "stop_order_scalp.market_data.mt5_feed",
                "stop_order_scalp.execution.mt5_broker",
            }
        ) == arch.MT5_ALLOWED

    def test_the_import_statement_lives_in_exactly_one_market_data_module(self) -> None:
        # Two market_data files naming the terminal directly is one too many for a
        # reviewer to hold in their head, which is the point of routing everything through
        # MT5Module.
        for name in sorted(arch.MT5_ALLOWED):
            if not name.startswith("stop_order_scalp.market_data."):
                continue
            relative = Path(*name.split(".")).with_suffix(".py")
            source = (arch.SRC / relative).read_text(encoding="utf-8")
            if name.endswith("mt5_module"):
                assert "import MetaTrader5" in source, name
            else:
                assert "import MetaTrader5" not in source, name


# =============================================================================
# Rule 2: albrooks is confined to the adapter
# =============================================================================


class TestAlBrooksBoundary:
    def test_albrooks_import_outside_the_adapter_is_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "strategy", "import albrooks\n")
        assert "albrooks-import" in _rules(path, tmp_path)

    def test_albrooks_import_inside_the_adapter_is_allowed(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "integrations",
            "def load():\n    import albrooks\n    return albrooks\n",
            filename="al_brooks_adapter.py",
        )
        assert arch.check(path, tmp_path) == []

    def test_the_adapter_is_the_only_permitted_module(self) -> None:
        assert frozenset(
            {"stop_order_scalp.integrations.al_brooks_adapter"}
        ) == arch.AL_BROOKS_ALLOWED


# =============================================================================
# Rule 3: layer direction
# =============================================================================


class TestLayerDirection:
    def test_domain_may_not_import_infrastructure(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "domain",
            "from stop_order_scalp.infrastructure.config import AppConfig\n",
        )
        assert _rules(path, tmp_path)

    def test_strategy_may_not_import_execution(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path, "strategy", "from stop_order_scalp.execution.broker import Broker\n"
        )
        assert _rules(path, tmp_path)

    def test_risk_may_not_import_the_cli(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "risk", "from stop_order_scalp.cli.main import main\n")
        assert _rules(path, tmp_path)

    def test_market_data_may_not_import_strategy(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path, "market_data", "from stop_order_scalp.strategy.entry_rules import rule\n"
        )
        assert _rules(path, tmp_path)

    def test_a_layer_may_import_itself(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path, "strategy", "from stop_order_scalp.strategy.entry_rules import rule\n"
        )
        assert arch.check(path, tmp_path) == []

    def test_the_broker_free_layers_are_the_documented_five(self) -> None:
        assert frozenset(
            {"domain", "strategy", "risk", "trailing", "lifecycle"}
        ) == arch.BROKER_FREE_LAYERS


# =============================================================================
# Rule 4: no float for money
# =============================================================================


class TestFloatMoney:
    def test_a_float_price_annotation_is_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "risk", "def f() -> None:\n    price: float = 1.0\n")
        assert "float-money" in _rules(path, tmp_path)

    def test_a_float_commission_annotation_is_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "risk", "def f(commission: float) -> None: ...\n")
        assert "float-money" in _rules(path, tmp_path)

    def test_a_float_ratio_is_allowed(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "risk", "def f() -> None:\n    risk_ratio: float = 0.5\n")
        assert "float-money" not in _rules(path, tmp_path)

    def test_a_decimal_price_annotation_is_allowed(self, tmp_path: Path) -> None:
        source = "from decimal import Decimal\ndef f() -> None:\n    price: Decimal\n"
        path = _write(tmp_path, "risk", source)
        assert arch.check(path, tmp_path) == []

    def test_an_unannotated_parameter_does_not_crash_the_checker(self, tmp_path: Path) -> None:
        # Regression: ast.arg.annotation is None for an un-annotated parameter, and
        # ast.unparse(None) raises AttributeError on Python 3.12.
        path = _write(tmp_path, "risk", "def f(value):\n    return value\n")
        assert arch.check(path, tmp_path) == []

    def test_a_file_of_bare_functions_is_scannable(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "risk",
            "def a(x):\n    return x\n\n\ndef b(y, z=1):\n    return y + z\n",
        )
        assert arch.check(path, tmp_path) == []


# =============================================================================
# Rule 5: no absolute machine paths
# =============================================================================


class TestAbsolutePaths:
    def test_a_windows_drive_path_is_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "infrastructure", 'TERMINAL = "C:\\\\Program Files\\\\mt5.exe"\n')
        assert "absolute-path" in _rules(path, tmp_path)

    def test_a_posix_home_path_is_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "infrastructure", 'TERMINAL = "/home/bagheri/mt5"\n')
        assert "absolute-path" in _rules(path, tmp_path)

    def test_a_relative_path_is_allowed(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "infrastructure", 'CONFIG = "config/default.yaml"\n')
        assert arch.check(path, tmp_path) == []

    def test_a_commented_rule_is_not_a_violation(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path, "infrastructure", '# never hardcode "C:\\\\mt5.exe" -- see docs\n'
        )
        assert arch.check(path, tmp_path) == []


# =============================================================================
# Rule 6: the wall clock is reached only through an injected Clock
# =============================================================================


class TestWallClock:
    def test_naive_datetime_now_is_rejected(self) -> None:
        assert "wall-clock" in _clock_rules("from datetime import datetime\ndatetime.now()\n", "stop_order_scalp.risk.x")

    def test_naive_utcnow_is_rejected(self) -> None:
        assert "wall-clock" in _clock_rules(
            "from datetime import datetime\ndatetime.utcnow()\n", "stop_order_scalp.risk.x"
        )

    def test_tz_aware_now_is_allowed(self) -> None:
        # Regression: the rule is "no naive datetime", not "no datetime".
        assert (
            _clock_rules(
                "from datetime import UTC, datetime\ndatetime.now(UTC)\n",
                "stop_order_scalp.domain.models",
            )
            == set()
        )

    def test_importing_time_directly_is_rejected(self) -> None:
        assert "wall-clock" in _clock_rules("import time\n", "stop_order_scalp.risk.x")

    def test_the_clock_module_is_exempt(self) -> None:
        # Regression: the allowlist must be spelled the way module_name() returns it.
        assert "stop_order_scalp.infrastructure.clock" in arch._CLOCK_ALLOWED
        assert (
            _clock_rules("import time\n", "stop_order_scalp.infrastructure.clock") == set()
        )

    def test_every_allowed_entry_is_fully_qualified(self) -> None:
        for module in arch._CLOCK_ALLOWED:
            assert module.startswith("stop_order_scalp."), module
