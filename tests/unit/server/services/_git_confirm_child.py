"""Child-process helper for the shared confirmation-token tests (never
collected by pytest: no ``test_`` prefix).

Builds its OWN PayloadCache over the store named on the command line (a
SQLite file, or a PostgreSQL DSN plus schema) and its OWN
GitOperationsService, then redeems a ``git clean`` confirmation token for
the given user/alias/repository and prints the service result as JSON.

When GIT_CONFIRM_BARRIER_DIR is set, the child finishes all setup, writes
``ready-<pid>`` there and redeems only once the parent creates ``go`` --
so several children redeem at the same moment.

Usage:
    python _git_confirm_child.py <scratch_dir> sqlite <db_path> <alias> <repo> <user> <token>
    python _git_confirm_child.py <scratch_dir> postgres <dsn> <alias> <repo> <user> <token> <schema>
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_SQLITE_ARGC = 8
_POSTGRES_ARGC = 9
_BARRIER_ENV = "GIT_CONFIRM_BARRIER_DIR"
_BARRIER_TIMEOUT_SECONDS = 60.0
_BARRIER_POLL_SECONDS = 0.01


class _AliasPaths:
    """Activated-repo lookup double: resolves exactly one alias to one path."""

    def __init__(self, alias: str, repo: str) -> None:
        self._alias = alias
        self._repo = repo

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        if user_alias != self._alias:
            raise FileNotFoundError(user_alias)
        return self._repo


def _await_barrier() -> bool:
    """True when no barrier is requested or the go signal arrived in time."""
    barrier = os.environ.get(_BARRIER_ENV)
    if not barrier:
        return True
    barrier_dir = Path(barrier)
    (barrier_dir / f"ready-{os.getpid()}").touch()
    deadline = time.monotonic() + _BARRIER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if (barrier_dir / "go").exists():
            return True
        time.sleep(_BARRIER_POLL_SECONDS)
    return False


def main(argv: List[str]) -> int:
    from code_indexer.server.cache.payload_cache import (
        PayloadCache,
        PayloadCacheConfig,
    )
    from code_indexer.server.services.git_operations_service import (
        GitOperationsService,
    )
    from code_indexer.server.utils.config_manager import GitTimeoutsConfig

    if len(argv) not in (_SQLITE_ARGC, _POSTGRES_ARGC):
        print(__doc__, file=sys.stderr)
        return 2
    scratch_dir, kind, location, alias, repo, user, token = argv[1:8]

    pool = None
    # Any: the two backend classes share only a structural Protocol, and
    # the PostgreSQL one is imported only on that branch.
    backend: Any
    if kind == "sqlite":
        from code_indexer.server.storage.sqlite_backends.payload_cache_backend import (
            PayloadCacheSqliteBackend,
        )

        backend = PayloadCacheSqliteBackend(location)
    elif kind == "postgres" and len(argv) == _POSTGRES_ARGC:
        from psycopg.conninfo import make_conninfo

        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.storage.postgres.payload_cache_backend import (
            PayloadCachePostgresBackend,
        )

        dsn = make_conninfo(location, options=f"-csearch_path={argv[8]}")
        pool = ConnectionPool(dsn, min_size=1, max_size=1)
        backend = PayloadCachePostgresBackend(pool)
    else:
        print(__doc__, file=sys.stderr)
        return 2

    result: Optional[Dict[str, Any]] = None
    try:
        cache = PayloadCache(
            db_path=Path(scratch_dir) / "unused_payload_cache.db",
            config=PayloadCacheConfig(),
            storage_backend=backend,
        )
        cache.initialize()
        # Any: the alias-path double stands in for ActivatedRepoManager.
        service: Any = GitOperationsService()
        service._git_timeouts = GitTimeoutsConfig()
        service.activated_repo_manager = _AliasPaths(alias, repo)
        service.payload_cache = cache
        if not _await_barrier():
            result = {"exception": "BarrierTimeout", "message": "no go signal"}
        else:
            try:
                result = service.clean_repository(alias, user, confirmation_token=token)
            except Exception as exc:  # reported to the parent test as data
                result = {"exception": type(exc).__name__, "message": str(exc)}
    finally:
        if pool is not None:
            pool.close()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
