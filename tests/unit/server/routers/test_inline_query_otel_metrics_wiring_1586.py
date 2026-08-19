"""Story #1586 remediation, Finding 2.

The story's own CLAUDE.md documentation claimed "REST reaches this through
the same MCP handler stack -- no second instrumentation point in
inline_query.py". That claim is false: `POST /api/query`'s `semantic_query`
route calls `semantic_query_manager.query_user_repositories()` and a real
`TantivyIndexManager.search()` DIRECTLY, never through AC1's instrumented
`_execute_tracked_search`/`_execute_regex_search` (server/mcp/handlers/
search.py). Confirmed live during manual E2E testing: real REST `/api/query`
calls left `cidx.search.requests` frozen while `cidx.embedding.requests`
rose, proving the search really executed but was invisible to the metric.

These tests drive the REAL route via a real FastAPI TestClient (mirroring
tests/unit/server/routers/test_inline_query_query_tracker_wiring_1458.py's
setup) combined with the real OTEL `active_application_metrics_singleton()`/
`find_metric()` helpers (mirroring
tests/unit/server/telemetry/test_search_handler_wiring_1586.py's
assertions). The FTS tests build and query a REAL on-disk Tantivy index
(same manager class production uses) rather than stubbing
TantivyIndexManager -- no mocking of the metric-recording code under test,
and no mocking of the FTS search engine either.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers.inline_query import register_query_routes

from tests.unit.server.telemetry.otel_test_support import (
    active_application_metrics_singleton,
    find_metric,
)


def _make_user(username: str = "alice") -> User:
    user = MagicMock(spec=User)
    user.username = username
    user.role = UserRole.NORMAL_USER
    return user


def _new_app(semantic_query_manager, activated_repo_manager) -> FastAPI:
    """Shared FastAPI app construction -- registers the real route with the
    given collaborators, a real auth override, and the app.state wiring
    the route reads."""
    fast_app = FastAPI()
    fast_app.state.payload_cache = None
    fast_app.state.access_filtering_service = None
    fast_app.state.search_event_log_writer = None
    fast_app.state.query_tracker = None

    register_query_routes(
        fast_app,
        semantic_query_manager=semantic_query_manager,
        activated_repo_manager=activated_repo_manager,
    )

    from code_indexer.server.auth import dependencies as auth_deps

    fast_app.dependency_overrides[auth_deps.get_current_user] = lambda: _make_user(
        "alice"
    )
    return fast_app


def _qm_success_result() -> dict:
    return {
        "results": [
            {
                "file_path": "src/auth.py",
                "line_number": 1,
                "code_snippet": "def authenticate(): pass",
                "similarity_score": 0.9,
                "repository_alias": "myrepo",
            }
        ],
        "total_results": 1,
        "query_metadata": {},
        "warning": None,
    }


def _run_semantic_query(tmp_path: Path, query_user_repositories_impl, request_json):
    """Build the semantic-mode app, issue one POST /api/query under a real
    OTEL metrics reader, and return (response, reader) for the caller to
    assert on. Shared by the success/error tests below.
    """
    activated_repos_dir = tmp_path / "activated-repos"

    mock_activated_repo_manager = MagicMock()
    mock_activated_repo_manager.get_activated_repos.return_value = []
    mock_activated_repo_manager.activated_repos_dir = str(activated_repos_dir)

    mock_semantic_query_manager = MagicMock()
    mock_semantic_query_manager.query_user_repositories.side_effect = (
        query_user_repositories_impl
    )

    fast_app = _new_app(mock_semantic_query_manager, mock_activated_repo_manager)
    client = TestClient(fast_app, raise_server_exceptions=False)

    with active_application_metrics_singleton() as (_metrics, reader):
        response = client.post("/api/query", json=request_json)
        return response, reader


class TestRestSemanticQueryEmitsSearchMetric:
    """Bug: POST /api/query's default semantic-mode branch calls
    query_user_repositories() directly, bypassing _execute_tracked_search
    entirely -- cidx.search.requests never fires for real REST traffic.
    """

    def test_success_increments_cidx_search_requests(self, tmp_path: Path):
        response, reader = _run_semantic_query(
            tmp_path,
            lambda **kwargs: _qm_success_result(),
            {"query_text": "find auth", "repository_alias": "myrepo"},
        )
        assert response.status_code == 200, response.text

        requests_metric = find_metric(reader, "cidx.search.requests")
        assert requests_metric is not None, (
            "cidx.search.requests was not emitted for a real REST "
            "/api/query semantic-mode call"
        )
        data_points = list(requests_metric.data.data_points)
        assert len(data_points) == 1
        dp = data_points[0]
        assert dp.value == 1
        assert dp.attributes["status"] == "success"

    def test_error_records_status_error(self, tmp_path: Path):
        def _boom(**kwargs):
            raise RuntimeError("backend exploded")

        response, reader = _run_semantic_query(
            tmp_path,
            _boom,
            {"query_text": "find auth", "repository_alias": "myrepo"},
        )
        assert response.status_code == 500, response.text

        requests_metric = find_metric(reader, "cidx.search.requests")
        assert requests_metric is not None, (
            "cidx.search.requests was not emitted on the REST semantic-mode error path"
        )
        dp = list(requests_metric.data.data_points)[0]
        assert dp.attributes["status"] == "error"


def _build_real_tantivy_repo(tmp_path: Path, *, populate: bool) -> Path:
    """Build a real (or deliberately empty) on-disk Tantivy FTS index under
    a fake activated-repo tree, using the REAL TantivyIndexManager -- same
    class production code constructs. Returns activated_repos_dir.
    """
    from code_indexer.services.tantivy_index_manager import TantivyIndexManager

    activated_repos_dir = tmp_path / "activated-repos"
    repo_dir = activated_repos_dir / "alice" / "myrepo"
    index_dir = repo_dir / ".code-indexer" / "tantivy_index"
    index_dir.mkdir(parents=True)

    if populate:
        manager = TantivyIndexManager(index_dir=index_dir)
        manager.initialize_index()
        manager.add_document(
            {
                "path": "src/auth.py",
                "content": "def authenticate(user, password): pass",
                "content_raw": "def authenticate(user, password): pass",
                "identifiers": ["authenticate", "user", "password"],
                "line_start": 1,
                "line_end": 1,
                "language": "python",
            }
        )
        manager.commit()
    # else: leave index_dir present-but-empty -- fts_available is a bare
    # directory-existence check in inline_query.py, so a genuinely-corrupt
    # (never-initialized) index directory reaches open_for_search() and
    # trips a REAL FileNotFoundError/RuntimeError from the tantivy library
    # itself -- a real error path, not an injected one.

    return activated_repos_dir


def _run_fts_query(tmp_path: Path, *, populate: bool, request_json):
    """Build the FTS-mode app against a real (or deliberately empty)
    on-disk Tantivy index, issue one POST /api/query under a real OTEL
    metrics reader, and return (response, reader). Shared by the
    success/error tests below.
    """
    activated_repos_dir = _build_real_tantivy_repo(tmp_path, populate=populate)

    mock_activated_repo_manager = MagicMock()
    mock_activated_repo_manager.activated_repos_dir = str(activated_repos_dir)
    mock_activated_repo_manager.list_activated_repositories.return_value = [
        {"user_alias": "myrepo", "username": "alice", "is_global": False}
    ]
    mock_semantic_query_manager = MagicMock()

    fast_app = _new_app(mock_semantic_query_manager, mock_activated_repo_manager)
    client = TestClient(fast_app, raise_server_exceptions=False)

    with active_application_metrics_singleton() as (_metrics, reader):
        response = client.post("/api/query", json=request_json)
        return response, reader


class TestRestFtsQueryEmitsFtsMetric:
    """Bug: POST /api/query's search_mode='fts' branch reads a real Tantivy
    index directly, bypassing _execute_regex_search entirely --
    cidx.fts.requests never fires for real REST FTS traffic.
    """

    def test_success_increments_cidx_fts_requests(self, tmp_path: Path):
        response, reader = _run_fts_query(
            tmp_path,
            populate=True,
            request_json={
                "query_text": "authenticate",
                "repository_alias": "myrepo",
                "search_mode": "fts",
            },
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["fts_results"]) >= 1, (
            "real Tantivy index produced no match for a real query -- "
            "test setup is broken, not the production code"
        )

        requests_metric = find_metric(reader, "cidx.fts.requests")
        assert requests_metric is not None, (
            "cidx.fts.requests was not emitted for a real REST /api/query fts-mode call"
        )
        dp = list(requests_metric.data.data_points)[0]
        assert dp.value == 1
        assert dp.attributes["status"] == "success"

    def test_error_records_status_error(self, tmp_path: Path):
        # Deliberately un-populated index directory: fts_available only
        # checks directory existence, so this reaches the real
        # TantivyIndexManager.open_for_search() call, which raises a real
        # error against a never-initialized index.
        response, reader = _run_fts_query(
            tmp_path,
            populate=False,
            request_json={
                "query_text": "authenticate",
                "repository_alias": "myrepo",
                "search_mode": "fts",
            },
        )
        assert response.status_code == 500, response.text

        requests_metric = find_metric(reader, "cidx.fts.requests")
        assert requests_metric is not None, (
            "cidx.fts.requests was not emitted on the REST fts-mode error path"
        )
        dp = list(requests_metric.data.data_points)[0]
        assert dp.attributes["status"] == "error"
