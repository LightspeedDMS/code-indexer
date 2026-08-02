"""
Unit tests for Bug #1508: git-pull success masks interrupted indexing.

GitPullUpdater.has_changes() only compares git refs (local HEAD vs
@{upstream}).  It has zero awareness of whether the last indexing pass for
the current local HEAD actually completed.  If a refresh's git-pull step
succeeds but the subsequent indexing step is interrupted (server restart,
crash, OOM) before .code-indexer/metadata.json is updated, every SUBSEQUENT
refresh sees "local HEAD == origin HEAD" and reports "No changes detected"
-- permanently skipping indexing while the on-disk index stays stale
relative to the git tree it is supposedly built from.

Fix: _execute_refresh now cross-checks .code-indexer/metadata.json before
honoring a has_changes()==False short-circuit:
  - metadata status "in_progress"/"failed" (crash mid-index) forces reconcile.
  - metadata current_commit != actual working-tree HEAD (pull advanced past
    the last successfully-started index run, e.g. via an interrupted refresh
    that partially completed the pull but never reached indexing) forces
    reconcile.
A fully consistent metadata.json (status completed, current_commit == HEAD)
must continue to produce the normal "No changes detected" skip -- this is
a regression guard against over-triggering reconcile on every cycle.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_refresh_scheduler_branch_guard.py precedent)
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_repos_dir(tmp_path):
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
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
    )


def _proc(returncode=0, stdout="", stderr=""):
    result = Mock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _write_metadata(source_path: Path, **fields):
    meta_dir = source_path / ".code-indexer"
    meta_dir.mkdir(parents=True, exist_ok=True)
    with open(meta_dir / "metadata.json", "w") as f:
        json.dump(fields, f)


def _run_execute_refresh(scheduler, golden_repos_dir, alias_name, master_path):
    """Run _execute_refresh with has_changes()==False, tracking whether
    _index_source (the real indexing entry point) was invoked."""
    index_source_calls = []

    def fake_subprocess_run(cmd, **kwargs):
        if cmd == ["git", "branch", "--show-current"]:
            return _proc(returncode=0, stdout="main")
        if cmd == ["git", "rev-parse", "HEAD"]:
            return _proc(returncode=0, stdout="cccc111\n")
        return _proc(returncode=0)

    mock_updater = Mock()
    mock_updater.has_changes.return_value = False
    mock_updater.get_source_path.return_value = master_path

    with (
        patch.object(
            scheduler.alias_manager,
            "read_alias",
            return_value=str(golden_repos_dir / ".versioned" / "my-repo" / "v_1000000"),
        ),
        patch.object(scheduler.alias_manager, "swap_alias"),
        patch.object(scheduler, "_detect_existing_indexes", return_value={}),
        patch.object(scheduler, "_reconcile_registry_with_filesystem"),
        patch.object(
            scheduler,
            "_index_source",
            side_effect=lambda *a, **k: index_source_calls.append((a, k)),
        ),
        patch.object(
            scheduler,
            "_create_snapshot",
            return_value=str(golden_repos_dir / ".versioned" / "my-repo" / "v_2000000"),
        ),
        patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
        patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
            return_value=mock_updater,
        ),
        patch(
            "code_indexer.global_repos.refresh_scheduler.subprocess.run",
            side_effect=fake_subprocess_run,
        ),
    ):
        result = scheduler._execute_refresh(alias_name)

    return result, index_source_calls


# ---------------------------------------------------------------------------
# Test 1 (RED): interrupted indexing (status=failed) must NOT be masked by
# has_changes()==False -- indexing must still run.
# ---------------------------------------------------------------------------


class TestStaleIndexMetadataForcesReconcile:
    def test_interrupted_indexing_status_forces_reindex_despite_no_git_changes(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        alias_name = "my-repo-global"
        master_path = golden_repos_dir / "my-repo"
        master_path.mkdir(parents=True, exist_ok=True)

        # metadata.json shows the last indexing run never completed --
        # simulating a server restart mid-refresh (the exact scenario in
        # Bug #1508's evidence: pull succeeded, index status stuck at an
        # older commit with no "completed" marker for the current HEAD).
        _write_metadata(
            master_path,
            status="failed",
            current_commit="aaaa000",
        )

        result, index_source_calls = _run_execute_refresh(
            scheduler, golden_repos_dir, alias_name, str(master_path)
        )

        assert index_source_calls, (
            "Interrupted/failed indexing status must force a reconcile pass "
            "even when has_changes() reports no new git commits -- otherwise "
            "the stale index is never repaired (Bug #1508)."
        )
        assert result["success"] is True

    def test_drifted_current_commit_forces_reindex_despite_no_git_changes(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        alias_name = "my-repo-global"
        master_path = golden_repos_dir / "my-repo"
        master_path.mkdir(parents=True, exist_ok=True)

        # metadata.json claims "completed" but for an OLDER commit than the
        # working tree's actual HEAD (cccc111, per _run_execute_refresh's
        # fake `git rev-parse HEAD`) -- the concrete drift scenario: pull
        # advanced the tree past the last commit that was ever indexed.
        _write_metadata(
            master_path,
            status="completed",
            current_commit="bbbb000",
        )

        result, index_source_calls = _run_execute_refresh(
            scheduler, golden_repos_dir, alias_name, str(master_path)
        )

        assert index_source_calls, (
            "A metadata current_commit that lags the actual git HEAD must "
            "force a reconcile pass even when has_changes() reports no new "
            "commits (Bug #1508 drift-detection gap)."
        )
        assert result["success"] is True


# ---------------------------------------------------------------------------
# Test 2 (regression guard): fully consistent metadata must still short-
# circuit to "No changes detected" -- do not force reconcile on every cycle.
# ---------------------------------------------------------------------------


class TestConsistentMetadataStillSkips:
    def test_consistent_completed_metadata_skips_reindex(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        alias_name = "my-repo-global"
        master_path = golden_repos_dir / "my-repo"
        master_path.mkdir(parents=True, exist_ok=True)

        # metadata matches the actual HEAD ("cccc111") and is marked
        # completed -- genuinely up to date, must NOT force reindex.
        _write_metadata(
            master_path,
            status="completed",
            current_commit="cccc111",
        )

        result, index_source_calls = _run_execute_refresh(
            scheduler, golden_repos_dir, alias_name, str(master_path)
        )

        assert not index_source_calls, (
            "Consistent, completed metadata must not force a reconcile pass "
            "-- this would turn every no-op refresh cycle into a wasted "
            "reindex."
        )
        assert result["message"] == "No changes detected"

    def test_missing_metadata_skips_reindex(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """No metadata.json at all (e.g. brand-new repo dir) must not
        force reconcile -- absence of metadata is not evidence of drift."""
        alias_name = "my-repo-global"
        master_path = golden_repos_dir / "my-repo"
        master_path.mkdir(parents=True, exist_ok=True)

        result, index_source_calls = _run_execute_refresh(
            scheduler, golden_repos_dir, alias_name, str(master_path)
        )

        assert not index_source_calls
        assert result["message"] == "No changes detected"
