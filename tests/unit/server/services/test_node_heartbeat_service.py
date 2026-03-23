"""
Unit tests for NodeHeartbeatService.

Story #422: Job Reconciliation Service

Mock hierarchy (no real PostgreSQL required):
    pool.connection() -> context manager -> conn
    conn.cursor()     -> context manager -> cur
    cur.execute(sql, params)
    cur.fetchall()
"""

from __future__ import annotations

from unittest.mock import MagicMock


from code_indexer.server.services.node_heartbeat_service import NodeHeartbeatService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(fetchall=None):
    """Build a mocked ConnectionPool."""
    cur = MagicMock()
    cur.fetchall.return_value = fetchall if fetchall is not None else []

    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    pool = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    return pool, conn, cur


NODE_ID = "node-test-1"


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestInit:
    def test_node_id_property(self):
        """node_id property must return the id passed to __init__."""
        pool, _, _ = _make_pool()
        svc = NodeHeartbeatService(pool, NODE_ID)
        assert svc.node_id == NODE_ID

    def test_thread_not_started_on_init(self):
        """Background thread must not start until start() is called."""
        pool, _, _ = _make_pool()
        svc = NodeHeartbeatService(pool, NODE_ID)
        assert svc._thread is None


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------


class TestStart:
    def test_start_creates_table(self):
        """start() must execute the CREATE TABLE IF NOT EXISTS DDL."""
        pool, _, cur = _make_pool()
        svc = NodeHeartbeatService(pool, NODE_ID, heartbeat_interval=100)

        svc.start()
        svc.stop()

        all_sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert any(
            "CREATE TABLE IF NOT EXISTS cluster_nodes" in sql for sql in all_sqls
        )

    def test_start_upserts_node_as_online(self):
        """start() must insert/update the node row with status='online'."""
        pool, _, cur = _make_pool()
        svc = NodeHeartbeatService(pool, NODE_ID, heartbeat_interval=100)

        svc.start()
        svc.stop()

        all_calls = cur.execute.call_args_list
        upsert_calls = [
            c
            for c in all_calls
            if len(c.args) > 1
            and isinstance(c.args[1], tuple)
            and NODE_ID in c.args[1]
            and "online" in c.args[1]
        ]
        assert len(upsert_calls) >= 1

    def test_start_spawns_daemon_thread(self):
        """start() must spawn a daemon thread named NodeHeartbeat-<node_id>."""
        pool, _, _ = _make_pool()
        svc = NodeHeartbeatService(pool, NODE_ID, heartbeat_interval=100)

        svc.start()
        try:
            assert svc._thread is not None
            assert svc._thread.is_alive()
            assert svc._thread.daemon is True
            assert NODE_ID in svc._thread.name
        finally:
            svc.stop()

    def test_start_idempotent_when_already_running(self):
        """Calling start() twice must not start a second thread."""
        pool, _, _ = _make_pool()
        svc = NodeHeartbeatService(pool, NODE_ID, heartbeat_interval=100)

        svc.start()
        first_thread = svc._thread
        svc.start()  # second call — must be a no-op
        second_thread = svc._thread

        try:
            assert first_thread is second_thread
        finally:
            svc.stop()


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------


class TestStop:
    def test_stop_marks_node_offline(self):
        """stop() must upsert the node row with status='offline'."""
        pool, _, cur = _make_pool()
        svc = NodeHeartbeatService(pool, NODE_ID, heartbeat_interval=100)

        svc.start()
        svc.stop()

        all_calls = cur.execute.call_args_list
        offline_calls = [
            c
            for c in all_calls
            if len(c.args) > 1
            and isinstance(c.args[1], tuple)
            and NODE_ID in c.args[1]
            and "offline" in c.args[1]
        ]
        assert len(offline_calls) >= 1

    def test_stop_clears_thread_reference(self):
        """After stop(), _thread must be None."""
        pool, _, _ = _make_pool()
        svc = NodeHeartbeatService(pool, NODE_ID, heartbeat_interval=100)

        svc.start()
        svc.stop()

        assert svc._thread is None

    def test_stop_signals_stop_event(self):
        """stop() must set the _stop_event so the loop exits."""
        pool, _, _ = _make_pool()
        svc = NodeHeartbeatService(pool, NODE_ID, heartbeat_interval=100)

        svc.start()
        svc.stop()

        assert svc._stop_event.is_set()

    def test_stop_tolerates_upsert_failure(self):
        """stop() must not raise even if the offline upsert fails."""
        pool, _, cur = _make_pool()
        svc = NodeHeartbeatService(pool, NODE_ID, heartbeat_interval=100)
        svc.start()

        def _failing_on_offline(sql, params=None):
            if params and "offline" in params:
                raise RuntimeError("DB gone")

        cur.execute.side_effect = _failing_on_offline

        # Must not propagate
        svc.stop()


# ---------------------------------------------------------------------------
# update_heartbeat()
# ---------------------------------------------------------------------------


class TestUpdateHeartbeat:
    def test_update_heartbeat_executes_update_sql(self):
        """update_heartbeat() must UPDATE last_heartbeat for this node."""
        pool, _, cur = _make_pool()
        svc = NodeHeartbeatService(pool, NODE_ID)

        svc.update_heartbeat()

        sql = cur.execute.call_args[0][0]
        params = cur.execute.call_args[0][1]
        assert "UPDATE cluster_nodes" in sql
        assert "last_heartbeat" in sql
        assert params[0] == NODE_ID

    def test_update_heartbeat_sets_status_online(self):
        """update_heartbeat() SQL must set status = 'online'."""
        pool, _, cur = _make_pool()
        svc = NodeHeartbeatService(pool, NODE_ID)

        svc.update_heartbeat()

        sql = cur.execute.call_args[0][0]
        assert "online" in sql


# ---------------------------------------------------------------------------
# get_active_nodes()
# ---------------------------------------------------------------------------


class TestGetActiveNodes:
    def test_get_active_nodes_returns_node_ids(self):
        """get_active_nodes() must return a list of node_id strings."""
        pool, _, cur = _make_pool(fetchall=[("node-a",), ("node-b",)])
        svc = NodeHeartbeatService(pool, NODE_ID)

        result = svc.get_active_nodes()

        assert result == ["node-a", "node-b"]

    def test_get_active_nodes_returns_empty_list_when_none(self):
        """get_active_nodes() must return [] when no active nodes."""
        pool, _, _ = _make_pool(fetchall=[])
        svc = NodeHeartbeatService(pool, NODE_ID)

        result = svc.get_active_nodes()

        assert result == []

    def test_get_active_nodes_filters_online_status(self):
        """SQL must filter WHERE status = 'online'."""
        pool, _, cur = _make_pool(fetchall=[])
        svc = NodeHeartbeatService(pool, NODE_ID)

        svc.get_active_nodes()

        sql = cur.execute.call_args[0][0]
        assert "status = 'online'" in sql

    def test_get_active_nodes_uses_interval_multiplication(self):
        """
        SQL must use %s * INTERVAL '1 second' — NOT INTERVAL '%s seconds'
        (which would embed the param inside a string literal and be ignored
        by psycopg).
        """
        pool, _, cur = _make_pool(fetchall=[])
        svc = NodeHeartbeatService(pool, NODE_ID, active_threshold_seconds=45)

        svc.get_active_nodes()

        sql = cur.execute.call_args[0][0]
        params = cur.execute.call_args[0][1]
        # The placeholder must be OUTSIDE quotes
        assert "INTERVAL '1 second'" in sql
        assert "%s" in sql
        # The threshold value must be passed as a parameter
        assert params[0] == 45

    def test_get_active_nodes_passes_threshold_as_param(self):
        """The active_threshold_seconds value must appear in the SQL params."""
        pool, _, cur = _make_pool(fetchall=[])
        svc = NodeHeartbeatService(pool, NODE_ID, active_threshold_seconds=60)

        svc.get_active_nodes()

        params = cur.execute.call_args[0][1]
        assert params[0] == 60
