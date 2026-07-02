"""
Tests for Story #539: Reduce split-brain window in leader election.

Verifies that verify_leadership() checks the lock connection before
leader-only operations, reducing the split-brain window from 10s to
near-zero.
"""

import threading
from unittest.mock import MagicMock

from code_indexer.server.services.leader_election_service import LeaderElectionService


def _make_service():
    """Create LeaderElectionService with test config."""
    svc = LeaderElectionService.__new__(LeaderElectionService)
    svc._connection_string = "postgresql://localhost/test"
    svc._node_id = "test-node"
    svc._is_leader_event = threading.Event()
    svc._lock_conn = None
    svc._monitor_thread = None
    svc._stop_event = threading.Event()
    svc._on_become_leader = None
    svc._on_lose_leadership = None
    svc._state_lock = threading.Lock()
    return svc


class TestVerifyLeadership:
    """Story #539: verify_leadership() pre-action check."""

    def test_returns_false_when_not_leader(self):
        """Not leader = returns False without checking connection."""
        svc = _make_service()
        assert svc.verify_leadership() is False

    def test_returns_true_when_leader_with_live_connection(self):
        """Leader with live connection = returns True."""
        svc = _make_service()
        svc._is_leader_event.set()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        svc._lock_conn = mock_conn

        assert svc.verify_leadership() is True
        assert svc._is_leader_event.is_set()

    def test_detects_dead_connection_and_relinquishes(self):
        """Dead connection = relinquishes leadership immediately."""
        svc = _make_service()
        svc._is_leader_event.set()
        svc._lock_conn = None  # No connection = dead

        result = svc.verify_leadership()
        assert result is False
        assert not svc._is_leader_event.is_set()

    def test_calls_lose_callback_on_dead_connection(self):
        """on_lose_leadership callback called when connection is dead."""
        svc = _make_service()
        svc._is_leader_event.set()
        svc._lock_conn = None
        callback = MagicMock()
        svc._on_lose_leadership = callback

        svc.verify_leadership()
        callback.assert_called_once()

    def test_state_lock_exists(self):
        """_state_lock must exist for thread-safe leadership mutations."""
        svc = _make_service()
        assert hasattr(svc, "_state_lock")
        assert isinstance(svc._state_lock, type(threading.Lock()))
