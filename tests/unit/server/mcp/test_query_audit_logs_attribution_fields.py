"""MCP ``query_audit_logs`` entries expose the audit attribution columns.

The new fields are additive: every pre-existing entry field keeps its name
and value.  Uses a REAL AuditLogService on a real temporary SQLite file.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterator

import pytest

from code_indexer.server.middleware.audit_request_context import (
    AuditRequestContext,
    bind_audit_request_context,
    reset_audit_request_context,
)
from code_indexer.server.services import audit_events
from code_indexer.server.services.audit_log_service import AuditLogService
from code_indexer.server.telemetry.correlation_bridge import _correlation_id_var

_NEW_FIELDS = (
    "id",
    "outcome",
    "source",
    "ip_address",
    "correlation_id",
    "node_id",
    "auth_method",
    "actor_is_system",
    "event_uuid",
)


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
def wired_audit_service(tmp_path) -> Iterator[AuditLogService]:
    import code_indexer.server.app as app_module

    service = AuditLogService(tmp_path / "groups.db")
    sentinel = object()
    previous = getattr(app_module.app.state, "audit_service", sentinel)
    app_module.app.state.audit_service = service
    try:
        yield service
    finally:
        if previous is sentinel:
            del app_module.app.state.audit_service
        else:
            app_module.app.state.audit_service = previous


def _only_entry(admin_user) -> Dict[str, Any]:
    from code_indexer.server.mcp.handlers.admin import handle_query_audit_logs

    result = handle_query_audit_logs.__wrapped__({"limit": 10}, admin_user)
    payload = json.loads(result["content"][0]["text"])
    assert payload["success"] is True
    assert len(payload["entries"]) == 1
    entry: Dict[str, Any] = payload["entries"][0]
    return entry


def test_entries_carry_the_attribution_columns(admin_user, wired_audit_service) -> None:
    audit_events.set_process_node_id("node-a")
    ctx_token = bind_audit_request_context(
        AuditRequestContext(source="mcp", client_ip="192.0.2.9", auth_method="jwt")
    )
    corr_token = _correlation_id_var.set("req-mcp-read")
    try:
        wired_audit_service.log(
            admin_id="admin",
            action_type="password_change_failure",
            target_type="auth",
            target_id="alice",
            details='{"reason": "x"}',
        )
    finally:
        _correlation_id_var.reset(corr_token)
        reset_audit_request_context(ctx_token)
        audit_events.set_process_node_id(None)

    entry = _only_entry(admin_user)
    # Pre-existing fields are unchanged.
    assert entry["user"] == "admin"
    assert entry["action"] == entry["action_type"] == "password_change_failure"
    assert entry["target_type"] == "auth"
    assert entry["target_id"] == entry["resource"] == "alice"
    assert entry["details"] == {"reason": "x"}
    # Additive attribution fields.
    assert isinstance(entry["id"], int)
    assert entry["outcome"] == "failure"
    assert entry["source"] == "mcp"
    assert entry["ip_address"] == "192.0.2.9"
    assert entry["correlation_id"] == "req-mcp-read"
    assert entry["node_id"] == "node-a"
    assert entry["auth_method"] == "jwt"
    assert entry["actor_is_system"] is False
    assert entry["event_uuid"]


def test_legacy_row_without_attribution_reports_nulls(
    admin_user, wired_audit_service, tmp_path
) -> None:
    conn = sqlite3.connect(str(tmp_path / "groups.db"))
    try:
        conn.execute(
            "INSERT INTO audit_logs (timestamp, admin_id, action_type, "
            "target_type, target_id, details) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-01-01T00:00:00+00:00", "admin", "group_create", "group", "7", None),
        )
        conn.commit()
    finally:
        conn.close()
    entry = _only_entry(admin_user)
    assert {k: entry[k] for k in _NEW_FIELDS if k != "id"} == {
        "outcome": None,
        "source": None,
        "ip_address": None,
        "correlation_id": None,
        "node_id": None,
        "auth_method": None,
        "actor_is_system": False,
        "event_uuid": None,
    }
