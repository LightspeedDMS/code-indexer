"""EVO-64608: a golden-repo registration 500 must log the swallowed exception.

Hard evidence from the running multipod (3h): ~315 `POST /api/admin/golden-repos`
500s with ZERO server-side log lines / tracebacks — the underlying exception is
wrapped in `HTTPException(500, "Failed to submit job: ...")` (inline_admin_ops.py)
and its detail goes only into the response body, never into cidx logs. This test
pins that the route logs the underlying exception with a traceback before raising.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

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


def _app_with_raising_manager(exc: Exception) -> tuple[FastAPI, MagicMock]:
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
    return app, grm


class TestAddGoldenRepo500Logging:
    def test_submit_failure_logs_traceback_and_returns_500(self):
        boom = RuntimeError("submit_job exploded")
        app, _grm = _app_with_raising_manager(boom)
        client = TestClient(app, raise_server_exceptions=False)

        with patch(
            "code_indexer.server.routers.inline_admin_ops.logger"
        ) as mock_logger:
            resp = client.post(
                "/api/admin/golden-repos",
                json={"repo_url": "https://example.com/repo.git", "alias": "evo64608"},
            )

        assert resp.status_code == 500, (
            f"expected 500, got {resp.status_code}: {resp.text}"
        )
        # The swallowed cause must be logged WITH a traceback: logger.exception(...),
        # or logger.error(..., exc_info=...).
        logged_with_traceback = mock_logger.exception.called or any(
            call.kwargs.get("exc_info") for call in mock_logger.error.call_args_list
        )
        assert logged_with_traceback, (
            "EVO-64608: a registration 500 must log the underlying exception with a "
            "traceback (logger.exception / exc_info) so the cause is diagnosable; "
            f"exception calls={mock_logger.exception.call_args_list} "
            f"error calls={mock_logger.error.call_args_list}"
        )
