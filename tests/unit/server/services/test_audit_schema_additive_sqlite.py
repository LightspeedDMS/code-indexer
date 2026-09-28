"""Additive SQLite schema for the attributed audit table.

A real temporary ``groups.db`` is used throughout; nothing about the store
is mocked.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import List

import pytest

from code_indexer.server.services.audit_events import build_event, build_legacy_event
from code_indexer.server.services.audit_log_service import (
    AUDIT_ATTRIBUTION_COLUMNS,
    AUDIT_ATTRIBUTION_INDEXES,
    AuditLogService,
    add_audit_column_tolerating_race,
)

NEW_COLUMNS = [
    "outcome",
    "source",
    "ip_address",
    "correlation_id",
    "node_id",
    "auth_method",
    "actor_is_system",
    "event_uuid",
]
NEW_INDEXES = [
    "idx_audit_logs_admin_id",
    "idx_audit_logs_target_id",
    "idx_audit_logs_target_type_timestamp",
    "idx_audit_logs_timestamp_id",
    "idx_audit_logs_correlation_id",
    "idx_audit_logs_event_uuid",
]


def _columns(db_path: Path) -> List[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [row[1] for row in conn.execute("PRAGMA table_info(audit_logs)")]
    finally:
        conn.close()


def _indexes(db_path: Path) -> List[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [row[1] for row in conn.execute("PRAGMA index_list(audit_logs)")]
    finally:
        conn.close()


def _create_legacy_table(db_path: Path) -> None:
    """A groups.db as the previous release left it (already in WAL mode)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            "CREATE TABLE audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT NOT NULL, admin_id TEXT NOT NULL, "
            "action_type TEXT NOT NULL, target_type TEXT NOT NULL, "
            "target_id TEXT NOT NULL, details TEXT)"
        )
        conn.execute(
            "INSERT INTO audit_logs (timestamp, admin_id, action_type, "
            "target_type, target_id, details) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-01-01T00:00:00+00:00", "admin", "group_create", "group", "7", "{}"),
        )
        conn.commit()
    finally:
        conn.close()


def test_declared_columns_and_indexes_match_the_schema_contract() -> None:
    assert [name for name, _ in AUDIT_ATTRIBUTION_COLUMNS] == NEW_COLUMNS
    assert [name for name, _ in AUDIT_ATTRIBUTION_INDEXES] == NEW_INDEXES


def test_fresh_database_has_all_columns_and_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "groups.db"
    AuditLogService(db_path)
    assert set(NEW_COLUMNS) <= set(_columns(db_path))
    assert set(NEW_INDEXES) <= set(_indexes(db_path))


def test_legacy_table_is_upgraded_and_old_rows_are_kept(tmp_path: Path) -> None:
    db_path = tmp_path / "groups.db"
    _create_legacy_table(db_path)
    AuditLogService(db_path)
    assert set(NEW_COLUMNS) <= set(_columns(db_path))
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT admin_id, action_type, outcome, source, ip_address, "
            "correlation_id, node_id, auth_method, actor_is_system, event_uuid "
            "FROM audit_logs"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("admin", "group_create", None, None, None, None, None, None, 0, None)


def test_second_start_is_a_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "groups.db"
    AuditLogService(db_path)
    before = (_columns(db_path), sorted(_indexes(db_path)))
    AuditLogService(db_path)
    assert (_columns(db_path), sorted(_indexes(db_path))) == before


def test_concurrent_schema_ensure_does_not_error(tmp_path: Path) -> None:
    db_path = tmp_path / "groups.db"
    _create_legacy_table(db_path)
    errors: List[BaseException] = []

    def _boot() -> None:
        try:
            AuditLogService(db_path)
        except BaseException as exc:  # noqa: BLE001 - collected for the assert
            errors.append(exc)

    threads = [threading.Thread(target=_boot) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert _columns(db_path).count("event_uuid") == 1


def test_duplicate_column_race_is_tolerated(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "race.db"))
    try:
        conn.execute("CREATE TABLE audit_logs (id INTEGER PRIMARY KEY)")
        add_audit_column_tolerating_race(conn, "outcome", "TEXT")
        add_audit_column_tolerating_race(conn, "outcome", "TEXT")
        columns = [row[1] for row in conn.execute("PRAGMA table_info(audit_logs)")]
    finally:
        conn.close()
    assert columns.count("outcome") == 1


def test_other_alter_errors_propagate(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "race.db"))
    try:
        with pytest.raises(sqlite3.OperationalError):
            add_audit_column_tolerating_race(conn, "outcome", "TEXT")
    finally:
        conn.close()


def test_insert_events_writes_every_column(tmp_path: Path) -> None:
    db_path = tmp_path / "groups.db"
    service = AuditLogService(db_path)
    event = build_event(
        actor="alice",
        action_type="user_deleted",
        target_type="user",
        target_id="bob",
        outcome="success",
        details={"deleted_role": "admin"},
    )
    legacy = build_legacy_event(
        actor="admin",
        action_type="group_create",
        target_type="group",
        target_id="7",
        details_json=None,
    )
    service.insert_events([event, legacy])
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT timestamp, admin_id, action_type, target_type, target_id, "
            "details, outcome, source, ip_address, correlation_id, node_id, "
            "auth_method, actor_is_system, event_uuid FROM audit_logs ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert rows[0] == (
        event.occurred_at,
        "alice",
        "user_deleted",
        "user",
        "bob",
        '{"deleted_role": "admin"}',
        "success",
        "system",
        None,
        event.correlation_id,
        None,
        None,
        0,
        event.event_uuid,
    )
    assert rows[1][13] == legacy.event_uuid
    assert rows[1][6] is None
