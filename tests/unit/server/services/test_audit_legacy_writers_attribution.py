"""Pre-existing audit writers gain attribution without any call-site change.

``GroupAccessManager.log_audit`` and ``PasswordChangeAuditLogger`` keep their
delivery (AuditLogService.log / log_raw) and their seven legacy columns;
their rows now also carry a per-event uuid, the front door, the peer
address, the correlation id and the node id.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterator, Tuple

import pytest

from code_indexer.server.middleware.audit_request_context import (
    AuditRequestContext,
    bind_audit_request_context,
    reset_audit_request_context,
)
from code_indexer.server.services import audit_events
from code_indexer.server.services.audit_log_service import (
    AuditLogService,
    migrate_flat_file_to_sqlite,
)
from code_indexer.server.telemetry.correlation_bridge import _correlation_id_var

_COLUMNS = (
    "timestamp, admin_id, action_type, target_type, target_id, details, "
    "outcome, source, ip_address, correlation_id, node_id, auth_method, "
    "actor_is_system, event_uuid"
)


@pytest.fixture()
def request_scope() -> Iterator[None]:
    audit_events.set_process_node_id("node-l")
    ctx_token = bind_audit_request_context(
        AuditRequestContext(source="rest", client_ip="192.0.2.7", auth_method="jwt")
    )
    corr_token = _correlation_id_var.set("req-legacy")
    try:
        yield
    finally:
        _correlation_id_var.reset(corr_token)
        reset_audit_request_context(ctx_token)
        audit_events.set_process_node_id(None)


def _only_row(db_path: Path) -> Tuple:
    conn = sqlite3.connect(str(db_path))
    try:
        found = conn.execute(f"SELECT {_COLUMNS} FROM audit_logs").fetchall()
    finally:
        conn.close()
    assert len(found) == 1
    return tuple(found[0])


def test_group_manager_log_audit_rows_are_attributed(
    tmp_path: Path, request_scope
) -> None:
    from code_indexer.server.services.group_access_manager import (
        GroupAccessManager,
    )

    db_path = tmp_path / "groups.db"
    manager = GroupAccessManager(db_path)
    manager.set_audit_service(AuditLogService(db_path))
    manager.log_audit(
        admin_id="admin",
        action_type="group_create",
        target_type="group",
        target_id="7",
        details={"name": "example"},
    )
    row = _only_row(db_path)
    assert row[1:6] == (
        "admin",
        "group_create",
        "group",
        "7",
        json.dumps({"name": "example"}),
    )
    assert row[0].endswith("+00:00")
    assert row[6:13] == (None, "rest", "192.0.2.7", "req-legacy", "node-l", "jwt", 0)
    assert row[13]


def _rows_in(db_path: Path, columns: str = "action_type, admin_id") -> list:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT {columns} FROM audit_logs").fetchall()
    finally:
        conn.close()


def test_manager_without_injected_service_writes_to_the_bound_sink_in_a_server(
    tmp_path: Path, request_scope
) -> None:
    from code_indexer.server.services import audit_capture
    from code_indexer.server.services.group_access_manager import (
        GroupAccessManager,
    )

    bound_path = tmp_path / "bound.db"
    own_path = tmp_path / "per_request.db"
    bound = AuditLogService(bound_path)
    bound.start()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(bound, node_id="node-l")
    try:
        manager = GroupAccessManager(own_path)  # built per request, no service
        manager.log_audit(
            admin_id="admin",
            action_type="group_delete",
            target_type="group",
            target_id="7",
        )
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        bound.stop()
    found = _rows_in(bound_path, "action_type, admin_id, event_uuid")
    assert [(r[0], r[1]) for r in found] == [("group_delete", "admin")]
    assert found[0][2]
    assert _rows_in(own_path) == []


def test_manager_without_service_outside_a_server_keeps_direct_write(
    tmp_path: Path,
) -> None:
    from code_indexer.server.services.group_access_manager import (
        GroupAccessManager,
    )

    own_path = tmp_path / "groups.db"
    GroupAccessManager(own_path).log_audit(
        admin_id="admin",
        action_type="group_delete",
        target_type="group",
        target_id="7",
    )
    assert [(r[0], r[1]) for r in _rows_in(own_path)] == [("group_delete", "admin")]


def test_password_audit_logger_rows_are_attributed(
    tmp_path: Path, request_scope
) -> None:
    from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

    db_path = tmp_path / "groups.db"
    audit_logger = PasswordChangeAuditLogger()
    audit_logger.set_audit_service(AuditLogService(db_path))
    audit_logger.log_password_change_success(username="alice", ip_address="192.0.2.7")
    row = _only_row(db_path)
    assert row[1:5] == ("alice", "password_change_success", "auth", "alice")
    assert row[6] == "success"
    assert row[7:11] == ("rest", "192.0.2.7", "req-legacy", "node-l")
    assert row[13]


def test_password_audit_logger_failure_row_has_failure_outcome(
    tmp_path: Path, request_scope
) -> None:
    from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

    db_path = tmp_path / "groups.db"
    audit_logger = PasswordChangeAuditLogger()
    audit_logger.set_audit_service(AuditLogService(db_path))
    audit_logger.log_password_change_failure(
        username="alice", ip_address="192.0.2.7", reason="old password invalid"
    )
    row = _only_row(db_path)
    assert (row[2], row[6]) == ("password_change_failure", "failure")


def test_password_audit_logger_denied_row_has_denied_outcome(
    tmp_path: Path, request_scope
) -> None:
    from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

    db_path = tmp_path / "groups.db"
    audit_logger = PasswordChangeAuditLogger()
    audit_logger.set_audit_service(AuditLogService(db_path))
    audit_logger.log_impersonation_denied(
        actor_username="alice",
        target_username="bob",
        reason="admin role required",
        session_id="s-1",
        ip_address="192.0.2.7",
    )
    row = _only_row(db_path)
    assert (row[2], row[6]) == ("impersonation_denied", "denied")


def test_flat_file_migration_rows_are_system_sourced(tmp_path: Path) -> None:
    db_path = tmp_path / "groups.db"
    flat = tmp_path / "password_audit.log"
    flat.write_text(
        "2026-01-01 00:00:00 UTC - INFO - PASSWORD_CHANGE_SUCCESS: "
        '{"event_type": "password_change_success", "username": "alice", '
        '"timestamp": "2026-01-01T00:00:00+00:00"}\n'
    )
    migrated, skipped = migrate_flat_file_to_sqlite(flat, AuditLogService(db_path))
    assert (migrated, skipped) == (1, 0)
    row = _only_row(db_path)
    assert row[0] == "2026-01-01T00:00:00+00:00"
    assert row[7] == "system"
    assert row[13]
