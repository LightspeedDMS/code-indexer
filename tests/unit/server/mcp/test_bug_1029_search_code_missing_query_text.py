"""Regression tests for Bug #1029 — search_code KeyError crash when query_text missing.

SEVERITY: HIGH — 130 crashes/24h in production.

Root cause: _build_search_kwargs() does a hard dict access params["query_text"]
at line 676 and line 799 in search.py.  search_code() at line 895 performs zero
validation on required parameters.

Fix: early validation in search_code() before routing; return clean error response
when query_text is missing, empty, or non-string.  Also change hard access at
line 799 to params.get("query_text", "") for defense-in-depth.

Test structure mirrors test_min_score_default.py which also calls search_code via
a -global repository path, patching the same infrastructure.
"""

import os
import json
import shutil
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_fake_user():
    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username="testuser",
        role=UserRole.ADMIN,
        email="test@example.com",
        password_hash="fakehash",
        created_at=datetime.now(timezone.utc),
    )


def _invoke_search_code(params, user, base_dir, mock_manager=None):
    """Invoke search_code with infrastructure patched out.

    Uses the same patch surface as test_min_score_default.py so that the call
    reaches the real search_code() validation logic without touching real
    repositories.  When mock_manager is None a MagicMock is created internally
    (validation should short-circuit before reaching the manager).
    """
    from code_indexer.server.mcp import handlers as h

    if mock_manager is None:
        mock_manager = MagicMock()

    repo_dir = os.path.join(base_dir, "repo")
    os.makedirs(repo_dir, exist_ok=True)
    golden_dir = os.path.join(base_dir, "golden")
    os.makedirs(golden_dir, exist_ok=True)

    mock_alias_manager = MagicMock()
    mock_alias_manager.read_alias.return_value = repo_dir
    mock_repo_entry = {"alias_name": "test-global", "repo_name": "test"}

    _empty_rerank_meta = {
        "reranker_used": False,
        "reranker_provider": None,
        "rerank_time_ms": 0,
        "reranker_status": {"status": "disabled"},
    }

    with patch.object(h, "_list_global_repos", return_value=[mock_repo_entry]):
        with patch.object(h, "_get_golden_repos_dir", return_value=golden_dir):
            with patch(
                "code_indexer.global_repos.alias_manager.AliasManager",
                return_value=mock_alias_manager,
            ):
                with patch(
                    "code_indexer.server.mcp.handlers._utils.app_module"
                ) as mock_app:
                    mock_app.semantic_query_manager = mock_manager
                    with patch.object(h, "_get_query_tracker", return_value=None):
                        with patch.object(
                            h, "_get_access_filtering_service", return_value=None
                        ):
                            with patch.object(
                                h,
                                "_apply_payload_truncation",
                                side_effect=lambda x: x,
                            ):
                                with patch.object(
                                    h, "_is_temporal_query", return_value=False
                                ):
                                    with patch.object(
                                        h,
                                        "_get_wiki_enabled_repos",
                                        return_value=set(),
                                    ):
                                        with patch(
                                            "code_indexer.server.mcp.handlers.search._load_category_map",
                                            return_value={},
                                        ):
                                            with patch(
                                                "code_indexer.server.mcp.handlers.search._apply_rerank_and_filter",
                                                side_effect=lambda results, *a, **kw: (
                                                    results,
                                                    _empty_rerank_meta,
                                                ),
                                            ):
                                                return h.search_code(params, user)


def _parse_response(result):
    """Extract the dict from an _mcp_response-wrapped result."""
    assert "content" in result, f"Expected MCP content wrapper, got: {result}"
    assert len(result["content"]) == 1
    return json.loads(result["content"][0]["text"])


# ---------------------------------------------------------------------------
# Bug #1029: query_text validation tests
# ---------------------------------------------------------------------------


class TestBug1029SearchCodeMissingQueryText:
    """search_code must return a clean error response when query_text is invalid.

    Before the fix these tests fail because params["query_text"] raises KeyError
    which bubbles up (or is caught by the outer except and converted to an
    unhelpful generic error).  After the fix they must pass with the specific
    error message.
    """

    def test_missing_query_text_returns_error_response(self):
        """search_code with no query_text must return a descriptive error, not KeyError.

        The pre-fix behavior catches KeyError in the outer except block and returns
        error="'query_text'" (raw key string).  The fix must return a proper message:
        "Missing required parameter: query_text".
        """
        user = _make_fake_user()
        params = {"repository_alias": "test-global", "limit": 5}

        base_dir = tempfile.mkdtemp()
        try:
            result = _invoke_search_code(params, user, base_dir)
        finally:
            shutil.rmtree(base_dir, ignore_errors=True)

        data = _parse_response(result)
        assert data["success"] is False, (
            f"Expected success=False for missing query_text, got: {data}"
        )
        assert data["error"] == "Missing required parameter: query_text", (
            f"Expected descriptive error message, got: {data['error']!r}. "
            "A raw KeyError string like \"'query_text'\" is not acceptable."
        )
        assert data["results"] == [], (
            f"Expected empty results list, got: {data['results']}"
        )

    def test_empty_string_query_text_returns_error_response(self):
        """search_code with query_text='' must return success=False."""
        user = _make_fake_user()
        params = {"repository_alias": "test-global", "query_text": "", "limit": 5}

        base_dir = tempfile.mkdtemp()
        try:
            result = _invoke_search_code(params, user, base_dir)
        finally:
            shutil.rmtree(base_dir, ignore_errors=True)

        data = _parse_response(result)
        assert data["success"] is False, (
            f"Expected success=False for empty query_text, got: {data}"
        )
        assert "query_text" in data["error"].lower(), (
            f"Error message must mention 'query_text', got: {data['error']!r}"
        )

    def test_whitespace_only_query_text_returns_error_response(self):
        """search_code with query_text='   ' (whitespace) must return success=False."""
        user = _make_fake_user()
        params = {"repository_alias": "test-global", "query_text": "   ", "limit": 5}

        base_dir = tempfile.mkdtemp()
        try:
            result = _invoke_search_code(params, user, base_dir)
        finally:
            shutil.rmtree(base_dir, ignore_errors=True)

        data = _parse_response(result)
        assert data["success"] is False, (
            f"Expected success=False for whitespace-only query_text, got: {data}"
        )
        assert "query_text" in data["error"].lower(), (
            f"Error message must mention 'query_text', got: {data['error']!r}"
        )

    def test_non_string_query_text_returns_error_response(self):
        """search_code with query_text=42 (non-string) must return success=False."""
        user = _make_fake_user()
        params = {"repository_alias": "test-global", "query_text": 42, "limit": 5}

        base_dir = tempfile.mkdtemp()
        try:
            result = _invoke_search_code(params, user, base_dir)
        finally:
            shutil.rmtree(base_dir, ignore_errors=True)

        data = _parse_response(result)
        assert data["success"] is False, (
            f"Expected success=False for non-string query_text, got: {data}"
        )
        assert "query_text" in data["error"].lower(), (
            f"Error message must mention 'query_text', got: {data['error']!r}"
        )

    def test_valid_query_text_reaches_search_manager(self):
        """Regression guard: valid query_text must still reach _perform_search."""
        from code_indexer.server.query.semantic_query_manager import QueryResult

        user = _make_fake_user()
        mock_manager = MagicMock()
        mock_manager._perform_search.return_value = [
            QueryResult(
                file_path="src/auth.py",
                line_number=1,
                code_snippet="def auth(): pass",
                similarity_score=0.45,
                repository_alias="test-global",
                source_provider="voyage",
            )
        ]
        params = {"repository_alias": "test-global", "query_text": "authentication"}

        base_dir = tempfile.mkdtemp()
        try:
            result = _invoke_search_code(params, user, base_dir, mock_manager)
        finally:
            shutil.rmtree(base_dir, ignore_errors=True)

        data = _parse_response(result)
        assert data["success"] is True, (
            f"Valid query_text must produce success=True, got: {data}"
        )
        assert mock_manager._perform_search.called, (
            "Valid query must reach _perform_search"
        )
