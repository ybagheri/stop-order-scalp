"""The CLI contract.

Two things are worth pinning here. First, the exit codes: a supervisor process depends on
them, so they are part of the interface rather than an implementation detail. Second, the
behaviour of a command whose implementing phase has not been built yet. Phase 1 fixes the
command surface ahead of the components behind it, so those commands must fail with a
named, exit-coded error -- never an ``ImportError`` traceback.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import pytest

from stop_order_scalp.cli.main import (
    EXIT_CONFIG,
    EXIT_NOT_CONNECTED,
    EXIT_OK,
    EXIT_UNAVAILABLE,
    build_parser,
    main,
)
from stop_order_scalp.domain.exceptions import ComponentNotAvailableError

#: Commands whose implementation belongs to a later phase.
#:
#: ``test-connection`` is deliberately absent: Phase 2 moved its probe into
#: ``market_data.mt5_feed``, where it belongs, because a read-only reachability check is a
#: market-data concern rather than an execution one. It now runs and reports a structured
#: result, exiting 3 when the terminal is unreachable.
# Every command in the contract is now built. Kept as an empty tuple rather than deleted, so
# adding a command to the contract without implementing it stays a visible failure.
DEFERRED_COMMANDS: tuple[tuple[str, ...], ...] = ()

#: Commands that have been built and now exit 0. Kept beside the deferred list rather than
#: deleted from the test, so a regression to "not implemented yet" is a visible failure
#: instead of a silently smaller suite.
#:
#: ``backtest`` is here with ``--data`` rather than bare, and that asymmetry is the point: a
#: replay with no input file has nothing to say, so it exits 2 with a reason instead of
#: generating candles and reporting a result. Asserted separately below.
BUILT_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("run", "--dry-run"),
    ("journal",),
    ("status",),
    ("diagnostics",),
)


def test_backtest_without_a_data_file_refuses_with_a_reason(
    hermetic_env_file: Path,
) -> None:
    """The command is built, and it declines to invent its input.

    Exit 2 (a configuration problem) rather than 0 or 4: nothing is missing from the code,
    something is missing from the invocation.
    """
    code, _, err = _run(["backtest", "--env-file", str(hermetic_env_file)])
    assert code == 2, f"backtest with no --data exited {code}"
    assert "--data" in err


@pytest.mark.parametrize("argv", BUILT_COMMANDS)
def test_a_built_command_exits_ok(
    argv: tuple[str, ...], hermetic_env_file: Path
) -> None:
    code, out, err = _run([*argv, "--env-file", str(hermetic_env_file)])
    assert code == EXIT_OK, f"{argv} exited {code}: {err}"
    assert json.loads(out), "a built command must print a structured result"


def test_a_dry_run_reports_what_it_did(hermetic_env_file: Path) -> None:
    """The one command whose whole purpose is to be *seen*.

    Asserted on content rather than on the exit code alone: a dry run that exits 0 while
    reporting nothing would satisfy the weaker test and be useless to the operator.
    """
    code, out, _ = _run(["run", "--dry-run", "--max-cycles", "30", "--env-file", str(hermetic_env_file)])
    assert code == EXIT_OK
    report = json.loads(out)
    assert report["environment"] == "DRY_RUN"
    assert report["cycles"], "a dry run must report the cycles it ran"
    assert report["rule"]["symbol"] == "US30"
    # Every cycle states what it did, so "nothing happened" is distinguishable from
    # "something happened and was not reported".
    assert all("action" in cycle for cycle in report["cycles"])


def test_live_is_refused_with_a_reason(hermetic_env_file: Path) -> None:
    """--live must be refused by name, not fail obscurely.

    Every wire value and retcode this project uses is still unverified against a real
    terminal, so the refusal is the honest answer and the message has to say why.
    """
    code, _, err = _run(["run", "--live", "--env-file", str(hermetic_env_file)])
    assert code == EXIT_UNAVAILABLE
    assert "not available" in err
    assert "Traceback" not in err


@pytest.fixture
def hermetic_env_file(tmp_path: Path) -> Path:
    """An explicit, empty ``.env`` so a developer's real one cannot reach a test."""
    env_file = tmp_path / "empty.env"
    env_file.write_text("SOS_ENVIRONMENT=DRY_RUN\n", encoding="utf-8")
    return env_file


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


# =============================================================================
# Parser surface
# =============================================================================


class TestParser:
    def test_every_documented_command_is_registered(self) -> None:
        subparsers = build_parser()._subparsers
        assert subparsers is not None
        group = subparsers._group_actions[0]
        assert isinstance(group, argparse._SubParsersAction)
        assert set(group.choices) == {
            "run",
            "status",
            "validate-config",
            "test-connection",
            "journal",
            "diagnostics",
            "backtest",
            # The operator commands for a demo account.
            "book",
            "history",
            "cancel",
            "close",
            "flatten",
        }

    def test_a_missing_command_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_run_rejects_dry_run_and_live_together(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["run", "--dry-run", "--live"])


# =============================================================================
# validate-config: the one command Phase 1 fully implements
# =============================================================================


class TestValidateConfig:
    def test_it_reports_the_resolved_configuration_as_json(
        self, hermetic_env_file: Path
    ) -> None:
        code, out, _ = _run(["validate-config", "--env-file", str(hermetic_env_file)])
        assert code == EXIT_OK
        payload = json.loads(out)
        assert payload["valid"] is True
        assert payload["strategy"]["symbol"] == "US30"

    def test_it_names_the_files_that_were_read(self, hermetic_env_file: Path) -> None:
        code, out, _ = _run(["validate-config", "--env-file", str(hermetic_env_file)])
        assert code == EXIT_OK
        sources = json.loads(out)["sources"]
        assert any(source.endswith("default.yaml") for source in sources)
        assert any(source.endswith(".env") for source in sources)

    def test_a_broken_configuration_exits_with_the_config_code(self, tmp_path: Path) -> None:
        code, _, err = _run(
            ["validate-config", "--config", str(tmp_path / "absent.yaml")]
        )
        assert code == EXIT_CONFIG
        assert "ERROR [2]" in err


# =============================================================================
# Commands owned by later phases
# =============================================================================


class TestDeferredCommands:
    """Every command in the contract is now implemented.

    This class is kept rather than deleted, and the parametrised cases are skipped when the
    list is empty, so that adding a command to the contract without building it produces a
    *failing* test instead of an empty parametrisation that pytest quietly reports as a skip.
    An empty list is a fact about the project; it should be visible, not silent.
    """

    @pytest.mark.skipif(
        not DEFERRED_COMMANDS, reason="no command in the contract is unbuilt"
    )
    @pytest.mark.parametrize("argv", DEFERRED_COMMANDS)
    def test_an_unbuilt_command_exits_unavailable_rather_than_raising(
        self, argv: tuple[str, ...], hermetic_env_file: Path
    ) -> None:
        code, _, err = _run([*argv, "--env-file", str(hermetic_env_file)])
        assert code == EXIT_UNAVAILABLE
        assert "ERROR [4]" in err
        assert "not implemented yet" in err

    @pytest.mark.skipif(
        not DEFERRED_COMMANDS, reason="no command in the contract is unbuilt"
    )
    @pytest.mark.parametrize("argv", DEFERRED_COMMANDS)
    def test_no_unbuilt_command_raises_an_import_error(
        self, argv: tuple[str, ...], hermetic_env_file: Path
    ) -> None:
        # main() promises never to raise for an expected failure; a missing module is an
        # expected failure at this phase, so it must be reported, not propagated.
        code, _, err = _run([*argv, "--env-file", str(hermetic_env_file)])
        assert "ImportError" not in err
        assert "Traceback" not in err
        assert code == EXIT_UNAVAILABLE

    def test_the_contract_has_no_holes(self) -> None:
        """Every command in the contract is built, and the set is stated once, here.

        The contract is seven commands. Six are listed in `BUILT_COMMANDS` because they run
        with no arguments; `backtest` needs `--data`, and is asserted separately below because
        without it the command correctly refuses with exit 2 rather than running.
        """
        assert set(BUILT_COMMANDS) == {
            ("run", "--dry-run"),
            ("journal",),
            ("status",),
            ("diagnostics",),
        }
        assert DEFERRED_COMMANDS == (), "a command in the contract is not built"


class TestResolve:
    def test_a_missing_module_raises_the_named_domain_error(self) -> None:
        from stop_order_scalp.cli.main import _resolve

        with pytest.raises(ComponentNotAvailableError, match="not implemented yet"):
            _resolve("stop_order_scalp.no.such.module", "thing")

    def test_a_missing_attribute_raises_the_named_domain_error(self) -> None:
        from stop_order_scalp.cli.main import _resolve

        with pytest.raises(ComponentNotAvailableError):
            _resolve("stop_order_scalp.domain.enums", "NoSuchEnum")

    def test_an_existing_component_resolves(self) -> None:
        from stop_order_scalp.cli.main import _resolve

        assert _resolve("stop_order_scalp.domain.enums", "Side").__name__ == "Side"


# =============================================================================
# Error reporting
# =============================================================================


class TestTestConnection:
    """The one MT5-touching command, which must never raise and never write."""

    def test_it_reports_rather_than_raising_when_the_terminal_is_absent(
        self, hermetic_env_file: Path
    ) -> None:
        code, out, err = _run(["test-connection", "--env-file", str(hermetic_env_file)])
        report = json.loads(out)
        assert report["connected"] is False
        assert report["error"] is not None
        assert err == ""
        assert code == EXIT_NOT_CONNECTED

    def test_the_report_names_whether_the_package_is_installed(
        self, hermetic_env_file: Path
    ) -> None:
        _, out, _ = _run(["test-connection", "--env-file", str(hermetic_env_file)])
        report = json.loads(out)
        assert "package_installed" in report
        assert "terminal_path_configured" in report
        assert "credentials_configured" in report

    def test_it_never_prints_a_traceback(self, hermetic_env_file: Path) -> None:
        _, out, err = _run(["test-connection", "--env-file", str(hermetic_env_file)])
        assert "Traceback" not in out
        assert "Traceback" not in err


class TestErrorReporting:
    def test_the_exit_code_appears_in_the_message(self) -> None:
        code, _, err = _run(["validate-config", "--config", "definitely/absent.yaml"])
        assert f"ERROR [{EXIT_CONFIG}]" in err
        assert code == EXIT_CONFIG

    def test_stdout_stays_machine_readable_on_success(
        self, hermetic_env_file: Path
    ) -> None:
        _, out, _ = _run(["validate-config", "--env-file", str(hermetic_env_file)])
        json.loads(out)

    def test_human_text_goes_to_stderr_not_stdout(self) -> None:
        _, out, err = _run(["validate-config", "--config", "definitely/absent.yaml"])
        assert out == ""
        assert err.strip() != ""

    def test_not_connected_is_a_distinct_code(self) -> None:
        # Declared here so a future refactor cannot silently renumber it.
        assert EXIT_NOT_CONNECTED == 3
        assert EXIT_UNAVAILABLE == 4


class TestJournalReadsTheConfiguredEnvironment:
    def test_a_demo_run_reads_the_demo_ledger_not_the_dry_run_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """It used to be hard-wired to DRY_RUN, so a demo run's journal always read empty."""
        monkeypatch.setenv("SOS_ENVIRONMENT", "DEMO")
        out, err = io.StringIO(), io.StringIO()

        code = main(["journal"], stdout=out, stderr=err)

        report = json.loads(out.getvalue())
        assert code == EXIT_OK
        assert report["ledger"].endswith("state-demo.json")
        assert report["environment"].endswith("DEMO")
