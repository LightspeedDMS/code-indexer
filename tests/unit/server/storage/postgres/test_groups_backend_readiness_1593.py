"""AC9 RED/GREEN tests for PostgreSQL tool-access readiness gate.

Mirrors the mocked-pool pattern in test_groups_backend_tool_access_1593.py
(AC2). Story #1593 AC9: is_tool_access_enforcement_ready() must return
False -- never raise -- when the marker table doesn't exist yet (Story 2
hasn't landed/seeded), and must reflect the marker's actual value once it
does exist.
"""

from unittest.mock import MagicMock

from psycopg import errors as psycopg_errors

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


def test_readiness_false_and_rolls_back_when_marker_table_missing() -> None:
    pool, connection, cursor = _pool(
        execute_side_effect=psycopg_errors.UndefinedTable("relation does not exist")
    )
    backend = GroupsPostgresBackend(pool)

    assert backend.is_tool_access_enforcement_ready() is False
    connection.rollback.assert_called_once()


def test_readiness_true_when_marker_row_complete() -> None:
    pool, connection, cursor = _pool(fetchone=(True,))
    backend = GroupsPostgresBackend(pool)

    assert backend.is_tool_access_enforcement_ready() is True


def test_readiness_false_when_marker_row_absent() -> None:
    pool, connection, cursor = _pool(fetchone=None)
    backend = GroupsPostgresBackend(pool)

    assert backend.is_tool_access_enforcement_ready() is False
