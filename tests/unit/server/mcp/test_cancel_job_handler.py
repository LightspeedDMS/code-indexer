"""Unit tests for cancel_job MCP handler."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import HANDLER_REGISTRY


def _make_user(role: UserRole = UserRole.NORMAL_USER, username: str = "alice") -> User:
    """Build a real User with the given role."""
    return User(
        username=username,
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse(result: dict) -> dict:
    """Unwrap MCP content envelope."""
    return json.loads(result["content"][0]["text"])  # type: ignore[no-any-return]


class TestCancelJobHandler:
    def test_cancel_job_handler_registered(self):
        assert "cancel_job" in HANDLER_REGISTRY
        assert callable(HANDLER_REGISTRY["cancel_job"])

    def test_cancel_job_success(self):
        user = _make_user()
        mock_bjm = MagicMock()
        mock_bjm.cancel_job.return_value = {
            "success": True,
            "message": "Job cancelled successfully",
        }
        handler = HANDLER_REGISTRY["cancel_job"]

        with patch(
            "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
            return_value=mock_bjm,
        ):
            result = handler({"job_id": "test-job-123"}, user)

        data = _parse(result)
        assert data["success"] is True

    def test_cancel_job_missing_job_id(self):
        user = _make_user()
        handler = HANDLER_REGISTRY["cancel_job"]

        result = handler({}, user)

        data = _parse(result)
        assert data["success"] is False
        assert "job_id" in data["message"].lower()

    def test_cancel_job_not_found(self):
        user = _make_user()
        mock_bjm = MagicMock()
        mock_bjm.cancel_job.return_value = {
            "success": False,
            "message": "Job not found or not authorized",
        }
        handler = HANDLER_REGISTRY["cancel_job"]

        with patch(
            "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
            return_value=mock_bjm,
        ):
            result = handler({"job_id": "nonexistent"}, user)

        data = _parse(result)
        assert data["success"] is False

    def test_cancel_job_already_completed(self):
        user = _make_user()
        mock_bjm = MagicMock()
        mock_bjm.cancel_job.return_value = {
            "success": False,
            "message": "Cannot cancel job in completed status",
        }
        handler = HANDLER_REGISTRY["cancel_job"]

        with patch(
            "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
            return_value=mock_bjm,
        ):
            result = handler({"job_id": "done-job"}, user)

        data = _parse(result)
        assert data["success"] is False
        assert "completed" in data["message"].lower()

    def test_cancel_job_authorization_non_admin(self):
        user = _make_user(role=UserRole.NORMAL_USER, username="alice")
        mock_bjm = MagicMock()
        mock_bjm.cancel_job.return_value = {"success": True, "message": "ok"}
        handler = HANDLER_REGISTRY["cancel_job"]

        with patch(
            "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
            return_value=mock_bjm,
        ):
            handler({"job_id": "j1"}, user)

        mock_bjm.cancel_job.assert_called_once_with("j1", "alice", False)

    def test_cancel_job_authorization_admin(self):
        user = _make_user(role=UserRole.ADMIN, username="admin")
        mock_bjm = MagicMock()
        mock_bjm.cancel_job.return_value = {"success": True, "message": "ok"}
        handler = HANDLER_REGISTRY["cancel_job"]

        with patch(
            "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
            return_value=mock_bjm,
        ):
            handler({"job_id": "j1"}, user)

        mock_bjm.cancel_job.assert_called_once_with("j1", "admin", True)
