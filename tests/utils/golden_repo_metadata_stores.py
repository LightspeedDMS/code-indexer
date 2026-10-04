"""REAL golden-repo metadata stores for tests that need both backends.

``sqlite`` is always available (production is solo SQLite). ``postgres``
runs only when ``TEST_POSTGRES_DSN`` points at a server the test may create
databases on: each use gets a throwaway database migrated by the real
``MigrationRunner`` and dropped afterwards.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest

from tests.utils.sqlite_db_templates import copy_migrated_sqlite_db

PG_DSN = os.environ.get("TEST_POSTGRES_DSN", "")
STORE_KINDS = ("sqlite", "postgres")


def _pg_dsn_for(dbname: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(PG_DSN)
    params["dbname"] = dbname
    return make_conninfo(**params)  # type: ignore[arg-type]


def _initialize_metadata_db(db_path: Path) -> None:
    from code_indexer.server.storage.database_manager import (
        DatabaseConnectionManager,
    )
    from code_indexer.server.storage.sqlite_backends.golden_repo_metadata_backend import (
        GoldenRepoMetadataSqliteBackend,
    )

    GoldenRepoMetadataSqliteBackend(str(db_path)).ensure_table_exists()
    # Release (and deregister) the pooled connection before the file is copied.
    DatabaseConnectionManager.get_instance(str(db_path)).close_all()


@contextlib.contextmanager
def golden_repo_metadata_store(kind: str, tmp_path: Path) -> Iterator[Any]:
    """Yield a real metadata backend of ``kind`` ('sqlite' or 'postgres')."""
    if kind == "sqlite":
        from code_indexer.server.storage.sqlite_backends.golden_repo_metadata_backend import (
            GoldenRepoMetadataSqliteBackend,
        )

        db_path = tmp_path / "metadata-store" / "cidx_server.db"
        # A migrated copy (a fresh one costs ~18 durable DDL syncs per test);
        # the real, idempotent ensure_table_exists() below still runs on it.
        copy_migrated_sqlite_db(
            "golden-repo-metadata-db", _initialize_metadata_db, db_path
        )
        backend = GoldenRepoMetadataSqliteBackend(str(db_path))
        backend.ensure_table_exists()
        yield backend
        return

    if kind != "postgres":
        raise ValueError(f"unknown store kind: {kind}")
    if not PG_DSN:
        pytest.skip("TEST_POSTGRES_DSN not set; PostgreSQL parity not run")

    import psycopg

    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
        GoldenRepoMetadataPostgresBackend,
    )
    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    name = f"bug2022_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    pool: Any = None
    try:
        with MigrationRunner(_pg_dsn_for(name)) as runner:
            runner.run()
        pool = ConnectionPool(_pg_dsn_for(name), min_size=1, max_size=2)
        yield GoldenRepoMetadataPostgresBackend(pool)
    finally:
        if pool is not None:
            pool.close()
        with psycopg.connect(PG_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
