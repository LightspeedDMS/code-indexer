"""
Unit tests for RefreshScheduler externally_managed gating (EVO-64493).

When golden repos are externally managed, the scheduler must:
- NOT launch its periodic refresh thread (start() is a no-op), and
- NOT run startup restore reconciliation, and specifically NOT write the
  completion marker (so turning the mode back off later still reconciles once).

The reader is only active in server mode (GlobalRepoOperations config_source);
the CLI ConfigManager path is always self-managed.
"""

import pytest
from unittest.mock import Mock

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.shared_operations import GlobalRepoOperations
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


@pytest.fixture
def golden_repos_dir(tmp_path):
    d = tmp_path / "golden-repos"
    d.mkdir(parents=True)
    return d


def _make_scheduler(golden_repos_dir, externally_managed, registry=None):
    # Mock(spec=GlobalRepoOperations) passes isinstance() in _is_externally_managed.
    config_source = Mock(spec=GlobalRepoOperations)
    config_source.get_config.return_value = {
        "refresh_interval": 3600,
        "externally_managed": externally_managed,
    }
    if registry is None:
        registry = Mock()
        registry.list_global_repos.return_value = []
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_source,
        query_tracker=Mock(spec=QueryTracker),
        cleanup_manager=Mock(spec=CleanupManager),
        registry=registry,
    )


def test_is_externally_managed_reflects_config(golden_repos_dir):
    assert _make_scheduler(golden_repos_dir, True)._is_externally_managed() is True
    assert _make_scheduler(golden_repos_dir, False)._is_externally_managed() is False


def test_start_skips_thread_when_externally_managed(golden_repos_dir):
    sched = _make_scheduler(golden_repos_dir, True)
    sched.start()
    assert sched.is_running() is False


def test_reconcile_skipped_and_no_marker_when_externally_managed(golden_repos_dir):
    registry = Mock()
    registry.list_global_repos.return_value = []
    sched = _make_scheduler(golden_repos_dir, True, registry=registry)

    sched.reconcile_golden_repos()

    # Skipped before touching the registry, and the marker was NOT written.
    registry.list_global_repos.assert_not_called()
    assert not (golden_repos_dir / ".reconciliation_complete_v1").exists()


def test_reconcile_runs_when_not_externally_managed(golden_repos_dir):
    registry = Mock()
    registry.list_global_repos.return_value = []
    sched = _make_scheduler(golden_repos_dir, False, registry=registry)

    sched.reconcile_golden_repos()

    # Self-managed: reconciliation ran (listed repos) and wrote the marker.
    registry.list_global_repos.assert_called_once()
    assert (golden_repos_dir / ".reconciliation_complete_v1").exists()
