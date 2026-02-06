"""
Tests for External API Integration Diagnostics (Story S4).

Tests all external API diagnostic checks:
- GitHub API diagnostic (AC1)
- GitLab API diagnostic (AC2)
- Claude Server diagnostic (AC3)
- OIDC Provider diagnostic (AC4)
- OpenTelemetry Collector diagnostic (AC5)
- 30-second timeout (AC6)
- Category dispatch (AC7)
- 5-minute caching (AC8)
- NOT_CONFIGURED status (AC9)
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from code_indexer.server.services.diagnostics_service import (
    DiagnosticCategory,
    DiagnosticResult,
    DiagnosticStatus,
    DiagnosticsService,
)


@pytest.fixture
def diagnostics_service():
    """Create DiagnosticsService instance for testing."""
    return DiagnosticsService()


@pytest.fixture
def mock_config_manager():
    """Mock ServerConfigManager with test configuration."""
    with patch(
        "code_indexer.server.services.diagnostics_service.ServerConfigManager"
    ) as mock:
        config = MagicMock()
        config.server_dir = "/test/server"
        config.oidc_provider_config = MagicMock()
        config.oidc_provider_config.enabled = True
        config.oidc_provider_config.issuer_url = "https://oidc.example.com"
        config.telemetry_config = MagicMock()
        config.telemetry_config.enabled = True
        config.telemetry_config.collector_endpoint = "http://localhost:4317"
        mock.return_value.load_config.return_value = config
        yield mock


@pytest.fixture
def mock_token_manager():
    """Mock CITokenManager for GitHub/GitLab tokens."""
    with patch(
        "code_indexer.server.services.diagnostics_service.CITokenManager"
    ) as mock:
        token_data_github = MagicMock()
        token_data_github.token = "ghp_test123456789012345678901234567890"
        token_data_github.platform = "github"

        token_data_gitlab = MagicMock()
        token_data_gitlab.token = "glpat-test1234567890123456"
        token_data_gitlab.platform = "gitlab"
        token_data_gitlab.base_url = "https://gitlab.com"

        instance = mock.return_value
        instance.get_token.side_effect = lambda platform: {
            "github": token_data_github,
            "gitlab": token_data_gitlab,
        }.get(platform)
        yield mock


@pytest.fixture
def mock_delegation_manager():
    """Mock ClaudeDelegationManager for Claude Server config."""
    with patch(
        "code_indexer.server.services.diagnostics_service.ClaudeDelegationManager"
    ) as mock:
        delegation_config = MagicMock()
        delegation_config.is_configured = True
        delegation_config.claude_server_url = "https://claude.example.com"
        delegation_config.claude_server_username = "test_user"
        delegation_config.claude_server_credential = "test_password"
        delegation_config.claude_server_credential_type = "password"
        mock.return_value.load_config.return_value = delegation_config
        yield mock


# AC1: GitHub API diagnostic tests


@pytest.mark.asyncio
async def test_check_github_api_working(
    diagnostics_service, mock_token_manager, mock_config_manager
):
    """Test GitHub API diagnostic returns WORKING when API responds successfully."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"rate": {"limit": 5000, "remaining": 4999}}
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await diagnostics_service.check_github_api()

        assert result.name == "GitHub API"
        assert result.status == DiagnosticStatus.WORKING
        assert "GitHub API is accessible" in result.message
        assert "rate_limit" in result.details


@pytest.mark.asyncio
async def test_check_github_api_error_http_error(
    diagnostics_service, mock_token_manager, mock_config_manager
):
    """Test GitHub API diagnostic returns ERROR when API returns HTTP error."""
    import httpx

    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.text = "Bad credentials"
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "401 Unauthorized", request=MagicMock(), response=mock_response
    )

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await diagnostics_service.check_github_api()

        assert result.name == "GitHub API"
        assert result.status == DiagnosticStatus.ERROR
        assert "GitHub API request failed" in result.message or "401" in result.message


@pytest.mark.asyncio
async def test_check_github_api_not_configured(
    diagnostics_service, mock_config_manager
):
    """Test GitHub API diagnostic returns NOT_CONFIGURED when no token configured."""
    with patch(
        "code_indexer.server.services.diagnostics_service.CITokenManager"
    ) as mock:
        mock.return_value.get_token.return_value = None

        result = await diagnostics_service.check_github_api()

        assert result.name == "GitHub API"
        assert result.status == DiagnosticStatus.NOT_CONFIGURED
        assert "not configured" in result.message.lower()


@pytest.mark.asyncio
async def test_check_github_api_timeout(
    diagnostics_service, mock_token_manager, mock_config_manager
):
    """Test GitHub API diagnostic handles timeout (30 seconds per AC6)."""
    import httpx

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("Request timeout"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await diagnostics_service.check_github_api()

        assert result.name == "GitHub API"
        assert result.status == DiagnosticStatus.ERROR
        assert "timed out" in result.message.lower()


# AC2: GitLab API diagnostic tests


@pytest.mark.asyncio
async def test_check_gitlab_api_working(
    diagnostics_service, mock_token_manager, mock_config_manager
):
    """Test GitLab API diagnostic returns WORKING when API responds successfully."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": 123, "username": "test_user"}
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await diagnostics_service.check_gitlab_api()

        assert result.name == "GitLab API"
        assert result.status == DiagnosticStatus.WORKING
        assert "GitLab API is accessible" in result.message
        assert "username" in result.details


@pytest.mark.asyncio
async def test_check_gitlab_api_not_configured(
    diagnostics_service, mock_config_manager
):
    """Test GitLab API diagnostic returns NOT_CONFIGURED when no token configured."""
    with patch(
        "code_indexer.server.services.diagnostics_service.CITokenManager"
    ) as mock:
        mock.return_value.get_token.return_value = None

        result = await diagnostics_service.check_gitlab_api()

        assert result.name == "GitLab API"
        assert result.status == DiagnosticStatus.NOT_CONFIGURED
        assert "not configured" in result.message.lower()


@pytest.mark.asyncio
async def test_check_gitlab_api_error_http_error(
    diagnostics_service, mock_token_manager, mock_config_manager
):
    """Test GitLab API diagnostic returns ERROR when API returns HTTP error."""
    import httpx

    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.text = "Forbidden"
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "403 Forbidden", request=MagicMock(), response=mock_response
    )

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await diagnostics_service.check_gitlab_api()

        assert result.name == "GitLab API"
        assert result.status == DiagnosticStatus.ERROR
        assert "GitLab API request failed" in result.message or "403" in result.message


# AC3: Claude Server diagnostic tests


@pytest.mark.asyncio
async def test_check_claude_server_working(
    diagnostics_service, mock_delegation_manager, mock_config_manager
):
    """Test Claude Server diagnostic returns WORKING when delegation endpoint accessible."""
    with patch("httpx.AsyncClient") as mock_client:
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "test_token"}
        mock_response.raise_for_status = MagicMock()

        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=mock_response
        )

        result = await diagnostics_service.check_claude_server()

        assert result.name == "Claude Server"
        assert result.status == DiagnosticStatus.WORKING
        assert "Claude Server is accessible" in result.message


@pytest.mark.asyncio
async def test_check_claude_server_not_configured(
    diagnostics_service, mock_config_manager
):
    """Test Claude Server diagnostic returns NOT_CONFIGURED when not configured."""
    with patch(
        "code_indexer.server.services.diagnostics_service.ClaudeDelegationManager"
    ) as mock:
        delegation_config = MagicMock()
        delegation_config.is_configured = False
        mock.return_value.load_config.return_value = delegation_config

        result = await diagnostics_service.check_claude_server()

        assert result.name == "Claude Server"
        assert result.status == DiagnosticStatus.NOT_CONFIGURED
        assert "not configured" in result.message.lower()


@pytest.mark.asyncio
async def test_check_claude_server_error_connection_failed(
    diagnostics_service, mock_delegation_manager, mock_config_manager
):
    """Test Claude Server diagnostic returns ERROR when connection fails."""
    import httpx

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        result = await diagnostics_service.check_claude_server()

        assert result.name == "Claude Server"
        assert result.status == DiagnosticStatus.ERROR
        assert (
            "Claude Server connection failed" in result.message
            or "connection" in result.message.lower()
        )


# AC4: OIDC Provider diagnostic tests


@pytest.mark.asyncio
async def test_check_oidc_provider_working(diagnostics_service, mock_config_manager):
    """Test OIDC Provider diagnostic returns WORKING when discovery endpoint responds."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "issuer": "https://oidc.example.com",
        "authorization_endpoint": "https://oidc.example.com/auth",
        "token_endpoint": "https://oidc.example.com/token",
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await diagnostics_service.check_oidc_provider()

        assert result.name == "OIDC Provider"
        assert result.status == DiagnosticStatus.WORKING
        assert "OIDC Provider is accessible" in result.message
        assert "issuer" in result.details


@pytest.mark.asyncio
async def test_check_oidc_provider_not_configured(
    diagnostics_service, mock_config_manager
):
    """Test OIDC Provider diagnostic returns NOT_CONFIGURED when OIDC disabled."""
    with patch(
        "code_indexer.server.services.diagnostics_service.ServerConfigManager"
    ) as mock:
        config = MagicMock()
        config.oidc_provider_config = MagicMock()
        config.oidc_provider_config.enabled = False
        mock.return_value.load_config.return_value = config

        result = await diagnostics_service.check_oidc_provider()

        assert result.name == "OIDC Provider"
        assert result.status == DiagnosticStatus.NOT_CONFIGURED
        assert (
            "not configured" in result.message.lower()
            or "disabled" in result.message.lower()
        )


@pytest.mark.asyncio
async def test_check_oidc_provider_error_http_error(
    diagnostics_service, mock_config_manager
):
    """Test OIDC Provider diagnostic returns ERROR when discovery endpoint fails."""
    import httpx

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Not found"
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404 Not Found", request=MagicMock(), response=mock_response
    )

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await diagnostics_service.check_oidc_provider()

        assert result.name == "OIDC Provider"
        assert result.status == DiagnosticStatus.ERROR
        assert (
            "OIDC Provider request failed" in result.message or "404" in result.message
        )


# AC5: OpenTelemetry Collector diagnostic tests


@pytest.mark.asyncio
async def test_check_otel_collector_working(diagnostics_service, mock_config_manager):
    """Test OpenTelemetry Collector diagnostic returns WORKING when endpoint accessible."""
    with patch("httpx.AsyncClient") as mock_client:
        # OTLP gRPC health check or simple connectivity test
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )

        result = await diagnostics_service.check_otel_collector()

        assert result.name == "OpenTelemetry Collector"
        assert result.status == DiagnosticStatus.WORKING
        assert "OpenTelemetry Collector is accessible" in result.message


@pytest.mark.asyncio
async def test_check_otel_collector_not_configured(
    diagnostics_service, mock_config_manager
):
    """Test OpenTelemetry Collector diagnostic returns NOT_CONFIGURED when telemetry disabled."""
    with patch(
        "code_indexer.server.services.diagnostics_service.ServerConfigManager"
    ) as mock:
        config = MagicMock()
        config.telemetry_config = MagicMock()
        config.telemetry_config.enabled = False
        mock.return_value.load_config.return_value = config

        result = await diagnostics_service.check_otel_collector()

        assert result.name == "OpenTelemetry Collector"
        assert result.status == DiagnosticStatus.NOT_CONFIGURED
        assert (
            "not configured" in result.message.lower()
            or "disabled" in result.message.lower()
        )


@pytest.mark.asyncio
async def test_check_otel_collector_error_connection_failed(
    diagnostics_service, mock_config_manager
):
    """Test OpenTelemetry Collector diagnostic returns ERROR when connection fails."""
    import httpx

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        result = await diagnostics_service.check_otel_collector()

        assert result.name == "OpenTelemetry Collector"
        assert result.status == DiagnosticStatus.ERROR
        assert (
            "OpenTelemetry Collector connection failed" in result.message
            or "connection" in result.message.lower()
        )


# AC6: 30-second timeout test


@pytest.mark.asyncio
async def test_external_api_checks_have_30_second_timeout(
    diagnostics_service,
    mock_token_manager,
    mock_delegation_manager,
    mock_config_manager,
):
    """Test that all API checks use 30-second timeout (AC6)."""
    import httpx

    # Verify that timeout is configured correctly by checking the constant
    from code_indexer.server.services.diagnostics_service import API_TIMEOUT_SECONDS

    assert API_TIMEOUT_SECONDS == 30.0, "API timeout should be 30 seconds"

    # Verify timeout exception is handled correctly
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("Request timeout"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await diagnostics_service.check_github_api()

        assert result.status == DiagnosticStatus.ERROR
        assert "timed out" in result.message.lower()


# AC7: run_category dispatch test


@pytest.mark.asyncio
async def test_run_category_dispatches_to_external_api_diagnostics(
    diagnostics_service,
    mock_token_manager,
    mock_delegation_manager,
    mock_config_manager,
):
    """Test that run_category(EXTERNAL_APIS) dispatches to run_external_api_diagnostics."""
    with patch("httpx.AsyncClient") as mock_client:
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_response.raise_for_status = MagicMock()

        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=mock_response
        )

        await diagnostics_service.run_category(DiagnosticCategory.EXTERNAL_APIS)

        # Check that cache was populated with actual results, not placeholders
        results = diagnostics_service.get_category_status(
            DiagnosticCategory.EXTERNAL_APIS
        )

        assert len(results) > 0
        assert any(r.name == "GitHub API" for r in results)
        assert any(r.name == "GitLab API" for r in results)
        assert any(r.name == "Claude Server" for r in results)
        assert any(r.name == "OIDC Provider" for r in results)
        assert any(r.name == "OpenTelemetry Collector" for r in results)

        # No result should have NOT_RUN status (placeholder status)
        assert all(r.status != DiagnosticStatus.NOT_RUN for r in results)


# AC8: 5-minute caching test


def test_external_api_cache_ttl_is_5_minutes():
    """Verify EXTERNAL_APIS uses 5-minute cache TTL per AC8."""
    from code_indexer.server.services.diagnostics_service import API_CACHE_TTL
    from datetime import timedelta

    assert API_CACHE_TTL == timedelta(minutes=5)


@pytest.mark.asyncio
async def test_external_api_diagnostics_cached_for_5_minutes(
    diagnostics_service,
    mock_token_manager,
    mock_delegation_manager,
    mock_config_manager,
):
    """Test that external API diagnostic results are cached for 5 minutes."""
    with patch("httpx.AsyncClient") as mock_client:
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_response.raise_for_status = MagicMock()

        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=mock_response
        )

        # First call - should populate cache
        await diagnostics_service.run_category(DiagnosticCategory.EXTERNAL_APIS)
        first_timestamp = diagnostics_service._cache_timestamps[
            DiagnosticCategory.EXTERNAL_APIS
        ]

        # Second call immediately - should use cache
        await diagnostics_service.run_category(DiagnosticCategory.EXTERNAL_APIS)
        second_timestamp = diagnostics_service._cache_timestamps[
            DiagnosticCategory.EXTERNAL_APIS
        ]

        # Timestamps should be the same (cache hit)
        assert first_timestamp == second_timestamp

        # Simulate cache expiration (patch API_CACHE_TTL to 0 for EXTERNAL_APIS)
        with patch(
            "code_indexer.server.services.diagnostics_service.API_CACHE_TTL",
            timedelta(seconds=0),
        ):
            # Third call - should refresh cache
            await diagnostics_service.run_category(DiagnosticCategory.EXTERNAL_APIS)
            third_timestamp = diagnostics_service._cache_timestamps[
                DiagnosticCategory.EXTERNAL_APIS
            ]

            # Timestamp should be newer (cache miss, refresh)
            assert third_timestamp > second_timestamp


# AC9: NOT_CONFIGURED status test


@pytest.mark.asyncio
async def test_all_apis_return_not_configured_when_not_configured(
    diagnostics_service, mock_config_manager
):
    """Test that all API diagnostics return NOT_CONFIGURED when endpoints/tokens not configured."""
    # Mock all configuration sources to return None/disabled
    with patch(
        "code_indexer.server.services.diagnostics_service.CITokenManager"
    ) as mock_token:
        mock_token.return_value.get_token.return_value = None

        with patch(
            "code_indexer.server.services.diagnostics_service.ClaudeDelegationManager"
        ) as mock_delegation:
            delegation_config = MagicMock()
            delegation_config.is_configured = False
            mock_delegation.return_value.load_config.return_value = delegation_config

            with patch(
                "code_indexer.server.services.diagnostics_service.ServerConfigManager"
            ) as mock_config:
                config = MagicMock()
                config.oidc_provider_config = MagicMock()
                config.oidc_provider_config.enabled = False
                config.telemetry_config = MagicMock()
                config.telemetry_config.enabled = False
                mock_config.return_value.load_config.return_value = config

                # Run all checks
                github_result = await diagnostics_service.check_github_api()
                gitlab_result = await diagnostics_service.check_gitlab_api()
                claude_result = await diagnostics_service.check_claude_server()
                oidc_result = await diagnostics_service.check_oidc_provider()
                otel_result = await diagnostics_service.check_otel_collector()

                # All should be NOT_CONFIGURED
                assert github_result.status == DiagnosticStatus.NOT_CONFIGURED
                assert gitlab_result.status == DiagnosticStatus.NOT_CONFIGURED
                assert claude_result.status == DiagnosticStatus.NOT_CONFIGURED
                assert oidc_result.status == DiagnosticStatus.NOT_CONFIGURED
                assert otel_result.status == DiagnosticStatus.NOT_CONFIGURED


# Parallel execution test


@pytest.mark.asyncio
async def test_run_external_api_diagnostics_executes_in_parallel(
    diagnostics_service,
    mock_token_manager,
    mock_delegation_manager,
    mock_config_manager,
):
    """Test that run_external_api_diagnostics executes all checks in parallel using asyncio.gather."""
    execution_order = []

    async def track_github(*args, **kwargs):
        execution_order.append("github_start")
        await asyncio.sleep(0.1)
        execution_order.append("github_end")
        response = AsyncMock()
        response.status_code = 200
        response.json.return_value = {}
        response.raise_for_status = MagicMock()
        return response

    async def track_gitlab(*args, **kwargs):
        execution_order.append("gitlab_start")
        await asyncio.sleep(0.1)
        execution_order.append("gitlab_end")
        response = AsyncMock()
        response.status_code = 200
        response.json.return_value = {}
        response.raise_for_status = MagicMock()
        return response

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = track_github
        mock_client.return_value.__aenter__.return_value.post = track_gitlab

        start = datetime.now()
        results = await diagnostics_service.run_external_api_diagnostics()
        duration = (datetime.now() - start).total_seconds()

        # If executed sequentially, would take 0.5s (5 checks * 0.1s each)
        # If executed in parallel, should take ~0.1s
        assert duration < 0.3, f"Expected parallel execution ~0.1s, got {duration}s"

        # Verify all checks completed
        assert len(results) == 5
        assert all(isinstance(r, DiagnosticResult) for r in results)
