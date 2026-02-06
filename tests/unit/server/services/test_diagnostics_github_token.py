"""
Tests for GitHub Token Diagnostics (Story S5 - AC2, AC7, AC8).

Tests GitHub token validation and API connectivity:
- AC2: GitHub Token diagnostic validates format AND tests API call, returns WORKING/ERROR/NOT_CONFIGURED
- AC7: API token checks have 30-second timeout
- AC8: Shows "Not Configured" status when token not configured
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import HTTPStatusError, Request, Response, TimeoutException

from code_indexer.server.services.diagnostics_service import (
    DiagnosticsService,
    DiagnosticStatus,
    API_TIMEOUT_SECONDS,
)


class TestCheckGitHubToken:
    """Tests for check_github_token() method (AC2, AC7, AC8)."""

    @pytest.mark.asyncio
    async def test_github_token_working_classic(self):
        """Test GitHub token working with classic token format (ghp_)."""
        service = DiagnosticsService()

        # Mock CITokenManager returning valid classic token
        mock_token_data = MagicMock()
        mock_token_data.token = "ghp_" + "x" * 36

        with patch(
            "code_indexer.server.services.diagnostics_service.CITokenManager"
        ) as mock_manager_class:
            mock_manager = mock_manager_class.return_value
            mock_manager.get_token.return_value = mock_token_data

            # Mock httpx client for API call
            with patch(
                "code_indexer.server.services.diagnostics_service.httpx.AsyncClient"
            ) as mock_client_class:
                mock_client = mock_client_class.return_value.__aenter__.return_value
                mock_response = MagicMock()
                mock_response.raise_for_status = MagicMock()
                mock_response.json = MagicMock(return_value={"login": "testuser"})
                mock_client.get = AsyncMock(return_value=mock_response)

                result = await service.check_github_token()

        assert result.name == "GitHub Token"
        assert result.status == DiagnosticStatus.WORKING
        assert "valid" in result.message.lower() or "working" in result.message.lower()

    @pytest.mark.asyncio
    async def test_github_token_working_fine_grained(self):
        """Test GitHub token working with fine-grained token format (github_pat_)."""
        service = DiagnosticsService()

        mock_token_data = MagicMock()
        mock_token_data.token = "github_pat_" + "x" * 82

        with patch(
            "code_indexer.server.services.diagnostics_service.CITokenManager"
        ) as mock_manager_class:
            mock_manager = mock_manager_class.return_value
            mock_manager.get_token.return_value = mock_token_data

            with patch(
                "code_indexer.server.services.diagnostics_service.httpx.AsyncClient"
            ) as mock_client_class:
                mock_client = mock_client_class.return_value.__aenter__.return_value
                mock_response = MagicMock()
                mock_response.raise_for_status = MagicMock()
                mock_response.json = MagicMock(return_value={"login": "testuser"})
                mock_client.get = AsyncMock(return_value=mock_response)

                result = await service.check_github_token()

        assert result.status == DiagnosticStatus.WORKING

    @pytest.mark.asyncio
    async def test_github_token_not_configured(self):
        """Test GitHub token not configured returns NOT_CONFIGURED (AC8)."""
        service = DiagnosticsService()

        with patch(
            "code_indexer.server.services.diagnostics_service.CITokenManager"
        ) as mock_manager_class:
            mock_manager = mock_manager_class.return_value
            mock_manager.get_token.return_value = None

            result = await service.check_github_token()

        assert result.status == DiagnosticStatus.NOT_CONFIGURED
        assert "not configured" in result.message.lower()

    @pytest.mark.asyncio
    async def test_github_token_invalid_format_warning(self):
        """Test GitHub token with invalid format returns WARNING."""
        service = DiagnosticsService()

        mock_token_data = MagicMock()
        mock_token_data.token = "invalid_token_format"

        with patch(
            "code_indexer.server.services.diagnostics_service.CITokenManager"
        ) as mock_manager_class:
            mock_manager = mock_manager_class.return_value
            mock_manager.get_token.return_value = mock_token_data

            result = await service.check_github_token()

        assert result.status == DiagnosticStatus.WARNING
        assert "format" in result.message.lower()

    @pytest.mark.asyncio
    async def test_github_token_api_call_fails_401(self):
        """Test GitHub token API call failing with 401 Unauthorized."""
        service = DiagnosticsService()

        mock_token_data = MagicMock()
        mock_token_data.token = "ghp_" + "x" * 36

        with patch(
            "code_indexer.server.services.diagnostics_service.CITokenManager"
        ) as mock_manager_class:
            mock_manager = mock_manager_class.return_value
            mock_manager.get_token.return_value = mock_token_data

            with patch(
                "code_indexer.server.services.diagnostics_service.httpx.AsyncClient"
            ) as mock_client_class:
                mock_client = mock_client_class.return_value.__aenter__.return_value
                mock_response_obj = Response(
                    status_code=401, request=Request("GET", "https://api.github.com/user")
                )
                mock_client.get = AsyncMock(
                    side_effect=HTTPStatusError(
                        "Unauthorized",
                        request=mock_response_obj.request,
                        response=mock_response_obj,
                    )
                )

                result = await service.check_github_token()

        assert result.status == DiagnosticStatus.ERROR
        assert "401" in result.message or "unauthorized" in result.message.lower()

    @pytest.mark.asyncio
    async def test_github_token_timeout(self):
        """Test GitHub token API call timing out after 30 seconds (AC7)."""
        service = DiagnosticsService()

        mock_token_data = MagicMock()
        mock_token_data.token = "ghp_" + "x" * 36

        with patch(
            "code_indexer.server.services.diagnostics_service.CITokenManager"
        ) as mock_manager_class:
            mock_manager = mock_manager_class.return_value
            mock_manager.get_token.return_value = mock_token_data

            with patch(
                "code_indexer.server.services.diagnostics_service.httpx.AsyncClient"
            ) as mock_client_class:
                mock_client = mock_client_class.return_value.__aenter__.return_value
                mock_client.get = AsyncMock(side_effect=TimeoutException("Timeout"))

                result = await service.check_github_token()

        assert result.status == DiagnosticStatus.ERROR
        assert "timeout" in result.message.lower() or "timed out" in result.message.lower()


class TestAPITimeoutConstant:
    """Tests for API_TIMEOUT_SECONDS constant (AC7)."""

    def test_api_timeout_constant_exists(self):
        """Test API_TIMEOUT_SECONDS constant exists and is 30."""
        assert API_TIMEOUT_SECONDS == 30.0
