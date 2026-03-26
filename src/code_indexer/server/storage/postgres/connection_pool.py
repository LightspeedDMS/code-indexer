"""
PostgreSQL connection pool for CIDX server storage backends.

Story #411: PostgreSQL Connection Pool

Provides a simple psycopg v3 synchronous connection pool.
Backends obtain a connection via the context manager and return it
automatically when the block exits.

Usage:
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    pool = ConnectionPool("postgresql://user:pass@localhost/db")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Generator

try:
    import psycopg
    from psycopg_pool import ConnectionPool as _PsycopgPool
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]
    _PsycopgPool = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


class ConnectionPool:
    """
    Thin wrapper around psycopg_pool.ConnectionPool providing a simplified
    context-manager interface for obtaining connections.

    Falls back gracefully to a single direct connection when psycopg_pool
    is unavailable (useful for testing with plain psycopg mocks).
    """

    def __init__(
        self, connection_string: str, min_size: int = 1, max_size: int = 20
    ) -> None:
        """
        Initialize the connection pool.

        Args:
            connection_string: PostgreSQL DSN.
            min_size: Minimum number of pooled connections.
            max_size: Maximum number of pooled connections.

        Raises:
            ImportError: if psycopg (v3) is not installed.
        """
        if psycopg is None:
            raise ImportError(
                "psycopg (v3) is required for PostgreSQL support. "
                "Install it with: pip install psycopg"
            )
        self._connection_string = connection_string
        if _PsycopgPool is not None:
            self._pool = _PsycopgPool(
                connection_string,
                min_size=min_size,
                max_size=max_size,
                open=True,
            )
            self._direct = None
        else:
            # psycopg_pool not available — fall back to a single direct connection
            logger.warning(
                "psycopg_pool not available; using direct connection (not suitable for production)"
            )
            self._pool = None
            self._direct = psycopg.connect(connection_string)

    @contextmanager
    def connection(self) -> Generator:
        """
        Obtain a connection from the pool (or direct connection fallback).

        Yields a psycopg connection.  The caller must NOT close the connection;
        it is returned to the pool automatically on context exit.
        """
        if self._pool is not None:
            with self._pool.connection() as conn:
                yield conn
        else:
            # Direct connection fallback (single-connection mode)
            yield self._direct

    def close(self) -> None:
        """Close the pool and all underlying connections."""
        if self._pool is not None:
            self._pool.close()
        elif self._direct is not None:
            self._direct.close()
            self._direct = None
