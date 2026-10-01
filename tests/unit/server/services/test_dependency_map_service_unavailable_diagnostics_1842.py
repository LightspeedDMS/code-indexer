"""
Unit tests for DependencyMapService.describe_unavailable_reason() (Bug #1842
AC4).

test_13_depmap_coordination_1133.py's test_ac1_concurrent_triggers_single_winner
failed with a bare "dep_map_service.is_available() still False after bounded
wait and cancel" -- naming the symptom, not the cause. This diagnostic method
reports the REAL SharedJobSentinel holder (job_id/node_id/elapsed) and
whether the in-process threading.Lock is held, so a failure message can
include it.

Uses a REAL SharedJobSentinel (file-based, Messi Rule #1 anti-mock) rather
than mocking sentinel state.
"""

import threading
import time
from unittest.mock import MagicMock

from code_indexer.server.services.dependency_map_service import DependencyMapService
from code_indexer.server.services.shared_job_sentinel import SharedJobSentinel

# Matches DependencyMapService's own ANALYSIS_STALE_TIMEOUT_SECONDS (4 hours).
STALE_TIMEOUT_SECONDS = 14400
# Bounded wait for the holder thread to signal it has acquired the lock.
THREAD_JOIN_TIMEOUT_SECONDS = 5
# Short settle delay so the holder thread's acquire() is observably in
# effect before the diagnostic probes it.
LOCK_SETTLE_SECONDS = 0.05


def _make_service(golden_repos_dir):
    golden_repos_manager = MagicMock()
    golden_repos_manager.golden_repos_dir = str(golden_repos_dir)

    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=MagicMock(),
        tracking_backend=MagicMock(),
        analyzer=MagicMock(),
    )


class TestDescribeUnavailableReasonNoSentinel:
    def test_reports_no_active_sentinel_and_free_lock(self, tmp_path):
        service = _make_service(tmp_path)

        reason = service.describe_unavailable_reason()

        assert "no active sentinel" in reason
        assert "free" in reason


class TestDescribeUnavailableReasonWithSentinel:
    def test_reports_real_sentinel_job_id_node_id_and_elapsed(self, tmp_path):
        service = _make_service(tmp_path)
        sentinel_dir = tmp_path / "cidx-meta" / "dependency-map"
        sentinel_dir.mkdir(parents=True)
        sentinel = SharedJobSentinel(
            sentinel_dir, stale_timeout_seconds=STALE_TIMEOUT_SECONDS
        )
        claim = sentinel.try_claim("analysis", "dep-map-full-abcdef", "node-1")
        assert claim.success is True

        reason = service.describe_unavailable_reason()

        assert "dep-map-full-abcdef" in reason
        assert "node-1" in reason
        assert "elapsed" in reason


class TestDescribeUnavailableReasonInProcessLock:
    def test_reports_lock_held_when_another_thread_holds_it(self, tmp_path):
        service = _make_service(tmp_path)

        held = threading.Event()
        release = threading.Event()

        def _holder():
            service._lock.acquire()
            held.set()
            release.wait(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
            service._lock.release()

        t = threading.Thread(target=_holder)
        t.start()
        try:
            assert held.wait(timeout=THREAD_JOIN_TIMEOUT_SECONDS), (
                "holder thread failed to acquire lock"
            )
            time.sleep(LOCK_SETTLE_SECONDS)

            reason = service.describe_unavailable_reason()

            assert "HELD" in reason
        finally:
            release.set()
            t.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)

    def test_reports_lock_free_when_uncontended(self, tmp_path):
        service = _make_service(tmp_path)

        reason = service.describe_unavailable_reason()

        assert "free" in reason
        assert "HELD" not in reason


class TestDescribeUnavailableReasonNeverRaises:
    def test_never_raises_when_golden_repos_manager_returns_non_path(self):
        golden_repos_manager = MagicMock()
        golden_repos_manager.golden_repos_dir = object()  # non-path type

        service = DependencyMapService(
            golden_repos_manager=golden_repos_manager,
            config_manager=MagicMock(),
            tracking_backend=MagicMock(),
            analyzer=MagicMock(),
        )

        reason = service.describe_unavailable_reason()

        assert isinstance(reason, str)
        assert len(reason) > 0

    def test_never_raises_when_in_process_lock_probe_itself_raises(self, tmp_path):
        """Bug #1842 Finding 3: _describe_in_process_lock_state() lacked
        the try/except its sibling _describe_sentinel_state() has. This
        output is interpolated directly into pytest assertion messages
        (see test_13_depmap_coordination_1133.py) -- a raise here would
        replace a real AssertionError with an unrelated exception."""
        service = _make_service(tmp_path)

        class _RaisingLock:
            def acquire(self, blocking=False):
                raise RuntimeError("simulated lock probe failure")

            def release(self):  # pragma: no cover - never reached
                pass

        service._lock = _RaisingLock()

        reason = service.describe_unavailable_reason()

        assert isinstance(reason, str)
        assert len(reason) > 0
