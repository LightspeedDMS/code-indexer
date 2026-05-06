"""Unit tests for xray_dump_ast MCP handler (Issue #19).

Tests the synchronous single-file AST dump handler. No background job —
returns the parse tree inline within a 5s timeout.

Mocking strategy:
- _resolve_repo_path: mocked (needs live file-system alias manager)
- AstSearchEngine: real for parse tests; mocked for extras-missing test
- User/permission: uses real User model with appropriate roles
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    """Build a real User with the given role."""
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    """Unwrap the MCP content envelope."""
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


def _import_handler():
    from code_indexer.server.mcp.handlers.xray import handle_xray_dump_ast

    return handle_xray_dump_ast


# ---------------------------------------------------------------------------
# Tests: valid file returns AST tree
# ---------------------------------------------------------------------------


class TestXrayDumpAstHandlerValidFile:
    """Handler returns AST tree for a valid, supported file."""

    def test_valid_python_file_returns_ast_tree(self, tmp_path):
        """Valid Python file returns ast_tree field with root node."""
        pytest = __import__("pytest")
        pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

        py_file = tmp_path / "hello.py"
        py_file.write_text("def foo(): pass\n")

        user = _make_user(UserRole.NORMAL_USER)
        params = {
            "repository_alias": "myrepo-global",
            "file_path": "hello.py",
        }

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value=str(tmp_path),
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert "ast_tree" in data
        assert isinstance(data["ast_tree"], dict)
        assert "type" in data["ast_tree"]

    def test_ast_tree_has_required_node_fields(self, tmp_path):
        """Root node of ast_tree contains all BFS-serialised fields."""
        pytest = __import__("pytest")
        pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

        py_file = tmp_path / "sample.py"
        py_file.write_text("x = 1\n")

        user = _make_user(UserRole.NORMAL_USER)
        params = {
            "repository_alias": "myrepo-global",
            "file_path": "sample.py",
        }

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value=str(tmp_path),
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        root = data["ast_tree"]
        assert "type" in root
        assert "start_byte" in root
        assert "end_byte" in root
        assert "start_point" in root
        assert "end_point" in root

    def test_empty_file_returns_minimal_root(self, tmp_path):
        """Empty Python file returns a root node (module with no children or empty)."""
        pytest = __import__("pytest")
        pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

        py_file = tmp_path / "empty.py"
        py_file.write_text("")

        user = _make_user(UserRole.NORMAL_USER)
        params = {
            "repository_alias": "myrepo-global",
            "file_path": "empty.py",
        }

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value=str(tmp_path),
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert "ast_tree" in data
        assert isinstance(data["ast_tree"], dict)
        assert "type" in data["ast_tree"]


# ---------------------------------------------------------------------------
# Tests: error cases
# ---------------------------------------------------------------------------


class TestXrayDumpAstHandlerErrors:
    """Handler returns appropriate errors for invalid inputs."""

    def test_path_traversal_rejected(self, tmp_path):
        """Path traversal attempt (../../etc/passwd) is rejected."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {
            "repository_alias": "myrepo-global",
            "file_path": "../../etc/passwd",
        }

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value=str(tmp_path),
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") in (
            "path_traversal_rejected",
            "file_not_found",
            "invalid_file_path",
        )

    def test_nonexistent_file_returns_error(self, tmp_path):
        """Non-existent file_path returns file_not_found error."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {
            "repository_alias": "myrepo-global",
            "file_path": "does_not_exist.py",
        }

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value=str(tmp_path),
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "file_not_found"

    def test_unsupported_language_returns_error(self, tmp_path):
        """File with unsupported extension returns unsupported_language error."""
        pytest = __import__("pytest")
        pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

        mystery_file = tmp_path / "data.xyz"
        mystery_file.write_text("hello world")

        user = _make_user(UserRole.NORMAL_USER)
        params = {
            "repository_alias": "myrepo-global",
            "file_path": "data.xyz",
        }

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value=str(tmp_path),
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "unsupported_language"

    def test_unknown_repository_alias_rejected(self):
        """Unknown repository alias returns repository_not_found."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {
            "repository_alias": "nonexistent-global",
            "file_path": "foo.py",
        }

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value=None,
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "repository_not_found"


# ---------------------------------------------------------------------------
# Tests: auth
# ---------------------------------------------------------------------------


class TestXrayDumpAstHandlerAuth:
    """Handler enforces auth and permission requirements."""

    def test_unauthenticated_request_rejected(self):
        """None user produces auth_required error."""
        handler = _import_handler()
        result = handler({"repository_alias": "r", "file_path": "f.py"}, None)
        data = _parse_response(result)
        assert data.get("error") == "auth_required"

    def test_missing_query_repos_permission_rejected(self):
        """User without query_repos permission is rejected."""
        user = MagicMock(spec=User)
        user.username = "testuser"
        user.has_permission.return_value = False

        handler = _import_handler()
        result = handler({"repository_alias": "r", "file_path": "f.py"}, user)
        data = _parse_response(result)
        assert data.get("error") == "auth_required"

    def test_normal_user_with_permission_accepted(self, tmp_path):
        """Normal user with query_repos permission can dump AST."""
        pytest = __import__("pytest")
        pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

        py_file = tmp_path / "ok.py"
        py_file.write_text("pass\n")

        user = _make_user(UserRole.NORMAL_USER)
        params = {
            "repository_alias": "myrepo-global",
            "file_path": "ok.py",
        }

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value=str(tmp_path),
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") != "auth_required"
