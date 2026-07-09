"""
Tests for PostgreSQL native type compatibility in routes.py utility functions.

Bug 2: Timestamp string slicing on datetime objects (_safe_ts_slice helper).
Bug 4: _format_datetime_display crashes on datetime objects instead of strings.
"""

from datetime import datetime, timezone

from code_indexer.server.web.routes import _format_datetime_display, _safe_ts_slice


def test_safe_ts_slice_with_string():
    """_safe_ts_slice returns sliced string for ISO string input (SQLite path)."""
    result = _safe_ts_slice("2026-01-30T14:32:00+00:00", 19)
    assert result == "2026-01-30T14:32:00"


def test_safe_ts_slice_with_datetime():
    """_safe_ts_slice converts datetime to ISO and slices it (PG native type path)."""
    dt = datetime(2026, 1, 30, tzinfo=timezone.utc)
    result = _safe_ts_slice(dt, 10)
    assert result == "2026-01-30"


def test_safe_ts_slice_with_none():
    """_safe_ts_slice returns None when value is None."""
    result = _safe_ts_slice(None, 19)
    assert result is None


def test_format_datetime_display_with_string():
    """_format_datetime_display works with ISO string input (SQLite path)."""
    result = _format_datetime_display("2026-01-30T14:32:00+00:00")
    assert result == "2026-01-30 14:32"


def test_format_datetime_display_with_datetime_object():
    """_format_datetime_display works with datetime object input (PG native type path)."""
    dt = datetime(2026, 1, 30, 14, 32, 0, tzinfo=timezone.utc)
    result = _format_datetime_display(dt)
    assert result == "2026-01-30 14:32"
