"""Bug #2125 (B): a cluster-claimed refresh job whose refresh published
PARTIALLY (``partial: True`` in its result) ends ``completed_partial`` --
exactly as the BackgroundJobManager path ends it (Bug #679) -- not
``completed``.

Real DistributedJobClaimer, DistributedJobWorkerService and PostgreSQL
``background_jobs`` table (a throwaway database migrated by the real
MigrationRunner; requires ``TEST_POSTGRES_DSN``; the claimer exists only in
cluster mode, so there is no SQLite variant). The refresh WORK is a small
double at the scheduler boundary returning the result dict a real refresh
returns (as in the Bug #1839 claimed-refresh tests).
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Iterator, Optional, Tuple

import pytest

from tests.utils.golden_repo_metadata_stores import PG_DSN, _pg_dsn_for

ALIAS = "example-repo-global"
JOB_ID = "job-2125"


class _RefreshReturning:
    """The scheduler boundary: returns a fixed refresh result."""

    def __init__(self, result: Dict[str, Any]) -> None:
        self._result = result

    def execute_refresh_for_claimed_job(
        self,
        alias_name: str,
        progress_callback: Optional[Any] = None,
        job_id: Optional[str] = None,
        submitter_username: str = "system",
        job_owned: Optional[Any] = None,
    ) -> Dict[str, Any]:
        return dict(self._result)


@pytest.fixture
def pool() -> Iterator[Any]:
    if not PG_DSN:
        pytest.skip("TEST_POSTGRES_DSN not set; the claimer is PostgreSQL-only")
    import psycopg

    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    name = f"bug2125_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    created: Any = None
    try:
        with MigrationRunner(_pg_dsn_for(name)) as runner:
            runner.run()
        created = ConnectionPool(_pg_dsn_for(name), min_size=1, max_size=2)
        yield created
    finally:
        if created is not None:
            created.close()
        with psycopg.connect(PG_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _run_claimed_refresh(pool: Any, result: Dict[str, Any]) -> Tuple[str, Any]:
    from code_indexer.server.services.distributed_job_claimer import (
        DistributedJobClaimer,
    )
    from code_indexer.server.services.distributed_job_worker import (
        DistributedJobWorkerService,
    )

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO background_jobs (job_id, operation_type, status, "
                "created_at, username, repo_alias) "
                "VALUES (%s, 'global_repo_refresh', 'pending', NOW(), 'system', %s)",
                (JOB_ID, ALIAS),
            )
        conn.commit()
    claimer = DistributedJobClaimer(pool, "node-1", memory_gate_enabled=False)
    DistributedJobWorkerService(claimer, _RefreshReturning(result))._process_one_job()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, result FROM background_jobs WHERE job_id = %s",
                (JOB_ID,),
            )
            row = cur.fetchone()
    assert row is not None
    return str(row[0]), row[1]


def test_a_claimed_partial_refresh_ends_completed_partial(pool: Any) -> None:
    status, stored = _run_claimed_refresh(
        pool,
        {
            "success": True,
            "alias": ALIAS,
            "message": "Refresh complete",
            "partial": True,
            "failed_files": 3,
        },
    )

    assert status == "completed_partial"
    assert stored["failed_files"] == 3


def test_a_claimed_full_refresh_still_ends_completed(pool: Any) -> None:
    status, _stored = _run_claimed_refresh(
        pool, {"success": True, "alias": ALIAS, "message": "Refresh complete"}
    )

    assert status == "completed"
