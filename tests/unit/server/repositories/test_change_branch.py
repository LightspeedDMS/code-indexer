"""Tests for GoldenRepoManager.change_branch() (Story #303).

Covers:
- AC1: Successful branch change lifecycle (fetch, validate, checkout, index, CoW, swap)
- AC2: Error handling for repo not found
- AC3: Error handling for nonexistent remote branch
- AC4: No-op when target branch equals current branch
- Lock acquisition / release lifecycle
- SQLite backend update_default_branch
"""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoManager,
    GoldenRepoNotFoundError,
)
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path):
    """Return a temp data directory with the golden-repos sub-directory."""
    d = tmp_path / "data"
    d.mkdir()
    (d / "golden-repos").mkdir()
    return str(d)


@pytest.fixture
def manager(data_dir):
    """GoldenRepoManager pre-populated with one golden repo on branch 'main'."""
    mgr = GoldenRepoManager(data_dir=data_dir)
    mgr.golden_repos["my-repo"] = GoldenRepo(
        alias="my-repo",
        repo_url="https://github.com/org/repo.git",
        default_branch="main",
        clone_path="/golden-repos/my-repo",
        created_at="2025-01-01T00:00:00Z",
    )
    # Mock the SQLite backend so tests stay fast and side-effect free.
    mgr._sqlite_backend = MagicMock()
    mgr.resource_config = None
    return mgr


# ---------------------------------------------------------------------------
# AC1: Successful branch change lifecycle
# ---------------------------------------------------------------------------


class TestChangeBranchSuccess:
    """Verify the complete happy-path lifecycle of change_branch."""

    def test_change_branch_calls_git_fetch(self, manager):
        """git fetch origin is invoked during a branch change."""
        with (
            patch.object(manager, "_cb_git_fetch_and_validate") as mock_fetch,
            patch.object(manager, "_cb_checkout_and_pull"),
            patch.object(manager, "_cb_cidx_index"),
            patch.object(manager, "_cb_cow_snapshot", return_value="/snap/v_1"),
            patch.object(manager, "_cb_swap_alias"),
        ):
            manager.change_branch("my-repo", "feature-x")

        mock_fetch.assert_called_once_with(
            "/golden-repos/my-repo", "feature-x", manager._CB_GIT_TIMEOUT
        )

    def test_change_branch_calls_git_checkout(self, manager):
        """git checkout + pull are invoked for the target branch."""
        with (
            patch.object(manager, "_cb_git_fetch_and_validate"),
            patch.object(manager, "_cb_checkout_and_pull") as mock_co,
            patch.object(manager, "_cb_cidx_index"),
            patch.object(manager, "_cb_cow_snapshot", return_value="/snap/v_1"),
            patch.object(manager, "_cb_swap_alias"),
        ):
            manager.change_branch("my-repo", "feature-x")

        mock_co.assert_called_once_with(
            "/golden-repos/my-repo", "feature-x", manager._CB_GIT_TIMEOUT
        )

    def test_change_branch_calls_cidx_index(self, manager):
        """cidx index is run on the base clone after checkout."""
        with (
            patch.object(manager, "_cb_git_fetch_and_validate"),
            patch.object(manager, "_cb_checkout_and_pull"),
            patch.object(manager, "_cb_cidx_index") as mock_idx,
            patch.object(manager, "_cb_cow_snapshot", return_value="/snap/v_1"),
            patch.object(manager, "_cb_swap_alias"),
        ):
            manager.change_branch("my-repo", "feature-x")

        mock_idx.assert_called_once_with(
            "/golden-repos/my-repo", manager._CB_INDEX_TIMEOUT
        )

    def test_change_branch_creates_cow_snapshot(self, manager):
        """A CoW snapshot is created and its path is passed to swap."""
        with (
            patch.object(manager, "_cb_git_fetch_and_validate"),
            patch.object(manager, "_cb_checkout_and_pull"),
            patch.object(manager, "_cb_cidx_index"),
            patch.object(
                manager, "_cb_cow_snapshot", return_value="/snap/v_999"
            ) as mock_cow,
            patch.object(manager, "_cb_swap_alias") as mock_swap,
        ):
            manager.change_branch("my-repo", "feature-x")

        mock_cow.assert_called_once()
        mock_swap.assert_called_once_with("my-repo", "/snap/v_999")

    def test_change_branch_updates_sqlite_and_in_memory(self, manager):
        """After a successful change the SQLite backend and in-memory state are updated."""
        with (
            patch.object(manager, "_cb_git_fetch_and_validate"),
            patch.object(manager, "_cb_checkout_and_pull"),
            patch.object(manager, "_cb_cidx_index"),
            patch.object(manager, "_cb_cow_snapshot", return_value="/snap/v_1"),
            patch.object(manager, "_cb_swap_alias"),
        ):
            result = manager.change_branch("my-repo", "feature-x")

        assert result["success"] is True
        assert "feature-x" in result["message"]
        manager._sqlite_backend.update_default_branch.assert_called_once_with(
            "my-repo", "feature-x"
        )
        assert manager.golden_repos["my-repo"].default_branch == "feature-x"

    def test_change_branch_invalidates_tracking_metadata(self, manager):
        """After branch change, tracking metadata is invalidated for re-processing."""
        with (
            patch.object(manager, "_cb_git_fetch_and_validate"),
            patch.object(manager, "_cb_checkout_and_pull"),
            patch.object(manager, "_cb_cidx_index"),
            patch.object(manager, "_cb_cow_snapshot", return_value="/snap/v_1"),
            patch.object(manager, "_cb_swap_alias"),
        ):
            manager.change_branch("my-repo", "feature-x")

        manager._sqlite_backend.invalidate_description_refresh_tracking.assert_called_once_with("my-repo")
        manager._sqlite_backend.invalidate_dependency_map_tracking.assert_called_once_with("my-repo")

    def test_change_branch_step_order(self, manager):
        """Steps execute in order: fetch/validate → checkout/pull → index → CoW → swap."""
        call_order = []

        with (
            patch.object(
                manager,
                "_cb_git_fetch_and_validate",
                side_effect=lambda *a, **kw: call_order.append("fetch"),
            ),
            patch.object(
                manager,
                "_cb_checkout_and_pull",
                side_effect=lambda *a, **kw: call_order.append("checkout"),
            ),
            patch.object(
                manager,
                "_cb_cidx_index",
                side_effect=lambda *a, **kw: call_order.append("index"),
            ),
            patch.object(
                manager,
                "_cb_cow_snapshot",
                side_effect=lambda *a, **kw: call_order.append("cow") or "/snap/v_1",
            ),
            patch.object(
                manager,
                "_cb_swap_alias",
                side_effect=lambda *a, **kw: call_order.append("swap"),
            ),
        ):
            manager.change_branch("my-repo", "feature-x")

        assert call_order == ["fetch", "checkout", "index", "cow", "swap"]


# ---------------------------------------------------------------------------
# AC4: No-op when target branch equals current branch
# ---------------------------------------------------------------------------


class TestChangeBranchNoop:
    """same branch → returns immediately with success, no git operations."""

    def test_same_branch_returns_early_without_git_ops(self, manager):
        """Switching to the already-active branch is a fast no-op."""
        with (
            patch.object(manager, "_cb_git_fetch_and_validate") as mock_fetch,
            patch.object(manager, "_cb_checkout_and_pull") as mock_co,
            patch.object(manager, "_cb_cidx_index") as mock_idx,
            patch.object(manager, "_cb_cow_snapshot") as mock_cow,
            patch.object(manager, "_cb_swap_alias") as mock_swap,
        ):
            result = manager.change_branch("my-repo", "main")

        assert result["success"] is True
        assert "main" in result["message"]
        mock_fetch.assert_not_called()
        mock_co.assert_not_called()
        mock_idx.assert_not_called()
        mock_cow.assert_not_called()
        mock_swap.assert_not_called()

    def test_same_branch_does_not_update_sqlite(self, manager):
        """SQLite backend is NOT called when branch is already active."""
        manager.change_branch("my-repo", "main")
        manager._sqlite_backend.update_default_branch.assert_not_called()

    def test_same_branch_does_not_invalidate_tracking(self, manager):
        """Tracking metadata is NOT invalidated when branch is already active."""
        manager.change_branch("my-repo", "main")
        manager._sqlite_backend.invalidate_description_refresh_tracking.assert_not_called()
        manager._sqlite_backend.invalidate_dependency_map_tracking.assert_not_called()


# ---------------------------------------------------------------------------
# AC2 / AC3: Error handling
# ---------------------------------------------------------------------------


class TestChangeBranchErrors:
    """Verify error semantics for non-happy paths."""

    def test_repo_not_found_raises_golden_repo_not_found_error(self, manager):
        """Unknown alias raises GoldenRepoNotFoundError."""
        with pytest.raises(GoldenRepoNotFoundError):
            manager.change_branch("no-such-repo", "main")

    def test_nonexistent_branch_raises_value_error(self, manager):
        """Branch absent on remote raises ValueError (propagated from _cb_git_fetch_and_validate)."""
        with (
            patch.object(
                manager,
                "_cb_git_fetch_and_validate",
                side_effect=ValueError("Branch 'ghost' does not exist on remote."),
            ),
        ):
            with pytest.raises(ValueError, match="ghost"):
                manager.change_branch("my-repo", "ghost")

    def test_git_fetch_failure_raises_runtime_error(self, manager):
        """A failing subprocess during fetch propagates as CalledProcessError / RuntimeError."""
        with (
            patch.object(
                manager,
                "_cb_git_fetch_and_validate",
                side_effect=subprocess.CalledProcessError(
                    returncode=1, cmd=["git", "fetch", "origin"]
                ),
            ),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                manager.change_branch("my-repo", "feature-x")

    def test_write_lock_conflict_raises_runtime_error(self, manager):
        """When the repo is already write-locked, RuntimeError is raised."""
        mock_scheduler = MagicMock()
        mock_scheduler.is_write_locked.return_value = True
        manager._refresh_scheduler = mock_scheduler

        with pytest.raises(RuntimeError, match="indexed or refreshed"):
            manager.change_branch("my-repo", "feature-x")

    def test_acquire_write_lock_failure_raises_runtime_error(self, manager):
        """When acquire_write_lock returns False, RuntimeError is raised."""
        mock_scheduler = MagicMock()
        mock_scheduler.is_write_locked.return_value = False
        mock_scheduler.acquire_write_lock.return_value = False
        manager._refresh_scheduler = mock_scheduler

        with pytest.raises(RuntimeError, match="Could not acquire write lock"):
            manager.change_branch("my-repo", "feature-x")

    def test_lock_released_on_error(self, manager):
        """write lock is released in the finally block even when an exception occurs."""
        mock_scheduler = MagicMock()
        mock_scheduler.is_write_locked.return_value = False
        mock_scheduler.acquire_write_lock.return_value = True
        manager._refresh_scheduler = mock_scheduler

        with (
            patch.object(
                manager,
                "_cb_git_fetch_and_validate",
                side_effect=RuntimeError("git exploded"),
            ),
        ):
            with pytest.raises(RuntimeError, match="git exploded"):
                manager.change_branch("my-repo", "feature-x")

        mock_scheduler.release_write_lock.assert_called_once_with(
            "my-repo", owner_name="branch_change"
        )

    def test_lock_released_on_success_too(self, manager):
        """write lock is released after a successful branch change as well."""
        mock_scheduler = MagicMock()
        mock_scheduler.is_write_locked.return_value = False
        mock_scheduler.acquire_write_lock.return_value = True
        manager._refresh_scheduler = mock_scheduler

        with (
            patch.object(manager, "_cb_git_fetch_and_validate"),
            patch.object(manager, "_cb_checkout_and_pull"),
            patch.object(manager, "_cb_cidx_index"),
            patch.object(manager, "_cb_cow_snapshot", return_value="/snap/v_1"),
            patch.object(manager, "_cb_swap_alias"),
        ):
            manager.change_branch("my-repo", "feature-x")

        mock_scheduler.release_write_lock.assert_called_once_with(
            "my-repo", owner_name="branch_change"
        )


# ---------------------------------------------------------------------------
# SQLite backend: update_default_branch
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path):
    """Real temporary SQLite database initialised with the full schema."""
    db_path = str(tmp_path / "test.db")
    db_schema = DatabaseSchema(db_path)
    db_schema.initialize_database()
    return db_path


@pytest.fixture
def sqlite_backend(temp_db):
    """GoldenRepoMetadataSqliteBackend backed by the temp DB."""
    backend = GoldenRepoMetadataSqliteBackend(temp_db)
    backend.ensure_table_exists()
    return backend


# ---------------------------------------------------------------------------
# _cb_cidx_index subprocess flags
# ---------------------------------------------------------------------------


class TestCbCidxIndex:
    """Verify _cb_cidx_index passes --fts flag to cidx index (without --clear).

    The HNSW filtered rebuild now handles branch isolation by rebuilding the
    HNSW index with only visible-branch files. The --clear flag is no longer
    needed because filtered rebuild eliminates ghost vectors without destroying
    the underlying vector JSON files (preserving expensive VoyageAI embeddings).
    """

    def test_cb_cidx_index_uses_fts_flag_without_clear(self, manager, tmp_path):
        """_cb_cidx_index must run cidx index --fts (without --clear).

        The HNSW filtered rebuild eliminates ghost vectors without the need
        to wipe the entire index. Removing --clear preserves VoyageAI embeddings.
        """
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            manager._cb_cidx_index(str(tmp_path), 300)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["cidx", "index", "--fts"], (
            f"Expected ['cidx', 'index', '--fts'] but got {cmd}. "
            "The --clear flag should be removed since HNSW filtered rebuild "
            "handles ghost vector elimination without destroying vector files."
        )
        assert "--clear" not in cmd, (
            "--clear must NOT be in the cidx index command. "
            "HNSW filtered rebuild handles branch isolation instead."
        )

    def test_cb_cidx_index_uses_correct_cwd(self, manager, tmp_path):
        """_cb_cidx_index must run cidx index in the given base_clone_path directory."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            manager._cb_cidx_index(str(tmp_path), 300)

        mock_run.assert_called_once()
        kwargs = mock_run.call_args[1]
        assert kwargs["cwd"] == str(tmp_path)


class TestUpdateDefaultBranchSqlite:
    """SQLite backend update_default_branch (Story #303)."""

    def test_updates_default_branch_column(self, sqlite_backend):
        """update_default_branch persists the new branch name in the database."""
        sqlite_backend.add_repo(
            alias="my-repo",
            repo_url="https://github.com/org/repo.git",
            default_branch="main",
            clone_path="/golden-repos/my-repo",
            created_at="2025-01-01T00:00:00Z",
        )

        sqlite_backend.update_default_branch("my-repo", "develop")

        row = sqlite_backend.get_repo("my-repo")
        assert row is not None
        assert row["default_branch"] == "develop"

    def test_updates_are_idempotent(self, sqlite_backend):
        """Calling update_default_branch twice with the same value leaves a consistent state."""
        sqlite_backend.add_repo(
            alias="my-repo",
            repo_url="https://github.com/org/repo.git",
            default_branch="main",
            clone_path="/golden-repos/my-repo",
            created_at="2025-01-01T00:00:00Z",
        )

        sqlite_backend.update_default_branch("my-repo", "release")
        sqlite_backend.update_default_branch("my-repo", "release")

        row = sqlite_backend.get_repo("my-repo")
        assert row["default_branch"] == "release"

    def test_update_nonexistent_alias_is_noop(self, sqlite_backend):
        """update_default_branch on a missing alias does not raise (documented no-op)."""
        # Should not raise
        sqlite_backend.update_default_branch("ghost-repo", "main")

    def test_invalidate_description_refresh_tracking(self, sqlite_backend):
        """invalidate_description_refresh_tracking sets last_known_commit to NULL."""
        import json

        sqlite_backend._conn_manager.execute_atomic(
            lambda conn: conn.execute(
                "INSERT OR REPLACE INTO description_refresh_tracking (repo_alias, last_known_commit) VALUES (?, ?)",
                ("my-repo", "abc123"),
            )
        )

        sqlite_backend.invalidate_description_refresh_tracking("my-repo")

        conn = sqlite_backend._conn_manager.get_connection()
        row = conn.execute(
            "SELECT last_known_commit FROM description_refresh_tracking WHERE repo_alias = ?",
            ("my-repo",),
        ).fetchone()
        assert row is not None
        assert row[0] is None  # Should be NULL after invalidation

    def test_invalidate_dependency_map_tracking(self, sqlite_backend):
        """invalidate_dependency_map_tracking removes alias from commit_hashes JSON."""
        import json

        hashes = {"my-repo": "abc123", "other-repo": "def456"}
        sqlite_backend._conn_manager.execute_atomic(
            lambda conn: conn.execute(
                "INSERT OR REPLACE INTO dependency_map_tracking (id, commit_hashes) VALUES (1, ?)",
                (json.dumps(hashes),),
            )
        )

        sqlite_backend.invalidate_dependency_map_tracking("my-repo")

        conn = sqlite_backend._conn_manager.get_connection()
        row = conn.execute(
            "SELECT commit_hashes FROM dependency_map_tracking WHERE id = 1"
        ).fetchone()
        result = json.loads(row[0])
        assert "my-repo" not in result
        assert result["other-repo"] == "def456"

    def test_does_not_affect_other_repos(self, sqlite_backend):
        """Updating branch of one repo leaves other repos' branch unchanged."""
        sqlite_backend.add_repo(
            alias="repo-a",
            repo_url="https://github.com/org/a.git",
            default_branch="main",
            clone_path="/golden-repos/repo-a",
            created_at="2025-01-01T00:00:00Z",
        )
        sqlite_backend.add_repo(
            alias="repo-b",
            repo_url="https://github.com/org/b.git",
            default_branch="main",
            clone_path="/golden-repos/repo-b",
            created_at="2025-01-01T00:00:00Z",
        )

        sqlite_backend.update_default_branch("repo-a", "hotfix")

        row_b = sqlite_backend.get_repo("repo-b")
        assert row_b["default_branch"] == "main"
