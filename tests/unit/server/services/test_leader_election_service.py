"""
Unit tests for LeaderElectionService (Story #423).

All tests mock psycopg connections — no real PostgreSQL required.
The mock hierarchy for a direct connection is:

    psycopg.connect(conn_str)  ->  conn  (MagicMock)
    conn.cursor()              ->  context manager  ->  cur  (MagicMock)
    cur.execute(sql, params)
    cur.fetchone()             ->  (True,) or (False,)

Tests cover:
- AC1: try_acquire_leadership returns True when lock is available
- AC2: try_acquire_leadership returns False when lock is held by another
- AC3: release_leadership closes the dedicated connection
- AC4: is_leader property reflects state correctly
- AC5: start_monitoring / stop_monitoring thread lifecycle
- AC6: monitor thread calls try_acquire_leadership periodically
- AC7: on_become_leader callback invoked on transition to leader
- AC8: on_lose_leadership callback invoked on transition away from leader
- AC9: Repeated try_acquire does NOT fire callback if already leader
- AC10: Connection loss detected in monitor triggers on_lose_leadership
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch


from code_indexer.server.services.leader_election_service import (
    LeaderElectionService,
    _LOCK_ID,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(advisory_result: bool = True) -> MagicMock:
    """
    Build a mock psycopg connection whose pg_try_advisory_lock returns
    the given boolean and whose SELECT 1 ping succeeds.
    """
    cur = MagicMock()
    # fetchone returns (True,) or (False,) matching pg_try_advisory_lock result
    cur.fetchone.return_value = (advisory_result,)
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def _make_service(node_id: str = "test-node") -> LeaderElectionService:
    return LeaderElectionService(
        connection_string="postgresql://localhost/test",
        node_id=node_id,
    )


# ---------------------------------------------------------------------------
# AC1: try_acquire returns True when lock is granted
# ---------------------------------------------------------------------------


def test_try_acquire_returns_true_when_lock_granted():
    """AC1: pg_try_advisory_lock returns True → try_acquire returns True."""
    service = _make_service()
    conn = _make_conn(advisory_result=True)

    with patch("psycopg.connect", return_value=conn):
        result = service.try_acquire_leadership()

    assert result is True
    assert service.is_leader is True


def test_try_acquire_stores_dedicated_connection():
    """AC1: When lock acquired, the connection is stored as the lock connection."""
    service = _make_service()
    conn = _make_conn(advisory_result=True)

    with patch("psycopg.connect", return_value=conn):
        service.try_acquire_leadership()

    assert service._lock_conn is conn


def test_try_acquire_sets_autocommit():
    """
    AC1: autocommit must be set to True so the advisory lock persists
    beyond transaction boundaries.
    """
    service = _make_service()
    conn = _make_conn(advisory_result=True)

    with patch("psycopg.connect", return_value=conn):
        service.try_acquire_leadership()

    assert conn.autocommit is True


def test_try_acquire_executes_correct_sql():
    """AC1: pg_try_advisory_lock is called with the canonical LOCK_ID."""
    service = _make_service()
    conn = _make_conn(advisory_result=True)
    cur = conn.cursor.return_value

    with patch("psycopg.connect", return_value=conn):
        service.try_acquire_leadership()

    cur.execute.assert_called_once_with("SELECT pg_try_advisory_lock(%s)", (_LOCK_ID,))


# ---------------------------------------------------------------------------
# AC2: try_acquire returns False when lock is held by another
# ---------------------------------------------------------------------------


def test_try_acquire_returns_false_when_lock_held():
    """AC2: pg_try_advisory_lock returns False → try_acquire returns False."""
    service = _make_service()
    conn = _make_conn(advisory_result=False)

    with patch("psycopg.connect", return_value=conn):
        result = service.try_acquire_leadership()

    assert result is False
    assert service.is_leader is False


def test_try_acquire_closes_connection_when_lock_not_granted():
    """AC2: If lock not acquired, the newly opened connection is closed immediately."""
    service = _make_service()
    conn = _make_conn(advisory_result=False)

    with patch("psycopg.connect", return_value=conn):
        service.try_acquire_leadership()

    conn.close.assert_called_once()
    assert service._lock_conn is None


def test_try_acquire_returns_false_on_db_error():
    """AC2: Database connection error → returns False, does not raise."""
    service = _make_service()

    with patch("psycopg.connect", side_effect=Exception("connection refused")):
        result = service.try_acquire_leadership()

    assert result is False
    assert service.is_leader is False


# ---------------------------------------------------------------------------
# AC3: release_leadership closes the dedicated connection
# ---------------------------------------------------------------------------


def test_release_closes_lock_connection():
    """AC3: release_leadership closes the dedicated connection."""
    service = _make_service()
    conn = _make_conn(advisory_result=True)

    with patch("psycopg.connect", return_value=conn):
        service.try_acquire_leadership()

    service.release_leadership()

    conn.close.assert_called_once()
    assert service._lock_conn is None


def test_release_sets_is_leader_false():
    """AC3: After release, is_leader is False."""
    service = _make_service()
    conn = _make_conn(advisory_result=True)

    with patch("psycopg.connect", return_value=conn):
        service.try_acquire_leadership()

    assert service.is_leader is True
    service.release_leadership()
    assert service.is_leader is False


def test_release_is_idempotent_when_not_leader():
    """AC3: release_leadership is safe to call when not holding the lock."""
    service = _make_service()
    # Should not raise
    service.release_leadership()
    assert service.is_leader is False


# ---------------------------------------------------------------------------
# AC4: is_leader property
# ---------------------------------------------------------------------------


def test_is_leader_false_initially():
    """AC4: is_leader is False before any acquisition attempt."""
    service = _make_service()
    assert service.is_leader is False


def test_is_leader_true_after_acquire():
    """AC4: is_leader is True after successful acquisition."""
    service = _make_service()
    conn = _make_conn(advisory_result=True)

    with patch("psycopg.connect", return_value=conn):
        service.try_acquire_leadership()

    assert service.is_leader is True


def test_is_leader_false_after_release():
    """AC4: is_leader is False after release."""
    service = _make_service()
    conn = _make_conn(advisory_result=True)

    with patch("psycopg.connect", return_value=conn):
        service.try_acquire_leadership()

    service.release_leadership()
    assert service.is_leader is False


# ---------------------------------------------------------------------------
# AC5: Monitor thread lifecycle
# ---------------------------------------------------------------------------


def test_start_monitoring_starts_thread():
    """AC5: start_monitoring spawns a background daemon thread."""
    service = _make_service()
    conn = _make_conn(advisory_result=False)  # never becomes leader in this test

    with patch("psycopg.connect", return_value=conn):
        service.start_monitoring(check_interval=60)
        try:
            assert service._monitor_thread is not None
            assert service._monitor_thread.is_alive()
            assert service._monitor_thread.daemon is True
        finally:
            service.stop_monitoring()


def test_stop_monitoring_stops_thread():
    """AC5: stop_monitoring signals stop and joins the thread."""
    service = _make_service()
    conn = _make_conn(advisory_result=False)

    with patch("psycopg.connect", return_value=conn):
        service.start_monitoring(check_interval=60)
        service.stop_monitoring()

    # Thread should have exited
    if service._monitor_thread is not None:
        assert not service._monitor_thread.is_alive()


def test_start_monitoring_idempotent():
    """AC5: Calling start_monitoring twice does not spawn a second thread."""
    service = _make_service()
    conn = _make_conn(advisory_result=False)

    with patch("psycopg.connect", return_value=conn):
        service.start_monitoring(check_interval=60)
        first_thread = service._monitor_thread
        service.start_monitoring(check_interval=60)  # second call — should be no-op
        second_thread = service._monitor_thread

    try:
        assert first_thread is second_thread
    finally:
        service.stop_monitoring()


# ---------------------------------------------------------------------------
# AC6: Monitor thread calls try_acquire periodically
# ---------------------------------------------------------------------------


def test_monitor_calls_try_acquire_at_least_twice():
    """
    AC6: When not leader, the monitor loop calls try_acquire periodically.
    We use a very short interval (0.05s) and wait for at least two calls.
    """
    service = _make_service()
    call_count = [0]
    acquire_event = threading.Event()

    original_try = service.try_acquire_leadership

    def counting_try():
        call_count[0] += 1
        if call_count[0] >= 2:
            acquire_event.set()
        return False  # Never become leader in this test

    service.try_acquire_leadership = counting_try

    service.start_monitoring(check_interval=1)
    try:
        triggered = acquire_event.wait(timeout=5)
        assert triggered, f"try_acquire called only {call_count[0]} time(s) in 5s"
        assert call_count[0] >= 2
    finally:
        service.stop_monitoring()
        service.try_acquire_leadership = original_try


# ---------------------------------------------------------------------------
# AC7: on_become_leader callback
# ---------------------------------------------------------------------------


def test_on_become_leader_called_on_first_acquire():
    """AC7: on_become_leader is invoked when transitioning from non-leader to leader."""
    service = _make_service()
    conn = _make_conn(advisory_result=True)
    on_become = MagicMock()
    on_lose = MagicMock()

    service.register_leader_callbacks(on_become, on_lose)

    with patch("psycopg.connect", return_value=conn):
        service.try_acquire_leadership()

    on_become.assert_called_once()
    on_lose.assert_not_called()


def test_on_become_leader_not_called_if_already_leader():
    """AC9: on_become_leader is NOT fired on re-entrant try_acquire when already leader."""
    service = _make_service()
    conn1 = _make_conn(advisory_result=True)
    conn2 = _make_conn(advisory_result=True)
    on_become = MagicMock()
    on_lose = MagicMock()

    service.register_leader_callbacks(on_become, on_lose)

    with patch("psycopg.connect", side_effect=[conn1, conn2]):
        service.try_acquire_leadership()  # first — fires callback
        service.try_acquire_leadership()  # second — already leader, no callback

    on_become.assert_called_once()


# ---------------------------------------------------------------------------
# AC8: on_lose_leadership callback
# ---------------------------------------------------------------------------


def test_on_lose_leadership_called_on_release():
    """AC8: on_lose_leadership is invoked when release_leadership is called."""
    service = _make_service()
    conn = _make_conn(advisory_result=True)
    on_become = MagicMock()
    on_lose = MagicMock()

    service.register_leader_callbacks(on_become, on_lose)

    with patch("psycopg.connect", return_value=conn):
        service.try_acquire_leadership()

    service.release_leadership()

    on_lose.assert_called_once()


def test_on_lose_leadership_not_called_when_was_not_leader():
    """AC8: on_lose_leadership is NOT called if this node was never leader."""
    service = _make_service()
    on_become = MagicMock()
    on_lose = MagicMock()

    service.register_leader_callbacks(on_become, on_lose)
    service.release_leadership()  # node was never leader

    on_lose.assert_not_called()


# ---------------------------------------------------------------------------
# AC10: Connection loss detected by monitor triggers on_lose_leadership
# ---------------------------------------------------------------------------


def test_monitor_detects_lost_connection_and_fires_callback():
    """
    AC10: If the lock connection dies while this node is leader, the monitor
    detects it via the ping, fires on_lose_leadership, and attempts re-election.
    """
    service = _make_service()

    on_become = MagicMock()
    on_lose = MagicMock()
    service.register_leader_callbacks(on_become, on_lose)

    # Simulate being leader with a now-dead connection
    dead_conn = MagicMock()
    dead_cur = MagicMock()
    dead_cur.__enter__ = MagicMock(return_value=dead_cur)
    dead_cur.__exit__ = MagicMock(return_value=False)
    dead_cur.execute.side_effect = Exception("connection lost")
    dead_conn.cursor.return_value = dead_cur

    # Force initial state: we are leader with the dead connection
    service._is_leader = True
    service._lock_conn = dead_conn

    # After detecting the loss, try_acquire will be called — keep it failing
    # so we don't re-acquire in this test
    lose_event = threading.Event()
    _original_lose = service._on_lose_leadership  # noqa: F841

    def lose_callback():
        on_lose()
        lose_event.set()

    service._on_lose_leadership = lose_callback

    # Patch try_acquire to not actually connect
    with patch.object(service, "try_acquire_leadership", return_value=False):
        service.start_monitoring(check_interval=1)
        triggered = lose_event.wait(timeout=5)
        service.stop_monitoring()

    assert triggered, "on_lose_leadership was not fired within 5 seconds"
    on_lose.assert_called_once()


# ---------------------------------------------------------------------------
# LOCK_ID constant
# ---------------------------------------------------------------------------


def test_lock_id_matches_module_constant():
    """The class attribute LOCK_ID must equal the module-level _LOCK_ID."""
    assert LeaderElectionService.LOCK_ID == _LOCK_ID


def test_lock_id_value():
    """LOCK_ID encodes 'CIDX_LDR' as a big-endian 64-bit integer."""
    expected = int.from_bytes(b"CIDX_LDR", "big")
    assert _LOCK_ID == expected
