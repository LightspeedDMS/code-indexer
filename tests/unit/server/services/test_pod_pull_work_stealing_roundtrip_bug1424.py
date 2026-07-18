"""M1: pod-pull work-stealing full round-trip (PR #1424).

A behavioral in-memory jobs table faithfully evaluates the REAL
DistributedJobClaimer SQL (claim UPDATE ... RETURNING with job_types/exclude
filters, update_progress, complete_job) against row dicts -- not a scripted
MagicMock -- so the test exercises the actual claim/execute/complete seam and
the dead-node reclaim -> re-claim -> re-execute path a work-stolen index op
relies on.

Bug #1430 coverage gap: every class below (except the last) seeds its
pending row via the hand-constructed ``_pending_row()`` helper, which sets
``executing_node: None`` directly -- bypassing the REAL insert path entirely
(BackgroundJobManager.submit_job -> JobTracker.register_job_if_no_conflict
-> _atomic_insert_impl), which is exactly where issue #1430's regression
lived (the atomic insert unconditionally stamped
executing_node=self._node_id, even for pod-pull rows, orphaning them
forever). TestPodPullRoundTripRealSubmitBug1430 below closes that gap: it
derives the pending row from a genuine submit_job() call against a real
SQLite-backed JobTracker, so a reintroduced insert-time stamp would make
this test fail exactly as issue #1430 described (the row can never be
claimed).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services import memory_governor as mg
from code_indexer.server.services.distributed_job_claimer import (
    DistributedJobClaimer,
    _SELECT_COLS,
)
from code_indexer.server.services.index_job_claim_loop import IndexJobClaimLoop
from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend


# Column order the claimer's RETURNING clause uses; parsed once so the fake row
# tuple stays correct even if the column list is reordered.
_COLS = [c.strip() for c in _SELECT_COLS.replace("\n", " ").split(",") if c.strip()]


def _row_tuple(row: dict):
    return tuple(row.get(c) for c in _COLS)


class _ClaimerJobsTable:
    """Faithful in-memory evaluator for the claimer statements this test drives."""

    def __init__(self, rows, now):
        self.rows = rows
        self.now = now

    def _pending_match(self, job_types, exclude_types):
        candidates = [
            r
            for r in self.rows
            if r["status"] == "pending" and r["executing_node"] is None
        ]
        if job_types is not None:
            candidates = [r for r in candidates if r["operation_type"] in job_types]
        if exclude_types is not None:
            candidates = [
                r for r in candidates if r["operation_type"] not in exclude_types
            ]
        candidates.sort(key=lambda r: r["created_at"])
        return candidates[0] if candidates else None

    def claim(self, node_id, job_types, exclude_types):
        row = self._pending_match(job_types, exclude_types)
        if row is None:
            return None
        row["status"] = "running"
        row["executing_node"] = node_id
        row["claimed_at"] = self.now
        row["started_at"] = self.now
        return _row_tuple(row)

    def _owned(self, job_id, node_id):
        for r in self.rows:
            if r["job_id"] == job_id and r["executing_node"] == node_id:
                return r
        return None

    def update_progress(self, job_id, node_id, progress, phase, detail):
        r = self._owned(job_id, node_id)
        if r is None:
            return 0
        r["progress"] = progress
        if phase is not None:
            r["current_phase"] = phase
        if detail is not None:
            r["phase_detail"] = detail
        return 1

    def complete(self, job_id, node_id, result_json):
        r = self._owned(job_id, node_id)
        if r is None:
            return 0
        r["status"] = "completed"
        r["result"] = result_json
        r["progress"] = 100
        return 1

    def fail(self, job_id, node_id, error):
        r = self._owned(job_id, node_id)
        if r is None:
            return 0
        r["status"] = "failed"
        r["error"] = error
        return 1

    def dead_node_reclaim(self, active_nodes):
        """Simulate JobReconciliationService resetting dead-node running rows."""
        reclaimed = []
        for r in self.rows:
            if r["status"] == "running" and r["executing_node"] not in active_nodes:
                r["status"] = "pending"
                r["executing_node"] = None
                r["started_at"] = None
                r["claimed_at"] = None
                reclaimed.append(r["job_id"])
        return reclaimed


def _make_pool(table: _ClaimerJobsTable):
    cur = MagicMock()

    def _execute(sql, params=()):
        normalized = " ".join(sql.split())
        if "SET executing_node = %s" in normalized and "RETURNING" in normalized:
            node_id = params[0]
            job_types = None
            exclude_types = None
            rest = list(params[1:])
            if "operation_type = ANY(%s)" in normalized:
                job_types = set(rest.pop(0))
            if "operation_type <> ALL(%s)" in normalized:
                exclude_types = set(rest.pop(0))
            cur.fetchone.return_value = table.claim(node_id, job_types, exclude_types)
        elif "SET progress" in normalized and "COALESCE" in normalized:
            progress, phase, detail, job_id, node_id = params
            cur.rowcount = table.update_progress(
                job_id, node_id, progress, phase, detail
            )
        elif "status = 'completed'" in normalized:
            result_json, job_id, node_id = params
            cur.rowcount = table.complete(job_id, node_id, result_json)
        elif "status = 'failed'" in normalized:
            error, job_id, node_id = params
            cur.rowcount = table.fail(job_id, node_id, error)
        else:
            cur.fetchone.return_value = None

    cur.execute.side_effect = _execute

    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool


def _pending_row(job_id, op, metadata, created_at):
    return {
        "job_id": job_id,
        "operation_type": op,
        "status": "pending",
        "created_at": created_at,
        "started_at": None,
        "completed_at": None,
        "result": None,
        "error": None,
        "progress": 0,
        "username": "admin",
        "is_admin": True,
        "cancelled": False,
        "repo_alias": metadata.get("alias", "repoA"),
        "resolution_attempts": 0,
        "claude_actions": None,
        "failure_reason": None,
        "extended_error": None,
        "language_resolution_status": None,
        "executing_node": None,
        "claimed_at": None,
        "metadata": metadata,
        "current_phase": None,
        "phase_detail": None,
    }


class TestPodPullRoundTrip:
    def teardown_method(self):
        mg.clear_memory_governor()

    def test_pending_claimed_reconstructed_executed_completed(self):
        now = datetime.now(timezone.utc)
        row = _pending_row("j1", "add_golden_repo", {"alias": "repoA"}, now)
        table = _ClaimerJobsTable([row], now)
        claimer = DistributedJobClaimer(pool=_make_pool(table), node_id="node-1")

        seen = {}

        def add_executor(metadata, progress_callback):
            seen.update(metadata)
            progress_callback(40, phase="index", detail="indexing")
            return {"success": True, "alias": metadata["alias"]}

        loop = IndexJobClaimLoop(
            claimer=claimer,
            dispatch={"add_golden_repo": add_executor},
            node_id="node-1",
        )

        assert loop._process_one_job() is True

        # Reconstructed from metadata.
        assert seen == {"alias": "repoA"}
        # Row transitioned pending -> completed with the result + progress
        # written mid-flight to the SHARED row.
        assert row["status"] == "completed"
        assert row["result"] == '{"success": true, "alias": "repoA"}'
        assert row["current_phase"] == "index"
        assert row["progress"] == 100  # complete_job sets 100 last

    def test_leader_exclude_and_loop_include_are_disjoint(self):
        now = datetime.now(timezone.utc)
        row = _pending_row("j1", "add_golden_repo", {"alias": "repoA"}, now)
        table = _ClaimerJobsTable([row], now)
        claimer = DistributedJobClaimer(pool=_make_pool(table), node_id="leader")

        # The leader worker excludes pod-pull ops: it must NOT claim this row.
        from code_indexer.server.repositories.background_jobs import POD_PULL_OPS

        claimed = claimer.claim_next_job(exclude_types=sorted(POD_PULL_OPS))
        assert claimed is None
        assert row["status"] == "pending"

    def test_dead_node_reclaim_then_reexecute(self):
        now = datetime.now(timezone.utc) - timedelta(hours=1)
        row = _pending_row("j1", "add_golden_repo", {"alias": "repoA"}, now)
        # Row was claimed by a since-dead node.
        row["status"] = "running"
        row["executing_node"] = "dead-node"
        row["claimed_at"] = now
        table = _ClaimerJobsTable([row], datetime.now(timezone.utc))

        # Reconciliation resets the dead node's running row back to pending.
        assert table.dead_node_reclaim(active_nodes=["node-2"]) == ["j1"]
        assert row["status"] == "pending"
        assert row["executing_node"] is None

        # A live node's loop now re-claims and completes it.
        claimer = DistributedJobClaimer(pool=_make_pool(table), node_id="node-2")
        ran = []

        def _dispatch(md, pc):
            ran.append(md)
            return {"ok": True}

        loop = IndexJobClaimLoop(
            claimer=claimer,
            dispatch={"add_golden_repo": _dispatch},
            node_id="node-2",
        )
        assert loop._process_one_job() is True
        assert ran == [{"alias": "repoA"}]
        assert row["status"] == "completed"
        assert row["executing_node"] == "node-2"


# ---------------------------------------------------------------------------
# Bug #1430: real-insert-then-claim round trip.
#
# Unlike the classes above (which hand-construct the pending row via
# _pending_row(), setting executing_node=None directly), this class derives
# the pending row from a GENUINE BackgroundJobManager.submit_job() call
# against a real SQLite-backed JobTracker -- the exact seam issue #1430
# proved broken on staging (the atomic insert stamped executing_node to the
# submitting node even for pod-pull rows, so DistributedJobClaimer's
# `executing_node IS NULL` predicate could never match it and the job hung
# PENDING forever). If the insert-time stamp regresses, this test fails at
# the claim step below exactly as production did.
# ---------------------------------------------------------------------------


def _create_schema(db_path: str) -> None:
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


def _real_submitted_pending_row(db_path: str, job_id: str) -> Dict[str, Any]:
    """Read back a submitted job's row from SQLite, shaped exactly like
    _pending_row()'s dict so it drops straight into _ClaimerJobsTable."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT job_id, operation_type, status, created_at, started_at,
                      completed_at, result, error, progress, username,
                      is_admin, cancelled, repo_alias, resolution_attempts,
                      claude_actions, failure_reason, extended_error,
                      language_resolution_status, executing_node, claimed_at,
                      metadata, current_phase, phase_detail
               FROM background_jobs WHERE job_id = ?""",
            (job_id,),
        ).fetchone()
    assert row is not None, f"job {job_id} not found in DB"
    d = dict(row)
    d["is_admin"] = bool(d["is_admin"])
    d["cancelled"] = bool(d["cancelled"])
    d["metadata"] = json.loads(d["metadata"]) if d["metadata"] else None
    return d


def simple_job() -> Dict[str, Any]:
    return {"status": "ok"}


class TestPodPullRoundTripRealSubmitBug1430:
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
        mg.clear_memory_governor()

    def test_real_submit_then_real_claim_execute_complete(self):
        """End-to-end: real submit_job() insert -> real claim_next_job() ->
        real IndexJobClaimLoop execution -> real completion, on a DIFFERENT
        node than the submitter (proving genuine cross-node work-stealing,
        not merely "some node can claim it")."""
        job_id = self.manager.submit_job(
            "add_golden_repo",
            simple_job,
            submitter_username="admin",
            is_admin=True,
            repo_alias="repoA",
            metadata={"alias": "repoA"},
        )

        row = _real_submitted_pending_row(self.db_path, job_id)
        assert row["status"] == "pending"

        table = _ClaimerJobsTable([row], datetime.now(timezone.utc))
        # node-2: a DIFFERENT node than the submitter (node-submitter),
        # proving this is genuine cross-node pod-pull work-stealing.
        claimer = DistributedJobClaimer(pool=_make_pool(table), node_id="node-2")

        seen = {}

        def add_executor(metadata, progress_callback):
            seen.update(metadata)
            progress_callback(40, phase="index", detail="indexing")
            return {"success": True, "alias": metadata["alias"]}

        loop = IndexJobClaimLoop(
            claimer=claimer,
            dispatch={"add_golden_repo": add_executor},
            node_id="node-2",
        )

        claimed_and_executed = loop._process_one_job()

        assert claimed_and_executed is True, (
            "Bug #1430: the real submit_job()-inserted row could not be "
            "claimed by node-2's IndexJobClaimLoop. This reproduces the "
            "production regression: a pod-pull row born with a non-NULL "
            "executing_node can never satisfy DistributedJobClaimer's "
            "`executing_node IS NULL` claim predicate and hangs PENDING "
            "forever."
        )
        assert seen == {"alias": "repoA"}
        assert row["status"] == "completed"
        assert row["executing_node"] == "node-2"
