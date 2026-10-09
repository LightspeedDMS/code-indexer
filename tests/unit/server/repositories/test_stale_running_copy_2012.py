"""Bug #2012 Part 2: a job interrupted by a server restart must not keep
showing as 'running'.

Root cause reproduced here: at construction every BackgroundJobManager
loaded all still-'running'/'pending' rows into its in-memory dict. After
the startup orphan sweep, any such row is owned by ANOTHER live process
(a sibling uvicorn worker or another cluster node) -- this process can
neither execute nor track it, so the copy is stale the moment it is
loaded. ``list_jobs``/``get_job_status`` let that in-memory copy override
the shared DB row, so once the owner saved the row 'interrupted' at its
shutdown, this worker kept reporting it 'running' (until its own restart).

The scenarios are backend-agnostic functions so the PostgreSQL mirror
(tests/unit/server/storage/postgres/test_stale_running_copy_postgres_2012.py)
runs the exact same steps against a real PostgreSQL backend.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from code_indexer.server.repositories.background_jobs import BackgroundJobManager

JOB_ID = "refresh-job-2012"
OWNER_NODE = "node-a"
RESTART_ERROR = "Indexing interrupted by server shutdown for example-repo-global"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_running_refresh_job(backend: Any) -> None:
    """The owning worker (THIS live test process, so its pid is alive)
    claims and starts a refresh job."""
    backend.save_job(
        job_id=JOB_ID,
        operation_type="global_repo_refresh",
        status="running",
        created_at=_now(),
        started_at=_now(),
        username="system",
        progress=10,
        is_admin=True,
        repo_alias="example-repo-global",
        executing_node=OWNER_NODE,
    )


def dead_pid() -> int:
    """A pid that provably belonged to a process that has exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


def run_stale_copy_scenario(
    owner_backend: Any, make_other_worker: Callable[[], BackgroundJobManager]
) -> None:
    save_running_refresh_job(owner_backend)

    # Another worker/node starts up while the job is genuinely running.
    other_worker = make_other_worker()
    try:
        # The owner is shut down mid-run and records the interruption.
        owner_backend.update_job(
            JOB_ID, status="interrupted", error=RESTART_ERROR, completed_at=_now()
        )
        assert owner_backend.get_job(JOB_ID)["status"] == "interrupted"

        running = other_worker.list_jobs(
            username="admin", status_filter="running", is_admin=True
        )
        assert JOB_ID not in [j["job_id"] for j in running["jobs"]], (
            "an interrupted job is still listed as running by another worker"
        )
        status = other_worker.get_job_status(JOB_ID, "admin", is_admin=True)
        assert status is not None and status["status"] == "interrupted"
        assert other_worker.count_active_refresh_jobs() == 0
    finally:
        other_worker.shutdown()


def test_interrupted_job_not_reported_running_by_other_worker_sqlite(
    tmp_path: Path,
) -> None:
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import (
        BackgroundJobsSqliteBackend,
    )

    db_path = str(tmp_path / "jobs.db")
    DatabaseSchema(db_path).initialize_database()
    owner = BackgroundJobsSqliteBackend(db_path)
    try:
        run_stale_copy_scenario(
            owner, lambda: BackgroundJobManager(use_sqlite=True, db_path=db_path)
        )
    finally:
        owner.close()


def test_running_row_left_by_failed_shutdown_save_is_swept_sqlite(
    tmp_path: Path,
) -> None:
    """The shutdown save never landed (the row is still 'running') and the
    owning process is gone: the next startup's sweep marks it interrupted
    and no worker reports it running."""
    import sqlite3

    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import (
        BackgroundJobsSqliteBackend,
    )

    db_path = str(tmp_path / "jobs.db")
    DatabaseSchema(db_path).initialize_database()
    owner = BackgroundJobsSqliteBackend(db_path)
    save_running_refresh_job(owner)
    owner.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE background_jobs SET executing_pid = ? WHERE job_id = ?",
            (dead_pid(), JOB_ID),
        )

    restarted = BackgroundJobManager(use_sqlite=True, db_path=db_path)
    try:
        status = restarted.get_job_status(JOB_ID, "admin", is_admin=True)
        assert status is not None and status["status"] == "interrupted"
        running = restarted.list_jobs(
            username="admin", status_filter="running", is_admin=True
        )
        assert running["jobs"] == []
    finally:
        restarted.shutdown()
