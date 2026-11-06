"""EVO-70528: the PostgreSQL backend is never asked for an unscoped startup sweep.

``BackgroundJobManager`` ran its startup orphan cleanup against the PostgreSQL
backend without a ``node_id``. The backend refuses (Bug #535) and logged
``cleanup_orphaned_jobs_on_startup called without node_id`` as a WARNING on every
process start, although the real, node-scoped sweep runs through
``JobTracker.cleanup_orphaned_jobs_on_startup(node_id)``. The manager now skips
the call for that backend, like ``fail_orphaned_jobs`` already does.
"""

import logging
from pathlib import Path
from typing import List

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend


def _backend_named(class_name: str, tmp_path: Path, calls: List[str]):
    """A real SQLite backend that answers to *class_name* and records sweeps."""

    def cleanup(self, *args, **kwargs):
        calls.append("cleanup_orphaned_jobs_on_startup")
        return 0

    backend_class = type(
        class_name,
        (BackgroundJobsSqliteBackend,),
        {"cleanup_orphaned_jobs_on_startup": cleanup},
    )
    db_path = tmp_path / "jobs.db"
    DatabaseSchema(str(db_path)).initialize_database()
    return backend_class(str(db_path))


def test_postgres_backend_is_not_swept_without_a_node_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    calls: List[str] = []
    backend = _backend_named("BackgroundJobsPostgresBackend", tmp_path, calls)

    with caplog.at_level(logging.DEBUG):
        manager = BackgroundJobManager(storage_backend=backend, node_id="node-a")
    manager.shutdown()

    assert calls == []


def test_sqlite_backend_is_still_swept_at_startup(tmp_path: Path) -> None:
    calls: List[str] = []
    backend = _backend_named("BackgroundJobsSqliteBackend", tmp_path, calls)

    manager = BackgroundJobManager(storage_backend=backend)
    manager.shutdown()

    assert calls == ["cleanup_orphaned_jobs_on_startup"]
