"""Regression tests for GitHub issue #1642.

Bug: query_audit_logs MCP tool fails 100% of the time with
    {"success": false, "error": "Object of type datetime is not JSON serializable"}

Root cause: AuditLogPostgresBackend.query()/get_pr_logs()/get_cleanup_logs()
(src/code_indexer/server/storage/postgres/audit_log_backend.py) return raw
psycopg row dicts without applying the project's established datetime-safe
serialization helper, sanitize_row() (src/code_indexer/server/storage/postgres/
pg_utils.py). PostgreSQL returns native datetime objects for TIMESTAMPTZ
columns (unlike the SQLite backend, which stores/returns timestamps as TEXT
strings), so the raw datetime flows untouched into handle_query_audit_logs's
response dict and blows up json.dumps() inside _mcp_response(). Every other
PostgreSQL backend in this codebase (users_backend.py, global_repos_backend.py,
groups_backend.py, etc.) calls sanitize_row() on fetched rows -- audit_log_backend.py
was the one that bypassed it.

These tests use REAL datetime objects, the REAL AuditLogPostgresBackend, and
REAL json.dumps/json.loads via the actual handler code. The only faked
component is the psycopg connection pool itself (an external service
boundary), matching the existing convention in test_audit_log_postgres.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from code_indexer.server.storage.postgres.audit_log_backend import (
    AuditLogPostgresBackend,
)

REAL_DT = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)


def _make_row(row_id: int) -> dict:
    """Build one raw psycopg-style audit row carrying a real datetime.

    Shared shape for every layer -- backend-level and handler-level tests
    all exercise the same TIMESTAMPTZ-as-datetime condition psycopg
    actually produces.
    """
    return {
        "id": row_id,
        "timestamp": REAL_DT,
        "admin_id": "admin",
        "action_type": "user_created",
        "target_type": "user",
        "target_id": "alice",
        "details": "{}",
    }


# ---------------------------------------------------------------------------
# Layer 1: AuditLogPostgresBackend must sanitize datetime values in rows
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pool():
    """Fake psycopg v3 ConnectionPool -- the external service boundary."""
    pool = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return pool, conn, cursor


@pytest.fixture
def backend(mock_pool):
    pool, _, _ = mock_pool
    return AuditLogPostgresBackend(pool)


# All three read paths accept `limit` and return datetime-bearing rows.
_READ_METHODS = ["query", "get_pr_logs", "get_cleanup_logs"]


class TestAuditLogPostgresBackendDatetimeSerialization:
    """AuditLogPostgresBackend rows must never carry raw datetime objects.

    psycopg returns native datetime.datetime instances for TIMESTAMPTZ
    columns. The established fix in this codebase is sanitize_row() --
    every other PG backend applies it; audit_log_backend.py must too.
    """

    @pytest.mark.parametrize("method_name", _READ_METHODS)
    def test_read_method_returns_string_timestamp_not_datetime(
        self, backend, mock_pool, method_name
    ):
        _, _, cursor = mock_pool
        cursor.fetchone.return_value = {"cnt": 1}
        cursor.fetchall.return_value = [_make_row(1)]

        result = getattr(backend, method_name)(limit=10)
        rows = result[0] if method_name == "query" else result

        assert len(rows) == 1
        assert not isinstance(rows[0]["timestamp"], datetime), (
            f"AuditLogPostgresBackend.{method_name}() must sanitize datetime "
            "columns via sanitize_row() -- a raw datetime leaks into JSON "
            "serialization"
        )
        assert rows[0]["timestamp"] == REAL_DT.isoformat()
        # Real json.dumps must succeed on the sanitized row -- this is the
        # exact failure mode reported in issue #1642.
        json.dumps(rows[0])


# ---------------------------------------------------------------------------
# Layer 2: handle_query_audit_logs MCP handler end-to-end reproduction
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_user():
    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username="admin",
        password_hash="x",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def audit_service_cursor(mock_pool):
    """Wire the REAL AuditLogPostgresBackend (backed by a fake psycopg pool)
    onto app.state.audit_service for the duration of the test, restoring
    whatever was there afterward, and pre-load it with one datetime-bearing
    row on both the count and fetch paths.

    Direct attribute assignment (not monkeypatch) so the fixture's control
    flow -- install, configure, yield, restore -- stays explicit.
    """
    import code_indexer.server.app as app_module

    pool, _, cursor = mock_pool
    cursor.fetchone.return_value = {"cnt": 1}
    cursor.fetchall.return_value = [_make_row(1)]

    real_backend = AuditLogPostgresBackend(pool)
    sentinel = object()
    previous = getattr(app_module.app.state, "audit_service", sentinel)
    app_module.app.state.audit_service = real_backend
    try:
        yield cursor
    finally:
        if previous is sentinel:
            del app_module.app.state.audit_service
        else:
            app_module.app.state.audit_service = previous


class TestHandleQueryAuditLogsDatetimeSerialization:
    """Reproduces GitHub issue #1642 end-to-end at the MCP handler boundary,
    using the REAL AuditLogPostgresBackend (only the psycopg pool is faked).

    Bypasses the @require_mcp_elevation() wrapper via .__wrapped__ since
    elevation is orthogonal to the datetime serialization bug under test.
    """

    def test_query_audit_logs_does_not_raise_on_datetime_field(
        self, admin_user, audit_service_cursor
    ):
        from code_indexer.server.mcp.handlers.admin import handle_query_audit_logs

        # Must not raise TypeError("Object of type datetime is not JSON
        # serializable") -- that is the exact crash reported in issue #1642.
        result = handle_query_audit_logs.__wrapped__({}, admin_user)

        assert "content" in result
        text = result["content"][0]["text"]
        # The response text itself must be valid, already-serialized JSON.
        payload = json.loads(text)
        assert payload["success"] is True

    def test_query_audit_logs_response_contains_iso_string_timestamp(
        self, admin_user, audit_service_cursor
    ):
        from code_indexer.server.mcp.handlers.admin import handle_query_audit_logs

        result = handle_query_audit_logs.__wrapped__({"limit": 5}, admin_user)
        payload = json.loads(result["content"][0]["text"])

        assert payload["success"] is True
        # The fake cursor returns the same datetime-bearing row on all three
        # underlying read paths (get_pr_logs, get_cleanup_logs, and the main
        # audit_logs table query), so every entry must carry the ISO string.
        assert len(payload["entries"]) > 0
        for entry in payload["entries"]:
            assert entry["timestamp"] == REAL_DT.isoformat()
