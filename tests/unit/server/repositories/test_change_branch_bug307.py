"""Tests for Bug #307: FTS branch isolation cleanup post-CoW snapshot.

Root cause: FTS cleanup runs on base clone before CoW snapshot is created.
Tantivy commit may not fully persist to segment files before CoW copy,
or the cleanup is lost because CoW copies the pre-cleanup state.

Fix: Add _cb_fts_branch_cleanup(snapshot_path, target_branch) step in
change_branch() AFTER _cb_cow_snapshot(). This method:
1. Runs 'git ls-files' on the versioned snapshot to get files in target_branch
2. Opens the FTS index inside the snapshot's .code-indexer/tantivy_index/
3. Deletes FTS documents for any files NOT returned by git ls-files
4. Commits the Tantivy index
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoManager,
)


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
    mgr._sqlite_backend = MagicMock()
    mgr.resource_config = None
    return mgr


# ---------------------------------------------------------------------------
# TestCbFtsBranchCleanup: tests for _cb_fts_branch_cleanup() method
# ---------------------------------------------------------------------------


class TestCbFtsBranchCleanup:
    """Tests for GoldenRepoManager._cb_fts_branch_cleanup() (Bug #307)."""

    def test_fts_branch_cleanup_skips_when_no_fts_index(self, manager, tmp_path):
        """_cb_fts_branch_cleanup() is a no-op when FTS index does not exist."""
        snapshot_path = str(tmp_path / "snapshot_v1")
        (tmp_path / "snapshot_v1").mkdir()
        # No .code-indexer/tantivy_index/ directory created

        # Should not raise - silently skips
        manager._cb_fts_branch_cleanup(snapshot_path, "master")

    def test_fts_branch_cleanup_deletes_documents_not_in_branch(
        self, manager, tmp_path
    ):
        """_cb_fts_branch_cleanup() deletes FTS docs for files not in git ls-files."""
        snapshot_path = tmp_path / "snapshot_v1"
        snapshot_path.mkdir()
        fts_index_dir = snapshot_path / ".code-indexer" / "tantivy_index"
        fts_index_dir.mkdir(parents=True)

        # git ls-files returns 2 files on master
        git_ls_output = "src/auth.py\nsrc/user.py\n"
        target_branch = "master"

        mock_fts_manager = MagicMock()

        with (
            patch("subprocess.run") as mock_run,
            patch(
                "code_indexer.services.tantivy_index_manager.TantivyIndexManager",
                return_value=mock_fts_manager,
            ) as mock_tantivy_cls,
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=git_ls_output,
                stderr="",
            )
            # FTS has 3 indexed paths (3rd is dev-only, not in master)
            mock_fts_manager.get_all_indexed_paths.return_value = [
                "src/auth.py",
                "src/user.py",
                "src/wiki_cache_invalidator.py",
            ]

            manager._cb_fts_branch_cleanup(str(snapshot_path), target_branch)

        # TantivyIndexManager created with snapshot's FTS dir
        mock_tantivy_cls.assert_called_once_with(fts_index_dir)
        mock_fts_manager.initialize_index.assert_called_once_with(create_new=False)
        # Only the dev-only file was deleted
        mock_fts_manager.delete_document.assert_called_once_with(
            "src/wiki_cache_invalidator.py"
        )
        mock_fts_manager.commit.assert_called_once()

    def test_fts_branch_cleanup_called_after_cow_snapshot_in_change_branch(
        self, manager
    ):
        """change_branch() calls _cb_fts_branch_cleanup() after _cb_cow_snapshot()."""
        call_order = []

        def record_cow(*args, **kwargs):
            call_order.append("cow_snapshot")
            return "/snap/v_1"

        def record_fts(*args, **kwargs):
            call_order.append("fts_cleanup")

        with (
            patch.object(manager, "_cb_git_fetch_and_validate"),
            patch.object(manager, "_cb_checkout_and_pull"),
            patch.object(manager, "_cb_cidx_index"),
            patch.object(manager, "_cb_cow_snapshot", side_effect=record_cow),
            patch.object(manager, "_cb_fts_branch_cleanup", side_effect=record_fts),
            patch.object(manager, "_cb_hnsw_branch_cleanup"),
            patch.object(manager, "_cb_swap_alias"),
        ):
            manager.change_branch("my-repo", "feature-x")

        assert "cow_snapshot" in call_order, "cow_snapshot must be called"
        assert "fts_cleanup" in call_order, "_cb_fts_branch_cleanup must be called"
        cow_idx = call_order.index("cow_snapshot")
        fts_idx = call_order.index("fts_cleanup")
        assert fts_idx > cow_idx, (
            f"_cb_fts_branch_cleanup (idx={fts_idx}) must be called AFTER "
            f"_cb_cow_snapshot (idx={cow_idx})"
        )


# ---------------------------------------------------------------------------
# TestCbHnswBranchCleanup: tests for _cb_hnsw_branch_cleanup() method
# ---------------------------------------------------------------------------


class TestCbHnswBranchCleanup:
    """Tests for GoldenRepoManager._cb_hnsw_branch_cleanup() (belt-and-suspenders fix)."""

    def test_cb_hnsw_branch_cleanup_skips_when_no_index_dir(self, manager, tmp_path):
        """_cb_hnsw_branch_cleanup() is a no-op when .code-indexer/index/ does not exist."""
        snapshot_path = str(tmp_path / "snapshot_v1")
        (tmp_path / "snapshot_v1").mkdir()
        # No .code-indexer/index/ directory

        # Should not raise - silently skips
        manager._cb_hnsw_branch_cleanup(snapshot_path, "master")

    def test_cb_hnsw_branch_cleanup_calls_rebuild_hnsw_filtered(
        self, manager, tmp_path
    ):
        """_cb_hnsw_branch_cleanup() calls rebuild_hnsw_filtered for each collection."""
        snapshot_path = tmp_path / "snapshot_v1"
        snapshot_path.mkdir()
        index_dir = snapshot_path / ".code-indexer" / "index"
        index_dir.mkdir(parents=True)

        # Create a fake collection directory with collection_meta.json
        collection_dir = index_dir / "my-collection"
        collection_dir.mkdir()
        (collection_dir / "collection_meta.json").write_text('{"vector_size": 1024}')

        git_ls_output = "src/auth.py\nsrc/user.py\n"
        target_branch = "master"

        mock_store = MagicMock()
        mock_store.list_collections.return_value = ["my-collection"]

        with (
            patch("subprocess.run") as mock_run,
            patch(
                "code_indexer.storage.filesystem_vector_store.FilesystemVectorStore",
                return_value=mock_store,
            ) as mock_store_cls,
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=git_ls_output,
                stderr="",
            )

            manager._cb_hnsw_branch_cleanup(str(snapshot_path), target_branch)

        # FilesystemVectorStore created with the index dir
        mock_store_cls.assert_called_once_with(index_dir)
        # rebuild_hnsw_filtered called for the collection
        mock_store.rebuild_hnsw_filtered.assert_called_once_with(
            "my-collection",
            visible_files={"src/auth.py", "src/user.py"},
            current_branch=target_branch,
        )

    def test_hnsw_branch_cleanup_called_after_fts_cleanup_in_change_branch(
        self, manager
    ):
        """change_branch() calls _cb_hnsw_branch_cleanup() after _cb_fts_branch_cleanup()."""
        call_order = []

        def record_cow(*args, **kwargs):
            call_order.append("cow_snapshot")
            return "/snap/v_1"

        def record_fts(*args, **kwargs):
            call_order.append("fts_cleanup")

        def record_hnsw(*args, **kwargs):
            call_order.append("hnsw_cleanup")

        def record_swap(*args, **kwargs):
            call_order.append("swap_alias")

        with (
            patch.object(manager, "_cb_git_fetch_and_validate"),
            patch.object(manager, "_cb_checkout_and_pull"),
            patch.object(manager, "_cb_cidx_index"),
            patch.object(manager, "_cb_cow_snapshot", side_effect=record_cow),
            patch.object(manager, "_cb_fts_branch_cleanup", side_effect=record_fts),
            patch.object(manager, "_cb_hnsw_branch_cleanup", side_effect=record_hnsw),
            patch.object(manager, "_cb_swap_alias", side_effect=record_swap),
        ):
            manager.change_branch("my-repo", "feature-x")

        assert "hnsw_cleanup" in call_order, "_cb_hnsw_branch_cleanup must be called"
        fts_idx = call_order.index("fts_cleanup")
        hnsw_idx = call_order.index("hnsw_cleanup")
        swap_idx = call_order.index("swap_alias")
        assert hnsw_idx > fts_idx, (
            f"_cb_hnsw_branch_cleanup (idx={hnsw_idx}) must be called AFTER "
            f"_cb_fts_branch_cleanup (idx={fts_idx})"
        )
        assert hnsw_idx < swap_idx, (
            f"_cb_hnsw_branch_cleanup (idx={hnsw_idx}) must be called BEFORE "
            f"_cb_swap_alias (idx={swap_idx})"
        )
