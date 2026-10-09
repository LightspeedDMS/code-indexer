"""
Bug #1964: /health must degrade only for RECENT failed jobs.

Before the fix /health read ``get_failed_job_count()`` -- an all-time
``COUNT(*) WHERE status='failed'`` -- and went ``degraded`` on any count
above zero. Job retention is 30 days, so ONE genuine failure kept the
server degraded for a month. The owner decision: count only failed jobs
completed within the last 24 hours.

Every test drives the REAL ``GET /health`` route (registered through
``register_misc_routes`` on an isolated FastAPI app -- never create_app(),
never ~/.cidx-server) over a REAL ``BackgroundJobManager`` whose job rows
live in real storage: a temp SQLite file, and -- when TEST_POSTGRES_DSN is
set -- a freshly migrated throwaway PostgreSQL database.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole

_OLD_FAILURE_AGE = timedelta(hours=25)
_RECENT_FAILURE_AGE = timedelta(hours=1)
_PG_DSN = os.environ.get("TEST_POSTGRES_DSN", "")


def _admin() -> User:
    return User(
        username="health-admin",
        password_hash="unused-hash",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )


def _seed_failed_job(backend: Any, age: timedelta) -> None:
    completed = datetime.now(timezone.utc) - age
    backend.save_job(
        job_id=f"failed-{uuid.uuid4().hex[:12]}",
        operation_type="add_golden_repo",
        status="failed",
        created_at=(completed - timedelta(minutes=5)).isoformat(),
        started_at=(completed - timedelta(minutes=5)).isoformat(),
        completed_at=completed.isoformat(),
        username="system",
        progress=25,
        error="Git clone failed: repository not found",
    )


def _health(manager: Any, tmp_path: Path) -> Dict[str, Any]:
    """GET /health through the real route over *manager*."""
    from code_indexer.server.auth.dependencies import get_current_user
    from code_indexer.server.routers.inline_misc import register_misc_routes

    app = FastAPI()
    register_misc_routes(
        app,
        golden_repo_manager=None,
        activated_repo_manager=None,
        config_service=None,
        server_config=None,
        data_dir=str(tmp_path),
        background_job_manager=manager,
        user_manager=None,
    )
    app.dependency_overrides[get_current_user] = _admin
    response = TestClient(app, raise_server_exceptions=True).get("/health")
    assert response.status_code == 200
    body: Dict[str, Any] = response.json()
    return body


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_jobs(tmp_path: Path) -> Iterator[Callable[[], Any]]:
    """Yields (seed_backend, manager) over one real, schema-initialized
    SQLite job database; every manager is shut down at teardown."""
    from code_indexer.server.repositories.background_jobs import (
        BackgroundJobManager,
    )
    from tests.unit.server.repositories._bug1950_test_helpers import (
        make_sqlite_backend,
    )

    built: List[BackgroundJobManager] = []
    backend = make_sqlite_backend(tmp_path, "jobs.db")

    def _manager() -> BackgroundJobManager:
        manager = BackgroundJobManager(
            use_sqlite=True, db_path=str(tmp_path / "jobs.db")
        )
        built.append(manager)
        return manager

    _manager.backend = backend  # type: ignore[attr-defined]
    yield _manager
    for manager in built:
        manager.shutdown()


def test_sqlite_failure_older_than_window_leaves_health_healthy(
    sqlite_jobs: Any, tmp_path: Path
) -> None:
    _seed_failed_job(sqlite_jobs.backend, _OLD_FAILURE_AGE)

    body = _health(sqlite_jobs(), tmp_path)

    assert body["status"] == "healthy", body["message"]
    assert body["job_queue"]["failed_jobs"] == 0
    assert body["job_queue"]["failed_jobs_window"] == "24h"


def test_sqlite_recent_failure_degrades_health(
    sqlite_jobs: Any, tmp_path: Path
) -> None:
    _seed_failed_job(sqlite_jobs.backend, _RECENT_FAILURE_AGE)

    body = _health(sqlite_jobs(), tmp_path)

    assert body["status"] == "degraded"
    assert body["job_queue"]["failed_jobs"] == 1
    assert "1 failed jobs detected in the last 24h" in body["message"]


def test_sqlite_reported_count_is_the_windowed_count(
    sqlite_jobs: Any, tmp_path: Path
) -> None:
    """The number shown is the number that drives the status: old failures
    are excluded from it, recent ones are all counted."""
    _seed_failed_job(sqlite_jobs.backend, _OLD_FAILURE_AGE)
    _seed_failed_job(sqlite_jobs.backend, _OLD_FAILURE_AGE)
    _seed_failed_job(sqlite_jobs.backend, _RECENT_FAILURE_AGE)
    _seed_failed_job(sqlite_jobs.backend, _RECENT_FAILURE_AGE)

    body = _health(sqlite_jobs(), tmp_path)

    assert body["status"] == "degraded"
    assert body["job_queue"]["failed_jobs"] == 2


def test_sqlite_windowed_stats_query_is_an_index_range_search(
    sqlite_jobs: Any,
) -> None:
    """The windowed query /health runs is bounded by the completed_at index:
    a SEARCH on idx_background_jobs_completed_status, never a walk over
    every retained job row (30 days of history)."""
    backend = sqlite_jobs.backend
    _seed_failed_job(backend, _OLD_FAILURE_AGE)
    conn = backend._conn_manager.get_connection()
    executed: List[str] = []
    conn.set_trace_callback(executed.append)
    try:
        backend.get_job_stats("24h")
    finally:
        conn.set_trace_callback(None)
    stats_sql = [s for s in executed if "GROUP BY" in s]
    assert len(stats_sql) == 1, executed

    plan = " | ".join(
        row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + stats_sql[0])
    )

    assert "SEARCH" in plan and "idx_background_jobs_completed_status" in plan, plan
    assert "SCAN" not in plan, plan


# ---------------------------------------------------------------------------
# PostgreSQL (live, TEST_POSTGRES_DSN-gated)
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_backend() -> Iterator[Any]:
    """A real BackgroundJobsPostgresBackend over a freshly created and
    fully migrated throwaway database, dropped at teardown."""
    if not _PG_DSN:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from code_indexer.server.storage.postgres.background_jobs_backend import (
        BackgroundJobsPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import (
        ConnectionPool,
    )
    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    name = f"health_window_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_PG_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(_PG_DSN)
    params["dbname"] = name
    dsn = make_conninfo(**params)  # type: ignore[arg-type]
    pool = None
    try:
        with MigrationRunner(dsn) as runner:
            runner.run()
        pool = ConnectionPool(dsn, min_size=1, max_size=2)
        yield BackgroundJobsPostgresBackend(pool)
    finally:
        if pool is not None:
            pool.close()
        with psycopg.connect(_PG_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _pg_health(backend: Any, tmp_path: Path) -> Dict[str, Any]:
    from code_indexer.server.repositories.background_jobs import (
        BackgroundJobManager,
    )

    manager = BackgroundJobManager(storage_backend=backend, node_id="node-a18")
    try:
        return _health(manager, tmp_path)
    finally:
        manager.shutdown()


def test_pg_failure_older_than_window_leaves_health_healthy(
    pg_backend: Any, tmp_path: Path
) -> None:
    _seed_failed_job(pg_backend, _OLD_FAILURE_AGE)

    body = _pg_health(pg_backend, tmp_path)

    assert body["status"] == "healthy", body["message"]
    assert body["job_queue"]["failed_jobs"] == 0


def test_pg_recent_failure_degrades_health_with_windowed_count(
    pg_backend: Any, tmp_path: Path
) -> None:
    _seed_failed_job(pg_backend, _OLD_FAILURE_AGE)
    _seed_failed_job(pg_backend, _RECENT_FAILURE_AGE)

    body = _pg_health(pg_backend, tmp_path)

    assert body["status"] == "degraded"
    assert body["job_queue"]["failed_jobs"] == 1


def test_pg_stuck_job_reclaimed_as_failed_gets_completed_at_and_degrades_health(
    pg_backend: Any, tmp_path: Path
) -> None:
    """A running job that never recorded a start, reclaimed to 'failed' by a
    real reconciliation sweep, must carry completed_at: without it the
    windowed /health query never sees it and retention never purges it."""
    from code_indexer.server.services.job_reconciliation_service import (
        JobReconciliationService,
    )
    from code_indexer.server.services.node_heartbeat_service import (
        NodeHeartbeatService,
    )

    pool = pg_backend._pool
    job_id = f"stuck-{uuid.uuid4().hex[:12]}"
    claimed = datetime.now(timezone.utc) - timedelta(hours=2)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO background_jobs
                       (job_id, operation_type, status, created_at, claimed_at,
                        username, progress, repo_alias, executing_node)
                   VALUES (%s, 'refresh_golden_repo', 'running', %s, %s,
                           'system', 0, 'example-repo', 'node-a18')""",
                (job_id, claimed, claimed),
            )
        conn.commit()

    service = JobReconciliationService(
        pool, NodeHeartbeatService(pool, "node-a18"), max_execution_time=60
    )
    assert service.sweep() == 1

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, completed_at FROM background_jobs WHERE job_id = %s",
                (job_id,),
            )
            status, completed_at = cur.fetchone()
    assert status == "failed"
    assert completed_at is not None
    body = _pg_health(pg_backend, tmp_path)
    assert body["status"] == "degraded"
    assert body["job_queue"]["failed_jobs"] == 1
