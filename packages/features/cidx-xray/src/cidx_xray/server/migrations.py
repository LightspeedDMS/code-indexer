"""The ``xray`` feature's schema migrations (``migrations()``).

Version 1 is the graph-build K-calibration table, which was the core's
PostgreSQL migration ``051_xray_graph_k_calibration_samples.sql`` before the
feature owned it. The core runs a feature's migrations while the app is built,
after its own schema, recording the newest applied version in
``feature_schema_versions`` (one row per feature).

Backward compatible, as every migration (rolling restarts share a schema):
``CREATE ... IF NOT EXISTS`` only. Removing the feature leaves the table.

A deployment that already applied core migration 051 is not migrated again:
version 1 sees 051 in the core's ``schema_migrations`` ledger, executes no
DDL, and the runner records version 1 -- the ledger entry is imported into
the feature's version row. A fresh PostgreSQL database (051 never applied,
because the core no longer ships it) gets the table from version 1. SQLite
(solo) never had 051; version 1 creates the SQLite form of the table in the
server database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Sequence

from cidx_sdk import Migration

#: The core migration this feature's version 1 replaces.
CORE_MIGRATION_051 = "051_xray_graph_k_calibration_samples.sql"

#: The PostgreSQL DDL (the former core migration 051, unchanged).
_PG_SQL_FILE = (
    Path(__file__).resolve().parent / "graph_governor" / "k_calibration_samples.sql"
)

_SQLITE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS xray_graph_k_calibration_samples (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        language         TEXT    NOT NULL,
        source_bytes     INTEGER NOT NULL,
        decls            INTEGER NOT NULL,
        call_sites       INTEGER NOT NULL,
        candidate_edges  INTEGER NOT NULL,
        actual_peak_rss  INTEGER NOT NULL,
        recorded_at      REAL    NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_xray_k_calibration_language
    ON xray_graph_k_calibration_samples (language)
    """,
)

_LEDGER_EXISTS_PG = "SELECT to_regclass('schema_migrations') IS NOT NULL"
_CORE_051_APPLIED_PG = "SELECT 1 FROM schema_migrations WHERE filename = %s"


def core_migration_051_applied(connection: Any) -> bool:
    """Whether the core's migration runner already applied 051 on this
    PostgreSQL database (its ``schema_migrations`` ledger has the file)."""
    with connection.cursor() as cur:
        cur.execute(_LEDGER_EXISTS_PG)
        row = cur.fetchone()
        if not row or not row[0]:
            return False
        cur.execute(_CORE_051_APPLIED_PG, (CORE_MIGRATION_051,))
        return cur.fetchone() is not None


def _create_k_calibration_samples(connection: Any) -> None:
    if isinstance(connection, sqlite3.Connection):
        for statement in _SQLITE_DDL:
            connection.execute(statement)
        return
    if core_migration_051_applied(connection):
        # Already created by the core's migration 051: import that state (the
        # runner records version 1) and run nothing.
        return
    with connection.cursor() as cur:
        cur.execute(_PG_SQL_FILE.read_text(encoding="utf-8"))


XRAY_MIGRATIONS: Sequence[Migration] = (
    Migration(
        version=1,
        description="graph-build K-calibration samples (formerly core migration 051)",
        apply=_create_k_calibration_samples,
    ),
)
