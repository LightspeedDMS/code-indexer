"""PostgreSQL mirror of
tests/unit/server/repositories/test_partial_job_status_tracker.py: a
partial job ends ``completed_partial`` in the shared ``background_jobs``
table (skipped without TEST_POSTGRES_DSN)."""

from __future__ import annotations

from pathlib import Path

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.job_tracker import JobTracker
from tests.unit.server.repositories.test_partial_job_status_tracker import (
    run_partial_job_scenario,
)


def test_partial_result_job_ends_completed_partial_postgres(
    migrated_scratch_pg_dsn: str, tmp_path: Path
) -> None:
    from code_indexer.server.storage.postgres.background_jobs_backend import (
        BackgroundJobsPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    backend = BackgroundJobsPostgresBackend(ConnectionPool(migrated_scratch_pg_dsn))
    try:
        tracker = JobTracker(
            str(tmp_path / "tracker.db"), storage_backend=backend, node_id="node-a"
        )
        bgm = BackgroundJobManager(
            storage_backend=backend, node_id="node-a", job_tracker=tracker
        )

        def stored_status(job_id: str) -> str:
            with backend._pool.connection() as conn:
                row = conn.execute(
                    "SELECT status FROM background_jobs WHERE job_id = %s",
                    (job_id,),
                ).fetchone()
            assert row is not None
            return str(row[0])

        try:
            run_partial_job_scenario(bgm, tracker, stored_status)
        finally:
            bgm.shutdown()
    finally:
        try:
            with backend._pool.connection() as conn:
                conn.execute("DELETE FROM background_jobs")
        finally:
            backend.close()
