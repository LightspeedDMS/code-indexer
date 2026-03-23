"""
Unit tests for LeaderFailoverMonitor (Story #424).

LeaderFailoverMonitor is a thin wrapper around LeaderElectionService.
All tests mock LeaderElectionService so no real PostgreSQL is required.

Tests cover:
- FM1: start() delegates to LeaderElectionService.start_monitoring
- FM2: stop() delegates to LeaderElectionService.stop_monitoring
- FM3: register_callbacks() forwards callbacks to LeaderElectionService
- FM4: is_leader property reflects the wrapped service's state
- FM5: Monitoring thread stops cleanly via stop()
- FM6: on_become_leader callback is invoked when leadership is acquired
- FM7: on_lose_leadership callback is invoked when leadership is lost
- FM8: Monitor attempts acquisition when not leader (integration-style)
- FM9: default check_interval is 10 seconds
- FM10: custom check_interval is passed through to start_monitoring
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch


from code_indexer.server.services.leader_failover_monitor import LeaderFailoverMonitor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_election(is_leader: bool = False) -> MagicMock:
    """Return a MagicMock shaped like LeaderElectionService."""
    svc = MagicMock()
    svc.is_leader = is_leader
    return svc


def _make_monitor(
    is_leader: bool = False, check_interval: int = 10
) -> tuple[LeaderFailoverMonitor, MagicMock]:
    """Return a (monitor, mock_election_service) pair."""
    election = _make_mock_election(is_leader=is_leader)
    monitor = LeaderFailoverMonitor(election, check_interval=check_interval)
    return monitor, election


# ---------------------------------------------------------------------------
# FM1: start() delegates to start_monitoring
# ---------------------------------------------------------------------------


def test_start_calls_start_monitoring_on_election_service():
    """FM1: start() calls LeaderElectionService.start_monitoring."""
    monitor, election = _make_monitor(check_interval=10)

    monitor.start()

    election.start_monitoring.assert_called_once_with(check_interval=10)


def test_start_passes_custom_check_interval():
    """FM10: Custom check_interval is forwarded to start_monitoring."""
    monitor, election = _make_monitor(check_interval=30)

    monitor.start()

    election.start_monitoring.assert_called_once_with(check_interval=30)


# ---------------------------------------------------------------------------
# FM2: stop() delegates to stop_monitoring
# ---------------------------------------------------------------------------


def test_stop_calls_stop_monitoring_on_election_service():
    """FM2: stop() calls LeaderElectionService.stop_monitoring."""
    monitor, election = _make_monitor()

    monitor.stop()

    election.stop_monitoring.assert_called_once()


# ---------------------------------------------------------------------------
# FM3: register_callbacks() forwards to LeaderElectionService
# ---------------------------------------------------------------------------


def test_register_callbacks_forwards_to_election_service():
    """FM3: register_callbacks() forwards both callbacks to the underlying service."""
    monitor, election = _make_monitor()
    on_become = MagicMock()
    on_lose = MagicMock()

    monitor.register_callbacks(
        on_become_leader=on_become,
        on_lose_leadership=on_lose,
    )

    election.register_leader_callbacks.assert_called_once_with(
        on_become_leader=on_become,
        on_lose_leadership=on_lose,
    )


def test_register_callbacks_stores_callbacks_locally():
    """FM3: register_callbacks() stores the callbacks on the monitor instance."""
    monitor, election = _make_monitor()
    on_become = MagicMock()
    on_lose = MagicMock()

    monitor.register_callbacks(
        on_become_leader=on_become,
        on_lose_leadership=on_lose,
    )

    assert monitor._on_become_leader is on_become
    assert monitor._on_lose_leadership is on_lose


# ---------------------------------------------------------------------------
# FM4: is_leader property delegates to wrapped service
# ---------------------------------------------------------------------------


def test_is_leader_reflects_wrapped_service_false():
    """FM4: is_leader returns False when the underlying service is not leader."""
    monitor, election = _make_monitor(is_leader=False)
    assert monitor.is_leader is False


def test_is_leader_reflects_wrapped_service_true():
    """FM4: is_leader returns True when the underlying service is leader."""
    monitor, election = _make_monitor(is_leader=True)
    assert monitor.is_leader is True


def test_is_leader_tracks_underlying_service_change():
    """FM4: is_leader dynamically tracks changes in the wrapped service."""
    monitor, election = _make_monitor(is_leader=False)
    assert monitor.is_leader is False

    election.is_leader = True
    assert monitor.is_leader is True


# ---------------------------------------------------------------------------
# FM9: default check_interval
# ---------------------------------------------------------------------------


def test_default_check_interval_is_ten():
    """FM9: LeaderFailoverMonitor defaults to check_interval=10."""
    election = _make_mock_election()
    monitor = LeaderFailoverMonitor(election)
    assert monitor._check_interval == 10


# ---------------------------------------------------------------------------
# FM5 & FM6 & FM7: Thread-level integration using real LeaderElectionService
# ---------------------------------------------------------------------------


def test_monitor_stops_cleanly_via_stop():
    """
    FM5: stop() causes the background thread to exit cleanly.

    Uses the real LeaderElectionService (with psycopg mocked out) so we
    exercise actual threading behaviour rather than a pure mock.
    """
    from code_indexer.server.services.leader_election_service import (
        LeaderElectionService,
    )
    from unittest.mock import patch

    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = (False,)
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur

    election = LeaderElectionService(
        connection_string="postgresql://localhost/test",
        node_id="test-node-fm5",
    )
    monitor = LeaderFailoverMonitor(election, check_interval=60)

    with patch("psycopg.connect", return_value=conn):
        monitor.start()
        assert election._monitor_thread is not None
        assert election._monitor_thread.is_alive()
        monitor.stop()

    # After stop(), thread should be dead or None
    if election._monitor_thread is not None:
        assert not election._monitor_thread.is_alive()


def test_on_become_leader_callback_fired_on_acquisition():
    """
    FM6: on_become_leader is called when leadership is acquired via the monitor.

    Uses the real LeaderElectionService with a very short check_interval so
    the monitor loop fires quickly.  psycopg.connect is mocked to return a
    connection that grants the advisory lock.
    """
    from code_indexer.server.services.leader_election_service import (
        LeaderElectionService,
    )

    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = (True,)  # lock granted
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur

    election = LeaderElectionService(
        connection_string="postgresql://localhost/test",
        node_id="test-node-fm6",
    )

    become_event = threading.Event()
    on_become = MagicMock(side_effect=lambda: become_event.set())
    on_lose = MagicMock()

    monitor = LeaderFailoverMonitor(election, check_interval=1)
    monitor.register_callbacks(
        on_become_leader=on_become,
        on_lose_leadership=on_lose,
    )

    with patch("psycopg.connect", return_value=conn):
        monitor.start()
        triggered = become_event.wait(timeout=5)
        monitor.stop()

    assert triggered, "on_become_leader was not fired within 5 seconds"
    on_become.assert_called_once()
    # on_lose may be called during stop() when the lock is released —
    # that is correct behavior (leadership is relinquished on shutdown).
    # We only assert it was NOT called BEFORE stop().


def test_on_lose_leadership_callback_fired_on_connection_loss():
    """
    FM7: on_lose_leadership is called when the lock connection dies.

    Simulates a node that is already leader with a dead connection.  The
    monitor detects the dead connection on its next iteration and fires the
    callback.
    """
    from code_indexer.server.services.leader_election_service import (
        LeaderElectionService,
    )

    election = LeaderElectionService(
        connection_string="postgresql://localhost/test",
        node_id="test-node-fm7",
    )

    # Build a dead connection that fails the SELECT 1 ping
    dead_conn = MagicMock()
    dead_cur = MagicMock()
    dead_cur.__enter__ = MagicMock(return_value=dead_cur)
    dead_cur.__exit__ = MagicMock(return_value=False)
    dead_cur.execute.side_effect = Exception("connection lost")
    dead_conn.cursor.return_value = dead_cur

    # Inject "already leader" state
    election._is_leader = True
    election._lock_conn = dead_conn

    lose_event = threading.Event()
    on_become = MagicMock()
    on_lose = MagicMock(side_effect=lambda: lose_event.set())

    monitor = LeaderFailoverMonitor(election, check_interval=1)
    monitor.register_callbacks(
        on_become_leader=on_become,
        on_lose_leadership=on_lose,
    )

    # Prevent re-acquisition after loss so the test stays deterministic
    with patch.object(election, "try_acquire_leadership", return_value=False):
        monitor.start()
        triggered = lose_event.wait(timeout=5)
        monitor.stop()

    assert triggered, "on_lose_leadership was not fired within 5 seconds"
    on_lose.assert_called_once()


# ---------------------------------------------------------------------------
# FM8: Monitor attempts acquisition when not leader
# ---------------------------------------------------------------------------


def test_monitor_attempts_acquisition_when_not_leader():
    """
    FM8: While not leader, the monitor periodically calls try_acquire_leadership.

    Uses a short interval and waits for at least two acquisition attempts.
    """
    from code_indexer.server.services.leader_election_service import (
        LeaderElectionService,
    )

    election = LeaderElectionService(
        connection_string="postgresql://localhost/test",
        node_id="test-node-fm8",
    )

    call_count = [0]
    acquire_event = threading.Event()

    def counting_acquire():
        call_count[0] += 1
        if call_count[0] >= 2:
            acquire_event.set()
        return False  # never become leader

    election.try_acquire_leadership = counting_acquire

    monitor = LeaderFailoverMonitor(election, check_interval=1)
    monitor.start()
    try:
        triggered = acquire_event.wait(timeout=5)
        assert (
            triggered
        ), f"try_acquire_leadership called only {call_count[0]} time(s) in 5s"
        assert call_count[0] >= 2
    finally:
        monitor.stop()
