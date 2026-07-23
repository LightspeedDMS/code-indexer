"""Bug #1407 Phase 2 (also resolves #1411): _get_commit_history() must
enumerate the FULL reachable commit universe -- NO last_commit..HEAD cursor
narrowing. A stale/buggy global cursor in temporal_meta.json must not hide
older commits from the universe fetch; per-embedder set-difference
(temporal_incremental_gate.py) is now solely responsible for skip logic.
"""

import json
import subprocess
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

from code_indexer.config import Config
from code_indexer.services.temporal.temporal_indexer import TemporalIndexer
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


def _run_git(args: List[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


def _init_repo_with_commits(tmp_path: Path, n: int) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-q"], repo)
    _run_git(["config", "user.email", "test@example.com"], repo)
    _run_git(["config", "user.name", "Test User"], repo)
    for i in range(n):
        (repo / f"f{i}.txt").write_text(f"content {i}\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", f"commit {i}"], repo)
    return repo


def _make_indexer(repo: Path, index_dir: Path) -> TemporalIndexer:
    index_dir.mkdir(parents=True, exist_ok=True)
    vector_store = FilesystemVectorStore(base_path=index_dir, project_root=repo)
    config = Config(codebase_dir=repo)
    config.embedding_provider = "voyage-ai"
    config_manager = MagicMock()
    config_manager.get_config.return_value = config
    config_manager.config_path = repo / ".code-indexer" / "config.json"
    return TemporalIndexer(
        config_manager, vector_store, collection_name="code-indexer-temporal-fake"
    )


class TestCursorNarrowingRemoved:
    def test_get_commit_history_ignores_stale_last_commit_cursor(self, tmp_path):
        repo = _init_repo_with_commits(tmp_path, 3)
        index_dir = tmp_path / "index"
        indexer = _make_indexer(repo, index_dir)

        all_commits = indexer._get_commit_history(
            all_branches=False, max_commits=None, since_date=None
        )
        assert len(all_commits) == 3

        # Simulate a stale temporal_meta.json cursor pointing at the LAST
        # commit (as if a prior buggy/partial run advanced it).
        indexer.temporal_dir.mkdir(parents=True, exist_ok=True)
        (indexer.temporal_dir / "temporal_meta.json").write_text(
            json.dumps({"last_commit": all_commits[-1].hash})
        )

        # A fresh TemporalIndexer instance re-reads temporal_meta.json from
        # disk (mirrors a real process restart between runs).
        indexer2 = _make_indexer(repo, index_dir)
        commits_after_cursor = indexer2._get_commit_history(
            all_branches=False, max_commits=None, since_date=None
        )

        assert len(commits_after_cursor) == 3, (
            "the stale last_commit cursor must NOT narrow the git-log "
            "fetch -- _get_commit_history always returns the FULL universe"
        )

    def test_load_last_indexed_commit_method_removed(self):
        """Dead code (Messi #12 anti-orphan-code): the cursor-narrowing
        helper must be deleted entirely, not left unreachable."""
        assert not hasattr(TemporalIndexer, "_load_last_indexed_commit")
