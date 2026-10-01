"""AC2 RED/GREEN tests for PostgreSQL tool-group access SQL/API."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from code_indexer.server.storage.postgres.groups_backend import GroupsPostgresBackend


def _pool(fetchone=None, fetchall=None, rowcount=1):
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone
    cursor.fetchall.return_value = fetchall or []
    cursor.rowcount = rowcount
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    connection.connection = connection
    pool = MagicMock()
    pool.connection.return_value.__enter__.return_value = connection
    return pool, connection, cursor


def _group(group_id: int) -> dict:
    return {
        "id": group_id,
        "name": f"group-{group_id}",
        "description": "test",
        "is_default": False,
        "created_at": datetime.now(timezone.utc),
    }


def test_tool_access_methods_use_postgres_sql() -> None:
    pool, connection, cursor = _pool(fetchone=_group(7), fetchall=[])
    cursor.fetchone.side_effect = [_group(7), (True,)]
    backend = GroupsPostgresBackend(pool)

    assert backend.set_tool_access("git_push", 7, True, "admin") is True
    assert backend.is_tool_allowed("git_push", 7) is True
    assert backend.get_group_tools(7) == []
    assert backend.get_tool_groups("git_push") == []

    sql = " ".join(str(call.args[0]) for call in cursor.execute.call_args_list)
    assert "tool_group_access" in sql
    assert "%s" in sql
    assert connection.commit.call_count >= 1


def test_atomic_fanout_uses_one_transaction_and_returns_ids() -> None:
    pool, connection, cursor = _pool(fetchall=[(3,), (4,)], rowcount=2)
    backend = GroupsPostgresBackend(pool)

    assert backend.set_tool_access_all_groups("search_code", False, "admin") == [3, 4]
    assert connection.commit.call_count == 1
    assert len(cursor.execute.call_args_list) == 1
    assert "INSERT INTO tool_group_access" in cursor.execute.call_args.args[0]


def test_atomic_fanout_rolls_back_on_execute_failure() -> None:
    pool, connection, cursor = _pool()
    cursor.execute.side_effect = RuntimeError("injected fan-out failure")
    backend = GroupsPostgresBackend(pool)

    try:
        backend.set_tool_access_all_groups("search_code", False, "admin")
    except RuntimeError as exc:
        assert "injected" in str(exc)
    else:
        raise AssertionError("failure injection did not propagate")
    connection.rollback.assert_called_once()
