"""Unit tests for Bug #1173 / Story #2117: what a REST search writes to the
search event log.

Bug #1173 stopped failed searches from being logged as successes. Story #2117
(owner ruling) replaced "a failed search writes nothing" with: a failed REST
search enqueues exactly ONE record with outcome "failure" and the exception's
class as error_class (never its message); a successful search enqueues one
success record.

These tests exercise the real register_query_routes() closure path -- not the
MCP search_code() handler -- because the door lives in inline_query.py.
"""

import pytest
from unittest.mock import MagicMock
from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers.inline_query import register_query_routes
from code_indexer.server.query.semantic_query_manager import (
    SearchRepositoryNotFoundError,
    SearchRequestError,
    SemanticQueryError,
)


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


class TestRejectedRequestEnqueuesNothing:
    """Story #2117: a request rejected before its repository resolved (an
    unknown repository, a refused parameter) is not a search: no record."""

    @pytest.mark.parametrize(
        "exc, expected_status",
        [
            (SearchRepositoryNotFoundError("missing-repo"), status.HTTP_404_NOT_FOUND),
            (
                SearchRequestError("no activated repositories for user"),
                status.HTTP_400_BAD_REQUEST,
            ),
        ],
    )
    def test_rejected_request_enqueues_nothing(
        self,
        app_with_routes,
        mock_semantic_query_manager,
        writer,
        exc,
        expected_status,
    ):
        mock_semantic_query_manager.query_user_repositories.side_effect = exc

        client = TestClient(app_with_routes, raise_server_exceptions=False)
        response = client.post(
            "/api/query",
            json={"query_text": "find me", "repository_alias": "missing-repo"},
        )

        assert response.status_code == expected_status
        assert writer.enqueued == []


class TestFailedSearchEnqueuesOneFailureRecord:
    """Story #2117: a REST search that raises enqueues exactly one failure
    record; error_class is the exception's class, never its message."""

    @pytest.mark.parametrize(
        "exc, expected_status",
        [
            (
                SemanticQueryError("Repository not found"),
                status.HTTP_500_INTERNAL_SERVER_ERROR,
            ),
            (
                SemanticQueryError("no activated repositories for user"),
                status.HTTP_500_INTERNAL_SERVER_ERROR,
            ),
            (ValueError("invalid parameter"), status.HTTP_500_INTERNAL_SERVER_ERROR),
            (RuntimeError("internal crash"), status.HTTP_500_INTERNAL_SERVER_ERROR),
        ],
    )
    def test_failed_search_enqueues_one_failure_record(
        self,
        app_with_routes,
        mock_semantic_query_manager,
        writer,
        exc,
        expected_status,
    ):
        mock_semantic_query_manager.query_user_repositories.side_effect = exc

        client = TestClient(app_with_routes, raise_server_exceptions=False)
        response = client.post(
            "/api/query",
            json={"query_text": "find me", "repository_alias": "missing-repo"},
        )

        assert response.status_code == expected_status
        [record] = writer.enqueued
        assert (record.tool, record.outcome, record.error_class) == (
            "POST /api/query",
            "failure",
            type(exc).__name__,
        )
        assert record.result_count == 0


class TestSuccessfulSearch:
    def test_enqueue_on_success(
        self,
        app_with_routes,
        mock_semantic_query_manager,
        writer,
    ):
        """A successful search enqueues exactly one success record."""
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
        assert writer.enqueued[0].outcome == "success"
