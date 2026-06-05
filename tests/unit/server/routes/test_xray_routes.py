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
    "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    Vec::new()\n}\n",
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


# ---------------------------------------------------------------------------
# REST tests: POST /api/xray/search/batch (Story #1055)
# ---------------------------------------------------------------------------

VALID_EVAL = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }"

VALID_BATCH_BODY: dict[str, Any] = {
    "repository_alias": "myrepo-global",
    "scans": [
        {
            "driver_regex": r"def ",
            "evaluator_code": VALID_EVAL,
            "search_target": "content",
        }
    ],
}


def _batch_err_code(response_body: dict) -> Optional[str]:
    """Extract error from batch HTTPException detail envelope."""
    detail = response_body.get("detail", {})
    if isinstance(detail, dict):
        return detail.get("error") or detail.get("error_code")
    return None


def _patch_batch_bjm(job_id: str = "batch-job-id"):
    """Mock BackgroundJobManager in xray_batch module."""
    mock_bjm = MagicMock()
    mock_bjm.submit_job.return_value = job_id
    return patch(
        "code_indexer.server.mcp.handlers.xray_batch._get_background_job_manager",
        return_value=mock_bjm,
    ), mock_bjm


def _patch_batch_repo_found(path: str = "/some/repo/path"):
    return patch(
        "code_indexer.server.mcp.handlers.xray_batch._resolve_repo_path",
        return_value=path,
    )


def _patch_batch_repo_not_found():
    return patch(
        "code_indexer.server.mcp.handlers.xray_batch._resolve_repo_path",
        return_value=None,
    )


def _patch_batch_arm_grm():
    return patch(
        "code_indexer.server.mcp.handlers.xray_batch._get_arm_and_grm",
        return_value=(None, None),
    )


def _patch_batch_cidx_meta():
    from pathlib import Path

    return patch(
        "code_indexer.server.mcp.handlers.xray_batch._get_cidx_meta_path",
        return_value=Path("/cidx-meta"),
    )


class TestXrayBatchRestEndpoint:
    """REST tests for POST /api/xray/search/batch (Story #1055)."""

    # ------------------------------------------------------------------
    # Happy path: 202 with job_id
    # ------------------------------------------------------------------

    def test_batch_post_returns_202_with_job_id(self, app, client):
        """Valid request returns HTTP 202 with {'job_id': '<id>'}."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        bjm_patch, mock_bjm = _patch_batch_bjm("batch-1")

        try:
            with (
                _patch_batch_repo_found(),
                _patch_batch_arm_grm(),
                _patch_batch_bjm("batch-1")[0],
                _patch_batch_cidx_meta(),
            ):
                resp = client.post("/api/xray/search/batch", json=VALID_BATCH_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 202
        assert resp.json()["job_id"] == "batch-1"

    def test_batch_operation_type_is_xray_search_batch(self, app, client):
        """submit_job is called with operation_type='xray_search_batch'."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        bjm_patch, mock_bjm = _patch_batch_bjm("batch-op")

        try:
            with (
                _patch_batch_repo_found(),
                _patch_batch_arm_grm(),
                bjm_patch,
                _patch_batch_cidx_meta(),
            ):
                client.post("/api/xray/search/batch", json=VALID_BATCH_BODY)
        finally:
            app.dependency_overrides.clear()

        assert mock_bjm.submit_job.called
        kwargs = mock_bjm.submit_job.call_args.kwargs
        assert kwargs.get("operation_type") == "xray_search_batch"

    def test_batch_repo_alias_none_in_submit_job(self, app, client):
        """submit_job is called with repo_alias=None (no per-repo dedup)."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        bjm_patch, mock_bjm = _patch_batch_bjm("batch-alias-none")

        try:
            with (
                _patch_batch_repo_found(),
                _patch_batch_arm_grm(),
                bjm_patch,
                _patch_batch_cidx_meta(),
            ):
                client.post("/api/xray/search/batch", json=VALID_BATCH_BODY)
        finally:
            app.dependency_overrides.clear()

        kwargs = mock_bjm.submit_job.call_args.kwargs
        assert kwargs.get("repo_alias") is None

    # ------------------------------------------------------------------
    # Auth: 401 (no auth header) — FastAPI dependency raises 401
    # ------------------------------------------------------------------

    def test_batch_no_auth_returns_401(self, client):
        """Request without Authorization returns 401."""
        resp = client.post("/api/xray/search/batch", json=VALID_BATCH_BODY)
        assert resp.status_code == 401

    # ------------------------------------------------------------------
    # Auth: 403 (missing permission)
    # ------------------------------------------------------------------

    def test_batch_no_permission_returns_403(self, app, client):
        """User without query_repos returns 403."""
        user_no_perm = MagicMock(spec=User)
        user_no_perm.has_permission.return_value = False
        user_no_perm.username = "limited"
        app.dependency_overrides[get_current_user] = lambda: user_no_perm

        try:
            resp = client.post("/api/xray/search/batch", json=VALID_BATCH_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 403
        assert _batch_err_code(resp.json()) == "auth_required"

    # ------------------------------------------------------------------
    # Validation: 422 for structural errors
    # ------------------------------------------------------------------

    def test_batch_missing_alias_returns_422(self, app, client):
        """Missing repository_alias returns 422 alias_required."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_BATCH_BODY}
        del body["repository_alias"]

        try:
            # Pydantic will reject the body itself (missing required field)
            resp = client.post("/api/xray/search/batch", json=body)
        finally:
            app.dependency_overrides.clear()

        # Pydantic validates repository_alias as required; 422 from FastAPI
        assert resp.status_code == 422

    def test_batch_empty_alias_string_returns_422(self, app, client):
        """Empty repository_alias string returns 422 alias_required."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_BATCH_BODY, "repository_alias": ""}

        try:
            with _patch_batch_arm_grm():
                resp = client.post("/api/xray/search/batch", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422
        assert _batch_err_code(resp.json()) == "alias_required"

    def test_batch_missing_scans_returns_422(self, app, client):
        """Missing scans field returns 422 (Pydantic required field)."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {"repository_alias": "myrepo-global"}

        try:
            resp = client.post("/api/xray/search/batch", json=body)
        finally:
            app.dependency_overrides.clear()

        # Pydantic validates scans as required; 422 from FastAPI
        assert resp.status_code == 422

    def test_batch_empty_scans_returns_422(self, app, client):
        """Empty scans list returns 422 scans_required."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_BATCH_BODY, "scans": []}

        try:
            with _patch_batch_arm_grm():
                resp = client.post("/api/xray/search/batch", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422
        assert _batch_err_code(resp.json()) == "scans_required"

    def test_batch_too_many_repositories_returns_422(self, app, client):
        """51 aliases returns 422 too_many_repositories."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {
            **VALID_BATCH_BODY,
            "repository_alias": [f"repo-{i}" for i in range(51)],
        }

        try:
            with _patch_batch_arm_grm():
                resp = client.post("/api/xray/search/batch", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422
        assert _batch_err_code(resp.json()) == "too_many_repositories"

    def test_batch_too_many_scans_returns_422(self, app, client):
        """51 scans returns 422 too_many_scans."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        scans = [{"driver_regex": "x", "search_target": "content"} for _ in range(51)]
        body = {**VALID_BATCH_BODY, "scans": scans}

        try:
            with _patch_batch_arm_grm():
                resp = client.post("/api/xray/search/batch", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422
        assert _batch_err_code(resp.json()) == "too_many_scans"

    def test_batch_bad_evaluator_returns_422(self, app, client):
        """Invalid evaluator code returns 422 xray_evaluator_validation_failed."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {
            **VALID_BATCH_BODY,
            "scans": [
                {
                    "driver_regex": "x",
                    "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { unsafe { vec![] } }",
                    "search_target": "content",
                }
            ],
        }

        try:
            with _patch_batch_arm_grm():
                resp = client.post("/api/xray/search/batch", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422
        assert _batch_err_code(resp.json()) == "xray_evaluator_validation_failed"

    def test_batch_mutually_exclusive_params_returns_422(self, app, client):
        """Both evaluator_code and pattern_name returns 422 mutually_exclusive_params."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {
            **VALID_BATCH_BODY,
            "scans": [
                {
                    "driver_regex": "x",
                    "evaluator_code": VALID_EVAL,
                    "pattern_name": "catch-rethrow",
                }
            ],
        }

        try:
            with _patch_batch_arm_grm():
                resp = client.post("/api/xray/search/batch", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422
        assert _batch_err_code(resp.json()) == "mutually_exclusive_params"

    def test_batch_timeout_out_of_range_returns_422(self, app, client):
        """timeout_seconds=5 returns 422 timeout_out_of_range."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_BATCH_BODY, "timeout_seconds": 5}

        try:
            with _patch_batch_arm_grm():
                resp = client.post("/api/xray/search/batch", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422
        assert _batch_err_code(resp.json()) == "timeout_out_of_range"

    # ------------------------------------------------------------------
    # Repo not found: 404
    # ------------------------------------------------------------------

    def test_batch_unknown_repo_returns_404(self, app, client):
        """Unknown alias returns 404 no_repositories_resolved."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER

        try:
            with _patch_batch_repo_not_found(), _patch_batch_arm_grm():
                resp = client.post("/api/xray/search/batch", json=VALID_BATCH_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 404
        assert _batch_err_code(resp.json()) == "no_repositories_resolved"

    # ------------------------------------------------------------------
    # Inline-result parity: 200 with full result when job completes inline
    # ------------------------------------------------------------------

    def test_batch_inline_result_returns_200_with_full_result(self, app, client):
        """When await_seconds > 0 and job completes inline, returns HTTP 200
        with the full batch result dict (not job_id).  This is the REST parity
        fix for Story #1055: the REST route must not KeyError on resp_data['job_id']
        when the MCP handler returns an inline result.
        """
        import json

        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER

        inline_result = {
            "matches": [{"file_path": "a.py", "line_number": 1}],
            "errors": [],
            "evaluation_errors": [],
            "total_repos": 1,
            "total_scans": 1,
            "total_cells": 1,
            "repos_completed": 1,
            "partial": False,
            "timeout": False,
            "cancelled": False,
            "truncated": False,
            "has_more": False,
            "cache_handle": None,
        }

        # Patch handle_xray_search_batch to return the MCP envelope wrapping
        # the inline result directly (simulates await_seconds completing).
        mcp_envelope = {
            "content": [{"type": "text", "text": json.dumps(inline_result)}]
        }

        body = {**VALID_BATCH_BODY, "await_seconds": 5.0}

        try:
            # The route imports handle_xray_search_batch locally at call time,
            # so patch it in the xray_batch module namespace.
            with patch(
                "code_indexer.server.mcp.handlers.xray_batch.handle_xray_search_batch",
                return_value=mcp_envelope,
            ):
                resp = client.post("/api/xray/search/batch", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        data = resp.json()
        assert "matches" in data
        assert "errors" in data
        assert data["total_repos"] == 1
        assert "job_id" not in data
