"""Bug #1430: pod-pull jobs must be inserted with executing_node NULL.

Root cause (proven on staging, see GitHub issue #1430): PR #1424's pod-pull
submission path routes through JobTracker.register_job_if_no_conflict ->
_atomic_insert_or_raise -> _atomic_insert_impl (Story #1400 CRITICAL 3),
which unconditionally stamps ``executing_node=self._node_id`` on INSERT --
even while the row is still ``pending``. PR #1424's DistributedJobClaimer
requires ``executing_node IS NULL`` in its claim SQL for a pending row to be
claimable by ANY node. Because the submitting node deliberately does NOT
dispatch a pod-pull op locally (that's the whole point of pod-pull -- let a
node with memory headroom claim it), a pod-pull row born with
executing_node already stamped to the submitter is orphaned forever: no
node (including the submitter) can ever claim it.

These tests use the REAL BackgroundJobManager.submit_job() path (not a
hand-crafted DB row) with a REAL JobTracker backed by a REAL SQLite
database (BackgroundJobsSqliteBackend) -- anti-mock rule. The bug is
proven purely via the persisted row's ``executing_node`` column value,
which is backend-agnostic (SQLite here; PostgreSQL in production uses the
identical job_tracker.py code path).
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any, Dict

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend


def _create_schema(db_path: str) -> None:
    """Create background_jobs table + idx_active_job_per_repo (mirrors production)."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS background_jobs (
            job_id TEXT PRIMARY KEY NOT NULL,
            operation_type TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            result TEXT,
            error TEXT,
            progress INTEGER NOT NULL DEFAULT 0,
            username TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            cancelled INTEGER NOT NULL DEFAULT 0,
            repo_alias TEXT,
            resolution_attempts INTEGER NOT NULL DEFAULT 0,
            claude_actions TEXT,
            failure_reason TEXT,
            extended_error TEXT,
            language_resolution_status TEXT,
            current_phase TEXT,
            phase_detail TEXT,
            actor_username TEXT,
            progress_info TEXT,
            metadata TEXT,
            executing_node TEXT,
            claimed_at TEXT
        )"""
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_job_per_repo
            ON background_jobs(operation_type, repo_alias)
            WHERE status IN ('pending', 'running')
              AND repo_alias IS NOT NULL
            """
        )
        conn.commit()


def simple_job() -> Dict[str, Any]:
    return {"status": "ok"}


def _read_row(db_path: str, job_id: str) -> Dict[str, Any]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM background_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    assert row is not None, f"job {job_id} not found in DB"
    return dict(row)


class TestPodPullSubmitLeavesExecutingNodeNull:
    """Bug #1430: the REAL submit_job() path, in cluster mode, for a
    POD_PULL_OPS operation with reconstruction metadata, must leave
    executing_node NULL so DistributedJobClaimer.claim_next_job's
    `executing_node IS NULL` predicate can match the row."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = str(Path(self.tmp) / "jobs.db")
        _create_schema(self.db_path)

        self.backend = BackgroundJobsSqliteBackend(self.db_path)
        self.tracker = JobTracker(
            db_path=self.db_path,
            storage_backend=self.backend,
            node_id="node-submitter",
        )
        self.manager = BackgroundJobManager(
            storage_path=None,
            cluster_mode=True,
            node_id="node-submitter",
        )
        self.manager._job_tracker = self.tracker  # type: ignore[assignment]

    def teardown_method(self):
        try:
            self.manager.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pod_pull_job_row_has_null_executing_node_immediately_after_submit(self):
        """RED before fix / GREEN after fix.

        Before the fix: the persisted row has executing_node="node-submitter"
        (the submitting node) even though status is still "pending" -- the
        exact contradiction reported in issue #1430 that orphans the job.

        After the fix: executing_node is NULL, matching the claimer's
        `pending (executing_node IS NULL)` lifecycle precondition.
        """
        job_id = self.manager.submit_job(
            "add_golden_repo",
            simple_job,
            submitter_username="admin",
            is_admin=True,
            repo_alias="repo-pod-pull",
            metadata={"alias": "repo-pod-pull", "repo_url": "https://x/y.git"},
        )

        row = _read_row(self.db_path, job_id)

        assert row["status"] == "pending", (
            f"expected job to remain pending (not locally dispatched), got "
            f"{row['status']!r}"
        )
        assert row["executing_node"] is None, (
            "Bug #1430: pod-pull job inserted with "
            f"executing_node={row['executing_node']!r} instead of NULL. "
            "DistributedJobClaimer.claim_next_job requires executing_node "
            "IS NULL for a pending row to be claimable -- a non-NULL value "
            "here means NO node (including the submitter, which deliberately "
            "does not dispatch pod-pull ops locally) can ever claim this row. "
            "It hangs PENDING forever."
        )


class TestNonPodPullSubmitStillStampsExecutingNode:
    """Control group: node-scoped orphan cleanup (Story #1400 CRITICAL 3)
    must be preserved for every submission path OTHER than pod-pull-eligible
    cluster-mode heavy ops. These jobs ARE dispatched to the submitting
    node's local pool, so executing_node must still be stamped at insert so
    a crash before "running" remains visible to that node's restart cleanup.
    """

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = str(Path(self.tmp) / "jobs.db")
        _create_schema(self.db_path)

        self.backend = BackgroundJobsSqliteBackend(self.db_path)
        self.tracker = JobTracker(
            db_path=self.db_path,
            storage_backend=self.backend,
            node_id="node-submitter",
        )
        self._managers: list = []

    def teardown_method(self):
        # These tests dispatch to real worker threads (non-pod-pull path) --
        # shut each manager down before deleting the temp dir so a worker
        # can't race teardown and try to write to an already-removed DB.
        # A shutdown failure is logged (not silently discarded) but does not
        # block cleanup of the other managers or the temp dir.
        for manager in self._managers:
            try:
                manager.shutdown()
            except Exception:
                logging.warning(
                    "teardown: manager.shutdown() failed for %r",
                    manager,
                    exc_info=True,
                )
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _manager(self, cluster_mode: bool) -> BackgroundJobManager:
        manager = BackgroundJobManager(
            storage_path=None,
            cluster_mode=cluster_mode,
            node_id="node-submitter",
        )
        manager._job_tracker = self.tracker  # type: ignore[assignment]
        self._managers.append(manager)
        return manager

    def test_solo_mode_pod_pull_op_still_stamps_executing_node(self):
        """Solo/SQLite (cluster_mode=False): pod-pull routing never engages,
        the op runs on this node's local pool -- executing_node must still
        be stamped for that node's own crash-recovery cleanup."""
        manager = self._manager(cluster_mode=False)
        job_id = manager.submit_job(
            "add_golden_repo",
            simple_job,
            submitter_username="admin",
            is_admin=True,
            repo_alias="repo-solo",
            metadata={"alias": "repo-solo"},
        )
        row = _read_row(self.db_path, job_id)
        assert row["executing_node"] == "node-submitter"

    def test_cluster_mode_non_pod_pull_op_still_stamps_executing_node(self):
        """Cluster mode but NOT a POD_PULL_OPS operation_type (e.g. an
        ordinary repo-scoped op) -- still dispatched locally, so
        executing_node must still be stamped."""
        manager = self._manager(cluster_mode=True)
        job_id = manager.submit_job(
            "deactivate_repository",
            simple_job,
            submitter_username="admin",
            is_admin=True,
            repo_alias="repo-not-pod-pull",
        )
        row = _read_row(self.db_path, job_id)
        assert row["executing_node"] == "node-submitter"

    def test_cluster_mode_pod_pull_op_without_metadata_still_stamps_executing_node(
        self,
    ):
        """Pod-pull-eligible operation_type but no reconstruction metadata
        (not-yet-migrated caller) falls back to local dispatch -- must still
        stamp executing_node, matching the pre-existing fallback semantics."""
        manager = self._manager(cluster_mode=True)
        job_id = manager.submit_job(
            "add_golden_repo",
            simple_job,
            submitter_username="admin",
            is_admin=True,
            repo_alias="repo-no-metadata",
            metadata=None,
        )
        row = _read_row(self.db_path, job_id)
        assert row["executing_node"] == "node-submitter"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
