"""Unit tests for debug router registration.

Story #405: Debug Memory Endpoint

Verifies that debug_router is exported from debug_routes and that both
/debug/memory-snapshot and /debug/memory-compare routes are registered.
"""

import pytest
from fastapi.testclient import TestClient


class TestDebugRouterRegistration:
    """Verify debug_router is exportable and has correct routes registered."""

    def test_debug_router_is_importable(self):
        from code_indexer.server.routers.debug_routes import debug_router
        from fastapi import APIRouter

        assert isinstance(debug_router, APIRouter)

    def test_snapshot_route_registered_in_router(self):
        from code_indexer.server.routers.debug_routes import debug_router

        paths = {route.path for route in debug_router.routes}
        assert "/debug/memory-snapshot" in paths, f"Routes found: {paths}"

    def test_compare_route_registered_in_router(self):
        from code_indexer.server.routers.debug_routes import debug_router

        paths = {route.path for route in debug_router.routes}
        assert "/debug/memory-compare" in paths, f"Routes found: {paths}"

    def test_snapshot_route_accepts_get(self):
        from code_indexer.server.routers.debug_routes import debug_router

        snapshot_routes = [r for r in debug_router.routes if r.path == "/debug/memory-snapshot"]
        assert len(snapshot_routes) > 0
        methods = {m for r in snapshot_routes for m in r.methods}
        assert "GET" in methods

    def test_compare_route_accepts_get(self):
        from code_indexer.server.routers.debug_routes import debug_router

        compare_routes = [r for r in debug_router.routes if r.path == "/debug/memory-compare"]
        assert len(compare_routes) > 0
        methods = {m for r in compare_routes for m in r.methods}
        assert "GET" in methods

    def test_debug_router_registered_in_app(self):
        """debug_router must be included in the FastAPI app."""
        from code_indexer.server.app import app

        app_paths = {route.path for route in app.routes}
        assert "/debug/memory-snapshot" in app_paths, (
            f"debug router not registered. App paths sample: {list(app_paths)[:30]}"
        )


class TestDebugRouterHttpHandlers:
    """Test HTTP handler success and error paths by patching the localhost guard."""

    def setup_method(self):
        import code_indexer.server.routers.debug_routes as mod

        mod._last_snapshot = None

    @pytest.fixture
    def localhost_client(self, monkeypatch):
        """TestClient with _check_localhost patched to always allow."""
        import code_indexer.server.routers.debug_routes as mod
        from code_indexer.server.app import app

        monkeypatch.setattr(mod, "_check_localhost", lambda req: True)
        return TestClient(app)

    def test_snapshot_returns_200_for_localhost(self, localhost_client):
        """Handler success path: 200 with JSON body."""
        response = localhost_client.get("/debug/memory-snapshot")
        assert response.status_code == 200
        data = response.json()
        assert "timestamp" in data
        assert "total_objects" in data
        assert "by_count" in data

    def test_compare_returns_404_when_no_baseline(self, localhost_client):
        """Handler 404 path: no stored baseline."""
        response = localhost_client.get(
            "/debug/memory-compare?baseline=2099-01-01T00:00:00Z"
        )
        assert response.status_code == 404
        assert "detail" in response.json()

    def test_compare_returns_200_with_valid_baseline(self, localhost_client):
        """Handler success path for compare: takes snapshot then compares."""
        # First, take a snapshot to store baseline
        snap_response = localhost_client.get("/debug/memory-snapshot")
        assert snap_response.status_code == 200
        ts = snap_response.json()["timestamp"]

        # Now compare against that baseline
        response = localhost_client.get(f"/debug/memory-compare?baseline={ts}")
        assert response.status_code == 200
        data = response.json()
        assert data["baseline_timestamp"] == ts
        assert "delta_objects" in data
