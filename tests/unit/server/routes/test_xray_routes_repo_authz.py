"""Regression tests: REST POST /api/xray/search and POST
/api/xray/search/batch must enforce repo-level authorization equivalent to
the MCP dispatcher's _check_repository_access(), both for the single search
(its own logic) and for the batch route, which calls the MCP handler
function directly.

Front door: real FastAPI TestClient against xray_routes.router.

Single search (/api/xray/search): a REAL AccessFilteringService backed by a
REAL GroupAccessManager (temp SQLite DB) is wired onto app.state, exactly as
the fixed route now expects.

Batch (/api/xray/search/batch): the route calls handle_xray_search_batch()
directly (mcp/handlers/xray_batch.py), which resolves access_filtering_service
via its own _get_access_filtering_service() seam (extracted for exactly this
kind of test) -- patched here to return a REAL AccessFilteringService/
GroupAccessManager pair, never a bare boolean mock of the access decision.

Scenarios (each endpoint):
- normal_user without the repo's group grant -> 403, job never submitted
- admin user bypasses the check entirely -> 202 (job submitted)
- normal_user WITH the repo's group grant -> 202 (job submitted)
- access_filtering_service missing -> fails closed, never 202
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager
from code_indexer.server.routes.xray_routes import router as xray_router


def _make_user(username: str, role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username=username,
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def group_db_path() -> Iterator[Path]:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


def _build_access_service(
    db_path: Path,
    *,
    granted_username: str,
    granted_repos: list,
    admin_username: str = "admin_user",
) -> AccessFilteringService:
    gam = GroupAccessManager(db_path)
    group = gam.create_group("restricted", "test group")
    gam.assign_user_to_group(granted_username, group.id, assigned_by="test")
    for repo in granted_repos:
        gam.grant_repo_access(repo, group.id, granted_by="test")

    admins_group = gam.get_group_by_name("admins")
    assert admins_group is not None, "bootstrap must create the 'admins' group"
    gam.assign_user_to_group(admin_username, admins_group.id, assigned_by="test")

    return AccessFilteringService(gam)


def _build_app(user: User, access_service) -> FastAPI:
    app = FastAPI()
    app.include_router(xray_router)

    def _unauthenticated():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials",
        )

    app.dependency_overrides[get_current_user] = _unauthenticated
    app.dependency_overrides[get_current_user] = lambda: user
    if access_service is not None:
        app.state.access_filtering_service = access_service
    return app


VALID_SINGLE_BODY = {
    "repository_alias": "example-repo-global",
    "driver_regex": r"prepareStatement",
    "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    Vec::new()\n}\n",
    "search_target": "content",
}

VALID_BATCH_BODY = {
    "repository_alias": "example-repo-global",
    "scans": [
        {
            "driver_regex": r"def ",
            "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }",
            "search_target": "content",
        }
    ],
}


def _patch_single_search_backend():
    return (
        patch(
            "code_indexer.server.routes.xray_routes._resolve_repo_path",
            return_value="/some/repo/path",
        ),
        patch(
            "code_indexer.server.routes.xray_routes._get_background_job_manager",
            return_value=MagicMock(submit_job=MagicMock(return_value="job-1")),
        ),
    )


class TestXraySearchRepoAuthz:
    def test_single_search_denied_for_ungranted_user_returns_403(self, group_db_path):
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path, granted_username=user.username, granted_repos=["other-repo"]
        )
        app = _build_app(user, access_service)
        client = TestClient(app)

        p1, p2 = _patch_single_search_backend()
        with p1, p2 as mock_bjm_factory:
            resp = client.post("/api/xray/search", json=VALID_SINGLE_BODY)

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error_code"] == "access_denied"
        mock_bjm_factory.assert_not_called()

    def test_single_search_admin_bypasses_check(self, group_db_path):
        admin = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _build_access_service(
            group_db_path, granted_username="someone_else", granted_repos=[]
        )
        app = _build_app(admin, access_service)
        client = TestClient(app)

        p1, p2 = _patch_single_search_backend()
        with p1, p2:
            resp = client.post("/api/xray/search", json=VALID_SINGLE_BODY)

        assert resp.status_code == 202

    def test_single_search_granted_user_succeeds(self, group_db_path):
        user = _make_user("granted_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["example-repo"],
        )
        app = _build_app(user, access_service)
        client = TestClient(app)

        p1, p2 = _patch_single_search_backend()
        with p1, p2:
            resp = client.post("/api/xray/search", json=VALID_SINGLE_BODY)

        assert resp.status_code == 202

    def test_single_search_access_filtering_service_unavailable_fails_closed(
        self, group_db_path
    ):
        user = _make_user("some_user")
        app = _build_app(user, access_service=None)
        client = TestClient(app)

        p1, p2 = _patch_single_search_backend()
        with p1, p2:
            resp = client.post("/api/xray/search", json=VALID_SINGLE_BODY)

        assert resp.status_code == 500


class TestXrayBatchSearchRepoAuthz:
    def test_batch_denied_for_ungranted_user_returns_403_no_job_submitted(
        self, group_db_path
    ):
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path, granted_username=user.username, granted_repos=["other-repo"]
        )
        app = _build_app(user, access_service=None)  # not used by batch path
        client = TestClient(app)

        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "batch-job"
        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_access_filtering_service",
                return_value=access_service,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._resolve_repo_path",
                return_value="/some/repo/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_arm_and_grm",
                return_value=(None, None),
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_background_job_manager",
                return_value=mock_bjm,
            ),
        ):
            resp = client.post("/api/xray/search/batch", json=VALID_BATCH_BODY)

        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert detail.get("error") == "access_denied"
        mock_bjm.submit_job.assert_not_called()

    def test_batch_admin_bypasses_check(self, group_db_path):
        admin = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _build_access_service(
            group_db_path, granted_username="someone_else", granted_repos=[]
        )
        app = _build_app(admin, access_service=None)
        client = TestClient(app)

        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "batch-job"
        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_access_filtering_service",
                return_value=access_service,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._resolve_repo_path",
                return_value="/some/repo/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_arm_and_grm",
                return_value=(None, None),
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_cidx_meta_path",
                return_value=Path("/cidx-meta"),
            ),
        ):
            resp = client.post("/api/xray/search/batch", json=VALID_BATCH_BODY)

        assert resp.status_code == 202, resp.text
        mock_bjm.submit_job.assert_called_once()

    def test_batch_granted_user_succeeds(self, group_db_path):
        user = _make_user("granted_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["example-repo"],
        )
        app = _build_app(user, access_service=None)
        client = TestClient(app)

        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "batch-job"
        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_access_filtering_service",
                return_value=access_service,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._resolve_repo_path",
                return_value="/some/repo/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_arm_and_grm",
                return_value=(None, None),
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_cidx_meta_path",
                return_value=Path("/cidx-meta"),
            ),
        ):
            resp = client.post("/api/xray/search/batch", json=VALID_BATCH_BODY)

        assert resp.status_code == 202, resp.text
        mock_bjm.submit_job.assert_called_once()

    def test_batch_access_filtering_service_unavailable_fails_closed(
        self, group_db_path
    ):
        user = _make_user("some_user")
        app = _build_app(user, access_service=None)
        client = TestClient(app)

        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "batch-job"
        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_access_filtering_service",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._resolve_repo_path",
                return_value="/some/repo/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_arm_and_grm",
                return_value=(None, None),
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_background_job_manager",
                return_value=mock_bjm,
            ),
        ):
            resp = client.post("/api/xray/search/batch", json=VALID_BATCH_BODY)

        assert resp.status_code == 500
        mock_bjm.submit_job.assert_not_called()
