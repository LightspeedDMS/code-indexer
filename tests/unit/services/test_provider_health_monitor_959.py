"""Tests for Bug #959: transition-gate provider health warnings to suppress repeated log spam.

When a provider stays in "degraded" or "down" state across repeated _compute_status()
calls, the logger must emit WARNING only on the transition into that state — not on
every subsequent call that yields the same status.

When the provider returns to "healthy", one INFO log is emitted and the tracker is
reset so that a future degraded/down transition again logs one WARNING.

Test inventory:
    TestTransitionBasedLogging
        test_warning_logged_once_when_entering_degraded
        test_warning_logged_once_when_entering_down
        test_warning_logged_again_on_down_to_degraded_transition
        test_recovery_to_healthy_logs_info_and_resets
        test_debug_on_repeated_same_status
"""

from __future__ import annotations

import logging

import pytest

from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Provider key used across all tests
_PROVIDER = "voyage-ai"

# Module logger name (for caplog targeting)
_LOGGER_NAME = "code_indexer.services.provider_health_monitor"

# Monitor configuration thresholds
_ERROR_RATE_THRESHOLD = 0.1  # 10% error rate -> degraded
_LATENCY_P95_MS = 5000.0  # 5 s p95 latency threshold

# Failure count well above DEFAULT_DOWN_CONSECUTIVE_FAILURES (5) -> "down"
_DOWN_FAILURE_COUNT = 10

# Latency used when recording failures (irrelevant; only success flag matters)
_FAILURE_CALL_LATENCY_MS = 0.0

# _drive_degraded pattern: _DEGRADED_ROUNDS rounds of _DEGRADED_SUCCESS_PER_ROUND
# successes + _DEGRADED_FAILURE_PER_ROUND failure = 20 calls, 20% error rate
_DEGRADED_ROUNDS = 5
_DEGRADED_SUCCESS_PER_ROUND = 4
_DEGRADED_FAILURE_PER_ROUND = 1
_DEGRADED_CALL_LATENCY_MS = 100.0

# Number of consecutive successes sufficient to restore "healthy" after "down".
# Must satisfy BOTH: error_rate < 0.1 AND availability >= 0.95 (DEFAULT_AVAILABILITY_THRESHOLD).
# With _DOWN_FAILURE_COUNT (10) failures: need N such that N/(10+N) >= 0.95 → N >= 190.
# N=200 gives availability=200/210≈0.952 > 0.95 and error_rate=10/210≈0.048 < 0.1.
_RECOVERY_SUCCESS_COUNT = 200

# Partial recovery: enough successes to drop below DEFAULT_DOWN_ERROR_RATE (50%)
# while keeping error rate above _ERROR_RATE_THRESHOLD (10%) -> "degraded".
# Trailing failures must be < DEFAULT_DOWN_CONSECUTIVE_FAILURES (5) to avoid
# re-triggering "down" via the consecutive-failures gate.
# With 10 prior + 40 successes + 4 failures = 54 calls: error_rate = 14/54 ≈ 25.9%,
# consecutive = 4 < 5 → "degraded".
_PARTIAL_RECOVERY_SUCCESS_COUNT = 40
_PARTIAL_RECOVERY_FAILURE_COUNT = 4

# get_health poll counts for the single-status-entry tests
_GET_HEALTH_CALLS = 3  # repeated polls in the degraded-entry test
_DOWN_GET_HEALTH_CALLS = 5  # repeated polls in the down-entry test

# How many times to call get_health in the "repeated same status" scenario
_REPEATED_GET_HEALTH_CALLS = 4

# Expected log counts used in assertions (no raw literals in test bodies)
_EXPECTED_SINGLE_WARNING = 1  # exactly one WARNING per status transition
_EXPECTED_SINGLE_INFO = 1  # exactly one INFO on recovery to healthy
_EXPECTED_NO_WARNINGS = 0  # no WARNINGs when status has not changed


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _reset() -> ProviderHealthMonitor:
    """Return a fresh ProviderHealthMonitor with the singleton reset."""
    ProviderHealthMonitor.reset_instance()
    return ProviderHealthMonitor(
        error_rate_threshold=_ERROR_RATE_THRESHOLD,
        latency_p95_threshold_ms=_LATENCY_P95_MS,
    )


def _count_logs(
    caplog: pytest.LogCaptureFixture,
    level: int,
    *,
    provider: str = _PROVIDER,
) -> int:
    """Count log records at *level* whose message contains *provider*."""
    return len(
        [r for r in caplog.records if r.levelno == level and provider in r.getMessage()]
    )


def _count_health_transition_logs(
    caplog: pytest.LogCaptureFixture,
    level: int,
    *,
    provider: str = _PROVIDER,
) -> int:
    """Count health-state transition logs, excluding sin-bin circuit-breaker messages."""
    return len(
        [
            r
            for r in caplog.records
            if r.levelno == level
            and provider in r.getMessage()
            and "sin-binned" not in r.getMessage()
        ]
    )


def _drive_down(monitor: ProviderHealthMonitor, n: int = _DOWN_FAILURE_COUNT) -> None:
    """Record n consecutive failures to push provider into 'down' state."""
    for _ in range(n):
        monitor.record_call(
            _PROVIDER, latency_ms=_FAILURE_CALL_LATENCY_MS, success=False
        )


def _drive_degraded(monitor: ProviderHealthMonitor) -> None:
    """Interleave successes/failures to produce >10% error rate without triggering 'down'.

    Pattern: _DEGRADED_SUCCESS_PER_ROUND successes then _DEGRADED_FAILURE_PER_ROUND
    failure per round, repeated _DEGRADED_ROUNDS times.
    Error rate = 20% > _ERROR_RATE_THRESHOLD.
    Consecutive failures never exceed 1, so the down threshold (5) is not reached.
    """
    for _ in range(_DEGRADED_ROUNDS):
        for _ in range(_DEGRADED_SUCCESS_PER_ROUND):
            monitor.record_call(
                _PROVIDER, latency_ms=_DEGRADED_CALL_LATENCY_MS, success=True
            )
        monitor.record_call(
            _PROVIDER, latency_ms=_DEGRADED_CALL_LATENCY_MS, success=False
        )


def _drive_healthy(
    monitor: ProviderHealthMonitor, n: int = _RECOVERY_SUCCESS_COUNT
) -> None:
    """Record n consecutive successes to return provider to 'healthy' state."""
    for _ in range(n):
        monitor.record_call(
            _PROVIDER, latency_ms=_DEGRADED_CALL_LATENCY_MS, success=True
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTransitionBasedLogging:
    """WARNING must fire once on status entry; DEBUG thereafter; INFO on healthy return."""

    def setup_method(self) -> None:
        ProviderHealthMonitor.reset_instance()

    def teardown_method(self) -> None:
        ProviderHealthMonitor.reset_instance()

    def test_warning_logged_once_when_entering_degraded(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Degraded polled _GET_HEALTH_CALLS times via get_health: exactly 1 WARNING logged."""
        monitor = _reset()
        _drive_degraded(monitor)  # establish degraded state

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            caplog.clear()
            for _ in range(_GET_HEALTH_CALLS):
                result = monitor.get_health(_PROVIDER)
                assert result[_PROVIDER].status == "degraded", (
                    f"Expected 'degraded', got {result[_PROVIDER].status}"
                )

        warning_count = _count_logs(caplog, logging.WARNING)
        assert warning_count == _EXPECTED_SINGLE_WARNING, (
            f"Expected {_EXPECTED_SINGLE_WARNING} WARNING for degraded entry, "
            f"got {warning_count}. "
            f"Records: {[r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]}"
        )

    def test_warning_logged_once_when_entering_down(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Down polled _DOWN_GET_HEALTH_CALLS times via get_health: exactly 1 WARNING logged."""
        monitor = _reset()
        _drive_down(monitor)  # establish down state

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            caplog.clear()
            for _ in range(_DOWN_GET_HEALTH_CALLS):
                result = monitor.get_health(_PROVIDER)
                assert result[_PROVIDER].status == "down", (
                    f"Expected 'down', got {result[_PROVIDER].status}"
                )

        warning_count = _count_logs(caplog, logging.WARNING)
        assert warning_count == _EXPECTED_SINGLE_WARNING, (
            f"Expected {_EXPECTED_SINGLE_WARNING} WARNING for down entry, "
            f"got {warning_count}. "
            f"Records: {[r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]}"
        )

    def test_warning_logged_again_on_down_to_degraded_transition(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Status transitions down -> degraded: exactly 1 WARNING for the new transition."""
        monitor = _reset()

        # Phase 1: drive to 'down'
        _drive_down(monitor)

        # Phase 2: inject _PARTIAL_RECOVERY_SUCCESS_COUNT successes to bring error_rate
        # below DEFAULT_DOWN_ERROR_RATE (50%), then _PARTIAL_RECOVERY_FAILURE_COUNT
        # failures to keep it above _ERROR_RATE_THRESHOLD (10%) -> "degraded".
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            caplog.clear()

            for _ in range(_PARTIAL_RECOVERY_SUCCESS_COUNT):
                monitor.record_call(
                    _PROVIDER, latency_ms=_DEGRADED_CALL_LATENCY_MS, success=True
                )
            for _ in range(_PARTIAL_RECOVERY_FAILURE_COUNT):
                monitor.record_call(
                    _PROVIDER, latency_ms=_DEGRADED_CALL_LATENCY_MS, success=False
                )

            new_status = monitor.get_health(_PROVIDER)[_PROVIDER].status
            assert new_status == "degraded", (
                f"Expected 'degraded' after partial recovery, got '{new_status}'"
            )

        # Use health-transition-only counter: sin-bin circuit-breaker WARNINGs are
        # unrelated to the transition gate and must not inflate the count.
        warning_count = _count_health_transition_logs(caplog, logging.WARNING)
        assert warning_count == _EXPECTED_SINGLE_WARNING, (
            f"Expected {_EXPECTED_SINGLE_WARNING} WARNING for down->degraded transition, "
            f"got {warning_count}. "
            f"Records: {[r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]}"
        )

    def test_recovery_to_healthy_logs_info_and_resets(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Healthy after down: exactly 1 INFO logged; next degraded entry logs exactly 1 WARNING."""
        monitor = _reset()

        # Establish 'down'
        _drive_down(monitor)

        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            caplog.clear()
            _drive_healthy(monitor)
            result = monitor.get_health(_PROVIDER)
            assert result[_PROVIDER].status == "healthy", (
                f"Expected 'healthy' after recovery, got '{result[_PROVIDER].status}'"
            )

        info_count = _count_logs(caplog, logging.INFO)
        assert info_count == _EXPECTED_SINGLE_INFO, (
            f"Expected {_EXPECTED_SINGLE_INFO} INFO on recovery to healthy, "
            f"got {info_count}. "
            f"Records: {[r.getMessage() for r in caplog.records if r.levelno == logging.INFO]}"
        )

        # Drive degraded again — tracker was reset on healthy, must log exactly 1 WARNING
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            caplog.clear()
            _drive_degraded(monitor)
            monitor.get_health(_PROVIDER)

        warning_count_after_reset = _count_health_transition_logs(
            caplog, logging.WARNING
        )
        assert warning_count_after_reset == _EXPECTED_SINGLE_WARNING, (
            f"Expected {_EXPECTED_SINGLE_WARNING} WARNING after recovery reset, "
            f"got {warning_count_after_reset}"
        )

    def test_debug_on_repeated_same_status(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Repeated same-status calls: 0 WARNINGs, at least _REPEATED_GET_HEALTH_CALLS DEBUGs."""
        monitor = _reset()

        # Establish 'down' and consume the first transition WARNING before the
        # assertion window. record_call() uses _log_transitions=False so the
        # tracker hasn't fired yet; this get_health() call fires the first WARNING
        # and updates _last_logged_status so subsequent calls produce DEBUG.
        _drive_down(monitor)
        monitor.get_health(_PROVIDER)  # consume first "down" transition

        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            caplog.clear()
            for _ in range(_REPEATED_GET_HEALTH_CALLS):
                result = monitor.get_health(_PROVIDER)
                assert result[_PROVIDER].status == "down", (
                    f"Expected 'down', got {result[_PROVIDER].status}"
                )

        warning_count = _count_logs(caplog, logging.WARNING)
        debug_count = _count_logs(caplog, logging.DEBUG)

        assert warning_count == _EXPECTED_NO_WARNINGS, (
            f"Expected {_EXPECTED_NO_WARNINGS} WARNINGs for repeated same-status (down), "
            f"got {warning_count}. "
            f"Records: {[r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]}"
        )
        assert debug_count >= _REPEATED_GET_HEALTH_CALLS, (
            f"Expected >= {_REPEATED_GET_HEALTH_CALLS} DEBUG records for "
            f"{_REPEATED_GET_HEALTH_CALLS} same-status get_health calls, got {debug_count}"
        )
