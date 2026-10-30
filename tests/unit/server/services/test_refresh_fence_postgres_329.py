"""Issue #329 D1 on real PostgreSQL: the claim generation fences a claimed
refresh, the alias-lease probe fences on a takeover, and a
BackgroundJobManager job reassigned by the reconciler is NOT fenced (its
rows carry no claim generation until run start stamps one). Requires
TEST_POSTGRES_DSN (a throwaway, migrated database per test); the cluster
claim path exists only in PostgreSQL mode."""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Iterator, List

import pytest

from tests.utils.golden_repo_metadata_stores import PG_DSN, _pg_dsn_for
from tests.unit.global_repos.test_refresh_scheduler_cancel_2012 import (
    ALIAS,
    CANCEL_BOUND_SECONDS,
    _wait_for,
    _wait_until_gone,
    alias_target,
    install_fake_cidx,
    make_local_repo_scheduler,
)
from tests.unit.global_repos import test_refresh_scheduler_cancel_2012 as _cancel_2012

# ^ external pytest registers a fixture under the module attribute name; an alias import would rename it
pid_file = _cancel_2012.pid_file

JOB_ID = "job-329"
NODE = "node-1"
REPO = "example-repo"
CHILD_START_BOUND_SECONDS = 30
GROUP_GONE_BOUND_SECONDS = 5.0
ROW_STATE_BOUND_SECONDS = 10
JOIN_GRACE_SECONDS = 1
# ^ invariant CLAIM_CLOCK_STEP_SECONDS > 0 between claims; equal NOW() => same claim generation
CLAIM_CLOCK_STEP_SECONDS = 0.01
WORK_POLL_SECONDS = 0.1
POLLS_AFTER_REASSIGNMENT = 10
REFRESH_LEASE_SECONDS = 86400
SHORT_RENEW_SECONDS = 3600


@pytest.fixture
def pg_dsn() -> Iterator[str]:
    if not PG_DSN:
        pytest.skip("TEST_POSTGRES_DSN not set; the claim path is PostgreSQL-only")
    import psycopg

    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    name = f"issue329_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        with MigrationRunner(_pg_dsn_for(name)) as runner:
            runner.run()
        yield _pg_dsn_for(name)
    finally:
        with psycopg.connect(PG_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def pool(pg_dsn: str) -> Iterator[Any]:
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    created = ConnectionPool(pg_dsn, min_size=1, max_size=4)
    try:
        yield created
    finally:
        created.close()


def _sql(pool: Any, sql: str, params: tuple = ()) -> List[tuple]:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
        conn.commit()
    return rows


def _raw(dsn: str, sql: str, params: tuple) -> List[tuple]:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _insert_pending(pool: Any, job_id: str = JOB_ID) -> None:
    _sql(
        pool,
        "INSERT INTO background_jobs (job_id, operation_type, status, created_at, "
        "username, repo_alias) VALUES (%s, 'global_repo_refresh', 'pending', NOW(), "
        "'system', %s)",
        (job_id, ALIAS),
    )


def _reset_for_reclaim(pool: Any, job_id: str = JOB_ID) -> None:
    # ^ see src/code_indexer/server/services/job_reconciliation_service.py:JobReconciliationService._reclaim_dead_node_jobs
    _sql(
        pool,
        "UPDATE background_jobs SET status = 'pending', executing_node = NULL "
        "WHERE job_id = %s",
        (job_id,),
    )


def _claimer(pool: Any, node: str = NODE) -> Any:
    from code_indexer.server.services.distributed_job_claimer import (
        DistributedJobClaimer,
    )

    return DistributedJobClaimer(pool, node, memory_gate_enabled=False)


def test_still_owns_follows_the_claim_generation(pool):
    _insert_pending(pool)
    claimer = _claimer(pool)
    first = claimer.claim_next_job()
    assert first is not None
    assert claimer.still_owns(JOB_ID, first["claimed_at"]) is True

    time.sleep(CLAIM_CLOCK_STEP_SECONDS)
    _reset_for_reclaim(pool)
    second = claimer.claim_next_job()
    assert second is not None and second["claimed_at"] != first["claimed_at"]
    assert claimer.still_owns(JOB_ID, first["claimed_at"]) is False, (
        "a re-claim by the SAME node must fence the earlier run"
    )
    assert claimer.still_owns(JOB_ID, second["claimed_at"]) is True
    assert _claimer(pool, "node-2").still_owns(JOB_ID, second["claimed_at"]) is False

    _sql(
        pool,
        "UPDATE background_jobs SET status = 'cancelled' WHERE job_id = %s",
        (JOB_ID,),
    )
    assert claimer.still_owns(JOB_ID, second["claimed_at"]) is False


def test_reclaimed_claimed_refresh_is_fenced_and_leaves_the_row(
    pool, tmp_path, monkeypatch, pid_file
):
    from code_indexer.server.services.distributed_job_worker import (
        DistributedJobWorkerService,
    )

    install_fake_cidx(tmp_path, monkeypatch, "--fts", pid_file)
    scheduler, recorder = make_local_repo_scheduler(tmp_path)
    target = alias_target(tmp_path)
    _insert_pending(pool)
    worker = DistributedJobWorkerService(_claimer(pool), scheduler)
    run = threading.Thread(target=worker._process_one_job, daemon=True)
    run.start()
    try:
        assert _wait_for(
            lambda: pid_file.exists() and pid_file.stat().st_size > 0,
            CHILD_START_BOUND_SECONDS,
        )
        time.sleep(CLAIM_CLOCK_STEP_SECONDS)
        _reset_for_reclaim(pool)
        new_claim = _claimer(pool).claim_next_job()
        assert new_claim is not None, "the same node re-claims the job"

        pids = json.loads(pid_file.read_text())
        assert _wait_until_gone(pids["child_pid"], CANCEL_BOUND_SECONDS)
        assert _wait_until_gone(pids["grandchild_pid"], GROUP_GONE_BOUND_SECONDS)
        run.join(CANCEL_BOUND_SECONDS)
        assert not run.is_alive()

        [(status, claimed_at, error)] = _sql(
            pool,
            "SELECT status, claimed_at, error FROM background_jobs WHERE job_id = %s",
            (JOB_ID,),
        )
        assert status == "running", "the fenced run must not finish the new owner's job"
        assert claimed_at == new_claim["claimed_at"] and error is None
        assert recorder.calls == [] and alias_target(tmp_path) == target
        state = scheduler.golden_repo_metadata.get_refresh_failure_backoff_state(ALIAS)
        assert state is None
    finally:
        run.join(JOIN_GRACE_SECONDS)


def test_lease_probe_fences_after_takeover_and_never_shortens(pg_dsn, tmp_path):
    from code_indexer.server.services.alias_lock_store.base import (
        AliasLockOwnershipLostError,
    )
    from code_indexer.server.services.alias_lock_store.postgres_store import (
        PostgresAliasLockStore,
    )

    store = PostgresAliasLockStore(pg_dsn, node_id=NODE)
    scheduler, _ = make_local_repo_scheduler(
        tmp_path,
        alias_lock_db_backed_enabled_getter=lambda: True,
        alias_lock_store_resolver=lambda: store,
    )
    assert scheduler.write_lock_manager.acquire(
        REPO, owner_name="refresh_scheduler", ttl_seconds=REFRESH_LEASE_SECONDS
    )
    try:
        scheduler.raise_if_write_lock_ownership_lost(
            REPO, owner_name="refresh_scheduler", ttl_seconds=SHORT_RENEW_SECONDS
        )
        [(ttl,)] = _raw(
            pg_dsn,
            "SELECT ttl_seconds FROM golden_repo_alias_locks WHERE lock_key = %s",
            (REPO,),
        )
        assert ttl == REFRESH_LEASE_SECONDS, "a probe must never shorten the lease"

        _raw(pg_dsn, "DELETE FROM golden_repo_alias_locks WHERE lock_key = %s", (REPO,))
        assert store.try_acquire(
            REPO,
            "refresh_scheduler",
            owner_token="sibling",
            ttl_seconds=SHORT_RENEW_SECONDS,
        )
        with pytest.raises(AliasLockOwnershipLostError):
            scheduler.raise_if_write_lock_ownership_lost(
                REPO, owner_name="refresh_scheduler", ttl_seconds=REFRESH_LEASE_SECONDS
            )
        assert store.get_holder(REPO) is not None, "the sibling keeps its lease"
    finally:
        scheduler.write_lock_manager.release(REPO, owner_name="refresh_scheduler")


def test_background_job_reassigned_by_the_reconciler_keeps_running(pool):
    from code_indexer.server.repositories.background_jobs import BackgroundJobManager
    from code_indexer.server.storage.postgres.background_jobs_backend import (
        BackgroundJobsPostgresBackend,
    )

    manager = BackgroundJobManager(
        storage_backend=BackgroundJobsPostgresBackend(pool), node_id=NODE
    )
    answers: List[bool] = []
    running = threading.Event()
    reassigned = threading.Event()

    # ^ why declare progress_callback; without it BGM ticks 25% (a row read) before work starts
    def work(progress_callback=None, cancel_check=None):
        running.set()
        reassigned.wait(CHILD_START_BOUND_SECONDS)
        for _ in range(POLLS_AFTER_REASSIGNMENT):
            answers.append(bool(cancel_check()))
            time.sleep(WORK_POLL_SECONDS)
        return {"success": True}

    try:
        job_id = manager.submit_job(
            "unscoped_test_op", work, submitter_username="system"
        )
        assert running.wait(CHILD_START_BOUND_SECONDS)
        _reset_for_reclaim(pool, job_id)
        _sql(
            pool,
            "UPDATE background_jobs SET status = 'running', executing_node = 'node-2', "
            "claimed_at = NOW() WHERE job_id = %s",
            (job_id,),
        )
        reassigned.set()
        assert _wait_for(
            lambda: job_id not in manager._running_jobs,
            CANCEL_BOUND_SECONDS + ROW_STATE_BOUND_SECONDS,
        )
        assert answers == [False] * POLLS_AFTER_REASSIGNMENT, (
            "a BGM job has no claim generation yet; the reconciler's reassignment "
            "must not fence it (the held lease keeps a duplicate from writing)"
        )
    finally:
        reassigned.set()
        manager.shutdown()
