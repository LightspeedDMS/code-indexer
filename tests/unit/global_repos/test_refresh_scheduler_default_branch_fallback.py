"""
Unit tests for how the branch guard resolves the expected branch (Bug #469
orphan alias, Bug #2148).

Context
-------
The branch guard in refresh_scheduler._execute_refresh determines the expected
branch before verifying (and resetting) the base clone.

Bug #469 (orphan alias): with no golden_repos_metadata row the value used to
fall back to the hard-coded string "main", so a repo whose remote default is
"master" (e.g. example-master-repo) logged a failing `git checkout main` ERROR
on every refresh cycle. A `git symbolic-ref refs/remotes/origin/HEAD` fallback
was added for that.

Bug #2148: that fallback is itself a guess. On a cluster node the per-node
SQLite read found no row, origin/HEAD said "main", and a repo registered on a
non-default branch was switched to "main". The ONLY authority is now the
registered branch in golden_repos_metadata, read through
``RefreshScheduler.golden_repo_metadata`` (the backend registry: PostgreSQL in
cluster, cidx_server.db in solo). Neither repo_info nor origin/HEAD ever picks
the branch; with no registered branch (Bug #469 orphan aliases have no row)
the refresh switches nothing, logs a WARNING naming the alias and the reason,
and continues on the clone's current branch.

golden_repos_metadata is keyed by the BARE alias (the scheduler looks up
``alias_name.removesuffix("-global")``), so scenarios seed the repo stem.

Design note on internal-method patching
----------------------------------------
This file patches _detect_existing_indexes, _reconcile_registry_with_filesystem,
_index_source, and _create_snapshot on the RefreshScheduler instance.  This is
the same isolation boundary used by test_refresh_scheduler_branch_guard.py
(the established project precedent).  These methods exercise real CoW filesystem
operations and indexing that are not relevant to the branch-guard logic under
test; patching them is the accepted way to isolate _execute_refresh's
branch-guard path. The metadata store is the REAL solo SQLite store, seeded
the way registration seeds it.
"""

import logging
import subprocess
from contextlib import contextmanager
from typing import Any, Dict, List, Optional
from unittest.mock import Mock, patch

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.storage.sqlite_backends import (
    GoldenRepoMetadataSqliteBackend,
)

NO_ROW = "no golden repo metadata row"
NO_BRANCH = "no registered branch recorded"
SCHEDULER_LOGGER = "code_indexer.global_repos.refresh_scheduler"

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> Mock:
    m = Mock(spec=subprocess.CompletedProcess)
    m.returncode, m.stdout, m.stderr = returncode, stdout, stderr
    return m


def _seed_registered_branch(
    golden_repos_dir, repo_stem: str, registered_branch: Optional[str]
) -> None:
    """Seed the real solo store (cidx_server.db next to golden-repos).

    None = no row at all (orphan alias); "" = a row without a branch.
    """
    backend = GoldenRepoMetadataSqliteBackend(
        str(golden_repos_dir.parent / "cidx_server.db")
    )
    backend.ensure_table_exists()
    if registered_branch is None:
        return
    backend.add_repo(
        alias=repo_stem,
        repo_url=f"git@example.com:org/{repo_stem}.git",
        default_branch=registered_branch,
        clone_path=str(golden_repos_dir / repo_stem),
        created_at="2026-01-01T00:00:00+00:00",
    )


def _make_subprocess_handler(
    current_branch: str,
    symbolic_ref_stdout: str,
    checkout_calls: List,
    symbolic_ref_calls: List,
):
    def handler(cmd, **kwargs):
        if cmd == ["git", "branch", "--show-current"]:
            return _proc(stdout=current_branch)
        if cmd == ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]:
            symbolic_ref_calls.append(cmd)
            return _proc(stdout=symbolic_ref_stdout)
        if cmd[:2] == ["git", "checkout"]:
            checkout_calls.append(cmd)
            return _proc()
        return _proc()

    return handler


def _make_updater(master_path: str, has_changes_calls: List) -> Mock:
    """Build a mock updater that records has_changes calls."""
    updater = Mock()
    updater.get_source_path.return_value = master_path

    def _side():
        has_changes_calls.append(True)
        return True

    updater.has_changes.side_effect = _side
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
        # Story #2118: the mocked snapshot path above holds no index to count.
        patch("code_indexer.server.services.index_spend_alarm.record_published_points"),
        patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
            return_value=updater,
        ),
    ):
        yield


def _run_scenario(
    scheduler,
    golden_repos_dir,
    mock_registry,
    *,
    alias_name: str,
    repo_info_extra: Dict[str, Any],
    current_branch: str,
    symbolic_ref_stdout: str,
    registered_branch: Optional[str],
    expected_warning: Optional[str],
    caplog: pytest.LogCaptureFixture,
) -> tuple:
    """Wire and run one scenario; returns (checkout_calls, symbolic_ref_calls,
    has_changes_calls). The refresh never raises. With *expected_warning*,
    exactly one registered-branch WARNING names the alias and that reason;
    without it, none is logged."""
    repo_stem = alias_name.removesuffix("-global")
    (golden_repos_dir / repo_stem).mkdir(parents=True, exist_ok=True)
    _seed_registered_branch(golden_repos_dir, repo_stem, registered_branch)
    mock_registry.get_global_repo.return_value = {
        "alias_name": alias_name,
        "repo_url": f"git@example.com:org/{repo_stem}.git",
        **repo_info_extra,
    }
    checkout_calls: List = []
    symbolic_ref_calls: List = []
    has_changes_calls: List = []
    updater = _make_updater(str(golden_repos_dir / repo_stem), has_changes_calls)
    handler = _make_subprocess_handler(
        current_branch, symbolic_ref_stdout, checkout_calls, symbolic_ref_calls
    )
    with (
        _refresh_context(scheduler, golden_repos_dir, alias_name, updater),
        patch("subprocess.run", side_effect=handler),
        caplog.at_level(logging.WARNING, logger=SCHEDULER_LOGGER),
    ):
        scheduler._execute_refresh(alias_name)
    branch_warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == SCHEDULER_LOGGER
        and "Registered branch of" in r.getMessage()
    ]
    if expected_warning is None:
        assert branch_warnings == [], branch_warnings
    else:
        assert len(branch_warnings) == 1, branch_warnings
        assert alias_name in branch_warnings[0], branch_warnings
        assert expected_warning in branch_warnings[0], branch_warnings
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
# Parametrized scenarios 1-6
# Each row: repo_info_extra, registered_branch, symbolic_ref_stdout,
#   expected_checkout_calls, expected_warning (None = the registered branch
#   is known, so no unknown-branch WARNING). The refresh always proceeds.
# The clone is always on "feature/x".
# ---------------------------------------------------------------------------

_SCENARIOS = [
    pytest.param(
        {"default_branch": "develop"},
        None,
        "origin/main",
        [],
        NO_ROW,
        id="1_repo_info_is_not_an_authority",
    ),
    pytest.param(
        {},
        "trunk",
        "origin/main",
        [["git", "checkout", "trunk"]],
        None,
        id="2_registered_branch_wins",
    ),
    pytest.param(
        {},
        None,
        "origin/master\n",
        [],
        NO_ROW,
        id="3_no_row_never_guesses_origin_head",
    ),
    pytest.param(
        {},
        "",
        "origin/master\n",
        [],
        NO_BRANCH,
        id="4_row_without_branch_never_guesses_origin_head",
    ),
    pytest.param(
        {},
        "feature/x",
        "origin/main",
        [],
        None,
        id="5_registered_non_default_branch_is_kept",
    ),
    pytest.param(
        {"default_branch": "main"},
        "develop",
        "origin/main",
        [["git", "checkout", "develop"]],
        None,
        id="6_registered_branch_beats_repo_info_and_origin_head",
    ),
]


@pytest.mark.parametrize(
    "repo_info_extra,registered_branch,symbolic_ref_stdout,"
    "expected_checkout_calls,expected_warning",
    _SCENARIOS,
)
def test_default_branch_fallback_scenarios(
    scheduler,
    golden_repos_dir,
    mock_registry,
    caplog,
    repo_info_extra,
    registered_branch,
    symbolic_ref_stdout,
    expected_checkout_calls,
    expected_warning,
):
    """Parametrized driver for scenarios 1-6."""
    checkout_calls, symbolic_ref_calls, has_changes_calls = _run_scenario(
        scheduler,
        golden_repos_dir,
        mock_registry,
        alias_name="my-repo-global",
        repo_info_extra=repo_info_extra,
        current_branch="feature/x",
        symbolic_ref_stdout=symbolic_ref_stdout,
        registered_branch=registered_branch,
        expected_warning=expected_warning,
        caplog=caplog,
    )

    assert checkout_calls == expected_checkout_calls, (
        f"Expected {expected_checkout_calls}, got {checkout_calls}"
    )
    assert symbolic_ref_calls == [], (
        f"origin/HEAD must never be used to guess the branch: {symbolic_ref_calls}"
    )
    # Known or unknown registered branch, the refresh continues past the
    # branch guard to the pull.
    assert has_changes_calls == [True], has_changes_calls


# ---------------------------------------------------------------------------
# Test 7: orphan alias whose remote default is 'master' (Bug #469)
# ---------------------------------------------------------------------------


def test_previous_bug_scenario_master_default(
    scheduler, golden_repos_dir, mock_registry, caplog
):
    """
    example-master-repo-global is an orphan alias (global_repos row, no
    golden_repos_metadata partner). Remote default is 'master'.  Base clone
    already on 'master'.

    - A WARNING names the alias and the missing row, and the refresh
      continues on the clone's branch (Bug #2148: nothing is guessed)
    - NO git checkout (pre-#469 checked out a hard-coded 'main' every cycle)
    - git symbolic-ref NOT invoked (origin/HEAD is not an authority)
    """
    checkout_calls, symbolic_ref_calls, has_changes_calls = _run_scenario(
        scheduler,
        golden_repos_dir,
        mock_registry,
        alias_name="example-master-repo-global",
        repo_info_extra={},
        current_branch="master",
        symbolic_ref_stdout="origin/master",
        registered_branch=None,
        expected_warning=NO_ROW,
        caplog=caplog,
    )

    assert checkout_calls == [], f"Expected no checkout, got {checkout_calls}"
    assert symbolic_ref_calls == [], (
        f"Expected git symbolic-ref never called, got {symbolic_ref_calls}"
    )
    assert has_changes_calls == [True], "the refresh must continue to the pull"
