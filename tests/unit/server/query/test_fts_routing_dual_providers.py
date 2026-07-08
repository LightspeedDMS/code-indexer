"""Regression: FTS/hybrid must not be hijacked by the dual-provider parallel
semantic path (the "dual-embedder FTS" bug).

When both embedding providers are configured, query_strategy auto-set to
"parallel", whose block runs a dual-provider SEMANTIC search and RETURNS before
the FTS branch -- so an fts query silently became a slow dual-provider embedding
search (and returned semantic results). Fix: only auto-parallel for
search_mode == "semantic".
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.query.semantic_query_manager import SemanticQueryManager


@pytest.fixture
def manager():
    with tempfile.TemporaryDirectory() as d:
        yield SemanticQueryManager(data_dir=d, activated_repo_manager=MagicMock())


def _search(manager, repo_path, search_mode):
    manager._search_single_repository(
        repo_path=repo_path,
        repository_alias="my-repo",
        query_text="q",
        limit=10,
        min_score=None,
        file_extensions=None,
        search_mode=search_mode,
    )


def test_fts_with_dual_providers_runs_fts_not_parallel(manager, tmp_path):
    repo_path = str(tmp_path / "my-repo")
    Path(repo_path).mkdir(parents=True, exist_ok=True)

    with (
        patch.object(manager, "_both_providers_configured", return_value=True),
        patch.object(manager, "_execute_fts_search", return_value=[]) as mock_fts,
        patch.object(manager, "_search_with_provider", return_value=[]) as mock_par,
    ):
        _search(manager, repo_path, "fts")

    mock_fts.assert_called_once()  # FTS branch reached (the fix)
    mock_par.assert_not_called()  # dual-provider parallel semantic must NOT run


def test_semantic_with_dual_providers_still_runs_parallel(manager, tmp_path):
    # Control: plain semantic must still auto-parallel (fix doesn't regress it).
    repo_path = str(tmp_path / "my-repo")
    Path(repo_path).mkdir(parents=True, exist_ok=True)

    with (
        patch.object(manager, "_both_providers_configured", return_value=True),
        patch.object(manager, "_execute_fts_search", return_value=[]) as mock_fts,
        patch.object(manager, "_search_with_provider", return_value=[]) as mock_par,
    ):
        _search(manager, repo_path, "semantic")

    mock_par.assert_called()  # parallel semantic ran
    mock_fts.assert_not_called()
