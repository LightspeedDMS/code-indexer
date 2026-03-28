"""
Unit tests for tools/perf-suite/client.py

Story #333: Performance Test Harness with Single-User Baselines
AC2: HTTP Client with MCP JSON-RPC and REST Support
AC3: JWT Authentication with Proactive Refresh

TDD: These tests were written BEFORE the implementation.
"""

import pytest
import time
import sys
import os

# Add the perf-suite directory to path so we can import from it
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "../../../../tools/perf-suite")
)


class TestMCPEnvelopeConstruction:
    """Tests for building MCP JSON-RPC request envelopes."""

    def test_basic_mcp_envelope(self):
        from client import build_mcp_envelope

        envelope = build_mcp_envelope(
            tool_name="search_code",
            arguments={"query_text": "auth", "repository_alias": "click-global"},
            request_id=1,
        )

        assert envelope["jsonrpc"] == "2.0"
        assert envelope["id"] == 1
        assert envelope["method"] == "tools/call"
        assert envelope["params"]["name"] == "search_code"
        assert envelope["params"]["arguments"]["query_text"] == "auth"

    def test_mcp_envelope_with_different_tool(self):
        from client import build_mcp_envelope

        envelope = build_mcp_envelope(
            tool_name="scip_callchain",
            arguments={"from_symbol": "AuthService", "to_symbol": "DatabaseClient"},
            request_id=42,
        )

        assert envelope["params"]["name"] == "scip_callchain"
        assert envelope["id"] == 42

    def test_mcp_envelope_is_json_serializable(self):
        import json
        from client import build_mcp_envelope

        envelope = build_mcp_envelope(
            tool_name="list_repositories",
            arguments={},
            request_id=1,
        )

        # Should not raise
        json.dumps(envelope)

    def test_mcp_envelope_increments_id(self):
        from client import build_mcp_envelope

        e1 = build_mcp_envelope("tool_a", {}, 1)
        e2 = build_mcp_envelope("tool_b", {}, 2)

        assert e1["id"] == 1
        assert e2["id"] == 2


class TestTokenRefreshLogic:
    """Tests for JWT token expiration tracking and proactive refresh."""

    def test_token_not_expired_at_start(self):
        from client import TokenTracker

        tracker = TokenTracker(token="test_token", acquired_at=time.time())
        assert not tracker.needs_refresh()

    def test_token_needs_refresh_at_8_minutes(self):
        from client import TokenTracker

        # Simulate token acquired 8 minutes ago (480 seconds)
        acquired_at = time.time() - 480
        tracker = TokenTracker(token="test_token", acquired_at=acquired_at)
        assert tracker.needs_refresh()

    def test_token_needs_refresh_at_9_minutes(self):
        from client import TokenTracker

        # Simulate token acquired 9 minutes ago (540 seconds)
        acquired_at = time.time() - 540
        tracker = TokenTracker(token="test_token", acquired_at=acquired_at)
        assert tracker.needs_refresh()

    def test_token_does_not_need_refresh_at_7_minutes(self):
        from client import TokenTracker

        # Simulate token acquired 7 minutes ago (420 seconds)
        acquired_at = time.time() - 420
        tracker = TokenTracker(token="test_token", acquired_at=acquired_at)
        assert not tracker.needs_refresh()

    def test_token_tracker_stores_token(self):
        from client import TokenTracker

        tracker = TokenTracker(token="my_jwt_token", acquired_at=time.time())
        assert tracker.token == "my_jwt_token"

    def test_token_tracker_refresh_threshold_is_8_minutes(self):
        from client import TokenTracker

        # At exactly 8 minutes (480 seconds), should need refresh
        acquired_at = time.time() - 480
        tracker = TokenTracker(token="test_token", acquired_at=acquired_at)
        assert tracker.needs_refresh()

        # At 479 seconds, should NOT need refresh
        acquired_at = time.time() - 479
        tracker2 = TokenTracker(token="test_token", acquired_at=acquired_at)
        assert not tracker2.needs_refresh()


class TestRestRequestConstruction:
    """Tests for building REST API request bodies."""

    def test_query_rest_payload(self):
        from client import build_rest_payload

        payload = build_rest_payload(
            endpoint="/api/query",
            parameters={
                "query_text": "authentication",
                "repository_alias": "code-indexer-global",
                "search_mode": "semantic",
                "limit": 5,
            },
        )

        assert payload["query_text"] == "authentication"
        assert payload["repository_alias"] == "code-indexer-global"

    def test_query_multi_rest_payload(self):
        from client import build_rest_payload

        payload = build_rest_payload(
            endpoint="/api/query/multi",
            parameters={
                "query_text": "authentication",
                "repository_aliases": ["code-indexer-global", "flask-large-global"],
                "search_mode": "semantic",
                "limit": 5,
            },
        )

        assert payload["repository_aliases"] == [
            "code-indexer-global",
            "flask-large-global",
        ]

    def test_rest_payload_is_json_serializable(self):
        import json
        from client import build_rest_payload

        payload = build_rest_payload(
            endpoint="/api/query",
            parameters={"query_text": "test", "limit": 5},
        )

        # Should not raise
        json.dumps(payload)


class TestAuthHeaderConstruction:
    """Tests for building Authorization headers."""

    def test_bearer_header_format(self):
        from client import build_auth_headers

        headers = build_auth_headers(token="my_jwt_token_abc123")
        assert headers["Authorization"] == "Bearer my_jwt_token_abc123"

    def test_content_type_included(self):
        from client import build_auth_headers

        headers = build_auth_headers(token="token")
        assert headers["Content-Type"] == "application/json"


class TestPerfClientTimeout:
    """Tests for configurable per-request timeout in PerfClient.

    Bug #351: PerfClient has no per-request timeout. One hung server response
    blocks the entire perf suite forever.

    AC2: execute_mcp() and execute_rest() must accept a configurable timeout
         parameter (default 60 seconds).
    """

    def test_execute_mcp_has_timeout_parameter(self):
        """execute_mcp() must have a timeout parameter."""
        import inspect
        from client import PerfClient

        sig = inspect.signature(PerfClient.execute_mcp)
        assert "timeout" in sig.parameters, (
            "PerfClient.execute_mcp() must have a 'timeout' parameter"
        )

    def test_execute_mcp_default_timeout_is_60_seconds(self):
        """execute_mcp() must default to 60-second timeout."""
        import inspect
        from client import PerfClient

        sig = inspect.signature(PerfClient.execute_mcp)
        assert "timeout" in sig.parameters, (
            "execute_mcp() must have a timeout parameter"
        )
        default = sig.parameters["timeout"].default
        assert default == 60, (
            f"execute_mcp() timeout default must be 60 seconds, got {default}"
        )

    def test_execute_rest_has_timeout_parameter(self):
        """execute_rest() must have a timeout parameter."""
        import inspect
        from client import PerfClient

        sig = inspect.signature(PerfClient.execute_rest)
        assert "timeout" in sig.parameters, (
            "PerfClient.execute_rest() must have a 'timeout' parameter"
        )

    def test_execute_rest_default_timeout_is_60_seconds(self):
        """execute_rest() must default to 60-second timeout."""
        import inspect
        from client import PerfClient

        sig = inspect.signature(PerfClient.execute_rest)
        assert "timeout" in sig.parameters, (
            "execute_rest() must have a timeout parameter"
        )
        default = sig.parameters["timeout"].default
        assert default == 60, (
            f"execute_rest() timeout default must be 60 seconds, got {default}"
        )

    @pytest.mark.asyncio
    async def test_execute_mcp_passes_timeout_to_httpx(self):
        """execute_mcp() must pass timeout to the underlying httpx client.post() call."""
        import httpx
        from unittest.mock import AsyncMock, MagicMock
        from client import PerfClient

        client = PerfClient(
            server_url="http://localhost:8000",
            username="admin",
            password="admin",
        )
        # Pre-seed a token so authenticate is not called
        import time
        from client import TokenTracker

        client._token_tracker = TokenTracker(
            token="test_token", acquired_at=time.time()
        )

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.content = b'{"result": {}}'
        mock_response.json.return_value = {"result": {}}

        mock_http_client = AsyncMock(spec=httpx.AsyncClient)
        mock_http_client.post = AsyncMock(return_value=mock_response)

        await client.execute_mcp(
            mock_http_client, "search_code", {"query_text": "auth"}, timeout=45
        )

        # Verify timeout was passed through to httpx client.post()
        call_kwargs = mock_http_client.post.call_args[1]
        assert "timeout" in call_kwargs, (
            "execute_mcp() must pass timeout kwarg to httpx client.post()"
        )
        assert call_kwargs["timeout"] == 45, (
            f"timeout must be 45, got {call_kwargs['timeout']}"
        )

    @pytest.mark.asyncio
    async def test_execute_rest_passes_timeout_to_httpx(self):
        """execute_rest() must pass timeout to the underlying httpx client.post() call."""
        import httpx
        from unittest.mock import AsyncMock, MagicMock
        from client import PerfClient

        client = PerfClient(
            server_url="http://localhost:8000",
            username="admin",
            password="admin",
        )
        import time
        from client import TokenTracker

        client._token_tracker = TokenTracker(
            token="test_token", acquired_at=time.time()
        )

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.content = b'{"result": {}}'

        mock_http_client = AsyncMock(spec=httpx.AsyncClient)
        mock_http_client.post = AsyncMock(return_value=mock_response)

        await client.execute_rest(
            mock_http_client, "/api/query", {"query_text": "auth"}, timeout=45
        )

        call_kwargs = mock_http_client.post.call_args[1]
        assert "timeout" in call_kwargs, (
            "execute_rest() must pass timeout kwarg to httpx client.post()"
        )
        assert call_kwargs["timeout"] == 45, (
            f"timeout must be 45, got {call_kwargs['timeout']}"
        )

    @pytest.mark.asyncio
    async def test_execute_mcp_timeout_returns_error_result_on_timeout(self):
        """execute_mcp() must return error RequestResult (not raise) on timeout."""
        import httpx
        from unittest.mock import AsyncMock
        from client import PerfClient

        client = PerfClient(
            server_url="http://localhost:8000",
            username="admin",
            password="admin",
        )
        import time
        from client import TokenTracker

        client._token_tracker = TokenTracker(
            token="test_token", acquired_at=time.time()
        )

        mock_http_client = AsyncMock(spec=httpx.AsyncClient)
        mock_http_client.post = AsyncMock(
            side_effect=httpx.TimeoutException("Request timed out")
        )

        result = await client.execute_mcp(
            mock_http_client, "search_code", {"query_text": "auth"}, timeout=1
        )

        assert result.success is False, (
            "execute_mcp() must return success=False on timeout"
        )
        assert result.error_message is not None, "error_message must be set on timeout"
        assert (
            "timeout" in result.error_message.lower()
            or "timed out" in result.error_message.lower()
        ), f"error_message must mention timeout, got: {result.error_message}"

    @pytest.mark.asyncio
    async def test_execute_rest_timeout_returns_error_result_on_timeout(self):
        """execute_rest() must return error RequestResult (not raise) on timeout."""
        import httpx
        from unittest.mock import AsyncMock
        from client import PerfClient

        client = PerfClient(
            server_url="http://localhost:8000",
            username="admin",
            password="admin",
        )
        import time
        from client import TokenTracker

        client._token_tracker = TokenTracker(
            token="test_token", acquired_at=time.time()
        )

        mock_http_client = AsyncMock(spec=httpx.AsyncClient)
        mock_http_client.post = AsyncMock(
            side_effect=httpx.TimeoutException("Request timed out")
        )

        result = await client.execute_rest(
            mock_http_client, "/api/query", {"query_text": "auth"}, timeout=1
        )

        assert result.success is False, (
            "execute_rest() must return success=False on timeout"
        )
        assert result.error_message is not None, "error_message must be set on timeout"
        assert (
            "timeout" in result.error_message.lower()
            or "timed out" in result.error_message.lower()
        ), f"error_message must mention timeout, got: {result.error_message}"


class TestPerfClientUnit:
    """Unit tests for PerfClient (non-async parts that can be tested without a server)."""

    def test_client_initialization(self):
        from client import PerfClient

        client = PerfClient(
            server_url="http://localhost:8000",
            username="admin",
            password="admin",
        )
        assert client.server_url == "http://localhost:8000"
        assert client.username == "admin"

    def test_client_server_url_no_trailing_slash(self):
        from client import PerfClient

        client = PerfClient(
            server_url="http://localhost:8000/",
            username="admin",
            password="admin",
        )
        # URL should be normalized without trailing slash
        assert not client.server_url.endswith("/")

    def test_mcp_url_construction(self):
        from client import PerfClient

        client = PerfClient(
            server_url="http://localhost:8000",
            username="admin",
            password="admin",
        )
        assert client.mcp_url == "http://localhost:8000/mcp"

    def test_auth_url_construction(self):
        from client import PerfClient

        client = PerfClient(
            server_url="http://localhost:8000",
            username="admin",
            password="admin",
        )
        assert client.auth_url == "http://localhost:8000/auth/login"
