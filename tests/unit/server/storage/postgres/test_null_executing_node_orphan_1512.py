"""
Issue #1512: a live-PostgreSQL end-to-end reproduction of a NULL-
executing_node 'running' background_jobs row permanently blocking repo
refresh via idx_active_job_per_repo, and its fix.

BACKGROUND: `cleanup_orphaned_jobs_on_startup(node_id)`'s UPDATE originally
filtered `WHERE status IN ('running', 'pending') AND executing_node = %s`.
In SQL, `NULL = <anything>` is never true, so a row with
`executing_node IS NULL` can never match this filter on ANY node -- there
is no node that can ever reclaim it. Such a row then permanently occupies
the `idx_active_job_per_repo` partial unique index slot for its
(operation_type, repo_alias) pair, blocking every future
register_job_if_no_conflict() call for that repo -- observed in production
as a golden repo that could never be refreshed again (staging incident,
2026-08-01).

The fix widens the UPDATE to also reclaim `status = 'running' AND
executing_node IS NULL` rows (never `'pending'`, which is the legitimate
pod-pull work-stealing queue state).

Mirrors this project's established live-PG conventions exactly (never
inventing a new one):
  - TEST_POSTGRES_DSN-gated module-scoped connectivity fixture + per-test
    real-table fixture
    (test_golden_repo_metadata_temporal_options_live_pg_1414.py).
  - Real `background_jobs` table + real `idx_active_job_per_repo` partial
    unique index, matching migration 004
    (test_bug1235_pg_duplicate_claim_race.py's SQLite mirror of the same
    schema, test_fleet_migration_quarantine_concurrency_1477.py's live-PG
    conventions).

TEST_POSTGRES_DSN is not set in this development environment as of this
writing -- the entire class below is skip-gated and will report "skipped"
rather than "passed" here. It is designed to run for real the moment a
developer points TEST_POSTGRES_DSN at an actual PostgreSQL instance.
"""

import os
import uuid
from datetime import datetime, timezone

import pytest

HAS_PSYCOPG_FOR_LIVE_PG = False
try:
    import psycopg as _psycopg_check  # noqa: F401

    HAS_PSYCOPG_FOR_LIVE_PG = True
except ImportError:
    pass


@pytest.fixture(scope="module")
def pg_dsn_for_null_executing_node_1512():
    """Module-scoped DSN string for the live-PG NULL-executing_node test.

    Skips cleanly if unavailable (matches pg_dsn_for_temporal_options in
    test_golden_repo_metadata_temporal_options_live_pg_1414.py).
    """
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
def background_jobs_table_1512(pg_dsn_for_null_executing_node_1512):
    """Create a real background_jobs table + idx_active_job_per_repo index
    (matching migration 004's shape) before each test, dropped after."""
    import psycopg

    dsn = pg_dsn_for_null_executing_node_1512
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
                actor_username TEXT
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


def _make_backend_and_pool(dsn: str):
    from code_indexer.server.storage.postgres.background_jobs_backend import (
        BackgroundJobsPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    pool = ConnectionPool(dsn, min_size=1, max_size=2)
    return BackgroundJobsPostgresBackend(pool), pool


def _insert_stuck_running_null_node_row(pool, repo_alias: str) -> str:
    """Insert a 'running' row with executing_node=NULL, simulating the
    production incident directly (bypassing the normal insert path so the
    test does not depend on how such a row originally came to exist)."""
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO background_jobs
                    (job_id, operation_type, status, created_at,
                     started_at, username, repo_alias, executing_node)
                VALUES (%s, %s, 'running', %s, %s, %s, %s, NULL)
                """,
                (job_id, "global_repo_refresh", now, now, "system", repo_alias),
            )
        conn.commit()
    return job_id


def _insert_pending_pod_pull_null_node_row(pool, repo_alias: str) -> str:
    """Insert a 'pending' + executing_node=NULL row -- the legitimate
    pod-pull work-stealing queue state that must never be reclaimed."""
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO background_jobs
                    (job_id, operation_type, status, created_at,
                     username, repo_alias, executing_node)
                VALUES (%s, %s, 'pending', %s, %s, %s, NULL)
                """,
                (job_id, "add_golden_repo", now, "system", repo_alias),
            )
        conn.commit()
    return job_id


@pytest.mark.skipif(not HAS_PSYCOPG_FOR_LIVE_PG, reason="psycopg not available")
class TestNullExecutingNodeOrphanBug1512:
    """End-to-end reproduction + fix verification against real PostgreSQL."""

    def test_stuck_running_null_node_row_is_reclaimed_by_cleanup(
        self, background_jobs_table_1512
    ) -> None:
        """cleanup_orphaned_jobs_on_startup() must reclaim (fail) a
        'running' row with executing_node=NULL, run from a node that never
        owned it -- proving the NULL-owner row is no longer permanently
        unreachable."""
        dsn = background_jobs_table_1512
        backend, pool = _make_backend_and_pool(dsn)
        try:
            job_id = _insert_stuck_running_null_node_row(
                pool, repo_alias="langfuse-repo-1512-global"
            )

            count = backend.cleanup_orphaned_jobs_on_startup(node_id="node-B")

            assert count == 1
            job = backend.get_job(job_id)
            assert job is not None
            assert job["status"] == "failed"
        finally:
            pool.close()

    def test_repo_is_registrable_again_via_job_tracker_after_reclaim(
        self, background_jobs_table_1512
    ) -> None:
        """After cleanup reclaims the stuck NULL-owner row, JobTracker's
        register_job_if_no_conflict() -- the exact call path golden-repo
        refresh submission uses -- must succeed for the SAME
        (operation_type, repo_alias) pair without a DuplicateJobError."""
        from code_indexer.server.services.job_tracker import JobTracker

        dsn = background_jobs_table_1512
        backend, pool = _make_backend_and_pool(dsn)
        try:
            repo_alias = "langfuse-repo-1512-global"
            _insert_stuck_running_null_node_row(pool, repo_alias=repo_alias)
            backend.cleanup_orphaned_jobs_on_startup(node_id="node-B")

            tracker = JobTracker(
                db_path=":memory:", storage_backend=backend, node_id="node-B"
            )
            new_job = tracker.register_job_if_no_conflict(
                job_id=str(uuid.uuid4()),
                operation_type="global_repo_refresh",
                username="system",
                repo_alias=repo_alias,
            )

            assert new_job.status == "pending"
            persisted = backend.get_job(new_job.job_id)
            assert persisted is not None
            assert persisted["status"] == "pending"
        finally:
            pool.close()

    def test_pending_pod_pull_null_executing_node_row_is_not_touched(
        self, background_jobs_table_1512
    ) -> None:
        """A PENDING row with executing_node=NULL (the legitimate pod-pull
        work-stealing queue state) must NOT be reclaimed by startup
        cleanup -- only 'running' + NULL rows are treated as orphans."""
        dsn = background_jobs_table_1512
        backend, pool = _make_backend_and_pool(dsn)
        try:
            job_id = _insert_pending_pod_pull_null_node_row(
                pool, repo_alias="some-repo"
            )

            count = backend.cleanup_orphaned_jobs_on_startup(node_id="node-A")

            assert count == 0
            job = backend.get_job(job_id)
            assert job is not None
            assert job["status"] == "pending"
        finally:
            pool.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
