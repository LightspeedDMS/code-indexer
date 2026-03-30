"""
Tests for Bug #579: RefreshTokenManager cross-node token rotation race.

Validates:
1. set_connection_pool() stores the pool reference
2. _distributed_lock() uses threading.Lock when no pool is set (standalone mode)
3. lifespan.py wires RefreshTokenManager with cluster pool
"""

from unittest.mock import MagicMock

import pytest

from code_indexer.server.auth.refresh_token_manager import RefreshTokenManager


@pytest.fixture
def rtm(tmp_path):
    """Create a RefreshTokenManager with a temporary database."""
    jwt_mgr = MagicMock()
    jwt_mgr.token_expiration_minutes = 10
    return RefreshTokenManager(
        jwt_manager=jwt_mgr,
        db_path=str(tmp_path / "refresh_tokens.db"),
    )


class TestSetConnectionPool:
    """Test set_connection_pool stores pool reference."""

    def test_set_connection_pool_stores_pool(self, rtm):
        """Pool attribute is None by default, then set after wiring."""
        assert rtm._pool is None

        fake_pool = MagicMock()
        rtm.set_connection_pool(fake_pool)

        assert rtm._pool is fake_pool


class TestDistributedLockStandalone:
    """Test _distributed_lock uses threading.Lock when no pool is set."""

    def test_distributed_lock_uses_threading_when_no_pool(self, rtm):
        """In standalone mode (no pool), _distributed_lock acquires threading.Lock."""
        assert rtm._pool is None

        lock_acquired = False

        with rtm._distributed_lock():
            # Verify that the threading lock is currently held
            # by trying a non-blocking acquire (should fail)
            lock_acquired = not rtm._lock.acquire(blocking=False)
            if not lock_acquired:
                # We got the lock, which means _distributed_lock
                # did NOT hold it -- release and fail
                rtm._lock.release()

        assert lock_acquired, (
            "_distributed_lock should hold self._lock in standalone mode"
        )


class TestLifespanWiresRefreshTokenManager:
    """Structural test: lifespan.py contains wiring for RefreshTokenManager."""

    def test_lifespan_wires_refresh_token_manager(self):
        """lifespan.py must wire set_connection_pool for refresh_token_manager."""
        import inspect
        from code_indexer.server.startup import lifespan as lifespan_mod

        source = inspect.getsource(lifespan_mod)

        assert "refresh_token_manager" in source, (
            "lifespan.py must reference refresh_token_manager"
        )
        assert "set_connection_pool" in source, (
            "lifespan.py must call set_connection_pool"
        )
