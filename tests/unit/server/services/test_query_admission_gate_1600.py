"""check_query_admission() -- the query-path memory-pressure admission gate
(Story #1600).

First "reject-the-live-caller" consumer of MemoryGovernor.admission_allowed().
Unlike background_jobs.py/distributed_job_claimer.py (which defer a
background job for a later poll), an inline MCP/REST request has no polling
loop to fall back on: it must be admitted now or rejected now.

Covers all branches of check_query_admission():
  - fail-open when no governor exists (CLI/solo/pre-init)
  - allow when governor.admission_allowed() is True
  - deny when governor.admission_allowed() is False (counter increments,
    retry_after_seconds derived from last_red_min_dwell_seconds)
  - fail-open on any exception raised while consulting the governor (WARNING
    logged, matching background_jobs.py's control-flow structure but at a
    louder level -- an intentional deviation, not a level-match)

Also covers the two response-shaping helpers:
  - memory_pressure_mcp_payload(): the MCP rejection envelope body
  - raise_memory_pressure_http_error(): the REST 503 + Retry-After translation
"""

from __future__ import annotations

import logging
import math
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Named constants (no magic numbers in assertions)
# ---------------------------------------------------------------------------

_MAX_USED_PCT_WATERMARK = 80.0
_RED_MIN_DWELL_SECONDS_FRACTIONAL = 31.4
_EXPECTED_RETRY_AFTER_SECONDS = 32  # math.ceil(31.4)
_ZERO_DENIALS = 0
_ONE_DENIAL = 1

_GOVERNOR_PATCH_TARGET = (
    "code_indexer.server.services.memory_governor.get_memory_governor"
)
_CONFIG_SERVICE_PATCH_TARGET = (
    "code_indexer.server.services.config_service.get_config_service"
)


@contextmanager
def _wired(governor, config_service):
    """Patch both lazy-import seams check_query_admission() consults."""
    with (
        patch(_GOVERNOR_PATCH_TARGET, return_value=governor),
        patch(_CONFIG_SERVICE_PATCH_TARGET, return_value=config_service),
    ):
        yield


@pytest.fixture()
def admitting_governor():
    """A mock governor that admits every request."""
    governor = MagicMock()
    governor.admission_allowed.return_value = True
    return governor


@pytest.fixture()
def denying_governor():
    """A mock governor that denies every request, with a known dwell value."""
    governor = MagicMock()
    governor.admission_allowed.return_value = False
    governor.last_red_min_dwell_seconds = _RED_MIN_DWELL_SECONDS_FRACTIONAL
    return governor


@pytest.fixture()
def config_service():
    """A mock config_service whose background_jobs_config carries the
    watermark check_query_admission() reads."""
    cfg = MagicMock()
    cfg.job_admission_memory_max_used_pct = _MAX_USED_PCT_WATERMARK
    service = MagicMock()
    service.get_config.return_value.background_jobs_config = cfg
    return service


class TestCheckQueryAdmissionFailOpenNoGovernor:
    def test_allows_and_does_not_raise_when_no_governor_installed(self, config_service):
        """Scenario 5: CLI/solo/pre-init -- fail open, no exception."""
        from code_indexer.server.services.query_admission_gate import (
            check_query_admission,
        )

        with _wired(governor=None, config_service=config_service):
            decision = check_query_admission()

        assert decision.allowed is True
        assert decision.retry_after_seconds is None


class TestCheckQueryAdmissionAllow:
    def test_allows_when_governor_admits(self, admitting_governor, config_service):
        from code_indexer.server.services.query_admission_gate import (
            check_query_admission,
        )

        with _wired(governor=admitting_governor, config_service=config_service):
            decision = check_query_admission()

        assert decision.allowed is True
        assert decision.retry_after_seconds is None
        admitting_governor.admission_allowed.assert_called_once_with(
            _MAX_USED_PCT_WATERMARK
        )
        admitting_governor.increment_query_admissions_denied.assert_not_called()


class TestCheckQueryAdmissionDeny:
    def test_denies_with_ceiled_retry_after_and_increments_counter_once(
        self, denying_governor, config_service
    ):
        from code_indexer.server.services.query_admission_gate import (
            check_query_admission,
        )

        with _wired(governor=denying_governor, config_service=config_service):
            decision = check_query_admission()

        assert decision.allowed is False
        assert decision.retry_after_seconds == _EXPECTED_RETRY_AFTER_SECONDS
        assert decision.retry_after_seconds == math.ceil(
            _RED_MIN_DWELL_SECONDS_FRACTIONAL
        )
        denying_governor.increment_query_admissions_denied.assert_called_once_with()

    def test_denial_against_real_governor_increments_real_counter(self, config_service):
        """End-to-end against the real MemoryGovernor (not a mock) so the
        _counters_lock-protected increment path is exercised for real."""
        from code_indexer.server.services.memory_governor import MemoryGovernor
        from code_indexer.server.services.query_admission_gate import (
            check_query_admission,
        )
        from tests.unit.server.services.test_memory_governor_fixtures import (
            CGROUP_LIMIT_4GB,
            FakeMemoryReaders,
            make_gov,
        )

        # 95% used -> RED band -> admission_allowed() is False regardless
        # of watermark.
        used_bytes = int(CGROUP_LIMIT_4GB * 0.95)
        readers = FakeMemoryReaders(
            cgroup_v2_max=str(CGROUP_LIMIT_4GB), cgroup_v2_current=str(used_bytes)
        )
        real_governor: MemoryGovernor = make_gov(readers, MemoryGovernor)
        real_governor._tick()
        assert real_governor.counters.query_admissions_denied == _ZERO_DENIALS

        with _wired(governor=real_governor, config_service=config_service):
            decision = check_query_admission()

        assert decision.allowed is False
        assert real_governor.counters.query_admissions_denied == _ONE_DENIAL


class TestCheckQueryAdmissionKillSwitch:
    def test_allows_when_gate_disabled_even_though_real_governor_would_deny(self):
        """B1: job_admission_memory_gate_enabled=False must short-circuit to
        allow=True BEFORE the governor is consulted at all, mirroring
        background_jobs.py's _admission_blocked() kill-switch check.

        Uses a real (not mocked) MemoryGovernor, pre-first-tick, which is
        the fail-safe RED state -- admission_allowed() would return False
        and, absent the fix, increment_query_admissions_denied() would fire.
        With the gate disabled, neither must happen.
        """
        from code_indexer.server.services.memory_governor import MemoryGovernor
        from code_indexer.server.services.query_admission_gate import (
            check_query_admission,
        )
        from tests.unit.server.services.test_memory_governor_fixtures import (
            CGROUP_LIMIT_4GB,
            FakeMemoryReaders,
            make_gov,
        )

        readers = FakeMemoryReaders(
            cgroup_v2_max=str(CGROUP_LIMIT_4GB), cgroup_v2_current="0"
        )
        real_governor: MemoryGovernor = make_gov(readers, MemoryGovernor)
        assert real_governor.admission_allowed(_MAX_USED_PCT_WATERMARK) is False
        assert real_governor.counters.query_admissions_denied == _ZERO_DENIALS

        cfg = MagicMock()
        cfg.job_admission_memory_max_used_pct = _MAX_USED_PCT_WATERMARK
        cfg.job_admission_memory_gate_enabled = False
        disabled_config_service = MagicMock()
        disabled_config_service.get_config.return_value.background_jobs_config = cfg

        with _wired(governor=real_governor, config_service=disabled_config_service):
            decision = check_query_admission()

        assert decision.allowed is True
        assert decision.retry_after_seconds is None
        assert real_governor.counters.query_admissions_denied == _ZERO_DENIALS


class TestCheckQueryAdmissionFailOpenOnException:
    def test_allows_and_logs_warning_when_config_service_raises(
        self, admitting_governor, caplog
    ):
        from code_indexer.server.services.query_admission_gate import (
            check_query_admission,
        )

        broken_config_service = MagicMock()
        broken_config_service.get_config.side_effect = RuntimeError("boom")

        with (
            _wired(governor=admitting_governor, config_service=broken_config_service),
            caplog.at_level(logging.WARNING),
        ):
            decision = check_query_admission()

        assert decision.allowed is True
        assert decision.retry_after_seconds is None
        assert any(record.levelno == logging.WARNING for record in caplog.records), (
            "a WARNING must be logged on the fail-open exception path"
        )

    def test_allows_and_does_not_raise_when_governor_raises(
        self, config_service, caplog
    ):
        """Scenario 6: fail-open on governor error, no exception propagates."""
        from code_indexer.server.services.query_admission_gate import (
            check_query_admission,
        )

        broken_governor = MagicMock()
        broken_governor.admission_allowed.side_effect = RuntimeError(
            "simulated governor bug"
        )

        with (
            _wired(governor=broken_governor, config_service=config_service),
            caplog.at_level(logging.WARNING),
        ):
            decision = check_query_admission()  # must not raise

        assert decision.allowed is True
        assert decision.retry_after_seconds is None
        assert any(record.levelno == logging.WARNING for record in caplog.records)


class TestMemoryPressureMcpPayload:
    def test_builds_expected_envelope_body(self):
        from code_indexer.server.services.query_admission_gate import (
            AdmissionDecision,
            MEMORY_PRESSURE_ERROR_CODE,
            memory_pressure_mcp_payload,
        )

        decision = AdmissionDecision(
            allowed=False, retry_after_seconds=_EXPECTED_RETRY_AFTER_SECONDS
        )
        payload = memory_pressure_mcp_payload(decision)

        assert payload["success"] is False
        assert payload["error_code"] == MEMORY_PRESSURE_ERROR_CODE
        assert payload["error_code"] == "memory_pressure"
        assert isinstance(payload["error"], str) and payload["error"]
        assert payload["retry_after_seconds"] == _EXPECTED_RETRY_AFTER_SECONDS


class TestRaiseMemoryPressureHttpError:
    def test_raises_http_503_with_retry_after_header(self):
        from fastapi import HTTPException

        from code_indexer.server.services.query_admission_gate import (
            AdmissionDecision,
            raise_memory_pressure_http_error,
        )

        decision = AdmissionDecision(
            allowed=False, retry_after_seconds=_EXPECTED_RETRY_AFTER_SECONDS
        )

        with pytest.raises(HTTPException) as exc_info:
            raise_memory_pressure_http_error(decision)

        assert exc_info.value.status_code == 503
        assert exc_info.value.headers is not None
        assert exc_info.value.headers["Retry-After"] == str(
            _EXPECTED_RETRY_AFTER_SECONDS
        )
        assert exc_info.value.detail["error_code"] == "memory_pressure"  # type: ignore[index]  # HTTPException.detail is str|Any; dict access is safe here
