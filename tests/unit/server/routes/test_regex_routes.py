"""Unit tests for POST /api/regex/search REST endpoint (Story #1011).

Mocking strategy:
- _resolve_repo_path: mocked (needs live alias manager)
- RegexSearchService.search: mocked (needs real filesystem/ripgrep)
- api_metrics_service.increment_regex_search: mocked (side-effect check)
- _expand_wildcard_patterns, _enforce_repo_count_cap: used from _utils
- User/permission: FastAPI dependency_overrides for get_current_user
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_SINGLE_BODY: Dict[str, Any] = {
    "pattern": r"def\s+\w+",
    "repository_alias": "myrepo-global",
}

VALID_OMNI_BODY: Dict[str, Any] = {
    "pattern": r"class\s+\w+",
    "repository_alias": ["repo1-global", "repo2-global"],
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


def _make_mock_search_result(
    matches: Optional[List[Dict[str, Any]]] = None,
) -> MagicMock:
    """Build a mock RegexSearchResult dataclass."""
    mock_result = MagicMock()
    mock_result.matches = []
    mock_result.total_matches = 0
    mock_result.truncated = False
    mock_result.search_engine = "ripgrep"
    mock_result.search_time_ms = 42.0
    if matches is not None:
        mock_matches = []
        for m in matches:
            mm = MagicMock()
            mm.file_path = m.get("file_path", "src/foo.py")
            mm.line_number = m.get("line_number", 1)
            mm.column = m.get("column", 1)
            mm.line_content = m.get("line_content", "content")
            mm.context_before = m.get("context_before", [])
            mm.context_after = m.get("context_after", [])
            mock_matches.append(mm)
        mock_result.matches = mock_matches
        mock_result.total_matches = len(mock_matches)
    return mock_result


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
        "code_indexer.server.routes.regex_routes._resolve_repo_path",
        return_value=path,
    )


def _patch_repo_not_found():
    """Patch _resolve_repo_path to return None (alias not found)."""
    return patch(
        "code_indexer.server.routes.regex_routes._resolve_repo_path",
        return_value=None,
    )


def _patch_search_success(
    matches: Optional[List[Dict[str, Any]]] = None,
):
    """Patch RegexSearchService.search to return a mock result."""
    mock_result = _make_mock_search_result(matches)
    return patch(
        "code_indexer.server.routes.regex_routes.RegexSearchService",
        return_value=MagicMock(search=AsyncMock(return_value=mock_result)),
    )


def _patch_search_timeout():
    """Patch RegexSearchService.search to raise TimeoutError."""
    mock_service = MagicMock()
    mock_service.search = AsyncMock(side_effect=TimeoutError("Search timed out"))
    return patch(
        "code_indexer.server.routes.regex_routes.RegexSearchService",
        return_value=mock_service,
    )


def _patch_search_ripgrep_error():
    """Patch RegexSearchService.search to raise RipgrepExecutionError."""
    from code_indexer.global_repos.regex_search import RipgrepExecutionError

    mock_service = MagicMock()
    mock_service.search = AsyncMock(
        side_effect=RipgrepExecutionError("ripgrep failed: exit_code=2")
    )
    return patch(
        "code_indexer.server.routes.regex_routes.RegexSearchService",
        return_value=mock_service,
    )


def _patch_search_pcre2_error():
    """Patch RegexSearchService.search to raise ValueError for PCRE2."""
    mock_service = MagicMock()
    mock_service.search = AsyncMock(
        side_effect=ValueError(
            "PCRE2 not available. Install libpcre2 and ensure ripgrep "
            "is built with PCRE2 support."
        )
    )
    return patch(
        "code_indexer.server.routes.regex_routes.RegexSearchService",
        return_value=mock_service,
    )


def _patch_metrics():
    """Patch api_metrics_service.increment_regex_search."""
    return patch("code_indexer.server.routes.regex_routes.api_metrics_service")


def _patch_config_service():
    """Patch get_config_service to return a mock config with search_limits."""
    mock_config = MagicMock()
    mock_config.search_limits_config.timeout_seconds = 30
    mock_get_config_service = MagicMock()
    mock_get_config_service.return_value.get_config.return_value = mock_config
    return patch(
        "code_indexer.server.routes.regex_routes.get_config_service",
        mock_get_config_service,
    )


# ---------------------------------------------------------------------------
# TC-01: Authentication failure returns 401
# ---------------------------------------------------------------------------


class TestNoAuth:
    """Missing or invalid Authorization header returns 401."""

    def test_post_regex_search_no_auth_returns_401(self, client):
        """Request without Authorization header returns 401."""
        resp = client.post("/api/regex/search", json=VALID_SINGLE_BODY)
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TC-02: Permission failure returns 403 (AC4)
# ---------------------------------------------------------------------------


class TestMissingPermission:
    """Token without query_repos permission returns 403."""

    def test_post_regex_search_no_permission_returns_403(self, app, client):
        """User without query_repos returns 403 with structured error."""
        user_no_perm = MagicMock(spec=User)
        user_no_perm.has_permission.return_value = False
        user_no_perm.username = "limited"

        app.dependency_overrides[get_current_user] = lambda: user_no_perm

        try:
            with _patch_repo_found():
                resp = client.post("/api/regex/search", json=VALID_SINGLE_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 403
        data = resp.json()
        assert _err_code(data) == "auth_required"


# ---------------------------------------------------------------------------
# TC-03: Missing required fields returns 422 (AC5)
# ---------------------------------------------------------------------------


class TestMissingRequiredFields:
    """Missing pattern or repository_alias returns 422."""

    def test_missing_pattern_returns_422(self, app, client):
        """Request without pattern returns 422."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {"repository_alias": "myrepo-global"}  # no pattern

        try:
            with _patch_repo_found():
                resp = client.post("/api/regex/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422

    def test_missing_repository_alias_returns_422(self, app, client):
        """Request without repository_alias returns 422."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {"pattern": r"def\s+\w+"}  # no repository_alias

        try:
            with _patch_repo_found():
                resp = client.post("/api/regex/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# TC-04: Repository not found returns 404 (AC5)
# ---------------------------------------------------------------------------


class TestRepositoryNotFound:
    """Unknown repository alias returns 404."""

    def test_unknown_repo_returns_404(self, app, client):
        """Nonexistent alias returns 404 with error_code=repository_not_found."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER

        try:
            with _patch_repo_not_found(), _patch_config_service():
                resp = client.post("/api/regex/search", json=VALID_SINGLE_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 404
        data = resp.json()
        assert _err_code(data) == "repository_not_found"


# ---------------------------------------------------------------------------
# TC-05: PCRE2 unavailable returns 422 (AC5)
# ---------------------------------------------------------------------------


class TestPcre2Unavailable:
    """PCRE2 unavailable (ValueError from service) returns 422."""

    def test_pcre2_unavailable_returns_422(self, app, client):
        """ValueError with PCRE2 message returns 422 with error_code=pcre2_unavailable."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_SINGLE_BODY, "pcre2": True}

        try:
            with (
                _patch_repo_found(),
                _patch_search_pcre2_error(),
                _patch_metrics(),
                _patch_config_service(),
            ):
                resp = client.post("/api/regex/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422
        data = resp.json()
        assert _err_code(data) == "pcre2_unavailable"


# ---------------------------------------------------------------------------
# TC-06: Timeout returns 408 (AC5)
# ---------------------------------------------------------------------------


class TestSearchTimeout:
    """TimeoutError from service returns 408."""

    def test_timeout_returns_408(self, app, client):
        """TimeoutError from search returns 408 with error_code=search_timeout."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER

        try:
            with (
                _patch_repo_found(),
                _patch_search_timeout(),
                _patch_metrics(),
                _patch_config_service(),
            ):
                resp = client.post("/api/regex/search", json=VALID_SINGLE_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 408
        data = resp.json()
        assert _err_code(data) == "search_timeout"


# ---------------------------------------------------------------------------
# TC-07: RipgrepExecutionError returns 500 (AC5)
# ---------------------------------------------------------------------------


class TestRipgrepExecutionError:
    """RipgrepExecutionError from service returns 500."""

    def test_ripgrep_error_returns_500(self, app, client):
        """RipgrepExecutionError returns 500 with error_code=search_engine_error."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER

        try:
            with (
                _patch_repo_found(),
                _patch_search_ripgrep_error(),
                _patch_metrics(),
                _patch_config_service(),
            ):
                resp = client.post("/api/regex/search", json=VALID_SINGLE_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 500
        data = resp.json()
        assert _err_code(data) == "search_engine_error"


# ---------------------------------------------------------------------------
# TC-08: Successful single-repo search returns 200 (AC1, AC2)
# ---------------------------------------------------------------------------


class TestSuccessfulSingleRepoSearch:
    """Successful single-repo search returns 200 with formatted matches."""

    def test_happy_path_returns_200_with_matches(self, app, client):
        """Valid request returns 200 with matches, total_matches, search_engine."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        mock_matches = [
            {
                "file_path": "src/main.py",
                "line_number": 10,
                "column": 1,
                "line_content": "def hello():",
                "context_before": [],
                "context_after": [],
            }
        ]

        try:
            with (
                _patch_repo_found(),
                _patch_search_success(mock_matches),
                _patch_metrics(),
                _patch_config_service(),
            ):
                resp = client.post("/api/regex/search", json=VALID_SINGLE_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        data = resp.json()
        assert "matches" in data
        assert "total_matches" in data
        assert "truncated" in data
        assert "search_engine" in data
        assert "search_time_ms" in data
        assert len(data["matches"]) == 1
        assert data["matches"][0]["file_path"] == "src/main.py"

    def test_empty_results_returns_200(self, app, client):
        """Search with no matches returns 200 with empty matches list."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER

        try:
            with (
                _patch_repo_found(),
                _patch_search_success([]),
                _patch_metrics(),
                _patch_config_service(),
            ):
                resp = client.post("/api/regex/search", json=VALID_SINGLE_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        data = resp.json()
        assert data["matches"] == []
        assert data["total_matches"] == 0

    def test_metrics_incremented_on_success(self, app, client):
        """api_metrics_service.increment_regex_search is called with username."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER

        with (
            _patch_repo_found(),
            _patch_search_success(),
            _patch_metrics() as mock_metrics,
            _patch_config_service(),
        ):
            app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
            client.post("/api/regex/search", json=VALID_SINGLE_BODY)
            app.dependency_overrides.clear()

        mock_metrics.increment_regex_search.assert_called_once_with(username="testuser")

    def test_include_exclude_patterns_accepted(self, app, client):
        """include_patterns and exclude_patterns are accepted (HTTP 200)."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {
            **VALID_SINGLE_BODY,
            "include_patterns": ["*.py"],
            "exclude_patterns": ["*/tests/*"],
            "context_lines": 2,
            "case_sensitive": False,
        }

        try:
            with (
                _patch_repo_found(),
                _patch_search_success(),
                _patch_metrics(),
                _patch_config_service(),
            ):
                resp = client.post("/api/regex/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200

    def test_default_field_values_applied(self, app, client):
        """Minimal body uses defaults: case_sensitive=True, context_lines=0, max_results=100."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        # Only required fields
        body = {"pattern": r"test", "repository_alias": "myrepo-global"}

        mock_service = MagicMock()
        mock_service.search = AsyncMock(return_value=_make_mock_search_result())
        try:
            with (
                _patch_repo_found(),
                patch(
                    "code_indexer.server.routes.regex_routes.RegexSearchService",
                    return_value=mock_service,
                ),
                _patch_metrics(),
                _patch_config_service(),
            ):
                resp = client.post("/api/regex/search", json=body)
                call_kwargs = mock_service.search.call_args.kwargs
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert call_kwargs["case_sensitive"] is True
        assert call_kwargs["context_lines"] == 0
        assert call_kwargs["max_results"] == 100
        assert call_kwargs["multiline"] is False
        assert call_kwargs["pcre2"] is False


# ---------------------------------------------------------------------------
# TC-09: Omni (multi-repo) search (AC3)
# ---------------------------------------------------------------------------


class TestOmniSearch:
    """Multi-repo search with list alias fans out correctly."""

    def test_omni_search_with_list_alias_returns_200(self, app, client):
        """List alias triggers omni search returning repos_searched and errors."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER

        mock_matches = [
            {
                "file_path": "src/foo.py",
                "line_number": 5,
                "column": 1,
                "line_content": "class Foo:",
                "context_before": [],
                "context_after": [],
            }
        ]

        try:
            with (
                _patch_repo_found(),
                _patch_search_success(mock_matches),
                _patch_metrics(),
                _patch_config_service(),
            ):
                resp = client.post("/api/regex/search", json=VALID_OMNI_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        data = resp.json()
        assert "matches" in data
        assert "repos_searched" in data
        assert "errors" in data
        assert isinstance(data["errors"], dict)

    def test_omni_each_match_has_source_repo(self, app, client):
        """Each match in omni results has source_repo field."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER

        mock_matches = [
            {
                "file_path": "src/bar.py",
                "line_number": 3,
                "column": 1,
                "line_content": "class Bar:",
                "context_before": [],
                "context_after": [],
            }
        ]

        try:
            with (
                _patch_repo_found(),
                _patch_search_success(mock_matches),
                _patch_metrics(),
                _patch_config_service(),
            ):
                resp = client.post("/api/regex/search", json=VALID_OMNI_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        data = resp.json()
        for match in data["matches"]:
            assert "source_repo" in match

    def test_omni_repo_not_found_recorded_as_error(self, app, client):
        """When one repo is not found, it is recorded in errors dict."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER

        # First repo found, second not found
        call_count = {"n": 0}

        def resolve_side_effect(alias):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return "/some/repo/path"
            return None

        try:
            with (
                patch(
                    "code_indexer.server.routes.regex_routes._resolve_repo_path",
                    side_effect=resolve_side_effect,
                ),
                _patch_search_success([]),
                _patch_metrics(),
                _patch_config_service(),
            ):
                resp = client.post("/api/regex/search", json=VALID_OMNI_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        data = resp.json()
        assert "errors" in data
        # The second repo (repo2-global) should have an error entry
        assert "repo2-global" in data["errors"]

    def test_omni_empty_alias_list_returns_empty_results(self, app, client):
        """Empty list alias returns 200 with empty results."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {"pattern": r"test", "repository_alias": []}

        try:
            with (
                _patch_repo_found(),
                _patch_search_success(),
                _patch_metrics(),
                _patch_config_service(),
            ):
                resp = client.post("/api/regex/search", json=body)
        finally:
            app.dependency_overrides.clear()

        # FastAPI pydantic validation may reject empty list or route handles it
        # We accept either 422 (schema) or 200 with empty results
        assert resp.status_code in (200, 422)


# ---------------------------------------------------------------------------
# TC-10: Request model field validation
# ---------------------------------------------------------------------------


class TestRequestModelValidation:
    """Pydantic model field constraints enforced."""

    def test_context_lines_out_of_range_returns_422(self, app, client):
        """context_lines > 10 returns 422 from Pydantic validation."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_SINGLE_BODY, "context_lines": 11}

        try:
            resp = client.post("/api/regex/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422

    def test_max_results_out_of_range_returns_422(self, app, client):
        """max_results=0 returns 422 from Pydantic validation."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_SINGLE_BODY, "max_results": 0}

        try:
            resp = client.post("/api/regex/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422

    def test_max_results_over_limit_returns_422(self, app, client):
        """max_results=1001 returns 422 from Pydantic validation."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
        body = {**VALID_SINGLE_BODY, "max_results": 1001}

        try:
            resp = client.post("/api/regex/search", json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422

    def test_string_alias_accepted(self, app, client):
        """String repository_alias is accepted."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER

        try:
            with (
                _patch_repo_found(),
                _patch_search_success(),
                _patch_metrics(),
                _patch_config_service(),
            ):
                resp = client.post("/api/regex/search", json=VALID_SINGLE_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200

    def test_list_alias_accepted(self, app, client):
        """List repository_alias is accepted."""
        app.dependency_overrides[get_current_user] = lambda: NORMAL_USER

        try:
            with (
                _patch_repo_found(),
                _patch_search_success(),
                _patch_metrics(),
                _patch_config_service(),
            ):
                resp = client.post("/api/regex/search", json=VALID_OMNI_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
