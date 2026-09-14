"""Unit tests for the analyze_graph MCP handler — Story #1811 (S5, AC3/AC4).

Tests the thin handler shim that validates inputs, pre-flight checks the
evaluator, resolves the repository alias, collects candidate files, and
delegates to RustNativeBackend.run_graph_analysis (Story #1811 AC2) off the
event loop.

Mocking strategy: `_resolve_repo_path` is mocked to point at a REAL on-disk
tmp_path repo -- mirroring test_xray_search_handler.py's own established
convention, since real golden-repo alias resolution needs a live server.
Everything downstream of that one boundary (validation, file collection,
compile, build-graph, analyze-graph) runs for REAL against the actual
compiled xray-cli binary -- no mocking of the analysis pipeline itself.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, cast
from unittest.mock import patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.xray.rust_backend import _XRAY_CLI_DEFAULT


def _require_xray_cli_binary() -> None:
    if not _XRAY_CLI_DEFAULT.exists():
        pytest.skip(
            f"xray-cli binary not built at {_XRAY_CLI_DEFAULT}; "
            "run 'cargo build --release' inside rust/ to enable this test."
        )


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username="testuser",
        password_hash="not-a-real-credential",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


def _import_handler():
    from code_indexer.server.mcp.handlers.xray_graph import handle_analyze_graph

    return handle_analyze_graph


CROSS_FILE_DEAD_CODE_EVALUATOR = """\
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    let mut i: u32 = 0;
    while i < 64 {
        if let Some(sym) = g.resolve_symbol(i) {
            if g.is_definitely_dead_code(i) == Some(false) {
                let sig = g.signature_for(i).unwrap_or("").to_string();
                result.findings.push(ReduceFinding {
                    pattern: "not_dead".to_string(),
                    message: sig.clone(),
                    involved: vec![sym],
                    signatures: vec![sig],
                });
            }
        }
        i += 1;
    }
    result
}
"""

UNSAFE_EVALUATOR = """\
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    unsafe {}
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    GraphResult::default()
}
"""

VALID_PARAMS: Dict[str, Any] = {
    "repository_alias": "myrepo-global",
    "evaluator_code": CROSS_FILE_DEAD_CODE_EVALUATOR,
}


@pytest.mark.asyncio
async def test_analyze_graph_requires_authentication() -> None:
    handler = _import_handler()
    # `None` is the real "no authenticated user" value the MCP dispatcher
    # passes for an unauthenticated call -- the handler's own contract is
    # `if user is None or not user.has_permission(...)`, mirroring
    # handle_xray_search's identical guard. The type: ignore below silences
    # the static `User` annotation for this one intentional None case.
    result = await handler(dict(VALID_PARAMS), None)  # type: ignore[arg-type]
    assert _parse_response(result)["error"] == "auth_required"


@pytest.mark.asyncio
async def test_analyze_graph_missing_evaluator_code_returns_structured_error() -> None:
    handler = _import_handler()
    params = dict(VALID_PARAMS)
    params["evaluator_code"] = ""
    result = await handler(params, _make_user())
    assert _parse_response(result)["error"] == "evaluator_code_required"


@pytest.mark.asyncio
async def test_analyze_graph_missing_repository_alias_returns_structured_error() -> (
    None
):
    handler = _import_handler()
    params = dict(VALID_PARAMS)
    params["repository_alias"] = ""
    result = await handler(params, _make_user())
    assert _parse_response(result)["error"] == "repository_alias_required"


@pytest.mark.asyncio
async def test_analyze_graph_unresolvable_alias_returns_repository_not_found() -> None:
    handler = _import_handler()
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
        return_value=None,
    ):
        result = await handler(dict(VALID_PARAMS), _make_user())
    assert _parse_response(result)["error"] == "repository_not_found"


@pytest.mark.asyncio
async def test_analyze_graph_forbidden_construct_never_touches_filesystem() -> None:
    """A forbidden-construct evaluator must be rejected at validation time --
    never reaching _resolve_repo_path/file collection/subprocess spawn.
    """
    handler = _import_handler()
    params = dict(VALID_PARAMS)
    params["evaluator_code"] = UNSAFE_EVALUATOR
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path"
    ) as mock_resolve:
        result = await handler(params, _make_user())
    mock_resolve.assert_not_called()
    parsed = _parse_response(result)
    assert parsed["error"] == "xray_evaluator_validation_failed"
    assert "unsafe" in parsed["message"].lower()


@pytest.mark.asyncio
async def test_analyze_graph_cross_file_reference_through_the_real_handler(
    tmp_path: Path,
) -> None:
    """Proves the real analysis pipeline (validation, file collection,
    compile, build-graph, analyze-graph) runs correctly through the actual
    handler, with only repo-alias resolution mocked: helper() declared in
    B.java, called only from A.java, resolves as NOT dead code -- provable
    only when the graph spans both files.

    The Java fixture below (an UNQUALIFIED `helper()` call, no `new
    B()`/`B.helper()` receiver) is deliberate, not a compile-validity bug:
    X-Ray parses source via tree-sitter and resolves cross-file references
    via NAME-based candidate binding, never real javac type-checking. This
    is the exact SAME fixture the real, pre-existing Rust test
    `repo_index.rs::tests::build_repo_graph_reports_complete_when_within_
    budget_and_untruncated_and_error_free` already uses verbatim.

    The evaluator (CROSS_FILE_DEAD_CODE_EVALUATOR) appends a finding for a
    symbol ONLY when `is_definitely_dead_code(i) == Some(false)` (i.e.
    genuinely referenced/reachable) and always tags it `pattern="not_dead"`
    -- never for a dead symbol -- so asserting BOTH the finding's presence
    AND its pattern makes the "reachable, not dead" claim explicit at the
    assertion level, not merely implicit in the evaluator's construction.
    """
    _require_xray_cli_binary()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "A.java").write_text("class A { void run() { helper(); } }\n")
    (repo_root / "B.java").write_text("class B { void helper() {} }\n")

    handler = _import_handler()
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
        return_value=str(repo_root),
    ):
        result = await handler(dict(VALID_PARAMS), _make_user())

    parsed = _parse_response(result)
    assert parsed.get("ok") is True, f"expected success, got: {parsed}"
    assert parsed["fact_graph_complete"] is True
    findings = parsed["findings"]
    b_findings = [
        f
        for f in findings
        if any("helper" in str(sig) for sig in f.get("signatures", []))
    ]
    assert len(b_findings) == 1, f"expected exactly one helper() finding: {findings}"
    assert b_findings[0]["pattern"] == "not_dead", (
        f"helper() must be classified reachable (not_dead), got: {b_findings[0]}"
    )


# H7 (consolidated review, Issue #1811/Bug #1812) wiring-test constants.
_H7_PREVIEW_SIZE_CHARS = 200
_H7_LARGE_FINDING_COUNT = 30
_H7_FINDING_MESSAGE_LENGTH = 80
_H7_SIGNATURE_PADDING_LENGTH = 60
_H7_TEST_COMPILE_MS = 1
_H7_EXPECTED_INLINE_LIMIT = 3
_H7_SAMPLE_INVOLVED_SYMBOL_IDS = [1, 2, 3]
_H7_ZERO_PARSE_ERRORS = 0


class _FakePayloadCacheConfigH7:
    preview_size_chars: int = _H7_PREVIEW_SIZE_CHARS


class _FakePayloadCacheH7:
    """Minimal fake PayloadCache -- mirrors test_xray_payload_cache.py's
    own fake, used here only to prove the WIRING (handler calls the
    truncation helper), not to re-test the helper's own mechanics."""

    def __init__(self, preview_size_chars: int = _H7_PREVIEW_SIZE_CHARS) -> None:
        self.config = _FakePayloadCacheConfigH7()
        self.config.preview_size_chars = preview_size_chars
        self._counter = 0

    def store(self, content: str) -> str:
        self._counter += 1
        return f"fake-handle-{self._counter}"

    def truncate_result(self, content: str) -> dict:
        preview_size = self.config.preview_size_chars
        if len(content) > preview_size:
            return {
                "preview": content[:preview_size],
                "cache_handle": self.store(content),
                "has_more": True,
                "total_size": len(content),
            }
        return {"content": content, "cache_handle": None, "has_more": False}


def _build_h7_large_result() -> Dict[str, Any]:
    """A findings/refine fixture large enough to exceed
    _H7_PREVIEW_SIZE_CHARS once serialized -- the raw pipeline result the
    real `_run_analyze_graph_pipeline` is mocked to return."""
    return {
        "ok": True,
        "status": "ran_ok",
        "findings": [
            {
                "pattern": f"pattern_{i}",
                "message": "x" * _H7_FINDING_MESSAGE_LENGTH,
                "involved": list(_H7_SAMPLE_INVOLVED_SYMBOL_IDS),
                "signatures": ["fn sig " + "y" * _H7_SIGNATURE_PADDING_LENGTH],
            }
            for i in range(_H7_LARGE_FINDING_COUNT)
        ],
        "refine": list(range(_H7_LARGE_FINDING_COUNT)),
        "fact_graph_complete": True,
        "build_status": "ok",
        "degradation": {"files_with_parse_errors": _H7_ZERO_PARSE_ERRORS},
        "cached": False,
        "compile_ms": _H7_TEST_COMPILE_MS,
    }


def _build_h7_fake_app_with_cache() -> Any:
    """A MagicMock app whose app.state.payload_cache is the fake cache,
    for patching onto code_indexer.server.mcp.handlers._utils.app_module."""
    from unittest.mock import MagicMock

    mock_state = MagicMock()
    mock_state.payload_cache = _FakePayloadCacheH7()
    mock_app = MagicMock()
    mock_app.state = mock_state
    return mock_app


@pytest.mark.asyncio
async def test_analyze_graph_large_result_is_routed_through_payload_cache_truncation() -> (
    None
):
    """H7 (consolidated review, Issue #1811/Bug #1812): a large findings/
    refine result must come back through handle_analyze_graph with
    PayloadCache truncation markers (cache_handle/has_more/truncated) and
    only 3 findings inline. Spies on `_truncate_graph_result` itself
    (wrapping the REAL implementation, so behavior is unchanged) to prove
    the handler actually CALLS it with the pipeline's result -- not just
    that the response happens to carry the right shape by coincidence."""
    from unittest.mock import AsyncMock, MagicMock

    from code_indexer.server.mcp.handlers import xray_graph as xray_graph_module
    from code_indexer.server.mcp.handlers.xray import (
        _truncate_graph_result as real_truncate_graph_result,
    )

    handler = _import_handler()
    large_result = _build_h7_large_result()
    mock_app = _build_h7_fake_app_with_cache()
    truncate_spy = MagicMock(wraps=real_truncate_graph_result)

    with (
        patch(
            "code_indexer.server.mcp.handlers.xray_graph._run_analyze_graph_pipeline",
            new=AsyncMock(return_value=large_result),
        ),
        patch.object(xray_graph_module, "_truncate_graph_result", truncate_spy),
        patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ),
    ):
        result = await handler(dict(VALID_PARAMS), _make_user())

    truncate_spy.assert_called_once_with(large_result)

    parsed = _parse_response(result)
    assert parsed["cache_handle"] is not None, (
        f"expected the handler to route the large result through PayloadCache "
        f"truncation, got: {parsed}"
    )
    assert parsed["has_more"] is True
    assert parsed["truncated"] is True
    assert len(parsed["findings"]) == _H7_EXPECTED_INLINE_LIMIT
    assert len(parsed["refine"]) == _H7_EXPECTED_INLINE_LIMIT
    assert parsed["fact_graph_complete"] is True
