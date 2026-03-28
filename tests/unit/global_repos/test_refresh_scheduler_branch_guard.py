"""
Unit tests for RefreshScheduler branch guard fix (Bug #469 Fix 1).

Tests the branch guard that ensures the base clone is on the expected
default_branch before git pull, preventing wrong-branch contamination.

Tests:
1. Wrong branch triggers checkout to default_branch before pulling
2. Correct branch skips checkout (no unnecessary git ops)
3. Failed checkout logs error but refresh continues (no crash)
4. Missing default_branch in repo_info defaults to "main"
"""

import subprocess
from unittest.mock import Mock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_repos_dir(tmp_path):
    """Create a temporary golden-repos directory."""
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir(parents=True)
    return golden_dir


@pytest.fixture
def mock_query_tracker():
    return Mock(spec=QueryTracker)


@pytest.fixture
def mock_cleanup_manager():
    return Mock(spec=CleanupManager)


@pytest.fixture
def mock_config_source():
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    return config


@pytest.fixture
def mock_registry():
    registry = Mock()
    registry.get_global_repo.return_value = {
        "alias_name": "my-repo-global",
        "repo_url": "git@github.com:org/my-repo.git",
        "default_branch": "main",
    }
    registry.list_global_repos.return_value = []
    registry.update_refresh_timestamp.return_value = None
    return registry


@pytest.fixture
def scheduler(
    golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
):
    """Create RefreshScheduler with a mock registry."""
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
    )


def _proc(returncode=0, stdout="", stderr=""):
    """Build a completed-process mock."""
    result = Mock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# ---------------------------------------------------------------------------
# Helper: common patch context for _execute_refresh
# ---------------------------------------------------------------------------


def _common_patches(scheduler, golden_repos_dir, alias_name="my-repo-global"):
    """Return the standard set of patches for _execute_refresh testing."""
    versioned_path = str(golden_repos_dir / ".versioned" / "my-repo" / "v_1000000")

    return (
        patch.object(
            scheduler.alias_manager, "read_alias", return_value=versioned_path
        ),
        patch.object(scheduler.alias_manager, "swap_alias"),
        patch.object(scheduler, "_detect_existing_indexes", return_value={}),
        patch.object(scheduler, "_reconcile_registry_with_filesystem"),
        patch.object(scheduler, "_index_source"),
        patch.object(
            scheduler,
            "_create_snapshot",
            return_value=str(golden_repos_dir / ".versioned" / "my-repo" / "v_2000000"),
        ),
        patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
    )


# ---------------------------------------------------------------------------
# Test 1: Wrong branch triggers checkout to default_branch
# ---------------------------------------------------------------------------


class TestBranchGuardWrongBranch:
    """
    When the base clone is on a different branch than default_branch,
    the refresh scheduler must run 'git checkout <default_branch>' before pulling.
    """

    def test_wrong_branch_triggers_checkout_to_default_branch(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        When git branch --show-current returns a branch different from default_branch,
        git checkout <default_branch> must be called before the pull.
        """
        alias_name = "my-repo-global"
        master_path = str(golden_repos_dir / "my-repo")
        (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
            "default_branch": "main",
        }

        # Track subprocess.run calls to verify checkout happened
        checkout_calls = []

        def fake_subprocess_run(cmd, **kwargs):
            if cmd == ["git", "branch", "--show-current"]:
                # Report we are on 'develop' instead of 'main'
                return _proc(returncode=0, stdout="develop")
            if cmd == ["git", "checkout", "main"]:
                checkout_calls.append(cmd)
                return _proc(returncode=0)
            return _proc(returncode=0)

        mock_updater = Mock()
        mock_updater.has_changes.return_value = True
        mock_updater.get_source_path.return_value = master_path

        with (
            patch.object(
                scheduler.alias_manager,
                "read_alias",
                return_value=str(
                    golden_repos_dir / ".versioned" / "my-repo" / "v_1000000"
                ),
            ),
            patch.object(scheduler.alias_manager, "swap_alias"),
            patch.object(scheduler, "_detect_existing_indexes", return_value={}),
            patch.object(scheduler, "_reconcile_registry_with_filesystem"),
            patch.object(scheduler, "_index_source"),
            patch.object(
                scheduler,
                "_create_snapshot",
                return_value=str(
                    golden_repos_dir / ".versioned" / "my-repo" / "v_2000000"
                ),
            ),
            patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
            patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
                return_value=mock_updater,
            ),
            patch("subprocess.run", side_effect=fake_subprocess_run),
        ):
            scheduler._execute_refresh(alias_name)

        # Must have called git checkout main
        assert len(checkout_calls) == 1, (
            f"Expected 1 'git checkout main' call, got {len(checkout_calls)}"
        )
        assert checkout_calls[0] == ["git", "checkout", "main"]


# ---------------------------------------------------------------------------
# Test 2: Correct branch skips checkout
# ---------------------------------------------------------------------------


class TestBranchGuardCorrectBranch:
    """
    When the base clone is already on default_branch, no checkout is performed.
    """

    def test_correct_branch_skips_checkout(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        When git branch --show-current returns default_branch, no git checkout
        must be called — the pull proceeds without any branch switching.
        """
        alias_name = "my-repo-global"
        master_path = str(golden_repos_dir / "my-repo")
        (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
            "default_branch": "main",
        }

        checkout_calls = []

        def fake_subprocess_run(cmd, **kwargs):
            if cmd == ["git", "branch", "--show-current"]:
                # Already on main — no checkout needed
                return _proc(returncode=0, stdout="main")
            if cmd[:2] == ["git", "checkout"]:
                checkout_calls.append(cmd)
                return _proc(returncode=0)
            return _proc(returncode=0)

        mock_updater = Mock()
        mock_updater.has_changes.return_value = True
        mock_updater.get_source_path.return_value = master_path

        with (
            patch.object(
                scheduler.alias_manager,
                "read_alias",
                return_value=str(
                    golden_repos_dir / ".versioned" / "my-repo" / "v_1000000"
                ),
            ),
            patch.object(scheduler.alias_manager, "swap_alias"),
            patch.object(scheduler, "_detect_existing_indexes", return_value={}),
            patch.object(scheduler, "_reconcile_registry_with_filesystem"),
            patch.object(scheduler, "_index_source"),
            patch.object(
                scheduler,
                "_create_snapshot",
                return_value=str(
                    golden_repos_dir / ".versioned" / "my-repo" / "v_2000000"
                ),
            ),
            patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
            patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
                return_value=mock_updater,
            ),
            patch("subprocess.run", side_effect=fake_subprocess_run),
        ):
            scheduler._execute_refresh(alias_name)

        # No checkout calls should have been made
        assert len(checkout_calls) == 0, (
            f"Expected no 'git checkout' calls, got: {checkout_calls}"
        )


# ---------------------------------------------------------------------------
# Test 3: Failed checkout logs error but refresh continues
# ---------------------------------------------------------------------------


class TestBranchGuardCheckoutFailure:
    """
    When git checkout <default_branch> fails, the error is logged but the
    refresh must continue (no exception raised, no crash).
    """

    def test_failed_checkout_logs_error_but_refresh_continues(
        self, scheduler, golden_repos_dir, mock_registry, caplog
    ):
        """
        When git checkout <default_branch> returns non-zero, the error is
        logged (warning/error level) but _execute_refresh does not raise
        and the pull still proceeds.
        """
        import logging

        alias_name = "my-repo-global"
        master_path = str(golden_repos_dir / "my-repo")
        (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
            "default_branch": "main",
        }

        def fake_subprocess_run(cmd, **kwargs):
            if cmd == ["git", "branch", "--show-current"]:
                return _proc(returncode=0, stdout="develop")
            if cmd == ["git", "checkout", "main"]:
                # Simulate checkout failure
                return _proc(returncode=1, stderr="error: checkout failed")
            return _proc(returncode=0)

        mock_updater = Mock()
        mock_updater.has_changes.return_value = True
        mock_updater.get_source_path.return_value = master_path

        # Track whether update() was still called after the failed checkout
        update_called = []

        def track_has_changes():
            update_called.append(True)
            return True

        mock_updater.has_changes.side_effect = track_has_changes

        with (
            patch.object(
                scheduler.alias_manager,
                "read_alias",
                return_value=str(
                    golden_repos_dir / ".versioned" / "my-repo" / "v_1000000"
                ),
            ),
            patch.object(scheduler.alias_manager, "swap_alias"),
            patch.object(scheduler, "_detect_existing_indexes", return_value={}),
            patch.object(scheduler, "_reconcile_registry_with_filesystem"),
            patch.object(scheduler, "_index_source"),
            patch.object(
                scheduler,
                "_create_snapshot",
                return_value=str(
                    golden_repos_dir / ".versioned" / "my-repo" / "v_2000000"
                ),
            ),
            patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
            patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
                return_value=mock_updater,
            ),
            patch("subprocess.run", side_effect=fake_subprocess_run),
            caplog.at_level(
                logging.WARNING, logger="code_indexer.global_repos.refresh_scheduler"
            ),
        ):
            # Must NOT raise even though checkout failed
            scheduler._execute_refresh(alias_name)

        # Refresh must continue despite checkout failure
        assert update_called, (
            "updater.has_changes() must still be called after failed checkout"
        )

        # An error/warning must have been logged about the checkout failure
        error_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "main" in msg or "checkout" in msg or "reset" in msg or "branch" in msg
            for msg in error_messages
        ), f"Expected warning/error about checkout failure, got: {error_messages}"


# ---------------------------------------------------------------------------
# Test 4: Missing default_branch defaults to "main"
# ---------------------------------------------------------------------------


class TestBranchGuardDefaultBranch:
    """
    When repo_info does not contain a 'default_branch' key,
    the guard must fall back to "main".
    """

    def test_missing_default_branch_defaults_to_main(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        When repo_info has no 'default_branch' key, the branch guard must
        compare against 'main' and checkout 'main' if needed.
        """
        alias_name = "my-repo-global"
        master_path = str(golden_repos_dir / "my-repo")
        (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

        # No 'default_branch' key in repo_info
        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
        }

        checkout_calls = []

        def fake_subprocess_run(cmd, **kwargs):
            if cmd == ["git", "branch", "--show-current"]:
                # On the wrong branch
                return _proc(returncode=0, stdout="feature/something")
            if cmd[:2] == ["git", "checkout"]:
                checkout_calls.append(cmd)
                return _proc(returncode=0)
            return _proc(returncode=0)

        mock_updater = Mock()
        mock_updater.has_changes.return_value = True
        mock_updater.get_source_path.return_value = master_path

        with (
            patch.object(
                scheduler.alias_manager,
                "read_alias",
                return_value=str(
                    golden_repos_dir / ".versioned" / "my-repo" / "v_1000000"
                ),
            ),
            patch.object(scheduler.alias_manager, "swap_alias"),
            patch.object(scheduler, "_detect_existing_indexes", return_value={}),
            patch.object(scheduler, "_reconcile_registry_with_filesystem"),
            patch.object(scheduler, "_index_source"),
            patch.object(
                scheduler,
                "_create_snapshot",
                return_value=str(
                    golden_repos_dir / ".versioned" / "my-repo" / "v_2000000"
                ),
            ),
            patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
            patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
                return_value=mock_updater,
            ),
            patch("subprocess.run", side_effect=fake_subprocess_run),
        ):
            scheduler._execute_refresh(alias_name)

        # Must have called git checkout main (the default fallback)
        assert len(checkout_calls) == 1, (
            f"Expected 1 checkout call, got {len(checkout_calls)}: {checkout_calls}"
        )
        assert checkout_calls[0] == [
            "git",
            "checkout",
            "main",
        ], f"Expected 'git checkout main' (default), got: {checkout_calls[0]}"
