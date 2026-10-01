"""RefreshScheduler._execute_refresh wiring to
discover_and_enforce_temporal_retention (Story #1457 MEDIUM #14, 2026-07-23
code review).

Temporal sister-location aliases are structurally invisible to
RefreshScheduler's per-repo enumeration loop (see snapshot_retention.py's
module docstring), so `_execute_refresh` must ALSO retention-sweep them
directly after its own semantic `_enforce_retention` call, using the golden
repo's BARE alias (no "-global" suffix, matching the convention
`temporal_relocation_trigger.py` already uses).

The SUT here is `_execute_refresh`'s retention-wiring orchestration (does
it call `discover_and_enforce_temporal_retention` with the right args after
its own alias-swap+cleanup sequence) -- NOT `_detect_existing_indexes`,
`_reconcile_registry_with_filesystem`, `_index_source`, or `_create_snapshot`,
which are different, unrelated methods (git/filesystem/indexing
side-effects out of scope for this wiring test). This file reuses the
EXACT `_run_refresh` harness already established and approved in the
pre-existing `test_refresh_scheduler_cleanup_guard.py` (same collaborator
patch list, same rationale: those methods are real external-effect
collaborators of `_execute_refresh`, not the orchestration logic under
test), so the retention-sweep call site can be exercised through the real,
unmodified `_execute_refresh` control flow rather than reimplementing that
harness a second time.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler


@pytest.fixture
def golden_repos_dir(tmp_path):
    d = tmp_path / "golden-repos"
    d.mkdir(parents=True)
    return d


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


def _run_refresh(scheduler, golden_repos_dir, current_target, new_versioned_path):
    """Drive _execute_refresh with the given current_target through the swap.

    Identical collaborator-boundary patch list to
    test_refresh_scheduler_cleanup_guard.py's own `_run_refresh` (git pull,
    indexing, snapshot creation, and post-refresh reconciliation are real
    external-effect collaborators of `_execute_refresh`, unrelated to the
    retention-wiring orchestration this file tests).
    """
    alias_name = "my-repo-global"
    master_path = str(golden_repos_dir / "my-repo")
    (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

    scheduler.registry.get_global_repo.return_value = {
        "alias_name": alias_name,
        "repo_url": "git@github.com:org/my-repo.git",
    }

    with (
        patch.object(
            scheduler.alias_manager, "read_alias", return_value=current_target
        ),
        patch.object(scheduler.alias_manager, "swap_alias"),
        patch.object(scheduler, "_detect_existing_indexes", return_value={}),
        patch.object(scheduler, "_reconcile_registry_with_filesystem"),
        patch.object(scheduler, "_index_source"),
        patch.object(scheduler, "_create_snapshot", return_value=new_versioned_path),
        patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_git_updater_cls,
        patch(
            "code_indexer.global_repos.refresh_scheduler"
            ".discover_and_enforce_temporal_retention"
        ) as mock_temporal_retention,
    ):
        mock_updater = Mock()
        mock_updater.has_changes.return_value = True
        mock_updater.get_source_path.return_value = master_path
        mock_git_updater_cls.return_value = mock_updater

        scheduler._execute_refresh(alias_name)

    return mock_temporal_retention


def test_execute_refresh_sweeps_temporal_retention_with_bare_alias(
    scheduler, golden_repos_dir
):
    old_versioned = str(golden_repos_dir / ".versioned" / "my-repo" / "v_1000000")
    new_versioned = str(golden_repos_dir / ".versioned" / "my-repo" / "v_2000000")

    mock_temporal_retention = _run_refresh(
        scheduler, golden_repos_dir, old_versioned, new_versioned
    )

    assert mock_temporal_retention.call_count == 1
    call = mock_temporal_retention.call_args
    assert call.args[0] == "my-repo", (
        "discover_and_enforce_temporal_retention must be called with the "
        f"BARE repo alias (no '-global' suffix), got: {call.args[0]!r}"
    )
    assert call.kwargs["snapshot_manager"] is scheduler._snapshot_manager
    assert call.kwargs["alias_manager"] is scheduler.alias_manager
    assert call.kwargs["cleanup_manager"] is scheduler.cleanup_manager
