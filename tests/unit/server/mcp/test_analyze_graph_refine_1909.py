"""Bug #1909: S3's refine phase (rust/xray-core/src/graph/refine.rs, the
`--refine` xray-cli subcommand) is fully built and tested in Rust, but had
ZERO callers anywhere under `src/code_indexer/` -- `xray_graph.py`'s pipeline
ran `--compile-only -> --build-graph -> --analyze-graph` and stopped, and the
shipped tool doc told a caller to populate `GraphResult.refine` while
admitting nothing consumed it ("Currently informational only -- this tool
does not yet invoke --refine automatically").

This module proves the fix: `refine` is a NEW, opt-in (`default: false`)
boolean request parameter. `--refine` is invoked ONLY when the caller passes
`refine: true` AND the evaluator's `analyze_graph` returned a non-empty
`GraphResult.refine` list (running a second per-file pass when nothing was
flagged is pure waste) -- both conditions are asserted independently as
cost guards, alongside the positive "it actually ran and merged real
per-file results into the response" proof.

These are genuine end-to-end tests against the REAL compiled xray-cli
release binary (skipped if not built), mirroring
test_analyze_graph_handler.py's own established convention: only
`_resolve_repo_path` is mocked (pointing at a real on-disk tmp_path repo),
everything downstream (validation, file collection, compile, build-graph,
analyze-graph, refine) runs for real.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, cast
from unittest.mock import patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.xray.rust_backend import RustNativeBackend, _XRAY_CLI_DEFAULT


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


def _write_cross_file_fixture(repo_root: Path) -> None:
    (repo_root / "A.java").write_text("class A { void run() { helper(); } }\n")
    (repo_root / "B.java").write_text("class B { void helper() {} }\n")


# Populates GraphResult.refine with EVERY resolvable dense symbol id (both
# A.run() and B.helper() in the 2-file fixture above), and defines a REAL
# `fn refine` that tags each visited file via FileContext.file -- so a
# non-empty refine_findings[] can only appear if `--refine` genuinely ran
# over real per-file ASTs, never a stub.
EVALUATOR_WITH_POPULATED_REFINE_SET = """\
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    let mut i: u32 = 0;
    while i < 64 {
        if let Some(sym) = g.resolve_symbol(i) {
            result.refine.push(sym);
        }
        i += 1;
    }
    result
}
fn refine(node: &OwnedNode, ctx: &FileContext, g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> Vec<EvalFinding> {
    vec![EvalFinding {
        pattern: "refine-visited".to_string(),
        line: node.start_line,
        snippet: ctx.file.clone(),
    }]
}
"""

# Same analyze_graph as above (a populated RefineSet) but with NO `fn
# refine` at all -- the "the compiled artifact does not export xray_refine"
# case, which must degrade to refine_status="absent" without failing the
# request.
EVALUATOR_WITH_POPULATED_REFINE_SET_BUT_NO_REFINE_FN = """\
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    let mut i: u32 = 0;
    while i < 64 {
        if let Some(sym) = g.resolve_symbol(i) {
            result.refine.push(sym);
        }
        i += 1;
    }
    result
}
"""

# analyze_graph never populates GraphResult.refine at all -- the "flagged
# nothing" cost-guard case.
EVALUATOR_WITH_EMPTY_REFINE_SET = """\
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    GraphResult::default()
}
fn refine(node: &OwnedNode, ctx: &FileContext, g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> Vec<EvalFinding> {
    vec![EvalFinding {
        pattern: "should-never-run".to_string(),
        line: node.start_line,
        snippet: ctx.file.clone(),
    }]
}
"""


@pytest.mark.asyncio
async def test_refine_true_with_populated_refine_set_produces_refine_findings(
    tmp_path: Path,
) -> None:
    """THE discriminating RED/GREEN proof: on the pre-fix tree, `refine:
    true` with a non-empty `GraphResult.refine` produced NOTHING -- no
    `--refine` invocation existed anywhere under `src/code_indexer/`. After
    the fix, the same request must produce real, per-file refine results
    (`refine_status == "ran"`, non-empty `refine_findings`, each one
    carrying a `file` from the real 2-file fixture) -- proving the whole
    front-door -> handler -> RustNativeBackend -> xray-cli --refine chain
    is wired, not merely present in Rust.
    """
    _require_xray_cli_binary()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_cross_file_fixture(repo_root)

    handler = _import_handler()
    params: Dict[str, Any] = {
        "repository_alias": "myrepo-global",
        "evaluator_code": EVALUATOR_WITH_POPULATED_REFINE_SET,
        "refine": True,
    }
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
        return_value=str(repo_root),
    ):
        result = await handler(params, _make_user())

    parsed = _parse_response(result)
    assert parsed.get("ok") is True, f"expected success, got: {parsed}"
    assert parsed["refine"], "fixture must have flagged a non-empty RefineSet"
    assert parsed.get("refine_status") == "ran", (
        f"refine=true with a non-empty RefineSet must actually invoke "
        f"--refine, got: {parsed}"
    )
    refine_findings = parsed.get("refine_findings")
    assert refine_findings, f"expected real refine findings, got: {parsed}"
    visited_files = {f["file"] for f in refine_findings}
    assert visited_files <= {"A.java", "B.java"}, (
        f"refine must only visit files from the real fixture, got: {visited_files}"
    )
    assert parsed.get("refine_files_examined") == len(visited_files)


@pytest.mark.asyncio
async def test_refine_false_by_default_never_invokes_refine_pass(
    tmp_path: Path,
) -> None:
    """Cost guard #1: `refine` defaults to False -- even with a populated
    RefineSet, omitting the parameter must never spawn the second
    subprocess. Proven both via the response's own `refine_status` AND by
    asserting the backend's refine step is never called.
    """
    _require_xray_cli_binary()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_cross_file_fixture(repo_root)

    handler = _import_handler()
    params: Dict[str, Any] = {
        "repository_alias": "myrepo-global",
        "evaluator_code": EVALUATOR_WITH_POPULATED_REFINE_SET,
    }
    with (
        patch(
            "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
            return_value=str(repo_root),
        ),
        patch.object(RustNativeBackend, "_run_refine_step") as mock_refine_step,
    ):
        result = await handler(params, _make_user())

    mock_refine_step.assert_not_called()
    parsed = _parse_response(result)
    assert parsed.get("ok") is True, f"expected success, got: {parsed}"
    assert parsed["refine"], "fixture must have flagged a non-empty RefineSet"
    assert parsed.get("refine_status") == "not_requested"
    assert parsed.get("refine_findings") == []


@pytest.mark.asyncio
async def test_refine_true_with_empty_refine_set_skips_invocation(
    tmp_path: Path,
) -> None:
    """Cost guard #2: `refine: true` but the evaluator flagged NOTHING must
    never spawn the second subprocess either -- running refine over zero
    files is pure waste. Proven both via `refine_status` and by asserting
    the backend's refine step is never called.
    """
    _require_xray_cli_binary()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_cross_file_fixture(repo_root)

    handler = _import_handler()
    params: Dict[str, Any] = {
        "repository_alias": "myrepo-global",
        "evaluator_code": EVALUATOR_WITH_EMPTY_REFINE_SET,
        "refine": True,
    }
    with (
        patch(
            "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
            return_value=str(repo_root),
        ),
        patch.object(RustNativeBackend, "_run_refine_step") as mock_refine_step,
    ):
        result = await handler(params, _make_user())

    mock_refine_step.assert_not_called()
    parsed = _parse_response(result)
    assert parsed.get("ok") is True, f"expected success, got: {parsed}"
    assert parsed["refine"] == [], "fixture must NOT have flagged anything"
    assert parsed.get("refine_status") == "skipped_empty_refine_set"
    assert parsed.get("refine_findings") == []


@pytest.mark.asyncio
async def test_refine_true_degrades_honestly_when_dylib_has_no_refine_export(
    tmp_path: Path,
) -> None:
    """A populated RefineSet but no `fn refine` at all in the evaluator must
    degrade to `refine_status == "absent"` -- never fail the whole request
    (the primary analyze_graph result, findings included, is what the
    caller came for)."""
    _require_xray_cli_binary()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_cross_file_fixture(repo_root)

    handler = _import_handler()
    params: Dict[str, Any] = {
        "repository_alias": "myrepo-global",
        "evaluator_code": EVALUATOR_WITH_POPULATED_REFINE_SET_BUT_NO_REFINE_FN,
        "refine": True,
    }
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
        return_value=str(repo_root),
    ):
        result = await handler(params, _make_user())

    parsed = _parse_response(result)
    assert parsed.get("ok") is True, (
        f"a missing xray_refine export must never fail the whole request, got: {parsed}"
    )
    assert parsed["refine"], "fixture must have flagged a non-empty RefineSet"
    assert parsed.get("refine_status") == "absent"
    assert parsed.get("refine_findings") == []


@pytest.mark.asyncio
async def test_refine_invalid_type_returns_structured_error() -> None:
    """`refine` must be a boolean -- a non-boolean value is rejected before
    any repo resolution, mirroring timeout_seconds_invalid's own
    fail-fast convention."""
    handler = _import_handler()
    params: Dict[str, Any] = {
        "repository_alias": "myrepo-global",
        "evaluator_code": EVALUATOR_WITH_EMPTY_REFINE_SET,
        "refine": "yes",
    }
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path"
    ) as mock_resolve:
        result = await handler(params, _make_user())

    mock_resolve.assert_not_called()
    assert _parse_response(result)["error"] == "refine_invalid"
