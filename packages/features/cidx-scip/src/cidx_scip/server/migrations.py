"""The ``scip`` feature's schema migrations (``migrations()``).

Version 1 is the dependency-installation audit table
(``scip_dependency_installations``) the self-healing build writes. Before the
feature owned it the core created it: in PostgreSQL by its migration
``025_runtime_only_tables.sql`` (a migration of several runtime tables, so
the core keeps shipping it), on a solo server in a separate SQLite file next
to the server data directory (``<server dir>/scip_audit.db``). The core runs
a feature's migrations while the app is built, after its own schema,
recording the newest applied version in ``feature_schema_versions``.

Backward compatible, as every migration (rolling restarts share a schema):
``CREATE ... IF NOT EXISTS`` and row copies only. Removing the feature leaves
the table.

=====================================  ========================================
Database                               What version 1 does
=====================================  ========================================
PostgreSQL, core 025 applied           no DDL; the runner records version 1
                                       (imports the state)
PostgreSQL, 025 not applied            creates the table and indexes (025's
                                       SQL)
SQLite (solo)                          creates the table in the server
                                       database and copies the rows of the
                                       old ``scip_audit.db`` when there is one
                                       (the file is left in place)
=====================================  ========================================
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from cidx_sdk import Migration

logger = logging.getLogger(__name__)

#: The core migration that creates the table in PostgreSQL.
CORE_MIGRATION_025 = "025_runtime_only_tables.sql"

#: The solo server's audit file before the feature owned the table, a sibling
#: of the server data directory that holds the server database.
LEGACY_SQLITE_FILENAME = "scip_audit.db"

#: The columns a legacy file may hold (it predates ``node_id`` when the old
#: repository created it).
_COLUMNS = (
    "timestamp",
    "job_id",
    "repo_alias",
    "project_path",
    "project_language",
    "project_build_system",
    "package",
    "command",
    "reasoning",
    "username",
    "node_id",
)

_PG_DDL = (
    """
    CREATE TABLE IF NOT EXISTS scip_dependency_installations (
        id SERIAL PRIMARY KEY,
        timestamp TIMESTAMPTZ DEFAULT NOW(),
        job_id VARCHAR(36) NOT NULL,
        repo_alias VARCHAR(255) NOT NULL,
        project_path VARCHAR(255),
        project_language VARCHAR(50),
        project_build_system VARCHAR(50),
        package VARCHAR(255) NOT NULL,
        command TEXT NOT NULL,
        reasoning TEXT,
        username VARCHAR(255),
        node_id VARCHAR(255)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_scip_audit_pg_timestamp "
    "ON scip_dependency_installations (timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_scip_audit_pg_repo_alias "
    "ON scip_dependency_installations (repo_alias)",
    "CREATE INDEX IF NOT EXISTS idx_scip_audit_pg_job_id "
    "ON scip_dependency_installations (job_id)",
    "CREATE INDEX IF NOT EXISTS idx_scip_audit_pg_project_language "
    "ON scip_dependency_installations (project_language)",
)

# Index names are database-wide in SQLite; the server database holds many
# tables, so these carry the table's prefix.
_SQLITE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS scip_dependency_installations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        job_id VARCHAR(36) NOT NULL,
        repo_alias VARCHAR(255) NOT NULL,
        project_path VARCHAR(255),
        project_language VARCHAR(50),
        project_build_system VARCHAR(50),
        package VARCHAR(255) NOT NULL,
        command TEXT NOT NULL,
        reasoning TEXT,
        username VARCHAR(255),
        node_id VARCHAR(255)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_scip_audit_timestamp "
    "ON scip_dependency_installations (timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_scip_audit_repo_alias "
    "ON scip_dependency_installations (repo_alias)",
    "CREATE INDEX IF NOT EXISTS idx_scip_audit_job_id "
    "ON scip_dependency_installations (job_id)",
    "CREATE INDEX IF NOT EXISTS idx_scip_audit_project_language "
    "ON scip_dependency_installations (project_language)",
)

_LEDGER_EXISTS_PG = "SELECT to_regclass('schema_migrations') IS NOT NULL"
_CORE_025_APPLIED_PG = "SELECT 1 FROM schema_migrations WHERE filename = %s"


def create_sqlite_audit_table(connection: sqlite3.Connection) -> None:
    """The audit table and its indexes in *connection*'s database."""
    for statement in _SQLITE_DDL:
        connection.execute(statement)


def core_migration_025_applied(connection: Any) -> bool:
    """Whether the core's migration runner applied 025 on this PostgreSQL
    database (its ``schema_migrations`` ledger has the file)."""
    with connection.cursor() as cur:
        cur.execute(_LEDGER_EXISTS_PG)
        row = cur.fetchone()
        if not row or not row[0]:
            return False
        cur.execute(_CORE_025_APPLIED_PG, (CORE_MIGRATION_025,))
        return cur.fetchone() is not None


def legacy_sqlite_path(connection: sqlite3.Connection) -> Optional[Path]:
    """Where a solo server kept the audit before: ``scip_audit.db`` beside
    the data directory holding *connection*'s database file. None for an
    in-memory database."""
    for _seq, name, filename in connection.execute("PRAGMA database_list"):
        if name == "main" and filename:
            return Path(filename).parent.parent / LEGACY_SQLITE_FILENAME
    return None


def _legacy_rows(legacy: Path) -> List[Dict[str, Any]]:
    """The old file's audit rows (oldest first), the columns it has only.
    Read through its own connection: the migration runs inside a
    transaction, where SQLite refuses ``ATTACH``."""
    source = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True, timeout=30.0)
    try:
        source.row_factory = sqlite3.Row
        present = {
            row[1]
            for row in source.execute(
                "PRAGMA table_info(scip_dependency_installations)"
            )
        }
        columns = [column for column in _COLUMNS if column in present]
        if "job_id" not in columns:
            return []
        names = ", ".join(columns)
        return [
            dict(row)
            for row in source.execute(
                f"SELECT {names} FROM scip_dependency_installations ORDER BY id"
            )
        ]
    finally:
        source.close()


def _import_legacy_rows(connection: sqlite3.Connection) -> None:
    """Copy the old audit file's rows into the new table, once: only when
    the new table is still empty (the migration is one transaction, so a
    failed import leaves nothing behind to block the retry)."""
    legacy = legacy_sqlite_path(connection)
    if legacy is None or not legacy.is_file():
        return
    (count,) = connection.execute(
        "SELECT COUNT(*) FROM scip_dependency_installations"
    ).fetchone()
    if count:
        return
    rows = _legacy_rows(legacy)
    for row in rows:
        names = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        connection.execute(
            f"INSERT INTO scip_dependency_installations ({names}) VALUES ({marks})",
            tuple(row.values()),
        )
    logger.info(
        "scip feature migration 1: imported %d audit rows from %s", len(rows), legacy
    )


def _create_dependency_installations(connection: Any) -> None:
    if isinstance(connection, sqlite3.Connection):
        create_sqlite_audit_table(connection)
        _import_legacy_rows(connection)
        return
    if core_migration_025_applied(connection):
        # Already created by the core's migration 025: import that state (the
        # runner records version 1) and run nothing.
        return
    with connection.cursor() as cur:
        for statement in _PG_DDL:
            cur.execute(statement)


SCIP_MIGRATIONS: Sequence[Migration] = (
    Migration(
        version=1,
        description=(
            "SCIP dependency-installation audit (formerly core migration 025's "
            "table and the solo scip_audit.db)"
        ),
        apply=_create_dependency_installations,
    ),
)
