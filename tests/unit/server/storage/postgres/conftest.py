"""Live-PostgreSQL fixtures for this directory.

``TEST_POSTGRES_DSN`` names a server the suite may create databases on; the
database it points at may or may not be migrated.  A test that needs the
real migrated schema takes ``migrated_scratch_pg_dsn`` instead of reading the
DSN directly, so it passes in either case and never alters shared state.
"""

from __future__ import annotations

import os
import uuid
from typing import Iterator, Tuple

import pytest


def _dsn_for(base_dsn: str, dbname: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(base_dsn)
    params["dbname"] = dbname
    return make_conninfo(**params)  # type: ignore[arg-type]


def _admin_execute(base_dsn: str, sql: str) -> None:
    import psycopg

    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute(sql)


@pytest.fixture(scope="session")
def _migrated_template_pg() -> Iterator[Tuple[str, str]]:
    """One database migrated by the real MigrationRunner per session (the
    migrations take seconds); modules clone it.  Dropped at session end."""
    base_dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not base_dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    name = f"template_{uuid.uuid4().hex[:12]}"
    _admin_execute(base_dsn, f'CREATE DATABASE "{name}"')
    try:
        with MigrationRunner(_dsn_for(base_dsn, name)) as runner:
            runner.run()
        yield base_dsn, name
    finally:
        _admin_execute(base_dsn, f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture(scope="module")
def migrated_scratch_pg_dsn(_migrated_template_pg: Tuple[str, str]) -> Iterator[str]:
    """A migrated database of the module's own on the TEST_POSTGRES_DSN
    server (a clone of the session template), dropped after the module."""
    base_dsn, template = _migrated_template_pg
    name = f"scratch_{uuid.uuid4().hex[:12]}"
    _admin_execute(base_dsn, f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
    try:
        yield _dsn_for(base_dsn, name)
    finally:
        # Plain DROP: a test that leaks a connection fails here, loudly.
        _admin_execute(base_dsn, f'DROP DATABASE IF EXISTS "{name}"')
