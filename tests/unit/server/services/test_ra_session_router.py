"""
Unit tests for RASessionRouter (Story #433).

Tests cover:
- Standalone mode (pool=None): every method works without DB access.
- Cluster mode: DB interactions are verified via mocked pool.

Mock hierarchy mirrors NodeHeartbeatService tests:
    pool.connection() -> context manager -> conn
    conn.cursor()     -> context manager -> cur
    cur.execute(sql, params)
    cur.fetchone()
"""

from __future__ import annotations

from unittest.mock import MagicMock

from code_indexer.server.services.ra_session_router import RASessionRouter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NODE_ID = "node-test-1"
REMOTE_NODE_ID = "node-test-2"
SESSION_ID = "session-abc-123"


def _make_pool(fetchone=None):
    """Build a mocked psycopg v3 ConnectionPool.

    Returns (pool, conn, cur) so tests can inspect call history.
    """
    cur = MagicMock()
    cur.fetchone.return_value = fetchone

    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    pool = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    return pool, conn, cur


# ---------------------------------------------------------------------------
# Standalone mode (pool=None)
# ---------------------------------------------------------------------------


class TestStandaloneMode:
    """All methods must work without a pool and never touch a database."""

    def test_node_id_property(self):
        """node_id property returns the value passed to __init__."""
        router = RASessionRouter(NODE_ID)
        assert router.node_id == NODE_ID

    def test_get_session_owner_returns_local_node(self):
        """get_session_owner() always returns self._node_id in standalone mode."""
        router = RASessionRouter(NODE_ID)
        assert router.get_session_owner(SESSION_ID) == NODE_ID

    def test_get_session_owner_returns_local_node_for_any_session(self):
        """get_session_owner() returns local node regardless of session_id."""
        router = RASessionRouter(NODE_ID)
        assert router.get_session_owner("any-session-id") == NODE_ID

    def test_register_session_is_noop(self):
        """register_session() is a no-op in standalone mode (no exception)."""
        router = RASessionRouter(NODE_ID)
        router.register_session(SESSION_ID)  # must not raise

    def test_is_local_session_always_true(self):
        """is_local_session() always returns True in standalone mode."""
        router = RASessionRouter(NODE_ID)
        assert router.is_local_session(SESSION_ID) is True

    def test_get_owner_node_url_returns_none(self):
        """get_owner_node_url() returns None in standalone mode."""
        router = RASessionRouter(NODE_ID)
        assert router.get_owner_node_url(SESSION_ID) is None

    def test_should_proxy_returns_false(self):
        """should_proxy() always returns False in standalone mode."""
        router = RASessionRouter(NODE_ID)
        assert router.should_proxy(SESSION_ID) is False

    def test_pool_none_is_default(self):
        """Omitting pool defaults to standalone mode."""
        router = RASessionRouter(NODE_ID)
        assert router._pool is None


# ---------------------------------------------------------------------------
# Cluster mode — get_session_owner
# ---------------------------------------------------------------------------


class TestClusterGetSessionOwner:
    """get_session_owner() must query research_sessions in cluster mode."""

    def test_returns_node_id_from_db(self):
        """get_session_owner() returns node_id column value from DB."""
        pool, _, cur = _make_pool(fetchone=(REMOTE_NODE_ID,))
        router = RASessionRouter(NODE_ID, pool=pool)

        result = router.get_session_owner(SESSION_ID)

        assert result == REMOTE_NODE_ID

    def test_returns_none_when_session_not_found(self):
        """get_session_owner() returns None when no row is returned."""
        pool, _, cur = _make_pool(fetchone=None)
        router = RASessionRouter(NODE_ID, pool=pool)

        result = router.get_session_owner(SESSION_ID)

        assert result is None

    def test_returns_local_node_when_node_id_column_is_null(self):
        """Rows with NULL node_id (pre-cluster) are treated as local."""
        pool, _, cur = _make_pool(fetchone=(None,))
        router = RASessionRouter(NODE_ID, pool=pool)

        result = router.get_session_owner(SESSION_ID)

        assert result == NODE_ID

    def test_queries_correct_table_and_column(self):
        """SQL must SELECT node_id FROM research_sessions WHERE id = %s."""
        pool, _, cur = _make_pool(fetchone=(NODE_ID,))
        router = RASessionRouter(NODE_ID, pool=pool)
        # Pre-mark column as ensured to isolate this test
        router._column_ensured = True

        router.get_session_owner(SESSION_ID)

        select_calls = [
            c for c in cur.execute.call_args_list if "SELECT node_id" in c.args[0]
        ]
        assert len(select_calls) == 1
        assert SESSION_ID in select_calls[0].args[1]

    def test_ensures_node_id_column_on_first_call(self):
        """First call executes ALTER TABLE ADD COLUMN IF NOT EXISTS."""
        pool, _, cur = _make_pool(fetchone=(NODE_ID,))
        router = RASessionRouter(NODE_ID, pool=pool)

        router.get_session_owner(SESSION_ID)

        alter_calls = [
            c
            for c in cur.execute.call_args_list
            if "ADD COLUMN IF NOT EXISTS" in c.args[0]
        ]
        assert len(alter_calls) == 1

    def test_column_ensure_is_cached(self):
        """ALTER TABLE is only executed once across multiple calls."""
        pool, _, cur = _make_pool(fetchone=(NODE_ID,))
        router = RASessionRouter(NODE_ID, pool=pool)

        router.get_session_owner(SESSION_ID)
        router.get_session_owner(SESSION_ID)

        alter_calls = [
            c
            for c in cur.execute.call_args_list
            if "ADD COLUMN IF NOT EXISTS" in c.args[0]
        ]
        assert len(alter_calls) == 1


# ---------------------------------------------------------------------------
# Cluster mode — register_session
# ---------------------------------------------------------------------------


class TestClusterRegisterSession:
    """register_session() must UPDATE research_sessions.node_id."""

    def test_updates_node_id_in_db(self):
        """register_session() executes UPDATE research_sessions SET node_id."""
        pool, _, cur = _make_pool()
        router = RASessionRouter(NODE_ID, pool=pool)
        router._column_ensured = True

        router.register_session(SESSION_ID)

        update_calls = [
            c
            for c in cur.execute.call_args_list
            if "UPDATE research_sessions" in c.args[0]
        ]
        assert len(update_calls) == 1

    def test_sets_correct_node_id(self):
        """register_session() sets node_id to this node's ID."""
        pool, _, cur = _make_pool()
        router = RASessionRouter(NODE_ID, pool=pool)
        router._column_ensured = True

        router.register_session(SESSION_ID)

        update_calls = [
            c
            for c in cur.execute.call_args_list
            if "UPDATE research_sessions" in c.args[0]
        ]
        assert len(update_calls) == 1
        params = update_calls[0].args[1]
        assert NODE_ID in params
        assert SESSION_ID in params

    def test_ensures_node_id_column(self):
        """register_session() runs ALTER TABLE on first call."""
        pool, _, cur = _make_pool()
        router = RASessionRouter(NODE_ID, pool=pool)

        router.register_session(SESSION_ID)

        alter_calls = [
            c
            for c in cur.execute.call_args_list
            if "ADD COLUMN IF NOT EXISTS" in c.args[0]
        ]
        assert len(alter_calls) == 1


# ---------------------------------------------------------------------------
# Cluster mode — is_local_session
# ---------------------------------------------------------------------------


class TestClusterIsLocalSession:
    """is_local_session() must compare owner to this node's ID."""

    def test_returns_true_for_own_session(self):
        """is_local_session() returns True when DB says this node owns it."""
        pool, _, cur = _make_pool(fetchone=(NODE_ID,))
        router = RASessionRouter(NODE_ID, pool=pool)
        router._column_ensured = True

        assert router.is_local_session(SESSION_ID) is True

    def test_returns_false_for_remote_session(self):
        """is_local_session() returns False when DB shows a different node."""
        pool, _, cur = _make_pool(fetchone=(REMOTE_NODE_ID,))
        router = RASessionRouter(NODE_ID, pool=pool)
        router._column_ensured = True

        assert router.is_local_session(SESSION_ID) is False

    def test_returns_true_when_session_not_found(self):
        """Unknown sessions are treated as local (fail at RA layer not routing)."""
        pool, _, cur = _make_pool(fetchone=None)
        router = RASessionRouter(NODE_ID, pool=pool)
        router._column_ensured = True

        assert router.is_local_session(SESSION_ID) is True


# ---------------------------------------------------------------------------
# Cluster mode — should_proxy
# ---------------------------------------------------------------------------


class TestClusterShouldProxy:
    """should_proxy() must return True only for remote sessions."""

    def test_returns_false_for_local_session(self):
        """should_proxy() returns False when session belongs to this node."""
        pool, _, cur = _make_pool(fetchone=(NODE_ID,))
        router = RASessionRouter(NODE_ID, pool=pool)
        router._column_ensured = True

        assert router.should_proxy(SESSION_ID) is False

    def test_returns_true_for_remote_session(self):
        """should_proxy() returns True when session belongs to a different node."""
        pool, _, cur = _make_pool(fetchone=(REMOTE_NODE_ID,))
        router = RASessionRouter(NODE_ID, pool=pool)
        router._column_ensured = True

        assert router.should_proxy(SESSION_ID) is True

    def test_returns_false_when_session_not_found(self):
        """Unknown sessions are not proxied (treated as local)."""
        pool, _, cur = _make_pool(fetchone=None)
        router = RASessionRouter(NODE_ID, pool=pool)
        router._column_ensured = True

        assert router.should_proxy(SESSION_ID) is False


# ---------------------------------------------------------------------------
# Cluster mode — get_owner_node_url
# ---------------------------------------------------------------------------


class TestClusterGetOwnerNodeUrl:
    """get_owner_node_url() must resolve hostname:port from cluster_nodes."""

    def _make_two_query_pool(self, session_owner: str, hostname: str, port: int):
        """
        Build a pool whose cursor returns different values for successive
        fetchone() calls: first the session owner, then hostname+port.
        """
        cur = MagicMock()
        cur.fetchone.side_effect = [(session_owner,), (hostname, port)]

        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        pool = MagicMock()
        pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
        pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        return pool, cur

    def test_returns_http_url_with_hostname_and_port(self):
        """get_owner_node_url() returns 'http://hostname:port'."""
        pool, cur = self._make_two_query_pool(REMOTE_NODE_ID, "node2.internal", 8000)
        router = RASessionRouter(NODE_ID, pool=pool)
        router._column_ensured = True

        url = router.get_owner_node_url(SESSION_ID)

        assert url == "http://node2.internal:8000"

    def test_queries_cluster_nodes_for_hostname(self):
        """SQL must SELECT hostname, port FROM cluster_nodes WHERE node_id = %s."""
        pool, cur = self._make_two_query_pool(REMOTE_NODE_ID, "node2.internal", 8000)
        router = RASessionRouter(NODE_ID, pool=pool)
        router._column_ensured = True

        router.get_owner_node_url(SESSION_ID)

        cluster_calls = [
            c for c in cur.execute.call_args_list if "cluster_nodes" in c.args[0]
        ]
        assert len(cluster_calls) == 1
        assert REMOTE_NODE_ID in cluster_calls[0].args[1]

    def test_returns_none_when_session_not_found(self):
        """get_owner_node_url() returns None when session row is absent."""
        pool, _, cur = _make_pool(fetchone=None)
        router = RASessionRouter(NODE_ID, pool=pool)
        router._column_ensured = True

        assert router.get_owner_node_url(SESSION_ID) is None

    def test_returns_none_when_owner_node_not_in_cluster_nodes(self):
        """get_owner_node_url() returns None when cluster_nodes has no matching row."""
        cur = MagicMock()
        # First fetchone: session owner; second fetchone: no cluster_nodes row
        cur.fetchone.side_effect = [(REMOTE_NODE_ID,), None]

        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        pool = MagicMock()
        pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
        pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        router = RASessionRouter(NODE_ID, pool=pool)
        router._column_ensured = True

        assert router.get_owner_node_url(SESSION_ID) is None

    def test_returns_none_in_standalone_mode(self):
        """get_owner_node_url() returns None in standalone mode."""
        router = RASessionRouter(NODE_ID)
        assert router.get_owner_node_url(SESSION_ID) is None

    def test_uses_non_default_port(self):
        """get_owner_node_url() includes non-standard port numbers."""
        pool, cur = self._make_two_query_pool(REMOTE_NODE_ID, "node2.internal", 9000)
        router = RASessionRouter(NODE_ID, pool=pool)
        router._column_ensured = True

        url = router.get_owner_node_url(SESSION_ID)

        assert url == "http://node2.internal:9000"
