"""M1: pod-pull work-stealing full round-trip (PR #1424).

A behavioral in-memory jobs table faithfully evaluates the REAL
DistributedJobClaimer SQL (claim UPDATE ... RETURNING with job_types/exclude
filters, update_progress, complete_job) against row dicts -- not a scripted
MagicMock -- so the test exercises the actual claim/execute/complete seam and
the dead-node reclaim -> re-claim -> re-execute path a work-stolen index op
relies on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from code_indexer.server.services import memory_governor as mg
from code_indexer.server.services.distributed_job_claimer import (
    DistributedJobClaimer,
    _SELECT_COLS,
)
from code_indexer.server.services.index_job_claim_loop import IndexJobClaimLoop


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
