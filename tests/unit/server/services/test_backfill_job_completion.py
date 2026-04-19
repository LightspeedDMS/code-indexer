"""
Unit tests for Bug #853 Fix 3 & 4: aggregate lifecycle_backfill job_id must be
stored and propagated so complete_job can be called when all repos finish.

TDD Red phase: tests written BEFORE the fix.

Covered scenarios:
1. _backfill_register_aggregate_job returns the exact job_id it passed to register_job
2. _backfill_register_aggregate_job returns None when job_tracker is None
3. _backfill_register_aggregate_job returns None when total is 0
4. _active_backfill_job_id initialised to None in __init__
5. _queue_lifecycle_backfill_if_needed stores the exact job_id in _active_backfill_job_id
6. scheduler.set_active_backfill_job_id receives the exact registered job_id
7. non-owner nodes leave _active_backfill_job_id as None
"""

from unittest.mock import MagicMock, Mock

from code_indexer.server.services.dependency_map_service import DependencyMapService
from code_indexer.server.services.job_tracker import DuplicateJobError

_CLUSTER_WIDE_TOTAL = 5


def _make_conn_manager_as_owner(total: int = _CLUSTER_WIDE_TOTAL):
    """Return a conn_manager where the count query returns a non-zero total."""
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (total,)
    conn.execute.return_value.fetchall.return_value = []
    conn.execute.return_value.rowcount = 0

    conn_manager = MagicMock()
    conn_manager.execute_atomic.side_effect = lambda fn: fn(conn)
    return conn_manager


def _make_tracker_as_owner():
    """Return a mock JobTracker that does not raise DuplicateJobError (this node is owner)."""
    mock_tracker = MagicMock()
    mock_tracker.check_operation_conflict.return_value = None
    return mock_tracker


def _make_tracker_as_non_owner():
    """Return a mock JobTracker that raises DuplicateJobError (another node is owner)."""
    mock_tracker = MagicMock()
    mock_tracker.check_operation_conflict.side_effect = DuplicateJobError(
        "lifecycle_backfill", None, "existing-job-id"
    )
    return mock_tracker


def _make_service(
    tmp_path, job_tracker=None, refresh_scheduler=None, conn_manager=None
):
    """Build a real DependencyMapService with controllable external dependencies."""
    effective_conn_manager = conn_manager or _make_conn_manager_as_owner()

    tracking = Mock()
    tracking._conn_manager = effective_conn_manager
    tracking.get_tracking.return_value = {"status": "pending", "commit_hashes": None}

    gm = Mock()
    gm.golden_repos_dir = str(tmp_path)

    return DependencyMapService(
        gm,
        Mock(),
        tracking,
        Mock(),
        refresh_scheduler=refresh_scheduler,
        job_tracker=job_tracker,
    )


class TestBackfillRegisterAggregateJobReturnsId:
    """Fix 3: _backfill_register_aggregate_job must return the exact job_id string."""

    def test_returns_exact_job_id_passed_to_register_job(self, tmp_path):
        """_backfill_register_aggregate_job returns the same job_id it passed to register_job."""
        mock_tracker = _make_tracker_as_owner()
        service = _make_service(tmp_path, job_tracker=mock_tracker)

        result = service._backfill_register_aggregate_job(_CLUSTER_WIDE_TOTAL)

        assert result is not None, (
            "_backfill_register_aggregate_job must return job_id, got None"
        )
        registered_job_id = mock_tracker.register_job.call_args[0][0]
        assert result == registered_job_id, (
            f"Returned job_id {result!r} must match the id passed to register_job "
            f"{registered_job_id!r}"
        )

    def test_returns_none_when_job_tracker_is_none(self, tmp_path):
        """Returns None (no-op) when job_tracker is not configured."""
        service = _make_service(tmp_path, job_tracker=None)

        result = service._backfill_register_aggregate_job(_CLUSTER_WIDE_TOTAL)

        assert result is None

    def test_returns_none_when_total_is_zero(self, tmp_path):
        """Returns None (no-op) when cluster_wide_total is 0."""
        mock_tracker = _make_tracker_as_owner()
        service = _make_service(tmp_path, job_tracker=mock_tracker)

        result = service._backfill_register_aggregate_job(0)

        assert result is None
        call_count = mock_tracker.register_job.call_count
        assert call_count == 0, (
            f"register_job must not be called when total=0, but was called {call_count} times"
        )


class TestActiveBackfillJobIdInitialised:
    """Fix 3: _active_backfill_job_id must be initialised to None in __init__."""

    def test_active_backfill_job_id_starts_as_none(self, tmp_path):
        """DependencyMapService.__init__ sets _active_backfill_job_id = None."""
        service = _make_service(tmp_path)

        has_attr = hasattr(service, "_active_backfill_job_id")
        assert has_attr is True, (
            "DependencyMapService must have _active_backfill_job_id attribute"
        )
        assert service._active_backfill_job_id is None


class TestQueueLifecycleBackfillStoresJobId:
    """Fix 3: _queue_lifecycle_backfill_if_needed stores exact returned job_id."""

    def _make_owner_service(self, tmp_path, scheduler):
        """Build service that acts as aggregate owner via real collaborator mocks."""
        mock_tracker = _make_tracker_as_owner()
        conn_manager = _make_conn_manager_as_owner(total=_CLUSTER_WIDE_TOTAL)
        return _make_service(
            tmp_path,
            job_tracker=mock_tracker,
            refresh_scheduler=scheduler,
            conn_manager=conn_manager,
        ), mock_tracker

    def test_stores_exact_job_id_in_active_backfill_job_id(self, tmp_path):
        """_queue_lifecycle_backfill_if_needed stores the exact job_id from registration."""
        scheduler = Mock()
        scheduler.set_active_backfill_job_id = Mock()
        service, mock_tracker = self._make_owner_service(tmp_path, scheduler)

        service._queue_lifecycle_backfill_if_needed()

        registered_job_id = mock_tracker.register_job.call_args[0][0]
        stored_job_id = service._active_backfill_job_id
        assert stored_job_id == registered_job_id, (
            f"_active_backfill_job_id {stored_job_id!r} must equal "
            f"the job_id passed to register_job {registered_job_id!r}"
        )

    def test_scheduler_receives_exact_registered_job_id(self, tmp_path):
        """scheduler.set_active_backfill_job_id receives the exact registered job_id."""
        scheduler = Mock()
        scheduler.set_active_backfill_job_id = Mock()
        service, mock_tracker = self._make_owner_service(tmp_path, scheduler)

        service._queue_lifecycle_backfill_if_needed()

        registered_job_id = mock_tracker.register_job.call_args[0][0]
        passed_to_scheduler = scheduler.set_active_backfill_job_id.call_args[0][0]
        assert passed_to_scheduler == registered_job_id, (
            f"Scheduler received {passed_to_scheduler!r} but expected "
            f"registered job_id {registered_job_id!r}"
        )

    def test_leaves_job_id_none_when_not_owner(self, tmp_path):
        """Non-owner nodes do not register the aggregate job and leave job_id None."""
        scheduler = Mock()
        non_owner_tracker = _make_tracker_as_non_owner()
        service = _make_service(
            tmp_path,
            job_tracker=non_owner_tracker,
            refresh_scheduler=scheduler,
        )

        service._queue_lifecycle_backfill_if_needed()

        stored_job_id = service._active_backfill_job_id
        assert stored_job_id is None, (
            f"Non-owner must leave _active_backfill_job_id as None, got {stored_job_id!r}"
        )
        call_count = non_owner_tracker.register_job.call_count
        assert call_count == 0, (
            f"register_job must not be called for non-owner, was called {call_count} times"
        )
