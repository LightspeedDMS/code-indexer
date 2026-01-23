"""
Unit tests for ApiKeyConnectivityTester - async connectivity testing for API keys.

Tests cover:
- Anthropic connectivity test via Claude CLI hello world invocation
- VoyageAI connectivity test via embedding API call
- Async non-blocking test execution
- Result reporting with status and error messages

Story #20: API Key Management for Claude CLI and VoyageAI
"""

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from code_indexer.server.services.api_key_management import (
    ApiKeyConnectivityTester,
    ConnectivityTestResult,
)


class TestConnectivityTestResultDataClass:
    """Test ConnectivityTestResult data class properties."""

    def test_connectivity_result_success_state(self):
        """ConnectivityTestResult with success=True."""
        result = ConnectivityTestResult(success=True, provider="anthropic")
        assert result.success is True
        assert result.provider == "anthropic"
        assert result.error is None
        assert result.response_time_ms is None

    def test_connectivity_result_failure_state(self):
        """ConnectivityTestResult with failure and error message."""
        result = ConnectivityTestResult(
            success=False, provider="voyageai", error="Connection refused"
        )
        assert result.success is False
        assert result.provider == "voyageai"
        assert result.error == "Connection refused"

    def test_connectivity_result_with_response_time(self):
        """ConnectivityTestResult includes response time on success."""
        result = ConnectivityTestResult(
            success=True, provider="anthropic", response_time_ms=150
        )
        assert result.success is True
        assert result.response_time_ms == 150


class TestAnthropicConnectivityTest:
    """Test Anthropic API key connectivity testing."""

    @pytest.mark.asyncio
    async def test_anthropic_connectivity_success(self):
        """AC: Anthropic connectivity test succeeds with valid API key."""
        tester = ApiKeyConnectivityTester()

        # Mock subprocess for Claude CLI hello world test
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"Hello!", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await tester.test_anthropic_connectivity(
                "sk-ant-api03-valid12345678901234567890123456789"
            )

        assert result.success is True
        assert result.provider == "anthropic"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_anthropic_connectivity_failure_invalid_key(self):
        """AC: Anthropic connectivity test fails with invalid API key."""
        tester = ApiKeyConnectivityTester()

        # Mock subprocess for failed Claude CLI test
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.communicate = AsyncMock(
            return_value=(b"", b"Invalid API key")
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await tester.test_anthropic_connectivity(
                "sk-ant-api03-invalid1234567890123456789012"
            )

        assert result.success is False
        assert result.provider == "anthropic"
        assert "Invalid" in result.error or "failed" in result.error.lower()

    @pytest.mark.asyncio
    async def test_anthropic_connectivity_timeout(self):
        """AC: Anthropic connectivity test handles timeout."""
        tester = ApiKeyConnectivityTester(timeout_seconds=1)

        # Mock subprocess that times out
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )
        mock_process.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await tester.test_anthropic_connectivity(
                "sk-ant-api03-timeout12345678901234567890123"
            )

        assert result.success is False
        assert result.provider == "anthropic"
        assert "timeout" in result.error.lower()


class TestVoyageAIConnectivityTest:
    """Test VoyageAI API key connectivity testing."""

    @pytest.mark.asyncio
    async def test_voyageai_connectivity_success(self):
        """AC: VoyageAI connectivity test succeeds with valid API key."""
        tester = ApiKeyConnectivityTester()

        # Mock httpx for successful embedding test
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [{"embedding": [0.1, 0.2, 0.3]}]
        }

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client.return_value
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value.post = AsyncMock(return_value=mock_response)

            result = await tester.test_voyageai_connectivity(
                "pa-validvoyagekey12345"
            )

        assert result.success is True
        assert result.provider == "voyageai"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_voyageai_connectivity_failure_invalid_key(self):
        """AC: VoyageAI connectivity test fails with invalid API key."""
        tester = ApiKeyConnectivityTester()

        # Mock httpx for 401 unauthorized
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.raise_for_status = MagicMock(
            side_effect=Exception("401 Unauthorized")
        )

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client.return_value
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value.post = AsyncMock(return_value=mock_response)

            result = await tester.test_voyageai_connectivity(
                "pa-invalidvoyagekey12"
            )

        assert result.success is False
        assert result.provider == "voyageai"
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_voyageai_connectivity_timeout(self):
        """AC: VoyageAI connectivity test handles timeout."""
        tester = ApiKeyConnectivityTester(timeout_seconds=1)

        # Mock httpx timeout
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client.return_value
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value.post = AsyncMock(
                side_effect=asyncio.TimeoutError()
            )

            result = await tester.test_voyageai_connectivity(
                "pa-timeoutvoyagekey12"
            )

        assert result.success is False
        assert result.provider == "voyageai"
        assert "timeout" in result.error.lower()
