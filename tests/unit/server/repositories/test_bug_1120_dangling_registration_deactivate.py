"""
Tests for Bug #1120: Dangling activated-repo registration cannot be deactivated.

Root cause:
  deactivate_repository() gates on os.path.exists(repo_dir) BEFORE checking the
  authoritative registry (PG row or metadata JSON file).  When the on-disk
  directory is gone but the registration row/file still exists the method raises
  ActivatedRepoError("not found") — locking the user out of cleanup permanently.

Fix:
  Gate the "not found" decision on _load_metadata() alone (the authoritative
  registry source for both solo/cluster modes).  os.path.exists(repo_dir) is
  removed from the pre-flight check in deactivate_repository().

Security invariants preserved:
  - A truly-nonexistent (username, alias) with neither registration NOR dir still
    raises ActivatedRepoError (404 semantics).
  - A user can only deactivate their OWN registration (username scoping unchanged).
"""

import os
import shutil
import tempfile
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
    ActivatedRepoManager,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(data_dir: str) -> ActivatedRepoManager:
    """Return a minimal ActivatedRepoManager backed by a temp filesystem dir."""
    golden_repo_manager = MagicMock()
    golden_repo_manager.golden_repos = {}
    background_job_manager = MagicMock()
    background_job_manager.submit_job.return_value = "job-test-1120"
    return ActivatedRepoManager(
        data_dir=data_dir,
        golden_repo_manager=golden_repo_manager,
        background_job_manager=background_job_manager,
    )


def _write_metadata(
    manager: ActivatedRepoManager,
    username: str,
    user_alias: str,
    repo_path: str,
) -> None:
    """Write a minimal registration record without creating the on-disk directory."""
    metadata = {
        "user_alias": user_alias,
        "username": username,
        "golden_repo_alias": "golden-test-repo",
        "path": repo_path,
        "current_branch": "main",
        "activated_at": "2025-01-01T00:00:00+00:00",
        "last_accessed": "2025-01-01T00:00:00+00:00",
        "is_composite": False,
    }
    manager._save_metadata(username, user_alias, metadata)


# ---------------------------------------------------------------------------
# Bug #1120 tests
# ---------------------------------------------------------------------------


class TestBug1120DanglingRegistrationDeactivate:
    """
    Regression tests for Bug #1120.

    Covers:
    1. deactivate_repository() submits job when registration exists but dir is missing
    2. _do_deactivate_repository worker removes the registration when dir is missing
    3. Missing-dir deactivation logs WARNING not ERROR
    4. Truly-nonexistent registration still raises ActivatedRepoError (security)
    5. Wrong-user cannot deactivate another user's repo (ownership)
    """

    def setup_method(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.manager = _make_manager(self.temp_dir)

    def teardown_method(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_deactivate_repository_registration_exists_dir_missing_submits_job(
        self,
    ) -> None:
        """
        deactivate_repository() must submit the background job when the
        registration exists but the on-disk directory is gone.

        Before the fix this raised:
          ActivatedRepoError("Activated repository 'dangling' not found for user 'alice'")
        """
        username = "alice"
        user_alias = "dangling"
        repo_path = os.path.join(self.manager.activated_repos_dir, username, user_alias)
        _write_metadata(self.manager, username, user_alias, repo_path)

        # Confirm: registration exists but directory does NOT
        assert self.manager._load_metadata(username, user_alias) is not None
        assert not os.path.exists(repo_path), "Pre-condition: directory must be absent"

        # Must NOT raise — must return a job_id
        job_id = self.manager.deactivate_repository(username, user_alias)
        assert job_id == "job-test-1120"

    def test_do_deactivate_repository_registration_exists_dir_missing_cleans_registration(
        self,
    ) -> None:
        """
        _do_deactivate_repository() (the worker) must remove the registration
        and return success even when the on-disk directory is absent.
        """
        username = "bob"
        user_alias = "dangling-worker"
        repo_path = os.path.join(self.manager.activated_repos_dir, username, user_alias)
        _write_metadata(self.manager, username, user_alias, repo_path)

        assert self.manager._load_metadata(username, user_alias) is not None
        assert not os.path.exists(repo_path)

        result = self.manager._do_deactivate_repository(username, user_alias)

        assert result["success"] is True
        assert self.manager._load_metadata(username, user_alias) is None

    def test_do_deactivate_repository_dir_missing_logs_warning_not_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        When the on-disk directory is missing during deactivation, only
        a WARNING must be emitted — not an ERROR (ERRORs trigger admin alerts).
        """
        import logging

        username = "carol"
        user_alias = "dangling-warn"
        repo_path = os.path.join(self.manager.activated_repos_dir, username, user_alias)
        _write_metadata(self.manager, username, user_alias, repo_path)

        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.server.repositories.activated_repo_manager",
        ):
            result = self.manager._do_deactivate_repository(username, user_alias)

        assert result["success"] is True
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(error_records) == 0, (
            f"Expected no ERROR logs for dangling-dir deactivation, got: {error_records}"
        )

    def test_deactivate_repository_truly_nonexistent_still_raises(self) -> None:
        """
        A (username, alias) with NO registration AND no directory must still
        raise ActivatedRepoError — preserving 404 semantics.
        """
        username = "dave"
        user_alias = "never-activated"

        assert self.manager._load_metadata(username, user_alias) is None
        repo_path = os.path.join(self.manager.activated_repos_dir, username, user_alias)
        assert not os.path.exists(repo_path)

        with pytest.raises(ActivatedRepoError, match="not found"):
            self.manager.deactivate_repository(username, user_alias)

    def test_deactivate_repository_wrong_user_cannot_deactivate_other_users_repo(
        self,
    ) -> None:
        """
        A registration for user 'alice' must NOT be deactivatable by 'eve'
        (different username) — ownership scoping is preserved.
        """
        owner = "alice"
        attacker = "eve"
        user_alias = "shared-alias"
        repo_path = os.path.join(self.manager.activated_repos_dir, owner, user_alias)
        _write_metadata(self.manager, owner, user_alias, repo_path)

        assert self.manager._load_metadata(attacker, user_alias) is None

        with pytest.raises(ActivatedRepoError, match="not found"):
            self.manager.deactivate_repository(attacker, user_alias)


class TestBug1120AdminListPathExists:
    """
    Admin all-users listing must surface dangling registrations (metadata present,
    dir absent) with path_exists=False, consistent with the PG path.

    Per-user listing (list_activated_repositories) must continue to EXCLUDE dangling
    registrations — omni-search, query manager, and inline-repos consumers depend on
    this behaviour.
    """

    def setup_method(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.manager = _make_manager(self.temp_dir)

    def teardown_method(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_admin_list_includes_dangling_registration_with_path_exists_false(
        self,
    ) -> None:
        """
        list_all_activated_repositories() must include a registration whose
        metadata exists but whose on-disk directory is absent, and the returned
        dict must carry path_exists=False.
        """
        username = "alice"
        user_alias = "dangling-admin"
        repo_path = os.path.join(self.manager.activated_repos_dir, username, user_alias)
        _write_metadata(self.manager, username, user_alias, repo_path)

        # Confirm: metadata present, directory absent
        assert self.manager._load_metadata(username, user_alias) is not None
        assert not os.path.exists(repo_path)

        repos = self.manager.list_all_activated_repositories()

        assert len(repos) == 1, f"Expected 1 repo, got {repos}"
        repo = repos[0]
        assert repo["user_alias"] == user_alias
        assert repo["path_exists"] is False

    def test_admin_list_normal_repo_has_path_exists_true(self) -> None:
        """
        list_all_activated_repositories() must return path_exists=True for a
        registration whose on-disk directory exists.
        """
        username = "bob"
        user_alias = "healthy-repo"
        repo_path = os.path.join(self.manager.activated_repos_dir, username, user_alias)
        _write_metadata(self.manager, username, user_alias, repo_path)

        # Create the on-disk directory so this is a healthy registration
        os.makedirs(repo_path, exist_ok=True)

        repos = self.manager.list_all_activated_repositories()

        assert len(repos) == 1, f"Expected 1 repo, got {repos}"
        assert repos[0]["path_exists"] is True

    def test_per_user_list_still_excludes_dangling_registration(self) -> None:
        """
        list_activated_repositories(username) must NOT include a dangling
        registration (metadata present, dir absent).  This preserves existing
        behaviour relied upon by omni-search, query manager, repo-listing, and
        inline-repos route consumers.
        """
        username = "carol"
        user_alias = "dangling-per-user"
        repo_path = os.path.join(self.manager.activated_repos_dir, username, user_alias)
        _write_metadata(self.manager, username, user_alias, repo_path)

        assert not os.path.exists(repo_path)

        per_user_repos = self.manager.list_activated_repositories(username)
        assert per_user_repos == [], (
            f"Per-user listing must exclude dangling registrations, got: {per_user_repos}"
        )
