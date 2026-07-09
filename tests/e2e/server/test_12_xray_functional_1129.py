"""Phase 3 — Story #1129: X-Ray Functional E2E Tests (MCP front door).

Tests in this module verify that the X-Ray two-phase pipeline (regex walk +
Rust evaluator compilation) works end-to-end through the MCP front door against
the markupsafe golden-repo fixture.

AC1 — Two-phase pipeline returns findings (TOOLCHAIN-GATED):
  xray_search with an INLINE deep-nesting Rust evaluator (threshold 2) against
  markupsafe.  The job reaches "completed" state and result["matches"] is
  non-empty.  UnsupportedLanguage evaluation errors (expected for .rst/.bat/
  Makefile files in the markupsafe snapshot) are tolerated; all other error
  types fail the test.

  Honest-scoping note: the seed "deep-nesting" pattern uses threshold 4, which
  yields 0 matches on this deliberately-small fixture.  This test uses threshold
  2 to produce a non-empty result — the algorithm IS exercised; only the constant
  is calibrated to the fixture size.  setup.py:35 is a confirmed match.

AC2 — AST dump and AST-debug explore (no Rust toolchain required):
  xray_dump_ast on src/markupsafe/__init__.py must return language=="python",
  ast_tree["type"]=="module", and at least one child node.  xray_explore (async)
  with pattern r"def\\s+escape" must return at least one match whose ast_debug
  root has type=="module".

AC3 — Store pattern, reuse by pattern_name, const injection (TOOLCHAIN-GATED):
  store_xray_pattern stores a deep-nesting evaluator with DEPTH_THRESHOLD
  declared in a parameters block (default 2).  xray_search referencing this
  pattern by name must return non-empty matches.  Because the seed pattern at
  threshold 4 finds nothing on this fixture, non-empty results can ONLY come
  from the injected const (threshold 2).

Mutation / negative control (TOOLCHAIN-GATED):
  The same evaluator body stored with DEPTH_THRESHOLD default 999 must return
  exactly zero matches.  Paired with AC3's non-empty result, this proves the
  const injection (not pattern logic) is the discriminating mechanism —
  identical evaluator body, only the declared threshold differs.

Anti-dual-app invariant:
  All tests reuse the existing ``seeded_indexed_client`` + ``auth_headers``
  fixtures.  No second app is created in this module.

Log-audit gate:
  xray_pattern_service: git commit failed entries are already in the log-audit
  allowlist (benign: cidx-meta backup git commit fails on the ephemeral in-
  process non-git data dir; pattern storage/resolution/const-injection/search
  all work correctly).  No new allowlist entries are required.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

from tests.e2e.helpers import require_xray_cli
from tests.e2e.server.mcp_helpers import (
    HTTP_OK,
    call_mcp_tool,
    parse_mcp_result,
)

# ---------------------------------------------------------------------------
# Module-level constants — evaluator body and pattern YAML
# These are shared across AC1, AC3, and mutation to avoid duplication (Messi #4).
# ---------------------------------------------------------------------------

# Python-tuned deep-nesting evaluator body.
# Control-flow kinds tuned to Python grammar node names.
# Used inline (AC1) and in stored patterns (AC3 / mutation).
_DEEP_NESTING_BODY: str = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    "    let mut findings = Vec::new();\n"
    "    walk(node, 0, &mut findings);\n"
    "    findings\n"
    "}\n"
    "fn is_cf(kind: &str) -> bool {\n"
    '    matches!(kind, "if_statement" | "for_statement" | "while_statement"\n'
    '        | "for_in_statement" | "with_statement" | "try_statement"\n'
    '        | "conditional_expression")\n'
    "}\n"
    "fn walk(node: &OwnedNode, depth: usize, f: &mut Vec<EvalFinding>) {\n"
    "    let nd = if is_cf(&node.kind) { depth + 1 } else { depth };\n"
    "    if nd >= DEPTH_THRESHOLD && is_cf(&node.kind) {\n"
    "        f.push(EvalFinding {\n"
    '            pattern: "deep-nesting".to_string(),\n'
    "            line: node.start_line,\n"
    "            snippet: String::new(),\n"
    "        });\n"
    "    }\n"
    "    for c in &node.children { walk(c, nd, f); }\n"
    "}\n"
)

# AC1: inline evaluator prepends a hardcoded threshold const so DEPTH_THRESHOLD
# is resolved at compile time without a parameters block.
_INLINE_EVALUATOR_THRESHOLD_2: str = (
    "const DEPTH_THRESHOLD: usize = 2;\n" + _DEEP_NESTING_BODY
)

# Pattern name used for AC3 (threshold 2 — should produce non-empty matches).
_PATTERN_NAME_THRESHOLD_2: str = "e2e-deep-nesting-t2-1129"

# Pattern name used for the mutation / negative control (threshold 999 — zero matches).
_PATTERN_NAME_THRESHOLD_999: str = "e2e-deep-nesting-t999-1129"

# YAML for AC3 stored pattern (DEPTH_THRESHOLD default 2).
# Required top-level fields: name, description, language, evaluator_code, parameters.
_PATTERN_YAML_THRESHOLD_2: str = (
    f"name: {_PATTERN_NAME_THRESHOLD_2}\n"
    "description: E2E deep-nesting pattern for Story #1129 (threshold 2)\n"
    "language: python\n"
    "evaluator_code: |\n"
    + "".join(f"  {line}\n" for line in _DEEP_NESTING_BODY.splitlines())
    + "parameters:\n"
    "  - name: DEPTH_THRESHOLD\n"
    "    type: usize\n"
    "    default: 2\n"
    '    description: "minimum control-flow nesting depth to flag"\n'
)

# YAML for mutation stored pattern (DEPTH_THRESHOLD default 999 — always zero matches).
_PATTERN_YAML_THRESHOLD_999: str = (
    f"name: {_PATTERN_NAME_THRESHOLD_999}\n"
    "description: E2E deep-nesting pattern for Story #1129 (threshold 999)\n"
    "language: python\n"
    "evaluator_code: |\n"
    + "".join(f"  {line}\n" for line in _DEEP_NESTING_BODY.splitlines())
    + "parameters:\n"
    "  - name: DEPTH_THRESHOLD\n"
    "    type: usize\n"
    "    default: 999\n"
    '    description: "unreachably high threshold — always zero matches"\n'
)

# Regex pattern covering common Python control-flow constructs.
_CF_REGEX: str = r"(if|for|while|with|try)\b"

# AST-dump target file (confirmed present in the markupsafe golden snapshot).
_AST_DUMP_FILE: str = "src/markupsafe/__init__.py"

# Explore regex that reliably matches at least one Python function definition.
_EXPLORE_REGEX: str = r"def\s+escape"

# Job polling constants.  90-second deadline is generous for in-process Rust
# compilation + evaluation on a warm machine.
_JOB_POLL_DEADLINE_SECONDS: float = 90.0
_JOB_POLL_INTERVAL_SECONDS: float = 0.5
_JOB_TERMINAL_STATES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

# Error type that is expected and MUST be tolerated (markupsafe ships non-Python files).
_UNSUPPORTED_LANGUAGE_ERROR: str = "UnsupportedLanguage"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _poll_job(
    client: TestClient, job_id: str, headers: dict[str, str]
) -> dict[str, Any]:
    """Poll GET /api/jobs/{job_id} until a terminal state is reached.

    Uses a monotonic deadline (Messi Rule #14 — bounded loop).  Raises
    TimeoutError if the job does not complete within _JOB_POLL_DEADLINE_SECONDS.

    Args:
        client: Session-scoped TestClient bound to the in-process server.
        job_id: Background job identifier returned by an async MCP tool call.
        headers: Authorization headers dict.

    Returns:
        The complete job status dict from the final poll response.

    Raises:
        TimeoutError: When the deadline is exceeded before a terminal state.
        AssertionError: When any poll response returns a non-2xx HTTP status.
    """
    deadline = time.monotonic() + _JOB_POLL_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}", headers=headers)
        assert resp.status_code == 200, (
            f"Job poll for {job_id!r} returned HTTP {resp.status_code}: "
            f"{resp.text[:300]}"
        )
        body: dict[str, Any] = resp.json()
        if body.get("status") in _JOB_TERMINAL_STATES:
            return body
        time.sleep(_JOB_POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"Job {job_id!r} did not reach a terminal state within "
        f"{_JOB_POLL_DEADLINE_SECONDS}s"
    )


def _resolve_search_result(
    client: TestClient, mcp_result: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    """Return the search result dict from an xray_search / xray_explore response.

    The async X-Ray tools complete INLINE when the work finishes within
    ``await_seconds`` (returning the full result dict directly, no job_id), and
    return a ``job_id`` to poll otherwise.  Handle both shapes:
      - job_id present -> poll GET /api/jobs/{job_id} until completed, return result
      - no job_id      -> the inline response IS the result dict
    """
    job_id = mcp_result.get("job_id")
    if not job_id:
        return mcp_result
    job_body = _poll_job(client, job_id, headers)
    assert job_body.get("status") == "completed", (
        f"xray job {job_id!r} ended with status {job_body.get('status')!r}: {job_body}"
    )
    return job_body.get("result") or {}


def _call_xray_search(
    client: TestClient,
    headers: dict[str, str],
    args: dict[str, Any],
) -> dict[str, Any]:
    """Call xray_search, resolve inline or async result, and return the result dict.

    The async X-Ray tools complete INLINE when the work finishes within
    ``await_seconds`` (returning the full result dict directly, no job_id), and
    return a ``job_id`` to poll otherwise.  Both shapes are handled transparently.

    Args:
        client: Session-scoped TestClient.
        headers: Authorization headers dict.
        args: xray_search argument dict (must include repository_alias, pattern,
              search_target, and one of evaluator_code or pattern_name).

    Returns:
        The result dict (inline or from the completed job body), or empty dict on failure.

    Raises:
        AssertionError: On HTTP-level failures or unexpected response shapes.
    """
    resp = call_mcp_tool(client, "xray_search", args, headers)
    assert resp.status_code == HTTP_OK, (
        f"xray_search returned HTTP {resp.status_code}: {resp.text[:400]}"
    )
    mcp_result = parse_mcp_result(resp.json())
    assert "error" not in mcp_result, (
        f"xray_search returned synchronous error (validation rejected?): {mcp_result}"
    )
    return _resolve_search_result(client, mcp_result, headers)


def _non_unsupported_errors(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return evaluation_errors that are NOT UnsupportedLanguage.

    UnsupportedLanguage errors are expected on the markupsafe snapshot because
    it ships .rst / .bat / Makefile files that have no tree-sitter grammar.
    All other error types indicate a real failure and must be asserted empty.

    Args:
        result: Job result dict containing optional ``evaluation_errors`` list.

    Returns:
        Filtered list of error dicts whose ``error_type`` is not UnsupportedLanguage.
    """
    errors: list[dict[str, Any]] = result.get("evaluation_errors") or []
    return [e for e in errors if e.get("error_type") != _UNSUPPORTED_LANGUAGE_ERROR]


# ---------------------------------------------------------------------------
# AC1 — Two-phase pipeline returns findings (inline evaluator, threshold 2)
# ---------------------------------------------------------------------------


class TestAC1TwoPhasePipelineFindings:
    """AC1: inline deep-nesting evaluator (threshold 2) returns non-empty matches.

    Honest-scoping rationale: the markupsafe golden snapshot is deliberately
    small.  The seed "deep-nesting" pattern (threshold 4) finds zero nesting
    violations on such flat code — that is intentional and correct.  This test
    uses threshold 2 to exercise the full two-phase pipeline (regex walk ->
    Rust compilation -> AST evaluation) and confirm the pipeline produces real
    findings.  The algorithm is genuinely exercised; only the constant is
    calibrated to the fixture size.

    Requires xray-cli binary (Rust evaluator compilation).
    """

    def test_inline_evaluator_returns_findings(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """xray_search with inline threshold-2 evaluator returns at least one match.

        Asserts:
          - HTTP 200 from /mcp.
          - xray_search returns a job_id (await_seconds=0) which is polled, or an inline result.
          - Job reaches "completed" state (or inline result returned directly).
          - result["matches"] is non-empty (>= 1 finding).
          - No evaluation errors of type other than UnsupportedLanguage.
        """
        require_xray_cli()
        client, alias = seeded_indexed_client

        result = _call_xray_search(
            client,
            auth_headers,
            {
                "repository_alias": alias,
                "pattern": _CF_REGEX,
                "search_target": "content",
                "evaluator_code": _INLINE_EVALUATOR_THRESHOLD_2,
                "await_seconds": 0,
            },
        )

        matches: list[dict[str, Any]] = result.get("matches") or []
        non_unsupported = _non_unsupported_errors(result)

        assert len(matches) >= 1, (
            f"AC1: expected at least 1 match with threshold 2 on markupsafe "
            f"but got {len(matches)}.  Full result: {result}"
        )
        assert non_unsupported == [], (
            f"AC1: unexpected (non-UnsupportedLanguage) evaluation errors: "
            f"{non_unsupported}.  Full result: {result}"
        )

    def test_inline_evaluator_match_shape(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """Each match from the inline evaluator has expected shape fields.

        Asserts that every match dict contains at least a 'file_path' key
        and a 'line' key (the two fields emitted by EvalFinding).
        """
        require_xray_cli()
        client, alias = seeded_indexed_client

        result = _call_xray_search(
            client,
            auth_headers,
            {
                "repository_alias": alias,
                "pattern": _CF_REGEX,
                "search_target": "content",
                "evaluator_code": _INLINE_EVALUATOR_THRESHOLD_2,
                "await_seconds": 30,
            },
        )

        matches: list[dict[str, Any]] = result.get("matches") or []
        assert matches, (
            f"AC1 shape: no matches returned — pipeline did not fire.  "
            f"Full result: {result}"
        )
        sample = matches[0]
        assert "file_path" in sample or "path" in sample, (
            f"AC1 shape: first match lacks 'file_path'/'path': {sample}"
        )
        assert "line_number" in sample or "line" in sample, (
            f"AC1 shape: first match lacks 'line_number'/'line': {sample}"
        )


# ---------------------------------------------------------------------------
# AC2 — AST dump and AST-debug explore (no Rust toolchain required)
# ---------------------------------------------------------------------------


class TestAC2AstDumpAndExplore:
    """AC2: xray_dump_ast and xray_explore exercise the tree-sitter AST layer.

    These tools use the Python tree-sitter path (no Rust compilation) so they
    run even when xray-cli is absent.  No require_xray_cli() gate.
    """

    def test_dump_ast_language_and_root(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """xray_dump_ast returns language==python and ast_tree root type==module.

        Asserts:
          - HTTP 200 from /mcp.
          - result["language"] == "python".
          - result["ast_tree"]["type"] == "module".
          - result["ast_tree"]["children"] has at least one element.
        """
        client, alias = seeded_indexed_client

        resp = call_mcp_tool(
            client,
            "xray_dump_ast",
            {
                "repository_alias": alias,
                "file_path": _AST_DUMP_FILE,
            },
            auth_headers,
        )
        assert resp.status_code == HTTP_OK, (
            f"xray_dump_ast returned HTTP {resp.status_code}: {resp.text[:400]}"
        )
        result = parse_mcp_result(resp.json())

        assert result.get("language") == "python", (
            f"AC2 dump_ast: expected language='python', "
            f"got {result.get('language')!r}.  Full result: {result}"
        )
        ast_tree: dict[str, Any] = result.get("ast_tree") or {}
        assert ast_tree.get("type") == "module", (
            f"AC2 dump_ast: expected ast_tree.type='module', "
            f"got {ast_tree.get('type')!r}.  ast_tree keys: {list(ast_tree.keys())}"
        )
        children: list = ast_tree.get("children") or []
        assert len(children) >= 1, (
            f"AC2 dump_ast: expected at least 1 child node in ast_tree, "
            f"got {len(children)}.  ast_tree: {ast_tree}"
        )

    def test_explore_returns_ast_debug(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """xray_explore returns matches with ast_debug root type==module.

        xray_explore completes inline when fast, else returns a job_id to poll.
        Each match in result["matches"] must contain an "ast_debug" dict whose
        root "type" is "module" (the tree root for Python).

        Asserts:
          - HTTP 200 from /mcp.
          - xray_explore returns an inline result or a job_id (polled to completion).
          - result["matches"] is non-empty.
          - matches[0]["ast_debug"]["type"] == "module".
        """
        client, alias = seeded_indexed_client

        resp = call_mcp_tool(
            client,
            "xray_explore",
            {
                "repository_alias": alias,
                "pattern": _EXPLORE_REGEX,
                "search_target": "content",
                "max_debug_nodes": 30,
                "await_seconds": 30,
            },
            auth_headers,
        )
        assert resp.status_code == HTTP_OK, (
            f"xray_explore returned HTTP {resp.status_code}: {resp.text[:400]}"
        )
        mcp_result = parse_mcp_result(resp.json())
        assert "error" not in mcp_result, (
            f"AC2 explore: xray_explore returned an error: {mcp_result}"
        )
        # xray_explore completes inline when fast, else returns a job_id to poll.
        result = _resolve_search_result(client, mcp_result, auth_headers)
        matches: list[dict[str, Any]] = result.get("matches") or []

        assert len(matches) >= 1, (
            f"AC2 explore: expected at least one match for pattern "
            f"{_EXPLORE_REGEX!r} on markupsafe but got {len(matches)}.  "
            f"Full result: {result}"
        )
        first_match = matches[0]
        ast_debug: dict[str, Any] = first_match.get("ast_debug") or {}
        assert ast_debug.get("type") == "module", (
            f"AC2 explore: expected matches[0].ast_debug.type=='module', "
            f"got {ast_debug.get('type')!r}.  first_match: {first_match}"
        )


# ---------------------------------------------------------------------------
# AC3 — Store pattern, reuse by pattern_name, const injection
# ---------------------------------------------------------------------------


class TestAC3StoreReusePatterName:
    """AC3: store_xray_pattern + xray_search by pattern_name + const injection.

    Stores a deep-nesting evaluator with DEPTH_THRESHOLD declared in a
    parameters block (default 2).  xray_search by pattern_name must return
    non-empty matches.

    Proof of const injection: the seed "deep-nesting" pattern at threshold 4
    returns ZERO matches on this fixture.  Non-empty results here can ONLY
    come from the injected const (threshold 2) — the evaluator body is identical.

    Requires xray-cli binary (Rust compilation + const injection).
    """

    def test_store_pattern_succeeds(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """store_xray_pattern returns success==True and a valid path."""
        require_xray_cli()
        client, _alias = seeded_indexed_client

        resp = call_mcp_tool(
            client,
            "store_xray_pattern",
            {
                "scope": "__any__",
                "pattern_yaml": _PATTERN_YAML_THRESHOLD_2,
                "overwrite": True,
            },
            auth_headers,
        )
        assert resp.status_code == HTTP_OK, (
            f"store_xray_pattern returned HTTP {resp.status_code}: {resp.text[:400]}"
        )
        result = parse_mcp_result(resp.json())
        assert result.get("success") is True, (
            f"AC3 store: expected success=True, got: {result}"
        )
        assert "path" in result, (
            f"AC3 store: expected 'path' key in store result, got: {result}"
        )

    def test_search_by_pattern_name_returns_findings(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """xray_search by pattern_name (threshold 2) returns non-empty matches.

        Proves store + reuse-by-name + const injection:
          - Pattern is loaded from cidx-meta by name.
          - DEPTH_THRESHOLD const (default 2) is prepended before compilation.
          - Evaluation finds real nesting violations in markupsafe at depth >= 2.
        """
        require_xray_cli()
        client, alias = seeded_indexed_client

        # Ensure the pattern is stored before searching (idempotent).
        store_resp = call_mcp_tool(
            client,
            "store_xray_pattern",
            {
                "scope": "__any__",
                "pattern_yaml": _PATTERN_YAML_THRESHOLD_2,
                "overwrite": True,
            },
            auth_headers,
        )
        assert store_resp.status_code == HTTP_OK
        store_result = parse_mcp_result(store_resp.json())
        assert store_result.get("success") is True, (
            f"AC3 search: pattern store failed before search: {store_result}"
        )

        result = _call_xray_search(
            client,
            auth_headers,
            {
                "repository_alias": alias,
                "pattern": _CF_REGEX,
                "search_target": "content",
                "pattern_name": _PATTERN_NAME_THRESHOLD_2,
                "await_seconds": 30,
            },
        )

        matches: list[dict[str, Any]] = result.get("matches") or []
        non_unsupported = _non_unsupported_errors(result)

        assert len(matches) >= 1, (
            f"AC3: expected non-empty matches via pattern_name "
            f"{_PATTERN_NAME_THRESHOLD_2!r} (threshold 2) but got "
            f"{len(matches)}.  Full result: {result}"
        )
        assert non_unsupported == [], (
            f"AC3: unexpected (non-UnsupportedLanguage) evaluation errors: "
            f"{non_unsupported}.  Full result: {result}"
        )


# ---------------------------------------------------------------------------
# Mutation / negative control — threshold 999 yields zero matches
# ---------------------------------------------------------------------------


class TestMutationThreshold999:
    """Mutation / negative control: identical evaluator at threshold 999 => zero matches.

    This test is the essential paired companion to AC3:

      - AC3  : DEPTH_THRESHOLD = 2   -> matches non-empty  (const injected)
      - Mutation: DEPTH_THRESHOLD = 999 -> matches == 0     (const injected)

    The evaluator body (_DEEP_NESTING_BODY) is IDENTICAL.  Only the declared
    threshold in the parameters block differs.  Any markupsafe Python function
    nesting 999 levels deep would be physically impossible, so zero matches is
    the only correct result.

    This proves that:
      1. The const injection mechanism is the discriminating factor.
      2. A zero result is NOT a pipeline failure — it is a deliberate,
         deterministic property of the injected constant.
      3. The AC3 non-empty result cannot come from a bug or off-by-one;
         it comes from the threshold being reachable (2 levels of nesting).

    Requires xray-cli binary.
    """

    def test_threshold_999_returns_zero_matches(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """Stored deep-nesting evaluator at threshold 999 returns exactly zero matches.

        Paired with AC3 (threshold 2 → non-empty) to prove const injection is the
        discriminating mechanism: same body, different injected constant, different
        result.
        """
        require_xray_cli()
        client, alias = seeded_indexed_client

        # Store the mutation variant (threshold 999) before searching.
        store_resp = call_mcp_tool(
            client,
            "store_xray_pattern",
            {
                "scope": "__any__",
                "pattern_yaml": _PATTERN_YAML_THRESHOLD_999,
                "overwrite": True,
            },
            auth_headers,
        )
        assert store_resp.status_code == HTTP_OK, (
            f"Mutation store returned HTTP {store_resp.status_code}: "
            f"{store_resp.text[:400]}"
        )
        store_result = parse_mcp_result(store_resp.json())
        assert store_result.get("success") is True, (
            f"Mutation: pattern store (threshold 999) failed: {store_result}"
        )

        result = _call_xray_search(
            client,
            auth_headers,
            {
                "repository_alias": alias,
                "pattern": _CF_REGEX,
                "search_target": "content",
                "pattern_name": _PATTERN_NAME_THRESHOLD_999,
                "await_seconds": 30,
            },
        )

        matches: list[dict[str, Any]] = result.get("matches") or []
        non_unsupported = _non_unsupported_errors(result)

        # Guard: the zero-match result must come from the threshold, NOT from a
        # Phase-1 (regex-walk) failure that processed nothing.  files_processed>=1
        # proves the pipeline ran over real markupsafe files and the const (999)
        # is what suppressed every finding.
        assert result.get("files_processed", 0) >= 1, (
            f"Mutation: Phase-1 processed 0 files — a zero-match result here would "
            f"be a pipeline failure, not a threshold effect.  Full result: {result}"
        )
        assert len(matches) == 0, (
            f"Mutation: expected zero matches with DEPTH_THRESHOLD=999 but got "
            f"{len(matches)}.  This means the const injection did NOT apply the "
            f"threshold correctly.  Full result: {result}"
        )
        assert non_unsupported == [], (
            f"Mutation: unexpected (non-UnsupportedLanguage) evaluation errors: "
            f"{non_unsupported}.  Full result: {result}"
        )
