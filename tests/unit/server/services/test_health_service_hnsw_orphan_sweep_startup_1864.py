"""
Regression tests for Bug #1864: /health has no signal for a per-node HNSW
orphan-repair sweep scheduler that failed to start.

Story #1360 (Epic #1333) ships HNSWOrphanRepairSweepScheduler as the fleet's
ONLY mechanism for repairing the pre-existing backlog of orphaned HNSW
indexes. On a real production node it silently never ran for ~2 months and
nothing anywhere flagged it: lifespan wraps construct-and-start() in one
try/except Exception whose only consequence was a WARNING log line, while
/health kept reporting the node healthy.

lifespan.py now sets, explicitly on every path:

    app.state.hnsw_orphan_repair_sweep_startup_error = None      # started OK
    app.state.hnsw_orphan_repair_sweep_startup_error = "<reason>"  # failed

The attribute being ABSENT is a third, distinct state: this process never
reached the sweep-startup block at all (e.g. a unit-test process, or a CLI
path that never runs real lifespan). It must never be folded into either of
the other two.

These tests exercise the new health_service.py surface:
  - _collect_hnsw_orphan_sweep_startup_failures() (AC1/AC2/AC7)
  - integration into _calculate_overall_status() (AC3)

The PRIMARY discriminating test is
TestHnswOrphanSweepWiredIntoOverallStatus.test_recorded_failure_reason_marks_status_degraded:
it calls the EXISTING _calculate_overall_status() with the startup-error
attribute set to a recorded failure reason, and asserts DEGRADED. Before any
implementation exists, this fails because status stays HEALTHY -- the actual
bug -- not because of a missing symbol.
"""

import contextlib
import logging
from typing import Iterator
from unittest.mock import patch

from code_indexer.server.services.health_service import HealthCheckService
from code_indexer.server.models.api_models import (
    HealthStatus,
    ServiceHealthInfo,
    SystemHealthInfo,
)

# Named constants -- no magic numbers in test bodies
NORMAL_MEMORY_PERCENT = 20.0
NORMAL_CPU_PERCENT = 20.0
NORMAL_DISK_FREE_GB = 200.0

STARTUP_ERROR_ATTR = "hnsw_orphan_repair_sweep_startup_error"
STARTUP_ERROR_PATCH_TARGET = f"code_indexer.server.app.app.state.{STARTUP_ERROR_ATTR}"
RECORDED_FAILURE_REASON = "RuntimeError: sentinel directory unwritable (Bug #1864 test)"


def _healthy_service_health() -> ServiceHealthInfo:
    return ServiceHealthInfo(
        status=HealthStatus.HEALTHY, response_time_ms=1, error_message=None
    )


def _healthy_system_info() -> SystemHealthInfo:
    return SystemHealthInfo(
        memory_usage_percent=NORMAL_MEMORY_PERCENT,
        cpu_usage_percent=NORMAL_CPU_PERCENT,
        active_jobs=0,
        disk_free_space_gb=NORMAL_DISK_FREE_GB,
        disk_read_kb_s=0.0,
        disk_write_kb_s=0.0,
        net_rx_kb_s=0.0,
        net_tx_kb_s=0.0,
    )


@contextlib.contextmanager
def _hnsw_startup_error_attribute_absent() -> Iterator[None]:
    """Guarantee app.state.hnsw_orphan_repair_sweep_startup_error is genuinely
    ABSENT for the duration of the context, restoring whatever was there
    (present or absent) afterward -- safe to use even if another test in the
    same process already set the attribute."""
    from code_indexer.server.app import app as app_module

    _unset = object()
    original = getattr(app_module.state, STARTUP_ERROR_ATTR, _unset)
    if original is not _unset:
        delattr(app_module.state, STARTUP_ERROR_ATTR)
    try:
        yield
    finally:
        if original is not _unset:
            setattr(app_module.state, STARTUP_ERROR_ATTR, original)


class TestHnswOrphanSweepWiredIntoOverallStatus:
    """_calculate_overall_status() must fold the sweep-startup collector's
    result into the overall health status -- this is the required
    discriminating RED evidence (Bug #1864)."""

    def test_recorded_failure_reason_marks_status_degraded(self):
        """PRIMARY discriminating test: a node whose sweep failed to start
        must NOT report healthy. Today (pre-implementation) this fails
        because _calculate_overall_status() never looks at the attribute at
        all and status stays HEALTHY -- a real behavioral RED, not a missing
        symbol."""
        service = HealthCheckService()

        with patch(STARTUP_ERROR_PATCH_TARGET, RECORDED_FAILURE_REASON, create=True):
            status, failure_reasons = service._calculate_overall_status(
                {"database": _healthy_service_health()},
                _healthy_system_info(),
                [],
            )

        assert status == HealthStatus.DEGRADED
        joined_reasons = " ".join(failure_reasons)
        assert RECORDED_FAILURE_REASON in joined_reasons
        # AC5: names the subsystem and says this node will not repair.
        assert "HNSW" in joined_reasons
        assert "not repair" in joined_reasons.lower()
        # AC5: points at the stats endpoint for detail.
        assert "hnsw-orphan-sweep/stats" in joined_reasons
        assert "local_scheduler" in joined_reasons
        # AC6: per-node framing, never a fleet-wide claim.
        assert "this node" in joined_reasons.lower()

    def test_explicit_none_leaves_status_unaffected(self):
        """Started cleanly -- no warning, status determined by other
        (healthy) indicators alone."""
        service = HealthCheckService()

        with patch(STARTUP_ERROR_PATCH_TARGET, None, create=True):
            status, failure_reasons = service._calculate_overall_status(
                {"database": _healthy_service_health()},
                _healthy_system_info(),
                [],
            )

        assert status == HealthStatus.HEALTHY
        assert not any("HNSW" in reason for reason in failure_reasons)

    def test_absent_attribute_does_not_crash_and_is_not_falsely_reported_started(self):
        """Attribute absent (this process never reached the sweep-startup
        lifespan block) must not raise, and must not be silently folded into
        the 'started cleanly' path -- it is fail-open (no warning) via its
        OWN distinct branch, per the agreed three-state design, not because
        it was mistaken for None."""
        service = HealthCheckService()

        with _hnsw_startup_error_attribute_absent():
            status, failure_reasons = service._calculate_overall_status(
                {"database": _healthy_service_health()},
                _healthy_system_info(),
                [],
            )

        assert status == HealthStatus.HEALTHY
        assert not any("HNSW" in reason for reason in failure_reasons)


class TestCollectHnswOrphanSweepStartupFailuresStates:
    """Unit-level tests for _collect_hnsw_orphan_sweep_startup_failures()'s
    three-state contract directly (AC1/AC2). These pin down the exact
    Tuple[bool, bool, List[str]] shape and sentinel branching. NOT the
    required discriminating RED evidence (calling a not-yet-implemented
    method raises AttributeError, which is explicitly disallowed as the
    primary RED) -- these exist to fully specify the collector's contract
    for the implementation turn."""

    def test_returns_no_warning_when_explicit_none(self):
        service = HealthCheckService()

        with patch(STARTUP_ERROR_PATCH_TARGET, None, create=True):
            has_warning, has_error, reasons = (
                service._collect_hnsw_orphan_sweep_startup_failures()
            )

        assert (has_warning, has_error, reasons) == (False, False, [])

    def test_returns_warning_with_reason_when_recorded_failure(self):
        service = HealthCheckService()

        with patch(STARTUP_ERROR_PATCH_TARGET, RECORDED_FAILURE_REASON, create=True):
            has_warning, has_error, reasons = (
                service._collect_hnsw_orphan_sweep_startup_failures()
            )

        assert has_warning is True
        assert has_error is False
        assert len(reasons) == 1
        assert RECORDED_FAILURE_REASON in reasons[0]
        assert "hnsw-orphan-sweep/stats" in reasons[0]
        assert "local_scheduler" in reasons[0]
        assert "this node" in reasons[0].lower()

    def test_returns_no_warning_when_attribute_absent(self):
        service = HealthCheckService()

        with _hnsw_startup_error_attribute_absent():
            has_warning, has_error, reasons = (
                service._collect_hnsw_orphan_sweep_startup_failures()
            )

        assert (has_warning, has_error, reasons) == (False, False, [])


class TestCollectHnswOrphanSweepStartupFailuresFailOpen:
    """AC7 fail-open contract: the absent-state branch must be genuinely
    distinct (own diagnostic DEBUG log, never reusing the 'started cleanly'
    path), and any exception resolving app.state must degrade to 'nothing to
    report' rather than propagate -- mirroring
    _resolve_golden_repos_dir()'s established broken-app.state test."""

    def test_absent_attribute_logs_diagnostic_debug_not_treated_as_started(
        self, caplog
    ):
        service = HealthCheckService()

        with _hnsw_startup_error_attribute_absent():
            with caplog.at_level(logging.DEBUG):
                service._collect_hnsw_orphan_sweep_startup_failures()

        assert any(
            "hnsw" in record.message.lower()
            and (
                "absent" in record.message.lower()
                or "never reached" in record.message.lower()
                or "not reached" in record.message.lower()
            )
            for record in caplog.records
        ), (
            f"expected a diagnostic DEBUG log for the absent state, got: {[r.message for r in caplog.records]}"
        )

    def test_never_raises_on_unexpected_app_state_error(self):
        service = HealthCheckService()

        class _BrokenApp:
            @property
            def state(self) -> None:
                raise RuntimeError("app.state not available")

        with patch("code_indexer.server.app.app", _BrokenApp()):
            has_warning, has_error, reasons = (
                service._collect_hnsw_orphan_sweep_startup_failures()
            )

        assert (has_warning, has_error, reasons) == (False, False, [])
