"""Unit tests for debug endpoint localhost security guard - AC2.

Story #405: Debug Memory Endpoint

AC2: GET /debug/memory-snapshot from non-localhost IP returns 403 Forbidden.
     The guard checks request.client.host against "127.0.0.1" and "::1" only.
"""

import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# AC2: _check_localhost guard function (unit tests with mocked Request)
# ---------------------------------------------------------------------------


class TestLocalhostGuard:
    """AC2: Localhost-only security - _check_localhost helper."""

    def test_ipv4_localhost_allowed(self):
        from code_indexer.server.routers.debug_routes import _check_localhost

        req = MagicMock()
        req.client.host = "127.0.0.1"
        assert _check_localhost(req) is True

    def test_ipv6_localhost_allowed(self):
        from code_indexer.server.routers.debug_routes import _check_localhost

        req = MagicMock()
        req.client.host = "::1"
        assert _check_localhost(req) is True

    def test_private_ip_rejected(self):
        from code_indexer.server.routers.debug_routes import _check_localhost

        req = MagicMock()
        req.client.host = "192.168.1.100"
        assert _check_localhost(req) is False

    def test_public_ip_rejected(self):
        from code_indexer.server.routers.debug_routes import _check_localhost

        req = MagicMock()
        req.client.host = "8.8.8.8"
        assert _check_localhost(req) is False

    def test_loopback_variant_rejected(self):
        """127.0.0.2 is NOT in the allowed set - only 127.0.0.1 and ::1."""
        from code_indexer.server.routers.debug_routes import _check_localhost

        req = MagicMock()
        req.client.host = "127.0.0.2"
        assert _check_localhost(req) is False

    def test_none_client_rejected(self):
        """request.client is None - must be rejected safely."""
        from code_indexer.server.routers.debug_routes import _check_localhost

        req = MagicMock()
        req.client = None
        assert _check_localhost(req) is False

    def test_empty_host_rejected(self):
        from code_indexer.server.routers.debug_routes import _check_localhost

        req = MagicMock()
        req.client.host = ""
        assert _check_localhost(req) is False


# ---------------------------------------------------------------------------
# AC2: HTTP-level tests via TestClient
# ---------------------------------------------------------------------------


class TestMemorySnapshotEndpointSecurity:
    """AC2: Endpoint returns 403 for non-localhost; route must be registered."""

    @pytest.fixture
    def client(self):
        from code_indexer.server.app import app

        return TestClient(app)

    def test_snapshot_endpoint_registered(self, client):
        """Route must exist - any non-404/405 response confirms registration."""
        response = client.get("/debug/memory-snapshot")
        assert response.status_code != 404, "Route not registered"
        assert response.status_code != 405, "Method not allowed - route misconfigured"

    def test_compare_endpoint_registered(self, client):
        """Route must exist."""
        response = client.get("/debug/memory-compare?baseline=2099-01-01T00:00:00Z")
        assert response.status_code != 404, "Route not registered"
        assert response.status_code != 405, "Method not allowed - route misconfigured"

    def test_check_localhost_rejects_non_localhost_ip(self):
        """AC2: _check_localhost returns False for non-localhost IPs."""
        from code_indexer.server.routers.debug_routes import _check_localhost

        req = MagicMock()
        req.client.host = "203.0.113.1"
        assert _check_localhost(req) is False
