"""Story 4 — Part 2: Admin REST endpoint GET /api/admin/memory-governor.

Tests:
- Full snapshot returned when governor active.
- Graceful not-active response (no 500) when governor absent.
- Admin authentication enforced (403 without valid admin creds).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from code_indexer.server.services.memory_governor import (
    MemoryBand,
    MemoryGovernor,
    clear_memory_governor,
    set_memory_governor,
)

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

BYTES_PER_GIB = 1024 * 1024 * 1024
HOST_100_GIB = 100 * BYTES_PER_GIB
GREEN_USAGE_PCT = 30.0
YELLOW_PCT_DEFAULT = 70.0
RED_PCT_DEFAULT = 85.0
HYSTERESIS_PCT_DEFAULT = 10.0
NO_SWAP_PAGES_IN = 0
NO_RED_DWELL_SECONDS = 0.0

HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_FORBIDDEN = 403
HTTP_INTERNAL_ERROR = 500

ENDPOINT_PATH = "/api/admin/memory-governor"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_readers(used_pct: float) -> MagicMock:
    readers = MagicMock()
    vm = MagicMock()
    vm.total = HOST_100_GIB
    vm.used = int(HOST_100_GIB * used_pct / 100)
    readers.read_host_memory.return_value = vm
    readers.read_cgroup_v2_max.side_effect = FileNotFoundError
    readers.read_cgroup_v1_limit.side_effect = FileNotFoundError
    readers.read_pswpin.return_value = NO_SWAP_PAGES_IN
    return readers


def _green_gov() -> MemoryGovernor:
    gov = MemoryGovernor(
        readers=_make_readers(GREEN_USAGE_PCT),
        enabled=True,
        start_sampler=False,
        yellow_pct=YELLOW_PCT_DEFAULT,
        red_pct=RED_PCT_DEFAULT,
        hysteresis_pct=HYSTERESIS_PCT_DEFAULT,
        red_min_dwell_seconds=NO_RED_DWELL_SECONDS,
    )
    gov._tick()
    assert gov.band == MemoryBand.GREEN
    return gov


def _make_authed_app(tmp_path: Path):
    """Build a minimal FastAPI app with admin-ops routes and mocked admin auth."""
    from fastapi import FastAPI
    from code_indexer.server.routers.inline_admin_ops import register_admin_ops_routes
    from code_indexer.server.auth import dependencies

    app = FastAPI()

    def mock_admin():
        user = MagicMock()
        user.role = "admin"
        return user

    app.dependency_overrides[dependencies.get_current_admin_user] = mock_admin
    register_admin_ops_routes(
        app,
        jwt_manager=MagicMock(),
        user_manager=MagicMock(),
        golden_repo_manager=MagicMock(),
        background_job_manager=MagicMock(),
        workspace_cleanup_service=MagicMock(),
        config_service=MagicMock(),
        server_config=MagicMock(),
        data_dir=str(tmp_path),
        job_tracker=MagicMock(),
    )
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAdminEndpointRoute:
    """GET /api/admin/memory-governor REST endpoint behaviour (3 focused tests)."""

    def test_returns_full_snapshot_when_governor_active(self, tmp_path: Path):
        """HTTP 200 with full §3.5 snapshot when governor active."""
        from fastapi.testclient import TestClient

        gov = _green_gov()
        set_memory_governor(gov)
        try:
            resp = TestClient(_make_authed_app(tmp_path)).get(ENDPOINT_PATH)
            assert resp.status_code == HTTP_OK
            body = resp.json()
            assert body["band"] == "GREEN"
            assert "used_pct" in body
            assert "pid" in body
            assert "enabled" in body
            assert "yellow_pct" in body
            assert "lru_evictions" in body
        finally:
            clear_memory_governor()

    def test_no_500_when_governor_absent(self, tmp_path: Path):
        """HTTP 200 or 404 (never 500) when governor is None."""
        from fastapi.testclient import TestClient

        clear_memory_governor()
        resp = TestClient(_make_authed_app(tmp_path)).get(ENDPOINT_PATH)
        assert resp.status_code != HTTP_INTERNAL_ERROR
        assert resp.status_code in (HTTP_OK, HTTP_NOT_FOUND)
        if resp.status_code == HTTP_OK:
            body = resp.json()
            assert body.get("enabled") is False or body.get("band") is None

    def test_requires_admin_authentication(self, tmp_path: Path):
        """HTTP 403 when admin dependency rejects the caller."""
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient
        from code_indexer.server.routers.inline_admin_ops import (
            register_admin_ops_routes,
        )
        from code_indexer.server.auth import dependencies

        app = FastAPI()

        def reject_non_admin():
            raise HTTPException(status_code=HTTP_FORBIDDEN, detail="Admin required")

        app.dependency_overrides[dependencies.get_current_admin_user] = reject_non_admin
        register_admin_ops_routes(
            app,
            jwt_manager=MagicMock(),
            user_manager=MagicMock(),
            golden_repo_manager=MagicMock(),
            background_job_manager=MagicMock(),
            workspace_cleanup_service=MagicMock(),
            config_service=MagicMock(),
            server_config=MagicMock(),
            data_dir=str(tmp_path),
            job_tracker=MagicMock(),
        )
        resp = TestClient(app, raise_server_exceptions=False).get(ENDPOINT_PATH)
        assert resp.status_code == HTTP_FORBIDDEN
