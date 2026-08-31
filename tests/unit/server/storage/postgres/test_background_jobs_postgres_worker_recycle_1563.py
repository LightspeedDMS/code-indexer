"""
Bug #1563: BackgroundJobsPostgresBackend.cleanup_orphaned_jobs_on_startup
must not fail a sibling worker's genuinely-running job merely because a
DIFFERENT worker process on the same node was recycled.

Mocking convention matches the sibling file test_background_jobs_postgres.py:
    pool.connection() -> context manager -> conn
    conn.cursor()     -> context manager -> cur
    cur.execute(sql, params)
    cur.fetchall()
    cur.rowcount

Only the psycopg driver plumbing (pool/connection/cursor) is mocked -- no
real PostgreSQL is required. The decisive worker-liveness logic under test
is never mocked: it is driven by REAL OS process ids (this test process's
own live pid, and a real subprocess that has been spawned AND reaped via
Popen.wait(), which is guaranteed to be a dead pid).
"""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import MagicMock


def _dead_pid() -> int:
    """Spawn a trivial subprocess, wait for it to exit (reaping it), and
    return its PID -- guaranteed absent from the OS process table."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    exit_code = proc.wait()
    assert exit_code == 0, f"helper subprocess exited with {exit_code}"
    return proc.pid


def _make_pool(fetchall_side_effect=None, rowcount=0):
    """Same mock hierarchy as test_background_jobs_postgres.py's
    _make_pool, extended with a fetchall side_effect list so successive
    cur.fetchall() calls (one per SELECT this fix issues) can each return
    a different row set."""
    cur = MagicMock()
    if fetchall_side_effect is not None:
        cur.fetchall.side_effect = fetchall_side_effect
    else:
        cur.fetchall.return_value = []
    cur.rowcount = rowcount

    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    pool = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    return pool, conn, cur


class TestCleanupOrphanedJobsWorkerRecycle1563:
    def test_node_scoped_candidate_with_live_pid_is_excluded_from_update(self):
        """A node-scoped candidate whose recorded owning pid is a REAL live
        process must NOT appear in the final UPDATE's job_id list."""
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        alive_pid = os.getpid()
        dead_pid = _dead_pid()

        pool, conn, cur = _make_pool(
            fetchall_side_effect=[
                [("job-alive", alive_pid), ("job-dead", dead_pid)],  # branch A
                [],  # branch B (NULL-owner)
            ],
            rowcount=1,
        )
        backend = BackgroundJobsPostgresBackend(pool)

        count = backend.cleanup_orphaned_jobs_on_startup(node_id="node-22")

        assert count == 1
        update_calls = [
            call
            for call in cur.execute.call_args_list
            if "UPDATE background_jobs" in call[0][0]
        ]
        assert len(update_calls) == 1
        sql, params = update_calls[0][0]
        assert "job-dead" in params[-1]
        assert "job-alive" not in params[-1]

    def test_null_executing_node_branch_reclaimed_unconditionally(self):
        """Bug #1512's NULL-owner branch must be reclaimed regardless of
        any pid value -- it is never pid-checked (a stale pid there can
        belong to a DIFFERENT host, per job_reconciliation_service's
        executing_node=NULL reset, so checking it locally would be a
        false-positive liveness hazard)."""
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(
            fetchall_side_effect=[
                [],  # branch A: nothing owned by this node
                [("job-null-owner",)],  # branch B: NULL-owner running row
            ],
            rowcount=1,
        )
        backend = BackgroundJobsPostgresBackend(pool)

        count = backend.cleanup_orphaned_jobs_on_startup(node_id="node-22")

        assert count == 1
        update_calls = [
            call
            for call in cur.execute.call_args_list
            if "UPDATE background_jobs" in call[0][0]
        ]
        assert len(update_calls) == 1
        _sql, params = update_calls[0][0]
        assert "job-null-owner" in params[-1]

    def test_full_node_restart_reclaims_all_dead_pid_candidates(self):
        """When every recorded owning pid is dead (a genuine full-node
        restart), every node-scoped candidate is still reclaimed."""
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        dead_pid_1 = _dead_pid()
        dead_pid_2 = _dead_pid()

        pool, conn, cur = _make_pool(
            fetchall_side_effect=[
                [("job-orphan-1", dead_pid_1), ("job-orphan-2", dead_pid_2)],
                [],
            ],
            rowcount=2,
        )
        backend = BackgroundJobsPostgresBackend(pool)

        count = backend.cleanup_orphaned_jobs_on_startup(node_id="node-22")

        assert count == 2
        update_calls = [
            call
            for call in cur.execute.call_args_list
            if "UPDATE background_jobs" in call[0][0]
        ]
        assert len(update_calls) == 1
        _sql, params = update_calls[0][0]
        assert set(params[-1]) == {"job-orphan-1", "job-orphan-2"}

    def test_no_candidates_to_fail_skips_update_entirely(self):
        """When every candidate's owning worker is alive, no UPDATE should
        be issued at all -- never a no-op UPDATE with an empty id list."""
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        alive_pid = os.getpid()

        pool, conn, cur = _make_pool(
            fetchall_side_effect=[
                [("job-alive", alive_pid)],
                [],
            ],
            rowcount=0,
        )
        backend = BackgroundJobsPostgresBackend(pool)

        count = backend.cleanup_orphaned_jobs_on_startup(node_id="node-22")

        assert count == 0
        update_calls = [
            call
            for call in cur.execute.call_args_list
            if "UPDATE background_jobs" in call[0][0]
        ]
        assert len(update_calls) == 0

    def test_node_id_none_still_returns_zero_and_executes_no_sql(self):
        """Bug #535 hardening preserved: node_id=None must remain a no-op
        with no SQL executed at all."""
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(rowcount=5)
        backend = BackgroundJobsPostgresBackend(pool)

        count = backend.cleanup_orphaned_jobs_on_startup(node_id=None)

        assert count == 0
        cur.execute.assert_not_called()


_ARBITRARY_TEST_PID = 12345


def test_owning_worker_process_is_alive_treats_probe_exception_as_alive(
    monkeypatch,
) -> None:
    """If psutil.pid_exists itself raises, the helper must conservatively
    return True (treat as alive / do not fail the job) -- never wrongly
    fail a job whose owner it could not disprove is still running."""
    from code_indexer.server.storage.postgres import background_jobs_backend

    def _raising_pid_exists(pid: int) -> bool:
        assert pid == _ARBITRARY_TEST_PID
        raise OSError("simulated probe failure")

    monkeypatch.setattr(
        background_jobs_backend.psutil, "pid_exists", _raising_pid_exists
    )

    assert (
        background_jobs_backend._owning_worker_process_is_alive(_ARBITRARY_TEST_PID)
        is True
    )
