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
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../tools/perf-suite"))


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

        assert payload["repository_aliases"] == ["code-indexer-global", "flask-large-global"]

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
