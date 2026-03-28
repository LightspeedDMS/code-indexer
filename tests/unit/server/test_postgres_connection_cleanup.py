"""
Tests for PostgreSQL connection pool cleanup on process exit (Bug #567).

Verifies that:
1. service_init.py registers an atexit handler for pool cleanup when postgres mode is used.
2. The ConnectionPool.close() method is callable and closes the underlying pool.
3. The cleanup function is safe to call even when no pool exists (no-op).
4. The atexit handler is registered via register_postgres_pool_atexit_cleanup().

These tests do NOT require a real PostgreSQL connection -- they mock the pool
and verify the cleanup wiring only.
"""

import importlib
from unittest.mock import MagicMock, patch


def _load_service_init():
    """Load service_init module directly to avoid circular __init__.py imports."""
    return importlib.import_module("code_indexer.server.startup.service_init")


class TestConnectionPoolClose:
    """Unit tests for ConnectionPool.close()."""

    def test_close_calls_underlying_pool_close(self):
        """ConnectionPool.close() must call _pool.close() on the psycopg pool."""
        mock_psycopg_pool = MagicMock()
        with patch(
            "code_indexer.server.storage.postgres.connection_pool._PsycopgPool",
            return_value=mock_psycopg_pool,
        ):
            # Local import -- ConnectionPool is in a separate module without
            # circular import issues.
            from code_indexer.server.storage.postgres.connection_pool import (
                ConnectionPool,
            )

            pool = ConnectionPool("postgresql://localhost/test")
            pool.close()
            mock_psycopg_pool.close.assert_called_once()


class TestCleanupPostgresPool:
    """Unit tests for the _cleanup_postgres_pool function in service_init."""

    def test_cleanup_noop_when_no_pool_registered(self):
        """_cleanup_postgres_pool() must not raise when called with no pool registered."""
        svc_init = _load_service_init()
        original = svc_init._postgres_pool_for_cleanup
        svc_init._postgres_pool_for_cleanup = None
        try:
            svc_init._cleanup_postgres_pool()  # Must not raise
        finally:
            svc_init._postgres_pool_for_cleanup = original

    def test_cleanup_calls_close_on_registered_pool(self):
        """_cleanup_postgres_pool() calls .close() on the registered pool."""
        svc_init = _load_service_init()
        mock_pool = MagicMock()
        original = svc_init._postgres_pool_for_cleanup
        svc_init._postgres_pool_for_cleanup = mock_pool
        try:
            svc_init._cleanup_postgres_pool()
            mock_pool.close.assert_called_once()
        finally:
            svc_init._postgres_pool_for_cleanup = original

    def test_cleanup_clears_reference_after_close(self):
        """_cleanup_postgres_pool() sets the module-level ref to None after closing."""
        svc_init = _load_service_init()
        mock_pool = MagicMock()
        original = svc_init._postgres_pool_for_cleanup
        svc_init._postgres_pool_for_cleanup = mock_pool
        try:
            svc_init._cleanup_postgres_pool()
            assert svc_init._postgres_pool_for_cleanup is None
        finally:
            svc_init._postgres_pool_for_cleanup = original

    def test_cleanup_safe_when_close_raises(self):
        """_cleanup_postgres_pool() must not propagate exceptions from pool.close()."""
        svc_init = _load_service_init()
        mock_pool = MagicMock()
        mock_pool.close.side_effect = RuntimeError("pool already closed")
        original = svc_init._postgres_pool_for_cleanup
        svc_init._postgres_pool_for_cleanup = mock_pool
        try:
            svc_init._cleanup_postgres_pool()  # Must not raise
        finally:
            svc_init._postgres_pool_for_cleanup = original


class TestRegisterPostgresPoolAtexitCleanup:
    """Unit tests for register_postgres_pool_atexit_cleanup()."""

    def test_registers_pool_in_module_state(self):
        """register_postgres_pool_atexit_cleanup() stores pool in module-level var."""
        svc_init = _load_service_init()
        mock_pool = MagicMock()
        original = svc_init._postgres_pool_for_cleanup
        try:
            svc_init.register_postgres_pool_atexit_cleanup(mock_pool)
            assert svc_init._postgres_pool_for_cleanup is mock_pool
        finally:
            svc_init._postgres_pool_for_cleanup = original

    def test_atexit_handler_is_registered(self):
        """register_postgres_pool_atexit_cleanup() registers _cleanup with atexit."""
        svc_init = _load_service_init()
        mock_pool = MagicMock()
        original = svc_init._postgres_pool_for_cleanup

        registered_callbacks = []

        def capture_register(fn, *args, **kwargs):
            registered_callbacks.append(fn)

        with patch("atexit.register", side_effect=capture_register):
            svc_init.register_postgres_pool_atexit_cleanup(mock_pool)

        assert svc_init._cleanup_postgres_pool in registered_callbacks, (
            "_cleanup_postgres_pool must be registered with atexit.register()"
        )

        svc_init._postgres_pool_for_cleanup = original

    def test_register_replaces_previous_pool(self):
        """Calling register twice updates the stored pool."""
        svc_init = _load_service_init()
        mock_pool_1 = MagicMock()
        mock_pool_2 = MagicMock()
        original = svc_init._postgres_pool_for_cleanup
        try:
            svc_init.register_postgres_pool_atexit_cleanup(mock_pool_1)
            svc_init.register_postgres_pool_atexit_cleanup(mock_pool_2)
            assert svc_init._postgres_pool_for_cleanup is mock_pool_2
        finally:
            svc_init._postgres_pool_for_cleanup = original
