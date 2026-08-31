"""Phase 3 — AC3: MCP SCIP tools respond via in-process TestClient.

Verifies that SCIP code-intelligence MCP tools return a valid JSON-RPC 2.0
response shape.  Tests accept HTTP 200 (tool executed) or 4xx with non-empty
body (tool registered but fails for missing SCIP index).  HTTP 5xx is failure.

Tool names sourced from tool_docs/scip/*.md name fields.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.e2e.server.mcp_helpers import (
    FIELD_ERROR,
    FIELD_JSONRPC,
    FIELD_RESULT,
    HTTP_OK,
    HTTP_SERVER_ERROR,
    JsonArgs,
    MAX_ERROR_SNIPPET,
    PARAMETRIZE_FIELDS,
    TOOL_LABEL_INDEX,
    call_mcp_tool,
    parse_mcp_result,
)

# ---------------------------------------------------------------------------
# Tool name constants (scip category)
# ---------------------------------------------------------------------------
TOOL_SCIP_DEFINITION: str = "scip_definition"
TOOL_SCIP_REFERENCES: str = "scip_references"
TOOL_SCIP_DEPENDENCIES: str = "scip_dependencies"
TOOL_SCIP_DEPENDENTS: str = "scip_dependents"
TOOL_SCIP_IMPACT: str = "scip_impact"
TOOL_SCIP_CONTEXT: str = "scip_context"
TOOL_SCIP_CALLCHAIN: str = "scip_callchain"
TOOL_SCIP_CLEANUP_STATUS: str = "scip_cleanup_status"
TOOL_SCIP_CLEANUP_HISTORY: str = "scip_cleanup_history"
TOOL_SCIP_CLEANUP_WORKSPACES: str = "scip_cleanup_workspaces"
TOOL_SCIP_PR_HISTORY: str = "scip_pr_history"

# ---------------------------------------------------------------------------
# Argument key and value constants
# ---------------------------------------------------------------------------
ARG_KEY_REPOSITORY_ALIAS: str = "repository_alias"
ARG_KEY_SYMBOL: str = "symbol"
ARG_KEY_FROM_SYMBOL: str = "from_symbol"
ARG_KEY_TO_SYMBOL: str = "to_symbol"
ARG_ALIAS_CIDX_META: str = "cidx-meta"
ARG_SYMBOL_TEST: str = "test"

# ---------------------------------------------------------------------------
# Parametrize table: (label, tool_name, arguments)
# ---------------------------------------------------------------------------
SCIP_TOOLS: list[tuple[str, str, JsonArgs]] = [
    (
        "scip_definition",
        TOOL_SCIP_DEFINITION,
        {
            ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META,
            ARG_KEY_SYMBOL: ARG_SYMBOL_TEST,
        },
    ),
    (
        "scip_references",
        TOOL_SCIP_REFERENCES,
        {
            ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META,
            ARG_KEY_SYMBOL: ARG_SYMBOL_TEST,
        },
    ),
    (
        "scip_dependencies",
        TOOL_SCIP_DEPENDENCIES,
        {
            ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META,
            ARG_KEY_SYMBOL: ARG_SYMBOL_TEST,
        },
    ),
    (
        "scip_dependents",
        TOOL_SCIP_DEPENDENTS,
        {
            ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META,
            ARG_KEY_SYMBOL: ARG_SYMBOL_TEST,
        },
    ),
    (
        "scip_impact",
        TOOL_SCIP_IMPACT,
        {
            ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META,
            ARG_KEY_SYMBOL: ARG_SYMBOL_TEST,
        },
    ),
    (
        "scip_context",
        TOOL_SCIP_CONTEXT,
        {
            ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META,
            ARG_KEY_SYMBOL: ARG_SYMBOL_TEST,
        },
    ),
    (
        "scip_callchain",
        TOOL_SCIP_CALLCHAIN,
        {
            ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META,
            ARG_KEY_FROM_SYMBOL: ARG_SYMBOL_TEST,
            ARG_KEY_TO_SYMBOL: ARG_SYMBOL_TEST,
        },
    ),
    (
        "scip_cleanup_status",
        TOOL_SCIP_CLEANUP_STATUS,
        {},
    ),
    (
        "scip_cleanup_history",
        TOOL_SCIP_CLEANUP_HISTORY,
        {},
    ),
    (
        "scip_cleanup_workspaces",
        TOOL_SCIP_CLEANUP_WORKSPACES,
        {},
    ),
    (
        "scip_pr_history",
        TOOL_SCIP_PR_HISTORY,
        {ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META},
    ),
]


@pytest.mark.parametrize(
    PARAMETRIZE_FIELDS,
    SCIP_TOOLS,
    ids=[str(t[TOOL_LABEL_INDEX]) for t in SCIP_TOOLS],
)
def test_mcp_scip_tool(
    label: str,
    tool: str,
    params: JsonArgs,
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    """Each MCP SCIP tool returns a valid JSON-RPC 2.0 response.

    Accepts HTTP 200 (tool executed) or 4xx with non-empty body (tool
    registered but fails due to missing SCIP index — informative error).
    Fails on HTTP 5xx (unhandled server error).
    """
    resp = call_mcp_tool(test_client, tool, params, auth_headers)
    assert resp.status_code < HTTP_SERVER_ERROR, (
        f"{label}: server error {resp.status_code} — {resp.text[:MAX_ERROR_SNIPPET]}"
    )
    if resp.status_code == HTTP_OK:
        body = resp.json()
        assert FIELD_JSONRPC in body, (
            f"{label}: missing {FIELD_JSONRPC!r} key in response"
        )
        assert FIELD_RESULT in body or FIELD_ERROR in body, (
            f"{label}: JSON-RPC response has neither {FIELD_RESULT!r} nor {FIELD_ERROR!r}"
        )
    else:
        # 4xx: tool must return a non-empty informative body
        assert resp.text, f"{label}: HTTP {resp.status_code} response has empty body"


# ---------------------------------------------------------------------------
# Bug #1603: scip_callchain must find a REAL, known-adjacent call chain
# through the real MCP front door in server mode (worker-thread dispatch).
#
# Pre-fix, trace_call_chain_v2_batched's signal.signal(SIGALRM, ...) raised
# ValueError when invoked from the worker thread every real server request
# uses, silently swallowed upstream into total_chains_found: 0. This test
# proves the fix end-to-end: real MCP dispatch, real worker thread, real
# sqlite recursive-CTE query, real chain found -- zero mocking.
#
# A DEDICATED fixture is built here (rather than reusing the shared
# tests/scip/fixtures/comprehensive_index.scip.db) because that fixture's
# MathUtils# target is a bare class with zero indexed methods --
# DatabaseBackend._expand_class_to_methods() legitimately returns [] for a
# class with no methods, and trace_call_chain treats that as "no chain
# possible" (a pre-existing, correct guard in backends.py, unrelated to
# Bug #1603). A genuine Method->Method edge sidesteps that guard entirely.
# ---------------------------------------------------------------------------

# Symbol names for the dedicated fixture below. Unique enough to never
# collide with real markupsafe source or the shared comprehensive_index
# fixture (both may also be present under the same golden repo's SCIP dir).
BUG_1603_CALLER_SYMBOL: str = "Bug1603Caller"
BUG_1603_CALLEE_SYMBOL: str = "Bug1603Callee"


def _make_bug_1603_index():
    """Construct a real scip_pb2.Index with one genuine METHOD -> METHOD
    call edge: Bug1603Caller#run() calls Bug1603Callee#assist(). The
    callee's ROLE_READ_ACCESS occurrence sits INSIDE the caller's own
    enclosing_range -- this is what SCIPDatabaseBuilder reads to derive the
    symbol_references "calls" edge (same pattern used throughout
    tests/unit/test_scip_database_queries.py, e.g.
    _create_hybrid_dependencies_test_database).
    """
    from code_indexer.scip.protobuf import scip_pb2
    from code_indexer.scip.database.builder import ROLE_DEFINITION, ROLE_READ_ACCESS

    index = scip_pb2.Index()

    caller = f"scip-python python e2e-1603 abc123 `src.caller`/{BUG_1603_CALLER_SYMBOL}#run()."
    sym = index.external_symbols.add()
    sym.symbol = caller
    sym.kind = scip_pb2.SymbolInformation.Method

    callee = f"scip-python python e2e-1603 abc123 `src.callee`/{BUG_1603_CALLEE_SYMBOL}#assist()."
    sym = index.external_symbols.add()
    sym.symbol = callee
    sym.kind = scip_pb2.SymbolInformation.Method

    doc = index.documents.add()
    doc.relative_path, doc.language = "src/caller.py", "python"
    occ = doc.occurrences.add()
    occ.symbol, occ.symbol_roles = caller, ROLE_DEFINITION
    occ.range.extend([10, 0, 10, 20])
    occ.enclosing_range.extend([10, 0, 20, 0])
    occ = doc.occurrences.add()
    occ.symbol, occ.symbol_roles = callee, ROLE_READ_ACCESS
    occ.range.extend([15, 4, 15, 10])
    occ.enclosing_range.extend([10, 0, 20, 0])

    doc = index.documents.add()
    doc.relative_path, doc.language = "src/callee.py", "python"
    occ = doc.occurrences.add()
    occ.symbol, occ.symbol_roles = callee, ROLE_DEFINITION
    occ.range.extend([5, 0, 5, 20])
    occ.enclosing_range.extend([5, 0, 15, 0])

    return index


def _build_bug_1603_callchain_fixture(build_dir: Path) -> Path:
    """Build a REAL SCIP database via the actual production DatabaseManager
    + SCIPDatabaseBuilder pipeline (no mocking) from _make_bug_1603_index().

    Returns the path to the built .scip.db file.
    """
    from code_indexer.scip.database.schema import DatabaseManager
    from code_indexer.scip.database.builder import SCIPDatabaseBuilder

    index = _make_bug_1603_index()
    scip_file = build_dir / "bug_1603_callchain_fixture.scip"
    with open(scip_file, "wb") as f:
        f.write(index.SerializeToString())

    manager = DatabaseManager(scip_file)
    manager.create_schema()
    SCIPDatabaseBuilder().build(scip_file, manager.db_path)
    return Path(manager.db_path)


def test_scip_callchain_real_index_finds_known_chain(
    seeded_indexed_client: tuple[TestClient, str],
    test_client_data_dir: Path,
    auth_headers: dict,
    tmp_path: Path,
) -> None:
    """scip_callchain finds a real chain for a known-adjacent symbol pair.

    Builds a dedicated real SCIP index (see _build_bug_1603_callchain_fixture)
    with a genuine Bug1603Caller#run() -> Bug1603Callee#assist() edge, copies
    it ALONGSIDE the existing seeded SCIP file(s) in the markupsafe golden
    repo's SCIP directory (SCIPQueryService globs **/*.scip.db, so both
    coexist without interfering with other tests), then calls the real
    scip_callchain MCP tool and asserts total_chains_found > 0 -- the exact
    field Bug #1603 reported as always 0 in server mode prior to the
    thread-watchdog fix (signal.alarm()'s worker-thread ValueError, silently
    swallowed upstream via the MCP-GENERAL-142 warning path).
    """
    import shutil

    client, alias = seeded_indexed_client

    built_db_path = _build_bug_1603_callchain_fixture(tmp_path)
    scip_dest_dir = (
        test_client_data_dir
        / "data"
        / "golden-repos"
        / alias
        / ".code-indexer"
        / "scip"
    )
    scip_dest_dir.mkdir(parents=True, exist_ok=True)
    copied_fixture_path = scip_dest_dir / "bug_1603_callchain_fixture.scip.db"

    try:
        shutil.copy2(built_db_path, copied_fixture_path)

        resp = call_mcp_tool(
            client,
            TOOL_SCIP_CALLCHAIN,
            {
                ARG_KEY_REPOSITORY_ALIAS: alias,
                ARG_KEY_FROM_SYMBOL: BUG_1603_CALLER_SYMBOL,
                ARG_KEY_TO_SYMBOL: BUG_1603_CALLEE_SYMBOL,
            },
            auth_headers,
        )
        assert resp.status_code == HTTP_OK, (
            f"scip_callchain HTTP {resp.status_code}: {resp.text[:MAX_ERROR_SNIPPET]}"
        )
        result = parse_mcp_result(resp.json())
        assert result.get("success") is True, (
            f"scip_callchain did not succeed: {result}"
        )
        assert result.get("total_chains_found", 0) > 0, (
            f"scip_callchain found 0 chains for a known-adjacent pair "
            f"({BUG_1603_CALLER_SYMBOL!r} -> {BUG_1603_CALLEE_SYMBOL!r}) "
            f"-- this is the exact Bug #1603 symptom (signal.alarm() worker-"
            f"thread ValueError swallowed upstream). Full result: {result}"
        )
    finally:
        # Bug #1603 code review Priority 3: seeded_indexed_client is
        # session-scoped, so this backdoor-seeded fixture would otherwise
        # persist for the rest of Phase 3 -- SCIPQueryService.find_scip_files
        # globs **/*.scip.db, so a leftover file here could pollute later
        # tests in this same golden repo.
        copied_fixture_path.unlink(missing_ok=True)
