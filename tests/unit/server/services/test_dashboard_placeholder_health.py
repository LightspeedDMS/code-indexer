"""
Unit tests for dashboard service placeholder health fix (Bug #266).

PROBLEM: dashboard_service.get_dashboard_data() calls _get_health_data() synchronously
which runs 40 uncached SQLite operations. When background jobs hold DB locks, this blocks
for 55-165+ seconds, freezing the server (--workers 1) including the login redirect.

FIX: get_dashboard_data() should return a placeholder HealthCheckResponse instead of
calling _get_health_data(). The HTMX partial at /admin/partials/dashboard-health handles
lazy loading via get_health_partial() which still calls _get_health_data() for real data.

ACCEPTANCE CRITERIA:
1. get_dashboard_data() returns a placeholder HealthCheckResponse (not real health check)
2. Placeholder has status "loading" or appropriate default, with valid default values
3. get_health_partial() continues to call _get_health_data() for real data
4. Initial dashboard page load does not call _get_health_data() at all
5. Template receives a valid HealthCheckResponse object (renders without errors)
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call


class TestDashboardPlaceholderHealth:
    """Tests for Bug #266: dashboard placeholder health avoids blocking health checks."""

    def test_get_dashboard_data_does_not_call_get_health_data(self):
        """
        AC1/AC4: get_dashboard_data() must NOT call _get_health_data() at all.

        The entire point of the fix is to avoid the blocking SQLite operations
        that _get_health_data() triggers. We verify it is never invoked during
        get_dashboard_data().
        """
        from src.code_indexer.server.services.dashboard_service import DashboardService

        service = DashboardService()

        with (
            patch.object(service, "_get_health_data") as mock_health,
            patch.object(service, "_get_job_counts", return_value=MagicMock()),
            patch.object(service, "_get_repo_counts", return_value=MagicMock()),
            patch.object(service, "_get_recent_jobs", return_value=[]),
        ):
            service.get_dashboard_data("admin", "admin")

            # CRITICAL: _get_health_data must NOT be called during initial page load
            mock_health.assert_not_called()

    def test_get_dashboard_data_returns_valid_health_check_response(self):
        """
        AC5: Template must receive a valid HealthCheckResponse so it renders without errors.

        The placeholder must be a proper HealthCheckResponse instance with all required
        fields populated with valid defaults.
        """
        from src.code_indexer.server.services.dashboard_service import DashboardService
        from src.code_indexer.server.models.api_models import HealthCheckResponse

        service = DashboardService()

        with (
            patch.object(service, "_get_job_counts", return_value=MagicMock()),
            patch.object(service, "_get_repo_counts", return_value=MagicMock()),
            patch.object(service, "_get_recent_jobs", return_value=[]),
        ):
            result = service.get_dashboard_data("admin", "admin")

            # Must be a valid HealthCheckResponse instance
            assert isinstance(result.health, HealthCheckResponse), (
                f"Expected HealthCheckResponse, got {type(result.health)}"
            )

    def test_placeholder_health_has_valid_status(self):
        """
        AC2: Placeholder must have a valid status field.

        The placeholder should use HealthStatus enum value. "loading" is not a valid
        HealthStatus enum value — the placeholder should use DEGRADED or a similar
        valid enum value that signals the health is not yet loaded.
        """
        from src.code_indexer.server.services.dashboard_service import DashboardService
        from src.code_indexer.server.models.api_models import HealthStatus

        service = DashboardService()

        with (
            patch.object(service, "_get_job_counts", return_value=MagicMock()),
            patch.object(service, "_get_repo_counts", return_value=MagicMock()),
            patch.object(service, "_get_recent_jobs", return_value=[]),
        ):
            result = service.get_dashboard_data("admin", "admin")

            # Status must be a valid HealthStatus enum member
            assert isinstance(result.health.status, HealthStatus), (
                f"Expected HealthStatus enum, got {type(result.health.status)}: "
                f"{result.health.status}"
            )

    def test_placeholder_health_has_valid_timestamp(self):
        """
        AC2: Placeholder must have a valid datetime timestamp.

        The HealthCheckResponse model requires a timestamp field.
        """
        from src.code_indexer.server.services.dashboard_service import DashboardService

        service = DashboardService()

        with (
            patch.object(service, "_get_job_counts", return_value=MagicMock()),
            patch.object(service, "_get_repo_counts", return_value=MagicMock()),
            patch.object(service, "_get_recent_jobs", return_value=[]),
        ):
            result = service.get_dashboard_data("admin", "admin")

            # Must have a valid datetime timestamp
            assert isinstance(result.health.timestamp, datetime), (
                f"Expected datetime, got {type(result.health.timestamp)}"
            )

    def test_placeholder_health_has_valid_services_dict(self):
        """
        AC2/AC5: Placeholder must have a valid services dict for template rendering.

        The HealthCheckResponse model requires a services field (dict of ServiceHealthInfo).
        """
        from src.code_indexer.server.services.dashboard_service import DashboardService

        service = DashboardService()

        with (
            patch.object(service, "_get_job_counts", return_value=MagicMock()),
            patch.object(service, "_get_repo_counts", return_value=MagicMock()),
            patch.object(service, "_get_recent_jobs", return_value=[]),
        ):
            result = service.get_dashboard_data("admin", "admin")

            # Must have a valid dict for services
            assert isinstance(result.health.services, dict), (
                f"Expected dict for services, got {type(result.health.services)}"
            )

    def test_placeholder_health_has_valid_system_info(self):
        """
        AC2/AC5: Placeholder must have a valid SystemHealthInfo for template rendering.

        The HealthCheckResponse model requires a system field (SystemHealthInfo).
        """
        from src.code_indexer.server.services.dashboard_service import DashboardService
        from src.code_indexer.server.models.api_models import SystemHealthInfo

        service = DashboardService()

        with (
            patch.object(service, "_get_job_counts", return_value=MagicMock()),
            patch.object(service, "_get_repo_counts", return_value=MagicMock()),
            patch.object(service, "_get_recent_jobs", return_value=[]),
        ):
            result = service.get_dashboard_data("admin", "admin")

            # Must have a valid SystemHealthInfo
            assert isinstance(result.health.system, SystemHealthInfo), (
                f"Expected SystemHealthInfo, got {type(result.health.system)}"
            )

    def test_get_health_partial_still_calls_get_health_data(self):
        """
        AC3: get_health_partial() must STILL call _get_health_data() for real data.

        The HTMX lazy-load endpoint /admin/partials/dashboard-health calls
        get_health_partial() which must continue fetching real health data.
        This is the endpoint that does the actual health check after page load.
        """
        from src.code_indexer.server.services.dashboard_service import DashboardService
        from src.code_indexer.server.models.api_models import (
            HealthCheckResponse,
            HealthStatus,
            ServiceHealthInfo,
            SystemHealthInfo,
        )

        service = DashboardService()

        # Build a real HealthCheckResponse to return from the mock
        fake_health = HealthCheckResponse(
            status=HealthStatus.HEALTHY,
            timestamp=datetime.now(timezone.utc),
            services={
                "database": ServiceHealthInfo(
                    status=HealthStatus.HEALTHY,
                    response_time_ms=5,
                )
            },
            system=SystemHealthInfo(
                memory_usage_percent=50.0,
                cpu_usage_percent=20.0,
                active_jobs=0,
                disk_free_space_gb=100.0,
            ),
        )

        with patch.object(service, "_get_health_data", return_value=fake_health) as mock_health:
            result = service.get_health_partial()

            # _get_health_data MUST be called by get_health_partial()
            mock_health.assert_called_once()

            # Result must be the real health data
            assert result is fake_health

    def test_get_dashboard_data_returns_complete_dashboard_data(self):
        """
        Regression: get_dashboard_data() must still return complete DashboardData
        with all fields populated (health, job_counts, repo_counts, recent_jobs).

        The fix must not break the overall structure of the return value.
        """
        from src.code_indexer.server.services.dashboard_service import DashboardService
        from src.code_indexer.server.services.dashboard_service import (
            DashboardData,
            JobCounts,
            RepoCounts,
        )
        from src.code_indexer.server.models.api_models import HealthCheckResponse

        service = DashboardService()

        mock_job_counts = JobCounts(running=2, queued=1, completed_24h=5, failed_24h=0)
        mock_repo_counts = RepoCounts(golden=10, activated=3)
        mock_recent_jobs = []

        with (
            patch.object(service, "_get_job_counts", return_value=mock_job_counts),
            patch.object(service, "_get_repo_counts", return_value=mock_repo_counts),
            patch.object(service, "_get_recent_jobs", return_value=mock_recent_jobs),
        ):
            result = service.get_dashboard_data("admin", "admin")

            # Result must be a DashboardData
            assert isinstance(result, DashboardData)

            # Health must be a valid HealthCheckResponse (placeholder)
            assert isinstance(result.health, HealthCheckResponse)

            # Other fields must come through unchanged
            assert result.job_counts is mock_job_counts
            assert result.repo_counts is mock_repo_counts
            assert result.recent_jobs is mock_recent_jobs

    def test_placeholder_health_is_independent_of_database(self):
        """
        AC1/AC4: Creating the placeholder must not touch the database at all.

        We verify by patching health_service.get_system_health() and confirming
        it is never called during get_dashboard_data(), even indirectly.
        """
        from src.code_indexer.server.services.dashboard_service import DashboardService

        service = DashboardService()

        with (
            patch(
                "src.code_indexer.server.services.dashboard_service.health_service"
            ) as mock_health_svc,
            patch.object(service, "_get_job_counts", return_value=MagicMock()),
            patch.object(service, "_get_repo_counts", return_value=MagicMock()),
            patch.object(service, "_get_recent_jobs", return_value=[]),
        ):
            service.get_dashboard_data("admin", "admin")

            # The real health_service.get_system_health must NEVER be called
            mock_health_svc.get_system_health.assert_not_called()

    def test_placeholder_contains_degraded_status_to_signal_loading(self):
        """
        AC2: Placeholder should use DEGRADED status (not HEALTHY) to signal
        that health data is not yet loaded and HTMX will refresh it.

        Using DEGRADED (not HEALTHY) prevents false-positive green indicators
        on the initial page load before real health data arrives via HTMX.
        """
        from src.code_indexer.server.services.dashboard_service import DashboardService
        from src.code_indexer.server.models.api_models import HealthStatus

        service = DashboardService()

        with (
            patch.object(service, "_get_job_counts", return_value=MagicMock()),
            patch.object(service, "_get_repo_counts", return_value=MagicMock()),
            patch.object(service, "_get_recent_jobs", return_value=[]),
        ):
            result = service.get_dashboard_data("admin", "admin")

            # Placeholder must NOT be HEALTHY (would be misleading before real check)
            assert result.health.status != HealthStatus.HEALTHY, (
                "Placeholder should not show HEALTHY before real health check runs. "
                "Use DEGRADED or UNHEALTHY to signal that HTMX will load real data."
            )

    def test_get_health_partial_is_unchanged_returns_health_check_response(self):
        """
        AC3: get_health_partial() must return a HealthCheckResponse (the real one).

        This is the method used by the HTMX /admin/partials/dashboard-health endpoint.
        It must continue to work exactly as before the fix.
        """
        from src.code_indexer.server.services.dashboard_service import DashboardService
        from src.code_indexer.server.models.api_models import (
            HealthCheckResponse,
            HealthStatus,
            ServiceHealthInfo,
            SystemHealthInfo,
        )

        service = DashboardService()

        fake_health = HealthCheckResponse(
            status=HealthStatus.HEALTHY,
            timestamp=datetime.now(timezone.utc),
            services={},
            system=SystemHealthInfo(
                memory_usage_percent=30.0,
                cpu_usage_percent=10.0,
                active_jobs=0,
                disk_free_space_gb=50.0,
            ),
        )

        with patch.object(service, "_get_health_data", return_value=fake_health):
            result = service.get_health_partial()

            # Must return the HealthCheckResponse from _get_health_data
            assert isinstance(result, HealthCheckResponse)
            assert result.status == HealthStatus.HEALTHY
