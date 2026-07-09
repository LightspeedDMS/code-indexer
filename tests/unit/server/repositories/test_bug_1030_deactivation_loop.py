"""
Tests for Bug #1030: Activated reaper deactivation loop on phantom repos.

Root Cause (Fix A):
  _do_deactivate_repository() raises ActivatedRepoError when metadata is None,
  causing the reaper to repeatedly reschedule jobs for phantom repos that can
  never be cleaned up.

Fix A:
  When metadata is None, perform orphan cleanup (remove repo dir if exists,
  delete metadata if present) and return success instead of raising.
  This makes deactivation IDEMPOTENT.

Root Cause (Fix B, tested separately in test_bug_1030_reaper_skip_empty.py):
  The reaper submits jobs for repos with empty username/user_alias which can
  never succeed.
"""

import os
import shutil
import tempfile
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
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
    background_job_manager.submit_job.return_value = "job-test-1030"
    return ActivatedRepoManager(
        data_dir=data_dir,
        golden_repo_manager=golden_repo_manager,
        background_job_manager=background_job_manager,
    )


# ---------------------------------------------------------------------------
# Tests for Fix A: idempotent deactivation when metadata is None
# ---------------------------------------------------------------------------


class TestBug1030IdempotentDeactivation:
    """Regression tests for Bug #1030 Fix A — deactivation loop on phantom repos."""

    def setup_method(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.manager = _make_manager(self.temp_dir)

    def teardown_method(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_bug_1030_deactivate_no_metadata_no_dir_succeeds(self) -> None:
        """
        When neither metadata nor repo dir exists, deactivation must succeed.

        This is the "already gone" case. Before the fix, this raised
        ActivatedRepoError("Metadata not found for repository 'ghost-repo'"),
        causing the reaper to reschedule the same failing job every cycle.
        """
        username = "alice"
        user_alias = "ghost-repo"

        # Verify nothing exists (clean state)
        repo_dir = os.path.join(self.manager.activated_repos_dir, username, user_alias)
        assert not os.path.exists(repo_dir)
        assert self.manager._load_metadata(username, user_alias) is None

        # Must NOT raise — deactivation of an already-gone repo must succeed
        result = self.manager._do_deactivate_repository(username, user_alias)

        assert result["success"] is True
        assert (
            "already deactivated" in result["message"].lower()
            or "ghost-repo" in result["message"]
        )

    def test_bug_1030_deactivate_no_metadata_dir_exists_cleans_up(self) -> None:
        """
        When repo dir exists but metadata is absent, deactivation must:
        1. Remove the repo directory
        2. Return success (not raise)

        This is the "orphan dir" case — dir left over after a partial cleanup.
        """
        username = "bob"
        user_alias = "orphan-repo"

        # Create orphan directory (no metadata file)
        repo_dir = os.path.join(self.manager.activated_repos_dir, username, user_alias)
        os.makedirs(repo_dir, exist_ok=True)
        # Create a dummy file inside to make the dir non-empty
        with open(os.path.join(repo_dir, "dummy.txt"), "w") as f:
            f.write("orphan content")

        # Verify dir exists but metadata is absent
        assert os.path.exists(repo_dir)
        assert self.manager._load_metadata(username, user_alias) is None

        # Must NOT raise
        result = self.manager._do_deactivate_repository(username, user_alias)

        assert result["success"] is True
        # Directory must have been removed
        assert not os.path.exists(repo_dir), "Orphan repo dir must be removed"

    def test_bug_1030_deactivate_no_metadata_warns_not_errors(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        Orphan cleanup must log at WARNING level, not ERROR level.

        ERROR level would trigger admin alerts for expected cleanup operations.
        """
        import logging

        username = "carol"
        user_alias = "warn-repo"

        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.server.repositories.activated_repo_manager",
        ):
            result = self.manager._do_deactivate_repository(username, user_alias)

        assert result["success"] is True
        # Should not have raised to ERROR level for orphan cleanup
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        # No ERROR logs for this normal orphan-cleanup scenario
        assert len(error_records) == 0, (
            f"Expected no ERROR logs for orphan cleanup, got: {error_records}"
        )

    def test_bug_1030_deactivate_valid_metadata_still_works(self) -> None:
        """
        Regression guard: normal deactivation with valid metadata must still work.

        Ensures Fix A doesn't break the happy path.
        """
        username = "dave"
        user_alias = "normal-repo"

        # Create repo dir
        repo_dir = os.path.join(self.manager.activated_repos_dir, username, user_alias)
        os.makedirs(repo_dir, exist_ok=True)
        with open(os.path.join(repo_dir, "file.txt"), "w") as f:
            f.write("content")

        # Create valid metadata (FS mode)
        metadata = {
            "user_alias": user_alias,
            "username": username,
            "golden_repo_alias": "golden-repo",
            "path": repo_dir,
            "current_branch": "main",
            "activated_at": "2025-01-01T00:00:00+00:00",
            "last_accessed": "2025-01-01T00:00:00+00:00",
            "is_composite": False,
        }
        self.manager._save_metadata(username, user_alias, metadata)

        # Verify metadata exists
        assert self.manager._load_metadata(username, user_alias) is not None

        # Normal deactivation must succeed
        result = self.manager._do_deactivate_repository(username, user_alias)

        assert result["success"] is True
        # Repo dir must be gone
        assert not os.path.exists(repo_dir)
        # Metadata must be gone
        assert self.manager._load_metadata(username, user_alias) is None

    def test_bug_1030_loop_broken_phantom_stays_gone_after_deactivation(self) -> None:
        """
        The deactivation loop is broken because after deactivation succeeds,
        the phantom repo is no longer returned by list_all_activated_repositories.

        This test verifies: deactivate phantom -> metadata stays None -> no more scheduling.
        Simulates multiple reaper cycles: after the first succeeds, subsequent
        deactivations also succeed (idempotent), so the reaper won't infinitely fail.
        """
        username = "eve"
        user_alias = "phantom-repo"

        # First deactivation — phantom with no metadata, no dir
        result1 = self.manager._do_deactivate_repository(username, user_alias)
        assert result1["success"] is True

        # Second deactivation — also succeeds (idempotent)
        result2 = self.manager._do_deactivate_repository(username, user_alias)
        assert result2["success"] is True

        # No artifacts remain
        repo_dir = os.path.join(self.manager.activated_repos_dir, username, user_alias)
        assert not os.path.exists(repo_dir)
        assert self.manager._load_metadata(username, user_alias) is None
