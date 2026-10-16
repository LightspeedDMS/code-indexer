"""PostgreSQL mirror of
tests/unit/server/repositories/test_completed_filter_includes_partial.py
(skipped without TEST_POSTGRES_DSN)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.job_tracker import JobTracker
from tests.unit.server.repositories.test_completed_filter_includes_partial import (
    run_completed_filter_scenario,
    run_list_jobs_scenario,
)


def _run_on_postgres(
    dsn: str, tmp_path: Path, scenario: Callable[[BackgroundJobManager], None]
) -> None:
    from code_indexer.server.storage.postgres.background_jobs_backend import (
        BackgroundJobsPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    backend = BackgroundJobsPostgresBackend(ConnectionPool(dsn))
    try:
        tracker = JobTracker(
            str(tmp_path / "tracker.db"), storage_backend=backend, node_id="node-a"
        )
        bgm = BackgroundJobManager(
            storage_backend=backend, node_id="node-a", job_tracker=tracker
        )
        try:
            scenario(bgm)
        finally:
            bgm.shutdown()
    finally:
        try:
            with backend._pool.connection() as conn:
                conn.execute("DELETE FROM background_jobs")
        finally:
            backend.close()


def test_completed_filter_includes_partial_postgres(
    migrated_scratch_pg_dsn: str, tmp_path: Path
) -> None:
    _run_on_postgres(migrated_scratch_pg_dsn, tmp_path, run_completed_filter_scenario)


def test_list_jobs_completed_includes_partial_postgres(
    migrated_scratch_pg_dsn: str, tmp_path: Path
) -> None:
    """The REST listing's backend ``list_jobs`` on PostgreSQL."""
    _run_on_postgres(migrated_scratch_pg_dsn, tmp_path, run_list_jobs_scenario)
