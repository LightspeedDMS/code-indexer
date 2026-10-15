"""EVO-69818: POST /api/admin/golden-repos maps DuplicateJobError to 409, not 500.

The handler's blanket ``except Exception`` used to swallow DuplicateJobError and
return 500. Sibling handlers (add_golden_repo_index) already return 409.
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
from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.routers.inline_admin_ops import register_admin_ops_routes


def _make_admin() -> User:
    return User(
        username="testadmin",
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _client_with_raising_manager(exc: Exception) -> TestClient:
    app = FastAPI()
    grm = MagicMock()
    grm.add_golden_repo.side_effect = exc
    register_admin_ops_routes(
        app,
        jwt_manager=MagicMock(),
        user_manager=MagicMock(),
        golden_repo_manager=grm,
        background_job_manager=MagicMock(),
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
    return TestClient(app, raise_server_exceptions=False)


_BODY = {"repo_url": "https://example.com/repo.git", "alias": "evo69818"}


class TestAddGoldenRepoDuplicateJob:
    def test_duplicate_job_returns_409(self):
        exc = DuplicateJobError("add_golden_repo", "evo69818", "job-existing-1")
        client = _client_with_raising_manager(exc)

        resp = client.post("/api/admin/golden-repos", json=_BODY)

        assert resp.status_code == 409, f"got {resp.status_code}: {resp.text}"

    def test_other_failure_still_returns_500(self):
        client = _client_with_raising_manager(RuntimeError("boom"))

        resp = client.post("/api/admin/golden-repos", json=_BODY)

        assert resp.status_code == 500
