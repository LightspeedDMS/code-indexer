"""Bug #2056 (P3-6): a golden repo whose FTS index was built before the
fix -- possibly partly emptied, undetectably -- carries no current FTS
content marker. A refresh with no upstream change must NOT skip it: it runs
the normal `cidx index --fts` (which rebuilds the FTS index once from disk)
and publishes as usual. With a current marker, or no FTS index at all, the
no-change refresh still skips.

The decision is tested on real files; the refresh wiring reuses the Bug
#1508 refresh harness unchanged, with a REAL Tantivy index in the base
clone.
"""

import json
from pathlib import Path
from typing import Optional
from unittest.mock import Mock

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.services.fts_file_documents import (
    FTS_CONTENT_VERSION,
    FTS_CONTENT_VERSION_FILE,
)
from code_indexer.services.fts_lifecycle import (
    fts_content_rebuild_due,
    fts_index_dir_for_repo,
)
from code_indexer.services.tantivy_index_manager import TantivyIndexManager
from tests.unit.global_repos.test_refresh_scheduler_stale_index_1508 import (
    _run_execute_refresh,
)

ALIAS = "my-repo-global"


@pytest.fixture
def golden_repos_dir(tmp_path: Path) -> Path:
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir(parents=True)
    return golden_dir


@pytest.fixture
def scheduler(golden_repos_dir: Path) -> RefreshScheduler:
    """As in the Bug #1508 module: plain stand-ins for collaborators."""
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    registry = Mock()
    registry.get_global_repo.return_value = {
        "alias_name": ALIAS,
        "repo_url": "git@example.com:org/my-repo.git",
        "default_branch": "main",
    }
    registry.list_global_repos.return_value = []
    registry.update_refresh_timestamp.return_value = None
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config,
        query_tracker=Mock(spec=QueryTracker),
        cleanup_manager=Mock(spec=CleanupManager),
        registry=registry,
    )


def _repo_with_fts(root: Path, marker: Optional[str]) -> Path:
    """`root` with up-to-date indexing metadata (HEAD cccc111, completed) and
    a real FTS index holding one document; `marker` is the content marker's
    text (None: no marker, as every pre-#2056 index)."""
    meta_dir = root / ".code-indexer"
    meta_dir.mkdir(parents=True)
    (meta_dir / "metadata.json").write_text(
        json.dumps({"status": "completed", "current_commit": "cccc111"})
    )
    index_dir = fts_index_dir_for_repo(root)
    fts = TantivyIndexManager(index_dir)
    fts.initialize_index(create_new=True)
    try:
        fts.add_document(
            {
                "path": "src/a.py",
                "content": "TOKEN",
                "content_raw": "TOKEN",
                "identifiers": ["TOKEN"],
                "line_start": 1,
                "line_end": 1,
                "language": "py",
            }
        )
        fts.commit()
    finally:
        fts.close()
    if marker is not None:
        (index_dir / FTS_CONTENT_VERSION_FILE).write_text(marker)
    return root


_STALE_MARKERS = pytest.mark.parametrize(
    "marker", [None, str(FTS_CONTENT_VERSION - 1)], ids=["missing", "older"]
)


class TestFtsContentRebuildDue2056:
    @_STALE_MARKERS
    def test_index_with_stale_marker_is_due(self, tmp_path: Path, marker) -> None:
        assert fts_content_rebuild_due(_repo_with_fts(tmp_path / "r", marker))

    def test_index_with_current_marker_is_not_due(self, tmp_path: Path) -> None:
        repo = _repo_with_fts(tmp_path / "r", str(FTS_CONTENT_VERSION))
        assert not fts_content_rebuild_due(repo)

    def test_repo_without_fts_index_is_not_due(self, tmp_path: Path) -> None:
        (tmp_path / "r" / ".code-indexer").mkdir(parents=True)
        assert not fts_content_rebuild_due(tmp_path / "r")


class TestNoChangeRefreshRebuildsPreFixFtsOnce2056:
    @_STALE_MARKERS
    def test_stale_marker_runs_the_normal_fts_indexing(
        self, scheduler, golden_repos_dir, marker
    ) -> None:
        master = _repo_with_fts(golden_repos_dir / "my-repo", marker)

        result, index_source_calls = _run_execute_refresh(
            scheduler, golden_repos_dir, ALIAS, str(master)
        )

        assert len(index_source_calls) == 1, "the normal `cidx index --fts` runs"
        _args, kwargs = index_source_calls[0]
        assert kwargs["force_reconcile"] is False
        assert result["success"] is True
        assert result.get("message") != "No changes detected"

    def test_current_marker_still_skips(self, scheduler, golden_repos_dir) -> None:
        master = _repo_with_fts(golden_repos_dir / "my-repo", str(FTS_CONTENT_VERSION))

        result, index_source_calls = _run_execute_refresh(
            scheduler, golden_repos_dir, ALIAS, str(master)
        )

        assert index_source_calls == []
        assert result["message"] == "No changes detected"
