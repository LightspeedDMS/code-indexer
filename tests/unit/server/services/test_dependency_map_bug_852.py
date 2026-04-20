"""
Bug #852: run_full_analysis skips lifecycle backfill queue on first-run repos.

run_delta_analysis calls _queue_lifecycle_backfill_if_needed() unconditionally,
FIRST, before any other logic.  run_full_analysis does NOT — that is the bug.
Fresh repos (no prior delta run) never queue lifecycle_backfill.

Fix: add _queue_lifecycle_backfill_if_needed() as the first statement in
run_full_analysis, matching run_delta_analysis exactly (Story #728 AC2 pattern).

Test approach:
  - Mock only external collaborators (tracking backend conn_manager).
  - Verify observable external effects via conn_manager.execute_atomic call count.
  - config_manager returns dependency_map_enabled=False for early return (no FS I/O).
  - job_tracker=None makes _backfill_try_acquire_aggregate_job return False directly.
  - Parametrized test eliminates duplicate setup across backend-present/absent cases.

Test classes:
  TestRunFullAnalysisBackfillExternalEffect
    — parametrized: conn_manager.execute_atomic call count proves backfill ran.
  TestQueueLifecycleBackfillIfNeededNoOpGuard
    — direct unit test: helper returns 0 when description_refresh_tracking_backend is None.
"""

import pytest
from unittest.mock import MagicMock, Mock

from code_indexer.server.services.dependency_map_service import DependencyMapService


# ---------------------------------------------------------------------------
# Shared factory
# ---------------------------------------------------------------------------


def _make_conn_manager():
    """Return a mock conn_manager with empty query results."""
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (0,)
    conn.execute.return_value.fetchall.return_value = []
    conn.execute.return_value.rowcount = 0
    conn_manager = MagicMock()
    conn_manager.execute_atomic.side_effect = lambda fn: fn(conn)
    return conn_manager


def _make_service_and_conn_manager(tmp_path, *, with_tracking_backend: bool):
    """
    Build a DependencyMapService with only external collaborators mocked.

    Returns (service, conn_manager_or_none).

    job_tracker=None: _backfill_try_acquire_aggregate_job returns False without
    requiring any SUT method patching.

    dependency_map_enabled=False: triggers early return in run_full_analysis
    (status='disabled') so no filesystem I/O is required.
    """
    gm = Mock()
    gm.golden_repos_dir = str(tmp_path)

    tracking = Mock()
    tracking.get_tracking.return_value = {"status": "pending", "commit_hashes": None}
    tracking.update_tracking.return_value = None

    config_manager = Mock()
    config_manager.get_claude_integration_config.return_value = Mock(
        dependency_map_enabled=False
    )

    conn_manager = None
    description_refresh_tracking_backend = None
    if with_tracking_backend:
        conn_manager = _make_conn_manager()
        description_refresh_tracking_backend = Mock()
        description_refresh_tracking_backend._conn_manager = conn_manager

    service = DependencyMapService(
        gm,
        config_manager,
        tracking,
        Mock(),
        refresh_scheduler=None,
        job_tracker=None,
        description_refresh_tracking_backend=description_refresh_tracking_backend,
    )
    return service, conn_manager


# ---------------------------------------------------------------------------
# Class 1: observable external effect in run_full_analysis
# ---------------------------------------------------------------------------


class TestRunFullAnalysisBackfillExternalEffect:
    """
    Parametrized test verifying conn_manager.execute_atomic call count when
    run_full_analysis executes, as the observable external effect of calling
    _queue_lifecycle_backfill_if_needed().

    With backend configured: execute_atomic must be called at least once.
      Fails before fix (no call), passes after fix.

    Without backend: execute_atomic is None; run_full_analysis must still
      complete normally (no-op path validation).
    """

    @pytest.mark.parametrize(
        "with_tracking_backend, min_execute_atomic_calls",
        [
            pytest.param(True, 1, id="with_backend_execute_atomic_called"),
            pytest.param(False, 0, id="without_backend_completes_normally"),
        ],
    )
    def test_execute_atomic_call_count_reflects_backend_presence(
        self, tmp_path, with_tracking_backend, min_execute_atomic_calls
    ):
        """
        conn_manager.execute_atomic call count reflects whether the tracking
        backend is configured and _queue_lifecycle_backfill_if_needed was invoked.

        id=with_backend_execute_atomic_called:
          Fails before fix (execute_atomic.call_count == 0), passes after.

        id=without_backend_completes_normally:
          No conn_manager exists; asserts only that run_full_analysis completes.
        """
        service, conn_manager = _make_service_and_conn_manager(
            tmp_path, with_tracking_backend=with_tracking_backend
        )

        result = service.run_full_analysis()

        assert result is not None
        assert "status" in result

        if conn_manager is not None:
            assert conn_manager.execute_atomic.call_count >= min_execute_atomic_calls, (
                f"conn_manager.execute_atomic must be called at least "
                f"{min_execute_atomic_calls} time(s) by run_full_analysis "
                f"when tracking backend is configured. "
                f"Got {conn_manager.execute_atomic.call_count} calls. "
                "Bug #852: _queue_lifecycle_backfill_if_needed is missing from run_full_analysis."
            )


# ---------------------------------------------------------------------------
# Class 2: direct unit test — no-op guard when backend absent
# ---------------------------------------------------------------------------


class TestQueueLifecycleBackfillIfNeededNoOpGuard:
    """
    Direct unit test: _queue_lifecycle_backfill_if_needed returns 0 immediately
    when description_refresh_tracking_backend is None.
    """

    def test_returns_zero_when_no_tracking_backend(self, tmp_path):
        """Returns 0 when description_refresh_tracking_backend is None."""
        service, _ = _make_service_and_conn_manager(
            tmp_path, with_tracking_backend=False
        )

        result = service._queue_lifecycle_backfill_if_needed()

        assert result == 0, (
            f"_queue_lifecycle_backfill_if_needed must return 0 when "
            f"description_refresh_tracking_backend is None. Got {result!r}."
        )
