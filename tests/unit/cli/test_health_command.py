"""
Unit tests for CLI health command.

Tests the health command implementation with mocked HNSWHealthService
to verify correct CLI behavior, output formatting, and exit codes.

Story #57: CLI cidx health Command
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from code_indexer.cli import cli
from code_indexer.services.hnsw_health_service import HealthCheckResult


class TestHealthCommandBasics:
    """Test basic health command functionality."""

    def test_health_command_exists(self):
        """Test that health command is registered."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "health" in result.output

    def test_health_command_help(self):
        """Test health command help text."""
        runner = CliRunner()
        result = runner.invoke(cli, ["health", "--help"])
        assert result.exit_code == 0
        assert "Check HNSW index health and integrity" in result.output
        assert "--json" in result.output
        assert "--quiet" in result.output
        assert "--index-path" in result.output


class TestHealthCommandHealthyIndex:
    """Test health command with healthy HNSW index."""

    @pytest.fixture
    def healthy_result(self):
        """Create a healthy HealthCheckResult."""
        return HealthCheckResult(
            valid=True,
            file_exists=True,
            readable=True,
            loadable=True,
            element_count=408234,
            connections_checked=12456789,
            min_inbound=8,
            max_inbound=64,
            index_path="/path/to/.code-indexer/index/hnsw.bin",
            file_size_bytes=1610612736,  # 1.6 GB
            last_modified=datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc),
            errors=[],
            check_duration_ms=638.5,
            from_cache=False,
        )

    def test_healthy_index_shows_healthy_status(self, healthy_result):
        """AC1: Health check on healthy index shows 'Index Health: HEALTHY'."""
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = healthy_result
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health"])

            assert result.exit_code == 0
            assert "HEALTHY" in result.output
            assert (
                "Index Health: HEALTHY" in result.output
                or "Status: HEALTHY" in result.output
            )

    def test_healthy_index_displays_metrics(self, healthy_result):
        """AC1: Health check displays metrics for healthy index."""
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = healthy_result
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health"])

            assert result.exit_code == 0
            # Check for key metrics in output
            assert (
                "408,234" in result.output or "408234" in result.output
            )  # element_count
            assert (
                "12,456,789" in result.output or "12456789" in result.output
            )  # connections_checked
            assert (
                "1.5 GB" in result.output or "1.6 GB" in result.output
            )  # file_size (rounding)
            assert "638" in result.output  # check_duration_ms

    def test_healthy_index_exit_code_zero(self, healthy_result):
        """AC1: Healthy index returns exit code 0."""
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = healthy_result
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health"])

            assert result.exit_code == 0


class TestHealthCommandUnhealthyIndex:
    """Test health command with unhealthy HNSW index."""

    @pytest.fixture
    def unhealthy_result(self):
        """Create an unhealthy HealthCheckResult."""
        return HealthCheckResult(
            valid=False,
            file_exists=True,
            readable=True,
            loadable=True,
            element_count=1000,
            connections_checked=5000,
            min_inbound=0,
            max_inbound=10,
            index_path="/path/to/index",
            file_size_bytes=1024000,
            last_modified=datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc),
            errors=["Element 12345 has orphaned connections"],
            check_duration_ms=45.5,
            from_cache=False,
        )

    def test_unhealthy_index_shows_unhealthy_status(self, unhealthy_result):
        """AC2: Health check on corrupted index shows 'Index Health: UNHEALTHY'."""
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = unhealthy_result
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health"])

            assert result.exit_code == 1
            assert "UNHEALTHY" in result.output
            assert (
                "Index Health: UNHEALTHY" in result.output
                or "Status: UNHEALTHY" in result.output
            )

    def test_unhealthy_index_displays_errors(self, unhealthy_result):
        """AC2: Health check displays error messages for unhealthy index."""
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = unhealthy_result
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health"])

            assert result.exit_code == 1
            assert "Element 12345 has orphaned connections" in result.output
            assert (
                "Errors Found:" in result.output or "errors:" in result.output.lower()
            )

    def test_unhealthy_index_exit_code_nonzero(self, unhealthy_result):
        """AC2: Unhealthy index returns non-zero exit code."""
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = unhealthy_result
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health"])

            assert result.exit_code == 1


class TestHealthCommandNoIndex:
    """Test health command when no index exists."""

    @pytest.fixture
    def no_index_result(self):
        """Create HealthCheckResult for non-existent index."""
        return HealthCheckResult(
            valid=False,
            file_exists=False,
            readable=False,
            loadable=False,
            index_path="/path/to/.code-indexer/index/hnsw.bin",
            errors=["Index file not found"],
            check_duration_ms=0.5,
            from_cache=False,
        )

    def test_no_index_shows_not_found_message(self, no_index_result):
        """AC3: Health check when no index exists shows 'No index found'."""
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = no_index_result
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health"])

            assert result.exit_code == 2
            assert (
                "No index found" in result.output
                or "Index file not found" in result.output
            )

    def test_no_index_suggests_indexing(self, no_index_result):
        """AC3: Health check suggests 'cidx index' when no index exists."""
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = no_index_result
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health"])

            assert result.exit_code == 2
            assert (
                "cidx index" in result.output or "code-indexer index" in result.output
            )

    def test_no_index_exit_code_two(self, no_index_result):
        """AC3: No index returns exit code 2."""
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = no_index_result
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health"])

            assert result.exit_code == 2


class TestHealthCommandNotReadable:
    """Test health command when index is not readable."""

    @pytest.fixture
    def not_readable_result(self):
        """Create HealthCheckResult for non-readable index."""
        return HealthCheckResult(
            valid=False,
            file_exists=True,
            readable=False,
            loadable=False,
            index_path="/path/to/.code-indexer/index/hnsw.bin",
            file_size_bytes=1024000,
            last_modified=datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc),
            errors=["Index file not readable (permission denied)"],
            check_duration_ms=0.5,
            from_cache=False,
        )

    def test_not_readable_index_exit_code_three(self, not_readable_result):
        """Test that non-readable index returns exit code 3."""
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = not_readable_result
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health"])

            assert result.exit_code == 3
            assert (
                "permission denied" in result.output.lower()
                or "not readable" in result.output.lower()
            )


class TestHealthCommandJsonOutput:
    """Test health command --json flag."""

    @pytest.fixture
    def healthy_result(self):
        """Create a healthy HealthCheckResult."""
        return HealthCheckResult(
            valid=True,
            file_exists=True,
            readable=True,
            loadable=True,
            element_count=408234,
            connections_checked=12456789,
            min_inbound=8,
            max_inbound=64,
            index_path="/path/to/.code-indexer/index/hnsw.bin",
            file_size_bytes=1610612736,
            last_modified=datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc),
            errors=[],
            check_duration_ms=638.5,
            from_cache=False,
        )

    def test_json_output_valid_json(self, healthy_result):
        """AC4: Health check with --json outputs valid JSON."""
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = healthy_result
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health", "--json"])

            assert result.exit_code == 0
            # Should be valid JSON
            data = json.loads(result.output)
            assert isinstance(data, dict)

    def test_json_output_matches_schema(self, healthy_result):
        """AC4: JSON output matches HealthCheckResult schema."""
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = healthy_result
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)

            # Verify required fields from HealthCheckResult
            assert "valid" in data
            assert "file_exists" in data
            assert "readable" in data
            assert "loadable" in data
            assert "index_path" in data
            assert "check_duration_ms" in data

            # Verify values
            assert data["valid"] is True
            assert data["element_count"] == 408234


class TestHealthCommandQuietFlag:
    """Test health command --quiet flag."""

    @pytest.fixture
    def healthy_result(self):
        """Create a healthy HealthCheckResult."""
        return HealthCheckResult(
            valid=True,
            file_exists=True,
            readable=True,
            loadable=True,
            element_count=408234,
            connections_checked=12456789,
            min_inbound=8,
            max_inbound=64,
            index_path="/path/to/.code-indexer/index/hnsw.bin",
            file_size_bytes=1610612736,
            last_modified=datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc),
            errors=[],
            check_duration_ms=638.5,
            from_cache=False,
        )

    def test_quiet_output_single_line(self, healthy_result):
        """AC5: Health check with --quiet shows only essential status in single line."""
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = healthy_result
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health", "--quiet"])

            assert result.exit_code == 0
            # Should be minimal output (single line or very short)
            lines = [line for line in result.output.strip().split("\n") if line.strip()]
            assert len(lines) <= 3  # Allow for minimal output

    def test_quiet_output_contains_status(self, healthy_result):
        """AC5: Quiet output contains essential status information."""
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = healthy_result
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health", "--quiet"])

            assert result.exit_code == 0
            assert "HEALTHY" in result.output or "OK" in result.output


class TestHealthCommandIndexPath:
    """Test health command --index-path option."""

    @pytest.fixture
    def healthy_result(self):
        """Create a healthy HealthCheckResult."""
        return HealthCheckResult(
            valid=True,
            file_exists=True,
            readable=True,
            loadable=True,
            element_count=1000,
            connections_checked=5000,
            min_inbound=2,
            max_inbound=10,
            index_path="/custom/path/to/index.bin",
            file_size_bytes=1024000,
            last_modified=datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc),
            errors=[],
            check_duration_ms=45.5,
            from_cache=False,
        )

    def test_custom_index_path_used(self, healthy_result):
        """AC6: Health check with --index-path uses explicit path."""
        runner = CliRunner()
        custom_path = "/custom/path/to/index.bin"

        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = healthy_result
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health", "--index-path", custom_path])

            assert result.exit_code == 0
            # Verify that check_health was called with custom path
            mock_service.check_health.assert_called_once()
            args, kwargs = mock_service.check_health.call_args
            assert args[0] == custom_path or custom_path in str(args)


class TestHealthCommandTimingInfo:
    """Test health command timing information."""

    @pytest.fixture
    def healthy_result(self):
        """Create a healthy HealthCheckResult."""
        return HealthCheckResult(
            valid=True,
            file_exists=True,
            readable=True,
            loadable=True,
            element_count=408234,
            connections_checked=12456789,
            min_inbound=8,
            max_inbound=64,
            index_path="/path/to/.code-indexer/index/hnsw.bin",
            file_size_bytes=1610612736,
            last_modified=datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc),
            errors=[],
            check_duration_ms=638.5,
            from_cache=False,
        )

    def test_timing_info_displayed(self, healthy_result):
        """AC7: Health check shows timing information in milliseconds."""
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = healthy_result
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health"])

            assert result.exit_code == 0
            # Check for timing information
            assert "638" in result.output or "Check Duration:" in result.output
            assert (
                "ms" in result.output.lower() or "milliseconds" in result.output.lower()
            )


class TestHealthCommandDefaultIndexPath:
    """Test that health command uses correct default index path."""

    def test_default_index_path_used_when_not_specified(self):
        """Test that default index path is used when --index-path not provided."""
        runner = CliRunner()

        healthy_result = HealthCheckResult(
            valid=True,
            file_exists=True,
            readable=True,
            loadable=True,
            element_count=1000,
            connections_checked=5000,
            min_inbound=2,
            max_inbound=10,
            index_path="/some/path/.code-indexer/index/hnsw.bin",
            file_size_bytes=1024000,
            last_modified=datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc),
            errors=[],
            check_duration_ms=45.5,
            from_cache=False,
        )

        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = healthy_result
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health"])

            assert result.exit_code == 0
            # Verify check_health was called
            mock_service.check_health.assert_called_once()
            args, kwargs = mock_service.check_health.call_args
            # Should contain .code-indexer/index path
            assert ".code-indexer" in str(args[0]) or "hnsw" in str(args[0]).lower()
