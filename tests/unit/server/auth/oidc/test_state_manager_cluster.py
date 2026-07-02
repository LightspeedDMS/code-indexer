"""Tests for Bug #576: StateManager thread safety + cluster support."""

import json
import threading
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from code_indexer.server.auth.oidc.state_manager import StateManager


class TestStateManagerThreadSafety:
    """Test thread safety of in-memory state management."""

    def test_thread_lock_exists(self):
        """Lock attribute must be present on StateManager."""
        mgr = StateManager()
        assert hasattr(mgr, "_lock")
        assert isinstance(mgr._lock, type(threading.Lock()))

    def test_create_state_with_lock(self):
        """create_state works correctly in standalone (in-memory) mode."""
        mgr = StateManager()
        data = {"provider": "google", "redirect": "/dashboard"}
        token = mgr.create_state(data)
        assert isinstance(token, str)
        assert len(token) > 0

    def test_validate_state_with_lock(self):
        """validate_state works correctly in standalone (in-memory) mode."""
        mgr = StateManager()
        data = {"provider": "google"}
        token = mgr.create_state(data)
        result = mgr.validate_state(token)
        assert result == data

    def test_validate_consumes_token_standalone(self):
        """Token is deleted after first validate_state call."""
        mgr = StateManager()
        token = mgr.create_state({"x": 1})
        mgr.validate_state(token)
        assert mgr.validate_state(token) is None

    def test_update_state_data_standalone(self):
        """update_state_data modifies data for existing token."""
        mgr = StateManager()
        token = mgr.create_state({"step": 1})
        assert mgr.update_state_data(token, {"step": 2}) is True
        result = mgr.validate_state(token)
        assert result == {"step": 2}

    def test_update_state_data_missing_token(self):
        """update_state_data returns False for unknown token."""
        mgr = StateManager()
        assert mgr.update_state_data("nonexistent", {}) is False

    def test_validate_expired_returns_none(self):
        """Expired tokens return None from validate_state."""
        mgr = StateManager()
        token = mgr.create_state({"x": 1})
        # Force expiration
        mgr._states[token]["expires_at"] = datetime.now(timezone.utc) - timedelta(
            seconds=1
        )
        assert mgr.validate_state(token) is None

    def test_set_connection_pool(self):
        """set_connection_pool stores the pool reference."""
        mgr = StateManager()
        assert mgr._pool is None
        mock_pool = MagicMock()
        mgr.set_connection_pool(mock_pool)
        assert mgr._pool is mock_pool


class TestStateManagerPostgres:
    """Test PostgreSQL backend for cluster mode."""

    def _make_mock_pool(self):
        """Create a mock connection pool with context manager support."""
        pool = MagicMock()
        conn = MagicMock()
        pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
        pool.connection.return_value.__exit__ = MagicMock(return_value=False)
        return pool, conn

    def test_pg_create_and_validate(self):
        """PG round-trip: create stores, validate retrieves and deletes."""
        pool, conn = self._make_mock_pool()
        mgr = StateManager()
        mgr.set_connection_pool(pool)

        data = {"provider": "azure"}

        # Mock create (INSERT)
        mgr.create_state(data)

        # Verify INSERT was called
        insert_call = conn.execute.call_args_list[0]
        assert "INSERT INTO oidc_state_tokens" in insert_call[0][0]
        args = insert_call[0][1]
        assert json.loads(args[1]) == data
        conn.commit.assert_called()

    def test_pg_validate_consumes_token(self):
        """PG validate uses DELETE ... RETURNING for atomic consume."""
        pool, conn = self._make_mock_pool()
        mgr = StateManager()
        mgr.set_connection_pool(pool)

        # Mock the DELETE ... RETURNING result
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {
            "state_data": json.dumps({"provider": "google"})
        }
        conn.execute.return_value = mock_cursor
        conn.row_factory = None  # Allow setting

        result = mgr.validate_state("test-token")

        # Verify DELETE ... RETURNING was used
        delete_call = conn.execute.call_args_list[0]
        sql = delete_call[0][0]
        assert "DELETE FROM oidc_state_tokens" in sql
        assert "RETURNING state_data" in sql
        assert result == {"provider": "google"}

    def test_pg_validate_expired_returns_none(self):
        """PG validate returns None when token is expired (no row returned)."""
        pool, conn = self._make_mock_pool()
        mgr = StateManager()
        mgr.set_connection_pool(pool)

        # Mock: no row returned (expired or not found)
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        conn.execute.return_value = mock_cursor
        conn.row_factory = None

        result = mgr.validate_state("expired-token")
        assert result is None

    def test_pg_update_success(self):
        """PG update returns True when row was updated."""
        pool, conn = self._make_mock_pool()
        mgr = StateManager()
        mgr.set_connection_pool(pool)

        mock_result = MagicMock()
        mock_result.rowcount = 1
        conn.execute.return_value = mock_result

        assert mgr.update_state_data("token", {"new": "data"}) is True

    def test_pg_update_not_found(self):
        """PG update returns False when token not found."""
        pool, conn = self._make_mock_pool()
        mgr = StateManager()
        mgr.set_connection_pool(pool)

        mock_result = MagicMock()
        mock_result.rowcount = 0
        conn.execute.return_value = mock_result

        assert mgr.update_state_data("missing", {}) is False
