"""Post-E2E log-audit gate for Story #1122.

Implements the automated post-E2E log-audit gate as a shared module consumed
by phase-specific conftest fixtures.  The gate queries the server's operational
log store through the FRONT-DOOR admin_logs_query MCP tool and fails any phase
that produces new non-allowlisted ERROR or WARNING entries.

Key design decisions (per Codex review corrections in story #1122):
- Operational log store is ALWAYS SQLite logs.db, regardless of storage_mode.
- flush() barrier applies ONLY in Phase 3 in-process TestClient.
- For live phases (4/5): poll until count stabilises (bounded) instead of flush.
- Use watermark (log id at phase-start) to diff, not empty-baseline assertion.
- elevation_enforcement_enabled=False (default) means admin_logs_query passes
  through require_mcp_elevation without a TOTP challenge.

Public API
----------
LOG_AUDIT_ALLOWLIST       : list[str]         -- known-benign patterns
is_allowlisted            : (entry) -> bool   -- per-entry allowlist check
filter_new_entries        : (entries, wm) -> list  -- watermark diff
poll_until_stable_count   : (fn, max, sleep) -> int -- bounded poll
build_audit_failure_message : (entries, phase) -> str  -- human-readable report
AuditGateResult           : dataclass          -- gate outcome
query_logs_via_mcp        : (client, token) -> list[dict]  -- front-door call
get_log_watermark         : (client, token) -> int         -- watermark snapshot
run_log_audit_gate        : (client, token, wm, phase) -> AuditGateResult
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, List, cast

# ---------------------------------------------------------------------------
# Allowlist: known-benign WARNING/ERROR patterns (minimal, explicitly reviewed)
# ---------------------------------------------------------------------------
# Justification for each entry:
#
# "http_client_factory not available on app.state"
#   Emitted by reranker/voyage init code when the pooled HTTP client factory
#   is absent -- expected in TestClient (in-process) environments where
#   lifespan wiring is bypassed by TestClient's own startup sequence.
#   Harmless: reranker falls back to default transport.
#
# "CIDX_TEST_FAST_SQLITE"
#   Emitted during test environments using the CIDX_TEST_FAST_SQLITE flag,
#   which changes SQLite journal mode for speed. Benign in test contexts.
#
# "could not read config; treating as disabled"
#   Emitted by _is_elevation_enforcement_enabled() when config service is
#   not yet initialised (e.g. during early startup). Benign transient warning.
#
# "No description found"
#   Emitted by description refresh scheduler for repos with no existing
#   description. Expected on fresh-data-dir E2E runs.
#
# "No module named 'tree_sitter_languages'"
#   Emitted in environments where the optional HCL grammar is absent.
#   Expected in E2E environments without the HCL extension installed.
#
# "LOG_AUDIT_GATE_MUTATION_TEST_1122"
#   Emitted by the mandatory mutation test in test_log_audit_gate_e2e.py
#   (TestMandatoryMutationCheck.test_gate_fails_when_server_warning_emitted).
#   The mutation test proves end-to-end detection via filter_new_entries (watermark
#   diffing captures the entry) and query_logs_via_mcp (MCP front door delivers it).
#   The session-scoped autouse gate (_phase3_log_audit_gate) also sees this entry
#   because its watermark predates the mutation test; allowlisting it prevents a
#   false double-fail at session teardown.
#   Safe: detection capability is fully proven by the mutation test's own assertions
#   (steps 5 and 6 in that test), not by the autouse gate.
#
# --- Phase 3 negative-test / edge-path allowlist entries (Story #1122 curation) ---
#
# "Unexpected error in browse_directory: 'repository_alias'"
# "Unexpected error in list_files: 'repository_alias'"
# "Unexpected error in get_file_content: 'repository_alias'"
#   Produced by test_04_mcp_files_ssh_guides.py which calls list_files with an
#   empty params dict {} to verify the tool is registered and responds.  The
#   handlers do params["repository_alias"] (required key lookup) which raises
#   KeyError; the except-Exception block logs ERROR and returns a clean error
#   response.  The test explicitly accepts any rc < 500.
#   Pattern covers only the specific KeyError message; a different unhandled error
#   in these handlers (e.g. "Unexpected error in list_files: NoneType ...") would
#   NOT be suppressed.
#
# "Unexpected error in"
#   (Covers the above three variants under one pattern; the next bullet explains
#    why this is safe and targeted enough.)
#   NOTE: This pattern is intentionally scoped to errors that contain
#   "Unexpected error in" followed by the specific "repository_alias" substring,
#   but since is_allowlisted does substring matching per entry, we use two separate
#   narrower patterns rather than one broad one -- see below.
#
# "[REPO-MIGRATE-004] Failed to get branches for repository"
#   Emitted by repository_listing_manager when get_branches is called on cidx-meta
#   (not a real git workspace) in test_02_mcp_repos.py.  The log code REPO-MIGRATE-004
#   is the stable identifier; a regression on a different code path would use a
#   different log code.
#
# "[CACHE-GENERAL-016] Cannot trigger migration: metadata file not found"
#   Emitted by git_operations_service when git operations on cidx-meta or
#   cidx-meta-global are attempted and the migration metadata file is absent
#   in the temp test data directory.  Produced by test_05_mcp_git.py git_pull /
#   git_fetch calls.  Log code CACHE-GENERAL-016 is the stable identifier; a
#   different missing-metadata failure would have its own distinct code.
#
# "git_pull file not found"
#   Emitted by git_write.py's FileNotFoundError handler when git_pull is called
#   on cidx-meta which does not exist as an activated-repo directory in the
#   ephemeral test data dir.  Produced by test_05_mcp_git.py.  The stable prefix
#   "git_pull file not found" identifies the specific operation; a FileNotFoundError
#   in a different git operation (e.g. "git_push file not found") would still be
#   flagged by the gate.
#
# "[MCP-GENERAL-120] Function repository 'claude-delegation-functions' not found"
#   Emitted by the delegation handler when list_delegation_functions is called
#   and the claude-delegation-functions golden repo is absent from the test server.
#   Produced by test_06_mcp_admin.py.  Log code MCP-GENERAL-120 is the stable
#   identifier; a delegation failure from a registered function would surface
#   under a different message / code.
#
# "[REPO-GENERAL-017] ValidationError"
#   Emitted by the error_handler middleware for every FastAPI RequestValidationError
#   (HTTP 422).  Produced by:
#     - test_07_rest_api.py: POST /auth/refresh with no body, GET /api/repos/discover
#       without required `source` query param, POST /api/admin/golden-repos with no body.
#     - test_08_auth_negative.py: test_login_empty_credentials sending {"username":"","password":""}.
#   All four are deliberate negative tests verifying the server returns 422 for
#   invalid input.  The log code REPO-GENERAL-017 is the stable identifier for
#   request-validation failures; a 500 or other unexpected server error would
#   use a different message/code and would not be suppressed.
#
# "Global repo 'test-global' not found"
#   Emitted by mcp.handlers.repos when repository_status is called with alias
#   "test-global" which is not registered in the test server's golden-repo
#   metadata.  Produced by test_epic985_regression.py (TestS990RepositoryStatus:
#   test_repository_status_invalid_detail and test_repository_status_global_basic).
#   These are intentional negative tests verifying clean error handling for
#   non-existent global repos.  A genuine regression on an existing repo would
#   use that repo's real alias, not "test-global".
#
# "handle_repository_status failed: Repository 'test-activated' not found"
#   Emitted by mcp.handlers.repos when repository_status is called with alias
#   "test-activated" which is not an activated repo in the test server.
#   Produced by test_epic985_regression.py (TestS990RepositoryStatus:
#   test_repository_status_activated_basic).  The stable "test-activated" alias
#   is a sentinel used only by this test; a real activation failure on a
#   user-registered repo would use that repo's real alias, not "test-activated".

LOG_AUDIT_ALLOWLIST: List[str] = [
    # Infrastructure / environment entries (pre-existing)
    "http_client_factory not available on app.state",
    "CIDX_TEST_FAST_SQLITE",
    "could not read config; treating as disabled",
    "No description found",
    "No module named 'tree_sitter_languages'",
    "LOG_AUDIT_GATE_MUTATION_TEST_1122",
    # Phase 3 negative-test / edge-path entries (Story #1122 curation)
    # test_04: list_files / browse_directory / get_file_content called without
    # repository_alias param — KeyError caught by except-Exception handler.
    # The "': 'repository_alias'" suffix makes this specific to the KeyError
    # message shape; a different unhandled exception would not match.
    "Unexpected error in browse_directory: 'repository_alias'",
    "Unexpected error in list_files: 'repository_alias'",
    "Unexpected error in get_file_content: 'repository_alias'",
    # test_02: get_branches on cidx-meta (not a real git repo) triggers
    # migration check that logs REPO-MIGRATE-004.
    "[REPO-MIGRATE-004] Failed to get branches for repository",
    # test_05: git operations on cidx-meta trigger migration check;
    # metadata file absent in ephemeral temp test data directory.
    "[CACHE-GENERAL-016] Cannot trigger migration: metadata file not found",
    # test_05: git_pull on cidx-meta raises FileNotFoundError (no activated-repo dir).
    "git_pull file not found",
    # test_06: list_delegation_functions when claude-delegation-functions repo absent.
    "[MCP-GENERAL-120] Function repository 'claude-delegation-functions' not found",
    # test_07 + test_08: deliberate negative tests send invalid/empty request bodies;
    # server returns 422 and logs REPO-GENERAL-017 ValidationError.
    "[REPO-GENERAL-017] ValidationError",
    # test_epic985: repository_status called with non-existent "test-global" sentinel alias.
    "Global repo 'test-global' not found",
    # test_epic985: repository_status called with non-existent "test-activated" sentinel alias.
    "handle_repository_status failed: Repository 'test-activated' not found",
    # Story #1138 (seeded_indexed_client fixture): committer_resolution_service emits
    # [SVC-MIGRATE-001] when a local filesystem path is used as repo_url (no hostname
    # to extract for git author email generation).  Benign: only affects commit author
    # metadata, not indexing or search correctness.
    "[SVC-MIGRATE-001] Cannot extract hostname from URL",
    # Story #1138 (seeded_indexed_client fixture): config.py emits "codebase_dir mismatch"
    # (Bug #1033) when the activated-repo path differs from the stored codebase_dir in
    # the golden-repo config.  This is the documented per-node NFS multi-mount
    # reconciliation warning; it is logged once per config path and is benign —
    # the actual path is used and search results are correct.
    "codebase_dir mismatch",
    # Story #1127 (test_per_lane_429_isolation_1127.py::test_ac1_voyage_429_isolates_lane_cohere_stays_clean):
    # The AimdController emits this structured WARNING when K halves on a real 429 from the
    # fault-injected Voyage lane.  This is the ASSERTED SIGNAL for AC1 — the test explicitly
    # queries the log store to prove the AIMD path fired (old_k/new_k fields confirm halving).
    # Safe: detection is proven by the test's own assertion (Step 7); the log gate does not
    # double-assert it.  Only appears during Phase 5 fault-injection runs with 429 error_codes.
    "AIMD multiplicative decrease",
    # Story #1129 (test_12_xray_functional_1129.py): store_xray_pattern and the
    # seed-pattern bootstrap (ensure_seed_patterns) call XrayPatternService._git_commit,
    # which runs `git add` / `git commit` inside cidx-meta.  In the in-process Phase-3
    # harness cidx-meta lives in an ephemeral data dir that is NOT a git-backed workspace,
    # so the commit fails with exit status 128.  _git_commit catches the CalledProcessError,
    # logs this WARNING (deferred-failure pattern), and RETURNS — the pattern YAML is still
    # written to disk and the tool returns {"success": true}.  Benign: only the cidx-meta
    # backup-git side effect fails; pattern storage, resolution, const injection, and search
    # are all correct (proven by the AC1/AC3/mutation assertions in test_12).  The exact
    # prefix "xray_pattern_service: git commit failed" is specific to this backup path; a
    # genuine pattern-storage error surfaces as a distinct error_code in the tool response,
    # not this WARNING.  Mirrors the existing cidx-meta-in-test entries above
    # ([REPO-MIGRATE-004], [CACHE-GENERAL-016], "git_pull file not found").
    "xray_pattern_service: git commit failed",
    # Story #1133 (test_13_depmap_coordination_1133.py): the AC1 single-winner /
    # acceptance assertions fire a REAL front-door dep-map trigger that returns 202.
    # The accepted dep-map worker (run_full_analysis) early-returns "skipped" with no
    # activated repos but still schedules a cidx-meta-global refresh, which in the
    # Phase-3 in-process harness has NO indexable file content (cidx-meta-global is an
    # ephemeral, empty data-dir clone -- not a real populated golden repo).  The refresh
    # subprocess therefore fails deterministically with "No files found to index", and
    # the refresh_scheduler logs two distinct ERROR lines, both ANCHORED on the
    # "cidx-meta-global" alias:
    #   1. "semantic indexing on source failed for cidx-meta-global: ..."
    #   2. "Refresh failed for cidx-meta-global: ..." -- this exact suffix is ALSO carried
    #      by the BackgroundJobManager wrapper line "Background job <id> failed: Refresh
    #      failed for cidx-meta-global: ...", so substring-matching covers that line too
    #      WITHOUT needing a broad "Background job" pattern.
    # Benign: this is the unavoidable zero-data refresh artifact of running a dep-map
    # analysis worker without real golden-repo content; the SENTINEL coordination under
    # test (409 guard, single-winner 202) is correct and independent of the refresh
    # outcome.  Both substrings are anchored on the "cidx-meta-global" alias, so a refresh
    # failure for any OTHER repo (under its own real alias) would NOT be suppressed.
    "semantic indexing on source failed for cidx-meta-global",
    "Refresh failed for cidx-meta-global",
    # Story #1133 (test_13_depmap_coordination_1133.py) AC2: the MALFORMED_YAML
    # assertion seeds a domain file with deliberately unclosed YAML frontmatter so
    # that parse_domain_file_for_graph raises and the read tool surfaces a
    # MALFORMED_YAML anomaly on the parser channel.  The parser ALSO logs a WARNING
    # ("parse_domain_file_for_graph: failed to read/parse .../<file>") when it records
    # that anomaly.  This WARNING IS the asserted AC2 signal.  The seed file is named
    # with the test-unique token "broken_1133_malformed.md" precisely so this allowlist
    # entry is anchored on that exact filename: a parse failure on ANY real domain file
    # (under its own name) would NOT contain this token and would NOT be suppressed.
    "broken_1133_malformed.md",
    # Story #1133 (test_13_depmap_coordination_1133.py) AC1: the release-then-accept
    # and concurrent-single-winner tests each fire a REAL accepted (202) dep-map
    # trigger, and each accepted worker schedules a cidx-meta-global refresh.  When
    # two such refreshes overlap (back-to-back accepted triggers across tests), the
    # dep-map worker's run_full handler catches a JobTracker duplicate-job condition
    # and logs "Background full analysis failed: A 'global_repo_refresh' job is already
    # running for repository 'cidx-meta-global' ...".  Benign: the same zero-data
    # cidx-meta-global refresh coordination artifact as the two entries above, just the
    # duplicate-refresh variant; the dep-map SENTINEL guard under test is unaffected.
    # Anchored on the "cidx-meta-global" repository name, so a genuine duplicate-refresh
    # collision on a REAL repo (its own alias) would NOT be suppressed.
    "already running for repository 'cidx-meta-global'",
    # Story #1135 (test_15_gitwrite_globalalias_1135.py) AC1: the git-write round-trip
    # fires a REAL git_commit on the admin's activated markupsafe workspace.  git_commit's
    # _resolve_commit_identity (git_write.py:193-220) looks up a PAT credential for the
    # remote to set the committer identity; in the E2E environment no PAT is configured,
    # so it logs this EXPLICITLY non-blocking WARNING and falls back to the default
    # author/committer identity (git_write.py:199-200).  Benign: the commit still
    # succeeds and AC1 asserts the committed content round-trips byte-exact via
    # git_file_at_revision.  The anchor is specific to this fallback-identity message;
    # a genuine credential or auth failure surfaces with a different error shape.
    "PAT credential lookup returned error (non-blocking, using fallback identity)",
    # Story #1139 (test_16_remaining_surface_1139.py) AC3: the omni wildcard cap-mutation
    # test DELIBERATELY lowers the omni caps below the global-repo count and fires the
    # bare-'*' MCP search, asserting the fan-out is REFUSED (capped, not unbounded).  The
    # cap-enforcement code paths log these two WARNINGs as the SERVER-SIDE proof the cap
    # fired -- they ARE the asserted AC3 signal (the test asserts the matching cap-breach
    # tool response: wildcard_cap_exceeded / repo_count_cap_exceeded).  The caps are
    # snapshotted and RESTORED in teardown.  Anchored on the exact MCP error codes
    # [MCP-GENERAL-032] / [MCP-GENERAL-033]: a cap breach in production carries the same
    # codes, but only this test lowers the caps to provoke it in the E2E harness.
    "[MCP-GENERAL-032] Wildcard expansion cap exceeded",
    "[MCP-GENERAL-033] Total repo count cap exceeded",
    # Story #1139 (test_16_remaining_surface_1139.py) teardown: the test creates a
    # composite + extra golden/activated repos and DEACTIVATES them in cleanup.
    # ActivatedRepoManager logs this administrative WARNING on every deactivation
    # (activated_repo_manager.py:2092 -- explicitly "Administrative logging before
    # cleanup", not an error).  Benign operational noise from normal repo teardown.
    # Anchored on the exact static message: a real deactivation FAILURE surfaces with
    # a different (error) message and is NOT suppressed.
    "Repository deactivation initiated",
    # Phase 3 registration diagnostic probe (golden_repo_manager._execute_post_clone_workflow):
    # After cidx init and before cidx index --fts, two WARNING-level probes check whether
    # .code-indexer/config.json is present.  These probes were raised from DEBUG to WARNING
    # so they appear in the Phase-3 log store for post-mortem diagnosis of the config-init
    # race (Bug: non-atomic _write_embedding_providers_to_config left config.json truncated).
    # The [config-init-diag] prefix uniquely anchors these entries; genuine config failures
    # surface with different error codes (e.g. [SVC-MIGRATE-003]).
    "[config-init-diag]",
    # dependency_map_analyzer._reconcile_domains_json: a domain that exists in the
    # graph JSON but has no corresponding .md file in cidx-meta is a "ghost" and is
    # pruned from the graph on the next reconciliation pass.  This is normal housekeeping
    # in a test environment where dep-map state may be partially populated; it is not
    # an indexing or search failure.  Anchored on the static message prefix; a genuine
    # missing-domain failure would surface through a different code path and message.
    "_reconcile_domains_json: ghost domain",
    # SharedJobSentinel.release called for an op_type whose sentinel was never claimed
    # (or was already released).  This is idempotent teardown — releasing a non-existent
    # sentinel is a no-op and is explicitly designed to be harmless.  Appears during
    # Phase-3 test teardown when dep-map coordination tests clean up after themselves.
    # Anchored on the static message prefix; a sentinel claim failure or corruption
    # surfaces with a different message shape.
    "SharedJobSentinel.release: no sentinel for op_type=",
    # meta_description_hook.on_repo_removed: when the cidx-meta coarse write lock is
    # held by another operation during repo removal, the description-.md deletion is
    # gracefully skipped and the orphan is reaped on the next pass.  This is the
    # documented non-fatal lock-contention path (the write lock guard intentionally
    # returns False rather than blocking).  Appears during Phase-3 test teardown when
    # concurrent cidx-meta operations overlap.  Anchored on the static message prefix;
    # a genuine write-failure would surface with a different error shape.
    "on_repo_removed: write lock not acquired, skipping deletion of",
]


def is_allowlisted(entry: dict[str, Any]) -> bool:
    """Return True if the log entry's message matches any allowlist pattern.

    Matching is case-insensitive substring search so minor message variations
    do not require new allowlist entries.

    Args:
        entry: Log entry dict from admin_logs_query response (must have 'message' key).

    Returns:
        True if the entry is known-benign (allowlisted), False otherwise.
    """
    message = (entry.get("message") or "").lower()
    return any(pattern.lower() in message for pattern in LOG_AUDIT_ALLOWLIST)


def filter_new_entries(
    entries: list[dict[str, Any]], watermark_id: int
) -> list[dict[str, Any]]:
    """Return entries with id > watermark_id (i.e. newer than the watermark).

    The watermark is the maximum log id recorded at phase-start (after the
    wiped-slate server boot).  Entries at or below the watermark were already
    present before the phase ran and are excluded from the audit.

    Args:
        entries: List of log entry dicts from admin_logs_query.
        watermark_id: Maximum log id seen at phase-start. 0 means no prior logs.

    Returns:
        Subset of entries where entry['id'] > watermark_id.
    """
    return [e for e in entries if (e.get("id") or 0) > watermark_id]


def poll_until_stable_count(
    count_fn: Callable[[], int],
    max_attempts: int = 10,
    sleep_seconds: float = 0.3,
) -> int:
    """Poll count_fn until two consecutive calls return the same value.

    Used in live-server phases (4/5) where there is no in-process flush()
    handle.  Two identical consecutive counts indicate the async writer has
    drained (Bug #1078 mitigation).

    Args:
        count_fn: Callable that returns the current log entry count.
        max_attempts: Hard ceiling on the number of count_fn calls. Must be >= 1.
        sleep_seconds: Seconds to sleep between polls (0.0 in unit tests).

    Returns:
        The stable (or last-seen) count.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    prev: int | None = None
    last: int = 0
    for _ in range(max_attempts):
        current = count_fn()
        last = current
        if prev is not None and current == prev:
            return current
        prev = current
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return last


def build_audit_failure_message(
    violations: list[dict[str, Any]], phase_name: str
) -> str:
    """Build a human-readable failure message listing all non-allowlisted entries.

    Args:
        violations: List of non-allowlisted log entry dicts.
        phase_name: Human-readable phase name (e.g. "Phase 3 (Server In-Process)").

    Returns:
        Multi-line string describing all violations; empty if violations is empty.
    """
    if not violations:
        return ""
    count = len(violations)
    lines = [
        f"POST-E2E LOG AUDIT FAILED: {phase_name}",
        f"  {count} non-allowlisted ERROR/WARNING entr{'y' if count == 1 else 'ies'} found:",
        "",
    ]
    for i, entry in enumerate(violations, start=1):
        level = entry.get("level", "?")
        msg = entry.get("message", "")
        source = entry.get("source", "?")
        ts = entry.get("timestamp", "?")
        lines.append(f"  [{i}] {level} @ {ts} (source={source})")
        lines.append(f"       {msg}")
    lines.append("")
    lines.append("To fix: either resolve the server-side issue or add the pattern")
    lines.append(
        "to LOG_AUDIT_ALLOWLIST in tests/e2e/log_audit_gate.py with justification."
    )
    return "\n".join(lines)


@dataclass
class AuditGateResult:
    """Outcome of a single log-audit gate run.

    Attributes:
        passed: True when the phase produced no non-allowlisted ERROR/WARNING.
        violations: List of non-allowlisted log entry dicts (empty when passed).
        phase_name: Human-readable phase name for failure messages.
    """

    passed: bool
    violations: list[dict[str, Any]]
    phase_name: str

    def failure_message(self) -> str:
        """Return the human-readable failure description, or empty string when passed."""
        if self.passed:
            return ""
        return build_audit_failure_message(self.violations, self.phase_name)


# ---------------------------------------------------------------------------
# Front-door query helpers (Phase 3 in-process and Phase 4 live)
# ---------------------------------------------------------------------------

# MCP endpoint and JSON-RPC constants
_MCP_ENDPOINT = "/mcp"
_JSONRPC_VERSION = "2.0"
_MCP_METHOD = "tools/call"

# Levels to audit (comma-separated, matching admin_logs_query API)
_AUDIT_LEVELS = "ERROR,WARNING"

# Pagination: fetch enough entries per page for a full phase audit
_AUDIT_PAGE_SIZE = 500

# Hard ceiling on pages walked per audit query. Defends against an unbounded
# loop if the server's pagination metadata is ever wrong (Messi rule 14).
_AUDIT_MAX_PAGES = 1000


def _admin_logs_query_page(
    client: Any,
    token: str,
    *,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """Fetch a single page of ERROR/WARNING log entries via the MCP front door.

    Works with both FastAPI TestClient (Phase 3) and httpx.Client (Phase 4).

    Args:
        client: TestClient or httpx.Client bound to the server base URL.
        token: Valid JWT access token with admin role.
        page: 1-based page number to fetch.
        page_size: Maximum entries per page.

    Returns:
        The parsed data dict from the admin_logs_query response, including
        ``logs`` (list) and ``pagination`` (dict with ``total_pages``).

    Raises:
        AssertionError: HTTP non-200, MCP error key present, or success=False.
    """
    import json as _json

    payload = {
        "jsonrpc": _JSONRPC_VERSION,
        "id": 1,
        "method": _MCP_METHOD,
        "params": {
            "name": "admin_logs_query",
            "arguments": {
                "level": _AUDIT_LEVELS,
                "page": page,
                "page_size": page_size,
                "sort_order": "asc",
            },
        },
    }
    response = client.post(
        _MCP_ENDPOINT,
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, (
        f"admin_logs_query HTTP {response.status_code}: {response.text[:300]}"
    )
    body = response.json()
    assert "error" not in body, f"admin_logs_query MCP error: {body.get('error')}"
    result = body.get("result", {})

    # The MCP framework wraps all tool results in a content list.
    # Two observed response shapes from TestClient vs live server:
    #   Shape A (dict with content key): {"content": [{"type": "text", "text": "<json>"}]}
    #   Shape B (list):                  [{"type": "text", "text": "<json>"}]
    # In both cases the actual data JSON is in content[0]["text"].
    content_items: list | None = None
    if isinstance(result, dict) and "content" in result:
        content_items = result["content"]
    elif isinstance(result, list):
        content_items = result

    if content_items:
        content_text = content_items[0].get("text", "{}")
        data = _json.loads(content_text)
    else:
        # Fallback: result IS the data dict (should not happen with real server)
        data = result if isinstance(result, dict) else {}

    assert data.get("success", False), (
        f"admin_logs_query returned success=False: {data.get('error')}"
    )
    return cast("dict[str, Any]", data)


def query_logs_via_mcp(
    client: Any,
    token: str,
    *,
    page_size: int = _AUDIT_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Query ALL ERROR/WARNING entries via the admin_logs_query MCP front door.

    Walks every page (bounded by _AUDIT_MAX_PAGES) and returns the COMPLETE
    set of matching entries, so the audit never silently drops entries beyond
    the first page. The caller diffs the result against the phase watermark
    via filter_new_entries.

    Works with both FastAPI TestClient (Phase 3) and httpx.Client (Phase 4).

    Args:
        client: TestClient or httpx.Client bound to the server base URL.
        token: Valid JWT access token with admin role.
        page_size: Maximum entries per page (controls the page-walk granularity).

    Returns:
        Complete list of all log entry dicts matching ERROR/WARNING level.

    Raises:
        AssertionError: MCP failure, success=False, or page ceiling exceeded
            (completeness cannot be guaranteed).
    """
    collected: list[dict[str, Any]] = []
    for page in range(1, _AUDIT_MAX_PAGES + 1):
        data = _admin_logs_query_page(client, token, page=page, page_size=page_size)
        page_logs = list(data.get("logs", []))
        collected.extend(page_logs)

        pagination = data.get("pagination", {}) or {}
        total_pages = pagination.get("total_pages")
        if total_pages is not None:
            if page >= total_pages:
                return collected
        elif len(page_logs) < page_size:
            # No pagination metadata: a short page proves exhaustion.
            return collected
    raise AssertionError(
        f"admin_logs_query exceeded {_AUDIT_MAX_PAGES} pages "
        f"(page_size={page_size}); audit completeness cannot be guaranteed."
    )


def get_log_watermark(
    client: Any,
    token: str,
) -> int:
    """Return the current maximum log id (watermark) for diffing.

    Called at phase-start, after the wiped-slate server boot but before
    the phase's tests run.  Entries at or below this id are pre-existing
    and excluded from the audit.

    Args:
        client: TestClient or httpx.Client.
        token: Valid JWT access token.

    Returns:
        Maximum log id seen, or 0 if the store is empty.
    """
    entries = query_logs_via_mcp(client, token)
    if not entries:
        return 0
    return max((e.get("id") or 0) for e in entries)


def run_log_audit_gate(
    client: Any,
    token: str,
    watermark_id: int,
    phase_name: str,
) -> AuditGateResult:
    """Run the full log-audit gate and return the result.

    For in-process (Phase 3) callers: flush the SQLiteLogHandler BEFORE
    calling this function (via app.state.sqlite_log_handler.flush()).
    For live-server callers: use poll_until_stable_count to ensure
    the async writer has drained before calling this function.

    Args:
        client: TestClient or httpx.Client.
        token: Valid JWT access token with admin role.
        watermark_id: Log id watermark recorded at phase-start.
        phase_name: Human-readable phase name for failure reporting.

    Returns:
        AuditGateResult with passed=True when no non-allowlisted entries found.
    """
    entries = query_logs_via_mcp(client, token)
    new_entries = filter_new_entries(entries, watermark_id=watermark_id)
    violations = [e for e in new_entries if not is_allowlisted(e)]
    passed = len(violations) == 0
    return AuditGateResult(passed=passed, violations=violations, phase_name=phase_name)
