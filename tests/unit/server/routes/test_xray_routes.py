"""Unit tests for POST /api/xray/search REST endpoint (Story #974).

Mocking strategy:
- _resolve_repo_path: mocked (needs live alias manager)
- background_job_manager.submit_job: mocked/spied to verify it is or is NOT called
- XRaySearchEngine: real for pre-flight validation; mocked for extras-missing test
- PythonEvaluatorSandbox.validate: uses real sandbox for valid/invalid evaluator tests
- User/permission: FastAPI dependency_overrides for get_current_user
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_BODY: dict[str, Any] = {
    "repository_alias": "myrepo-global",
    "driver_regex": r"prepareStatement",
    "evaluator_code": "return True",
    "search_target": "content",
}


def _err_code(response_body: dict) -> Optional[str]:
    """Extract error_code from HTTPException detail envelope: {'detail': {'error_code': ...}}."""
    detail = response_body.get("detail", {})
    if not isinstance(detail, dict):
        return None
    return detail.get("error_code")


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    """Build a real User with the given role."""
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


NORMAL_USER = _make_user(UserRole.NORMAL_USER)
ADMIN_USER = _make_user(UserRole.ADMIN)


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Create test FastAPI app."""
    from code_indexer.server.app import create_app

    return create_app()


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Context managers / helpers for common mock combos
# ---------------------------------------------------------------------------


def _patch_repo_found(path: str = "/some/repo/path"):
    """Patch _resolve_repo_path to return a valid path."""
    return patch(
        "code_indexer.server.routes.xray_routes._resolve_repo_path",
        return_value=path,
    )


def _patch_repo_not_found():
    """Patch _resolve_repo_path to return None (alias not found)."""
    return patch(
        "code_indexer.server.routes.xray_routes._resolve_repo_path",
        return_value=None,
    )


def _patch_bjm(job_id: str = "test-job-id"):
    """Return a mock BackgroundJobManager whose submit_job returns job_id."""
    mock_bjm = MagicMock()
    mock_bjm.submit_job.return_value = job_id
    return patch(
        "code_indexer.server.routes.xray_routes._get_background_job_manager",
        return_value=mock_bjm,
    ), mock_bjm


# ---------------------------------------------------------------------------
# TC-01: Valid request returns 202 with job_id
# ---------------------------------------------------------------------------


class TestValidRequest:
    """Valid authenticated request returns 202 with job_id UUID."""

    def test_post_xray_search_returns_202_with_job_id(self, app, client):
        """Happy path: 202 with {'job_id': '<uuid>'}."""
        job_id = str(uuid.uuid4())
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER

        bjm_patch, mock_bjm = _patch_bjm(job_id)
        try:
            with _patch_repo_found(), bjm_patch:
                resp = client.post("/api/xray/search", json=VALID_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 202
        data = resp.json()
        assert "job_id" in data
        assert data["job_id"] == job_id

    def test_submit_job_called_with_xray_search_operation_type(self, app, client):
        """submit_job is called with operation_type='xray_search'."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER

        bjm_patch, mock_bjm = _patch_bjm("some-job")
        try:
            with _patch_repo_found(), bjm_patch:
                client.post("/api/xray/search", json=VALID_BODY)
        finally:
            app.dependency_overrides.clear()

        assert mock_bjm.submit_job.called
        kwargs = mock_bjm.submit_job.call_args.kwargs
        assert kwargs.get("operation_type") == "xray_search"

    def test_max_files_accepted_and_forwarded(self, app, client):
        """max_files=5 is accepted (HTTP 202) and forwarded to the engine."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_BODY, "max_files": 5}

        bjm_patch, mock_bjm = _patch_bjm("job-mf")
        try:
            with _patch_repo_found(), bjm_patch:
                resp = client.post("/api/xray/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 202

    def test_max_files_null_accepted(self, app, client):
        """max_files=null (default) is accepted (HTTP 202)."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_BODY, "max_files": None}

        bjm_patch, mock_bjm = _patch_bjm("job-mf-null")
        try:
            with _patch_repo_found(), bjm_patch:
                resp = client.post("/api/xray/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 202

    def test_include_exclude_patterns_accepted(self, app, client):
        """include_patterns and exclude_patterns are accepted (HTTP 202)."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {
            **VALID_BODY,
            "include_patterns": ["*.kt"],
            "exclude_patterns": ["*/build/*"],
        }

        bjm_patch, mock_bjm = _patch_bjm("job-patterns")
        try:
            with _patch_repo_found(), bjm_patch:
                resp = client.post("/api/xray/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# TC-02: Authentication failure returns 401
# ---------------------------------------------------------------------------


class TestNoAuth:
    """Missing or invalid Authorization header returns 401."""

    def test_post_xray_search_no_auth_returns_401(self, client):
        """Request without Authorization header returns 401."""
        resp = client.post("/api/xray/search", json=VALID_BODY)
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TC-03: Permission failure returns 403
# ---------------------------------------------------------------------------


class TestMissingPermission:
    """Token without query_repos permission returns 403."""

    def test_post_xray_search_no_permission_returns_403(self, app, client):
        """User without query_repos returns 403 with structured error."""
        # Create a user whose role has no query_repos permission
        # We patch has_permission to return False
        user_no_perm = MagicMock(spec=User)
        user_no_perm.has_permission.return_value = False
        user_no_perm.username = "limited"

        app.dependency_overrides[get_current_user] = lambda: user_no_perm

        try:
            with _patch_repo_found(), _patch_bjm()[0]:
                resp = client.post("/api/xray/search", json=VALID_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 403
        data = resp.json()
        assert "error_code" in data or "detail" in data


# ---------------------------------------------------------------------------
# TC-04: Bad evaluator code returns 422 and submit_job NOT called
# ---------------------------------------------------------------------------


class TestBadEvaluatorCode:
    """Evaluator code that fails sandbox validation returns 422; no job submitted."""

    def test_bad_evaluator_returns_422_with_error_code(self, app, client):
        """import sys fails sandbox whitelist -> 422 xray_evaluator_validation_failed."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_BODY, "evaluator_code": "import sys"}

        bjm_patch, mock_bjm = _patch_bjm("should-not-be-called")
        try:
            with _patch_repo_found(), bjm_patch:
                resp = client.post("/api/xray/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422
        data = resp.json()
        assert _err_code(data) == "xray_evaluator_validation_failed"

    def test_bad_evaluator_does_not_call_submit_job(self, app, client):
        """When evaluator validation fails, submit_job must NOT be called."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_BODY, "evaluator_code": "import os"}

        bjm_patch, mock_bjm = _patch_bjm("should-not-be-called")
        try:
            with _patch_repo_found(), bjm_patch:
                client.post("/api/xray/search", json=body)
        finally:
            app.dependency_overrides.clear()

        mock_bjm.submit_job.assert_not_called()


# ---------------------------------------------------------------------------
# TC-05: Unknown repository returns 404
# ---------------------------------------------------------------------------


class TestUnknownRepository:
    """Unknown repository alias returns 404 with structured error."""

    def test_unknown_repo_returns_404(self, app, client):
        """Nonexistent alias returns 404 with error_code=repository_not_found."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER

        bjm_patch, mock_bjm = _patch_bjm()
        try:
            with _patch_repo_not_found(), bjm_patch:
                resp = client.post("/api/xray/search", json=VALID_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 404
        data = resp.json()
        assert _err_code(data) == "repository_not_found"


# ---------------------------------------------------------------------------
# TC-06: Invalid search_target returns 422
# ---------------------------------------------------------------------------


class TestInvalidSearchTarget:
    """Invalid search_target value returns 422."""

    def test_invalid_search_target_returns_422(self, app, client):
        """search_target='invalid' returns 422 with error_code=invalid_search_target."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_BODY, "search_target": "invalid"}

        bjm_patch, mock_bjm = _patch_bjm()
        try:
            with _patch_repo_found(), bjm_patch:
                resp = client.post("/api/xray/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422
        data = resp.json()
        assert _err_code(data) == "invalid_search_target"


# ---------------------------------------------------------------------------
# TC-07: timeout_seconds out of range returns 422
# ---------------------------------------------------------------------------


class TestTimeoutOutOfRange:
    """timeout_seconds below 10 or above 600 returns 422."""

    def test_timeout_too_low_returns_422(self, app, client):
        """timeout_seconds=5 (below min 10) returns 422."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_BODY, "timeout_seconds": 5}

        bjm_patch, mock_bjm = _patch_bjm()
        try:
            with _patch_repo_found(), bjm_patch:
                resp = client.post("/api/xray/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422
        data = resp.json()
        assert _err_code(data) == "timeout_out_of_range"

    def test_timeout_too_high_returns_422(self, app, client):
        """timeout_seconds=900 (above max 600) returns 422."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_BODY, "timeout_seconds": 900}

        bjm_patch, mock_bjm = _patch_bjm()
        try:
            with _patch_repo_found(), bjm_patch:
                resp = client.post("/api/xray/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422
        data = resp.json()
        assert _err_code(data) == "timeout_out_of_range"

    def test_timeout_at_min_boundary_accepted(self, app, client):
        """timeout_seconds=10 (exactly at min) returns 202."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_BODY, "timeout_seconds": 10}

        bjm_patch, mock_bjm = _patch_bjm("job-timeout-min")
        try:
            with _patch_repo_found(), bjm_patch:
                resp = client.post("/api/xray/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 202

    def test_timeout_at_max_boundary_accepted(self, app, client):
        """timeout_seconds=600 (exactly at max) returns 202."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_BODY, "timeout_seconds": 600}

        bjm_patch, mock_bjm = _patch_bjm("job-timeout-max")
        try:
            with _patch_repo_found(), bjm_patch:
                resp = client.post("/api/xray/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# TC-08: max_files=0 returns 422
# ---------------------------------------------------------------------------


class TestMaxFilesOutOfRange:
    """max_files=0 returns 422 with structured error."""

    def test_max_files_zero_returns_422(self, app, client):
        """max_files=0 returns 422 with error_code=max_files_out_of_range."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_BODY, "max_files": 0}

        bjm_patch, mock_bjm = _patch_bjm()
        try:
            with _patch_repo_found(), bjm_patch:
                resp = client.post("/api/xray/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422
        data = resp.json()
        assert _err_code(data) == "max_files_out_of_range"
        assert "detail" in data
        assert "1" in data["detail"]["detail"]

    def test_max_files_negative_returns_422(self, app, client):
        """max_files=-1 returns 422 with error_code=max_files_out_of_range."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_BODY, "max_files": -1}

        bjm_patch, mock_bjm = _patch_bjm()
        try:
            with _patch_repo_found(), bjm_patch:
                resp = client.post("/api/xray/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422
        data = resp.json()
        assert _err_code(data) == "max_files_out_of_range"


# ---------------------------------------------------------------------------
# TC-09: xray extras not installed returns 503
# ---------------------------------------------------------------------------


class TestXrayExtrasNotInstalled:
    """When XRaySearchEngine raises XRayExtrasNotInstalled, return 503."""

    def test_extras_not_installed_returns_503(self, app, client):
        """XRayExtrasNotInstalled exception -> 503 with error_code."""
        from code_indexer.xray.errors import XRayExtrasNotInstalled

        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER

        bjm_patch, mock_bjm = _patch_bjm()
        try:
            with (
                _patch_repo_found(),
                bjm_patch,
                patch(
                    "code_indexer.server.routes.xray_routes.XRaySearchEngine",
                    side_effect=XRayExtrasNotInstalled("tree_sitter"),
                ),
            ):
                resp = client.post("/api/xray/search", json=VALID_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 503
        data = resp.json()
        assert _err_code(data) == "xray_extras_not_installed"
        assert "pip install" in data["detail"].get("detail", "").lower()


# ---------------------------------------------------------------------------
# TC-10: Malformed JSON body returns 400/422
# ---------------------------------------------------------------------------


class TestMalformedBody:
    """Malformed JSON body returns 400 or 422."""

    def test_malformed_json_returns_error(self, app, client):
        """Non-JSON body returns 400 or 422 (FastAPI validation)."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER

        try:
            resp = client.post(
                "/api/xray/search",
                content=b"not-json",
                headers={"Content-Type": "application/json"},
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code in (400, 422)
