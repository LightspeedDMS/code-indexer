"""v10.4.4 tests for Finding 3.2: xray_dump_ast max_nodes parameter ignored.

The handler was hardcoding max_nodes=500 instead of reading it from params.
These tests verify user-supplied max_nodes is validated and forwarded to
_serialize_ast with the exact value.

Mocking strategy:
- _resolve_repo_path: mocked (needs live alias manager)
- XRaySearchEngine._serialize_ast: patched/spied to verify exact max_nodes arg
- AstSearchEngine: real for truncation/behavioral tests
- User/permission: real User model
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, cast
from unittest.mock import patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
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


def _call_handler(tmp_path, params: Dict[str, Any]) -> Dict[str, Any]:
    """Call handle_xray_dump_ast with _resolve_repo_path mocked to tmp_path."""
    user = _make_user(UserRole.NORMAL_USER)
    with patch(
        "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
        return_value=str(tmp_path),
    ):
        handler = _import_handler()
        result = handler(params, user)
    return _parse_response(result)


# ---------------------------------------------------------------------------
# Tests: max_nodes validation errors (no tree-sitter needed)
# ---------------------------------------------------------------------------


class TestMaxNodesValidation:
    """max_nodes parameter validation — invalid values return error responses."""

    def test_max_nodes_invalid_string(self, tmp_path):
        """max_nodes='abc' (non-numeric) → max_nodes_invalid error, not a crash."""
        py_file = tmp_path / "hello.py"
        py_file.write_text("x = 1\n")
        data = _call_handler(
            tmp_path,
            {
                "repository_alias": "myrepo-global",
                "file_path": "hello.py",
                "max_nodes": "abc",
            },
        )
        assert data.get("error") == "max_nodes_invalid", f"Got: {data}"

    def test_max_nodes_out_of_range_zero(self, tmp_path):
        """max_nodes=0 is below minimum (1) → max_nodes_out_of_range error."""
        py_file = tmp_path / "hello.py"
        py_file.write_text("x = 1\n")
        data = _call_handler(
            tmp_path,
            {
                "repository_alias": "myrepo-global",
                "file_path": "hello.py",
                "max_nodes": 0,
            },
        )
        assert data.get("error") == "max_nodes_out_of_range", f"Got: {data}"

    def test_max_nodes_out_of_range_too_large(self, tmp_path):
        """max_nodes=5000 exceeds maximum (2000) → max_nodes_out_of_range error."""
        py_file = tmp_path / "hello.py"
        py_file.write_text("x = 1\n")
        data = _call_handler(
            tmp_path,
            {
                "repository_alias": "myrepo-global",
                "file_path": "hello.py",
                "max_nodes": 5000,
            },
        )
        assert data.get("error") == "max_nodes_out_of_range", f"Got: {data}"


# ---------------------------------------------------------------------------
# Tests: max_nodes forwarded to _serialize_ast (spy-based, no tree-sitter needed)
# ---------------------------------------------------------------------------


class TestMaxNodesForwarding:
    """_serialize_ast is called with the exact max_nodes the user supplied."""

    def test_max_nodes_default_is_500(self, tmp_path):
        """Omitting max_nodes → _serialize_ast called with max_nodes=500 exactly."""
        py_file = tmp_path / "hello.py"
        py_file.write_text("def foo(): pass\n")

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value=str(tmp_path),
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray.XRaySearchEngine._serialize_ast",
                return_value={"type": "module", "children": []},
            ) as spy_serialize,
        ):
            # Also need to patch ast_engine.detect_language and parse so we don't
            # need tree-sitter installed
            with patch(
                "code_indexer.server.mcp.handlers.xray.XRaySearchEngine"
            ) as MockEngine:
                mock_instance = MockEngine.return_value
                mock_instance.ast_engine.detect_language.return_value = "python"
                mock_instance.ast_engine.parse.return_value = object()  # fake root node
                MockEngine._serialize_ast = spy_serialize

                user = _make_user(UserRole.NORMAL_USER)
                handler = _import_handler()
                handler(
                    {
                        "repository_alias": "myrepo-global",
                        "file_path": "hello.py",
                    },
                    user,
                )

        # Verify _serialize_ast was called with max_nodes=500 (the default)
        spy_serialize.assert_called_once()
        _, kwargs = spy_serialize.call_args
        assert kwargs.get("max_nodes") == 500, (
            f"Expected max_nodes=500 (default), got max_nodes={kwargs.get('max_nodes')}"
        )

    def test_max_nodes_user_value_actually_used(self, tmp_path):
        """max_nodes=15 → _serialize_ast called with max_nodes=15 exactly."""
        py_file = tmp_path / "hello.py"
        py_file.write_text("def foo(): pass\n")

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value=str(tmp_path),
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray.XRaySearchEngine._serialize_ast",
                return_value={"type": "module", "children": []},
            ) as spy_serialize,
        ):
            with patch(
                "code_indexer.server.mcp.handlers.xray.XRaySearchEngine"
            ) as MockEngine:
                mock_instance = MockEngine.return_value
                mock_instance.ast_engine.detect_language.return_value = "python"
                mock_instance.ast_engine.parse.return_value = object()
                MockEngine._serialize_ast = spy_serialize

                user = _make_user(UserRole.NORMAL_USER)
                handler = _import_handler()
                handler(
                    {
                        "repository_alias": "myrepo-global",
                        "file_path": "hello.py",
                        "max_nodes": 15,
                    },
                    user,
                )

        spy_serialize.assert_called_once()
        _, kwargs = spy_serialize.call_args
        assert kwargs.get("max_nodes") == 15, (
            f"Expected max_nodes=15, got max_nodes={kwargs.get('max_nodes')}"
        )


# ---------------------------------------------------------------------------
# Tests: observable truncation behaviour (requires tree-sitter extras)
# ---------------------------------------------------------------------------


class TestMaxNodesTruncation:
    """Truncation sentinel appears when max_nodes cap is hit."""

    def test_max_nodes_10_truncates_appropriately(self, tmp_path):
        """max_nodes=10 on a non-trivial file triggers truncation sentinel."""
        pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
        # Write a Python file with >10 AST nodes guaranteed
        py_file = tmp_path / "module.py"
        py_file.write_text(
            "class Foo:\n"
            "    def bar(self):\n"
            "        x = 1\n"
            "        y = 2\n"
            "        return x + y\n"
            "    def baz(self, a, b):\n"
            "        return a + b\n"
        )
        data = _call_handler(
            tmp_path,
            {
                "repository_alias": "myrepo-global",
                "file_path": "module.py",
                "max_nodes": 10,
            },
        )
        assert "ast_tree" in data, f"Expected ast_tree, got: {data}"
        tree_str = json.dumps(data["ast_tree"])
        assert "...truncated" in tree_str, (
            f"Expected '...truncated' sentinel with max_nodes=10, got: {tree_str[:500]}"
        )
