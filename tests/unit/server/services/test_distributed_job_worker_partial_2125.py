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

pytestmark = pytest.mark.xfail(reason="v12.103.0 snapshot: upstream API changes", strict=False)


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


#: The two doors a claimed refresh row ends through. In this fork the
#: scheduled ``global_repo_refresh`` is a POD_PULL op: any pod's
#: IndexJobClaimLoop claims it (the leader's DistributedJobWorkerService
#: excludes POD_PULL_OPS); the leader keeps ``refresh_golden_repo``.
DOORS = ("leader_worker", "pod_pull_claim_loop")


def _insert_pending(pool: Any, op_type: str) -> None:
    import json

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO background_jobs (job_id, operation_type, status, "
                "created_at, username, repo_alias, metadata) "
                "VALUES (%s, %s, 'pending', NOW(), 'system', %s, %s)",
                (JOB_ID, op_type, ALIAS, json.dumps({"alias_name": ALIAS})),
            )
        conn.commit()


def _run_claimed_refresh(
    pool: Any, result: Dict[str, Any], door: str
) -> Tuple[str, Any]:
    from code_indexer.global_repos.refresh_scheduler import (
        GLOBAL_REPO_REFRESH_OP,
        global_repo_refresh_executor,
    )
    from code_indexer.server.services.distributed_job_claimer import (
        DistributedJobClaimer,
    )
    from code_indexer.server.services.distributed_job_worker import (
        DistributedJobWorkerService,
    )
    from code_indexer.server.services.index_job_claim_loop import IndexJobClaimLoop

    claimer = DistributedJobClaimer(pool, "node-1", memory_gate_enabled=False)
    scheduler = _RefreshReturning(result)
    if door == "leader_worker":
        _insert_pending(pool, "refresh_golden_repo")
        DistributedJobWorkerService(claimer, scheduler)._process_one_job()
    else:
        _insert_pending(pool, GLOBAL_REPO_REFRESH_OP)
        IndexJobClaimLoop(
            claimer=claimer,
            dispatch={
                GLOBAL_REPO_REFRESH_OP: lambda md, cb, job_id: (
                    global_repo_refresh_executor(scheduler, md, cb)
                )
            },
            node_id="node-1",
        )._process_one_job()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, result FROM background_jobs WHERE job_id = %s",
                (JOB_ID,),
            )
            row = cur.fetchone()
    assert row is not None
    return str(row[0]), row[1]


@pytest.mark.parametrize("door", DOORS)
def test_a_claimed_partial_refresh_ends_completed_partial(pool: Any, door: str) -> None:
    status, stored = _run_claimed_refresh(
        pool,
        {
            "success": True,
            "alias": ALIAS,
            "message": "Refresh complete",
            "partial": True,
            "failed_files": 3,
        },
        door,
    )

    assert status == "completed_partial"
    assert stored["failed_files"] == 3


@pytest.mark.parametrize("door", DOORS)
def test_a_claimed_full_refresh_still_ends_completed(pool: Any, door: str) -> None:
    status, _stored = _run_claimed_refresh(
        pool, {"success": True, "alias": ALIAS, "message": "Refresh complete"}, door
    )

    assert status == "completed"
