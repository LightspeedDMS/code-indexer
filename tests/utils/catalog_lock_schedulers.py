"""A real RefreshScheduler whose cidx-meta write lock is the file lock, the
SQLite alias-lock store or (with TEST_POSTGRES_DSN) the PostgreSQL store."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from tests.fixtures.refresh_scheduler_stores import real_metadata_store

CATALOG = "cidx-meta"
CATALOG_LOCK_KINDS = ["file", "sqlite", "postgres"]


def _store_resolver(kind: str, tmp_path: Path) -> Any:
    if kind == "sqlite":
        from code_indexer.server.services.alias_lock_store.sqlite_store import (
            SqliteAliasLockStore,
        )

        store: Any = SqliteAliasLockStore(tmp_path / "alias_locks")
        return lambda: store
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN not set")
    from code_indexer.server.services.alias_lock_store.postgres_store import (
        PostgresAliasLockStore,
    )
    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    MigrationRunner(dsn).run()
    pg_store = PostgresAliasLockStore(dsn)
    assert not pg_store.is_held(CATALOG), "a previous test left the lock held"
    return lambda: pg_store


def catalog_lock_scheduler(kind: str, tmp_path: Path) -> RefreshScheduler:
    """The scheduler over ``tmp_path/golden-repos`` (with an empty cidx-meta)."""
    golden = tmp_path / "golden-repos"
    (golden / CATALOG).mkdir(parents=True, exist_ok=True)
    db_kwargs: Dict[str, Any] = {}
    if kind != "file":
        db_kwargs = {
            "alias_lock_db_backed_enabled_getter": lambda: True,
            "alias_lock_store_resolver": _store_resolver(kind, tmp_path),
        }
    config_source = MagicMock()
    config_source.get_global_refresh_interval.return_value = 3600
    return RefreshScheduler(
        golden_repos_dir=str(golden),
        config_source=config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=MagicMock(),
        golden_repo_metadata_backend=real_metadata_store(tmp_path / "server-data"),
        **db_kwargs,
    )
