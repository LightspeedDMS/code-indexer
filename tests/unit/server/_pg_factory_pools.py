"""Postgres-mode ``create_git_credential_manager`` tests without a database.

The postgres-mode factory builds a real ``ConnectionPool``, which opens at
construction: psycopg_pool starts a scheduler and worker threads that keep
trying to connect in the background.  These tests only check key derivation,
so they point the pool at a DSN nothing listens on (loopback, port 1: no DNS
lookup, connection refused at once) and close every pool they create.

``FactoryPools`` records the managers a test builds, closes their pools at
teardown and fails if any ``pool-*`` thread started during the test survives.
"""

from __future__ import annotations

import threading
from typing import Any, List, Set

UNREACHABLE_PG_DSN = "postgresql://cidx@127.0.0.1:1/cidx"
_POOL_THREAD_PREFIX = "pool-"  # psycopg_pool names: pool-N-scheduler/-worker-K


def _pool_threads() -> Set[str]:
    return {
        t.name for t in threading.enumerate() if t.name.startswith(_POOL_THREAD_PREFIX)
    }


class FactoryPools:
    """Close the pools of postgres-mode managers; prove no pool thread leaks."""

    def __init__(self) -> None:
        self._before = _pool_threads()
        self._managers: List[Any] = []

    def track(self, manager: Any) -> Any:
        """Register a postgres-mode manager; returns it for inline use."""
        self._managers.append(manager)
        return manager

    def close_and_check(self) -> None:
        for manager in self._managers:
            manager._backend._pool.close()
        leaked = sorted(_pool_threads() - self._before)
        assert not leaked, f"connection-pool threads survived the test: {leaked}"
