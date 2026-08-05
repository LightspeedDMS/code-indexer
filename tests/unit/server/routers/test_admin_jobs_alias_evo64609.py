"""EVO-64609: GET /api/admin/jobs/{job_id} back-compat alias.

Hard evidence (running multipod, 3h): 80 `GET /api/admin/jobs/<id>` -> 404 and 0
200s. That path had no route (only /api/admin/jobs/stats + /cleanup existed), so the
golden-repo-seeder and agent-orchestrator workers were getting route-not-found 404s
they can misread as job failures. The alias delegates to background_job_manager.
get_job_status, which already falls back to the shared persistent store (Story #267),
so it resolves cross-pod.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import (
    get_current_admin_user,
    get_current_admin_user_hybrid,
    get_current_user,
)
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers.inline_admin_ops import register_admin_ops_routes


def _make_admin() -> User:
    return User(
        username="testadmin",
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _full_job_status(job_id: str) -> dict:
    return {
        "job_id": job_id,
        "operation_type": "add_golden_repo",
        "status": "completed",
        "created_at": "2026-08-05T00:00:00Z",
        "started_at": "2026-08-05T00:00:01Z",
        "completed_at": "2026-08-05T00:00:02Z",
        "progress": 100,
        "result": {"ok": True},
        "error": None,
        "username": "testadmin",
        "current_phase": None,
        "phase_detail": None,
    }


def _app(get_job_status_return) -> tuple[FastAPI, MagicMock]:
    app = FastAPI()
    bjm = MagicMock()
    bjm.jobs = {}  # so the sibling /stats route works with an empty set
    bjm.get_job_status.return_value = get_job_status_return
    register_admin_ops_routes(
        app,
        jwt_manager=MagicMock(),
        user_manager=MagicMock(),
        golden_repo_manager=MagicMock(),
        background_job_manager=bjm,
        workspace_cleanup_service=MagicMock(),
        config_service=MagicMock(),
        server_config=MagicMock(),
        data_dir="/tmp",
        job_tracker=MagicMock(),
    )
    admin = _make_admin()
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[get_current_admin_user] = lambda: admin
    app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin
    return app, bjm


class TestAdminJobsAlias:
    def test_alias_returns_job_status(self):
        app, bjm = _app(_full_job_status("job-123"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/admin/jobs/job-123")
        assert resp.status_code == 200, (
            f"expected 200, got {resp.status_code}: {resp.text}"
        )
        assert resp.json()["job_id"] == "job-123"
        bjm.get_job_status.assert_called_once()

    def test_alias_404_when_job_missing(self):
        app, _bjm = _app(None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/admin/jobs/does-not-exist")
        assert resp.status_code == 404
        assert "not found" in resp.text.lower()

    def test_stats_route_not_shadowed_by_alias(self):
        # /api/admin/jobs/stats must still hit the stats route, not {job_id}="stats"
        app, _bjm = _app(None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/admin/jobs/stats")
        assert not (resp.status_code == 404 and "Job not found: stats" in resp.text), (
            "/api/admin/jobs/stats must route to the stats endpoint, not the {job_id} alias"
        )
