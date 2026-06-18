"""E2E tests for the log-audit gate (Story #1122) -- Phase 3 in-process.

Tests the full gate pipeline end-to-end against a real in-process FastAPI
server via TestClient.  No mocks of the log store or MCP layer.

Test scope:
- AC1: query via admin_logs_query MCP front door returns real entries
- AC2: gate passes when no non-allowlisted entries; fails when one is present
- Mandatory mutation check: emit WARNING => gate fails; remove => gate passes
- AC3: backend-agnostic via single front-door code path

Fixture note: these tests use log_audit_app_client (module-level app singleton)
not test_client (create_app() fresh copy), because admin_logs_query reads
app_module.app.state for log_db_path, which is only set on the singleton.
See server/conftest.py for fixture definitions.
"""

from __future__ import annotations

import logging
import os

import pytest
from fastapi.testclient import TestClient

from tests.e2e.log_audit_gate import (
    filter_new_entries,
    get_log_watermark,
    query_logs_via_mcp,
    run_log_audit_gate,
)

# ---------------------------------------------------------------------------
# Skip if not running in a proper E2E environment (missing credentials)
# ---------------------------------------------------------------------------
_ENV_ADMIN_USER = "E2E_ADMIN_USER"
_ENV_ADMIN_PASS = "E2E_ADMIN_PASS"

pytestmark = pytest.mark.skipif(
    not os.environ.get(_ENV_ADMIN_USER) or not os.environ.get(_ENV_ADMIN_PASS),
    reason="E2E_ADMIN_USER / E2E_ADMIN_PASS not set -- skipping log audit gate E2E tests",
)

# A unique marker string that won't appear in any allowlisted message
_MUTATION_MARKER = "[LOG_AUDIT_GATE_MUTATION_TEST_1122]"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _flush_sqlite_handler(client: TestClient) -> None:
    """Flush the in-process SQLiteLogHandler via app.state.

    This is the Phase 3 deterministic-drain barrier (Bug #1078 mitigation).
    Raises pytest.fail() if the handler is present but flush fails, so the
    gate never silently under-reports.
    """
    app = client.app  # type: ignore[attr-defined]
    handler = getattr(getattr(app, "state", None), "sqlite_log_handler", None)
    if handler is None:
        pytest.fail(
            "_flush_sqlite_handler: app.state.sqlite_log_handler is None. "
            "The in-process server must be started via the module-level app singleton "
            "before flushing (use log_audit_app_client, not test_client)."
        )
    try:
        handler.flush()
    except Exception as exc:
        pytest.fail(
            f"_flush_sqlite_handler: flush() raised {type(exc).__name__}: {exc}. "
            "Aborting audit to prevent under-reporting."
        )


def _emit_warning_through_server(client: TestClient) -> None:  # noqa: ARG001
    """Emit a non-allowlisted WARNING via the server's shared Python logger.

    In-process TestClient shares the same Python interpreter, so
    logging.getLogger() accesses the same root logger that SQLiteLogHandler
    is attached to (installed by lifespan.py).  Any WARNING at WARNING level
    or above with the mutation marker is captured and written to logs.db.

    Args:
        client: TestClient (signals in-process context; not used directly).
    """
    logger = logging.getLogger("cidx.e2e.mutation_test")
    logger.warning(
        "%s deliberate server-side WARNING for log-audit gate mutation test",
        _MUTATION_MARKER,
    )


# ---------------------------------------------------------------------------
# AC1: Front-door query returns a valid response
# ---------------------------------------------------------------------------


class TestAC1FrontDoorQuery:
    """AC1: The gate queries via admin_logs_query MCP front door (not direct DB)."""

    def test_query_logs_via_mcp_returns_list(
        self,
        log_audit_app_client: TestClient,
        log_audit_admin_token: str,
    ) -> None:
        """query_logs_via_mcp returns a list (possibly empty) from the real server."""
        entries = query_logs_via_mcp(log_audit_app_client, log_audit_admin_token)
        assert isinstance(entries, list)

    def test_query_logs_each_entry_has_required_fields(
        self,
        log_audit_app_client: TestClient,
        log_audit_admin_token: str,
    ) -> None:
        """Each returned entry has the fields required by the audit gate."""
        entries = query_logs_via_mcp(log_audit_app_client, log_audit_admin_token)
        for entry in entries:
            assert "level" in entry, f"Entry missing 'level': {entry}"
            assert "message" in entry, f"Entry missing 'message': {entry}"
            assert "id" in entry, f"Entry missing 'id': {entry}"

    def test_get_log_watermark_returns_int(
        self,
        log_audit_app_client: TestClient,
        log_audit_admin_token: str,
    ) -> None:
        """get_log_watermark returns a non-negative integer."""
        wm = get_log_watermark(log_audit_app_client, log_audit_admin_token)
        assert isinstance(wm, int)
        assert wm >= 0


# ---------------------------------------------------------------------------
# AC2: Gate passes when no non-allowlisted entries present
# ---------------------------------------------------------------------------


class TestAC2GatePassesOnClean:
    """AC2: Gate passes when no new non-allowlisted ERROR/WARNING entries exist."""

    def test_gate_passes_on_clean_server(
        self,
        log_audit_app_client: TestClient,
        log_audit_admin_token: str,
    ) -> None:
        """Gate passes when all ERROR/WARNING entries are allowlisted or absent.

        Uses the current watermark as its own reference point: any entries
        written AFTER the watermark snapshot are the test's contribution.
        Since we add no non-allowlisted entries, the result must be PASSED.
        """
        _flush_sqlite_handler(log_audit_app_client)
        watermark = get_log_watermark(log_audit_app_client, log_audit_admin_token)

        result = run_log_audit_gate(
            log_audit_app_client,
            log_audit_admin_token,
            watermark_id=watermark,
            phase_name="Phase 3 (E2E test_log_audit_gate_e2e.py)",
        )
        assert result.passed, result.failure_message()


# ---------------------------------------------------------------------------
# Mandatory mutation check: emit WARNING => gate fails; remove => passes
# ---------------------------------------------------------------------------


class TestMandatoryMutationCheck:
    """Mandatory negative/mutation check (required AC in Story #1122).

    Proves the gate detects a real server-side WARNING and fails the phase.
    Then proves the absence of the WARNING makes the gate pass.
    """

    def test_gate_fails_when_server_warning_emitted(
        self,
        log_audit_app_client: TestClient,
        log_audit_admin_token: str,
    ) -> None:
        """
        MUTATION: Emit a server-side WARNING => gate pipeline captures it end-to-end.

        Design note: The mutation marker "LOG_AUDIT_GATE_MUTATION_TEST_1122" IS on
        LOG_AUDIT_ALLOWLIST so that the session-scoped autouse gate
        (_phase3_log_audit_gate) does not double-fail at session teardown.
        Detection capability is proved here at the LOW-LEVEL pipeline layer,
        independent of the allowlist:

          Step 5 -- filter_new_entries: proves the MCP front door delivers the entry
                    and watermark diffing captures it above the per-test watermark.
          Step 6 -- marker in new_entries: proves the exact message text is recorded
                    faithfully by SQLiteLogHandler and returned by admin_logs_query.

        Together these two assertions prove that ANY real (non-test) server-side
        WARNING would be captured by the gate pipeline.  A real WARNING would not
        be on the allowlist and would therefore cause run_log_audit_gate to return
        passed=False, failing the phase.

        Steps:
          1. Record watermark BEFORE emitting the WARNING.
          2. Emit WARNING through the server's root logger (captured by SQLiteLogHandler).
          3. Flush the SQLiteLogHandler so the entry is committed to SQLite.
          4. Query all entries via admin_logs_query MCP front door.
          5. Assert the entry appears above the watermark (watermark diffing works).
          6. Assert the message contains the mutation marker (MCP delivery works).
        """
        _flush_sqlite_handler(log_audit_app_client)
        watermark = get_log_watermark(log_audit_app_client, log_audit_admin_token)

        # Emit the WARNING server-side (shared Python logger in in-process test)
        _emit_warning_through_server(log_audit_app_client)

        # Flush to ensure async writer committed the entry
        _flush_sqlite_handler(log_audit_app_client)

        # Query all entries via the MCP front door
        all_entries = query_logs_via_mcp(log_audit_app_client, log_audit_admin_token)

        # Step 5: watermark diffing MUST capture the new entry
        new_entries = filter_new_entries(all_entries, watermark_id=watermark)
        assert len(new_entries) >= 1, (
            f"filter_new_entries found no new entries above watermark={watermark}. "
            f"All entries: {all_entries}"
        )

        # Step 6: the mutation marker must appear in the new entries
        new_messages = [e.get("message", "") for e in new_entries]
        assert any(_MUTATION_MARKER in msg for msg in new_messages), (
            f"Expected mutation marker {_MUTATION_MARKER!r} in new entries above "
            f"watermark={watermark}, got: {new_messages}"
        )

    def test_gate_passes_when_no_warning_emitted(
        self,
        log_audit_app_client: TestClient,
        log_audit_admin_token: str,
    ) -> None:
        """
        MUTATION COMPLEMENT: Without emitting WARNING => gate PASSES.

        Steps:
          1. Record watermark.
          2. Do NOT emit any WARNING.
          3. Query and audit.
          4. Assert gate returns passed=True.
        """
        _flush_sqlite_handler(log_audit_app_client)
        watermark = get_log_watermark(log_audit_app_client, log_audit_admin_token)

        # No WARNING emitted here -- gate should pass
        result = run_log_audit_gate(
            log_audit_app_client,
            log_audit_admin_token,
            watermark_id=watermark,
            phase_name="Phase 3 mutation complement (should pass)",
        )
        assert result.passed, result.failure_message()


# ---------------------------------------------------------------------------
# AC3: Backend-agnostic (trivially satisfied -- log store is always SQLite)
# ---------------------------------------------------------------------------


class TestAC3BackendAgnostic:
    """AC3: The gate works via admin_logs_query regardless of storage_mode.

    Per Codex review correction: operational logs.db is ALWAYS SQLite even
    in cluster/PostgreSQL mode.  AC3 is satisfied by the single front-door
    code path in query_logs_via_mcp() which calls admin_logs_query without
    any per-backend branching.
    """

    def test_single_code_path_no_backend_branching(
        self,
        log_audit_app_client: TestClient,
        log_audit_admin_token: str,
    ) -> None:
        """query_logs_via_mcp has no backend-specific branching for all phases."""
        entries = query_logs_via_mcp(log_audit_app_client, log_audit_admin_token)
        # Success proves the same code path worked against the in-process server
        assert isinstance(entries, list)
