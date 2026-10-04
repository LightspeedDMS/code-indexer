"""Real stores for tests that run ``RefreshScheduler._scheduler_loop``.

Without an injected registry / golden-repo-metadata backend, the scheduler
lazily falls back to SQLite stores next to ``golden_repos_dir`` -- an
uninitialized file whose every loop iteration then fails ("no such table"),
or a shared location such as ``/tmp/cidx_server.db``.  The loop catches
each failure and logs ``SCHEDULER_ITERATION_FAILED``, so a test that only
checks its own side effect still passes.  These helpers build real,
schema-initialized stores under the test's own directory, and recognise
that failure record.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, List, Tuple

# The refresh_scheduler loop's per-iteration ERROR text (Bug #735 backoff).
SCHEDULER_ITERATION_FAILED = "scheduler iteration failed"


def scheduler_iteration_failures(records: Iterable[logging.LogRecord]) -> List[str]:
    """Messages of the records that report a failed scheduler iteration."""
    return [
        r.getMessage() for r in records if SCHEDULER_ITERATION_FAILED in r.getMessage()
    ]


def make_registered_repos_due(registry: Any) -> None:
    """Make a mock registry report every registered repo as due.

    A MagicMock's ``list_due_repos`` iterates as empty, so the loop's
    per-repo logic never runs and a test of it passes vacuously.  This
    returns ``list_global_repos.return_value`` as read at call time (never
    calling ``list_global_repos``, so a test's own side_effect counter on it
    is not advanced)."""

    def due(*args: Any, **kwargs: Any) -> List[Any]:
        return list(registry.list_global_repos.return_value)

    registry.list_due_repos.side_effect = due


def real_metadata_store(server_dir: Path) -> Any:
    """A real, empty golden-repo metadata store in *server_dir*."""
    from code_indexer.server.storage.sqlite_backends.golden_repo_metadata_backend import (
        GoldenRepoMetadataSqliteBackend,
    )

    server_dir.mkdir(parents=True, exist_ok=True)
    store = GoldenRepoMetadataSqliteBackend(str(server_dir / "cidx_server.db"))
    store.ensure_table_exists()
    return store


def initialize_server_database(server_dir: Path) -> Path:
    """Initialize the full server schema in *server_dir*/cidx_server.db, as
    the server does at startup -- for components that resolve their stores
    from ``golden_repos_dir.parent`` themselves."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    server_dir.mkdir(parents=True, exist_ok=True)
    db_path = server_dir / "cidx_server.db"
    DatabaseSchema(str(db_path)).initialize_database()
    return db_path


def real_scheduler_stores(server_dir: Path, golden_repos_dir: Path) -> Tuple[Any, Any]:
    """A real SQLite registry and metadata store sharing one fully
    initialized server database in *server_dir*."""
    from code_indexer.global_repos.global_registry import GlobalRegistry

    db_path = initialize_server_database(server_dir)
    registry = GlobalRegistry(
        golden_repos_dir=str(golden_repos_dir), use_sqlite=True, db_path=str(db_path)
    )
    return registry, real_metadata_store(server_dir)
