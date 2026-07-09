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
import time as _time
from contextlib import contextmanager
from typing import Generator

import psycopg  # noqa: F401  (imported for type annotations in callers)
from psycopg_pool import ConnectionPool as _PsycopgPool

logger = logging.getLogger(__name__)

# Bug #545: Warn when connection acquisition takes longer than this (seconds).
_SLOW_ACQUISITION_THRESHOLD = 5.0


class ConnectionPool:
    """
    Thin wrapper around psycopg_pool.ConnectionPool providing a simplified
    context-manager interface for obtaining connections.

    Bug #545: Supports named pools (e.g., 'critical', 'general') with
    configurable timeouts and slow-acquisition warnings.
    """

    def __init__(
        self,
        connection_string: str,
        min_size: int = 1,
        max_size: int = 20,
        timeout: float = 30.0,
        name: str = "general",
    ) -> None:
        """
        Initialize the connection pool.

        Args:
            connection_string: PostgreSQL DSN.
            min_size: Minimum number of pooled connections.
            max_size: Maximum number of pooled connections.
            timeout: Max seconds to wait for a connection (Bug #545).
            name: Pool name for logging (e.g., 'general', 'critical').
        """
        self._connection_string = connection_string
        self._name = name
        self._timeout = timeout
        self._pool = _PsycopgPool(
            connection_string,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            open=True,
        )

    @contextmanager
    def connection(self) -> Generator:
        """
        Obtain a connection from the pool.

        Yields a psycopg connection.  The caller must NOT close the connection;
        it is returned to the pool automatically on context exit.

        Bug #545: Logs a WARNING if acquisition takes longer than 5 seconds,
        indicating potential pool starvation.
        """
        start = _time.monotonic()
        with self._pool.connection() as conn:
            elapsed = _time.monotonic() - start
            if elapsed > _SLOW_ACQUISITION_THRESHOLD:
                logger.warning(
                    "Slow connection acquisition on '%s' pool: %.2fs "
                    "(threshold: %.1fs). Possible pool starvation.",
                    self._name,
                    elapsed,
                    _SLOW_ACQUISITION_THRESHOLD,
                )
            yield conn

    def close(self) -> None:
        """Close the pool and all underlying connections."""
        self._pool.close()
