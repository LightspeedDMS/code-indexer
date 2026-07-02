"""
Unit tests for Story #863: GET /admin/partials/golden-repos/{alias}/details route.

Tests invoke golden_repo_details_partial directly, mocking only true external
dependencies:
  - _require_admin_session
  - _get_golden_repo_manager   (list_golden_repos returns controlled data)
  - _get_repo_category_service (list_categories / get_repo_category_map)
  - templates                  (Jinja2Templates mock to capture context dict)
  - get_csrf_token_from_cookie
  - set_csrf_cookie

Note: DashboardService is only called when global_alias is truthy. All test
repos use global_alias=None, so DashboardService is never reached and needs
no mock.

Acceptance criteria covered:
  - Auth: redirect when no admin session
  - AC7: HTTPException(404) when alias not in list_golden_repos()
  - AC2: HTTP 200 with all declared context fields populated
  - CSRF non-rotation: set_csrf_cookie() must NOT be called
  - Categories: populated from _get_repo_category_service().list_categories()
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _make_request() -> MagicMock:
    """Build a minimal mock Request with no cookies."""
    req = MagicMock(spec=Request)
    req.cookies = {}
    req.app = MagicMock()
    req.app.state.backend_registry = None
    return req


def _make_admin_session(username: str = "admin") -> MagicMock:
    """Build a mock admin SessionData."""
    session = MagicMock()
    session.username = username
    session.role = "admin"
    return session


def _make_base_repo_dict(alias: str) -> dict:
    """
    Return a raw repo dict as returned by list_golden_repos().

    Uses neutral, non-environment-coupled values.
    """
    return {
        "alias": alias,
        "repo_url": "git+https://example.test/org/repo",
        "default_branch": "main",
        "status": "ready",
        "error_message": None,
        "created_at": "2024-01-01T00:00:00",
        "file_count": 42,
        "chunk_count": 1234,
        "wiki_enabled": False,
        "temporal_options": None,
        "clone_path": None,  # No filesystem path — no index checks performed
    }


def _make_mock_category(cat_id: str, name: str) -> MagicMock:
    """Build a minimal mock RepoCategoryInfo object."""
    cat = MagicMock()
    cat.id = cat_id
    cat.name = name
    return cat


def _make_mock_templates() -> MagicMock:
    """Return a Jinja2Templates mock that always returns HTTP 200."""
    mock = MagicMock(spec=Jinja2Templates)
    mock.TemplateResponse.return_value = HTMLResponse(
        content="<html>details</html>", status_code=200
    )
    return mock


def _invoke_handler(
    alias: str,
    repo_list: list,
    categories: list,
    csrf_token: str = "existing-csrf-tok",
) -> tuple:
    """
    Invoke golden_repo_details_partial with all external deps mocked.

    Returns (response_or_exception, set_csrf_cookie_mock, templates_mock).
    HTTPException instances are caught and returned as the first element.
    """
    from src.code_indexer.server.web.routes import golden_repo_details_partial

    req = _make_request()
    session = _make_admin_session()

    mock_manager = MagicMock()
    mock_manager.list_golden_repos.return_value = repo_list
    mock_manager.golden_repos = {r["alias"]: r for r in repo_list}

    mock_category_service = MagicMock()
    mock_category_service.list_categories.return_value = categories
    mock_category_service.get_repo_category_map.return_value = {}

    mock_set_csrf = MagicMock()
    mock_tmpl = _make_mock_templates()

    with (
        patch(
            "src.code_indexer.server.web.routes._require_admin_session",
            return_value=session,
        ),
        patch(
            "src.code_indexer.server.web.routes._get_golden_repo_manager",
            return_value=mock_manager,
        ),
        patch(
            "src.code_indexer.server.web.routes._get_repo_category_service",
            return_value=mock_category_service,
        ),
        patch(
            "src.code_indexer.server.web.routes.get_csrf_token_from_cookie",
            return_value=csrf_token,
        ),
        patch("src.code_indexer.server.web.routes.set_csrf_cookie", mock_set_csrf),
        patch("src.code_indexer.server.web.routes.templates", mock_tmpl),
    ):
        try:
            response = golden_repo_details_partial(request=req, alias=alias)
            return response, mock_set_csrf, mock_tmpl
        except HTTPException as exc:
            return exc, mock_set_csrf, mock_tmpl


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


class TestGoldenRepoDetailsPartialAuth:
    """No admin session -> handler must return 401 HTML error fragment (Finding 2 / AC6)."""

    def test_no_session_returns_401_html_error_fragment(self):
        """
        _require_admin_session returns None -> 401 HTML fragment with 'Session expired'.

        After Finding 2 fix, the handler no longer calls _create_login_redirect.
        Instead it returns an inline HTML error fragment so htmx can swap it into
        the details cell without replacing the whole page with the login form.
        """
        from src.code_indexer.server.web.routes import golden_repo_details_partial

        req = _make_request()

        with patch(
            "src.code_indexer.server.web.routes._require_admin_session",
            return_value=None,
        ):
            result = golden_repo_details_partial(request=req, alias="any-alias")

        assert result.status_code == 401
        body = _extract_body(result)
        assert "Session expired" in body


# ---------------------------------------------------------------------------
# 404 behavior
# ---------------------------------------------------------------------------


def _invoke_404_handler(alias: str):
    """Invoke handler with no repos registered; return the response."""
    from src.code_indexer.server.web.routes import golden_repo_details_partial

    req = _make_request()
    session = _make_admin_session()
    mock_manager = MagicMock()
    mock_manager.list_golden_repos.return_value = []
    mock_manager.golden_repos = {}

    with (
        patch(
            "src.code_indexer.server.web.routes._require_admin_session",
            return_value=session,
        ),
        patch(
            "src.code_indexer.server.web.routes._get_golden_repo_manager",
            return_value=mock_manager,
        ),
    ):
        return golden_repo_details_partial(request=req, alias=alias)


class TestGoldenRepoDetailsPartial404:
    """When alias not found, handler must return 404 HTML error fragment (Finding 2/AC7)."""

    def test_unknown_alias_returns_404_html_fragment(self):
        """AC7: alias absent -> 404 HTML fragment with 'Repository not found'."""
        result = _invoke_404_handler("nonexistent-alias-863")
        assert result.status_code == 404
        assert result.media_type == "text/html"
        assert "Repository not found" in _extract_body(result)

    def test_404_fragment_is_html_not_json(self):
        """AC7: 404 response is text/html and body is not JSON (cell swap requires HTML)."""
        result = _invoke_404_handler("missing-repo-863")
        assert result.status_code == 404
        assert result.media_type == "text/html"
        body = _extract_body(result)
        assert "Repository not found" in body
        assert not body.strip().startswith("{")


# ---------------------------------------------------------------------------
# 200 success — template context variables
# ---------------------------------------------------------------------------


class TestGoldenRepoDetailsPartial200:
    """
    AC2: Alias in list_golden_repos() → HTTP 200 with all declared context fields.

    Context fields asserted (per story spec 'Template context enumeration'):
    alias, repo_url, default_branch, status, file_count, chunk_count,
    wiki_enabled, has_semantic, has_fts, has_temporal, has_scip,
    csrf_token, categories (from list_categories()), template name.
    """

    @pytest.fixture
    def invoke_result(self) -> tuple:
        """Invoke handler with a known repo; return (response, set_csrf, templates_mock)."""
        alias = "ctx-test-repo-863"
        repo = _make_base_repo_dict(alias)
        cat = _make_mock_category("cat-001", "TestCategory863")
        return _invoke_handler(
            alias=alias,
            repo_list=[repo],
            categories=[cat],
            csrf_token="test-csrf-token-863",
        )

    @pytest.fixture
    def context(self, invoke_result: tuple) -> dict:
        """Extract template context dict from templates.TemplateResponse call args."""
        from typing import cast as _cast

        _, _, mock_tmpl = invoke_result
        assert mock_tmpl.TemplateResponse.called, (
            "templates.TemplateResponse must be called for a valid alias."
        )
        # cast: MagicMock.call_args is typed Any; narrowing to fixture contract
        return _cast(dict, mock_tmpl.TemplateResponse.call_args[0][1])

    def test_response_status_200(self, invoke_result: tuple):
        """AC2: Handler returns HTTP 200 for a valid alias."""
        response, _, _ = invoke_result
        assert response.status_code == 200

    def test_template_name_contains_golden_repo_details(self, invoke_result: tuple):
        """templates.TemplateResponse must be called with the details partial template."""
        _, _, mock_tmpl = invoke_result
        template_name = mock_tmpl.TemplateResponse.call_args[0][0]
        assert "golden_repo_details" in template_name, (
            f"Expected 'golden_repo_details' in template name, got: {template_name}"
        )

    def test_context_alias(self, context: dict):
        """'repo' in context must contain the correct alias."""
        assert "repo" in context
        assert context["repo"]["alias"] == "ctx-test-repo-863"

    def test_context_repo_url(self, context: dict):
        """repo_url must be present in context."""
        assert context["repo"]["repo_url"] == "git+https://example.test/org/repo"

    def test_context_default_branch(self, context: dict):
        """default_branch must be present in context."""
        assert context["repo"]["default_branch"] == "main"

    def test_context_status(self, context: dict):
        """status field must be present and correct."""
        assert context["repo"]["status"] == "ready"

    def test_context_file_count(self, context: dict):
        """file_count must be present in context."""
        assert context["repo"]["file_count"] == 42

    def test_context_chunk_count(self, context: dict):
        """chunk_count must be present in context."""
        assert context["repo"]["chunk_count"] == 1234

    def test_context_wiki_enabled(self, context: dict):
        """wiki_enabled must be present in context (wiki toggle form requirement)."""
        assert "wiki_enabled" in context["repo"]

    def test_context_index_flags_present(self, context: dict):
        """Index presence flags must be in context for the indexes management section."""
        for flag in ("has_semantic", "has_fts", "has_temporal", "has_scip"):
            assert flag in context["repo"], f"Context must include index flag '{flag}'."

    def test_context_csrf_token(self, context: dict):
        """csrf_token must be in context for form hidden inputs."""
        assert "csrf_token" in context
        assert context["csrf_token"] == "test-csrf-token-863"

    def test_context_categories_from_service(self, context: dict):
        """categories list from list_categories() must be in context."""
        assert "categories" in context
        cats = context["categories"]
        assert len(cats) == 1
        assert cats[0].name == "TestCategory863"


# ---------------------------------------------------------------------------
# CSRF non-rotation
# ---------------------------------------------------------------------------


class TestGoldenRepoDetailsPartialCsrfNonRotation:
    """
    CRITICAL: The details partial must NOT call set_csrf_cookie().

    Calling set_csrf_cookie() would rotate the page-wide CSRF token and
    invalidate all other open forms (add-repo, wiki-toggle, per-row actions).
    """

    def test_set_csrf_cookie_not_called_on_success_path(self):
        """set_csrf_cookie must NOT be invoked for a valid alias (200 path)."""
        alias = "csrf-no-rotate-863"
        repo = _make_base_repo_dict(alias)
        _, mock_set_csrf, _ = _invoke_handler(
            alias=alias, repo_list=[repo], categories=[]
        )
        mock_set_csrf.assert_not_called()


# ---------------------------------------------------------------------------
# Shared test helper
# ---------------------------------------------------------------------------


def _extract_body(response) -> str:
    """Extract decoded body string from an HTMLResponse."""
    body = response.body
    return body.decode() if isinstance(body, bytes) else str(body)


# ---------------------------------------------------------------------------
# Finding 4a: Error/auth handling — HTML error fragments (AC6, AC7, AC8)
# ---------------------------------------------------------------------------


class TestGoldenRepoDetailsErrorFragments:
    """
    Finding 4a coverage for Finding 2: AC6/AC7/AC8 error paths must return HTML fragments.

    AC6: Unauthenticated request -> 401 HTML fragment with "Session expired" message.
    AC7: Unknown alias -> 404 HTML fragment with "Repository not found" message.
    AC8: Internal error -> 500 HTML fragment with Retry button.
    """

    def test_unauthenticated_returns_html_error_fragment_not_redirect(self):
        """
        AC6: No admin session -> 401 HTML error fragment (not a 302 redirect).

        The cell must show "Session expired" inline so the user can act on it
        without the login page HTML being swapped into the details cell.
        """
        from src.code_indexer.server.web.routes import golden_repo_details_partial

        req = _make_request()

        with patch(
            "src.code_indexer.server.web.routes._require_admin_session",
            return_value=None,
        ):
            response = golden_repo_details_partial(request=req, alias="any-alias")

        assert response.status_code == 401, (
            f"Unauthenticated request must return 401, got {response.status_code}"
        )
        assert response.media_type == "text/html", (
            f"Error fragment must be text/html, got {response.media_type}"
        )
        body = _extract_body(response)
        assert "Session expired" in body, (
            f"Error fragment must contain 'Session expired', got: {body[:200]}"
        )
        assert 'action="/admin/login"' not in body and 'action="/login"' not in body, (
            "Error fragment must not contain login form HTML"
        )

    def test_unknown_alias_returns_html_error_fragment(self):
        """
        AC7: Unknown alias -> 404 HTML error fragment (not JSON, not HTTPException raised).

        htmx swaps the fragment into the cell so the user sees "Repository not found"
        inline instead of an unhandled 404 JSON response.
        """
        from src.code_indexer.server.web.routes import golden_repo_details_partial

        req = _make_request()
        session = _make_admin_session()

        mock_manager = MagicMock()
        mock_manager.list_golden_repos.return_value = []
        mock_manager.golden_repos = {}

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=session,
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_manager",
                return_value=mock_manager,
            ),
        ):
            response = golden_repo_details_partial(request=req, alias="__nonexistent__")

        assert response.status_code == 404, (
            f"Unknown alias must return 404, got {response.status_code}"
        )
        assert response.media_type == "text/html", (
            f"Error fragment must be text/html, got {response.media_type}"
        )
        body = _extract_body(response)
        assert "Repository not found" in body, (
            f"Error fragment must contain 'Repository not found', got: {body[:200]}"
        )
        assert not body.strip().startswith("{"), "Error fragment must not be JSON"

    def test_unexpected_error_returns_html_error_fragment_with_retry(self):
        """
        AC8: Unexpected error (500) -> HTML error fragment containing a Retry button.

        The cell shows "Failed to load repository details" and a Retry button so the
        user can recover without a full page reload.
        """
        from src.code_indexer.server.web.routes import golden_repo_details_partial

        req = _make_request()
        session = _make_admin_session()

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=session,
            ),
            patch(
                "src.code_indexer.server.web.routes._get_single_repo_enriched",
                side_effect=RuntimeError("simulated internal error"),
            ),
        ):
            response = golden_repo_details_partial(request=req, alias="any-alias")

        assert response.status_code == 500, (
            f"Unexpected error must return 500, got {response.status_code}"
        )
        assert response.media_type == "text/html", (
            f"Error fragment must be text/html, got {response.media_type}"
        )
        body = _extract_body(response)
        assert "Failed to load" in body, (
            f"500 fragment must contain 'Failed to load', got: {body[:200]}"
        )
        assert "Retry" in body or "retry" in body.lower(), (
            f"500 fragment must contain a Retry button, got: {body[:200]}"
        )


# ---------------------------------------------------------------------------
# Finding 4b: List path must NOT call _load_temporal_status (AC1 perf)
# ---------------------------------------------------------------------------


class TestListPathDoesNotCallTemporalStatus:
    """
    Finding 4b coverage for Finding 3: _get_golden_repos_list must not call
    _load_temporal_status.

    temporal_status is consumed only by the lazy-loaded details partial and must
    be deferred to _get_single_repo_enriched to avoid per-alias overhead at page load.
    """

    def test_list_endpoint_does_not_call_load_temporal_status(self):
        """
        _get_golden_repos_list must never call _load_temporal_status.

        Verifies Finding 3: the temporal_status call must be removed from the list
        path and kept only in _get_single_repo_enriched (the details partial helper).
        """
        from src.code_indexer.server.web.routes import _get_golden_repos_list

        with patch(
            "src.code_indexer.server.web.routes._load_temporal_status"
        ) as mock_temporal:
            _get_golden_repos_list(backend_registry=None)

        (
            mock_temporal.assert_not_called(),
            (
                "_load_temporal_status must NOT be called from _get_golden_repos_list. "
                "Move it to _get_single_repo_enriched (the details partial path) only."
            ),
        )
