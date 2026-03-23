"""
PostgreSQL backend utilities.

Provides shared helpers for all PostgreSQL backend implementations.
"""

from __future__ import annotations

from datetime import datetime


def to_iso(val):
    """Convert datetime to ISO string, pass other types through unchanged."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return val


def sanitize_row(row: dict) -> dict:
    """Convert all datetime values in a row dict to ISO strings.

    PostgreSQL returns datetime objects for TIMESTAMPTZ columns,
    but the application models expect ISO-8601 strings.
    """
    return {k: to_iso(v) for k, v in row.items()}
