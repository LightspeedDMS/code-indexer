"""v10.4.3 Finding 3: invalid regex pre-validation in xray handlers.

Tests assert that handle_xray_search and handle_xray_explore reject
invalid non-PCRE2 regex patterns immediately (before alias resolution or
job submission) with error='invalid_regex'.

PCRE2 patterns with non-Python syntax must NOT be short-circuited here —
they are validated by ripgrep at execution time.

Mocking strategy (matches test_xray_search_handler.py pattern):
- _resolve_repo_path: mocked to return a fake path (should not be reached
  for invalid-regex cases)
- _get_background_job_manager: mocked to capture submit_job calls
- User: real User object (admin role) with query_repos permission
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_admin_user() -> User:
    """Build a real admin User (has query_repos permission)."""
    return User(
        username="admin",
        password_hash="$2b$12$x",
        role=UserRole.ADMIN,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    """Unwrap the MCP content envelope."""
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


def _base_search_params(**overrides: Any) -> Dict[str, Any]:
    """Minimal valid params for handle_xray_search."""
    params: Dict[str, Any] = {
        "repository_alias": "myrepo-global",
        "pattern": r"def\s+\w+",
        "search_target": "content",
    }
    params.update(overrides)
    return params


def _base_explore_params(**overrides: Any) -> Dict[str, Any]:
    """Minimal valid params for handle_xray_explore."""
    params: Dict[str, Any] = {
        "repository_alias": "myrepo-global",
        "pattern": r"def\s+\w+",
        "search_target": "content",
    }
    params.update(overrides)
    return params


# ---------------------------------------------------------------------------
# Tests: handle_xray_search
# ---------------------------------------------------------------------------


class TestXraySearchInvalidRegex:
    """handle_xray_search rejects bad non-PCRE2 patterns before job submission."""

    def test_search_invalid_regex_returns_invalid_regex_error(self):
        """Invalid non-PCRE2 pattern returns error='invalid_regex'."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        user = _make_admin_user()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-id-unused"

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/repo/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
        ):
            result = handle_xray_search(
                _base_search_params(pattern="[unclosed", pcre2=False), user
            )

        data = _parse_response(result)
        assert data.get("error") == "invalid_regex", (
            f"Expected error='invalid_regex', got {data!r}"
        )
        assert "message" in data, "Response must include a message field"

    def test_search_invalid_regex_does_not_submit_job(self):
        """submit_job must NOT be called when the pattern is invalid (non-PCRE2)."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        user = _make_admin_user()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-id-unused"

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/repo/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
        ):
            handle_xray_search(
                _base_search_params(pattern="[unclosed", pcre2=False), user
            )

        mock_bjm.submit_job.assert_not_called()

    def test_search_pcre2_invalid_regex_skips_pre_validation(self):
        """pcre2=True bypasses Python pre-validation; handler returns job_id."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        user = _make_admin_user()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "pcre2-job-id"

        mock_app = MagicMock()
        mock_app.background_job_manager = mock_bjm

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/repo/path",
            ),
            patch("code_indexer.server.mcp.handlers._utils.app_module", mock_app),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
        ):
            result = handle_xray_search(
                _base_search_params(pattern="[unclosed", pcre2=True), user
            )

        data = _parse_response(result)
        # With pcre2=True the Python pre-validation is skipped; handler submits
        # the job and returns job_id (ripgrep validates PCRE2 at execution time).
        assert "job_id" in data, (
            f"Expected job_id in response for pcre2=True, got {data!r}"
        )
        assert data.get("error") != "invalid_regex"


# ---------------------------------------------------------------------------
# Tests: handle_xray_explore
# ---------------------------------------------------------------------------


class TestXrayExploreInvalidRegex:
    """handle_xray_explore rejects bad non-PCRE2 patterns before job submission."""

    def test_explore_invalid_regex_returns_invalid_regex_error(self):
        """Invalid non-PCRE2 pattern returns error='invalid_regex'."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_explore

        user = _make_admin_user()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-id-unused"

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/repo/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
        ):
            result = handle_xray_explore(
                _base_explore_params(pattern="[unclosed", pcre2=False), user
            )

        data = _parse_response(result)
        assert data.get("error") == "invalid_regex", (
            f"Expected error='invalid_regex', got {data!r}"
        )
        assert "message" in data, "Response must include a message field"

    def test_explore_invalid_regex_does_not_submit_job(self):
        """submit_job must NOT be called when the pattern is invalid (non-PCRE2)."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_explore

        user = _make_admin_user()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-id-unused"

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/repo/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
        ):
            handle_xray_explore(
                _base_explore_params(pattern="[unclosed", pcre2=False), user
            )

        mock_bjm.submit_job.assert_not_called()

    def test_explore_pcre2_invalid_regex_skips_pre_validation(self):
        """pcre2=True bypasses Python pre-validation; handler returns job_id."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_explore

        user = _make_admin_user()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "pcre2-explore-job-id"

        mock_app = MagicMock()
        mock_app.background_job_manager = mock_bjm

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/repo/path",
            ),
            patch("code_indexer.server.mcp.handlers._utils.app_module", mock_app),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
        ):
            result = handle_xray_explore(
                _base_explore_params(pattern="[unclosed", pcre2=True), user
            )

        data = _parse_response(result)
        # With pcre2=True the Python pre-validation is skipped; handler submits
        # the job and returns job_id (ripgrep validates PCRE2 at execution time).
        assert "job_id" in data, (
            f"Expected job_id in response for pcre2=True, got {data!r}"
        )
        assert data.get("error") != "invalid_regex"
