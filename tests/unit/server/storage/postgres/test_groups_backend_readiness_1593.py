"""AC9 RED/GREEN tests for PostgreSQL tool-access readiness gate.

Mirrors the mocked-pool pattern in test_groups_backend_tool_access_1593.py
(AC2). Story #1593 AC9: is_tool_access_enforcement_ready() must return
False -- never raise -- when the marker table doesn't exist yet (Story 2
hasn't landed/seeded), and must reflect the marker's actual value once it
does exist.
"""

import os
import sys
import uuid
from typing import Iterator
from unittest.mock import MagicMock

import pytest
from psycopg import errors as psycopg_errors
from psycopg.conninfo import make_conninfo

from code_indexer.server.storage.postgres.groups_backend import GroupsPostgresBackend


def _pool(fetchone=None, fetchall=None, rowcount=1, execute_side_effect=None):
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone
    cursor.fetchall.return_value = fetchall or []
    cursor.rowcount = rowcount
    if execute_side_effect is not None:
        cursor.execute.side_effect = execute_side_effect
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    connection.connection = connection
    pool = MagicMock()
    pool.connection.return_value.__enter__.return_value = connection
    return pool, connection, cursor


def test_readiness_true_when_marker_row_complete() -> None:
    pool, connection, cursor = _pool(fetchone=(True,))
    backend = GroupsPostgresBackend(pool)

    assert backend.is_tool_access_enforcement_ready() is True


def test_readiness_false_when_marker_row_absent() -> None:
    pool, connection, cursor = _pool(fetchone=None)
    backend = GroupsPostgresBackend(pool)

    assert backend.is_tool_access_enforcement_ready() is False


@pytest.fixture
def real_pg_backend() -> Iterator[GroupsPostgresBackend]:
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN not set")
    import psycopg

    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    schema = f"readiness_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = ConnectionPool(
        make_conninfo(dsn, options=f"-csearch_path={schema}"), min_size=1
    )
    try:
        yield GroupsPostgresBackend(pool)
    finally:
        pool.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


def _exceptions_raised_during(fn):
    seen = []

    def tracer(frame, event, arg):
        if event == "exception":
            seen.append(arg[0])
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        result = fn()
    finally:
        sys.settrace(previous)
    return result, seen


def test_real_pg_absent_marker_table_raises_nothing_inside_the_check(
    real_pg_backend: GroupsPostgresBackend,
) -> None:
    result, seen = _exceptions_raised_during(
        real_pg_backend.is_tool_access_enforcement_ready
    )

    assert result is False
    assert [t for t in seen if issubclass(t, psycopg_errors.UndefinedTable)] == []


def test_real_pg_marker_table_created_later_is_observed_live(
    real_pg_backend: GroupsPostgresBackend,
) -> None:
    assert real_pg_backend.is_tool_access_enforcement_ready() is False

    with real_pg_backend._pool.connection() as conn:
        conn.execute(
            "CREATE TABLE tool_access_migration_state "
            "(id INTEGER PRIMARY KEY, complete BOOLEAN NOT NULL)"
        )
        conn.commit()
    assert real_pg_backend.is_tool_access_enforcement_ready() is False

    with real_pg_backend._pool.connection() as conn:
        conn.execute(
            "INSERT INTO tool_access_migration_state (id, complete) VALUES (1, TRUE)"
        )
        conn.commit()
    assert real_pg_backend.is_tool_access_enforcement_ready() is True
