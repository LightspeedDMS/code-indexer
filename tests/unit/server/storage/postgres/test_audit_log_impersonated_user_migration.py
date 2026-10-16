"""PostgreSQL parity for the ``audit_logs.impersonated_user`` column.

Audit records written during MCP impersonation name the authenticated
administrator as the actor and the impersonated user as the subject.  The
SQLite store adds the column in ``AuditLogService._ensure_schema``; on
PostgreSQL migration 063 adds the same name and type, and the backend reads
it back through the shared read path.

Static checks always run; the live round-trip needs ``TEST_POSTGRES_DSN``
(``migrated_scratch_pg_dsn`` skips cleanly without it).
"""

from __future__ import annotations

import re
from pathlib import Path

from code_indexer.server.services.audit_log_service import (
    AUDIT_IMPERSONATION_COLUMNS,
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
_MIGRATION_NAME = "063_audit_logs_impersonated_user.sql"


def _migration_sql() -> str:
    sql = (_SQL_DIR / _MIGRATION_NAME).read_text()
    return re.sub(r"--[^\n]*", "", re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL))


def _columns(text: str) -> list:
    return [c.strip() for c in text.split(",")]


def test_migration_number_is_unique() -> None:
    prefixes = [p.name.split("_")[0] for p in _SQL_DIR.glob("*.sql")]
    assert prefixes.count("063") == 1
    assert (_SQL_DIR / _MIGRATION_NAME).exists()


def test_migration_adds_exactly_the_impersonation_column() -> None:
    added = re.findall(
        r"ALTER\s+TABLE\s+audit_logs\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
        r"(\w+)\s+([^;]+);",
        _migration_sql(),
        flags=re.IGNORECASE,
    )
    assert [(name, " ".join(ddl.split()).upper()) for name, ddl in added] == [
        (name, ddl.upper()) for name, ddl in AUDIT_IMPERSONATION_COLUMNS
    ]


def test_migration_is_additive_only() -> None:
    sql = _migration_sql().upper()
    for forbidden in ("DROP ", "RENAME", "ALTER COLUMN", " TYPE ", "NOT NULL"):
        assert forbidden not in sql, forbidden
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    assert len(statements) == len(AUDIT_IMPERSONATION_COLUMNS)


def test_backend_select_columns_match_the_sqlite_store() -> None:
    from code_indexer.server.services.audit_log_query import AUDIT_READ_COLUMNS
    from code_indexer.server.storage.postgres import audit_log_backend

    assert _columns(audit_log_backend._SELECT_COLS) == _columns(AUDIT_READ_COLUMNS)
    assert "impersonated_user" in _columns(AUDIT_READ_COLUMNS)


def test_impersonated_event_round_trips_on_postgres(
    migrated_scratch_pg_dsn: str,
) -> None:
    from code_indexer.server.middleware.audit_request_context import (
        bind_audit_request_context,
        build_request_context,
        note_mcp_principal,
        reset_audit_request_context,
    )
    from code_indexer.server.services.audit_events import build_event
    from code_indexer.server.services.audit_log_query import (
        build_filters,
        query_audit_log,
    )
    from code_indexer.server.storage.postgres.audit_log_backend import (
        AuditLogPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    token = bind_audit_request_context(build_request_context("/mcp", "127.0.0.1"))
    try:
        note_mcp_principal("example-admin", "example-subject")
        impersonated = build_event(
            actor="example-subject",
            action_type="api_key_created",
            target_type="api_key",
            target_id="example-key-1",
            outcome="success",
            details={"key_id": "example-key-1"},
        )
        note_mcp_principal("example-admin", None)
        plain = build_event(
            actor="example-admin",
            action_type="api_key_created",
            target_type="api_key",
            target_id="example-key-2",
            outcome="success",
            details={"key_id": "example-key-2"},
        )
    finally:
        reset_audit_request_context(token)

    pool = ConnectionPool(migrated_scratch_pg_dsn, min_size=1, max_size=2)
    try:
        backend = AuditLogPostgresBackend(pool)
        backend.insert_events([impersonated, plain])
        page = query_audit_log(
            backend, build_filters(action_type="api_key_created"), with_total=False
        )
    finally:
        pool.close()

    seen = sorted((r.target_id, r.admin_id, r.impersonated_user) for r in page.rows)
    assert seen == [
        ("example-key-1", "example-admin", "example-subject"),
        ("example-key-2", "example-admin", None),
    ]
