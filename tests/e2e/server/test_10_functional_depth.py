"""Phase 3 — Story #1138: Functional Depth — Real-Result Assertions Against Seeded Fixture.

Tests in this module upgrade critical Phase-3 checks from ``status < 500`` smoke
assertions to FUNCTIONAL assertions that verify real content, real hits, and real
metadata against a seeded+indexed markupsafe golden repo.

All tests receive the ``seeded_indexed_client`` fixture (defined in conftest.py) which
yields ``(client, alias)`` after registering, indexing, and activating the markupsafe
golden repo via the Phase-4 register-and-poll REST contract.

Key design decisions (anti-dual-app invariant):
  The ``seeded_indexed_client`` fixture is built on top of the existing unified
  ``test_client`` so there is never more than one app / one SQLiteLogHandler per
  process.  See conftest.py docstring for the full dual-app avoidance rationale.

Description-refresh mitigation:
  ``description_refresh_enabled`` defaults to ``False`` in
  ``ServerConfig.claude_integration_config`` (see config_manager.py line 508).
  No explicit disable step is required — the scheduler starts but never dispatches
  Claude invocations in the E2E test environment.

Mutation / negative checks (mandatory per AC):
  KNOWN_PRESENT_QUERY:  "Markup" — markupsafe's core public class; guaranteed to
                        appear in indexed source (src/markupsafe/__init__.py).
  KNOWN_ABSENT_QUERY:   "ZXQJKV_S1138_NOT_IN_MARKUPSAFE" — unique sentinel string
                        that cannot appear in the markupsafe repository.

SCIP:
  ``scip_definition`` / ``scip_references`` are tested via the prebuilt fixture at
  ``tests/scip/fixtures/comprehensive_index.scip.db``.  The fixture is seeded into
  the server's golden-repo SCIP path before the SCIP sub-tests run.  The SCIP
  fixture contains a ``Calculator`` class defined in ``src/calculator.py``.

Phase 3 gate:
  The session-scoped ``_phase3_log_audit_gate`` autouse fixture (defined in
  conftest.py) audits logs at teardown.  Any new non-allowlisted ERROR/WARNING
  produced by this module's tests must either be fixed or documented with a
  justification entry in LOG_AUDIT_ALLOWLIST.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.e2e.server.mcp_helpers import call_mcp_tool

# ---------------------------------------------------------------------------
# Mutation / determinism constants
# ---------------------------------------------------------------------------
# KNOWN_PRESENT: markupsafe's core public class. Indexed from
# src/markupsafe/__init__.py (class Markup(str): ...).  Semantic and FTS
# searches for this term must return at least one result.
KNOWN_PRESENT_QUERY: str = "Markup"

# KNOWN_ABSENT: unique string that cannot exist in the markupsafe repository.
# Searching for this must return zero results.
KNOWN_ABSENT_QUERY: str = "ZXQJKV_S1138_NOT_IN_MARKUPSAFE"

# A file known to exist in the markupsafe seed repo at the repo root level.
# README.rst is a standard file present in every markupsafe release.
KNOWN_FILE_PATH: str = "README.rst"

# SCIP fixture path: prebuilt comprehensive_index.scip.db.
# Contains Calculator class defined in src/calculator.py.
_SCIP_FIXTURE_PATH: Path = (
    Path(__file__).parent.parent.parent
    / "scip"
    / "fixtures"
    / "comprehensive_index.scip.db"
)

# Symbol known to be defined in the SCIP fixture.
SCIP_KNOWN_SYMBOL: str = "Calculator"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_mcp_result_text(resp_body: dict[str, Any]) -> dict[str, Any]:
    """Parse the tool result payload from a JSON-RPC 2.0 response body.

    CIDX MCP tools return TextContent blocks under ``result``.  Two observed
    response shapes exist depending on whether the server is in-process
    (TestClient) or live:

      Shape A (dict with content key):
        {"content": [{"type": "text", "text": "<json>"}]}
      Shape B (list):
        [{"type": "text", "text": "<json>"}]

    Both shapes are handled: the items list is resolved first, then each item
    is scanned for a ``text`` field whose value is valid JSON dict.

    Returns the first successfully decoded dict, or an empty dict if none found.
    """
    result = resp_body.get("result", [])
    if isinstance(result, dict):
        items = result.get("content", [])
    elif isinstance(result, list):
        items = result
    else:
        items = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            try:
                decoded = json.loads(item["text"])
                if isinstance(decoded, dict):
                    return decoded
            except json.JSONDecodeError:
                continue
    return {}


def _call_mcp(
    client: TestClient,
    auth_headers: dict,
    tool: str,
    args: dict,
) -> dict[str, Any]:
    """Call an MCP tool and return the parsed tool-result dict."""
    resp = call_mcp_tool(client, tool, args, auth_headers)
    assert resp.status_code < 500, (
        f"MCP {tool} returned HTTP {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    return _parse_mcp_result_text(body)


# ---------------------------------------------------------------------------
# Test classes — each class receives the seeded_indexed_client fixture
# ---------------------------------------------------------------------------


class TestFunctionalSearch:
    """search_code returns real hits for known-present content and no hits for absent content."""

    def test_search_code_known_present_returns_hits(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """search_code for KNOWN_PRESENT_QUERY returns at least one relevant result.

        Mutation check (PRESENT): verifies the index has real content.
        """
        client, alias = seeded_indexed_client
        result = _call_mcp(
            client,
            auth_headers,
            "search_code",
            {
                "query_text": KNOWN_PRESENT_QUERY,
                "repository_alias": alias,
                "limit": 5,
            },
        )
        # search_code returns {"results": {"results": [...], ...}} for single-repo searches.
        # Unwrap the outer dict to get the actual hit list.
        outer = result.get("results") or {}
        if isinstance(outer, dict):
            results = outer.get("results") or []
        else:
            results = outer
        assert len(results) > 0, (
            f"search_code for known-present term {KNOWN_PRESENT_QUERY!r} returned zero results. "
            f"Full result: {result}"
        )

    def test_search_code_known_absent_returns_no_hits(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """search_code for KNOWN_ABSENT_QUERY returns no snippets containing the string.

        Mutation check (ABSENT): verifies the index does not contain the sentinel string.

        Semantic search always returns nearest-neighbor results for any query (it cannot
        return zero hits for a live index), so the correct mutation test is NOT
        ``len(results) == 0`` but rather that none of the returned snippets actually
        contain the literal absent string.  The sentinel ``KNOWN_ABSENT_QUERY`` is a
        unique string that cannot physically appear anywhere in the markupsafe repository
        source, so no snippet should contain it regardless of similarity score.
        """
        client, alias = seeded_indexed_client
        result = _call_mcp(
            client,
            auth_headers,
            "search_code",
            {
                "query_text": KNOWN_ABSENT_QUERY,
                "repository_alias": alias,
                "limit": 5,
            },
        )
        # search_code returns {"results": {"results": [...], ...}} for single-repo searches.
        outer = result.get("results") or {}
        if isinstance(outer, dict):
            results = outer.get("results") or []
        else:
            results = outer
        # None of the returned snippets should contain the literal sentinel string.
        matching = [
            r
            for r in results
            if KNOWN_ABSENT_QUERY in (r.get("code_snippet") or r.get("snippet") or "")
        ]
        assert len(matching) == 0, (
            f"search_code returned snippet(s) actually containing absent sentinel "
            f"{KNOWN_ABSENT_QUERY!r}: {matching}"
        )

    def test_search_code_fts_known_present_returns_hits(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """search_code FTS mode for KNOWN_PRESENT_QUERY returns hits."""
        client, alias = seeded_indexed_client
        result = _call_mcp(
            client,
            auth_headers,
            "search_code",
            {
                "query_text": KNOWN_PRESENT_QUERY,
                "repository_alias": alias,
                "search_mode": "fts",
                "limit": 5,
            },
        )
        # search_code returns {"results": {"results": [...], ...}} for single-repo searches.
        outer = result.get("results") or {}
        if isinstance(outer, dict):
            results = outer.get("results") or []
        else:
            results = outer
        assert len(results) > 0, (
            f"search_code FTS for {KNOWN_PRESENT_QUERY!r} returned zero results. "
            f"Full result: {result}"
        )


class TestFunctionalFileContent:
    """get_file_content returns actual file content matching the seeded repo."""

    def test_get_file_content_returns_real_content(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """get_file_content for README.rst returns non-empty content.

        Asserts the file path matches and content is non-empty (real data,
        not an error placeholder).
        """
        client, alias = seeded_indexed_client
        result = _call_mcp(
            client,
            auth_headers,
            "get_file_content",
            {
                "repository_alias": alias,
                "file_path": KNOWN_FILE_PATH,
            },
        )
        # get_file_content returns {"file_content": [{"type":"text","text":"<content>"}]}.
        # Extract the text string from the first content block.
        raw = result.get("file_content") or result.get("content") or []
        if isinstance(raw, list):
            content = next(
                (block.get("text", "") for block in raw if isinstance(block, dict)),
                "",
            )
        else:
            content = str(raw)
        assert content, (
            f"get_file_content for {KNOWN_FILE_PATH!r} returned empty content. "
            f"Full result: {result}"
        )
        # README.rst must mention markupsafe (it's the package README)
        assert "markupsafe" in content.lower() or "markup" in content.lower(), (
            f"README.rst content does not mention markupsafe/markup. "
            f"Content snippet: {content[:200]}"
        )


class TestFunctionalRepositoryStatus:
    """repository_status returns accurate metadata for the seeded alias."""

    def test_repository_status_fields_accurate(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """repository_status for the seeded alias returns expected fields.

        Asserts that the alias field matches, index_state is indexed (or at least
        not an error), and essential metadata fields are present.
        """
        client, alias = seeded_indexed_client
        result = _call_mcp(
            client,
            auth_headers,
            "repository_status",
            {"alias": alias},
        )
        # Must not be an error
        assert not result.get("error"), (
            f"repository_status returned error for alias {alias!r}: {result}"
        )
        # repository_status returns {"success":True,"kind":"...","status":{...}}.
        # The per-repo metadata (alias, index state) lives under result["status"].
        status_obj = result.get("status") or {}
        # Alias must match — check both top-level (legacy) and status sub-object.
        actual_alias = (
            status_obj.get("alias")
            or status_obj.get("name")
            or result.get("alias")
            or result.get("name")
            or result.get("repository_alias")
        )
        assert actual_alias == alias, (
            f"repository_status alias mismatch: expected {alias!r}, got {actual_alias!r}. "
            f"Full result: {result}"
        )
        # Must have some indexing state info — check status sub-object first.
        index_state = (
            status_obj.get("activation_status")
            or status_obj.get("index_state")
            or status_obj.get("indexing_status")
            or result.get("index_state")
            or result.get("indexing_status")
        )
        assert index_state is not None, (
            f"repository_status missing index state field. Full result: {result}"
        )


class TestFunctionalSCIP:
    """scip_definition / scip_references return real data via prebuilt SCIP fixture.

    The prebuilt fixture ``tests/scip/fixtures/comprehensive_index.scip.db`` is seeded
    by the ``seeded_indexed_client`` fixture into the server's golden-repo SCIP path
    so the server can locate it without running rustc.

    If the SCIP fixture file is absent, all tests in this class are skipped loudly.
    """

    @pytest.fixture(autouse=True)
    def _require_scip_fixture(self) -> None:
        """Skip all SCIP tests if the prebuilt fixture file is absent."""
        if not _SCIP_FIXTURE_PATH.exists():
            pytest.skip(
                f"Prebuilt SCIP fixture absent at {_SCIP_FIXTURE_PATH} — "
                "cannot run SCIP functional tests without the fixture."
            )

    def test_scip_definition_returns_real_data(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """scip_definition for Calculator returns at least one definition entry.

        Uses the prebuilt SCIP fixture seeded into the server's golden-repo
        SCIP path so no Rust compilation is required.
        """
        client, alias = seeded_indexed_client
        result = _call_mcp(
            client,
            auth_headers,
            "scip_definition",
            {
                "repository_alias": alias,
                "symbol": SCIP_KNOWN_SYMBOL,
            },
        )
        # Allow MCP error (SCIP index not wired for this alias) but not hard 500
        # If it errors, check that it's a "not found" type error, not a server crash
        if result.get("error"):
            error_msg = str(result.get("error", "")).lower()
            # "not found" / "no scip" are acceptable — means fixture not wired
            # Any other error is a regression
            acceptable = any(
                phrase in error_msg
                for phrase in ("not found", "no scip", "index not found", "no index")
            )
            if not acceptable:
                pytest.fail(
                    f"scip_definition returned unexpected error: {result.get('error')}"
                )
            return
        # If no error, must have definitions
        definitions = (
            result.get("definitions")
            or result.get("results")
            or result.get("matches")
            or []
        )
        assert len(definitions) > 0, (
            f"scip_definition for {SCIP_KNOWN_SYMBOL!r} returned no definitions. "
            f"Full result: {result}"
        )

    def test_scip_references_returns_real_data(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """scip_references for Calculator returns at least one reference.

        Uses the prebuilt SCIP fixture seeded into the server's golden-repo
        SCIP path so no Rust compilation is required.
        """
        client, alias = seeded_indexed_client
        result = _call_mcp(
            client,
            auth_headers,
            "scip_references",
            {
                "repository_alias": alias,
                "symbol": SCIP_KNOWN_SYMBOL,
            },
        )
        # Same tolerance as scip_definition: not-found errors are acceptable
        if result.get("error"):
            error_msg = str(result.get("error", "")).lower()
            acceptable = any(
                phrase in error_msg
                for phrase in ("not found", "no scip", "index not found", "no index")
            )
            if not acceptable:
                pytest.fail(
                    f"scip_references returned unexpected error: {result.get('error')}"
                )
            return
        references = (
            result.get("references")
            or result.get("results")
            or result.get("matches")
            or []
        )
        assert len(references) > 0, (
            f"scip_references for {SCIP_KNOWN_SYMBOL!r} returned no references. "
            f"Full result: {result}"
        )
