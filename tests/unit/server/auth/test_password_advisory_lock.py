"""
Tests for Story #538: PostgreSQL advisory locks for password changes.

Verifies that PasswordChangeConcurrencyProtection uses pg_try_advisory_lock
when a connection pool is set, and falls back to file locks when not.
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.concurrency_protection import (
    ConcurrencyConflictError,
    PasswordChangeConcurrencyProtection,
)


def _make_protection_with_pool():
    """Create protection instance with mocked PG pool."""
    prot = PasswordChangeConcurrencyProtection(lock_dir="/tmp/test-locks")
    mock_pool = MagicMock()
    prot.set_connection_pool(mock_pool)
    return prot, mock_pool


def _setup_pool_lock_result(pool, acquired=True):
    """Configure pool mock to return lock acquisition result."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (acquired,)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    mock_cm = MagicMock()
    mock_cm.__enter__ = MagicMock(return_value=mock_conn)
    mock_cm.__exit__ = MagicMock(return_value=False)
    pool.connection.return_value = mock_cm

    return mock_cursor


class TestAdvisoryLockAcquire:
    """Story #538: Advisory lock acquisition."""

    def test_acquires_lock_successfully(self):
        """pg_try_advisory_lock returns True = lock acquired."""
        prot, pool = _make_protection_with_pool()
        _setup_pool_lock_result(pool, acquired=True)

        with prot.acquire_password_change_lock("alice") as locked:
            assert locked is True

    def test_raises_conflict_when_lock_held(self):
        """pg_try_advisory_lock returns False = conflict."""
        prot, pool = _make_protection_with_pool()
        _setup_pool_lock_result(pool, acquired=False)

        with pytest.raises(ConcurrencyConflictError, match="already in progress"):
            with prot.acquire_password_change_lock("alice"):
                pass

    def test_unlock_called_in_finally(self):
        """pg_advisory_unlock must be called after yield."""
        prot, pool = _make_protection_with_pool()
        cursor = _setup_pool_lock_result(pool, acquired=True)

        with prot.acquire_password_change_lock("alice"):
            pass

        calls = [str(c) for c in cursor.execute.call_args_list]
        assert any("pg_advisory_unlock" in c for c in calls)


class TestSetConnectionPool:
    """Story #538: Pool configuration."""

    def test_set_pool_enables_advisory_mode(self):
        """After set_connection_pool, advisory locks are used."""
        prot = PasswordChangeConcurrencyProtection(lock_dir="/tmp/test-locks")
        mock_pool = MagicMock()
        prot.set_connection_pool(mock_pool)

        _setup_pool_lock_result(mock_pool, acquired=True)
        with prot.acquire_password_change_lock("alice"):
            pass

        # Verify PG SQL was called (advisory lock behavior)
        mock_pool.connection.assert_called()

    def test_file_lock_used_without_pool(self):
        """Without pool, _acquire_file_lock is invoked."""
        prot = PasswordChangeConcurrencyProtection(lock_dir="/tmp/test-locks")

        with patch.object(prot, "_acquire_file_lock") as mock_file_lock:
            mock_file_lock.return_value = iter([True])

            with prot.acquire_password_change_lock("alice"):
                pass

            mock_file_lock.assert_called_once_with("alice")


class TestIsUserLocked:
    """Story #538: Lock status check."""

    def test_advisory_check_returns_false_when_not_locked(self):
        """is_user_locked returns False when lock can be acquired and released."""
        prot, pool = _make_protection_with_pool()

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (True,)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        assert prot.is_user_locked("alice") is False

    def test_advisory_check_returns_true_when_locked(self):
        """is_user_locked returns True when lock cannot be acquired."""
        prot, pool = _make_protection_with_pool()

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (False,)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        assert prot.is_user_locked("alice") is True
