"""
Regression tests for Bug #1317 -- golden-repo registry-orphans.

A "registry-orphan" is a `golden_repos` row (PostgreSQL in cluster mode,
SQLite in solo mode) with NO corresponding on-disk clone and/or NO alias
pointer file. This module proves the provisioning-atomicity guard:

(a) A clone-creation failure during add_golden_repo() must never leave a
    dangling registry row -- the background job raises and the row is
    absent from both the in-memory cache and the shared backend.

(b) A global-activation failure (i.e. the alias pointer file could not be
    written) must roll back the just-inserted registry row -- a
    "successfully-provisioned" global repo MUST always have its alias
    pointer file; if the pointer can't be written, registration is not
    considered successful and nothing is left dangling.

(c) remove_golden_repo() must remove the registry row BEFORE touching
    on-disk files. If registry removal fails, on-disk files must be left
    completely untouched (the old ordering deleted files first, which
    could leave a "row survives with no clone" orphan if the subsequent
    registry delete then failed).

Uses the REAL GoldenRepoManager, REAL SQLite backend, and REAL
GlobalActivator/AliasManager (filesystem + SQLite-backed registry) -- only
the BackgroundJobManager (a pure threading/dispatch concern) is mocked, so
job submission is captured and the worker closure is invoked synchronously
in the test thread.
"""

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepoManager,
    GoldenRepo,
    GitOperationError,
)
from code_indexer.server.repositories.background_jobs import BackgroundJobManager


@pytest.mark.e2e
class TestGoldenRepoRegistryOrphanBug1317:
    """Provisioning-atomicity guard tests for golden-repo registry-orphans."""

    @pytest.fixture
    def temp_data_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            yield temp_dir

    @pytest.fixture
    def mock_background_job_manager(self):
        mock_manager = MagicMock(spec=BackgroundJobManager)
        mock_manager.submit_job.return_value = "test-job-id-1317"
        return mock_manager

    @pytest.fixture
    def manager(self, temp_data_dir, mock_background_job_manager):
        mgr = GoldenRepoManager(data_dir=temp_data_dir)
        mgr.background_job_manager = mock_background_job_manager

        # Initialize the full DB schema (incl. global_repos table) so
        # GlobalActivator's SQLite-backed registry works end-to-end, matching
        # the pattern in test_golden_repo_manager_locking.py's cascade test.
        from code_indexer.server.storage.database_manager import DatabaseSchema

        DatabaseSchema(mgr.db_path).initialize_database()
        return mgr

    def _captured_worker(self, manager):
        """Return the func passed to the most recent submit_job() call."""
        call_args = manager.background_job_manager.submit_job.call_args
        return call_args[1]["func"]

    def _register_existing_repo(self, manager, alias: str) -> str:
        """Register alias in the SQLite backend + in-memory cache with a
        real on-disk clone directory, mirroring a completed add_golden_repo().
        """
        clone_path = os.path.join(manager.golden_repos_dir, alias)
        os.makedirs(clone_path, exist_ok=True)
        golden_repo = GoldenRepo(
            alias=alias,
            repo_url=f"https://github.com/test/{alias}.git",
            default_branch="main",
            clone_path=clone_path,
            created_at=datetime.now(timezone.utc).isoformat(),
            enable_temporal=False,
            temporal_options=None,
        )
        manager.golden_repos[alias] = golden_repo
        manager._sqlite_backend.add_repo(
            alias=golden_repo.alias,
            repo_url=golden_repo.repo_url,
            default_branch=golden_repo.default_branch,
            clone_path=golden_repo.clone_path,
            created_at=golden_repo.created_at,
            enable_temporal=golden_repo.enable_temporal,
            temporal_options=golden_repo.temporal_options,
        )
        return clone_path

    # ------------------------------------------------------------------
    # (a) Clone-creation failure leaves no dangling registry row
    # ------------------------------------------------------------------

    def test_clone_failure_leaves_no_dangling_registry_row(self, manager):
        """Bug #1317(a): a clone failure must raise AND leave zero trace of
        the alias in either the in-memory cache or the shared SQLite backend.
        """
        with (
            patch.object(manager, "_validate_git_repository", return_value=True),
            patch.object(
                manager,
                "_clone_repository",
                side_effect=GitOperationError("simulated clone failure"),
            ),
        ):
            manager.add_golden_repo(
                repo_url="https://github.com/test/new-repo.git",
                alias="new-repo",
            )
            background_worker = self._captured_worker(manager)

            with pytest.raises(GitOperationError):
                background_worker()

        assert "new-repo" not in manager.golden_repos
        assert manager._sqlite_backend.get_repo("new-repo") is None

    # ------------------------------------------------------------------
    # (b) Global-activation failure rolls back the registry row
    # ------------------------------------------------------------------

    def test_successful_registration_writes_alias_pointer(self, manager):
        """Bug #1317(b) happy path: a successfully-provisioned global repo
        always has its alias pointer file written."""
        with (
            patch.object(manager, "_validate_git_repository", return_value=True),
            patch.object(manager, "_clone_repository", return_value=None) as mock_clone,
            patch.object(manager, "_execute_post_clone_workflow", return_value=None),
        ):
            clone_path = os.path.join(manager.golden_repos_dir, "good-repo")
            os.makedirs(clone_path, exist_ok=True)
            mock_clone.return_value = clone_path

            manager.add_golden_repo(
                repo_url="https://github.com/test/good-repo.git",
                alias="good-repo",
                default_branch="main",
            )
            background_worker = self._captured_worker(manager)
            result = background_worker()

        assert result["success"] is True
        assert manager._sqlite_backend.get_repo("good-repo") is not None

        from code_indexer.global_repos.alias_manager import AliasManager

        alias_manager = AliasManager(os.path.join(manager.golden_repos_dir, "aliases"))
        assert alias_manager.alias_exists("good-repo-global")

    def test_activation_failure_rolls_back_dangling_registry_row(self, manager):
        """Bug #1317(b): when the alias pointer cannot be written (global
        activation fails), the golden_repos row that was just inserted must
        be rolled back -- never left dangling with no pointer.
        """
        with (
            patch.object(manager, "_validate_git_repository", return_value=True),
            patch.object(manager, "_clone_repository", return_value=None) as mock_clone,
            patch.object(manager, "_execute_post_clone_workflow", return_value=None),
            patch(
                "code_indexer.global_repos.global_activation.GlobalActivator.activate_golden_repo",
                side_effect=RuntimeError("simulated disk-full on alias write"),
            ),
        ):
            clone_path = os.path.join(manager.golden_repos_dir, "bad-repo")
            os.makedirs(clone_path, exist_ok=True)
            mock_clone.return_value = clone_path

            manager.add_golden_repo(
                repo_url="https://github.com/test/bad-repo.git",
                alias="bad-repo",
                default_branch="main",
            )
            background_worker = self._captured_worker(manager)

            with pytest.raises(GitOperationError):
                background_worker()

        # No dangling row -- rolled back from both cache and shared backend.
        assert "bad-repo" not in manager.golden_repos
        assert manager._sqlite_backend.get_repo("bad-repo") is None

        # Orphaned clone directory cleaned up too (retry must be possible).
        assert not os.path.exists(clone_path)

    # ------------------------------------------------------------------
    # (c) remove_golden_repo: registry removal happens before file deletion
    # ------------------------------------------------------------------

    def test_registry_removal_failure_never_touches_files(self, manager):
        """Bug #1317(c): if removing the registry row fails, on-disk files
        must be left completely untouched -- proves the row-then-files
        ordering (the old code deleted files first).
        """
        clone_path = self._register_existing_repo(manager, "orphan-risk-repo")

        with (
            patch.object(
                manager._sqlite_backend,
                "remove_repo",
                side_effect=RuntimeError("simulated transient DB failure"),
            ),
            patch.object(manager, "_cleanup_repository_files") as mock_cleanup,
        ):
            manager.remove_golden_repo("orphan-risk-repo")
            background_worker = self._captured_worker(manager)

            with pytest.raises(GitOperationError):
                background_worker()

            mock_cleanup.assert_not_called()

        # Files were never touched; the clone directory still exists.
        assert os.path.exists(clone_path)
        # Registry row is unaffected (the mocked remove_repo never actually
        # deleted the real row).
        assert manager._sqlite_backend.get_repo("orphan-risk-repo") is not None

    def test_successful_removal_deletes_row_and_files(self, manager):
        """Sanity check: the happy path still removes both the registry row
        and the on-disk clone directory."""
        clone_path = self._register_existing_repo(manager, "removable-repo")

        manager.remove_golden_repo("removable-repo")
        background_worker = self._captured_worker(manager)
        result = background_worker()

        assert result["success"] is True
        assert manager._sqlite_backend.get_repo("removable-repo") is None
        assert not os.path.exists(clone_path)
