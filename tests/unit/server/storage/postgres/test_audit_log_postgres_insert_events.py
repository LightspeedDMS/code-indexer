"""PostgreSQL side of the attributed audit table.

Static checks of migration 053 run everywhere.  The live checks run against a
REAL PostgreSQL server (``TEST_POSTGRES_DSN``); each test gets its own fresh
database, the real ``MigrationRunner`` (with its advisory lock) and the
project's real ``ConnectionPool``.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Iterator, List, Set, Tuple

import pytest

from code_indexer.server.services.audit_events import build_event, build_legacy_event
from code_indexer.server.services.audit_log_service import (
    AUDIT_ATTRIBUTION_COLUMNS,
    AUDIT_ATTRIBUTION_INDEXES,
)

_SQL_DIR = (
    Path(__file__).resolve().parents[5]
    / "src"
    / "code_indexer"
    / "server"
    / "storage"
    / "postgres"
    / "migrations"
    / "sql"
)
_MIGRATION_NAME = "053_audit_logs_attribution_columns.sql"


def _strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL))


def _migration_sql() -> str:
    return _strip_comments((_SQL_DIR / _MIGRATION_NAME).read_text())


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------


def test_migration_number_is_unique() -> None:
    prefixes = [p.name.split("_")[0] for p in _SQL_DIR.glob("*.sql")]
    assert prefixes.count("053") == 1
    assert (_SQL_DIR / _MIGRATION_NAME).exists()


def test_migration_adds_exactly_the_attribution_columns() -> None:
    added = re.findall(
        r"ALTER\s+TABLE\s+audit_logs\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
        r"(\w+)\s+([^;]+);",
        _migration_sql(),
        flags=re.IGNORECASE,
    )
    assert [(name, " ".join(ddl.split()).upper()) for name, ddl in added] == [
        (name, ddl.upper()) for name, ddl in AUDIT_ATTRIBUTION_COLUMNS
    ]


def test_migration_creates_exactly_the_attribution_indexes() -> None:
    created = re.findall(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+ON\s+audit_logs\s*"
        r"\(([^)]*)\)",
        _migration_sql(),
        flags=re.IGNORECASE,
    )
    assert [(name, " ".join(cols.split())) for name, cols in created] == list(
        AUDIT_ATTRIBUTION_INDEXES
    )


def test_migration_is_additive_only() -> None:
    sql = _migration_sql().upper()
    for forbidden in ("DROP ", "RENAME", "ALTER COLUMN", " TYPE ", "CONCURRENTLY"):
        assert forbidden not in sql, forbidden
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    assert len(statements) == len(AUDIT_ATTRIBUTION_COLUMNS) + len(
        AUDIT_ATTRIBUTION_INDEXES
    )


def test_backend_satisfies_the_protocol_with_insert_events() -> None:
    from code_indexer.server.storage.postgres.audit_log_backend import (
        AuditLogPostgresBackend,
    )
    from code_indexer.server.storage.protocols import AuditLogBackend

    backend = AuditLogPostgresBackend(pool=object())
    assert isinstance(backend, AuditLogBackend)
    assert callable(getattr(backend, "insert_events", None))


def test_each_audit_store_module_has_exactly_one_insert() -> None:
    """One write function per backend: the legacy entry points reuse it."""
    src = _SQL_DIR.parents[3]  # .../code_indexer/server
    for module in (
        src / "storage" / "postgres" / "audit_log_backend.py",
        src / "services" / "audit_log_service.py",
    ):
        text = module.read_text()
        assert len(re.findall(r"INSERT\s+INTO\s+audit_logs", text)) == 1, module


def test_no_other_postgres_module_inserts_audit_rows() -> None:
    """Every PostgreSQL audit row goes through the backend's one write."""
    pg_dir = _SQL_DIR.parents[1]  # .../storage/postgres
    writers = sorted(
        path.name
        for path in pg_dir.rglob("*.py")
        if re.search(r"INSERT\s+INTO\s+audit_logs", path.read_text())
    )
    assert writers == ["audit_log_backend.py"]


# ---------------------------------------------------------------------------
# Live PostgreSQL
# ---------------------------------------------------------------------------

_DSN = os.environ.get("TEST_POSTGRES_DSN", "")
live = pytest.mark.skipif(
    not _DSN, reason="No PostgreSQL available (set TEST_POSTGRES_DSN to enable)"
)


def _dsn_for(dbname: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(_DSN)
    params["dbname"] = dbname
    return make_conninfo(**params)  # type: ignore[arg-type]


@pytest.fixture()
def fresh_db() -> Iterator[str]:
    import psycopg

    name = f"audit_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _dsn_for(name)
    finally:
        with psycopg.connect(_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _run_migrations(dsn: str, sql_dir: Path = _SQL_DIR) -> int:
    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    with MigrationRunner(dsn) as runner:
        runner._sql_dir = sql_dir
        applied: int = runner.run()
    return applied


def _query(dsn: str, sql: str, params: Tuple = ()) -> List[Tuple]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        return conn.execute(sql, params).fetchall()


def _applied_migrations(dsn: str) -> Set[str]:
    return {r[0] for r in _query(dsn, "SELECT filename FROM schema_migrations")}


# A legacy writer stores only the read-allowlisted fields: 'name' is in the
# group allowlist, 'k' is not (audit_log_query.restrict_legacy_details).
_GROUP_DETAILS_IN = '{"name": "g7", "k": 1}'
_GROUP_DETAILS_STORED = '{"name": "g7"}'


def _schema(dsn: str) -> Tuple[List[Tuple], List[str]]:
    columns = _query(
        dsn,
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_name = 'audit_logs' "
        "ORDER BY ordinal_position",
    )
    indexes = [
        r[0]
        for r in _query(
            dsn,
            "SELECT indexname FROM pg_indexes WHERE tablename = 'audit_logs' "
            "ORDER BY indexname",
        )
    ]
    return columns, indexes


def _events():
    return [
        build_event(
            actor="alice",
            action_type="user_deleted",
            target_type="user",
            target_id="bob",
            outcome="success",
            details={"deleted_role": "normal_user"},
        ),
        build_legacy_event(
            actor="admin",
            action_type="group_create",
            target_type="group",
            target_id="7",
            details_json='{"k": 1}',
        ),
    ]


@live
def test_fresh_database_gets_every_column_and_index(fresh_db: str) -> None:
    _run_migrations(fresh_db)
    columns, indexes = _schema(fresh_db)
    names = {c[0]: c for c in columns}
    for name, _ in AUDIT_ATTRIBUTION_COLUMNS:
        assert name in names
    assert names["actor_is_system"][1:3] == ("integer", "NO")
    assert names["event_uuid"][1] == "text"
    for index_name, _ in AUDIT_ATTRIBUTION_INDEXES:
        assert index_name in indexes


@live
def test_upgrade_keeps_legacy_rows_and_is_idempotent(
    fresh_db: str, tmp_path: Path
) -> None:
    previous = tmp_path / "sql"
    previous.mkdir()
    for path in _SQL_DIR.glob("*.sql"):
        if int(path.name.split("_")[0]) <= 52:
            shutil.copy(path, previous / path.name)
    _run_migrations(fresh_db, previous)
    import psycopg

    with psycopg.connect(fresh_db) as conn:
        conn.execute(
            "INSERT INTO audit_logs (timestamp, admin_id, action_type, "
            "target_type, target_id, details) VALUES (NOW(), 'admin', "
            "'group_create', 'group', '7', '{}')"
        )
    before_upgrade = _applied_migrations(fresh_db)
    assert _MIGRATION_NAME not in before_upgrade
    _run_migrations(fresh_db)
    # Not a total count: later migrations land after 052 too.
    assert _MIGRATION_NAME in _applied_migrations(fresh_db) - before_upgrade
    row = _query(
        fresh_db,
        "SELECT admin_id, outcome, source, ip_address, correlation_id, node_id, "
        "auth_method, actor_is_system, event_uuid FROM audit_logs",
    )
    assert row == [("admin", None, None, None, None, None, None, 0, None)]
    before = _schema(fresh_db)
    assert _run_migrations(fresh_db) == 0
    with psycopg.connect(fresh_db) as conn:  # the file itself is re-runnable
        conn.execute((_SQL_DIR / _MIGRATION_NAME).read_text())
    assert _schema(fresh_db) == before


@live
def test_insert_events_writes_one_transaction_per_batch(fresh_db: str) -> None:
    from code_indexer.server.storage.postgres.audit_log_backend import (
        AuditLogPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import (
        ConnectionPool,
    )

    _run_migrations(fresh_db)
    pool = ConnectionPool(fresh_db, min_size=1, max_size=2)
    try:
        events = _events()
        AuditLogPostgresBackend(pool).insert_events(events)
    finally:
        pool.close()
    rows = _query(
        fresh_db,
        "SELECT xmin::text, admin_id, action_type, details, outcome, source, "
        "correlation_id, actor_is_system, event_uuid FROM audit_logs ORDER BY id",
    )
    assert len(rows) == 2
    assert rows[0][0] == rows[1][0]  # same inserting transaction
    assert rows[0][1:5] == (
        "alice",
        "user_deleted",
        '{"deleted_role": "normal_user"}',
        "success",
    )
    assert rows[0][5] == "system"
    assert rows[0][6] == events[0].correlation_id
    assert rows[0][7] == 0
    assert [r[8] for r in rows] == [e.event_uuid for e in events]


@live
def test_legacy_log_entry_points_write_through_insert_events(fresh_db: str) -> None:
    from code_indexer.server.storage.postgres.audit_log_backend import (
        AuditLogPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import (
        ConnectionPool,
    )

    _run_migrations(fresh_db)
    pool = ConnectionPool(fresh_db, min_size=1, max_size=2)
    try:
        backend = AuditLogPostgresBackend(pool)
        backend.log("admin", "group_create", "group", "7", _GROUP_DETAILS_IN)
        backend.log_raw("2026-01-01T00:00:00+00:00", "admin", "migrated", "x", "all")
    finally:
        pool.close()
    rows = _query(
        fresh_db,
        "SELECT action_type, details, actor_is_system, event_uuid, "
        "to_char(timestamp AT TIME ZONE 'UTC', 'YYYY-MM-DD') "
        "FROM audit_logs ORDER BY id",
    )
    assert [r[:3] for r in rows] == [
        ("group_create", _GROUP_DETAILS_STORED, 0),
        ("migrated", None, 0),
    ]
    assert all(r[3] for r in rows) and rows[0][3] != rows[1][3]
    assert rows[1][4] == "2026-01-01"


@live
def test_query_returns_the_attribution_columns(fresh_db: str) -> None:
    from code_indexer.server.storage.postgres.audit_log_backend import (
        AuditLogPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import (
        ConnectionPool,
    )

    from code_indexer.server.services.audit_log_query import AuditFilters

    _run_migrations(fresh_db)
    events = _events()
    pool = ConnectionPool(fresh_db, min_size=1, max_size=2)
    try:
        backend = AuditLogPostgresBackend(pool)
        backend.insert_events(events)
        wanted = AuditFilters(action_type="user_deleted")
        found = backend.query_page(
            wanted, "all", seek=None, direction="older", limit=10
        )
        total = backend.count_capped(wanted, "all", cap=10)
    finally:
        pool.close()
    assert total == 1
    row = found[0]
    assert (row["outcome"], row["source"], row["actor_is_system"]) == (
        "success",
        "system",
        0,
    )
    assert row["correlation_id"] == events[0].correlation_id
    assert row["event_uuid"] == events[0].event_uuid
    assert "node_id" in row and "auth_method" in row and "ip_address" in row


@live
def test_groups_backend_audit_rows_go_through_the_one_write(fresh_db: str) -> None:
    from code_indexer.server.storage.postgres.connection_pool import (
        ConnectionPool,
    )
    from code_indexer.server.storage.postgres.groups_backend import (
        GroupsPostgresBackend,
    )

    _run_migrations(fresh_db)
    pool = ConnectionPool(fresh_db, min_size=1, max_size=2)
    try:
        GroupsPostgresBackend(pool).log_audit(
            admin_id="admin",
            action_type="group_create",
            target_type="group",
            target_id="7",
            details=_GROUP_DETAILS_IN,
        )
    finally:
        pool.close()
    rows = _query(
        fresh_db,
        "SELECT admin_id, action_type, details, event_uuid, correlation_id "
        "FROM audit_logs",
    )
    assert [r[:3] for r in rows] == [("admin", "group_create", _GROUP_DETAILS_STORED)]
    assert rows[0][3] and rows[0][4]


@live
def test_failed_batch_is_rolled_back_and_the_writer_keeps_good_rows(
    fresh_db: str,
) -> None:
    from code_indexer.server.services import audit_capture
    from code_indexer.server.services.audit_log_service import AuditLogService
    from code_indexer.server.storage.postgres.audit_log_backend import (
        AuditLogPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import (
        ConnectionPool,
    )

    _run_migrations(fresh_db)
    good, _ = _events()
    poison = build_legacy_event(
        actor=None,  # type: ignore[arg-type]  # violates admin_id NOT NULL
        action_type="group_create",
        target_type="group",
        target_id="8",
        details_json=None,
    )
    pool = ConnectionPool(fresh_db, min_size=1, max_size=2)
    try:
        backend = AuditLogPostgresBackend(pool)
        with pytest.raises(Exception):
            backend.insert_events([good, poison])
        assert _query(fresh_db, "SELECT COUNT(*) FROM audit_logs") == [(0,)]
        service = AuditLogService(Path("unused.db"), storage_backend=backend)
        before = audit_capture.records_dropped_since_boot()
        service._write_batch([good, poison])
        assert audit_capture.records_dropped_since_boot() == before + 1
    finally:
        pool.close()
    assert _query(fresh_db, "SELECT event_uuid FROM audit_logs") == [(good.event_uuid,)]


@live
def test_durable_capture_reaches_postgres(fresh_db: str) -> None:
    from code_indexer.server.services import audit_capture
    from code_indexer.server.services.audit_log_service import AuditLogService
    from code_indexer.server.storage.postgres.audit_log_backend import (
        AuditLogPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import (
        ConnectionPool,
    )

    _run_migrations(fresh_db)
    pool = ConnectionPool(fresh_db, min_size=1, max_size=2)
    service = AuditLogService(
        Path("unused.db"), storage_backend=AuditLogPostgresBackend(pool)
    )
    service.start()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(service, node_id="node-pg")
    try:
        audit_capture.capture(
            actor="alice",
            action_type="api_key_deleted",
            target_type="api_key",
            target_id="key-1",
            outcome="success",
            details={"key_id": "key-1"},
        )
        rows = _query(fresh_db, "SELECT action_type, node_id, outcome FROM audit_logs")
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        service.stop()
        pool.close()
    assert rows == [("api_key_deleted", "node-pg", "success")]
