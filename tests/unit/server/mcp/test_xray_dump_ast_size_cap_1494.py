"""Story #1494 AC1: xray_dump_ast no longer performs an unbounded
in-process tree-sitter parse.

Finding A2 (GIL-blocking analysis report, HIGH): tree-sitter 0.21.3 holds
the GIL for the entire parse (measured 0.89x thread scaling -- i.e. no
release at all; 159ms whole-process freeze parsing a 759KB file).
`xray_dump_ast` called `engine.ast_engine.parse(source_bytes, lang)` fully
in-process with no cap on `target.read_bytes()` -- `max_nodes` bounds only
the serialized output, not the parse itself.

Mitigation chosen (report's fallback #2, since the Rust `xray-cli`
subprocess has no AST-dump capability -- verified by inspecting
rust/xray-cli/src/main.rs, which only supports --dynlib/--files/--json
evaluator-scan mode, no AST-dump subcommand/flag): a file-size cap on the
in-process parse. A file above the cap is refused with a clear, actionable
error instead of an unbounded freeze; a file within the cap is parsed
exactly as before.

Real handler, real tree-sitter parsing for in-limit files -- no mocking of
the parse path itself.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, cast
from unittest.mock import patch

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import xray as xray_handlers

_HUGE_FILE_FUNCTION_INDEX = 0
_HUGE_FILE_REPEAT_COUNT = 40000
_HUGE_FILE_MIN_EXPECTED_SIZE_BYTES = 1024 * 100
_SINGLE_REQUEST_BOUND_SECONDS = 0.05
_CONCURRENT_TEST_CAP_BYTES = 1024
_CONCURRENT_THREAD_COUNT = 8
_CONCURRENT_BOUND_SECONDS = 0.2


def _make_huge_python_source(repeat_count: int) -> str:
    assert repeat_count >= 0, "repeat_count must be non-negative"
    i = _HUGE_FILE_FUNCTION_INDEX
    return f"def f_{i}(): return {i}\n" * repeat_count


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


class TestXrayDumpAstSizeCap:
    def test_size_cap_is_a_named_module_level_constant(self) -> None:
        """Must be an explicit named constant, never a magic number inline
        in the handler."""
        assert hasattr(xray_handlers, "_DUMP_AST_MAX_FILE_SIZE_BYTES")
        assert isinstance(xray_handlers._DUMP_AST_MAX_FILE_SIZE_BYTES, int)
        assert xray_handlers._DUMP_AST_MAX_FILE_SIZE_BYTES > 0

    def test_file_within_limit_returns_ast_tree_unchanged(self, tmp_path) -> None:
        """A file within the size limit is parsed exactly as before --
        real tree-sitter parse, real AST tree returned."""
        import pytest

        pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

        py_file = tmp_path / "small.py"
        py_file.write_text("def foo(): pass\n")

        user = _make_user()
        params = {"repository_alias": "myrepo-global", "file_path": "small.py"}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value=str(tmp_path),
        ):
            result = xray_handlers.handle_xray_dump_ast(params, user)

        data = _parse_response(result)
        assert "ast_tree" in data
        assert isinstance(data["ast_tree"], dict)
        assert "type" in data["ast_tree"]

    def test_file_exceeding_limit_is_refused_with_actionable_error(
        self, tmp_path, monkeypatch
    ) -> None:
        """A file above the cap must never reach the in-process parse --
        it gets a clear, actionable error instead."""
        monkeypatch.setattr(xray_handlers, "_DUMP_AST_MAX_FILE_SIZE_BYTES", 100)

        big_file = tmp_path / "big.py"
        big_file.write_text("x = 1\n" * 100)  # well over 100 bytes
        assert big_file.stat().st_size > 100

        user = _make_user()
        params = {"repository_alias": "myrepo-global", "file_path": "big.py"}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value=str(tmp_path),
        ):
            result = xray_handlers.handle_xray_dump_ast(params, user)

        data = _parse_response(result)
        assert data.get("error") == "file_too_large"
        assert "message" in data
        assert "100" in data["message"]
        assert "ast_tree" not in data

    def test_oversized_file_request_returns_near_instantly(
        self, tmp_path, monkeypatch
    ) -> None:
        """The refusal must happen BEFORE any parse attempt -- proven by
        timing: a request against a file far larger than the cap must
        return in well under the time a real parse of that much source
        would take, demonstrating the freeze is bounded (rejected), not
        proportional to file size."""
        monkeypatch.setattr(xray_handlers, "_DUMP_AST_MAX_FILE_SIZE_BYTES", 1024)

        huge_file = tmp_path / "huge.py"
        # ~1 MB of real, syntactically valid-looking Python source.
        huge_file.write_text(_make_huge_python_source(_HUGE_FILE_REPEAT_COUNT))
        assert huge_file.stat().st_size > 1024 * 100

        user = _make_user()
        params = {"repository_alias": "myrepo-global", "file_path": "huge.py"}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value=str(tmp_path),
        ):
            start = time.perf_counter()
            result = xray_handlers.handle_xray_dump_ast(params, user)
            elapsed = time.perf_counter() - start

        data = _parse_response(result)
        assert data.get("error") == "file_too_large"
        # A rejection based on os.stat() alone is microseconds; generously
        # bound it well under what a real multi-hundred-KB tree-sitter
        # parse would cost (the report measured 159ms for 759KB).
        assert elapsed < _SINGLE_REQUEST_BOUND_SECONDS, (
            f"Oversized-file rejection took {elapsed:.4f}s -- suggests the "
            f"parse ran anyway instead of being refused up front"
        )

    def test_concurrent_requests_against_oversized_file_stay_bounded(
        self, tmp_path, monkeypatch
    ) -> None:
        """AC1 mandatory real concurrent-thread evidence: N real threads
        calling the real handler against a real oversized file must
        complete in near the SAME bounded time as one call -- proving the
        cap-based rejection (a cheap os.stat()) scales with concurrency
        instead of a per-call GIL-held parse accumulating N x freeze time."""
        monkeypatch.setattr(
            xray_handlers, "_DUMP_AST_MAX_FILE_SIZE_BYTES", _CONCURRENT_TEST_CAP_BYTES
        )

        huge_file = tmp_path / "concurrent_huge.py"
        huge_file.write_text(_make_huge_python_source(_HUGE_FILE_REPEAT_COUNT))
        assert huge_file.stat().st_size > _HUGE_FILE_MIN_EXPECTED_SIZE_BYTES

        user = _make_user()
        params = {
            "repository_alias": "myrepo-global",
            "file_path": "concurrent_huge.py",
        }

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value=str(tmp_path),
        ):
            start = time.perf_counter()
            with ThreadPoolExecutor(max_workers=_CONCURRENT_THREAD_COUNT) as pool:
                futures = [
                    pool.submit(xray_handlers.handle_xray_dump_ast, params, user)
                    for _ in range(_CONCURRENT_THREAD_COUNT)
                ]
                results = [f.result() for f in futures]
            elapsed = time.perf_counter() - start

        for result in results:
            assert _parse_response(result).get("error") == "file_too_large"

        assert elapsed < _CONCURRENT_BOUND_SECONDS, (
            f"{_CONCURRENT_THREAD_COUNT} concurrent oversized-file requests "
            f"took {elapsed:.4f}s -- suggests work scales with file "
            f"size/thread count instead of being bounded by the cheap "
            f"size-cap rejection"
        )
