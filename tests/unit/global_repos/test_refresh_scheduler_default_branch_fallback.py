"""
Unit tests for the three-layer default_branch fallback fix (Bug #469 orphan alias).

Context
-------
The branch guard in refresh_scheduler._execute_refresh determines the expected
default branch before verifying (and resetting) the base clone.  Prior to this
fix, if repo_info had no 'default_branch' key AND golden_repos_metadata had no
row for the alias (orphan global_repos row), the value silently fell back to the
hard-coded string "main".

For repos whose actual remote default is "master" (e.g. dotnet-playground) that
caused `git checkout main` to fail with:
    "pathspec 'main' did not match any file(s) known to git"
and an ERROR log entry on every refresh cycle (~90-min interval).

Fix: add a third authoritative fallback that reads
    git symbolic-ref --short refs/remotes/origin/HEAD
which returns "origin/master" or "origin/main" etc.  Strip the "origin/" prefix
to get the authoritative default_branch.  If that also fails, skip the
branch-reset step entirely (no-op rather than crashing with a misleading error).

Design note on internal-method patching
----------------------------------------
This file patches _detect_existing_indexes, _reconcile_registry_with_filesystem,
_index_source, and _create_snapshot on the RefreshScheduler instance.  This is
the same isolation boundary used by test_refresh_scheduler_branch_guard.py
(the established project precedent).  These methods exercise real CoW filesystem
operations and indexing that are not relevant to the branch-guard logic under
test; patching them is the accepted way to isolate _execute_refresh's
branch-guard path.

Tests
-----
Parametrized scenarios 1–6: 2 unconditional asserts each (checkout, has_changes).
Test 7 (standalone, uses caplog): 3 unconditional asserts (checkout, symbolic-ref
invoked, no ERROR logged) — caplog is not available in parametrized fixtures.
"""

import logging
import subprocess
from contextlib import contextmanager
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, Mock, patch

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

_DB_PATCH = (
    "code_indexer.global_repos.refresh_scheduler.GoldenRepoMetadataSqliteBackend"
)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> Mock:
    m = Mock(spec=subprocess.CompletedProcess)
    m.returncode, m.stdout, m.stderr = returncode, stdout, stderr
    return m


def _build_db_backend(db_get_repo_return: Optional[Dict]) -> MagicMock:
    backend = MagicMock()
    backend.get_repo.return_value = db_get_repo_return
    return backend


def _make_subprocess_handler(
    current_branch: str,
    symbolic_ref_stdout: str,
    symbolic_ref_returncode: int,
    checkout_calls: List,
    symbolic_ref_calls: List,
):
    def handler(cmd, **kwargs):
        if cmd == ["git", "branch", "--show-current"]:
            return _proc(stdout=current_branch)
        if cmd == ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]:
            symbolic_ref_calls.append(cmd)
            return _proc(returncode=symbolic_ref_returncode, stdout=symbolic_ref_stdout)
        if cmd[:2] == ["git", "checkout"]:
            checkout_calls.append(cmd)
            return _proc()
        return _proc()

    return handler


def _make_updater(
    master_path: str,
    has_changes_calls: List,
    track_has_changes: bool,
) -> Mock:
    """Build a mock updater; record has_changes calls only when requested."""
    updater = Mock()
    updater.get_source_path.return_value = master_path
    if track_has_changes:

        def _side():
            has_changes_calls.append(True)
            return True

        updater.has_changes.side_effect = _side
    else:
        updater.has_changes.return_value = True
    return updater


@contextmanager
def _refresh_context(scheduler, golden_repos_dir, alias_name: str, updater):
    """
    Patch boilerplate _execute_refresh dependencies — same isolation pattern
    as test_refresh_scheduler_branch_guard.py (established project precedent).
    """
    repo_stem = alias_name.removesuffix("-global")
    with (
        patch.object(
            scheduler.alias_manager,
            "read_alias",
            return_value=str(golden_repos_dir / ".versioned" / repo_stem / "v_1"),
        ),
        patch.object(scheduler.alias_manager, "swap_alias"),
        patch.object(scheduler, "_detect_existing_indexes", return_value={}),
        patch.object(scheduler, "_reconcile_registry_with_filesystem"),
        patch.object(scheduler, "_index_source"),
        patch.object(
            scheduler,
            "_create_snapshot",
            return_value=str(golden_repos_dir / ".versioned" / repo_stem / "v_2"),
        ),
        patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
        patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
            return_value=updater,
        ),
    ):
        yield


def _execute_with_patches(
    scheduler,
    golden_repos_dir,
    alias_name,
    updater,
    db_get_repo_return,
    handler,
):
    with (
        _refresh_context(scheduler, golden_repos_dir, alias_name, updater),
        patch(_DB_PATCH, return_value=_build_db_backend(db_get_repo_return)),
        patch("subprocess.run", side_effect=handler),
    ):
        scheduler._execute_refresh(alias_name)


def _run_scenario(
    scheduler,
    golden_repos_dir,
    mock_registry,
    *,
    alias_name: str,
    repo_info_extra: Dict[str, Any],
    current_branch: str,
    symbolic_ref_stdout: str,
    symbolic_ref_returncode: int,
    db_get_repo_return: Optional[Dict],
    track_has_changes: bool,
) -> tuple:
    """Wire and run one scenario; returns (checkout_calls, symbolic_ref_calls, has_changes_calls)."""
    repo_stem = alias_name.removesuffix("-global")
    (golden_repos_dir / repo_stem).mkdir(parents=True, exist_ok=True)
    mock_registry.get_global_repo.return_value = {
        "alias_name": alias_name,
        "repo_url": f"git@github.com:org/{repo_stem}.git",
        **repo_info_extra,
    }
    checkout_calls: List = []
    symbolic_ref_calls: List = []
    has_changes_calls: List = []
    updater = _make_updater(
        str(golden_repos_dir / repo_stem), has_changes_calls, track_has_changes
    )
    handler = _make_subprocess_handler(
        current_branch,
        symbolic_ref_stdout,
        symbolic_ref_returncode,
        checkout_calls,
        symbolic_ref_calls,
    )
    _execute_with_patches(
        scheduler,
        golden_repos_dir,
        alias_name,
        updater,
        db_get_repo_return,
        handler,
    )
    return checkout_calls, symbolic_ref_calls, has_changes_calls


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_repos_dir(tmp_path):
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir(parents=True)
    return golden_dir


@pytest.fixture
def mock_registry():
    registry = Mock()
    registry.list_global_repos.return_value = []
    registry.update_refresh_timestamp.return_value = None
    return registry


@pytest.fixture
def scheduler(golden_repos_dir, mock_registry):
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config,
        query_tracker=Mock(spec=QueryTracker),
        cleanup_manager=Mock(spec=CleanupManager),
        registry=mock_registry,
    )


# ---------------------------------------------------------------------------
# Parametrized scenarios 1–6
# Each row: repo_info_extra, db_get_repo_return, symbolic_ref_stdout,
#   symbolic_ref_returncode, expected_checkout_calls,
#   track_has_changes, expected_has_changes_called
# ---------------------------------------------------------------------------

_SCENARIOS = [
    pytest.param(
        {"default_branch": "develop"},
        None,
        "origin/main",
        0,
        [["git", "checkout", "develop"]],
        False,
        False,
        id="1_repo_info_wins",
    ),
    pytest.param(
        {},
        {"default_branch": "trunk"},
        "origin/main",
        0,
        [["git", "checkout", "trunk"]],
        False,
        False,
        id="2_db_wins",
    ),
    pytest.param(
        {},
        None,
        "origin/master\n",
        0,
        [["git", "checkout", "master"]],
        False,
        False,
        id="3_symbolic_ref_db_none",
    ),
    pytest.param(
        {},
        {"alias": "my-repo"},
        "origin/master\n",
        0,
        [["git", "checkout", "master"]],
        False,
        False,
        id="4_symbolic_ref_db_no_field",
    ),
    pytest.param(
        {},
        None,
        "",
        128,
        [],
        True,
        True,  # no checkout; refresh must continue (has_changes called)
        id="5_all_fallbacks_fail",
    ),
    pytest.param(
        {},
        None,
        "origin/develop\n",
        0,
        [["git", "checkout", "develop"]],
        False,
        False,  # NOT "origin/develop"
        id="6_strips_origin_prefix",
    ),
]


@pytest.mark.parametrize(
    "repo_info_extra,db_get_repo_return,symbolic_ref_stdout,"
    "symbolic_ref_returncode,expected_checkout_calls,"
    "track_has_changes,expected_has_changes_called",
    _SCENARIOS,
)
def test_default_branch_fallback_scenarios(
    scheduler,
    golden_repos_dir,
    mock_registry,
    repo_info_extra,
    db_get_repo_return,
    symbolic_ref_stdout,
    symbolic_ref_returncode,
    expected_checkout_calls,
    track_has_changes,
    expected_has_changes_called,
):
    """Parametrized driver for scenarios 1–6.  Exactly 2 unconditional asserts."""
    checkout_calls, _, has_changes_calls = _run_scenario(
        scheduler,
        golden_repos_dir,
        mock_registry,
        alias_name="my-repo-global",
        repo_info_extra=repo_info_extra,
        current_branch="feature/x",
        symbolic_ref_stdout=symbolic_ref_stdout,
        symbolic_ref_returncode=symbolic_ref_returncode,
        db_get_repo_return=db_get_repo_return,
        track_has_changes=track_has_changes,
    )

    assert checkout_calls == expected_checkout_calls, (
        f"Expected {expected_checkout_calls}, got {checkout_calls}"
    )
    assert bool(has_changes_calls) == expected_has_changes_called, (
        f"Expected has_changes_called={expected_has_changes_called}, "
        f"got {bool(has_changes_calls)}"
    )


# ---------------------------------------------------------------------------
# Test 7: dotnet-playground production bug
# 3 asserts: no-checkout + symbolic-ref-invoked + no-ERROR-logged.
# The third assert requires caplog which is not available in parametrized fixtures.
# ---------------------------------------------------------------------------


def test_previous_bug_scenario_master_default(
    scheduler, golden_repos_dir, mock_registry, caplog
):
    """
    Reproduces the exact production failure: dotnet-playground-global is an
    orphan alias (global_repos row, no golden_repos_metadata partner).
    Remote default is 'master'.  Base clone already on 'master'.

    After the fix:
    - NO git checkout (current branch already matches resolved default)
    - git symbolic-ref WAS invoked (third-fallback layer discovers 'master')
    - NO ERROR logged (pre-fix produced ERROR every ~90-min refresh cycle)
    """
    with caplog.at_level(
        logging.ERROR,
        logger="code_indexer.global_repos.refresh_scheduler",
    ):
        checkout_calls, symbolic_ref_calls, _ = _run_scenario(
            scheduler,
            golden_repos_dir,
            mock_registry,
            alias_name="dotnet-playground-global",
            repo_info_extra={},
            current_branch="master",
            symbolic_ref_stdout="origin/master",
            symbolic_ref_returncode=0,
            db_get_repo_return=None,
            track_has_changes=False,
        )

    assert checkout_calls == [], (
        f"Expected no checkout (current already matches default), got {checkout_calls}"
    )
    assert len(symbolic_ref_calls) == 1, (
        f"Expected git symbolic-ref called once, got {symbolic_ref_calls}"
    )
    assert [r.message for r in caplog.records if r.levelno >= logging.ERROR] == [], (
        "Expected no ERROR log entries"
    )
