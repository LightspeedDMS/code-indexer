"""Regression tests for bug #1498.

`cidx scip references <symbol>` / `cidx scip definition <symbol>` used to emit a
bare "No references/definitions found for '<symbol>'" whenever the SCIP index
generation is in a partial (LIMBO) state -- i.e. some projects failed to
generate their SCIP index. That silent empty result is indistinguishable from
a symbol that genuinely has zero references/definitions, producing a false
negative for users.

These tests drive the REAL CLI commands (`scip_references`/`scip_definition`
from `code_indexer.cli_scip`) via Click's CliRunner against a REAL, on-disk
`GenerationStatus`/`ProjectStatus` (via the real `StatusTracker.save()`/
`.load()` round trip from `code_indexer.scip.status`) -- not a bare Mock --
so the status object driving the gate is faithful to production behavior.
Only `SCIPQueryEngine` (the expensive SQLite query engine) is mocked to
return an empty result list, matching the established pattern in
`tests/unit/test_scip_cli_limit_option.py`.
"""

from unittest.mock import Mock, patch

import click.testing
import pytest

from code_indexer.scip.status import (
    GenerationStatus,
    OverallStatus,
    ProjectStatus,
    StatusTracker,
)

_TIMESTAMP = "2026-01-01T00:00:00"


def _write_status(scip_dir, *, overall_status, total, successful, failed, projects):
    """Persist a real GenerationStatus via the real StatusTracker."""
    tracker = StatusTracker(scip_dir)
    status = GenerationStatus(
        overall_status=overall_status,
        total_projects=total,
        successful_projects=successful,
        failed_projects=failed,
        projects=projects,
    )
    tracker.save(status)
    return status


def _limbo_projects_3_of_6_failed():
    """3 succeeded / 3 failed -- mirrors the exact bug #1498 repro (6 total)."""
    return {
        "backend": ProjectStatus(
            status=OverallStatus.SUCCESS,
            language="python",
            build_system="poetry",
            timestamp=_TIMESTAMP,
        ),
        "frontend": ProjectStatus(
            status=OverallStatus.SUCCESS,
            language="typescript",
            build_system="npm",
            timestamp=_TIMESTAMP,
        ),
        "worker": ProjectStatus(
            status=OverallStatus.SUCCESS,
            language="python",
            build_system="poetry",
            timestamp=_TIMESTAMP,
        ),
        "gateway": ProjectStatus(
            status=OverallStatus.FAILED,
            language="java",
            build_system="maven",
            timestamp=_TIMESTAMP,
            error_message="build failed",
        ),
        "cli": ProjectStatus(
            status=OverallStatus.FAILED,
            language="python",
            build_system="poetry",
            timestamp=_TIMESTAMP,
            error_message="build failed",
        ),
        "docs": ProjectStatus(
            status=OverallStatus.FAILED,
            language="python",
            build_system="poetry",
            timestamp=_TIMESTAMP,
            error_message="build failed",
        ),
    }


def _complete_success_projects():
    return {
        "backend": ProjectStatus(
            status=OverallStatus.SUCCESS,
            language="python",
            build_system="poetry",
            timestamp=_TIMESTAMP,
        ),
    }


@pytest.fixture
def scip_env(tmp_path, monkeypatch):
    """Real .code-indexer/scip/ dir with one scip.db file, cwd chdir'd into it."""
    scip_dir = tmp_path / ".code-indexer" / "scip"
    scip_dir.mkdir(parents=True)
    scip_file = scip_dir / "test.scip.db"
    scip_file.touch()

    # A valid (empty) config.json so command-mode detection resolves to
    # "local" rather than "uninitialized"/"remote".
    config_file = tmp_path / ".code-indexer" / "config.json"
    config_file.write_text("{}")

    monkeypatch.chdir(tmp_path)
    return scip_dir


class TestReferencesIncompletenessWarning:
    """Bug #1498: `cidx scip references` must warn on LIMBO/partial index."""

    def test_limbo_status_with_empty_result_warns_of_partial_index(self, scip_env):
        _write_status(
            scip_env,
            overall_status=OverallStatus.LIMBO,
            total=6,
            successful=3,
            failed=3,
            projects=_limbo_projects_3_of_6_failed(),
        )

        with patch("code_indexer.scip.query.SCIPQueryEngine") as mock_engine_cls:
            mock_engine = Mock()
            mock_engine.find_references.return_value = []
            mock_engine_cls.return_value = mock_engine

            from code_indexer.cli_scip import scip_references

            runner = click.testing.CliRunner()
            result = runner.invoke(scip_references, ["register_child_process"])

        assert result.exit_code == 0
        assert "No references found for 'register_child_process'" in result.output
        # Discriminating assertion: bug #1498 fix must add a LOUD warning
        # mentioning the partial/failed project counts and remediation.
        assert "partial" in result.output.lower()
        assert "3" in result.output and "6" in result.output
        assert "cidx scip generate" in result.output

    def test_complete_status_with_empty_result_has_no_incompleteness_warning(
        self, scip_env
    ):
        _write_status(
            scip_env,
            overall_status=OverallStatus.SUCCESS,
            total=1,
            successful=1,
            failed=0,
            projects=_complete_success_projects(),
        )

        with patch("code_indexer.scip.query.SCIPQueryEngine") as mock_engine_cls:
            mock_engine = Mock()
            mock_engine.find_references.return_value = []
            mock_engine_cls.return_value = mock_engine

            from code_indexer.cli_scip import scip_references

            runner = click.testing.CliRunner()
            result = runner.invoke(scip_references, ["totally_unused_symbol"])

        assert result.exit_code == 0
        assert "No references found for 'totally_unused_symbol'" in result.output
        # A complete index's genuine zero must NOT be polluted with a warning.
        assert "partial" not in result.output.lower()
        assert "⚠" not in result.output


class TestDefinitionIncompletenessWarning:
    """Bug #1498: `cidx scip definition` must warn on LIMBO/partial index."""

    def test_limbo_status_with_empty_result_warns_of_partial_index(self, scip_env):
        _write_status(
            scip_env,
            overall_status=OverallStatus.LIMBO,
            total=6,
            successful=3,
            failed=3,
            projects=_limbo_projects_3_of_6_failed(),
        )

        with patch("code_indexer.scip.query.SCIPQueryEngine") as mock_engine_cls:
            mock_engine = Mock()
            mock_engine.find_definition.return_value = []
            mock_engine_cls.return_value = mock_engine

            from code_indexer.cli_scip import scip_definition

            runner = click.testing.CliRunner()
            result = runner.invoke(scip_definition, ["register_child_process"])

        assert result.exit_code == 0
        assert "No definitions found for 'register_child_process'" in result.output
        assert "partial" in result.output.lower()
        assert "3" in result.output and "6" in result.output
        assert "cidx scip generate" in result.output

    def test_complete_status_with_empty_result_has_no_incompleteness_warning(
        self, scip_env
    ):
        _write_status(
            scip_env,
            overall_status=OverallStatus.SUCCESS,
            total=1,
            successful=1,
            failed=0,
            projects=_complete_success_projects(),
        )

        with patch("code_indexer.scip.query.SCIPQueryEngine") as mock_engine_cls:
            mock_engine = Mock()
            mock_engine.find_definition.return_value = []
            mock_engine_cls.return_value = mock_engine

            from code_indexer.cli_scip import scip_definition

            runner = click.testing.CliRunner()
            result = runner.invoke(scip_definition, ["totally_unused_symbol"])

        assert result.exit_code == 0
        assert "No definitions found for 'totally_unused_symbol'" in result.output
        assert "partial" not in result.output.lower()
        assert "⚠" not in result.output
