"""
Tests for Story #1400 Phase 8: handle_poll_search_job MCP handler.

A thin reader wrapping poll_temporal_job_status: ownership/authorization is
via background_job_manager.get_job_status(job_id, username, is_admin)
(returns None for both not-found AND unauthorized, by design). Composes
read_temporal_snapshot for the PayloadCache side.

TDD: written BEFORE implementation.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from datetime import datetime


def _make_user(role=UserRole.NORMAL_USER) -> User:
    return User(
        username="alice",
        password_hash="irrelevant",
        role=role,
        created_at=datetime.now(),
    )


class TestHandlePollSearchJobMissingParam:
    def test_missing_job_id_returns_error(self):
        from code_indexer.server.mcp.handlers.search import handle_poll_search_job

        response = handle_poll_search_job({}, _make_user())
        payload = json.loads(response["content"][0]["text"])
        assert payload["success"] is False
        assert "job_id" in payload["error"]


class TestHandlePollSearchJobDelegatesToCoreLogic:
    def test_not_found_job_returns_not_found_status(self):
        from code_indexer.server.mcp.handlers.search import handle_poll_search_job
        from code_indexer.server.mcp.handlers import search as search_module

        mock_bgm = MagicMock()
        mock_bgm.get_job_status.return_value = None

        with patch.object(
            search_module._utils.app_module,
            "background_job_manager",
            mock_bgm,
        ):
            response = handle_poll_search_job({"job_id": "job-1"}, _make_user())

        payload = json.loads(response["content"][0]["text"])
        assert payload["status"] == "not_found"
        assert payload["continue_polling"] is False
        mock_bgm.get_job_status.assert_called_once()

    def test_completed_job_forwards_through_snapshot_and_postprocessor(self):
        """Proves the handler actually composes read_temporal_snapshot +
        poll_temporal_job_status, not just short-circuiting to not_found."""
        from code_indexer.server.mcp.handlers import search as search_module

        mock_bgm = MagicMock()
        mock_bgm.get_job_status.return_value = {"status": "completed"}

        fake_snapshot = {
            "results": [
                {"file_path": "a.py", "repository_alias": "my-repo", "metadata": {}}
            ],
            "shards_completed": 2,
            "shards_total": 2,
            "ctx": {"requested_limit": 10},
        }

        with (
            patch.object(
                search_module._utils.app_module,
                "background_job_manager",
                mock_bgm,
            ),
            patch.object(
                search_module,
                "read_temporal_snapshot",
                return_value=fake_snapshot,
            ),
            patch.object(
                search_module,
                "_get_access_filtering_service",
                return_value=MagicMock(
                    is_admin_user=lambda u: False,
                    filter_query_results=lambda results, u: results,
                ),
            ),
        ):
            response = search_module.handle_poll_search_job(
                {"job_id": "job-2"}, _make_user()
            )

        payload = json.loads(response["content"][0]["text"])
        assert payload["status"] == "completed"
        assert payload["continue_polling"] is False
        assert len(payload["results"]) == 1
        assert payload["results"][0]["file_path"] == "a.py"
        assert payload["shards_completed"] == 2
        assert payload["shards_total"] == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
