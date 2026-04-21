"""
Tests for Story #876 Phase C: SQLite partial unique index on background_jobs.

PostgreSQL migration 004 (004_active_job_unique_constraint.sql) installs:

    CREATE UNIQUE INDEX IF NOT EXISTS idx_active_job_per_repo
        ON background_jobs (operation_type, repo_alias)
        WHERE status IN ('pending', 'running')
          AND repo_alias IS NOT NULL;

This SQLite mirror is the cluster-atomic gate that `register_job_if_no_conflict`
relies on: two nodes must NOT be able to register the same
(operation_type, repo_alias) active job simultaneously.  SQLite supports
partial indexes with `CREATE UNIQUE INDEX ... WHERE ...`, so the guarantee
is identical to PostgreSQL.

Coverage:
  1. Schema provisioning      — index exists with correct shape (unique + predicate)
  2. Uniqueness enforcement   — duplicate active jobs rejected
  3. Completed-job exemption  — partial predicate allows historical duplicates
  4. NULL repo_alias exemption — system-wide jobs coexist
  5. Idempotence              — re-running initialize_database is a no-op

Backward-compatible additive change — the index is created unconditionally via
`CREATE UNIQUE INDEX IF NOT EXISTS`, making it a safe no-op on pre-existing
databases and on every subsequent initialize_database() run.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


def _init_schema(tmp_path: Path, filename: str = "cidx_server.db") -> str:
    """Initialize a fresh SQLite DB via DatabaseSchema and return its path."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = tmp_path / filename
    schema = DatabaseSchema(db_path=str(db_path))
    schema.initialize_database()
    return str(db_path)


def _fetch_index_rows(db_path: str, index_name: str) -> list[tuple]:
    """Return rows from sqlite_master describing the given index, if any."""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchall()
    finally:
        conn.close()


def _insert_background_job(
    conn: sqlite3.Connection,
    job_id: str,
    operation_type: str,
    status: str,
    created_at: str,
    repo_alias: Optional[str],
    username: str = "admin",
) -> None:
    """Insert one background_jobs row with the minimum NOT NULL columns.

    Centralises the INSERT so each test focuses on the behaviour it exercises
    (uniqueness, exemption, etc.) rather than on SQL scaffolding.
    """
    conn.execute(
        """
        INSERT INTO background_jobs
            (job_id, operation_type, status, created_at, username, repo_alias)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (job_id, operation_type, status, created_at, username, repo_alias),
    )


def test_initialize_database_creates_active_job_partial_unique_index(
    tmp_path: Path,
) -> None:
    """Fresh DB must have idx_active_job_per_repo as a partial unique index.

    The partial predicate (status IN ('pending','running') AND repo_alias IS
    NOT NULL) is load-bearing: it limits the uniqueness guarantee to ACTIVE
    jobs so completed/failed jobs can safely have duplicates across refreshes.
    """
    db_path = _init_schema(tmp_path, "active_idx_shape.db")

    rows = _fetch_index_rows(db_path, "idx_active_job_per_repo")
    assert len(rows) == 1, (
        "DatabaseSchema.initialize_database must create idx_active_job_per_repo "
        "to mirror PostgreSQL migration 004 (Story #876 Phase C). "
        f"Got {len(rows)} matching index rows."
    )

    name, tbl_name, sql = rows[0]
    assert name == "idx_active_job_per_repo"
    assert tbl_name == "background_jobs", (
        f"Index must target background_jobs; got tbl_name={tbl_name!r}"
    )
    assert sql is not None, "sqlite_master.sql must not be NULL for this index"

    sql_upper = sql.upper()
    assert "UNIQUE" in sql_upper, (
        "Index must be UNIQUE so two active jobs for the same "
        "(operation_type, repo_alias) cannot coexist. "
        f"Got SQL: {sql}"
    )
    assert "OPERATION_TYPE" in sql_upper and "REPO_ALIAS" in sql_upper, (
        f"Index must cover both operation_type and repo_alias columns. Got SQL: {sql}"
    )
    assert "WHERE" in sql_upper, (
        "Index must be a PARTIAL index (contain a WHERE clause) so uniqueness "
        f"applies only to active jobs. Got SQL: {sql}"
    )
    assert "'PENDING'" in sql_upper and "'RUNNING'" in sql_upper, (
        "Partial predicate must include status IN ('pending','running'). "
        f"Got SQL: {sql}"
    )
    assert "REPO_ALIAS IS NOT NULL" in sql_upper, (
        "Partial predicate must include repo_alias IS NOT NULL so system-wide "
        f"jobs (repo_alias IS NULL) are not constrained. Got SQL: {sql}"
    )


def test_active_job_unique_index_blocks_duplicate_active_jobs(
    tmp_path: Path,
) -> None:
    """Inserting a second active job for the same (op, alias) must fail."""
    db_path = _init_schema(tmp_path, "active_idx_blocks.db")

    conn = sqlite3.connect(db_path)
    try:
        _insert_background_job(
            conn,
            job_id="job-1",
            operation_type="refresh_golden_repo",
            status="running",
            created_at="2026-04-20T00:00:00Z",
            repo_alias="my-repo",
        )
        conn.commit()

        try:
            _insert_background_job(
                conn,
                job_id="job-2",
                operation_type="refresh_golden_repo",
                status="pending",
                created_at="2026-04-20T00:00:01Z",
                repo_alias="my-repo",
            )
            conn.commit()
            raised = False
        except sqlite3.IntegrityError:
            conn.rollback()
            raised = True

        assert raised, (
            "Inserting a second pending/running job with the same "
            "(operation_type, repo_alias) must raise IntegrityError. "
            "The partial unique index is missing or has wrong predicate."
        )
    finally:
        conn.close()


def test_active_job_unique_index_allows_completed_duplicates(
    tmp_path: Path,
) -> None:
    """Completed jobs for the same (op, alias) must NOT collide.

    Historical runs pile up over time: the same repo is refreshed many times.
    The partial predicate excludes non-active jobs so completed/failed rows
    can coexist freely.
    """
    db_path = _init_schema(tmp_path, "active_idx_completed.db")

    conn = sqlite3.connect(db_path)
    try:
        for idx in range(3):
            _insert_background_job(
                conn,
                job_id=f"job-done-{idx}",
                operation_type="refresh_golden_repo",
                status="completed",
                created_at=f"2026-04-20T00:00:0{idx}Z",
                repo_alias="my-repo",
            )
        conn.commit()

        count = conn.execute(
            "SELECT COUNT(*) FROM background_jobs "
            "WHERE operation_type = ? AND repo_alias = ? AND status = ?",
            ("refresh_golden_repo", "my-repo", "completed"),
        ).fetchone()[0]
        assert count == 3, (
            "Completed jobs with the same (operation_type, repo_alias) must be "
            f"allowed to coexist. Got count={count}."
        )
    finally:
        conn.close()


def test_active_job_unique_index_allows_null_repo_alias_duplicates(
    tmp_path: Path,
) -> None:
    """System-wide jobs (repo_alias IS NULL) must not be uniqueness-constrained.

    Some operations (dependency_map_refresh, full-fleet scans) run without a
    specific repo alias. Multiple such active jobs must remain allowed because
    the partial predicate filters them out via `repo_alias IS NOT NULL`.
    """
    db_path = _init_schema(tmp_path, "active_idx_null.db")

    conn = sqlite3.connect(db_path)
    try:
        for idx in range(2):
            _insert_background_job(
                conn,
                job_id=f"sysjob-{idx}",
                operation_type="dependency_map_refresh",
                status="running",
                created_at=f"2026-04-20T00:00:0{idx}Z",
                repo_alias=None,
            )
        conn.commit()

        count = conn.execute(
            "SELECT COUNT(*) FROM background_jobs "
            "WHERE operation_type = ? AND repo_alias IS NULL AND status = ?",
            ("dependency_map_refresh", "running"),
        ).fetchone()[0]
        assert count == 2, (
            "Active jobs with repo_alias IS NULL must coexist — the partial "
            f"predicate excludes them. Got count={count}."
        )
    finally:
        conn.close()


def test_initialize_database_is_idempotent_for_active_job_index(
    tmp_path: Path,
) -> None:
    """Running initialize_database twice must not fail on the partial index."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = tmp_path / "idempotent_active_idx.db"
    schema = DatabaseSchema(db_path=str(db_path))
    schema.initialize_database()
    # Second call — must be a no-op.  Would raise if the CREATE lacked IF NOT EXISTS.
    schema.initialize_database()

    rows = _fetch_index_rows(str(db_path), "idx_active_job_per_repo")
    assert len(rows) == 1, (
        "idx_active_job_per_repo must exist exactly once after re-initialization; "
        f"got {len(rows)} rows."
    )
