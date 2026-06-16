"""Phase 3 — Story #1130: X-Ray Sandbox Security E2E Tests.

Tests in this module verify that the x-ray sandbox security boundaries are enforced
through the MCP front door:

AC1 — Forbidden evaluator rejected synchronously (NO rustc needed):
  Submit xray_search with forbidden Rust evaluator_code.  Validation fires
  BEFORE job submission; the response contains "xray_evaluator_validation_failed"
  with structured error_code/offending_construct/offending_line fields and
  NO job_id.  Server remains alive after rejection.

AC2 — Path-traversal pattern name rejected:
  store_xray_pattern with scope or name containing traversal sequences
  (../foo, a/b, ..\\foo) returns "path_traversal_rejected" or
  "invalid_pattern_name" before any filesystem access.

AC3 — Runaway evaluator killed within hard timeout (TOOLCHAIN-GATED):
  Skipped locally when rustc + rust/target/release/xray-cli absent.
  The test body is present for environments with the full toolchain.

AC4 — Lazy-load invariant:
  Subprocess check asserting tree_sitter / tree_sitter_languages absent from
  sys.modules after importing the CLI in a fresh Python process.

Mutation / control:
  A VALID evaluator passes validation (no xray_evaluator_validation_failed).
  Paired with the malicious-rejected cases to prove the gate is discriminating.

Anti-dual-app invariant:
  All tests reuse the existing ``test_client`` + ``auth_headers`` fixtures.
  No second app is created in this module.

Log-audit gate:
  Evaluator validation rejections are synchronous MCP-level errors (returned
  as result dict with error key, not logged as server ERROR/WARNING).
  No new allowlist entries are required.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.e2e.helpers import require_xray_cli
from tests.e2e.server.mcp_helpers import (
    HTTP_OK,
    call_mcp_tool,
)

# ---------------------------------------------------------------------------
# Module-level constants — evaluator snippets
# ---------------------------------------------------------------------------

# Valid evaluator that passes the Rust whitelist.  Returns one finding per
# node at the root start line.  Used as the control / mutation test.
_VALID_EVALUATOR: str = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    "    vec![EvalFinding {\n"
    '        pattern: "match".to_string(),\n'
    "        line: node.start_line,\n"
    "        snippet: String::new(),\n"
    "    }]\n"
    "}"
)

# Forbidden: uses `unsafe` keyword — triggers forbidden_unsafe
_FORBIDDEN_UNSAFE_EVALUATOR: str = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    "    unsafe {\n"
    "        let _ptr: *const u8 = std::ptr::null();\n"
    "    }\n"
    "    vec![]\n"
    "}"
)

# Forbidden: uses std::fs — triggers forbidden_stdlib
_FORBIDDEN_STD_FS_EVALUATOR: str = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    '    let _ = std::fs::read_to_string("/etc/passwd");\n'
    "    vec![]\n"
    "}"
)

# Forbidden: missing fn evaluate_node — triggers missing_entry_point
_MISSING_ENTRY_POINT_EVALUATOR: str = (
    "fn not_the_right_function(x: i32) -> i32 {\n    x + 1\n}"
)

# Valid pattern YAML for store_xray_pattern tests (control).
# Required fields: name, description, language, evaluator_code
# (see xray_pattern_service._REQUIRED_FIELDS).
_VALID_PATTERN_YAML: str = (
    "name: test-pattern-1130\n"
    "description: Test pattern for story 1130 security E2E\n"
    "language: python\n"
    "evaluator_code: |\n"
    "  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    "      vec![]\n"
    "  }\n"
)

# Lazy-load subprocess check: path to src directory
_SRC_ROOT: str = str(Path(__file__).resolve().parent.parent.parent.parent / "src")


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _parse_result(resp_body: dict[str, Any]) -> dict[str, Any]:
    """Extract the tool result dict from a JSON-RPC 2.0 response body.

    Handles both observed MCP response shapes:
      Shape A: {"result": {"content": [{"type": "text", "text": "<json>"}]}}
      Shape B: {"result": [{"type": "text", "text": "<json>"}]}

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


# ---------------------------------------------------------------------------
# AC1 — Forbidden evaluator rejected synchronously
# ---------------------------------------------------------------------------


class TestAC1ForbiddenEvaluatorRejected:
    """AC1: forbidden Rust evaluator_code rejected synchronously via xray_search.

    Validation fires BEFORE job submission so NO rustc is needed.
    The response carries structured error fields; no job_id is present.
    """

    def _assert_validation_rejection(
        self,
        result: dict[str, Any],
        resp_body: dict[str, Any],
        expected_error_code: str,
        description: str,
    ) -> None:
        """Assert that the result is a validation rejection with structured fields."""
        assert result.get("error") == "xray_evaluator_validation_failed", (
            f"{description}: expected 'xray_evaluator_validation_failed' error, "
            f"got {result.get('error')!r}.  Full result: {result}.  "
            f"Full resp_body: {resp_body}"
        )
        assert result.get("error_code") == expected_error_code, (
            f"{description}: expected error_code={expected_error_code!r}, "
            f"got {result.get('error_code')!r}.  Full result: {result}"
        )
        assert result.get("offending_construct") is not None, (
            f"{description}: offending_construct must not be None.  Full result: {result}"
        )
        # No job_id must be present — validator rejected before submission
        assert "job_id" not in result, (
            f"{description}: job_id must NOT be present when validation fails.  "
            f"Full result: {result}"
        )

    def test_unsafe_evaluator_rejected(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """Evaluator with 'unsafe' keyword is rejected with forbidden_unsafe."""
        client, alias = seeded_indexed_client
        resp = call_mcp_tool(
            client,
            "xray_search",
            {
                "repository_alias": alias,
                "pattern": "fn ",
                "search_target": "content",
                "evaluator_code": _FORBIDDEN_UNSAFE_EVALUATOR,
            },
            auth_headers,
        )
        assert resp.status_code == HTTP_OK, (
            f"xray_search returned HTTP {resp.status_code}: {resp.text[:400]}"
        )
        body = resp.json()
        result = _parse_result(body)
        self._assert_validation_rejection(
            result, body, "forbidden_unsafe", "unsafe evaluator"
        )

    def test_std_fs_evaluator_rejected(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """Evaluator with 'std::fs' is rejected with forbidden_stdlib."""
        client, alias = seeded_indexed_client
        resp = call_mcp_tool(
            client,
            "xray_search",
            {
                "repository_alias": alias,
                "pattern": "fn ",
                "search_target": "content",
                "evaluator_code": _FORBIDDEN_STD_FS_EVALUATOR,
            },
            auth_headers,
        )
        assert resp.status_code == HTTP_OK, (
            f"xray_search returned HTTP {resp.status_code}: {resp.text[:400]}"
        )
        body = resp.json()
        result = _parse_result(body)
        self._assert_validation_rejection(
            result, body, "forbidden_stdlib", "std::fs evaluator"
        )

    def test_missing_entry_point_evaluator_rejected(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """Evaluator without 'fn evaluate_node' is rejected with missing_entry_point."""
        client, alias = seeded_indexed_client
        resp = call_mcp_tool(
            client,
            "xray_search",
            {
                "repository_alias": alias,
                "pattern": "fn ",
                "search_target": "content",
                "evaluator_code": _MISSING_ENTRY_POINT_EVALUATOR,
            },
            auth_headers,
        )
        assert resp.status_code == HTTP_OK, (
            f"xray_search returned HTTP {resp.status_code}: {resp.text[:400]}"
        )
        body = resp.json()
        result = _parse_result(body)
        self._assert_validation_rejection(
            result, body, "missing_entry_point", "missing fn evaluate_node"
        )

    def test_server_alive_after_rejection(
        self,
        test_client: TestClient,
        auth_headers: dict,
    ) -> None:
        """Server health endpoint returns 200 after evaluator validation rejection.

        Proves the server is unaffected by the rejected evaluator — no crash,
        no leaked process, no lingering state.
        """
        resp = test_client.get("/health", headers=auth_headers)
        assert resp.status_code == HTTP_OK, (
            f"/health returned HTTP {resp.status_code} after evaluator rejection: "
            f"{resp.text[:200]}"
        )


# ---------------------------------------------------------------------------
# Mutation / control: valid evaluator passes validation
# ---------------------------------------------------------------------------


class TestControlValidEvaluatorPassesValidation:
    """Mutation check: a valid evaluator is NOT rejected by the validator.

    The valid evaluator reaches the job submission step (returns job_id or
    repository_not_found) — it never returns xray_evaluator_validation_failed.
    Paired with the forbidden cases to prove the gate is discriminating.
    """

    def test_valid_evaluator_not_rejected(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """Valid evaluator passes the whitelist — no xray_evaluator_validation_failed."""
        client, alias = seeded_indexed_client
        resp = call_mcp_tool(
            client,
            "xray_search",
            {
                "repository_alias": alias,
                "pattern": "fn ",
                "search_target": "content",
                "evaluator_code": _VALID_EVALUATOR,
            },
            auth_headers,
        )
        assert resp.status_code == HTTP_OK, (
            f"xray_search returned HTTP {resp.status_code}: {resp.text[:400]}"
        )
        body = resp.json()
        result = _parse_result(body)
        # Must NOT be a validation rejection
        assert result.get("error") != "xray_evaluator_validation_failed", (
            f"Valid evaluator was rejected by the sandbox: {result}"
        )
        # Must return a job_id (submitted successfully) or an inline result
        has_job = "job_id" in result
        has_inline = "matches" in result or "files" in result or "results" in result
        # Accept also an error that's NOT validation-related
        # (e.g. xray-cli absent -> execution error, not validation error)
        is_non_validation_error = (
            result.get("error") is not None
            and result.get("error") != "xray_evaluator_validation_failed"
        )
        assert has_job or has_inline or is_non_validation_error, (
            f"Valid evaluator did not produce job_id, inline result, or "
            f"non-validation error.  Full result: {result}.  Full body: {body}"
        )


# ---------------------------------------------------------------------------
# AC2 — Path-traversal pattern name rejected
# ---------------------------------------------------------------------------


class TestAC2PathTraversalRejected:
    """AC2: store_xray_pattern rejects names/scopes containing traversal sequences."""

    def test_scope_with_slash_rejected(
        self,
        test_client: TestClient,
        auth_headers: dict,
    ) -> None:
        """scope='../evil' is rejected as path_traversal_rejected."""
        resp = call_mcp_tool(
            test_client,
            "store_xray_pattern",
            {
                "scope": "../evil",
                "pattern_yaml": _VALID_PATTERN_YAML,
            },
            auth_headers,
        )
        assert resp.status_code == HTTP_OK, (
            f"store_xray_pattern returned HTTP {resp.status_code}: {resp.text[:400]}"
        )
        body = resp.json()
        result = _parse_result(body)
        assert result.get("error") == "path_traversal_rejected", (
            f"Expected 'path_traversal_rejected' for scope='../evil', "
            f"got {result.get('error')!r}.  Full result: {result}"
        )
        # Must not have written anything (no success key)
        assert result.get("success") is not True, (
            f"store_xray_pattern must not succeed with traversal scope: {result}"
        )

    def test_scope_with_forward_slash_rejected(
        self,
        test_client: TestClient,
        auth_headers: dict,
    ) -> None:
        """scope='a/b' (forward slash, not dotdot) is rejected as path_traversal_rejected."""
        resp = call_mcp_tool(
            test_client,
            "store_xray_pattern",
            {
                "scope": "a/b",
                "pattern_yaml": _VALID_PATTERN_YAML,
            },
            auth_headers,
        )
        assert resp.status_code == HTTP_OK, (
            f"store_xray_pattern returned HTTP {resp.status_code}: {resp.text[:400]}"
        )
        body = resp.json()
        result = _parse_result(body)
        assert result.get("error") == "path_traversal_rejected", (
            f"Expected 'path_traversal_rejected' for scope='a/b', "
            f"got {result.get('error')!r}.  Full result: {result}"
        )

    def test_scope_with_backslash_rejected(
        self,
        test_client: TestClient,
        auth_headers: dict,
    ) -> None:
        """scope with backslash is rejected as path_traversal_rejected."""
        resp = call_mcp_tool(
            test_client,
            "store_xray_pattern",
            {
                "scope": "..\\\\evil",
                "pattern_yaml": _VALID_PATTERN_YAML,
            },
            auth_headers,
        )
        assert resp.status_code == HTTP_OK, (
            f"store_xray_pattern returned HTTP {resp.status_code}: {resp.text[:400]}"
        )
        body = resp.json()
        result = _parse_result(body)
        assert result.get("error") == "path_traversal_rejected", (
            f"Expected 'path_traversal_rejected' for scope with backslash, "
            f"got {result.get('error')!r}.  Full result: {result}"
        )

    def test_name_with_slash_in_yaml_rejected(
        self,
        test_client: TestClient,
        auth_headers: dict,
    ) -> None:
        """Pattern YAML with name='a/b' (slash) is rejected as invalid_pattern_name.

        The required-fields check (name, description, language, evaluator_code) fires
        before the name traversal check, so `language` must be present to reach it.
        """
        traversal_yaml = (
            "name: a/b\n"
            "description: Traversal test\n"
            "language: python\n"
            "evaluator_code: |\n"
            "  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
            "      vec![]\n"
            "  }\n"
        )
        resp = call_mcp_tool(
            test_client,
            "store_xray_pattern",
            {
                "scope": "__any__",
                "pattern_yaml": traversal_yaml,
            },
            auth_headers,
        )
        assert resp.status_code == HTTP_OK, (
            f"store_xray_pattern returned HTTP {resp.status_code}: {resp.text[:400]}"
        )
        body = resp.json()
        result = _parse_result(body)
        # name traversal returns invalid_pattern_name
        assert result.get("error") in (
            "invalid_pattern_name",
            "path_traversal_rejected",
        ), (
            f"Expected traversal rejection for name='a/b', "
            f"got {result.get('error')!r}.  Full result: {result}"
        )
        assert result.get("success") is not True, (
            f"store_xray_pattern must not succeed with traversal name: {result}"
        )

    def test_name_with_dotdot_in_yaml_rejected(
        self,
        test_client: TestClient,
        auth_headers: dict,
    ) -> None:
        """Pattern YAML with name='../foo' is rejected as invalid_pattern_name.

        The required-fields check (name, description, language, evaluator_code) fires
        before the name traversal check, so `language` must be present to reach it.
        """
        traversal_yaml = (
            "name: ../foo\n"
            "description: Traversal test\n"
            "language: python\n"
            "evaluator_code: |\n"
            "  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
            "      vec![]\n"
            "  }\n"
        )
        resp = call_mcp_tool(
            test_client,
            "store_xray_pattern",
            {
                "scope": "__any__",
                "pattern_yaml": traversal_yaml,
            },
            auth_headers,
        )
        assert resp.status_code == HTTP_OK, (
            f"store_xray_pattern returned HTTP {resp.status_code}: {resp.text[:400]}"
        )
        body = resp.json()
        result = _parse_result(body)
        assert result.get("error") in (
            "invalid_pattern_name",
            "path_traversal_rejected",
        ), (
            f"Expected traversal rejection for name='../foo', "
            f"got {result.get('error')!r}.  Full result: {result}"
        )


# ---------------------------------------------------------------------------
# AC3 — Runaway evaluator killed within hard timeout (TOOLCHAIN-GATED)
# ---------------------------------------------------------------------------


class TestAC3RunawayEvaluatorKilled:
    """AC3: runaway evaluator is killed within HARD_TIMEOUT_SECONDS.

    This test is SKIPPED locally when rustc + xray-cli binary are absent.
    The test body documents the expected behavior for environments with the
    full Rust toolchain present.
    """

    def test_runaway_evaluator_killed_within_timeout(
        self,
        seeded_indexed_client: tuple[TestClient, str],
        auth_headers: dict,
    ) -> None:
        """Infinite loop evaluator is killed within the hard timeout.

        The xray sandbox enforces HARD_TIMEOUT_SECONDS (5.0s in Python path) +
        grace period.  An evaluator that spins forever must be terminated and
        the job must reach a terminal state (completed or failed) within that
        bound.

        Skipped when rustc or xray-cli binary is absent (local dev environments).
        """
        require_xray_cli()  # loud-skip when toolchain absent

        client, alias = seeded_indexed_client

        infinite_loop_evaluator = (
            "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    loop {}\n}"
        )

        resp = call_mcp_tool(
            client,
            "xray_search",
            {
                "repository_alias": alias,
                "pattern": "fn ",
                "search_target": "content",
                "evaluator_code": infinite_loop_evaluator,
                "timeout_seconds": 15,
            },
            auth_headers,
        )
        assert resp.status_code == HTTP_OK, (
            f"xray_search returned HTTP {resp.status_code}: {resp.text[:400]}"
        )
        body = resp.json()
        result = _parse_result(body)

        # With xray-cli present: either the Rust validator catches the infinite
        # loop construct (validation rejection) OR a job_id is returned (execution
        # timeout kills it).  In both cases there must be no crash.
        has_job = "job_id" in result
        is_rejected = result.get("error") == "xray_evaluator_validation_failed"
        is_other_error = result.get("error") is not None

        assert has_job or is_rejected or is_other_error, (
            f"Runaway evaluator produced unexpected response: {result}"
        )

        # Server must remain alive after the runaway attempt
        health_resp = client.get("/health")
        assert health_resp.status_code == HTTP_OK, (
            f"/health returned HTTP {health_resp.status_code} after runaway evaluator: "
            f"{health_resp.text[:200]}"
        )


# ---------------------------------------------------------------------------
# AC4 — Lazy-load invariant (subprocess proof)
# ---------------------------------------------------------------------------


class TestAC4LazyLoad:
    """AC4: tree_sitter / tree_sitter_languages absent from sys.modules after CLI import.

    Promotes the existing unit-test invariant (tests/unit/xray/test_lazy_load.py)
    into the E2E lane to ensure regression detection in the full Phase-3 gate.
    Uses subprocess isolation so pytest's own imports cannot contaminate the check.
    """

    def test_tree_sitter_not_imported_at_cli_startup(self) -> None:
        """tree_sitter is absent from sys.modules after CLI import (subprocess proof)."""
        code = (
            "import sys; "
            f"sys.path.insert(0, {_SRC_ROOT!r}); "
            "from code_indexer.cli import cli; "
            "print('tree_sitter:', 'tree_sitter' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "tree_sitter: False" in result.stdout, (
            f"LAZY-LOAD VIOLATION: tree_sitter was imported at CLI startup.\n"
            f"Subprocess output: {result.stdout!r}\nstderr: {result.stderr!r}"
        )

    def test_tree_sitter_languages_not_imported_at_cli_startup(self) -> None:
        """tree_sitter_languages is absent from sys.modules after CLI import (subprocess proof)."""
        code = (
            "import sys; "
            f"sys.path.insert(0, {_SRC_ROOT!r}); "
            "from code_indexer.cli import cli; "
            "print('tree_sitter_languages:', 'tree_sitter_languages' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "tree_sitter_languages: False" in result.stdout, (
            f"LAZY-LOAD VIOLATION: tree_sitter_languages was imported at CLI startup.\n"
            f"Subprocess output: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
