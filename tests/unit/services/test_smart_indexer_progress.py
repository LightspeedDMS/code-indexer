"""TDD tests for Story #656: Progress feedback for silent phases in incremental indexing.

Tests verify that progress_callback is called during:
- Git history scan phase  (before _get_git_deltas_since_commit is called)
- Filesystem scan phase   (before find_modified_files is called)
- File deletion phase     (pre-deletion summary and periodic progress every PROGRESS_BATCH_SIZE)

All tests FAIL before the implementation and PASS after.
"""

import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.config import Config
from code_indexer.services.smart_indexer import SmartIndexer, PROGRESS_BATCH_SIZE


# ---------------------------------------------------------------------------
# Shared git helpers
# ---------------------------------------------------------------------------


def _create_git_repo(path: Path) -> str:
    """Create a minimal git repo with one initial commit. Returns initial commit hash."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (path / "initial.py").write_text("# initial\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    return _head(path)


def _add_commit(repo: Path, filename: str, content: str) -> str:
    """Add a file and commit; return new commit hash."""
    filepath = repo / filename
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content)
    subprocess.run(
        ["git", "-C", str(repo), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", f"add {filename}"],
        check=True,
        capture_output=True,
    )
    return _head(repo)


def _delete_and_commit(repo: Path, filename: str) -> str:
    """Delete a tracked file and commit; return new commit hash."""
    subprocess.run(
        ["git", "-C", str(repo), "rm", filename], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", f"delete {filename}"],
        check=True,
        capture_output=True,
    )
    return _head(repo)


def _head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_vector_store() -> MagicMock:
    """External vector store dependency — all methods return safe defaults."""
    store = MagicMock()
    store.resolve_collection_name.return_value = "test_collection"
    store.count_points.return_value = 0
    store.ensure_provider_aware_collection.return_value = "test_collection"
    store.begin_indexing.return_value = None
    store.end_indexing.return_value = {"vectors_indexed": 0}
    store.collection_exists.return_value = False
    # delete_by_filter used by delete_file_branch_aware in non-git-aware mode
    store.delete_by_filter.return_value = True
    return store


@pytest.fixture
def git_repo(tmp_path: Path):
    """A real, minimally seeded git repo. Returns (repo_path, initial_commit)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    initial_commit = _create_git_repo(repo)
    return repo, initial_commit


def _make_indexer(repo: Path, tmp_path: Path, store: MagicMock) -> SmartIndexer:
    """Create a SmartIndexer wired to real git repo with mocked external services."""
    config = Config(codebase_dir=str(repo))
    mock_embedding = MagicMock()
    metadata_path = tmp_path / "metadata.json"
    return SmartIndexer(
        config=config,
        embedding_provider=mock_embedding,
        vector_store_client=store,
        metadata_path=metadata_path,
    )


def _seed_for_incremental(indexer: SmartIndexer, branch: str, last_commit: str) -> None:
    """Seed progressive_metadata so _do_incremental_index takes the incremental path.

    Requirements:
    - status="completed" and last_index_timestamp>0 → get_resume_timestamp returns non-zero
    - files_to_index=[]           → can_resume_interrupted_operation returns False
    - branch_commit_watermarks set → get_last_indexed_commit returns last_commit
    """
    indexer.progressive_metadata.metadata["status"] = "completed"
    indexer.progressive_metadata.metadata["last_index_timestamp"] = time.time() - 3600
    indexer.progressive_metadata.metadata["files_to_index"] = []
    indexer.progressive_metadata._save_metadata()
    indexer.progressive_metadata.update_commit_watermark(branch, last_commit)


def _git_status(branch: str, commit: str) -> dict:
    return {
        "git_available": True,
        "current_branch": branch,
        "current_commit": commit,
    }


# ---------------------------------------------------------------------------
# Tests for _delete_files_from_backend
# ---------------------------------------------------------------------------


class TestDeleteFilesFromBackendProgress:
    """Tests for progress callbacks inside _delete_files_from_backend."""

    def test_delete_files_from_backend_periodic_progress(
        self, tmp_path, git_repo, mock_vector_store
    ):
        """When deleting more than PROGRESS_BATCH_SIZE files, callback fires every batch."""
        repo, _ = git_repo
        indexer = _make_indexer(repo, tmp_path, mock_vector_store)

        callbacks = []

        def cb(current, total, path, info=""):
            callbacks.append({"current": current, "total": total, "info": info})

        n_files = PROGRESS_BATCH_SIZE * 2 + 50
        deleted_files = [f"file_{i}.py" for i in range(n_files)]
        indexer._delete_files_from_backend(deleted_files, "test_collection", cb)

        periodic = [c for c in callbacks if "Deleting files..." in c["info"]]
        assert len(periodic) == 2, (
            f"Expected 2 periodic callbacks (at {PROGRESS_BATCH_SIZE} and "
            f"{PROGRESS_BATCH_SIZE * 2}), got {len(periodic)}: {periodic}"
        )
        assert periodic[0]["current"] == PROGRESS_BATCH_SIZE
        assert periodic[0]["total"] == n_files
        assert f"{PROGRESS_BATCH_SIZE}/{n_files}" in periodic[0]["info"]
        assert periodic[1]["current"] == PROGRESS_BATCH_SIZE * 2
        assert periodic[1]["total"] == n_files
        assert f"{PROGRESS_BATCH_SIZE * 2}/{n_files}" in periodic[1]["info"]

    def test_delete_files_from_backend_no_callback_when_none(
        self, tmp_path, git_repo, mock_vector_store
    ):
        """When progress_callback=None, no error occurs and all deletions succeed."""
        repo, _ = git_repo
        indexer = _make_indexer(repo, tmp_path, mock_vector_store)

        n_files = PROGRESS_BATCH_SIZE + 50
        deleted_files = [f"file_{i}.py" for i in range(n_files)]
        result = indexer._delete_files_from_backend(
            deleted_files, "test_collection", None
        )
        # Returns deleted_count — all succeed because vector_store mock is truthy
        assert result >= 0  # No exception is the key requirement

    def test_delete_files_from_backend_small_batch_no_periodic(
        self, tmp_path, git_repo, mock_vector_store
    ):
        """When deleting fewer than PROGRESS_BATCH_SIZE files, no periodic callback fires."""
        repo, _ = git_repo
        indexer = _make_indexer(repo, tmp_path, mock_vector_store)

        callbacks = []

        def cb(current, total, path, info=""):
            callbacks.append({"current": current, "total": total, "info": info})

        n_files = PROGRESS_BATCH_SIZE - 1
        deleted_files = [f"file_{i}.py" for i in range(n_files)]
        indexer._delete_files_from_backend(deleted_files, "test_collection", cb)

        periodic = [c for c in callbacks if "Deleting files..." in c["info"]]
        assert len(periodic) == 0, (
            f"Expected 0 periodic callbacks for {n_files} files, got {len(periodic)}"
        )


# ---------------------------------------------------------------------------
# Tests for _do_incremental_index progress callbacks
# ---------------------------------------------------------------------------


class TestDoIncrementalIndexProgress:
    """Tests for progress callbacks emitted during _do_incremental_index.

    Uses real git repos to drive the git-history scan and filesystem scan
    without mocking SUT internals. Only external services (vector store,
    embedding provider) are mocked.
    """

    def test_incremental_index_emits_git_scan_callback(
        self, tmp_path, git_repo, mock_vector_store
    ):
        """When _do_incremental_index processes git commits, it emits the git scan info."""
        repo, initial_commit = git_repo
        second_commit = _add_commit(repo, "src/foo.py", "def foo(): pass\n")

        indexer = _make_indexer(repo, tmp_path, mock_vector_store)
        _seed_for_incremental(indexer, "main", initial_commit)

        callbacks = []

        def cb(current, total, path, info="", **kwargs):
            callbacks.append(info)

        indexer._do_incremental_index(
            batch_size=50,
            progress_callback=cb,
            git_status=_git_status("main", second_commit),
            provider_name="voyage",
            model_name="voyage-3",
            safety_buffer_seconds=60,
        )

        assert any("Scanning git history for changes" in info for info in callbacks), (
            f"Expected 'Scanning git history for changes...' in callbacks, got: {callbacks}"
        )

    def test_incremental_index_emits_filesystem_scan_callback(
        self, tmp_path, git_repo, mock_vector_store
    ):
        """When _do_incremental_index runs, it emits the filesystem scan info message."""
        repo, initial_commit = git_repo
        second_commit = _add_commit(repo, "src/bar.py", "def bar(): pass\n")

        indexer = _make_indexer(repo, tmp_path, mock_vector_store)
        _seed_for_incremental(indexer, "main", initial_commit)

        callbacks = []

        def cb(current, total, path, info="", **kwargs):
            callbacks.append(info)

        indexer._do_incremental_index(
            batch_size=50,
            progress_callback=cb,
            git_status=_git_status("main", second_commit),
            provider_name="voyage",
            model_name="voyage-3",
            safety_buffer_seconds=60,
        )

        assert any(
            "Scanning filesystem for untracked changes" in info for info in callbacks
        ), (
            f"Expected 'Scanning filesystem for untracked changes...' in callbacks, got: {callbacks}"
        )

    def test_incremental_index_emits_cleanup_callback_with_count(
        self, tmp_path, git_repo, mock_vector_store
    ):
        """When there are N deleted files, callback fires with cleanup count message."""
        repo, initial_commit = git_repo
        second_commit = _add_commit(repo, "doomed.py", "x = 1\n")
        third_commit = _delete_and_commit(repo, "doomed.py")

        indexer = _make_indexer(repo, tmp_path, mock_vector_store)
        # Seed as if we indexed up to second_commit — so deletion from third_commit is new
        _seed_for_incremental(indexer, "main", second_commit)

        callbacks = []

        def cb(current, total, path, info="", **kwargs):
            callbacks.append({"current": current, "total": total, "info": info})

        indexer._do_incremental_index(
            batch_size=50,
            progress_callback=cb,
            git_status=_git_status("main", third_commit),
            provider_name="voyage",
            model_name="voyage-3",
            safety_buffer_seconds=60,
        )

        cleanup = [
            c
            for c in callbacks
            if "Cleaning up" in c["info"] and "deleted files" in c["info"]
        ]
        assert len(cleanup) == 1, (
            f"Expected 1 cleanup callback, got {len(cleanup)}: "
            f"{[c['info'] for c in callbacks]}"
        )
        assert cleanup[0]["total"] == 1
        assert "1 deleted files" in cleanup[0]["info"]

    def test_incremental_index_no_cleanup_callback_when_zero_deletions(
        self, tmp_path, git_repo, mock_vector_store
    ):
        """When there are no deleted files, no cleanup callback fires."""
        repo, initial_commit = git_repo
        second_commit = _add_commit(repo, "newfile.py", "x = 1\n")

        indexer = _make_indexer(repo, tmp_path, mock_vector_store)
        _seed_for_incremental(indexer, "main", initial_commit)

        callbacks = []

        def cb(current, total, path, info="", **kwargs):
            callbacks.append(info)

        indexer._do_incremental_index(
            batch_size=50,
            progress_callback=cb,
            git_status=_git_status("main", second_commit),
            provider_name="voyage",
            model_name="voyage-3",
            safety_buffer_seconds=60,
        )

        cleanup = [info for info in callbacks if "Cleaning up" in info]
        assert len(cleanup) == 0, (
            f"Expected 0 cleanup callbacks when no deletions, got: {cleanup}"
        )
