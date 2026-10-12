"""Live-PostgreSQL tests for ControlPlaneMetadataPostgresBackend (step 5a).

Runs the real migration 054 DDL in a private schema and drives the backend
over a real psycopg pool -- the cluster twin of the SQLite tests in
tests/unit/global_repos/test_control_plane_metadata.py. Skips without
TEST_POSTGRES_DSN; guarded against non-disposable databases.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest

from code_indexer.server.storage.protocols.control_plane_metadata_backend import (
    KIND_ALIAS,
    KIND_WRITE_MODE,
    format_updated_at,
)
from tests.utils.live_pg import live_pg_dsn, migration_ddl, private_schema


@pytest.fixture
def backend() -> Iterator[object]:
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.control_plane_metadata_backend import (
        ControlPlaneMetadataPostgresBackend,
    )

    dsn = live_pg_dsn()
    with private_schema(
        dsn, "cidx_cpm_", migration_ddl("054_control_plane_metadata.sql")
    ) as scoped:
        pool = ConnectionPool(scoped, min_size=0, max_size=4, name="test")
        try:
            yield ControlPlaneMetadataPostgresBackend(pool)
        finally:
            pool.close()


def test_put_get_list_delete(backend):
    backend.put(KIND_ALIAS, "a_b-global", {"target_path": "/s/1"})
    backend.put(KIND_ALIAS, "aXb-global", {"target_path": "/s/2"})
    backend.put(KIND_WRITE_MODE, "a_b", {"source_path": "/live"})

    assert backend.get(KIND_ALIAS, "a_b-global") == {"target_path": "/s/1"}
    assert set(backend.list_all(KIND_ALIAS, "a_b")) == {"a_b-global"}
    assert backend.list_all(KIND_WRITE_MODE) == {"a_b": {"source_path": "/live"}}

    backend.delete(KIND_ALIAS, "a_b-global")
    assert backend.get(KIND_ALIAS, "a_b-global") is None


def test_import_if_newer_never_older_over_newer(backend):
    now = datetime.now(timezone.utc)
    backend.put(KIND_ALIAS, "repo-global", {"target_path": "/s/registry"})

    stored_old = backend.import_if_newer(
        KIND_ALIAS,
        "repo-global",
        {"target_path": "/s/old-file"},
        format_updated_at(now - timedelta(hours=1)),
    )
    stored_new = backend.import_if_newer(
        KIND_ALIAS,
        "repo-global",
        {"target_path": "/s/new-file"},
        format_updated_at(now + timedelta(hours=1)),
    )
    stored_missing = backend.import_if_newer(
        KIND_ALIAS, "fresh-global", {"target_path": "/s/f"}, format_updated_at(now)
    )

    assert (stored_old, stored_new, stored_missing) == (False, True, True)
    assert backend.get(KIND_ALIAS, "repo-global") == {"target_path": "/s/new-file"}


def test_import_never_resurrects_a_deleted_key(backend):
    """The delete's tombstone outranks any file dated before it; a file dated
    after it (an old-image pod re-creating the key) still imports."""
    now = datetime.now(timezone.utc)
    backend.put(KIND_WRITE_MODE, "repo", {"source_path": "/live"})
    backend.delete(KIND_WRITE_MODE, "repo")

    stale = backend.import_if_newer(
        KIND_WRITE_MODE,
        "repo",
        {"source_path": "/live"},
        format_updated_at(now - timedelta(hours=1)),
    )
    assert stale is False
    assert backend.get(KIND_WRITE_MODE, "repo") is None

    recreated = backend.import_if_newer(
        KIND_WRITE_MODE,
        "repo",
        {"source_path": "/again"},
        format_updated_at(now + timedelta(hours=1)),
    )
    assert recreated is True
    assert backend.get(KIND_WRITE_MODE, "repo") == {"source_path": "/again"}
