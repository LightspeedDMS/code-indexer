"""
Unit tests for RefreshScheduler branch guard fix (Bug #469 Fix 1).

Tests the branch guard that ensures the base clone is on the expected
default_branch before git pull, preventing wrong-branch contamination.

Tests:
1. Wrong branch triggers checkout to default_branch before pulling
2. Correct branch skips checkout (no unnecessary git ops)
3. Failed checkout logs an error and FAILS the refresh before pulling
   (Bug #2148: never continue on an unverified branch)
4. No registered branch in golden_repos_metadata (Bug #469 orphan alias)
   logs a WARNING and the refresh continues with no checkout (Bug #2148:
   never guessed from origin/HEAD)
"""

import subprocess
from unittest.mock import Mock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.server.storage.sqlite_backends import (
    GoldenRepoMetadataSqliteBackend,
)


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
    """Create RefreshScheduler with a mock registry.

    Bug #2148: the registered branch is read only from golden_repos_metadata
    (the solo store here: cidx_server.db next to golden-repos), never from
    the registry row, so the fixture registers my-repo on 'main' there.
    """
    metadata = GoldenRepoMetadataSqliteBackend(
        str(golden_repos_dir.parent / "cidx_server.db")
    )
    metadata.ensure_table_exists()
    metadata.add_repo(
        alias="my-repo",
        repo_url="git@github.com:org/my-repo.git",
        default_branch="main",
        clone_path=str(golden_repos_dir / "my-repo"),
        created_at="2026-01-01T00:00:00+00:00",
    )
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
            if cmd == ["git", "symbolic-ref", "--short", "HEAD"]:
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
            if cmd == ["git", "symbolic-ref", "--short", "HEAD"]:
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
# Test 3: Failed checkout logs an error and fails the refresh
# ---------------------------------------------------------------------------


class TestBranchGuardCheckoutFailure:
    """
    When git checkout <default_branch> fails, the error is logged and the
    refresh FAILS before pulling: the clone is on an unverified branch, and
    indexing it could publish the wrong branch (Bug #2148).
    """

    def test_failed_checkout_logs_error_and_fails_refresh(
        self, scheduler, golden_repos_dir, mock_registry, caplog
    ):
        """
        When git checkout <default_branch> returns non-zero, the error is
        logged and _execute_refresh raises before the pull (has_changes is
        never reached).
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
            if cmd == ["git", "symbolic-ref", "--short", "HEAD"]:
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
            with pytest.raises(RuntimeError, match="git checkout failed"):
                scheduler._execute_refresh(alias_name)

        # The refresh stops before pulling
        assert update_called == [], (
            "updater.has_changes() must not be called after a failed checkout"
        )

        # An error/warning must have been logged about the checkout failure
        error_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "Failed to reset my-repo-global to main" in msg for msg in error_messages
        ), f"Expected warning/error about checkout failure, got: {error_messages}"


# ---------------------------------------------------------------------------
# Test 4: Missing registered branch warns and continues without a checkout
# ---------------------------------------------------------------------------


class TestBranchGuardDefaultBranch:
    """
    When golden_repos_metadata holds no registered branch for the repo (Bug
    #469 orphan aliases), the refresh switches nothing -- no checkout, and no
    guess from origin/HEAD (Bug #2148) -- logs a WARNING naming the alias,
    and continues to the pull on the clone's current branch.
    """

    def test_missing_registered_branch_warns_and_continues_without_checkout(
        self, scheduler, golden_repos_dir, mock_registry, caplog
    ):
        """
        With no registered row, the refresh must not check out a guessed
        branch (even though origin/HEAD names one); it warns and proceeds.
        """
        import logging

        alias_name = "my-repo-global"
        master_path = str(golden_repos_dir / "my-repo")
        (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)
        GoldenRepoMetadataSqliteBackend(
            str(golden_repos_dir.parent / "cidx_server.db")
        ).remove_repo("my-repo")

        # No 'default_branch' key in repo_info
        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
        }

        checkout_calls = []
        symbolic_ref_calls = []

        def fake_subprocess_run(cmd, **kwargs):
            if cmd == ["git", "symbolic-ref", "--short", "HEAD"]:
                return _proc(returncode=0, stdout="feature/something")
<<<<<<< HEAD
            if cmd == ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"]:
                # All fallbacks exhausted — symbolic-ref returns empty
                return _proc(returncode=0, stdout="")
=======
            if cmd == ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]:
                symbolic_ref_calls.append(cmd)
                return _proc(returncode=0, stdout="origin/main")
>>>>>>> upstream/master
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
            caplog.at_level(
                logging.WARNING, logger="code_indexer.global_repos.refresh_scheduler"
            ),
        ):
            scheduler._execute_refresh(alias_name)

        # No registered branch: no checkout, origin/HEAD never consulted,
        # a WARNING names the alias, and the refresh continues to the pull
        assert checkout_calls == [], (
            f"Expected no checkout without a registered branch, got: {checkout_calls}"
        )
        assert symbolic_ref_calls == [], (
            f"origin/HEAD must never be used to guess the branch: {symbolic_ref_calls}"
        )
        mock_updater.has_changes.assert_called_once()
        branch_warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "Registered branch of" in r.getMessage()
        ]
        assert len(branch_warnings) == 1, branch_warnings
        assert alias_name in branch_warnings[0], branch_warnings
        assert "no golden repo metadata row" in branch_warnings[0], branch_warnings
