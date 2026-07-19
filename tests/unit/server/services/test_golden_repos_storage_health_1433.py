"""
Regression tests for Bug #1433: /health has no probe of golden-repo storage
readability.

During a real staging incident, a node whose CoW/NFS storage host was down
kept reporting /health -> healthy (DB connectivity was fine, the generic
storage check only looks at root-volume disk-space %) while every real
query failed with "[Errno 5] Input/output error" reading
.cidx-server/data/golden-repos. HAProxy kept routing traffic to it.

These tests exercise the new bounded-timeout golden-repos-directory
readability probe on HealthCheckService:
  - _resolve_golden_repos_dir()
  - _probe_golden_repos_dir_readable()
  - _collect_golden_repos_storage_failures()
  - integration into _calculate_overall_status()
"""

import os
import stat
import time
from unittest.mock import patch

from code_indexer.server.services import health_service
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
SHORT_TIMEOUT_SECONDS = 1
SHORT_GRACE_SECONDS = 1
# Generous upper bound for the hang test's wall-clock assertion: the probe
# must return within timeout+grace, plus slack for process scheduling.
HANG_TEST_MAX_ELAPSED_SECONDS = 6.0


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


class TestResolveGoldenReposDir:
    """_resolve_golden_repos_dir() must never raise and must skip cleanly
    when app.state.golden_repos_dir is not yet configured."""

    def test_returns_none_when_not_configured(self):
        service = HealthCheckService()

        with patch(
            "code_indexer.server.app.app.state.golden_repos_dir",
            None,
            create=True,
        ):
            result = service._resolve_golden_repos_dir()

        assert result is None

    def test_returns_configured_path(self, tmp_path):
        service = HealthCheckService()
        configured_path = str(tmp_path)

        with patch(
            "code_indexer.server.app.app.state.golden_repos_dir",
            configured_path,
            create=True,
        ):
            result = service._resolve_golden_repos_dir()

        assert result == configured_path

    def test_never_raises_on_unexpected_error(self):
        service = HealthCheckService()

        class _BrokenApp:
            """Stand-in for the app module's `app` object whose `.state`
            access raises -- proves _resolve_golden_repos_dir() fails open
            (returns None) instead of propagating, mirroring
            _check_storage_health()'s RuntimeError-fallback pattern."""

            @property
            def state(self) -> None:
                raise RuntimeError("app.state not available")

        with patch("code_indexer.server.app.app", _BrokenApp()):
            result = service._resolve_golden_repos_dir()

        assert result is None


class TestProbeGoldenReposDirReadable:
    """_probe_golden_repos_dir_readable() bounded-timeout readability probe."""

    def test_readable_directory_is_healthy(self, tmp_path):
        service = HealthCheckService()

        is_readable, error_message = service._probe_golden_repos_dir_readable(
            str(tmp_path)
        )

        assert is_readable is True
        assert error_message is None

    def test_nonexistent_path_is_unhealthy_with_reason(self, tmp_path):
        service = HealthCheckService()
        missing_path = str(tmp_path / "does-not-exist")

        is_readable, error_message = service._probe_golden_repos_dir_readable(
            missing_path
        )

        assert is_readable is False
        assert error_message is not None
        assert missing_path in error_message

    def test_mocked_timeout_expired_is_unhealthy_with_reason(self, tmp_path):
        """Defense-in-depth: if the outer `timeout` binary itself is
        unavailable/broken, Python's own subprocess.run(timeout=...) must
        still bound the call. Simulated via TimeoutExpired since a real
        unkillable D-state syscall cannot be constructed in a unit test.
        """
        service = HealthCheckService()

        import subprocess

        with patch(
            "code_indexer.server.services.health_service.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ls", timeout=1),
        ):
            start = time.time()
            is_readable, error_message = service._probe_golden_repos_dir_readable(
                str(tmp_path)
            )
            elapsed = time.time() - start

        assert is_readable is False
        assert error_message is not None
        assert elapsed < HANG_TEST_MAX_ELAPSED_SECONDS

    def test_genuinely_hung_probe_is_bounded_and_reports_failure(self, tmp_path):
        """Real (non-mocked) bounded-timeout proof: a fake `ls` binary that
        sleeps far longer than the configured timeout is shadowed onto PATH.
        A live hung NFS mount can't be constructed in a unit test, so this
        controlled double stands in for it -- the health-service probe logic
        itself is exercised for real, unmocked.
        """
        fake_bin_dir = tmp_path / "fakebin"
        fake_bin_dir.mkdir()
        fake_ls = fake_bin_dir / "ls"
        fake_ls.write_text("#!/bin/sh\nsleep 30\n")
        fake_ls.chmod(fake_ls.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)

        target_dir = tmp_path / "golden-repos"
        target_dir.mkdir()

        service = HealthCheckService()

        original_path = os.environ.get("PATH", "")
        patched_path = f"{fake_bin_dir}:{original_path}"

        with (
            patch.dict(os.environ, {"PATH": patched_path}),
            patch.object(
                health_service,
                "GOLDEN_REPOS_HEALTH_TIMEOUT_SECONDS",
                SHORT_TIMEOUT_SECONDS,
            ),
            patch.object(
                health_service,
                "GOLDEN_REPOS_HEALTH_SUBPROCESS_GRACE_SECONDS",
                SHORT_GRACE_SECONDS,
            ),
        ):
            start = time.time()
            is_readable, error_message = service._probe_golden_repos_dir_readable(
                str(target_dir)
            )
            elapsed = time.time() - start

        assert is_readable is False
        assert error_message is not None
        assert elapsed < HANG_TEST_MAX_ELAPSED_SECONDS, (
            f"Probe took {elapsed:.1f}s -- must be bounded by "
            f"{SHORT_TIMEOUT_SECONDS + SHORT_GRACE_SECONDS}s + slack even "
            "against a fully hung filesystem call."
        )


class TestCollectGoldenReposStorageFailures:
    """_collect_golden_repos_storage_failures() -- integration of resolve + probe."""

    def test_skips_when_not_configured(self):
        service = HealthCheckService()

        with patch.object(service, "_resolve_golden_repos_dir", return_value=None):
            has_warning, has_error, reasons = (
                service._collect_golden_repos_storage_failures()
            )

        assert (has_warning, has_error, reasons) == (False, False, [])

    def test_reports_error_when_unreadable(self, tmp_path):
        service = HealthCheckService()
        missing_path = str(tmp_path / "gone")

        with patch.object(
            service, "_resolve_golden_repos_dir", return_value=missing_path
        ):
            has_warning, has_error, reasons = (
                service._collect_golden_repos_storage_failures()
            )

        assert has_error is True
        assert has_warning is False
        assert len(reasons) == 1
        assert "Golden repos storage" in reasons[0]

    def test_no_failure_when_readable(self, tmp_path):
        service = HealthCheckService()

        with patch.object(
            service, "_resolve_golden_repos_dir", return_value=str(tmp_path)
        ):
            has_warning, has_error, reasons = (
                service._collect_golden_repos_storage_failures()
            )

        assert (has_warning, has_error, reasons) == (False, False, [])


class TestOverallStatusIntegration:
    """_calculate_overall_status() must fold the golden-repos storage probe
    into DEGRADED/UNHEALTHY, matching the existing severity convention."""

    def test_unreadable_golden_repos_storage_downgrades_to_unhealthy(self):
        service = HealthCheckService()
        services = {
            "database": _healthy_service_health(),
            "storage": _healthy_service_health(),
        }
        system_info = _healthy_system_info()

        with patch.object(
            service,
            "_collect_golden_repos_storage_failures",
            return_value=(
                False,
                True,
                ["Golden repos storage: golden-repos directory not readable"],
            ),
        ):
            status, failure_reasons = service._calculate_overall_status(
                services, system_info, []
            )

        assert status == HealthStatus.UNHEALTHY
        assert any("Golden repos storage" in reason for reason in failure_reasons)

    def test_readable_golden_repos_storage_stays_healthy(self):
        service = HealthCheckService()
        services = {
            "database": _healthy_service_health(),
            "storage": _healthy_service_health(),
        }
        system_info = _healthy_system_info()

        with patch.object(
            service,
            "_collect_golden_repos_storage_failures",
            return_value=(False, False, []),
        ):
            status, failure_reasons = service._calculate_overall_status(
                services, system_info, []
            )

        assert status == HealthStatus.HEALTHY
        assert failure_reasons == []
