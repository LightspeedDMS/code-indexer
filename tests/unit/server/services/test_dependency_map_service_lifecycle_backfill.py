"""
Regression tests for Bug #748: _queue_lifecycle_backfill_if_needed
accessed self._refresh_scheduler._tracking_backend._conn_manager, but
RefreshScheduler has no _tracking_backend attribute. The attribute lives
on DependencyMapService itself (self._tracking_backend).

These tests call the REAL _queue_lifecycle_backfill_if_needed without
patching it out, so the original attribute-error class of bugs is caught.
"""

from unittest.mock import MagicMock, Mock

from code_indexer.server.services.dependency_map_service import DependencyMapService


def _make_service_with_tracking_backend(tmp_path, refresh_scheduler=None):
    """
    Build a real DependencyMapService where tracking_backend has a real
    _conn_manager attribute (correct structure). refresh_scheduler
    deliberately lacks _tracking_backend to mirror the real RefreshScheduler.
    """
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (0,)
    conn.execute.return_value.fetchall.return_value = []
    conn.execute.return_value.rowcount = 0

    conn_manager = MagicMock()
    conn_manager.execute_atomic.side_effect = lambda fn: fn(conn)

    tracking = Mock()
    tracking._conn_manager = conn_manager
    tracking.get_tracking.return_value = {"status": "pending", "commit_hashes": None}

    gm = Mock()
    gm.golden_repos_dir = str(tmp_path)

    return DependencyMapService(
        gm,
        Mock(),
        tracking,
        Mock(),
        refresh_scheduler=refresh_scheduler,
        description_refresh_tracking_backend=tracking,
    )


class TestLifecycleBackfillAttributeWiring:
    """Regression guard for Bug #748: _queue_lifecycle_backfill_if_needed
    accessed an attribute on RefreshScheduler that doesn't exist."""

    def test_method_accesses_only_attributes_that_exist(self, tmp_path):
        """Constructing DependencyMapService and invoking
        _queue_lifecycle_backfill_if_needed must not raise AttributeError.

        The refresh_scheduler mock's spec omits _tracking_backend, matching
        the real RefreshScheduler. The old bug path would crash here with:
          AttributeError: 'Mock' object has no attribute '_tracking_backend'
        """
        scheduler = Mock(spec=["acquire_write_lock", "release_write_lock"])
        service = _make_service_with_tracking_backend(
            tmp_path, refresh_scheduler=scheduler
        )

        queued_count = service._queue_lifecycle_backfill_if_needed()

        assert isinstance(queued_count, int)

    def test_returns_zero_when_refresh_scheduler_is_none(self, tmp_path):
        """Edge case path: if _refresh_scheduler is None,
        the method returns 0 without attempting attribute access."""
        service = _make_service_with_tracking_backend(tmp_path, refresh_scheduler=None)

        assert service._queue_lifecycle_backfill_if_needed() == 0
