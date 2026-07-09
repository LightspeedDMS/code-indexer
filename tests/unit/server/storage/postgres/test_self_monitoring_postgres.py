"""
Tests for SelfMonitoringPostgresBackend missing methods (Bug 6).

Verifies that list_scans() and get_running_scan_count() are implemented
and return the types specified by the SelfMonitoringBackend Protocol.

Uses MagicMock pool — no real PostgreSQL required.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_pool() -> MagicMock:
    """Return a MagicMock mimicking a psycopg ConnectionPool context-manager."""
    pool = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = []
    cursor.rowcount = 0
    conn.execute.return_value = cursor
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool


def test_list_scans_returns_list() -> None:
    """list_scans() must exist and return a list (Protocol requirement)."""
    from code_indexer.server.storage.postgres.self_monitoring_backend import (
        SelfMonitoringPostgresBackend,
    )

    backend = SelfMonitoringPostgresBackend(_make_pool())
    result = backend.list_scans(limit=10)
    assert isinstance(result, list)


def test_get_running_scan_count_returns_int() -> None:
    """get_running_scan_count() must exist and return an int (Protocol requirement)."""
    from code_indexer.server.storage.postgres.self_monitoring_backend import (
        SelfMonitoringPostgresBackend,
    )

    pool = _make_pool()
    conn = pool.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchone.return_value = (0,)
    backend = SelfMonitoringPostgresBackend(pool)
    result = backend.get_running_scan_count()
    assert isinstance(result, int)
