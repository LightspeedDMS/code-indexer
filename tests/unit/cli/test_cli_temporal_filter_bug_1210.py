"""Bug #1210 — CLI temporal query drops --exclude-path and honors only one --path-filter.

Tests assert that the CLI temporal path (`cidx query ... --time-range-all`) forwards:
1. `exclude_path` (was silently dropped — omitted from _execute_temporal_fusion call)
2. ALL `--path-filter` values (was collapsed to None when >1)

These tests are written RED-first against the CURRENT (buggy) code and must FAIL
before the fix. After the fix they must all PASS.

Patching strategy (matches test_cli_temporal_rerank_wiring.py exactly):
- FilesystemVectorStore patched at its defining module so the lazy import inside
  cli.py gets the mock (does not create real filesystem state).
- TemporalSearchService.has_temporal_index patched to return True (bypass early exit).
- execute_temporal_query_with_fusion patched at the source module (local import inside
  the CLI function is resolved from that module).
- BackendFactory.create patched for the second vector-store init inside the else branch.
- A real minimal project directory is created so index_dir.iterdir() succeeds without
  raising FileNotFoundError.
"""

import json
import os
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from code_indexer.cli import cli
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResults,
)

# ---------------------------------------------------------------------------
# Helpers: project directory and mock objects
# ---------------------------------------------------------------------------

_VOYAGE_KEY = "test-voyage-key-1210"


def _make_project(tmp_path: Path) -> Path:
    """Create a minimal project tree with a stub temporal collection directory.

    The CLI checks `index_dir.iterdir()` for dirs whose names match
    `is_temporal_collection(d.name)`.  Creating a dir named
    'code-indexer-temporal' satisfies that check without real index data.
    """
    config_dir = tmp_path / ".code-indexer"
    config_dir.mkdir(parents=True)
    index_dir = config_dir / "index"
    index_dir.mkdir()

    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "codebase_dir": str(tmp_path),
                "filesystem": {"port": 6333, "grpc_port": 6334},
                "voyage_api": {"api_key": _VOYAGE_KEY},
                "embedding_provider": "voyage",
            }
        ),
        encoding="utf-8",
    )

    # Stub temporal collection dir — satisfies the iterdir() check in the CLI
    temporal_dir = index_dir / "code-indexer-temporal"
    temporal_dir.mkdir()
    (temporal_dir / "collection_meta.json").write_text(
        json.dumps(
            {
                "name": "code-indexer-temporal",
                "vector_count": 0,
                "file_count": 0,
                "indexed_at": "2025-01-01T00:00:00",
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _make_mock_config(project: Path) -> MagicMock:
    mock_config = MagicMock()
    mock_config.codebase_dir = project
    mock_config.embedding_provider = "voyage-ai"
    mock_config.voyage_api = MagicMock(api_key=_VOYAGE_KEY)
    mock_config.filesystem = MagicMock(port=6333)
    mock_config.daemon = MagicMock(enabled=False)
    mock_config.vector_store = None
    return mock_config


def _make_mock_cm(project: Path) -> MagicMock:
    mock_config = _make_mock_config(project)
    mock_cm = MagicMock()
    mock_cm.get_config.return_value = mock_config
    mock_cm.load.return_value = mock_config
    mock_cm.get_daemon_config.return_value = {"enabled": False}
    return mock_cm


def _make_mock_fsvs(project: Path) -> MagicMock:
    """Minimal FilesystemVectorStore mock — health_check passes."""
    mock_vs = MagicMock()
    mock_vs.health_check.return_value = True
    mock_vs.base_path = project / ".code-indexer" / "index"
    mock_vs.project_root = project
    return mock_vs


def _make_mock_backend(project: Path) -> MagicMock:
    mock_vs = _make_mock_fsvs(project)
    mock_backend = MagicMock()
    mock_backend.get_vector_store_client.return_value = mock_vs
    return mock_backend


def _empty_results() -> TemporalSearchResults:
    return TemporalSearchResults(
        results=[],
        query="test query",
        filter_type="time_range",
        filter_value=("1970-01-01", "2100-12-31"),
        total_found=0,
    )


# ---------------------------------------------------------------------------
# Core invocation helper
# ---------------------------------------------------------------------------


def _invoke(project: Path, extra_args: List[str], fusion_mock: MagicMock) -> Any:
    """Invoke `cidx query ... --time-range-all --quiet` from the project directory.

    Patches:
      - ConfigManager: returns deterministic mock config
      - FilesystemVectorStore (defining module): returns mock VS (used for the
        initial temporal-index-check path AND the health-check path)
      - TemporalSearchService.has_temporal_index: returns True (bypass early exit)
      - execute_temporal_query_with_fusion (source module): captured by fusion_mock
      - BackendFactory.create: returns mock backend for the second VS init

    Returns the Click CliResult.
    """
    mock_cm = _make_mock_cm(project)
    mock_backend = _make_mock_backend(project)
    mock_vs = mock_backend.get_vector_store_client()

    # The CLI does `FilesystemVectorStore(base_path=..., project_root=...)` directly
    # on the initial path (before BackendFactory). Patch at the defining module.
    mock_fsvs_class = MagicMock(return_value=mock_vs)

    runner = CliRunner()
    old_cwd = os.getcwd()
    try:
        os.chdir(str(project))
        with (
            patch(
                "code_indexer.cli.ConfigManager.create_with_backtrack",
                return_value=mock_cm,
            ),
            patch(
                "code_indexer.storage.filesystem_vector_store.FilesystemVectorStore",
                mock_fsvs_class,
            ),
            patch(
                "code_indexer.services.temporal.temporal_search_service"
                ".TemporalSearchService.has_temporal_index",
                return_value=True,
            ),
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch"
                ".execute_temporal_query_with_fusion",
                side_effect=fusion_mock,
            ),
            patch(
                "code_indexer.cli.BackendFactory.create",
                return_value=mock_backend,
            ),
        ):
            return runner.invoke(
                cli,
                ["query", "my search", "--time-range-all", "--quiet"] + extra_args,
                catch_exceptions=False,
            )
    finally:
        os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Tests: exclude_path forwarding (Bug #1210 part 1)
# ---------------------------------------------------------------------------


class TestExcludePathForwarding:
    """The CLI temporal path MUST forward --exclude-path to execute_temporal_query_with_fusion."""

    def test_single_exclude_path_is_forwarded(self, tmp_path: Path) -> None:
        """A single --exclude-path value must arrive as exclude_path kwarg."""
        project = _make_project(tmp_path)
        fusion_mock = MagicMock(return_value=_empty_results())

        _invoke(project, ["--exclude-path", "*/tests/*"], fusion_mock)

        assert fusion_mock.called, "execute_temporal_query_with_fusion was never called"
        _, kwargs = fusion_mock.call_args
        assert "exclude_path" in kwargs, (
            "BUG #1210: exclude_path was not forwarded to execute_temporal_query_with_fusion"
        )
        assert kwargs["exclude_path"] is not None, (
            "BUG #1210: exclude_path was forwarded as None"
        )
        assert "*/tests/*" in kwargs["exclude_path"], (
            f"BUG #1210: exclude pattern not in forwarded value: {kwargs['exclude_path']!r}"
        )

    def test_multiple_exclude_paths_are_all_forwarded(self, tmp_path: Path) -> None:
        """Multiple --exclude-path values must all be forwarded (comma-joined)."""
        project = _make_project(tmp_path)
        fusion_mock = MagicMock(return_value=_empty_results())

        _invoke(
            project,
            ["--exclude-path", "*/tests/*", "--exclude-path", "*.min.js"],
            fusion_mock,
        )

        assert fusion_mock.called, "execute_temporal_query_with_fusion was never called"
        _, kwargs = fusion_mock.call_args
        assert "exclude_path" in kwargs, (
            "BUG #1210: exclude_path not forwarded with multiple --exclude-path"
        )
        exclude_val = kwargs["exclude_path"]
        assert exclude_val is not None, "exclude_path was forwarded as None"
        assert "*/tests/*" in exclude_val, (
            f"First exclude pattern missing from: {exclude_val!r}"
        )
        assert "*.min.js" in exclude_val, (
            f"Second exclude pattern missing from: {exclude_val!r}"
        )

    def test_no_exclude_path_forwards_none(self, tmp_path: Path) -> None:
        """When no --exclude-path is given, exclude_path must be None (regression guard)."""
        project = _make_project(tmp_path)
        fusion_mock = MagicMock(return_value=_empty_results())

        _invoke(project, [], fusion_mock)

        assert fusion_mock.called, "execute_temporal_query_with_fusion was never called"
        _, kwargs = fusion_mock.call_args
        assert kwargs.get("exclude_path") is None, (
            f"exclude_path should be None when not specified, got: {kwargs.get('exclude_path')!r}"
        )


# ---------------------------------------------------------------------------
# Tests: path_filter forwarding (Bug #1210 part 2)
# ---------------------------------------------------------------------------


class TestPathFilterForwarding:
    """The CLI temporal path MUST forward ALL --path-filter values."""

    def test_single_path_filter_is_forwarded(self, tmp_path: Path) -> None:
        """A single --path-filter must arrive as file_path_filter kwarg (regression guard)."""
        project = _make_project(tmp_path)
        fusion_mock = MagicMock(return_value=_empty_results())

        _invoke(project, ["--path-filter", "*/src/*"], fusion_mock)

        assert fusion_mock.called, "execute_temporal_query_with_fusion was never called"
        _, kwargs = fusion_mock.call_args
        assert "file_path_filter" in kwargs, "file_path_filter not in kwargs"
        assert kwargs["file_path_filter"] is not None, (
            "BUG: single --path-filter forwarded as None"
        )
        assert "*/src/*" in kwargs["file_path_filter"], (
            f"Path filter pattern missing from: {kwargs['file_path_filter']!r}"
        )

    def test_multiple_path_filters_all_forwarded(self, tmp_path: Path) -> None:
        """Multiple --path-filter values must ALL be forwarded (not dropped to None).

        BUG #1210: current code has `list(path_filter)[0] if len(path_filter)==1 else None`
        so 2+ path filters are silently dropped (forwarded as None). After the fix,
        all patterns must appear in file_path_filter.
        """
        project = _make_project(tmp_path)
        fusion_mock = MagicMock(return_value=_empty_results())

        _invoke(
            project,
            ["--path-filter", "*/src/*", "--path-filter", "*/lib/*"],
            fusion_mock,
        )

        assert fusion_mock.called, "execute_temporal_query_with_fusion was never called"
        _, kwargs = fusion_mock.call_args
        assert "file_path_filter" in kwargs, "file_path_filter not in kwargs"
        pf = kwargs["file_path_filter"]
        assert pf is not None, (
            "BUG #1210: multiple --path-filter values collapsed to None"
        )
        assert "*/src/*" in pf, f"First path filter missing from: {pf!r}"
        assert "*/lib/*" in pf, f"Second path filter missing from: {pf!r}"

    def test_no_path_filter_forwards_none(self, tmp_path: Path) -> None:
        """When no --path-filter is given, file_path_filter must be None (regression guard)."""
        project = _make_project(tmp_path)
        fusion_mock = MagicMock(return_value=_empty_results())

        _invoke(project, [], fusion_mock)

        assert fusion_mock.called, "execute_temporal_query_with_fusion was never called"
        _, kwargs = fusion_mock.call_args
        assert kwargs.get("file_path_filter") is None, (
            f"file_path_filter should be None when not specified, "
            f"got: {kwargs.get('file_path_filter')!r}"
        )


# ---------------------------------------------------------------------------
# Tests: parity — both filters together
# ---------------------------------------------------------------------------


class TestCombinedFilterParity:
    """Verify that exclude_path AND path_filter are both forwarded simultaneously."""

    def test_both_filters_forwarded_simultaneously(self, tmp_path: Path) -> None:
        """When --path-filter and --exclude-path are both given, both reach fusion."""
        project = _make_project(tmp_path)
        fusion_mock = MagicMock(return_value=_empty_results())

        _invoke(
            project,
            [
                "--path-filter",
                "*/src/*",
                "--path-filter",
                "*/lib/*",
                "--exclude-path",
                "*/tests/*",
                "--exclude-path",
                "*/vendor/*",
            ],
            fusion_mock,
        )

        assert fusion_mock.called, "execute_temporal_query_with_fusion was never called"
        _, kwargs = fusion_mock.call_args

        # Check path_filter forwarding
        pf = kwargs.get("file_path_filter")
        assert pf is not None, "BUG #1210: file_path_filter not forwarded"
        assert "*/src/*" in pf, f"First path filter missing from {pf!r}"
        assert "*/lib/*" in pf, f"Second path filter missing from {pf!r}"

        # Check exclude_path forwarding
        ep = kwargs.get("exclude_path")
        assert ep is not None, "BUG #1210: exclude_path not forwarded"
        assert "*/tests/*" in ep, f"First exclude pattern missing from {ep!r}"
        assert "*/vendor/*" in ep, f"Second exclude pattern missing from {ep!r}"
