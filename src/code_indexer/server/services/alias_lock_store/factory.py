"""AliasLockStoreFactory: production backend selection for the DB-backed
golden-repo alias lock (Issue #1546 Phase 2).

Backend selection is a CORRECTNESS property, not a convenience setting
(CLAUDE.md's Critical Architecture Invariants): cluster/PostgreSQL mode
MUST use the PostgreSQL store; solo mode MUST use a node-local SQLite
store, dispatched via `is_postgres_storage_mode()`
(server/utils/registry_factory.py) -- THE single authority for this
probe, never re-implemented here. The golden-repos NFS mount is
`vers=3,nolock,hard` -- `nolock` makes byte-range locks client-side-only,
so a SQLite lock file placed there would give each node its own private
lock and silently reproduce the exact split-brain bug this story exists
to eliminate. `default_sqlite_lock_dir()` therefore NEVER derives from
`golden_repos_dir` -- only from an explicit `server_data_dir` or the
`CIDX_DATA_DIR` env var (this project's established node-local-data-dir
convention, see e.g. `server/services/applied_worker_count.py`'s
`_default_data_dir()`).

Cluster mode with no configured DSN fails LOUD (`RuntimeError`) rather
than silently falling back to a node-local SQLite store other cluster
nodes cannot see -- this can only happen from a wiring bug (a
`postgres_dsn` is mandatory for `storage_mode: postgres`), never
legitimate operator configuration.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ...utils.registry_factory import (
    is_postgres_storage_mode,
    is_storage_mode_undetermined,
)

if TYPE_CHECKING:
    from .base import AliasLockStore

_ALIAS_LOCKS_SUBDIR = ("data", "alias_locks")


def default_sqlite_lock_dir(
    server_data_dir: Optional[str] = None,
    *,
    golden_repos_dir: Optional[str] = None,
) -> Path:
    """Node-local directory for the per-alias SQLite lock files. Never
    derives from golden_repos_dir -- see module docstring.

    Codex Fix 1 (containment): when `golden_repos_dir` is supplied, the
    resolved lock dir is validated to NEVER be equal to, or nested under,
    it -- that NFS mount is `vers=3,nolock,hard`, so a SQLite lock file
    placed there gives each node its own private lock, silently
    reproducing the split-brain bug this mechanism exists to eliminate.
    An explicit `server_data_dir` (or an operator misconfiguring
    `CIDX_DATA_DIR`) that resolves at or under `golden_repos_dir` fails
    LOUD here rather than silently placing the lock DB on shared,
    unlocked storage.
    """
    base = (
        Path(server_data_dir)
        if server_data_dir
        else Path(os.environ.get("CIDX_DATA_DIR", str(Path.home() / ".cidx-server")))
    )
    for part in _ALIAS_LOCKS_SUBDIR:
        base = base / part

    if golden_repos_dir is not None:
        resolved_base = base.resolve()
        resolved_golden = Path(golden_repos_dir).resolve()
        try:
            resolved_base.relative_to(resolved_golden)
        except ValueError:
            pass
        else:
            raise RuntimeError(
                f"AliasLockStoreFactory: refusing to place the SQLite "
                f"alias lock directory ({resolved_base}) at or under the "
                f"golden-repos directory ({resolved_golden}) -- that NFS "
                f"mount is vers=3,nolock,hard, so a SQLite lock file "
                f"there would give each node its own private lock, "
                f"invisible to other cluster nodes, and silently "
                f"reproduce the exact split-brain bug this mechanism "
                f"exists to eliminate. Configure a node-local "
                f"server_data_dir or CIDX_DATA_DIR instead."
            )

    return base


class AliasLockStoreFactory:
    """Lazily constructs and caches, per-process, the correct
    AliasLockStore for the CURRENT storage mode. `resolve()` is the
    callable handed to AliasLockCoordinator as its `store_resolver`.
    """

    def __init__(
        self,
        *,
        postgres_dsn: Optional[str] = None,
        server_data_dir: Optional[str] = None,
        golden_repos_dir: Optional[str] = None,
    ) -> None:
        self._postgres_dsn = postgres_dsn
        self._server_data_dir = server_data_dir
        self._golden_repos_dir = golden_repos_dir
        self._lock = threading.Lock()
        self._sqlite_store: Optional["AliasLockStore"] = None
        self._postgres_store: Optional["AliasLockStore"] = None

    def _resolve_sqlite(self) -> "AliasLockStore":
        with self._lock:
            if self._sqlite_store is None:
                from .sqlite_store import SqliteAliasLockStore

                lock_dir = default_sqlite_lock_dir(
                    self._server_data_dir,
                    golden_repos_dir=self._golden_repos_dir,
                )
                self._sqlite_store = SqliteAliasLockStore(lock_dir)
            return self._sqlite_store

    def _resolve_postgres(self) -> "AliasLockStore":
        with self._lock:
            if self._postgres_store is None:
                if not self._postgres_dsn:
                    raise RuntimeError(
                        "AliasLockStoreFactory: cluster (postgres) storage mode "
                        "is active but no postgres_dsn was configured -- "
                        "refusing to silently fall back to a node-local SQLite "
                        "alias lock store, which would be invisible to other "
                        "cluster nodes and would reproduce the exact "
                        "split-brain bug this mechanism exists to eliminate."
                    )
                from .postgres_store import PostgresAliasLockStore

                self._postgres_store = PostgresAliasLockStore(self._postgres_dsn)
            return self._postgres_store

    def resolve(self) -> "AliasLockStore":
        # Codex Fix 1 (most serious): re-evaluated on EVERY call, never
        # cached -- if storage mode is still the genuinely undetermined
        # pending-startup window, refuse to resolve AT ALL rather than
        # silently picking (and caching) a node-local SQLite store that
        # other cluster nodes cannot see. A raised exception here is
        # correct and recoverable (the caller's acquire attempt fails and
        # can be retried); a wrongly-cached SQLite lock is not.
        if is_storage_mode_undetermined():
            raise RuntimeError(
                "AliasLockStoreFactory: storage mode is not yet determined "
                "(server still starting up) -- refusing to resolve an "
                "alias lock store. Resolving to SQLite now could silently "
                "create a node-private lock invisible to other cluster "
                "nodes once postgres mode is confirmed. Retry once "
                "app.state.storage_mode has resolved to its real value."
            )
        if is_postgres_storage_mode():
            return self._resolve_postgres()
        return self._resolve_sqlite()
