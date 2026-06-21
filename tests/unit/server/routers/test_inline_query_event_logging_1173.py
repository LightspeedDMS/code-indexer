"""Unit tests for Bug #1173: failed REST searches must NOT write to search_event_log.

The REST semantic_query handler at POST /api/query had its search event enqueue
call in the `finally:` block, meaning it fired on BOTH success and failure paths.
Spec H11 requires that only successful (2xx) searches produce a log row.

These tests exercise the real register_query_routes() closure path — not the MCP
search_code() handler — because the bug lives in inline_query.py.

Tests:
  test_no_enqueue_on_semantic_query_error_not_found - SemanticQueryError('not found') -> 404 -> no enqueue
  test_no_enqueue_on_semantic_query_error_bad_request - SemanticQueryError('no activated') -> 400 -> no enqueue
  test_no_enqueue_on_value_error - ValueError -> 400 -> no enqueue
  test_no_enqueue_on_unexpected_exception - RuntimeError -> 500 -> no enqueue
  test_enqueue_on_success - successful search -> exactly one record enqueued
"""

import pytest
from unittest.mock import MagicMock
from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers.inline_query import register_query_routes
from code_indexer.server.query.semantic_query_manager import SemanticQueryError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(username: str = "alice") -> User:
    user = MagicMock(spec=User)
    user.username = username
    user.role = UserRole.NORMAL_USER
    return user


def _make_writer():
    """Real-enough writer stub that records enqueue calls."""
    records: list = []

    class _W:
        enqueued = records
        backend = None

        def enqueue(self, record):
            records.append(record)

    return _W()


def _qm_result(n: int = 3):
    """Minimal valid result dict for query_user_repositories."""
    return {
        "results": [
            {
                "file_path": f"f{i}.py",
                "score": 0.9,
                "content": "",
                "preview": None,
                "cache_handle": None,
                "total_size": None,
                "source_repo": None,
                "repository_alias": "myrepo",
            }
            for i in range(n)
        ],
        "total_results": n,
        "query_metadata": {},
        "warning": None,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_semantic_query_manager():
    mgr = MagicMock()
    mgr.query_user_repositories.return_value = _qm_result(3)
    return mgr


@pytest.fixture
def mock_activated_repo_manager():
    arm = MagicMock()
    arm.get_activated_repos.return_value = []
    return arm


@pytest.fixture
def writer():
    return _make_writer()


@pytest.fixture
def app_with_routes(
    mock_semantic_query_manager, mock_activated_repo_manager, writer, monkeypatch
):
    """FastAPI app with the query route registered, minimal state wired."""
    fast_app = FastAPI()

    # Minimal app.state attributes consumed by the handler
    fast_app.state.payload_cache = None
    fast_app.state.access_filtering_service = None
    fast_app.state.search_event_log_writer = writer

    register_query_routes(
        fast_app,
        semantic_query_manager=mock_semantic_query_manager,
        activated_repo_manager=mock_activated_repo_manager,
    )

    # Override auth dependency
    from code_indexer.server.auth import dependencies as auth_deps

    fast_app.dependency_overrides[auth_deps.get_current_user] = lambda: _make_user(
        "alice"
    )

    # Patch get_config_service used inside the handler closure
    cfg_mock = MagicMock()
    cfg_mock.get_config.return_value.node_id = "test-node"
    monkeypatch.setattr(
        "code_indexer.server.routers.inline_query.get_config_service",
        lambda: cfg_mock,
        raising=False,
    )

    return fast_app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoEnqueueOnFailure:
    """Bug #1173: search event must NOT be enqueued when the handler raises."""

    def test_no_enqueue_on_semantic_query_error_not_found(
        self,
        app_with_routes,
        mock_semantic_query_manager,
        writer,
    ):
        """SemanticQueryError 'not found' -> 404 -> no log row (Spec H11)."""
        mock_semantic_query_manager.query_user_repositories.side_effect = (
            SemanticQueryError("Repository not found")
        )

        client = TestClient(app_with_routes, raise_server_exceptions=False)
        response = client.post(
            "/api/query",
            json={"query_text": "find me", "repository_alias": "missing-repo"},
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert len(writer.enqueued) == 0, (
            "Bug #1173: event was enqueued for a failed (404) search — "
            "enqueue must not fire in the finally: block"
        )

    def test_no_enqueue_on_semantic_query_error_bad_request(
        self,
        app_with_routes,
        mock_semantic_query_manager,
        writer,
    ):
        """SemanticQueryError 'no activated repositories' -> 400 -> no log row."""
        mock_semantic_query_manager.query_user_repositories.side_effect = (
            SemanticQueryError("no activated repositories for user")
        )

        client = TestClient(app_with_routes, raise_server_exceptions=False)
        response = client.post(
            "/api/query",
            json={"query_text": "find me", "repository_alias": "any"},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert len(writer.enqueued) == 0, (
            "Bug #1173: event was enqueued for a failed (400) search"
        )

    def test_no_enqueue_on_value_error(
        self,
        app_with_routes,
        mock_semantic_query_manager,
        writer,
    ):
        """ValueError -> 400 -> no log row."""
        mock_semantic_query_manager.query_user_repositories.side_effect = ValueError(
            "invalid parameter"
        )

        client = TestClient(app_with_routes, raise_server_exceptions=False)
        response = client.post(
            "/api/query",
            json={"query_text": "find me", "repository_alias": "any"},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert len(writer.enqueued) == 0, (
            "Bug #1173: event was enqueued for a failed (400/ValueError) search"
        )

    def test_no_enqueue_on_unexpected_exception(
        self,
        app_with_routes,
        mock_semantic_query_manager,
        writer,
    ):
        """Unexpected exception -> 500 -> no log row."""
        mock_semantic_query_manager.query_user_repositories.side_effect = RuntimeError(
            "internal crash"
        )

        client = TestClient(app_with_routes, raise_server_exceptions=False)
        response = client.post(
            "/api/query",
            json={"query_text": "find me", "repository_alias": "any"},
        )

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert len(writer.enqueued) == 0, (
            "Bug #1173: event was enqueued for a failed (500) search"
        )

    def test_enqueue_on_success(
        self,
        app_with_routes,
        mock_semantic_query_manager,
        writer,
    ):
        """Sanity: successful search DOES enqueue exactly one record."""
        # Default mock returns 3 results — no side_effect set

        client = TestClient(app_with_routes, raise_server_exceptions=False)
        response = client.post(
            "/api/query",
            json={"query_text": "find me", "repository_alias": "myrepo"},
        )

        assert response.status_code == status.HTTP_200_OK
        assert len(writer.enqueued) == 1, (
            "Expected exactly one SearchEventRecord on success"
        )
        assert writer.enqueued[0].username == "alice"
