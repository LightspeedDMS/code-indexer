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


def test_list_scans_normalizes_timestamptz_datetime_to_iso_string() -> None:
    """Bug #1701: TIMESTAMPTZ columns (started_at, completed_at) deserialize
    to native datetime objects via psycopg -- list_scans() must normalize
    them to ISO-8601 str, mirroring research_sessions_backend.py's
    sanitize_row() pattern, so web/routes.py's
    datetime.fromisoformat(scan["started_at"]) does not raise TypeError."""
    from datetime import datetime, timezone

    from code_indexer.server.storage.postgres.self_monitoring_backend import (
        SelfMonitoringPostgresBackend,
    )

    started = datetime(2026, 8, 27, 10, 30, 0, tzinfo=timezone.utc)
    completed = datetime(2026, 8, 27, 10, 35, 0, tzinfo=timezone.utc)

    pool = _make_pool()
    conn = pool.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.return_value = [
        ("scan1", started, completed, "SUCCESS", 1, 2, 0, None)
    ]
    backend = SelfMonitoringPostgresBackend(pool)
    result = backend.list_scans(limit=10)

    assert isinstance(result[0]["started_at"], str), (
        f"expected str, got {type(result[0]['started_at'])!r}"
    )
    assert result[0]["started_at"] == started.isoformat()
    assert isinstance(result[0]["completed_at"], str), (
        f"expected str, got {type(result[0]['completed_at'])!r}"
    )
    assert result[0]["completed_at"] == completed.isoformat()

    # Must not raise -- this is the actual production symptom (routes.py
    # _add_scan_duration()).
    parsed = datetime.fromisoformat(result[0]["started_at"])
    assert parsed == started


def test_list_issues_normalizes_timestamptz_datetime_to_iso_string() -> None:
    """Bug #1701: created_at (TIMESTAMPTZ) deserializes to a native datetime
    via psycopg -- list_issues() must normalize it to ISO-8601 str, matching
    SQLite/solo mode's shape."""
    from datetime import datetime, timezone

    from code_indexer.server.storage.postgres.self_monitoring_backend import (
        SelfMonitoringPostgresBackend,
    )

    created = datetime(2026, 8, 27, 9, 0, 0, tzinfo=timezone.utc)

    pool = _make_pool()
    conn = pool.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.return_value = [
        (1, "scan1", None, None, "bug", "title1", "fp1", "1", "f.py", created)
    ]
    backend = SelfMonitoringPostgresBackend(pool)
    result = backend.list_issues(limit=10)

    assert isinstance(result[0]["created_at"], str), (
        f"expected str, got {type(result[0]['created_at'])!r}"
    )
    assert result[0]["created_at"] == created.isoformat()


def test_get_last_started_at_normalizes_timestamptz_datetime_to_iso_string() -> None:
    """Bug #1701: get_last_started_at() must normalize the TIMESTAMPTZ
    started_at datetime to ISO-8601 str, so
    web/routes.py's _calculate_next_scan_time()'s
    datetime.fromisoformat(last_scan_time) does not raise TypeError (which
    was logged as ERROR WEB-SELF-MONITORING-003 on every dashboard
    render)."""
    from datetime import datetime, timezone

    from code_indexer.server.storage.postgres.self_monitoring_backend import (
        SelfMonitoringPostgresBackend,
    )

    started = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    pool = _make_pool()
    conn = pool.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchone.return_value = (started,)
    backend = SelfMonitoringPostgresBackend(pool)
    result = backend.get_last_started_at()

    assert isinstance(result, str), f"expected str, got {type(result)!r}"
    assert result == started.isoformat()
    # Must not raise -- the actual production symptom.
    assert datetime.fromisoformat(result) == started


def test_fetch_stored_fingerprints_normalizes_timestamptz_datetime_to_iso_string() -> (
    None
):
    """Bug #1701: fetch_stored_fingerprints()'s created_at (5th tuple
    element, TIMESTAMPTZ) must be normalized to ISO-8601 str, matching
    SQLite/solo mode's shape."""
    from datetime import datetime, timezone

    from code_indexer.server.storage.postgres.self_monitoring_backend import (
        SelfMonitoringPostgresBackend,
    )

    created = datetime(2026, 8, 27, 8, 0, 0, tzinfo=timezone.utc)

    pool = _make_pool()
    conn = pool.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.return_value = [
        ("fp1", "bug", "E1", "title1", created)
    ]
    backend = SelfMonitoringPostgresBackend(pool)
    result = backend.fetch_stored_fingerprints(retention_days=365)

    assert isinstance(result[0][4], str), f"expected str, got {type(result[0][4])!r}"
    assert result[0][4] == created.isoformat()
