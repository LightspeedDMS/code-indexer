"""
Tests for Bug #1842 Finding 1 (second required fix): cancel_running_analysis()
must not treat "self._lock is free" as proof nothing is running.

Before this fix, cancel_running_analysis() probed ONLY self._lock: if it
could acquire the lock (meaning nothing in THIS process currently holds it),
it returned {"status": "no_active_job"} unconditionally -- even when a fresh
SharedJobSentinel was still active. This is exactly the state a route-
pre-claimed-then-leaked sentinel (Finding 1's first defect, now fixed) or any
other orphaned local claim leaves behind: self._lock free (no worker thread
ever reached it), sentinel fresh (not yet stale). Cancel therefore could not
help recover from that state, matching test_13_depmap_coordination_1133.py's
"is_available() still False after bounded wait and cancel" failure text
exactly.

The fix mirrors is_available()'s own sentinel-then-lock check order: when
self._lock is free but a fresh sentinel is active, consult its node_id --
release it directly if it belongs to THIS node (nothing to signal via
_cancel_event, since no local worker thread is executing), but leave a
DIFFERENT node's legitimate claim untouched.

Uses a REAL SharedJobSentinel (Messi Rule #1 anti-mock).
"""

from pathlib import Path
from unittest.mock import MagicMock

from code_indexer.server.services.dependency_map_service import (
    ANALYSIS_STALE_TIMEOUT_SECONDS,
    DependencyMapService,
)
from code_indexer.server.services.shared_job_sentinel import SharedJobSentinel


def _make_service(golden_repos_dir):
    golden_repos_manager = MagicMock()
    golden_repos_manager.golden_repos_dir = str(golden_repos_dir)
    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=MagicMock(),
        tracking_backend=MagicMock(),
        analyzer=MagicMock(),
    )


def _claim_sentinel(golden_repos_dir, job_id, node_id):
    sentinel_dir = Path(golden_repos_dir) / "cidx-meta" / "dependency-map"
    sentinel = SharedJobSentinel(sentinel_dir, ANALYSIS_STALE_TIMEOUT_SECONDS)
    claim = sentinel.try_claim("analysis", job_id, node_id)
    assert claim.success is True
    return sentinel


class TestCancelReleasesOrphanedLocalSentinel:
    def test_releases_local_orphaned_sentinel_and_reports_success(self, tmp_path):
        service = _make_service(tmp_path)
        local_node_id = service._get_node_id()
        _claim_sentinel(tmp_path, "orphaned-local-job", local_node_id)

        # self._lock is free -- no worker thread is executing.
        result = service.cancel_running_analysis()

        assert result.get("success") is True, result
        assert service.is_available() is True, (
            "orphaned local sentinel must be released so is_available() recovers"
        )

    def test_no_active_job_when_sentinel_and_lock_both_free(self, tmp_path):
        """Regression guard: the ordinary idle case (no sentinel at all)
        must still report no_active_job, unchanged."""
        service = _make_service(tmp_path)

        result = service.cancel_running_analysis()

        assert result.get("status") == "no_active_job", result
        assert not service._cancel_event.is_set()


class TestCancelNeverTouchesForeignNodeSentinel:
    def test_does_not_release_a_different_nodes_live_sentinel(self, tmp_path):
        service = _make_service(tmp_path)
        local_node_id = service._get_node_id()
        foreign_node_id = f"{local_node_id}-definitely-foreign-xyz"
        sentinel = _claim_sentinel(tmp_path, "foreign-job", foreign_node_id)

        service.cancel_running_analysis()

        # The foreign node's legitimate claim must survive untouched.
        active = sentinel.read_active("analysis")
        assert active is not None, (
            "cancel_running_analysis must never release another node's "
            "live sentinel claim"
        )
        assert active.job_id == "foreign-job"
        assert not service._cancel_event.is_set(), (
            "no local worker exists for a foreign node's job -- nothing to signal"
        )


class TestCancelStillSignalsRealLocalRun:
    def test_lock_held_still_sets_cancel_event_and_reports_success(self, tmp_path):
        """Regression guard: the existing, already-tested behavior (a real
        in-process run holding self._lock) must be completely unaffected
        by this fix."""
        service = _make_service(tmp_path)
        service._lock.acquire()
        try:
            result = service.cancel_running_analysis()
            assert result.get("success") is True
            assert service._cancel_event.is_set()
        finally:
            service._lock.release()
