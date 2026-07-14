"""
Unit tests for Bug #1336: RefreshScheduler skips orphaned golden repos during
global_repo_refresh instead of raising ValueError.

An orphaned golden alias is a registry row (golden_repos_metadata / registry)
that is present, but whose on-disk clone directory at
{golden_repos_dir}/{repo_name} is absent (e.g. after a partial provisioning
failure, or after the #1317 reconciler removed the clone but a stale refresh
job was already queued for it).

Before this fix: _execute_refresh() computes master_path = golden_repos_dir /
repo_name for the git-repo branch, then constructs GitPullUpdater(master_path),
whose __init__ raises:
    ValueError: Repository path does not exist: <master_path>
This exception propagates out of _execute_refresh() as a RuntimeError (Bug #84
re-raise), which BackgroundJobManager/JobTracker records as a FAILED
"global_repo_refresh" job -- exactly the disease observed on staging for the
flask/uvicorn/starlette/httpx aliases (Bug #1336, related to #1317).

Fix: _execute_refresh() wraps the real GitPullUpdater(master_path) construction
in a try/except ValueError. When the clone directory is missing, the ValueError
is caught, a WARNING is logged, and a graceful success=True skip result is
returned -- mirroring the pre-existing Bug #268 "uninitialized local repo"
skip pattern in the same function. This is deliberately narrower than a
pre-emptive path-existence check: it only fires for the real, unmocked
GitPullUpdater constructor, so it does not disturb the many pre-existing unit
tests that patch GitPullUpdater out entirely (and therefore never touch the
real filesystem for their fictitious repo aliases).

Orphan CLEANUP (removing the stale registry row) is explicitly out of scope
here -- it is delegated to the #1317 reconciler (golden_repo_reconciler.py).
This fix only makes the refresh path tolerant of orphans; it must never
delete anything itself.
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_refresh_scheduler_uninitialized_local.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_repos_dir(tmp_path):
    """Golden repos directory (real filesystem root for existence checks)."""
    d = tmp_path / "golden-repos"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def mock_registry():
    """Registry mock with sensible defaults."""
    registry = MagicMock()
    registry.list_global_repos.return_value = []
    registry.get_global_repo.return_value = None
    registry.update_refresh_timestamp = MagicMock()
    registry.update_enable_temporal = MagicMock()
    registry.update_enable_scip = MagicMock()
    return registry


@pytest.fixture
def mock_config_source():
    """Config source mock."""
    cs = MagicMock()
    cs.get_global_refresh_interval.return_value = 3600
    return cs


@pytest.fixture
def scheduler(golden_repos_dir, mock_registry, mock_config_source):
    """RefreshScheduler with injected mock registry."""
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=mock_registry,
    )


def _make_git_repo_info(alias_name: str) -> dict:
    """Build a minimal repo_info dict for a REMOTE git repo (registry row present)."""
    return {
        "alias_name": alias_name,
        "repo_url": "https://github.com/example-org/example-repo.git",
        "default_branch": "main",
        "enable_temporal": False,
        "enable_scip": False,
    }


# ---------------------------------------------------------------------------
# Orphaned git repo: registry row present, on-disk clone absent
# ---------------------------------------------------------------------------


class TestOrphanedGitRepoSkippedGracefully:
    """
    An orphaned golden alias (registry row present, clone dir absent) must be
    skipped with a WARNING and success=True -- never raise ValueError from
    GitPullUpdater's constructor.

    GitPullUpdater is deliberately NOT mocked in these tests: its real
    __init__ is exactly what raises ValueError for a missing master_path,
    which is the condition under test (Bug #1336's catch-and-skip guard).
    """

    def test_orphaned_git_repo_returns_success_not_raises(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        alias_name = "flask-global"
        repo_name = "flask"
        # Deliberately do NOT create golden_repos_dir / repo_name -- this is
        # the orphan: registry row present, clone missing on disk.
        master_path = golden_repos_dir / repo_name
        assert not master_path.exists()

        repo_info = _make_git_repo_info(alias_name)
        mock_registry.get_global_repo.return_value = repo_info
        # Alias pointer resolves to SOME truthy target (registry row/alias
        # both present -- only the clone directory itself is missing).
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(master_path))

        with patch.object(scheduler, "_detect_existing_indexes", return_value={}):
            with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                result = scheduler._execute_refresh(alias_name)

        assert result["success"] is True, (
            "Bug #1336: orphaned golden repo must return success=True, not raise."
        )
        assert not master_path.exists(), "No clone should be created as a side effect"

    def test_orphaned_git_repo_message_indicates_orphan_or_skipped(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        alias_name = "uvicorn-global"
        repo_name = "uvicorn"
        master_path = golden_repos_dir / repo_name

        repo_info = _make_git_repo_info(alias_name)
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(master_path))

        with patch.object(scheduler, "_detect_existing_indexes", return_value={}):
            with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                result = scheduler._execute_refresh(alias_name)

        message = result.get("message", "").lower()
        skip_keywords = ["orphan", "clone missing", "skipped"]
        assert any(kw in message for kw in skip_keywords), (
            f"Bug #1336: result message '{result.get('message')}' does not "
            f"indicate the golden repo was skipped due to a missing clone. "
            f"Expected one of: {skip_keywords}"
        )

    def test_job_tracker_completes_not_fails_for_orphaned_repo(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        alias_name = "starlette-global"
        repo_name = "starlette"
        master_path = golden_repos_dir / repo_name

        repo_info = _make_git_repo_info(alias_name)
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(master_path))

        mock_job_tracker = MagicMock()
        scheduler._job_tracker = mock_job_tracker

        with patch.object(scheduler, "_detect_existing_indexes", return_value={}):
            with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                scheduler._execute_refresh(alias_name)

        mock_job_tracker.complete_job.assert_called_once()
        mock_job_tracker.fail_job.assert_not_called()

    def test_no_orphan_cleanup_performed(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """Bug #1336 explicitly delegates orphan CLEANUP to the #1317 reconciler.
        This refresh-path fix must only SKIP -- never delete/remove anything."""
        alias_name = "httpx-global"
        repo_name = "httpx"
        master_path = golden_repos_dir / repo_name

        repo_info = _make_git_repo_info(alias_name)
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(master_path))

        with patch.object(scheduler, "_detect_existing_indexes", return_value={}):
            with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                scheduler._execute_refresh(alias_name)

        # No registry-row removal, no filesystem cleanup attempted for the
        # skipped orphan -- the clone directory remains absent, untouched.
        assert not master_path.exists()
        deletion_like_calls = [
            call
            for call in mock_registry.mock_calls
            if "remove" in call[0] or "delete" in call[0]
        ]
        assert deletion_like_calls == [], (
            f"Bug #1336: orphan skip must never delete/remove registry state "
            f"(delegated to #1317 reconciler). Got: {deletion_like_calls}"
        )


# ---------------------------------------------------------------------------
# Non-orphaned git repo: must still process normally (no regression)
# ---------------------------------------------------------------------------


class TestValidGitRepoUnaffectedByOrphanSkip:
    """A fully-valid (non-orphaned) golden repo must be completely unaffected
    by the Bug #1336 orphan-skip guard."""

    def test_valid_git_repo_still_calls_git_pull_updater(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        alias_name = "some-repo-global"
        repo_name = "some-repo"
        source_dir = golden_repos_dir / repo_name
        source_dir.mkdir(parents=True)  # Clone DOES exist -- not orphaned.

        repo_info = _make_git_repo_info(alias_name)
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(source_dir))

        mock_updater = MagicMock()
        mock_updater.has_changes.return_value = False

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
            return_value=mock_updater,
        ) as mock_git_cls:
            with patch.object(scheduler, "_detect_existing_indexes", return_value={}):
                with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                    result = scheduler._execute_refresh(alias_name)

        mock_git_cls.assert_called_once()
        assert result["success"] is True
