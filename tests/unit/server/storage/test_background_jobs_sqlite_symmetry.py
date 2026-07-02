"""
Tests for Story #876 symmetry gap: SQLite background_jobs must have
``executing_node`` and ``claimed_at`` columns + index to match PostgreSQL.

PostgreSQL baseline (already present, never modified here):
  - migrations/sql/001_initial_schema.sql lines 150-151: executing_node TEXT,
    claimed_at TIMESTAMPTZ on background_jobs
  - migrations/sql/005_executing_node_index.sql: CREATE INDEX on executing_node

SQLite must carry identical schema via:
  1. Updated CREATE_BACKGROUND_JOBS_TABLE DDL (for fresh installs)
  2. _migrate_add_executing_node_claimed_at() (for existing DBs, idempotent)
  3. idx_background_jobs_executing_node index (mirrors PG migration 005)
  4. BackgroundJobsSqliteBackend.save_job / get_job round-trip for both columns

Coverage (5 tests):
  A. Fresh-install DDL — PRAGMA table_info shows both executing_node and claimed_at
  B. Migration idempotency — adds both columns to pre-existing DB, re-run is no-op
  C. Index presence — PRAGMA index_list contains idx_background_jobs_executing_node
  D. Writer round-trip — insert row with both fields set, read back, values survive
  E. Backward-compat null — omitting both fields in save_job stores NULL; get_job
     returns the keys with value None (existing callers unaffected)

Backward-compat invariants (all respected by the implementation):
  - Both columns are nullable with no DEFAULT — old rows remain valid
  - ALTER TABLE ADD COLUMN only — no DROP/RENAME/TYPE change
  - Migration is idempotent (safe for rolling cluster upgrades)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_schema(tmp_path: Path, filename: str = "cidx_server.db") -> str:
    """Initialise a fresh SQLite DB via DatabaseSchema and return its path."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = tmp_path / filename
    schema = DatabaseSchema(db_path=str(db_path))
    schema.initialize_database()
    return str(db_path)


def _table_columns(db_path: str, table: str) -> set[str]:
    """Return the set of column names present in *table* via PRAGMA table_info."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cursor.fetchall()}
    finally:
        conn.close()


def _index_names(db_path: str, table: str) -> set[str]:
    """Return the set of index names on *table* via PRAGMA index_list."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(f"PRAGMA index_list({table})")
        return {row[1] for row in cursor.fetchall()}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test A — Fresh DDL includes executing_node and claimed_at
# ---------------------------------------------------------------------------


def test_fresh_ddl_includes_executing_node_and_claimed_at(tmp_path: Path) -> None:
    """CREATE_BACKGROUND_JOBS_TABLE DDL must provision both new columns.

    PostgreSQL migration 001 has executing_node and claimed_at.  A fresh
    SQLite install must match so cluster nodes sharing the same schema never
    encounter a missing column regardless of which backend they use.
    """
    db_path = _init_schema(tmp_path, "fresh_schema.db")

    cols = _table_columns(db_path, "background_jobs")
    assert "executing_node" in cols, (
        "Fresh SQLite background_jobs table must include 'executing_node' column "
        "to match PostgreSQL migration 001 schema (Story #876)."
    )
    assert "claimed_at" in cols, (
        "Fresh SQLite background_jobs table must include 'claimed_at' column "
        "to match PostgreSQL migration 001 schema (Story #876)."
    )


# ---------------------------------------------------------------------------
# Test B — Migration idempotency
# ---------------------------------------------------------------------------


def test_migration_adds_columns_to_existing_db_and_reruns_safely(
    tmp_path: Path,
) -> None:
    """_migrate_add_executing_node_claimed_at must:
    1. Add both columns to a pre-existing DB that lacks them.
    2. Be safely re-runnable (idempotent) on an already-migrated DB.

    Simulates a pre-existing database (created before this migration existed)
    by building the table without the two new columns, then running the
    migration once to add them, then running it again to verify idempotence.
    """
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = tmp_path / "migrate_idempotent.db"

    # Build a minimal legacy schema WITHOUT executing_node / claimed_at.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE background_jobs (
                job_id TEXT PRIMARY KEY NOT NULL,
                operation_type TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                username TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                is_admin INTEGER NOT NULL DEFAULT 0,
                cancelled INTEGER NOT NULL DEFAULT 0,
                resolution_attempts INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    # Verify the two columns are absent before migration (pre-condition).
    pre_cols = _table_columns(str(db_path), "background_jobs")
    assert "executing_node" not in pre_cols, "Pre-condition: column must be absent"
    assert "claimed_at" not in pre_cols, "Pre-condition: column must be absent"

    schema = DatabaseSchema(db_path=str(db_path))

    # First run — must add both columns.
    conn2 = sqlite3.connect(str(db_path))
    try:
        schema._migrate_add_executing_node_claimed_at(conn2)
        conn2.commit()
    finally:
        conn2.close()

    post_cols = _table_columns(str(db_path), "background_jobs")
    assert "executing_node" in post_cols, (
        "_migrate_add_executing_node_claimed_at must add 'executing_node' "
        "column to pre-existing databases (Story #876)."
    )
    assert "claimed_at" in post_cols, (
        "_migrate_add_executing_node_claimed_at must add 'claimed_at' "
        "column to pre-existing databases (Story #876)."
    )

    # Second run — must not raise and must leave exactly one copy of each column.
    conn3 = sqlite3.connect(str(db_path))
    try:
        schema._migrate_add_executing_node_claimed_at(conn3)
        conn3.commit()
    finally:
        conn3.close()

    final_cols = _table_columns(str(db_path), "background_jobs")
    assert "executing_node" in final_cols
    assert "claimed_at" in final_cols


# ---------------------------------------------------------------------------
# Test C — Index presence
# ---------------------------------------------------------------------------


def test_initialize_database_creates_executing_node_index(tmp_path: Path) -> None:
    """initialize_database must create idx_background_jobs_executing_node.

    PostgreSQL migration 005 (005_executing_node_index.sql) creates this index.
    The SQLite mirror ensures query plans on executing_node are efficient for
    both backends (Story #876 symmetry).
    """
    db_path = _init_schema(tmp_path, "executing_node_index.db")

    indexes = _index_names(db_path, "background_jobs")
    assert "idx_background_jobs_executing_node" in indexes, (
        "initialize_database must create idx_background_jobs_executing_node "
        "to mirror PostgreSQL migration 005 (Story #876). "
        f"Present indexes: {sorted(indexes)}"
    )


# ---------------------------------------------------------------------------
# Test D — Writer round-trip with explicit values
# ---------------------------------------------------------------------------


def test_save_job_and_get_job_round_trip_executing_node_and_claimed_at(
    tmp_path: Path,
) -> None:
    """save_job with executing_node + claimed_at survives a get_job round-trip.

    BackgroundJobsSqliteBackend must include these two fields in both the
    INSERT (save_job) and SELECT (get_job) so stored values can be read back
    without loss.  This is the behavioural parity test — not just schema, but
    actual data flow.
    """
    from code_indexer.server.storage.sqlite_backends import (
        BackgroundJobsSqliteBackend,
    )

    db_path = _init_schema(tmp_path, "round_trip.db")
    backend = BackgroundJobsSqliteBackend(db_path)

    job_id = "test-job-symmetry-001"
    executing_node_value = "node-1"
    claimed_at_value = "2026-04-20T12:00:00Z"

    backend.save_job(
        job_id=job_id,
        operation_type="refresh_golden_repo",
        status="running",
        created_at="2026-04-20T11:59:00Z",
        username="admin",
        progress=0,
        executing_node=executing_node_value,
        claimed_at=claimed_at_value,
    )

    job = backend.get_job(job_id)
    assert job is not None, f"get_job must return the saved row for job_id={job_id!r}"
    assert job.get("executing_node") == executing_node_value, (
        f"executing_node must round-trip through save_job/get_job. "
        f"Expected {executing_node_value!r}, got {job.get('executing_node')!r}."
    )
    assert job.get("claimed_at") == claimed_at_value, (
        f"claimed_at must round-trip through save_job/get_job. "
        f"Expected {claimed_at_value!r}, got {job.get('claimed_at')!r}."
    )


# ---------------------------------------------------------------------------
# Test E — Backward-compat: omitting fields stores NULL
# ---------------------------------------------------------------------------


def test_save_job_without_new_fields_stores_null_and_get_job_returns_keys(
    tmp_path: Path,
) -> None:
    """save_job without executing_node/claimed_at stores NULL; get_job returns
    the keys with value None so existing callers are not broken.

    Existing callers that never set these fields must continue to work.
    The returned dict must contain the keys with value None (not absent /
    KeyError) to avoid AttributeError at call sites that do job.get(key).
    """
    from code_indexer.server.storage.sqlite_backends import (
        BackgroundJobsSqliteBackend,
    )

    db_path = _init_schema(tmp_path, "null_defaults.db")
    backend = BackgroundJobsSqliteBackend(db_path)

    job_id = "test-job-null-defaults"
    backend.save_job(
        job_id=job_id,
        operation_type="dependency_map_refresh",
        status="pending",
        created_at="2026-04-20T11:59:00Z",
        username="admin",
        progress=0,
    )

    job = backend.get_job(job_id)
    assert job is not None

    assert "executing_node" in job, (
        "get_job result dict must always include 'executing_node' key "
        "(Story #876 symmetry — None when not set)."
    )
    assert job["executing_node"] is None, (
        f"executing_node must be None when not supplied to save_job, "
        f"got {job['executing_node']!r}."
    )
    assert "claimed_at" in job, (
        "get_job result dict must always include 'claimed_at' key "
        "(Story #876 symmetry — None when not set)."
    )
    assert job["claimed_at"] is None, (
        f"claimed_at must be None when not supplied to save_job, "
        f"got {job['claimed_at']!r}."
    )


# ---------------------------------------------------------------------------
# Test F — Index predicate: WHERE executing_node IS NOT NULL (Nit #1)
# ---------------------------------------------------------------------------


def test_executing_node_index_uses_partial_predicate(tmp_path: Path) -> None:
    """idx_background_jobs_executing_node must be a partial index with
    WHERE executing_node IS NOT NULL, mirroring PostgreSQL migration 005.

    SQLite has supported partial indexes since 3.8.0 (2013).  A full index
    would diverge from the PG definition and create an asymmetry in storage
    size and query-plan behaviour for NULL-heavy columns.
    """
    db_path = _init_schema(tmp_path, "index_predicate.db")

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_background_jobs_executing_node'"
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    assert row is not None, (
        "idx_background_jobs_executing_node must exist in sqlite_master "
        "(Nit #1 — partial-index predicate symmetry with PG migration 005)."
    )
    normalized_sql = row[0].upper().replace("\n", " ").replace("  ", " ")
    assert "WHERE EXECUTING_NODE IS NOT NULL" in normalized_sql, (
        "idx_background_jobs_executing_node must include "
        "WHERE executing_node IS NOT NULL to match PG migration 005. "
        f"Actual DDL: {row[0]!r}"
    )


# ---------------------------------------------------------------------------
# Test G — Legacy migration path creates the index (Nit #4)
# ---------------------------------------------------------------------------


def test_migration_creates_executing_node_index_on_legacy_db(
    tmp_path: Path,
) -> None:
    """_migrate_add_executing_node_claimed_at must create
    idx_background_jobs_executing_node on a DB that pre-existed before the
    migration was introduced.

    Fresh-install path (Test C) proves the index is created by
    initialize_database.  This test proves the LEGACY path — a DB that was
    already live before Story #876 — also gets the index after running the
    migration function directly, without going through initialize_database.
    """
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = tmp_path / "legacy_index_migration.db"

    # Build a minimal legacy schema: background_jobs table WITHOUT the new
    # columns and WITHOUT the index.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE background_jobs (
                job_id TEXT PRIMARY KEY NOT NULL,
                operation_type TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                username TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                is_admin INTEGER NOT NULL DEFAULT 0,
                cancelled INTEGER NOT NULL DEFAULT 0,
                resolution_attempts INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    # Pre-condition: index must not exist yet.
    pre_indexes = _index_names(str(db_path), "background_jobs")
    assert "idx_background_jobs_executing_node" not in pre_indexes, (
        "Pre-condition: idx_background_jobs_executing_node must be absent "
        "from the legacy DB before migration runs."
    )

    schema = DatabaseSchema(db_path=str(db_path))
    conn2 = sqlite3.connect(str(db_path))
    try:
        schema._migrate_add_executing_node_claimed_at(conn2)
        conn2.commit()
    finally:
        conn2.close()

    post_indexes = _index_names(str(db_path), "background_jobs")
    assert "idx_background_jobs_executing_node" in post_indexes, (
        "_migrate_add_executing_node_claimed_at must create "
        "idx_background_jobs_executing_node on legacy DBs (Nit #4). "
        f"Present indexes after migration: {sorted(post_indexes)}"
    )
