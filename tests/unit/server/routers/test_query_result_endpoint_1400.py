"""
Tests for Story #1400 Phase 8: REST GET /api/query/result/{job_id}.

Ownership-checked via background_job_manager.get_job_status (same
not-found/unauthorized-indistinguishable contract as the MCP
poll_search_job tool). Thin reader around poll_temporal_job_status.

TDD: written BEFORE implementation.
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.auth import dependencies
from code_indexer.server.routers.inline_query import register_query_routes


def _make_user() -> User:
    return User(
        username="alice",
        password_hash="irrelevant",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


@pytest.fixture
def app_and_client():
    app = FastAPI()
    mock_semantic_query_manager = MagicMock()
    mock_activated_repo_manager = MagicMock()
    register_query_routes(
        app,
        semantic_query_manager=mock_semantic_query_manager,
        activated_repo_manager=mock_activated_repo_manager,
    )

    app.dependency_overrides[dependencies.get_current_user] = _make_user

    mock_bgm = MagicMock()
    mock_bgm.get_job_status.return_value = None
    app.state.background_job_manager = mock_bgm
    app.state.payload_cache = MagicMock()

    client = TestClient(app)
    return app, client, mock_bgm


class TestGetQueryResultNotFound:
    def test_not_found_job_returns_404(self, app_and_client):
        _app, client, mock_bgm = app_and_client

        response = client.get("/api/query/result/job-1")

        assert response.status_code == 404
        body = response.json()
        assert body["status"] == "not_found"
        mock_bgm.get_job_status.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
