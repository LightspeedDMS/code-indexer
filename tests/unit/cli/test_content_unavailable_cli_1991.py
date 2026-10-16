"""Bug #1991: CLI standalone and daemon keep the store's content_unavailable
signal and display a marker instead of an empty snippet.

Results come from a REAL FilesystemVectorStore search over a real git repo
whose ``Broken.cs`` is unreadable on both retrieval tiers; staleness comes
from the real StalenessDetector. Only the embedding provider is a
deterministic in-process stand-in (external service).
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict, List

import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from tests.unit.server.query.content_unavailable_env_1991 import (
    BROKEN_FILE,
    COLLECTION,
    GOOD_FILE,
    UNAVAILABLE_MARKER,
    FakeEmbeddingProvider,
    build_indexed_repo,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return build_indexed_repo(tmp_path)


def _store_results(repo: Path) -> List[Dict[str, Any]]:
    store = FilesystemVectorStore(
        base_path=repo / ".code-indexer" / "index", project_root=repo
    )
    results = store.search(
        query="image generator",
        embedding_provider=FakeEmbeddingProvider(),
        collection_name=COLLECTION,
        limit=10,
    )
    assert isinstance(results, list)
    return results


def _by_path(results: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {r["payload"]["path"]: r for r in results}


def _assert_signal_kept(results: List[Dict[str, Any]]) -> None:
    rows = _by_path(results)
    assert set(rows) == {GOOD_FILE, BROKEN_FILE}
    assert rows[BROKEN_FILE]["staleness"]["content_unavailable"] is True
    assert rows[BROKEN_FILE]["staleness"]["is_stale"] is True
    assert "content_unavailable" not in rows[GOOD_FILE]["staleness"]


def _enhanced(results: List[Dict[str, Any]], repo: Path) -> Any:
    from code_indexer.api_clients.remote_query_client import QueryResultItem
    from code_indexer.remote.staleness_detector import StalenessDetector

    items = [
        QueryResultItem(
            similarity_score=r["score"],
            file_path=r["payload"]["path"],
            line_number=r["payload"]["line_start"],
            code_snippet=r["payload"]["content"],
            repository_alias=repo.name,
            file_last_modified=None,
            indexed_timestamp=None,
        )
        for r in results
    ]
    return StalenessDetector().apply_staleness_detection(items, repo, mode="local")


@pytest.mark.parametrize("preserve_order", [True, False])
def test_cli_annotate_staleness_keeps_content_unavailable(repo, preserve_order):
    from code_indexer.cli import _annotate_staleness

    results = _store_results(repo)
    annotated = _annotate_staleness(
        results, _enhanced(results, repo), preserve_order=preserve_order
    )

    _assert_signal_kept(annotated)


@pytest.mark.parametrize("quiet", [True, False])
def test_display_semantic_results_shows_marker(repo, quiet):
    from rich.console import Console

    from code_indexer.cli import _display_semantic_results

    console = Console(file=io.StringIO(), width=200, record=True)
    _display_semantic_results(
        results=_store_results(repo),
        console=console,
        quiet=quiet,
        current_display_branch="main",
    )
    output = console.export_text()

    assert output.count(UNAVAILABLE_MARKER) == 1
    assert "def image_generator" in output
    marker_line = next(ln for ln in output.splitlines() if UNAVAILABLE_MARKER in ln)
    assert marker_line.startswith("  ")


def _server_items() -> List[Any]:
    """Rows as the remote client parses them from a server response."""
    from code_indexer.server.models.api_models import QueryResultItem

    return [
        QueryResultItem(
            similarity_score=0.9,
            file_path=BROKEN_FILE,
            line_number=1,
            code_snippet="",
            repository_alias="example-repo",
            file_last_modified=None,
            indexed_timestamp=None,
            content_unavailable=True,
        ),
        QueryResultItem(
            similarity_score=0.8,
            file_path=GOOD_FILE,
            line_number=1,
            code_snippet="def image_generator():\n    return 'thumb'",
            repository_alias="example-repo",
            file_last_modified=None,
            indexed_timestamp=None,
        ),
    ]


def test_remote_staleness_detection_carries_content_unavailable(tmp_path):
    from code_indexer.remote.staleness_detector import StalenessDetector

    enhanced = StalenessDetector().apply_staleness_detection(
        _server_items(), tmp_path, mode="remote"
    )

    flags = {e.file_path: e.content_unavailable for e in enhanced}
    assert flags == {BROKEN_FILE: True, GOOD_FILE: False}


@pytest.mark.parametrize("quiet", [True, False])
def test_remote_mode_query_shows_marker(tmp_path, quiet):
    from unittest.mock import Mock, patch

    from click.testing import CliRunner

    from code_indexer.cli import query
    from code_indexer.remote.staleness_detector import StalenessDetector

    (tmp_path / ".code-indexer").mkdir()
    enhanced = StalenessDetector().apply_staleness_detection(
        _server_items(), tmp_path, mode="remote"
    )
    with patch(
        "code_indexer.disabled_commands.detect_current_mode", return_value="remote"
    ):
        with patch(
            "code_indexer.remote.query_execution.execute_remote_query",
            return_value=enhanced,
        ):
            result = CliRunner().invoke(
                query,
                ["image generator"] + (["--quiet"] if quiet else []),
                obj={
                    "mode": "remote",
                    "project_root": tmp_path,
                    "config_manager": Mock(),
                },
            )

    assert result.exit_code == 0, result.output
    assert result.output.count(UNAVAILABLE_MARKER) == 1
    assert f"  {UNAVAILABLE_MARKER}" in result.output
    assert "def image_generator" in result.output


def test_daemon_staleness_metadata_keeps_content_unavailable(repo):
    from code_indexer.daemon.service import CIDXDaemonService

    results = _store_results(repo)
    CIDXDaemonService()._apply_staleness_metadata(results, str(repo))

    _assert_signal_kept(results)
