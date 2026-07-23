"""DistributedJobClaimer.update_progress: DB-backed progress for work-stolen jobs.

PR #1424 H2: a pod-pull index op executes on whichever node claims it, NOT the
submitting node. Its progress must be written back to the shared background_jobs
row (ownership-scoped) so the originating node's dashboard -- which polls that
row -- surfaces incremental progress (progress + current_phase + phase_detail)
instead of a 0 -> 100 jump.
"""

from unittest.mock import MagicMock

from code_indexer.server.services.distributed_job_claimer import DistributedJobClaimer


def _claimer():
    pool = MagicMock()
    return DistributedJobClaimer(pool=pool, node_id="node-1"), pool


def _wire_cursor(pool, rowcount=1):
    conn = MagicMock()
    cur = MagicMock()
    cur.rowcount = rowcount
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return cur


class TestUpdateProgress:
    def test_writes_progress_phase_and_detail_scoped_to_executing_node(self):
        c, pool = _claimer()
        cur = _wire_cursor(pool, rowcount=1)

        ok = c.update_progress("j1", 50, phase="index", detail="building")

        assert ok is True
        sql, params = cur.execute.call_args[0]
        # All three progress columns are updated.
        assert "progress" in sql
        assert "current_phase" in sql
        assert "phase_detail" in sql
        # Ownership-scoped by executing_node.
        assert "executing_node = %s" in sql
        assert params[-1] == "node-1"
        # Values pass through.
        assert "j1" in params
        assert 50 in params
        assert "index" in params
        assert "building" in params

    def test_returns_false_when_not_owned(self):
        c, pool = _claimer()
        _wire_cursor(pool, rowcount=0)
        assert c.update_progress("j1", 10) is False

    def test_phase_and_detail_default_to_none(self):
        c, pool = _claimer()
        cur = _wire_cursor(pool, rowcount=1)
        # Omitting phase/detail must still write progress, passing None through
        # for the phase/detail params (COALESCE preserves the existing values).
        assert c.update_progress("j1", 25) is True
        sql, params = cur.execute.call_args[0]
        assert 25 in params
        assert None in params
