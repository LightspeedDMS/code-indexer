"""AC2 live PostgreSQL round-trip coverage, gated by TEST_POSTGRES_DSN."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import pytest

try:
    import psycopg

    HAS_PSYCOPG = True
except ImportError:
    HAS_PSYCOPG = False

from code_indexer.server.storage.postgres.groups_backend import GroupsPostgresBackend


class _SingleConnectionPool:
    """Pool-shaped adapter retaining one real psycopg connection for a test."""

    def __init__(self, connection):
        self._connection = connection

    @contextmanager
    def connection(self):
        yield self._connection


@pytest.fixture()
def live_backend():
    if not HAS_PSYCOPG:
        pytest.skip("psycopg not available")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")

    connection = psycopg.connect(dsn)
    name = f"tool-access-{uuid.uuid4().hex}"
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS groups ("
            "id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, "
            "description TEXT NOT NULL, is_default BOOLEAN NOT NULL DEFAULT FALSE, "
            "created_at TIMESTAMPTZ NOT NULL)"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS tool_group_access ("
            "group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE, "
            "tool_name TEXT NOT NULL, allowed BOOLEAN NOT NULL, "
            "granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), granted_by TEXT, "
            "PRIMARY KEY (group_id, tool_name))"
        )
        cursor.execute(
            "INSERT INTO groups (name, description, is_default, created_at) "
            "VALUES (%s, %s, FALSE, NOW()) RETURNING id",
            (name, "live AC2 group"),
        )
        row = cursor.fetchone()
        assert row is not None
        group_id = row[0]
    connection.commit()

    backend = GroupsPostgresBackend(_SingleConnectionPool(connection))
    try:
        yield backend, group_id
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM tool_group_access WHERE group_id = %s", (group_id,)
            )
            cursor.execute("DELETE FROM groups WHERE id = %s", (group_id,))
        connection.commit()
        connection.close()


@pytest.mark.skipif(not HAS_PSYCOPG, reason="psycopg not available")
def test_tool_access_round_trip_on_real_postgresql(live_backend) -> None:
    backend, group_id = live_backend

    assert backend.is_tool_allowed("live_git_push", group_id) is False
    assert backend.set_tool_access("live_git_push", group_id, True, "live-admin")
    assert backend.is_tool_allowed("live_git_push", group_id) is True
    assert backend.get_group_tools(group_id) == ["live_git_push"]
    assert [group.id for group in backend.get_tool_groups("live_git_push")] == [
        group_id
    ]
