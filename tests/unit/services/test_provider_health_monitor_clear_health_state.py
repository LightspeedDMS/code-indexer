"""Tests for Bug #902: clear_health_state_all() on ProviderHealthMonitor.

Verifies that clear_health_state_all() resets ALL rolling health state:
  - _metrics (HealthMetric deques)
  - _consecutive_failures
  - _sinbin_failure_deque (windowed failure timestamps)
  - _last_known_status
  - _sinbin_until (active sinbin expiry timestamps)
  - _sinbin_rounds (backoff round counters)
  - Active recovery probe threads (stopped before state wipe)

Does NOT clear:
  - _probe_functions (registered by provider constructors; not re-registered after clearing)

This is distinct from clear_sinbin_all() which only clears sinbin cooldown timers.
The broader wipe is required so _compute_status() stops returning "down" even after
sinbin is cleared (pre-skip gate checks BOTH is_sinbinned() AND _compute_status().status).

Bug #902 root-cause: clear_sinbin_all() cleared sinbin timers but left _metrics with
error_rate=1.0 and _consecutive_failures >= 5, causing _compute_status() to return "down",
which caused the pre-skip gate in semantic_query_manager to skip all providers even after
clear_sinbin_all() was called from the test fixture.
"""

import queue
import threading
import time

import pytest


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset ProviderHealthMonitor singleton before/after each test."""
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    ProviderHealthMonitor.reset_instance()
    yield
    ProviderHealthMonitor.reset_instance()


@pytest.fixture
def monitor():
    """Fresh ProviderHealthMonitor instance (not singleton)."""
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    return ProviderHealthMonitor()


# ---------------------------------------------------------------------------
# Helper: populate monitor with failed call state to simulate "down" provider
# ---------------------------------------------------------------------------


def _drive_provider_to_down(monitor, provider: str, num_failures: int = 6) -> None:
    """Record enough consecutive failures to drive provider status to 'down'."""
    for _ in range(num_failures):
        monitor.record_call(provider, 100.0, success=False)


# ---------------------------------------------------------------------------
# Core state clearing: each of the 6 collections must be empty after call
# ---------------------------------------------------------------------------


class TestClearHealthStateAllClearsMetrics:
    """_metrics deque must be empty for all providers after clear_health_state_all()."""

    def test_clears_metrics_for_single_provider(self, monitor):
        monitor.record_call("voyage-ai", 100.0, success=False)
        assert len(monitor._metrics["voyage-ai"]) > 0
        monitor.clear_health_state_all()
        assert len(monitor._metrics) == 0

    def test_clears_metrics_for_multiple_providers(self, monitor):
        monitor.record_call("voyage-ai", 100.0, success=False)
        monitor.record_call("cohere", 100.0, success=False)
        monitor.clear_health_state_all()
        assert len(monitor._metrics) == 0

    def test_clears_metrics_on_empty_monitor_is_noop(self, monitor):
        """No state to clear -- must not raise."""
        monitor.clear_health_state_all()
        assert len(monitor._metrics) == 0


class TestClearHealthStateAllClearsConsecutiveFailures:
    """_consecutive_failures must be empty for all providers after clear_health_state_all()."""

    def test_clears_consecutive_failures(self, monitor):
        _drive_provider_to_down(monitor, "voyage-ai")
        assert monitor._consecutive_failures.get("voyage-ai", 0) >= 5
        monitor.clear_health_state_all()
        assert len(monitor._consecutive_failures) == 0

    def test_clears_consecutive_failures_for_multiple_providers(self, monitor):
        _drive_provider_to_down(monitor, "voyage-ai")
        _drive_provider_to_down(monitor, "cohere")
        monitor.clear_health_state_all()
        assert len(monitor._consecutive_failures) == 0


class TestClearHealthStateAllClearsSinbinFailureDeque:
    """_sinbin_failure_deque must be empty for all providers after clear_health_state_all()."""

    def test_clears_sinbin_failure_deque(self, monitor):
        monitor.record_call("voyage-ai", 100.0, success=False)
        assert "voyage-ai" in monitor._sinbin_failure_deque
        monitor.clear_health_state_all()
        assert len(monitor._sinbin_failure_deque) == 0

    def test_clears_sinbin_failure_deque_for_multiple_providers(self, monitor):
        monitor.record_call("voyage-ai", 100.0, success=False)
        monitor.record_call("cohere", 100.0, success=False)
        monitor.clear_health_state_all()
        assert len(monitor._sinbin_failure_deque) == 0


class TestClearHealthStateAllClearsLastKnownStatus:
    """_last_known_status must be empty for all providers after clear_health_state_all()."""

    def test_clears_last_known_status(self, monitor):
        monitor.record_call("voyage-ai", 100.0, success=False)
        # _last_known_status is populated by record_call via _compute_status transition
        assert "voyage-ai" in monitor._last_known_status
        monitor.clear_health_state_all()
        assert len(monitor._last_known_status) == 0

    def test_clears_last_known_status_for_multiple_providers(self, monitor):
        monitor.record_call("voyage-ai", 100.0, success=False)
        monitor.record_call("cohere", 100.0, success=True)
        monitor.clear_health_state_all()
        assert len(monitor._last_known_status) == 0


class TestClearHealthStateAllClearsSinbinTimers:
    """_sinbin_until and _sinbin_rounds must be empty after clear_health_state_all()."""

    def test_clears_sinbin_until(self, monitor):
        monitor.sinbin("voyage-ai")
        assert "voyage-ai" in monitor._sinbin_until
        monitor.clear_health_state_all()
        assert len(monitor._sinbin_until) == 0

    def test_clears_sinbin_rounds(self, monitor):
        monitor.sinbin("voyage-ai")
        monitor.sinbin("voyage-ai")
        assert monitor._sinbin_rounds.get("voyage-ai", 0) == 2
        monitor.clear_health_state_all()
        assert len(monitor._sinbin_rounds) == 0

    def test_is_sinbinned_returns_false_after_clear(self, monitor):
        """After clear_health_state_all(), is_sinbinned() must return False."""
        monitor.sinbin("voyage-ai")
        assert monitor.is_sinbinned("voyage-ai") is True
        monitor.clear_health_state_all()
        assert monitor.is_sinbinned("voyage-ai") is False

    def test_clears_both_sinbin_collections_for_multiple_providers(self, monitor):
        monitor.sinbin("voyage-ai")
        monitor.sinbin("cohere")
        monitor.clear_health_state_all()
        assert len(monitor._sinbin_until) == 0
        assert len(monitor._sinbin_rounds) == 0


# ---------------------------------------------------------------------------
# Critical: _compute_status() must return "healthy" after clear
# ---------------------------------------------------------------------------


class TestClearHealthStateAllFixesComputeStatus:
    """After clear_health_state_all(), _compute_status() must NOT return 'down'.

    This is the actual bug: clear_sinbin_all() cleared sinbin timers but left
    _metrics with error_rate=1.0 causing _compute_status() to still return 'down'.
    The pre-skip gate in semantic_query_manager checks BOTH is_sinbinned() AND
    _compute_status().status -- so providers were still skipped after sinbin-only clear.
    """

    def test_compute_status_returns_healthy_after_clear(self, monitor):
        """Provider driven to 'down' must report 'healthy' after clear_health_state_all()."""
        _drive_provider_to_down(monitor, "voyage-ai")
        with monitor._data_lock:
            status_before = monitor._compute_status("voyage-ai").status
        assert status_before == "down"

        monitor.clear_health_state_all()

        # After clear, _metrics is empty -> _compute_status returns empty status = "healthy"
        with monitor._data_lock:
            status_after = monitor._compute_status("voyage-ai").status
        assert status_after == "healthy"

    def test_compute_status_healthy_for_both_providers_after_clear(self, monitor):
        """Both providers driven to down must both report 'healthy' after clear."""
        _drive_provider_to_down(monitor, "voyage-ai")
        _drive_provider_to_down(monitor, "cohere")

        monitor.clear_health_state_all()

        with monitor._data_lock:
            assert monitor._compute_status("voyage-ai").status == "healthy"
            assert monitor._compute_status("cohere").status == "healthy"

    def test_sinbin_cleared_does_not_fix_compute_status_without_full_clear(
        self, monitor
    ):
        """Regression guard: clear_sinbin_all() alone does NOT fix _compute_status.

        This test documents the original Bug #902 root cause: clearing sinbin timers
        alone is insufficient because _compute_status() uses _metrics error_rate,
        not sinbin state, to determine 'down' status.
        """
        _drive_provider_to_down(monitor, "voyage-ai")
        monitor.sinbin("voyage-ai")

        # clear_sinbin_all() only -- the original incomplete fix
        monitor.clear_sinbin_all()

        # Provider is no longer sinbinned ...
        assert monitor.is_sinbinned("voyage-ai") is False
        # ... but _compute_status still returns 'down' because _metrics has error_rate=1.0
        with monitor._data_lock:
            status = monitor._compute_status("voyage-ai").status
        assert status == "down", (
            "Regression guard: clear_sinbin_all() alone does not fix compute_status. "
            "clear_health_state_all() is required."
        )


# ---------------------------------------------------------------------------
# Probe functions: _probe_functions must NOT be cleared
# ---------------------------------------------------------------------------


class TestClearHealthStateAllPreservesProbe:
    """_probe_functions must survive clear_health_state_all() (cannot be re-registered)."""

    def test_probe_functions_preserved_after_clear(self, monitor):
        probe_fn = lambda: True  # noqa: E731
        monitor.register_probe("voyage-ai", probe_fn)
        assert "voyage-ai" in monitor._probe_functions

        monitor.clear_health_state_all()

        assert "voyage-ai" in monitor._probe_functions
        assert monitor._probe_functions["voyage-ai"] is probe_fn

    def test_probe_functions_for_multiple_providers_preserved(self, monitor):
        probe_voyage = lambda: True  # noqa: E731
        probe_cohere = lambda: True  # noqa: E731
        monitor.register_probe("voyage-ai", probe_voyage)
        monitor.register_probe("cohere", probe_cohere)

        monitor.clear_health_state_all()

        assert monitor._probe_functions["voyage-ai"] is probe_voyage
        assert monitor._probe_functions["cohere"] is probe_cohere


# ---------------------------------------------------------------------------
# Active recovery probes: must be stopped before state wipe
# ---------------------------------------------------------------------------

# Timeout (seconds) for joining a probe thread that was signaled to stop.
# The fake thread only waits on the stop event; 2 s is ample headroom.
_PROBE_JOIN_TIMEOUT: float = 2.0


class TestClearHealthStateAllStopsRecoveryProbes:
    """Active recovery probe threads must be stopped by clear_health_state_all()."""

    def test_probe_threads_cleared_after_stop(self, monitor):
        """After clear, no probe threads remain registered and the thread has exited."""
        # Inject a fake probe that blocks on a stop event (simulates a live probe)
        stop_evt = threading.Event()
        fake_thread = threading.Thread(target=stop_evt.wait, daemon=True)
        fake_thread.start()
        with monitor._probe_lock:
            monitor._probe_threads["voyage-ai"] = fake_thread
            monitor._probe_stop_events["voyage-ai"] = stop_evt

        monitor.clear_health_state_all()

        # Both probe registries must be empty
        assert len(monitor._probe_threads) == 0
        assert len(monitor._probe_stop_events) == 0
        # Stop event must have been signaled
        assert stop_evt.is_set()
        # The probe thread must have actually exited, not merely been signaled
        fake_thread.join(timeout=_PROBE_JOIN_TIMEOUT)
        assert not fake_thread.is_alive(), (
            "Recovery probe thread did not terminate after clear_health_state_all()"
        )

    def test_no_active_probes_clear_is_noop(self, monitor):
        """clear_health_state_all() with no active probes must not raise."""
        monitor.clear_health_state_all()
        assert len(monitor._probe_threads) == 0
        assert len(monitor._probe_stop_events) == 0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestClearHealthStateAllIdempotent:
    """Calling clear_health_state_all() multiple times must not raise."""

    def test_double_clear_is_safe(self, monitor):
        _drive_provider_to_down(monitor, "voyage-ai")
        monitor.clear_health_state_all()
        monitor.clear_health_state_all()  # must not raise

        assert len(monitor._metrics) == 0
        assert monitor.is_sinbinned("voyage-ai") is False

    def test_clear_then_new_calls_tracked_normally(self, monitor):
        """After clear, the monitor still tracks new calls correctly."""
        _drive_provider_to_down(monitor, "voyage-ai")
        monitor.clear_health_state_all()

        # New successful call after clear
        monitor.record_call("voyage-ai", 50.0, success=True)
        health = monitor.get_health("voyage-ai")
        assert health["voyage-ai"].total_requests == 1
        assert health["voyage-ai"].error_rate == 0.0


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

# Number of record_call iterations in the bounded recorder thread.
# Large enough to race with clear operations, small enough to finish quickly.
_RECORD_ITERATIONS: int = 200


class TestClearHealthStateAllThreadSafety:
    """clear_health_state_all() must be thread-safe under concurrent access."""

    def test_concurrent_clear_and_record_do_not_corrupt_state(self, monitor):
        """Concurrent clear + record must not raise or deadlock.

        Uses a bounded loop (_RECORD_ITERATIONS) to guarantee termination.
        Thread errors captured via queue.Queue for thread-safe collection.
        Both threads asserted non-alive after join to detect deadlock.
        """
        errors: queue.Queue = queue.Queue()

        def record_loop():
            try:
                for _ in range(_RECORD_ITERATIONS):
                    monitor.record_call("voyage-ai", 50.0, success=False)
            except Exception as exc:
                errors.put(exc)

        def clear_loop():
            try:
                for _ in range(10):
                    monitor.clear_health_state_all()
                    time.sleep(0.01)
            except Exception as exc:
                errors.put(exc)

        recorder = threading.Thread(target=record_loop)
        clearer = threading.Thread(target=clear_loop)
        recorder.start()
        clearer.start()
        clearer.join(timeout=5.0)
        recorder.join(timeout=5.0)

        # Both threads must have completed -- if either is still alive, it deadlocked
        assert not clearer.is_alive(), "clearer thread deadlocked"
        assert not recorder.is_alive(), "recorder thread deadlocked"

        collected = []
        while not errors.empty():
            collected.append(errors.get_nowait())
        assert not collected, f"Thread errors: {collected}"
