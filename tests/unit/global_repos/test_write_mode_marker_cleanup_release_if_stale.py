"""Marker cleanup releases a lock only if it is still stale AT the release.

Between the cleanup's liveness check and its release, a lapsed lease can be
renewed again (e.g. by a node still on the previous release). The release
must re-check staleness atomically and then keep the lock and the marker.
The file lock is not parametrized: its liveness check evicts a stale lock
file in the same call, so no renewal can land in between.

Real RefreshScheduler, real SQLite / PostgreSQL stores, a real renewing
thread; the interleaving is forced right after the check, no sleeps.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Iterator, Tuple
from unittest.mock import Mock

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)
from tests.fixtures.refresh_scheduler_stores import real_metadata_store

_ALIAS = "cidx-meta"
_OWNER = "mcp_write_mode"
_TOKEN = "write-mode-token"
_JOIN_TIMEOUT_SECONDS = 30

# (store, set last_renewed_at to an SQL expression, lapsed expr, fresh expr)
Backend = Tuple[Any, Callable[[str], None], str, str]


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request, tmp_path: Path) -> Iterator[Backend]:
    if request.param == "sqlite":
        store = SqliteAliasLockStore(tmp_path / "alias_locks")

        def set_sqlite(expression: str) -> None:
            path = str(store._lock_dir / f"{_ALIAS}.db")
            with closing(sqlite3.connect(path)) as db, db:
                db.execute(
                    f"UPDATE golden_repo_alias_locks SET last_renewed_at = {expression}"
                )

        yield (
            store,
            set_sqlite,
            "strftime('%Y-%m-%d %H:%M:%f', 'now', '-2 hours')",
            "strftime('%Y-%m-%d %H:%M:%f', 'now')",
        )
        return
    import psycopg

    from code_indexer.server.services.alias_lock_store.postgres_store import (
        PostgresAliasLockStore,
    )
    from tests.shared.alias_lock_pg import postgres_lock_store_dsn

    with postgres_lock_store_dsn(_ALIAS) as dsn:

        def set_pg(expression: str) -> None:
            with closing(psycopg.connect(dsn)) as pg, pg:
                pg.execute(
                    f"UPDATE golden_repo_alias_locks SET last_renewed_at = "
                    f"{expression} WHERE lock_key = %s",
                    (_ALIAS,),
                )

        yield (
            PostgresAliasLockStore(dsn),
            set_pg,
            "now() - interval '2 hours'",
            "now()",
        )


def _scheduler(tmp_path: Path, store: Any) -> RefreshScheduler:
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    registry = Mock()
    registry.list_global_repos.return_value = []
    return RefreshScheduler(
        golden_repos_dir=str(tmp_path / "golden-repos"),
        config_source=config,
        query_tracker=Mock(spec=QueryTracker),
        cleanup_manager=Mock(spec=CleanupManager),
        registry=registry,
        golden_repo_metadata_backend=real_metadata_store(tmp_path / "server-data"),
        alias_lock_db_backed_enabled_getter=lambda: True,
        alias_lock_store_resolver=lambda: store,
    )


class TestReleaseRechecksStalenessAtomically:
    def test_lease_renewed_after_the_check_keeps_lock_and_marker(
        self, tmp_path: Path, backend: Backend
    ) -> None:
        store, set_renewed, lapsed, fresh = backend
        scheduler = _scheduler(tmp_path, store)
        assert scheduler.write_lock_manager.acquire(
            _ALIAS, owner_name=_OWNER, owner_token=_TOKEN
        )
        marker = tmp_path / "golden-repos" / ".write_mode" / f"{_ALIAS}.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"alias": _ALIAS, "owner_token": _TOKEN}))
        set_renewed(lapsed)

        checked = scheduler.is_write_locked

        def check_then_renew(alias: str) -> bool:
            live = checked(alias)
            renewer = threading.Thread(target=set_renewed, args=(fresh,))
            renewer.start()
            renewer.join(_JOIN_TIMEOUT_SECONDS)
            return live

        scheduler.is_write_locked = check_then_renew  # type: ignore[method-assign]
        scheduler.cleanup_stale_write_mode_markers(force=True)

        assert checked(_ALIAS) is True
        assert marker.exists()
