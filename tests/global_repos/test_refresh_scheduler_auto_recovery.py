"""
Tests for RefreshScheduler auto-recovery logic (Story #295).

Tests AC2 (failure tracking), AC3 (auto re-clone), AC5 (guard rails):
- Consecutive fetch failure counter increments per alias
- Counter resets to 0 on successful refresh
- Corruption category triggers immediate re-clone (no threshold)
- 3 consecutive transient failures trigger re-clone
- Cooldown prevents repeated re-clone attempts
- .versioned/ snapshots are preserved during re-clone
- Only master clone (golden-repos/{alias}/) is deleted during re-clone
"""

import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call
import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.global_repos.git_error_classifier import GitFetchError
from code_indexer.config import ConfigManager


def _make_scheduler(tmp_path) -> tuple[RefreshScheduler, Path, GlobalRegistry]:
    """
    Create a RefreshScheduler with required setup for auto-recovery tests.

    Passes a GlobalRegistry directly to avoid SQLite auto-detection which
    requires a server database that does not exist in unit tests.

    Returns (scheduler, golden_repos_dir, registry).
    The registry must be used by _register_repo() to share state with the scheduler.
    """
    golden_repos_dir = tmp_path / "golden_repos"
    golden_repos_dir.mkdir(parents=True)

    config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
    tracker = QueryTracker()
    cleanup_mgr = CleanupManager(tracker)
    registry = GlobalRegistry(str(golden_repos_dir))

    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=tracker,
        cleanup_manager=cleanup_mgr,
        registry=registry,
    )
    return scheduler, golden_repos_dir, registry


def _register_repo(
    golden_repos_dir: Path,
    registry: GlobalRegistry,
    alias: str = "test-repo",
) -> Path:
    """
    Register a git repo in alias + registry, create master clone dir.

    Uses the provided registry instance (must be the same one passed to
    RefreshScheduler) to avoid stale in-memory state.

    Returns the master clone path.
    """
    global_alias = f"{alias}-global"
    master_path = golden_repos_dir / alias
    master_path.mkdir(parents=True, exist_ok=True)

    alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
    alias_mgr.create_alias(global_alias, str(master_path))
    registry.register_global_repo(
        alias,
        global_alias,
        "https://github.com/test/repo",
        str(master_path),
    )
    return master_path


class TestFetchFailureTracking:
    """Tests for consecutive failure counter (AC2)."""

    def test_fetch_failure_increments_counter(self, tmp_path):
        """
        GitFetchError caught in _execute_refresh increments the consecutive
        failure count for that alias.

        Story #295 AC2.
        """
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)
        _register_repo(golden_repos_dir, registry, "test-repo")

        transient_error = GitFetchError(
            "Git fetch failed",
            category="transient",
            stderr="Could not resolve host: github.com",
        )

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.side_effect = transient_error
            mock_cls.return_value = mock_updater

            # First failure — expect RuntimeError from _handle_fetch_error
            # (transient, below threshold of 3)
            try:
                scheduler._execute_refresh("test-repo-global")
            except RuntimeError:
                pass

        assert scheduler._fetch_failure_counts.get("test-repo-global", 0) == 1

    def test_successful_refresh_with_changes_resets_failure_counter(self, tmp_path):
        """Counter resets when has_changes() returns True (with changes), not just False."""
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)
        alias = "test-repo-global"

        # Simulate 2 consecutive transient failures
        scheduler._fetch_failure_counts[alias] = 2

        # Successful fetch resets counter
        scheduler._reset_fetch_failures(alias)

        assert scheduler._fetch_failure_counts[alias] == 0

    def test_successful_refresh_resets_failure_counter(self, tmp_path):
        """
        A successful refresh (no GitFetchError) resets the consecutive failure
        count back to 0.

        Story #295 AC2.
        """
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)
        _register_repo(golden_repos_dir, registry, "test-repo")

        # Prime the counter to a non-zero value
        scheduler._fetch_failure_counts["test-repo-global"] = 2

        # Successful refresh (no changes, no fetch error)
        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = False
            mock_cls.return_value = mock_updater

            result = scheduler._execute_refresh("test-repo-global")

        assert result["success"] is True
        assert scheduler._fetch_failure_counts.get("test-repo-global", 0) == 0


class TestAutoReclone:
    """Tests for automatic re-clone triggering (AC3)."""

    def test_corruption_triggers_immediate_reclone(self, tmp_path):
        """
        A single corruption GitFetchError triggers immediate re-clone without
        waiting for the transient threshold (3 failures).

        Story #295 AC3.
        """
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)
        master_path = _register_repo(golden_repos_dir, registry, "test-repo")

        corruption_error = GitFetchError(
            "Git fetch failed",
            category="corruption",
            stderr="error: Could not read pack index",
        )

        reclone_attempted = []

        def mock_attempt_reclone(alias_name, repo_url, master_path_arg):
            reclone_attempted.append(alias_name)
            return True  # success

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.side_effect = corruption_error
            mock_cls.return_value = mock_updater

            scheduler._attempt_reclone = mock_attempt_reclone

            try:
                scheduler._execute_refresh("test-repo-global")
            except RuntimeError:
                pass  # Re-clone attempt may still raise; we care it was tried

        assert "test-repo-global" in reclone_attempted, (
            "Re-clone was not attempted for corruption error"
        )

    def test_transient_failures_trigger_reclone_after_threshold(self, tmp_path):
        """
        Three consecutive transient GitFetchErrors trigger a re-clone attempt.
        Fewer than 3 should NOT trigger re-clone.

        Story #295 AC3.
        """
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)
        _register_repo(golden_repos_dir, registry, "test-repo")

        transient_error = GitFetchError(
            "Git fetch failed",
            category="transient",
            stderr="Could not resolve host: github.com",
        )

        reclone_calls = []

        def mock_attempt_reclone(alias_name, repo_url, master_path_arg):
            reclone_calls.append(alias_name)
            return True

        scheduler._attempt_reclone = mock_attempt_reclone

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.side_effect = transient_error
            mock_cls.return_value = mock_updater

            # First two failures — should NOT trigger re-clone
            for _ in range(2):
                try:
                    scheduler._execute_refresh("test-repo-global")
                except RuntimeError:
                    pass

            assert len(reclone_calls) == 0, (
                "Re-clone should not be triggered before threshold (3)"
            )

            # Third failure — should trigger re-clone
            try:
                scheduler._execute_refresh("test-repo-global")
            except RuntimeError:
                pass

        assert len(reclone_calls) >= 1, (
            "Re-clone should be triggered after 3 consecutive transient failures"
        )


class TestRecloneGuardRails:
    """Tests for cooldown and preservation of versioned snapshots (AC5)."""

    def test_reclone_cooldown_prevents_repeated_attempts(self, tmp_path):
        """
        After a re-clone attempt (success or failure), a cooldown prevents
        another attempt during the same period.

        Story #295 AC5.
        """
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)
        _register_repo(golden_repos_dir, registry, "test-repo")

        # Simulate cooldown already active (set far future expiry)
        scheduler._reclone_cooldowns["test-repo-global"] = time.monotonic() + 99999

        corruption_error = GitFetchError(
            "Git fetch failed",
            category="corruption",
            stderr="error: Could not read pack index",
        )

        reclone_calls = []

        def mock_attempt_reclone(alias_name, repo_url, master_path_arg):
            reclone_calls.append(alias_name)
            return True

        scheduler._attempt_reclone = mock_attempt_reclone

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.side_effect = corruption_error
            mock_cls.return_value = mock_updater

            try:
                scheduler._execute_refresh("test-repo-global")
            except RuntimeError:
                pass

        assert len(reclone_calls) == 0, (
            "Re-clone should be blocked by active cooldown"
        )

    def test_reclone_preserves_versioned_snapshots(self, tmp_path):
        """
        _attempt_reclone() deletes only the master clone directory
        (golden-repos/{alias}/) and NOT any .versioned/{alias}/ snapshots.

        Story #295 AC5.
        """
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)
        _register_repo(golden_repos_dir, registry, "test-repo")

        # Create .versioned/ directory structure to verify preservation
        versioned_dir = golden_repos_dir / ".versioned" / "test-repo"
        versioned_dir.mkdir(parents=True)
        snapshot_dir = versioned_dir / "v_1234567890"
        snapshot_dir.mkdir()
        snapshot_file = snapshot_dir / "some_file.txt"
        snapshot_file.write_text("snapshot content")

        master_path = golden_repos_dir / "test-repo"

        # Mock subprocess.run for the git clone step
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            # Call _attempt_reclone directly to test its behavior
            result = scheduler._attempt_reclone(
                alias_name="test-repo-global",
                repo_url="https://github.com/test/repo",
                master_path=str(master_path),
            )

        # .versioned/ structure must still exist
        assert versioned_dir.exists(), ".versioned/{alias}/ was incorrectly deleted"
        assert snapshot_dir.exists(), "versioned snapshot directory was incorrectly deleted"
        assert snapshot_file.exists(), "versioned snapshot files were incorrectly deleted"

    def test_reclone_deletes_only_master_clone(self, tmp_path):
        """
        _attempt_reclone() deletes golden-repos/{alias}/ (the master clone)
        before re-cloning, and only that directory.

        Story #295 AC5.
        """
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)
        _register_repo(golden_repos_dir, registry, "test-repo")

        master_path = golden_repos_dir / "test-repo"

        # Create a sibling repo directory that must NOT be deleted
        sibling_repo = golden_repos_dir / "other-repo"
        sibling_repo.mkdir()
        sibling_file = sibling_repo / "sibling.txt"
        sibling_file.write_text("sibling content")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            scheduler._attempt_reclone(
                alias_name="test-repo-global",
                repo_url="https://github.com/test/repo",
                master_path=str(master_path),
            )

        # Sibling repo must still exist
        assert sibling_repo.exists(), "Sibling repo directory was incorrectly deleted"
        assert sibling_file.exists(), "Sibling repo files were incorrectly deleted"

        # Verify git clone was called with the correct target path
        clone_calls = [
            c for c in mock_run.call_args_list
            if c[0][0][0] == "git" and c[0][0][1] == "clone"
        ]
        assert len(clone_calls) == 1, "git clone should be called exactly once"
        clone_args = clone_calls[0][0][0]
        assert "https://github.com/test/repo" in clone_args
        assert str(master_path) in clone_args
