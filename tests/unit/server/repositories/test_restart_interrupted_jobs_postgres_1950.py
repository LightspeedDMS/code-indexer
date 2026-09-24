"""
Bug #1950 Part 4: PostgreSQL backend dual-backend coverage.

Mocked-cursor unit tests (no live DB required) verify
BackgroundJobsPostgresBackend never writes status='failed' for a
restart-interrupted row -- mirroring the established mocked-pool pattern
in test_background_jobs_postgres_symmetry.py, which mocks ONLY the
psycopg connection pool (an external dependency), never the code under
test.

A live-PostgreSQL mirror is TEST_POSTGRES_DSN-gated (skips cleanly when
unavailable), matching test_null_executing_node_orphan_1512.py's
established convention exactly -- including its disposable-table
drop/create/drop lifecycle, since TEST_POSTGRES_DSN is documented across
this project's live-PG suite as pointing at a throwaway test database,
never production.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

HAS_PSYCOPG_FOR_LIVE_PG = False
try:
    import psycopg as _psycopg_check  # noqa: F401

    HAS_PSYCOPG_FOR_LIVE_PG = True
except ImportError:
    # Mirrors test_null_executing_node_orphan_1512.py's optional-dependency
    # probe: psycopg is an extra only needed for the PostgreSQL backend and
    # is legitimately absent in a pure-solo dev environment.
    pass


def _make_pg_pool(fetchall=None, rowcount=0):
    cur = MagicMock()
    cur.fetchall.return_value = fetchall if fetchall is not None else []
    cur.rowcount = rowcount

    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    pool = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    return pool, cur


def _assert_no_execute_call_writes_failed_literal(cur) -> None:
    """Scan every cur.execute(sql, params) call and fail if any SQL text
    contains a quoted 'failed' status literal, or the bare string "failed"
    was bound as a parameter (covers a hypothetical parameterized
    `status = %s` rewrite too, not just the current inline-literal form)."""
    for call_args in cur.execute.call_args_list:
        sql_text = call_args.args[0]
        params = call_args.args[1] if len(call_args.args) > 1 else ()
        assert "'failed'" not in sql_text, (
            f"Bug #1950: found a literal 'failed' status write in SQL: {sql_text!r}"
        )
        if isinstance(params, (list, tuple)):
            assert "failed" not in params, (
                f"Bug #1950: found 'failed' bound as a parameter: {params!r}"
            )


def test_cleanup_orphaned_jobs_on_startup_does_not_write_failed_status() -> None:
    from code_indexer.server.storage.postgres.background_jobs_backend import (
        BackgroundJobsPostgresBackend,
    )

    pool, cur = _make_pg_pool(fetchall=[("job-1", 12345)], rowcount=1)
    backend = BackgroundJobsPostgresBackend(pool)

    backend.cleanup_orphaned_jobs_on_startup(node_id="node-A")

    _assert_no_execute_call_writes_failed_literal(cur)


def test_fail_orphaned_jobs_does_not_write_failed_status() -> None:
    from code_indexer.server.storage.postgres.background_jobs_backend import (
        BackgroundJobsPostgresBackend,
    )

    pool, cur = _make_pg_pool(rowcount=2)
    backend = BackgroundJobsPostgresBackend(pool)

    backend.fail_orphaned_jobs(error="Orphaned by server restart")

    _assert_no_execute_call_writes_failed_literal(cur)


@pytest.fixture(scope="module")
def pg_dsn_for_1950():
    if not HAS_PSYCOPG_FOR_LIVE_PG:
        pytest.skip("psycopg not available")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    try:
        import psycopg

        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    return dsn


@pytest.fixture
def background_jobs_table_1950(pg_dsn_for_1950):
    """Real background_jobs table (migration 004 shape), dropped after."""
    import psycopg

    dsn = pg_dsn_for_1950
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS background_jobs")
        conn.execute(
            """
            CREATE TABLE background_jobs (
                job_id TEXT PRIMARY KEY NOT NULL,
                operation_type TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                result JSONB,
                error TEXT,
                progress INTEGER NOT NULL DEFAULT 0,
                username TEXT NOT NULL,
                is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                cancelled BOOLEAN NOT NULL DEFAULT FALSE,
                repo_alias TEXT,
                resolution_attempts INTEGER NOT NULL DEFAULT 0,
                claude_actions JSONB,
                failure_reason TEXT,
                extended_error JSONB,
                language_resolution_status JSONB,
                progress_info TEXT,
                metadata JSONB,
                executing_node TEXT,
                claimed_at TIMESTAMPTZ,
                current_phase TEXT,
                phase_detail TEXT,
                actor_username TEXT,
                executing_pid INTEGER
            )
            """
        )
        conn.execute(
            """CREATE UNIQUE INDEX idx_active_job_per_repo
            ON background_jobs (operation_type, repo_alias)
            WHERE status IN ('pending', 'running') AND repo_alias IS NOT NULL"""
        )
    yield dsn
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS background_jobs")


@pytest.mark.skipif(not HAS_PSYCOPG_FOR_LIVE_PG, reason="psycopg not available")
def test_live_pg_cleanup_orphaned_jobs_reclassifies_not_fails(
    background_jobs_table_1950,
) -> None:
    from code_indexer.server.storage.postgres.background_jobs_backend import (
        BackgroundJobsPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    dsn = background_jobs_table_1950
    pool = ConnectionPool(dsn, min_size=1, max_size=2)
    try:
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO background_jobs
                        (job_id, operation_type, status, created_at,
                         started_at, username, repo_alias, executing_node)
                    VALUES (%s, %s, 'running', %s, %s, %s, %s, %s)
                    """,
                    (
                        job_id,
                        "global_repo_refresh",
                        now,
                        now,
                        "system",
                        "typescript-global",
                        "node-A",
                    ),
                )
            conn.commit()

        backend = BackgroundJobsPostgresBackend(pool)
        count = backend.cleanup_orphaned_jobs_on_startup(node_id="node-A")

        assert count == 1
        job = backend.get_job(job_id)
        assert job is not None
        assert job["status"] != "failed", (
            "Bug #1950: a restart-interrupted row must not be classified "
            "'failed' on the live PostgreSQL backend."
        )
    finally:
        pool.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
