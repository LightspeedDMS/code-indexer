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

LOG_AUDIT_ALLOWLIST: List[str] = [
    "http_client_factory not available on app.state",
    "CIDX_TEST_FAST_SQLITE",
    "could not read config; treating as disabled",
    "No description found",
    "No module named 'tree_sitter_languages'",
    "LOG_AUDIT_GATE_MUTATION_TEST_1122",
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
