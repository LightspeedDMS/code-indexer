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
from typing import TYPE_CHECKING, Any, Generator, Optional

if TYPE_CHECKING:
    # Bug #1468: psycopg is a heavy PostgreSQL-only dependency that
    # previously loaded unconditionally merely by importing this module
    # (which happens transitively from FilesystemVectorStore, including pure
    # CLI/solo usage with no PostgreSQL need at all). Type-only here.
    import psycopg  # noqa: F401  (type annotations only)

logger = logging.getLogger(__name__)

# Bug #545: Warn when connection acquisition takes longer than this (seconds).
_SLOW_ACQUISITION_THRESHOLD = 5.0

# Bug #1468: module-level sentinel, deliberately None (not yet imported) so
# merely importing this module never forces psycopg_pool to load. Kept as a
# real module attribute (not folded entirely into a local import) so the
# pre-existing test suite's `unittest.mock.patch(
# "...connection_pool._PsycopgPool")` continues to work: ConnectionPool.
# __init__ reads this SAME module-global name, so a test's patched Mock is
# picked up transparently -- the real `psycopg_pool.ConnectionPool` is only
# ever imported lazily, on first non-mocked construction, and cached back
# here for subsequent calls in the same process. Typed as Optional[Any]
# (not the real psycopg_pool.ConnectionPool[...] generic type) specifically
# so it can hold either None or a plain class object without a mypy
# "Cannot assign to a type" conflict.
_PsycopgPool: Optional[Any] = None


def _configure_session(conn: Any) -> None:
    """Pin every pooled connection's session TimeZone to UTC.

    Bug #1663: without this, a PostgreSQL session's configured timezone can
    differ from the application process's local timezone, and naive
    datetimes written/read by application code would be silently
    (mis)interpreted according to whichever timezone happened to be
    configured. Application code (e.g. diagnostics_service.py) now writes
    timezone-AWARE UTC values specifically to be robust to this, but pinning
    the session explicitly is cheap defense-in-depth at this one shared
    choke point every PostgreSQL backend in this project already goes
    through -- it removes the hazard for any future naive-datetime write
    site too, not just the one this bug was filed against.

    psycopg_pool requires a connection returned from a `configure` callback
    to be left IDLE, never mid-transaction -- confirmed live against a real
    local PostgreSQL instance: without the trailing commit() below, every
    new pooled connection was discarded with "connection left in status
    INTRANS by configure function ...: discarded" and the pool never
    became usable.
    """
    conn.execute("SET TIME ZONE 'UTC'")
    conn.commit()


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
        # Bug #1468: lazy import via the module-global sentinel -- this is
        # the ONLY runtime use site of psycopg_pool in this module, deferred
        # so importing this module (or anything that transitively imports
        # it, e.g. FilesystemVectorStore) does not force psycopg/psycopg_pool
        # to load for callers that never actually construct a ConnectionPool.
        # Reading/caching the SAME module-global name (rather than a purely
        # local import) keeps `unittest.mock.patch("...connection_pool.
        # _PsycopgPool")` working for tests.
        global _PsycopgPool
        if _PsycopgPool is None:
            from psycopg_pool import ConnectionPool as _imported_psycopg_pool

            _PsycopgPool = _imported_psycopg_pool

        self._connection_string = connection_string
        self._name = name
        self._timeout = timeout
        self._pool = _PsycopgPool(
            connection_string,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            configure=_configure_session,
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
