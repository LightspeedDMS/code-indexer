"""Tests verifying that the CLI displays the temporal warning when no index exists.

When `execute_temporal_query_with_fusion` returns a TemporalSearchResults with
`results=[]` and a non-empty `.warning`, the CLI `query` command must display
that warning to the user so they know why they got no results.

Story: Fix misleading "fallback to regular search" messaging in temporal pipeline.

Note on testing approach: the `query` command creates a local `console = Console()`
on line ~4901 of cli.py (rather than using the module-level `console`), so the
CliRunner captures all console output in `result.output` — we assert against that
rather than patching the module-level console object.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from src.code_indexer.cli import query as query_command
from src.code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResults,
)

WARNING_TEXT = (
    "No temporal indexes available. "
    "Run cidx index --index-commits to create temporal indexes."
)


def _make_empty_temporal_results() -> TemporalSearchResults:
    """TemporalSearchResults with no results and the standard warning message."""
    return TemporalSearchResults(
        results=[],
        query="authentication",
        filter_type="time_range",
        filter_value=None,
        warning=WARNING_TEXT,
        total_found=0,
    )


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def temp_project(tmp_path):
    """Create a minimal project directory with .code-indexer structure."""
    code_indexer_dir = tmp_path / ".code-indexer" / "index"
    code_indexer_dir.mkdir(parents=True)
    # Create a legacy temporal collection directory so the _has_temporal check passes.
    # is_temporal_collection() recognises "code-indexer-temporal" (LEGACY_TEMPORAL_COLLECTION).
    temporal_dir = code_indexer_dir / "code-indexer-temporal"
    temporal_dir.mkdir()
    return tmp_path


def _make_config_mock(project_root: Path):
    """Build a minimal config mock for the CLI temporal path."""
    config = MagicMock()
    config.codebase_dir = project_root
    config.embedding_model = "voyage-code-3"
    config.provider = "voyageai"
    # resolve_temporal_collection_from_config uses embedding_provider
    config.embedding_provider = "voyage-ai"
    return config


def _make_config_manager_mock(project_root: Path):
    """Build a minimal config_manager mock."""
    config = _make_config_mock(project_root)
    manager = MagicMock()
    manager.load.return_value = config
    manager.get_config.return_value = config
    # Return None so the daemon delegation check in the CLI is skipped
    manager.get_daemon_config.return_value = None
    return manager


def _invoke_temporal_query(runner, temp_project, temporal_results):
    """Helper: invoke the CLI query command with a mocked temporal fusion result.

    Returns the CliRunner result (with .output and .exit_code).
    External dependencies patched:
    - src.code_indexer.cli.BackendFactory (module-level import in cli.py)
    - src.code_indexer.services.temporal.temporal_fusion_dispatch
      .execute_temporal_query_with_fusion (lazy import inside query function)
    """
    config_manager_mock = _make_config_manager_mock(temp_project)
    backend_mock = MagicMock()
    vector_store_mock = MagicMock()
    vector_store_mock.health_check.return_value = True
    backend_mock.get_vector_store_client.return_value = vector_store_mock

    with (
        patch(
            # Lazy import in query(): "from .services.temporal.temporal_fusion_dispatch
            # import execute_temporal_query_with_fusion as _execute_temporal_fusion"
            # resolves in the src.code_indexer namespace.
            "src.code_indexer.services.temporal.temporal_fusion_dispatch"
            ".execute_temporal_query_with_fusion",
            return_value=temporal_results,
        ),
        # BackendFactory is imported at module level in cli.py; patch it there.
        patch("src.code_indexer.cli.BackendFactory") as mock_backend_cls,
    ):
        mock_backend_cls.create.return_value = backend_mock
        return runner.invoke(
            query_command,
            ["--time-range-all", "authentication"],
            obj={
                "config_manager": config_manager_mock,
                "project_root": str(temp_project),
                "mode": "local",
                # standalone=True bypasses daemon delegation check (ctx.obj["standalone"])
                "standalone": True,
            },
            catch_exceptions=False,
        )


@pytest.fixture
def temp_project_no_temporal(tmp_path):
    """Project directory WITHOUT any temporal collection — _has_temporal check fails."""
    code_indexer_dir = tmp_path / ".code-indexer" / "index"
    code_indexer_dir.mkdir(parents=True)
    # No temporal collection directory created — simulates missing temporal index
    return tmp_path


class TestCLITemporalWarningDisplay:
    """Verify the CLI displays the warning from TemporalSearchResults."""

    def test_cli_no_temporal_index_returns_immediately(
        self, runner, temp_project_no_temporal
    ):
        """When no temporal collection directory exists, CLI returns immediately.

        Must print honest "No results returned" message and must NOT invoke
        execute_temporal_query_with_fusion at all (no fallback search).
        """
        config_manager_mock = _make_config_manager_mock(temp_project_no_temporal)

        with patch(
            "src.code_indexer.services.temporal.temporal_fusion_dispatch"
            ".execute_temporal_query_with_fusion"
        ) as mock_fusion:
            result = runner.invoke(
                query_command,
                ["--time-range-all", "authentication"],
                obj={
                    "config_manager": config_manager_mock,
                    "project_root": str(temp_project_no_temporal),
                    "mode": "local",
                    "standalone": True,
                },
                catch_exceptions=False,
            )

        assert result.exit_code == 0, (
            f"Expected exit code 0, got {result.exit_code}. Output: {result.output!r}"
        )
        assert "No results returned" in result.output, (
            f"CLI must display honest 'No results returned' message. Output: {result.output!r}"
        )
        assert "cidx index --index-commits" in result.output, (
            f"CLI must mention the build command. Output: {result.output!r}"
        )
        assert not mock_fusion.called, (
            "Fusion dispatch must NOT be called when temporal index is absent — "
            "no fallback search allowed"
        )

    def test_cli_displays_temporal_warning_when_no_index(self, runner, temp_project):
        """When fusion dispatch returns empty results with a warning, CLI must display it.

        The user must see the warning text so they understand why no results were
        returned and what to do about it.
        """
        result = _invoke_temporal_query(
            runner, temp_project, _make_empty_temporal_results()
        )

        assert result.exit_code == 0, (
            f"Expected exit code 0, got {result.exit_code}. Output: {result.output!r}"
        )
        assert "No temporal indexes available" in result.output, (
            "CLI must display the warning from TemporalSearchResults when "
            f"no temporal index exists. CLI output: {result.output!r}"
        )
        assert "cidx index --index-commits" in result.output, (
            "CLI warning display must mention the build command. "
            f"CLI output: {result.output!r}"
        )

    def test_cli_does_not_display_warning_when_results_present(
        self, runner, temp_project
    ):
        """When results are returned, no temporal warning should be printed."""
        from src.code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchResult,
        )

        fake_result = MagicMock(spec=TemporalSearchResult)
        fake_result.metadata = {
            "type": "commit_diff",
            "file_path": "src/auth.py",
            "commit_hash": "abc1234",
            "commit_date": "2024-01-15",
            "author_name": "Test User",
        }
        fake_result.score = 0.9
        fake_result.content = "def authenticate(): pass"
        fake_result.temporal_context = {}

        results_with_data = TemporalSearchResults(
            results=[fake_result],
            query="authentication",
            filter_type="time_range",
            filter_value=None,
            warning=None,
            total_found=1,
        )

        result = _invoke_temporal_query(runner, temp_project, results_with_data)

        assert "No temporal indexes available" not in result.output, (
            "CLI must NOT display the warning when results are present. "
            f"CLI output: {result.output!r}"
        )
