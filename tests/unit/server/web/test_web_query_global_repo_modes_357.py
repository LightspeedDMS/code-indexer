"""The Web UI Query page honours search mode and filters for GLOBAL repos.

Front door: the real app (``create_app`` over an isolated server home) and
its two query handlers -- the htmx partial (POST /admin/partials/query-results)
and the full page (POST /admin/query) -- with real Web sessions.

Behind them every query service is real (tests/unit/server/query/
query_repo_access_env.py): the SemanticQueryManager, the global repo
registry, alias pointers, ActivatedRepoManager and AccessFilteringService,
plus real Tantivy FTS indexes on disk. Only the external embedding + HNSW
boundary (SemanticSearchService.search_repository_path) returns fixed rows
and records which repository paths it searched.
"""

from __future__ import annotations

import importlib
import logging
import re
import shutil
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import patch

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from code_indexer.config import ConfigManager
from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.services import config_service as config_service_module
from code_indexer.server.web import auth as web_auth
from code_indexer.server.web import routes as web_routes
from code_indexer.services.tantivy_index_manager import TantivyIndexManager
from tests.unit.server._isolated_app import isolated_app
from tests.unit.server.query.query_repo_access_env import (
    ADMIN,
    ALL_REPOS,
    GRANTED_REPO,
    SEARCH_BOUNDARY,
    UNGRANTED_REPO,
    USER,
    QueryAccessEnv,
    build_server_db_template,
    global_alias,
)

PASSWORD = "Example-Web-Query-Passw0rd!"
PARTIAL = "/admin/partials/query-results"
FULL_PAGE = "/admin/query"
HANDLERS = (PARTIAL, FULL_PAGE)

GRANTED_GLOBAL = global_alias(GRANTED_REPO)
UNGRANTED_GLOBAL = global_alias(UNGRANTED_REPO)
OWN_ACTIVATION = "my-web-repo"

# Latin-1 source: the bytes on disk are not UTF-8; the FTS index holds the
# decoded text, so FTS returns it as the Tantivy snippet.
LATIN1_TEXT = "def legacy_greeting():\n    return 'café crème'\n"

# The module-scoped real create_app() and FTS index templates (~10 s alone)
# are paid by whichever test runs first, slower under parallel gate load.
pytestmark = pytest.mark.timeout(45)

_BADGE = re.compile(r'class="search-mode-badge search-mode-([a-z]+)"')


def _auth_path(repo: str) -> str:
    return f"src/{repo}_auth.py"


def _legacy_path(repo: str) -> str:
    return f"src/{repo}_legacy.py"


def _semantic_row_path(repo: str, i: int = 0) -> str:
    """A row only the faked semantic boundary returns."""
    return f"src/{repo}_{i}.py"


def _add_doc(
    manager: TantivyIndexManager, path: str, body: str, ids: List[str]
) -> None:
    manager.add_document(
        {
            "path": path,
            "content": body,
            "content_raw": body,
            "identifiers": ids,
            "line_start": 1,
            "line_end": body.count("\n") + 1,
            # The file suffix, as the indexer stores it (chunk_fts_documents).
            "language": "py",
        }
    )


def _build_fts_index(index_dir: Path, repo: str) -> None:
    manager = TantivyIndexManager(index_dir=index_dir)
    manager.initialize_index()
    _add_doc(
        manager,
        _auth_path(repo),
        f"def authenticate(user): return '{repo}'",
        ["authenticate", "user"],
    )
    _add_doc(manager, _legacy_path(repo), LATIN1_TEXT, ["legacy_greeting"])
    manager.commit()
    manager.close()


@pytest.fixture(scope="module")
def fts_index_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    template = tmp_path_factory.mktemp("web_fts_index_template")
    for repo in ALL_REPOS:
        _build_fts_index(template / repo, repo)
    return template


@pytest.fixture(scope="module")
def server_db_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_server_db_template(tmp_path_factory.mktemp("web_server_db"))


@pytest.fixture(scope="module")
def web_app(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """The real app over an isolated server home (never ~/.cidx-server).

    Both accounts hold the admin ROLE (the Query page is admin-only); repo
    access is decided by GROUP membership: ADMIN is in 'admins', USER only in
    the group granted GRANTED_REPO (see QueryAccessEnv).
    """
    with isolated_app(tmp_path_factory.mktemp("web-query-app")) as app:
        users = app.state.user_manager
        users.create_user(ADMIN, PASSWORD, UserRole.ADMIN)
        users.create_user(USER, PASSWORD, UserRole.ADMIN)
        yield app


@pytest.fixture
def env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    web_app: Any,
    server_db_template: Path,
    fts_index_template: Path,
) -> Iterator[QueryAccessEnv]:
    monkeypatch.delenv("CO_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    # QueryAccessEnv installs its own config service; put the app's back.
    monkeypatch.setattr(
        config_service_module,
        "_config_service",
        config_service_module._config_service,
    )
    e = QueryAccessEnv(tmp_path, server_db_template)
    for repo in ALL_REPOS:
        # Every indexed repository carries its own config.json; the temporal
        # path verifies it before looking for a temporal index.
        ConfigManager(
            e.repo_paths[repo] / ".code-indexer" / "config.json"
        ).create_default_config(codebase_dir=e.repo_paths[repo])
        shutil.copytree(
            fts_index_template / repo,
            e.repo_paths[repo] / ".code-indexer" / "tantivy_index",
        )
    latin1_file = e.repo_paths[GRANTED_REPO] / _legacy_path(GRANTED_REPO)
    latin1_file.parent.mkdir(parents=True, exist_ok=True)
    latin1_file.write_bytes(LATIN1_TEXT.encode("latin-1"))
    # The Web handlers list global repos from the app's registry (set by the
    # lifespan in production; the lifespan does not run here, so the
    # attribute does not exist yet and is removed again afterwards).
    web_app.state.backend_registry = SimpleNamespace(global_repos=e.global_repos)
    try:
        with e.installed(e.access_service):
            # The Web activated-repo listing reads the manager from the
            # installed app's state.
            app_module = importlib.import_module("code_indexer.server.app")
            stand_in = vars(app_module)["app"]
            stand_in.state.activated_repo_manager = e.activated_repo_manager
            fake = e.fake_search_repository_path()

            def _search(
                self: Any, repo_path: str, search_request: Any, **kw: Any
            ) -> Any:
                # Same fake, callable positionally or by keyword.
                return fake(
                    self, repo_path=repo_path, search_request=search_request, **kw
                )

            with patch(SEARCH_BOUNDARY, _search):
                yield e
    finally:
        del web_app.state.backend_registry
        e.close()


def _cookie_value(response: Response, name: str) -> str:
    cookie: SimpleCookie = SimpleCookie()
    for header in response.headers.getlist("set-cookie"):
        cookie.load(header)
    return cookie[name].value


def _client(web_app: Any, username: str) -> TestClient:
    """A client holding a real Web session and a valid CSRF cookie."""
    client = TestClient(web_app, follow_redirects=False)
    session_response = Response()
    web_auth.get_session_manager().create_session(session_response, username, "admin")
    client.cookies.set(
        web_auth.SESSION_COOKIE_NAME,
        _cookie_value(session_response, web_auth.SESSION_COOKIE_NAME),
    )
    csrf_response = Response()
    web_routes.set_csrf_cookie(csrf_response, "example-csrf-token")
    client.cookies.set(
        web_routes.CSRF_COOKIE_NAME,
        _cookie_value(csrf_response, web_routes.CSRF_COOKIE_NAME),
    )
    return client


def _query(
    web_app: Any,
    handler: str,
    repository: str,
    query_text: str,
    search_mode: str,
    username: str = ADMIN,
    **fields: Any,
) -> str:
    form: Dict[str, Any] = {
        "query_text": query_text,
        "repository": repository,
        "search_mode": search_mode,
        "limit": "10",
        "csrf_token": "example-csrf-token",
    }
    form.update({k: ("true" if v is True else v) for k, v in fields.items()})
    response = _client(web_app, username).post(handler, data=form)
    assert response.status_code == 200, response.text
    return response.text


def _badge(html: str) -> Optional[str]:
    match = _BADGE.search(html)
    return match.group(1) if match else None


def _error(html: str) -> Optional[str]:
    match = re.search(r"<strong>Error:</strong>\s*(.*?)\s*</div>", html, re.S)
    return match.group(1) if match else None


def _warning(html: str) -> Optional[str]:
    match = re.search(r"<strong>Warning:</strong>\s*(.*?)\s*</div>", html, re.S)
    return match.group(1) if match else None


@pytest.mark.parametrize("handler", HANDLERS)
class TestGlobalRepoModes:
    def test_fts_returns_fts_rows_and_fts_badge(self, web_app, env, handler):
        html = _query(web_app, handler, GRANTED_GLOBAL, "authenticate", "fts")

        assert _error(html) is None
        assert _auth_path(GRANTED_REPO) in html
        assert _semantic_row_path(GRANTED_REPO) not in html
        assert env.searched_paths == []  # the semantic boundary never ran
        assert _badge(html) == "fts"

    def test_hybrid_merges_fts_and_semantic_rows(self, web_app, env, handler):
        html = _query(web_app, handler, GRANTED_GLOBAL, "authenticate", "hybrid")

        assert _error(html) is None
        assert _auth_path(GRANTED_REPO) in html
        assert _semantic_row_path(GRANTED_REPO) in html
        assert _badge(html) == "hybrid"

    def test_min_score_reaches_the_query(self, web_app, env, handler):
        # Semantic rows score 0.80, 0.79, 0.78: only the first passes 0.795.
        html = _query(
            web_app, handler, GRANTED_GLOBAL, "find", "semantic", min_score="0.795"
        )

        assert _error(html) is None
        assert _semantic_row_path(GRANTED_REPO, 0) in html
        assert _semantic_row_path(GRANTED_REPO, 1) not in html
        assert _badge(html) == "semantic"

    def test_case_sensitive_reaches_the_query(self, web_app, env, handler):
        insensitive = _query(
            web_app, handler, GRANTED_GLOBAL, "AUTH.*ATE", "fts", regex=True
        )
        sensitive = _query(
            web_app,
            handler,
            GRANTED_GLOBAL,
            "AUTH.*ATE",
            "fts",
            regex=True,
            case_sensitive=True,
        )

        assert _auth_path(GRANTED_REPO) in insensitive
        assert _auth_path(GRANTED_REPO) not in sensitive
        assert _error(sensitive) is None

    def test_fuzzy_reaches_the_query(self, web_app, env, handler):
        exact = _query(web_app, handler, GRANTED_GLOBAL, "authentcate", "fts")
        fuzzy = _query(
            web_app, handler, GRANTED_GLOBAL, "authentcate", "fts", fuzzy=True
        )

        assert _auth_path(GRANTED_REPO) not in exact
        assert _auth_path(GRANTED_REPO) in fuzzy

    def test_regex_reaches_the_query(self, web_app, env, handler):
        literal = _query(web_app, handler, GRANTED_GLOBAL, "auth.*ate", "fts")
        pattern = _query(
            web_app, handler, GRANTED_GLOBAL, "auth.*ate", "fts", regex=True
        )

        assert _auth_path(GRANTED_REPO) not in literal
        assert _auth_path(GRANTED_REPO) in pattern

    def test_time_range_runs_the_temporal_path(self, web_app, env, handler):
        html = _query(
            web_app,
            handler,
            GRANTED_GLOBAL,
            "find",
            "temporal",
            time_range="2024-01-01..2024-12-31",
        )

        # The repo has no temporal index: the temporal path returns nothing
        # and never falls back to a plain semantic search.
        assert env.searched_paths == []
        assert _semantic_row_path(GRANTED_REPO) not in html

    def test_temporal_query_without_index_shows_the_warning(
        self, web_app, env, handler
    ):
        html = _query(
            web_app,
            handler,
            GRANTED_GLOBAL,
            "find",
            "temporal",
            time_range="2024-01-01..2024-12-31",
        )

        # The query layer's own warning (REST and MCP return it too),
        # rendered through the template's autoescape.
        assert _error(html) is None
        warning = _warning(html)
        assert warning is not None
        assert "Temporal index not available for this repository" in warning
        assert "&#39;cidx index --index-commits&#39;" in warning

    def test_invalid_time_range_is_validated_by_the_temporal_path(
        self, web_app, env, handler
    ):
        html = _query(
            web_app,
            handler,
            GRANTED_GLOBAL,
            "find",
            "temporal",
            time_range="not-a-range",
        )

        assert _error(html) is not None
        assert env.searched_paths == []

    def test_latin1_file_returns_the_fts_snippet(self, web_app, env, handler):
        html = _query(web_app, handler, GRANTED_GLOBAL, "legacy_greeting", "fts")

        assert _error(html) is None
        assert _legacy_path(GRANTED_REPO) in html
        assert "café crème" in html
        assert "decode" not in html.lower()


@pytest.mark.parametrize("handler", HANDLERS)
class TestGlobalRepoFilters:
    """Each Web form filter reaches the query layer for a global repo."""

    def test_language_reaches_the_query(self, web_app, env, handler):
        python = _query(
            web_app, handler, GRANTED_GLOBAL, "authenticate", "fts", language="python"
        )
        javascript = _query(
            web_app,
            handler,
            GRANTED_GLOBAL,
            "authenticate",
            "fts",
            language="javascript",
        )

        assert _error(python) is None
        assert _auth_path(GRANTED_REPO) in python
        assert _error(javascript) is None
        assert _auth_path(GRANTED_REPO) not in javascript

    def test_path_filter_reaches_the_query(self, web_app, env, handler):
        matching = _query(
            web_app,
            handler,
            GRANTED_GLOBAL,
            "authenticate",
            "fts",
            path_pattern="*_auth.py",
        )
        other = _query(
            web_app,
            handler,
            GRANTED_GLOBAL,
            "authenticate",
            "fts",
            path_pattern="*_legacy.py",
        )

        assert _error(matching) is None
        assert _auth_path(GRANTED_REPO) in matching
        assert _error(other) is None
        assert _auth_path(GRANTED_REPO) not in other

    def test_at_commit_reaches_the_temporal_path(self, web_app, env, handler):
        unknown = _query(
            web_app,
            handler,
            GRANTED_GLOBAL,
            "find",
            "temporal",
            at_commit="no-such-ref-example",
        )
        known = _query(
            web_app, handler, GRANTED_GLOBAL, "find", "temporal", at_commit="main"
        )

        # The temporal path resolves the ref against the repository's git.
        error = _error(unknown)
        assert error is not None
        assert "no-such-ref-example" in error
        assert _error(known) is None
        assert _warning(known) is not None
        assert env.searched_paths == []

    def test_time_range_all_runs_the_temporal_path(self, web_app, env, handler):
        plain = _query(web_app, handler, GRANTED_GLOBAL, "find", "semantic")
        assert _semantic_row_path(GRANTED_REPO) in plain
        env.searched_paths.clear()

        all_history = _query(
            web_app,
            handler,
            GRANTED_GLOBAL,
            "find",
            "semantic",
            time_range_all=True,
        )

        # time_range_all routes even a semantic-mode query to the temporal
        # index (absent here): no semantic search, the temporal warning.
        assert env.searched_paths == []
        assert _semantic_row_path(GRANTED_REPO) not in all_history
        assert _error(all_history) is None
        assert "Temporal index not available" in (_warning(all_history) or "")


@pytest.mark.parametrize("handler", HANDLERS)
class TestGlobalRepoAccess:
    def test_user_without_access_is_refused(self, web_app, env, handler):
        html = _query(
            web_app, handler, UNGRANTED_GLOBAL, "find", "semantic", username=USER
        )

        assert _semantic_row_path(UNGRANTED_REPO) not in html
        assert UNGRANTED_REPO not in env.searched_repos()
        assert _error(html) is not None

    def test_user_with_access_gets_rows(self, web_app, env, handler):
        html = _query(
            web_app, handler, GRANTED_GLOBAL, "authenticate", "fts", username=USER
        )

        assert _error(html) is None
        assert _auth_path(GRANTED_REPO) in html


def _routes_records(caplog: pytest.LogCaptureFixture) -> List[logging.LogRecord]:
    return [r for r in caplog.records if r.name == web_routes.logger.name]


@pytest.mark.parametrize("handler", HANDLERS)
class TestQueryFailureLogging:
    """Expected refusals log WARNING (no traceback); the unexpected, ERROR."""

    def _assert_refusal_logged_as_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> str:
        records = _routes_records(caplog)
        assert [r for r in records if r.levelno >= logging.ERROR] == []
        refusals = [r for r in records if "[STORE-GENERAL-053]" in r.getMessage()]
        assert len(refusals) == 1
        assert refusals[0].levelno == logging.WARNING
        assert refusals[0].exc_info is None
        return refusals[0].getMessage()

    def test_access_refusal_logs_warning_not_error(self, web_app, env, handler, caplog):
        with caplog.at_level(logging.WARNING):
            html = _query(
                web_app, handler, UNGRANTED_GLOBAL, "find", "semantic", username=USER
            )

        assert _error(html) is not None
        message = self._assert_refusal_logged_as_warning(caplog)
        # A repository the user cannot access is refused as not found: a
        # client error (classify_search_error), so its message is logged.
        assert "SearchRequestError" in message
        assert UNGRANTED_GLOBAL in message

    def test_invalid_time_range_logs_warning_not_error(
        self, web_app, env, handler, caplog
    ):
        with caplog.at_level(logging.WARNING):
            html = _query(
                web_app,
                handler,
                GRANTED_GLOBAL,
                "find",
                "temporal",
                time_range="not-a-range",
            )

        # A client error: the user sees its reason.
        assert "YYYY-MM-DD..YYYY-MM-DD" in (_error(html) or "")
        message = self._assert_refusal_logged_as_warning(caplog)
        assert "SearchParameterError" in message

    def test_provider_error_text_is_not_logged_by_the_web_page(
        self, web_app, env, handler, caplog
    ):
        token = "example-secret-token"

        def _provider_outage(_service: Any, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(f"voyage-ai: HTTP 503 (Authorization: Bearer {token})")

        with patch(SEARCH_BOUNDARY, _provider_outage):
            with caplog.at_level(logging.WARNING):
                html = _query(web_app, handler, GRANTED_GLOBAL, "find", "semantic")

        # Not a client error: the page shows only the fixed public message.
        error = _error(html)
        assert error is not None
        assert "Search failed" in error
        assert token not in html
        message = self._assert_refusal_logged_as_warning(caplog)
        assert "SearchFailedError" in message
        assert token not in message

    def test_unwired_access_filtering_fails_closed_with_its_fixed_text(
        self, web_app, env, handler, caplog, monkeypatch
    ):
        # A process whose access filtering service is not wired must fail
        # closed. Its error carries a fixed text that classify_search_error
        # allows to the client, so it is shown; an unwired server-side
        # service is an internal failure, so it is logged at ERROR.
        app_module = importlib.import_module("code_indexer.server.app")
        monkeypatch.setattr(
            vars(app_module)["app"].state, "access_filtering_service", None
        )

        with caplog.at_level(logging.WARNING):
            html = _query(web_app, handler, GRANTED_GLOBAL, "find", "semantic")

        assert "Repository access control is unavailable" in (_error(html) or "")
        assert env.searched_paths == []
        records = _routes_records(caplog)
        code = "STORE-GENERAL-041" if handler == PARTIAL else "STORE-GENERAL-035"
        refusals = [r for r in records if f"[{code}]" in r.getMessage()]
        assert len(refusals) == 1
        assert refusals[0].levelno == logging.ERROR
        assert refusals[0].exc_info

    def test_unexpected_query_layer_error_text_never_reaches_the_page(
        self, web_app, env, handler, caplog
    ):
        # Not a SemanticQueryError/ValueError: the outer page handler gets it.
        sentinel_path = "/srv/example-internal/sentinel-index-dir"
        token = "example-fake-token-0123456789"
        manager = vars(importlib.import_module("code_indexer.server.app"))[
            "semantic_query_manager"
        ]

        def _fault(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(f"cannot open {sentinel_path} (token={token})")

        with patch.object(manager, "query_user_repositories", _fault):
            with caplog.at_level(logging.WARNING):
                html = _query(web_app, handler, GRANTED_GLOBAL, "find", "semantic")

        # The fixed public message (it may carry a correlation id suffix).
        assert (_error(html) or "").startswith("Query failed: Search failed")
        assert sentinel_path not in html
        assert token not in html
        # The detail stays server-side, logged once at ERROR with a traceback.
        errors = [r for r in _routes_records(caplog) if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        code = "STORE-GENERAL-041" if handler == PARTIAL else "STORE-GENERAL-035"
        assert f"[{code}]" in errors[0].getMessage()
        assert sentinel_path in errors[0].getMessage()
        assert errors[0].exc_info is not None

    def test_unexpected_value_error_in_scip_mode_logs_error_with_traceback(
        self, web_app, env, handler, caplog, monkeypatch
    ):
        # Only the text query's own failures are expected outcomes; a
        # ValueError anywhere else (here the repository listing a SCIP
        # query starts from) is a server fault. Only the query's own listing
        # fails; the full page lists repositories again to render the form.
        real_listing = web_routes._get_all_activated_repos_for_query
        calls: List[int] = []

        def _listing_fault(registry: Any = None) -> List[Dict[str, Any]]:
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("unexpected repository listing fault")
            return list(real_listing(registry))

        monkeypatch.setattr(
            web_routes, "_get_all_activated_repos_for_query", _listing_fault
        )

        with caplog.at_level(logging.WARNING):
            html = _query(web_app, handler, GRANTED_GLOBAL, "Example", "scip")

        assert _error(html) is not None
        records = _routes_records(caplog)
        assert [r for r in records if "[STORE-GENERAL-053]" in r.getMessage()] == []
        errors = [r for r in records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        code = "STORE-GENERAL-041" if handler == PARTIAL else "STORE-GENERAL-035"
        assert f"[{code}]" in errors[0].getMessage()
        assert errors[0].exc_info is not None


@pytest.mark.parametrize("handler", HANDLERS)
class TestUnchangedPaths:
    def test_activated_repo_semantic_query(self, web_app, env, handler):
        env.activate_for(ADMIN, GRANTED_REPO, OWN_ACTIVATION)

        html = _query(web_app, handler, OWN_ACTIVATION, "find", "semantic")

        assert _error(html) is None
        assert _semantic_row_path(OWN_ACTIVATION) in html
        assert _badge(html) == "semantic"

    def test_query_service_unavailable_is_reported(self, web_app, env, handler):
        # Restored by hand: the env fixture restores the app module's names
        # on teardown, so a monkeypatch undo would run after it and leave
        # this test's manager behind.
        namespace = vars(importlib.import_module("code_indexer.server.app"))
        installed = namespace["semantic_query_manager"]
        namespace["semantic_query_manager"] = None
        try:
            html = _query(web_app, handler, GRANTED_GLOBAL, "find", "semantic")
        finally:
            namespace["semantic_query_manager"] = installed

        assert _error(html) == "Query service not available"
        assert env.searched_paths == []

    def test_scip_on_global_repo_without_index(self, web_app, env, handler):
        html = _query(web_app, handler, GRANTED_GLOBAL, "Example", "scip")

        error = _error(html)
        assert error is not None
        assert "No SCIP index found" in error
        assert env.searched_paths == []
