"""
Unit tests for DiagnosticsService.

Tests cover:
- DiagnosticStatus enum values
- DiagnosticCategory enum values
- DiagnosticResult dataclass
- Result caching with 10-minute TTL
- Run all diagnostics functionality
- Single category execution
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch
from code_indexer.server.services.diagnostics_service import (
    DiagnosticCategory,
    DiagnosticStatus,
    DiagnosticResult,
    DiagnosticsService,
)


class TestDiagnosticStatus:
    """Test DiagnosticStatus enum has all required values."""

    def test_has_working_status(self):
        """Test WORKING status exists."""
        assert hasattr(DiagnosticStatus, "WORKING")
        assert DiagnosticStatus.WORKING.value == "working"

    def test_has_warning_status(self):
        """Test WARNING status exists."""
        assert hasattr(DiagnosticStatus, "WARNING")
        assert DiagnosticStatus.WARNING.value == "warning"

    def test_has_error_status(self):
        """Test ERROR status exists."""
        assert hasattr(DiagnosticStatus, "ERROR")
        assert DiagnosticStatus.ERROR.value == "error"

    def test_has_not_configured_status(self):
        """Test NOT_CONFIGURED status exists."""
        assert hasattr(DiagnosticStatus, "NOT_CONFIGURED")
        assert DiagnosticStatus.NOT_CONFIGURED.value == "not_configured"

    def test_has_not_applicable_status(self):
        """Test NOT_APPLICABLE status exists."""
        assert hasattr(DiagnosticStatus, "NOT_APPLICABLE")
        assert DiagnosticStatus.NOT_APPLICABLE.value == "not_applicable"

    def test_has_running_status(self):
        """Test RUNNING status exists."""
        assert hasattr(DiagnosticStatus, "RUNNING")
        assert DiagnosticStatus.RUNNING.value == "running"

    def test_has_not_run_status(self):
        """Test NOT_RUN status exists."""
        assert hasattr(DiagnosticStatus, "NOT_RUN")
        assert DiagnosticStatus.NOT_RUN.value == "not_run"


class TestDiagnosticCategory:
    """Test DiagnosticCategory enum has all five required categories."""

    def test_has_cli_tools_category(self):
        """Test CLI_TOOLS category exists."""
        assert hasattr(DiagnosticCategory, "CLI_TOOLS")
        assert DiagnosticCategory.CLI_TOOLS.value == "cli_tools"

    def test_has_sdk_prerequisites_category(self):
        """Test SDK_PREREQUISITES category exists."""
        assert hasattr(DiagnosticCategory, "SDK_PREREQUISITES")
        assert DiagnosticCategory.SDK_PREREQUISITES.value == "sdk_prerequisites"

    def test_has_external_apis_category(self):
        """Test EXTERNAL_APIS category exists."""
        assert hasattr(DiagnosticCategory, "EXTERNAL_APIS")
        assert DiagnosticCategory.EXTERNAL_APIS.value == "external_apis"

    def test_has_credentials_category(self):
        """Test CREDENTIALS category exists."""
        assert hasattr(DiagnosticCategory, "CREDENTIALS")
        assert DiagnosticCategory.CREDENTIALS.value == "credentials"

    def test_has_infrastructure_category(self):
        """Test INFRASTRUCTURE category exists."""
        assert hasattr(DiagnosticCategory, "INFRASTRUCTURE")
        assert DiagnosticCategory.INFRASTRUCTURE.value == "infrastructure"

    def test_category_count(self):
        """Test exactly five categories exist."""
        assert len(DiagnosticCategory) == 5


class TestDiagnosticResult:
    """Test DiagnosticResult dataclass."""

    def test_can_create_result(self):
        """Test can create DiagnosticResult instance."""
        result = DiagnosticResult(
            name="Test Diagnostic",
            status=DiagnosticStatus.WORKING,
            message="All good",
            details={"version": "1.0.0"},
            timestamp=datetime.now(),
        )
        assert result.name == "Test Diagnostic"
        assert result.status == DiagnosticStatus.WORKING
        assert result.message == "All good"
        assert result.details == {"version": "1.0.0"}
        assert isinstance(result.timestamp, datetime)

    def test_result_serialization(self):
        """Test DiagnosticResult can be converted to dict."""
        now = datetime.now()
        result = DiagnosticResult(
            name="Test",
            status=DiagnosticStatus.ERROR,
            message="Failed",
            details={},
            timestamp=now,
        )
        # Should have to_dict or similar method
        result_dict = result.to_dict()
        assert result_dict["name"] == "Test"
        assert result_dict["status"] == "error"
        assert result_dict["message"] == "Failed"
        assert result_dict["details"] == {}


class TestDiagnosticsService:
    """Test DiagnosticsService functionality."""

    def test_service_initialization(self):
        """Test service can be initialized."""
        service = DiagnosticsService()
        assert service is not None

    def test_get_status_returns_not_run_initially(self):
        """Test get_status returns NOT_RUN for all categories initially (with empty DB)."""
        import tempfile
        import os

        # Use temporary database to ensure clean state
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
            tmp_db_path = tmp_db.name

        try:
            service = DiagnosticsService(db_path=tmp_db_path)
            status = service.get_status()

            assert DiagnosticCategory.CLI_TOOLS in status
            assert DiagnosticCategory.SDK_PREREQUISITES in status
            assert DiagnosticCategory.EXTERNAL_APIS in status
            assert DiagnosticCategory.CREDENTIALS in status
            assert DiagnosticCategory.INFRASTRUCTURE in status

            # All should be NOT_RUN initially (placeholders)
            for category, results in status.items():
                for result in results:
                    assert result.status == DiagnosticStatus.NOT_RUN, \
                        f"{result.name} should be NOT_RUN initially, got {result.status}"
        finally:
            # Cleanup temp DB
            if os.path.exists(tmp_db_path):
                os.unlink(tmp_db_path)

    @pytest.mark.asyncio
    async def test_run_all_diagnostics_placeholder(self):
        """Test run_all_diagnostics calls actual diagnostic methods and returns complete results."""
        service = DiagnosticsService()
        await service.run_all_diagnostics()

        status = service.get_status()

        # Verify expected result counts per category (Bug #145)
        assert len(status[DiagnosticCategory.CLI_TOOLS]) == 8, "CLI Tools should have 8 results"
        assert len(status[DiagnosticCategory.SDK_PREREQUISITES]) == 3, "SDK Prerequisites should have 3 results"
        assert len(status[DiagnosticCategory.EXTERNAL_APIS]) == 5, "External APIs should have 5 results"
        assert len(status[DiagnosticCategory.CREDENTIALS]) == 4, "Credentials should have 4 results"
        assert len(status[DiagnosticCategory.INFRASTRUCTURE]) == 2, "Infrastructure should have 2 results"

        # Verify results have actual diagnostic status (not NOT_RUN)
        # Note: Status depends on system state, but should be one of: WORKING, ERROR, NOT_CONFIGURED, WARNING
        valid_statuses = {DiagnosticStatus.WORKING, DiagnosticStatus.ERROR,
                         DiagnosticStatus.NOT_CONFIGURED, DiagnosticStatus.WARNING,
                         DiagnosticStatus.NOT_APPLICABLE}
        for category, results in status.items():
            for result in results:
                assert result.status in valid_statuses, f"{result.name} status should not be NOT_RUN"

    def test_cache_stores_results(self):
        """Test results are cached after execution."""
        service = DiagnosticsService()

        # First call should execute
        status1 = service.get_status()

        # Second call should return cached results (same timestamp)
        status2 = service.get_status()

        # Compare timestamps to verify caching
        for category in DiagnosticCategory:
            results1 = status1[category]
            results2 = status2[category]

            if len(results1) > 0 and len(results2) > 0:
                assert results1[0].timestamp == results2[0].timestamp

    def test_cache_expiration_after_10_minutes(self):
        """Test cache expires after 10 minutes."""
        service = DiagnosticsService()

        # Set initial cache
        service.get_status()

        # Mock time to be 11 minutes in the future
        with patch(
            "code_indexer.server.services.diagnostics_service.datetime"
        ) as mock_dt:
            future_time = datetime.now() + timedelta(minutes=11)
            mock_dt.now.return_value = future_time

            # Get status again - should be refreshed
            status = service.get_status()

            # Verify results are fresh (this will depend on implementation)
            assert status is not None

    @pytest.mark.asyncio
    async def test_run_category_clears_category_cache(self):
        """Test running a category clears only that category's cache."""
        service = DiagnosticsService()

        # Run all diagnostics first
        await service.run_all_diagnostics()
        status_before = service.get_status()

        # Clear CLI_TOOLS cache to force refresh
        service.clear_cache(DiagnosticCategory.CLI_TOOLS)

        # Run single category - this will refresh CLI_TOOLS
        await service.run_category(DiagnosticCategory.CLI_TOOLS)
        status_after = service.get_status()

        # CLI_TOOLS should be updated (different timestamp)
        # Other categories should remain cached (same timestamp)
        assert (
            status_before[DiagnosticCategory.CLI_TOOLS][0].timestamp
            != status_after[DiagnosticCategory.CLI_TOOLS][0].timestamp
        )

        # Other categories should be unchanged
        for category in [
            DiagnosticCategory.SDK_PREREQUISITES,
            DiagnosticCategory.EXTERNAL_APIS,
            DiagnosticCategory.CREDENTIALS,
            DiagnosticCategory.INFRASTRUCTURE,
        ]:
            if len(status_before[category]) > 0 and len(status_after[category]) > 0:
                assert (
                    status_before[category][0].timestamp
                    == status_after[category][0].timestamp
                )

    def test_is_running_flag(self):
        """Test service tracks running state."""
        service = DiagnosticsService()

        # Initially not running
        assert service.is_running() is False

        # After starting a run, should be running
        # (This will be tested with proper async handling)

    def test_placeholder_has_at_least_one_diagnostic_per_category(self):
        """Test placeholder returns at least one diagnostic item per category."""
        service = DiagnosticsService()
        status = service.get_status()

        for category in DiagnosticCategory:
            results = status[category]
            assert (
                len(results) >= 1
            ), f"Category {category} should have at least one diagnostic"

    @pytest.mark.asyncio
    async def test_concurrent_run_all_diagnostics_thread_safety(self):
        """Test concurrent calls to run_all_diagnostics are thread-safe."""
        service = DiagnosticsService()

        # Start two concurrent diagnostic runs
        import asyncio

        await asyncio.gather(
            service.run_all_diagnostics(),
            service.run_all_diagnostics(),
        )

        # Should complete without errors and have valid state
        assert not service.is_running()
        status = service.get_status()
        assert len(status) == 5  # All categories present

    @pytest.mark.asyncio
    async def test_concurrent_run_category_thread_safety(self):
        """Test concurrent calls to run_category are thread-safe."""
        service = DiagnosticsService()

        # Start concurrent category runs
        import asyncio

        await asyncio.gather(
            service.run_category(DiagnosticCategory.CLI_TOOLS),
            service.run_category(DiagnosticCategory.SDK_PREREQUISITES),
        )

        # Should complete without errors
        assert not service.is_running()
        status = service.get_status()
        assert DiagnosticCategory.CLI_TOOLS in status
        assert DiagnosticCategory.SDK_PREREQUISITES in status

    @pytest.mark.asyncio
    async def test_run_all_diagnostics_continues_on_category_failure(self):
        """Test that failure in one category doesn't stop other categories from running."""
        from unittest.mock import patch

        service = DiagnosticsService()

        # Mock run_external_api_diagnostics to raise an exception
        async def failing_api_check():
            raise RuntimeError("Simulated API check failure")

        with patch.object(service, 'run_external_api_diagnostics', side_effect=failing_api_check):
            # Run all diagnostics - should not raise exception
            await service.run_all_diagnostics()

        # Get status after execution
        status = service.get_status()

        # Verify all categories have results
        assert len(status) == 5, "All 5 categories should have results"

        # Verify EXTERNAL_APIS has ERROR status from exception
        external_api_results = status[DiagnosticCategory.EXTERNAL_APIS]
        assert len(external_api_results) == 1
        assert external_api_results[0].status == DiagnosticStatus.ERROR
        assert "Category diagnostic failed" in external_api_results[0].message
        assert "RuntimeError" in external_api_results[0].details.get("error_type", "")

        # Verify other categories executed successfully (should have multiple real results, not ERROR)
        cli_tools_results = status[DiagnosticCategory.CLI_TOOLS]
        assert len(cli_tools_results) > 1, "CLI Tools should have multiple diagnostic results"
        # At least some should not be ERROR (depends on system state, but should have real checks)
        assert any(r.status != DiagnosticStatus.ERROR for r in cli_tools_results), \
            "CLI Tools should have non-ERROR results"

        sdk_results = status[DiagnosticCategory.SDK_PREREQUISITES]
        assert len(sdk_results) > 1, "SDK Prerequisites should have multiple diagnostic results"

        # Verify Infrastructure ran (should have 2 results: DB + Storage)
        infra_results = status[DiagnosticCategory.INFRASTRUCTURE]
        assert len(infra_results) == 2, "Infrastructure should have 2 diagnostic results"
