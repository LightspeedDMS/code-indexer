"""Bug #2012 Part 2, PostgreSQL mirror: the same stale-'running' scenarios as
tests/unit/server/repositories/test_stale_running_copy_2012.py, against a
real migrated PostgreSQL database (skipped without TEST_POSTGRES_DSN).

In cluster mode the in-memory copy that kept an interrupted job 'running'
was loaded from ANOTHER node's row (PostgreSQL ``list_jobs`` is not
node-scoped), e.g. by a node that restarted earlier in a rolling deploy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from tests.unit.server.repositories.test_stale_running_copy_2012 import (
    JOB_ID,
    OWNER_NODE,
    dead_pid,
    run_stale_copy_scenario,
    save_running_refresh_job,
)


@pytest.fixture
def pg_backends(migrated_scratch_pg_dsn: str) -> Iterator[tuple]:
    """Two independent backends (own pools) on one database: the owning
    node and another node."""
    from code_indexer.server.storage.postgres.background_jobs_backend import (
        BackgroundJobsPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    owner = BackgroundJobsPostgresBackend(ConnectionPool(migrated_scratch_pg_dsn))
    other = BackgroundJobsPostgresBackend(ConnectionPool(migrated_scratch_pg_dsn))
    try:
        yield owner, other
    finally:
        with owner._pool.connection() as conn:
            conn.execute("DELETE FROM background_jobs")
        owner.close()
        other.close()


def test_interrupted_job_not_reported_running_by_other_node_postgres(
    pg_backends: tuple,
) -> None:
    owner, other = pg_backends
    run_stale_copy_scenario(
        owner,
        lambda: BackgroundJobManager(storage_backend=other, node_id="node-b"),
    )


def test_running_row_left_by_failed_shutdown_save_is_swept_postgres(
    pg_backends: tuple, tmp_path: Path
) -> None:
    from code_indexer.server.services.job_tracker import JobTracker

    owner, other = pg_backends
    save_running_refresh_job(owner)
    with owner._pool.connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET executing_pid = %s WHERE job_id = %s",
            (dead_pid(), JOB_ID),
        )

    # The owning node restarts: its node-scoped startup sweep runs first.
    JobTracker(
        str(tmp_path / "tracker.db"), storage_backend=other, node_id=OWNER_NODE
    ).cleanup_orphaned_jobs_on_startup()
    restarted = BackgroundJobManager(storage_backend=other, node_id=OWNER_NODE)
    try:
        status = restarted.get_job_status(JOB_ID, "admin", is_admin=True)
        assert status is not None and status["status"] == "interrupted"
        running = restarted.list_jobs(
            username="admin", status_filter="running", is_admin=True
        )
        assert running["jobs"] == []
    finally:
        restarted.shutdown()
