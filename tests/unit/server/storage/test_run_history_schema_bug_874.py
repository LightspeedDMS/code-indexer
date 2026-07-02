"""
Unit tests for Bug #874 Story B: run_type + phase_timings_json schema columns.

Verifies SQLite schema migration for dependency_map_run_history:
  1. Fresh table: both new columns present after _ensure_run_history_table().
  2. Legacy table (created without new columns): ALTER TABLE adds them idempotently.
  3. Second call to _ensure_run_history_table: no-op, no exception.

No server infrastructure needed — raw sqlite3 connections, no mocks.
"""

from __future__ import annotations

import sqlite3
from typing import Any

_RUN_HISTORY_TABLE = "dependency_map_run_history"


def _run_history_column_names(db_path: str) -> set:
    """Return column names for dependency_map_run_history via PRAGMA table_info."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute("PRAGMA table_info(dependency_map_run_history)")
        return {row[1] for row in cursor.fetchall()}
    finally:
        conn.close()


class TestRunHistorySchemaBug874:
    """_ensure_run_history_table must provision run_type and phase_timings_json."""

    def test_fresh_table_has_run_type_and_phase_timings_columns(
        self, tmp_path: Any
    ) -> None:
        """A freshly created dependency_map_run_history has both new columns."""
        from code_indexer.server.storage.sqlite_backends import (
            DependencyMapTrackingBackend,
        )

        db_path = str(tmp_path / "fresh.db")
        backend = DependencyMapTrackingBackend(db_path)
        try:
            backend._ensure_run_history_table()

            cols = _run_history_column_names(db_path)
            assert "run_type" in cols, f"run_type column missing. Found: {sorted(cols)}"
            assert "phase_timings_json" in cols, (
                f"phase_timings_json column missing. Found: {sorted(cols)}"
            )
        finally:
            backend.close()

    def test_existing_table_without_new_columns_gets_them_on_ensure(
        self, tmp_path: Any
    ) -> None:
        """A legacy table missing run_type/phase_timings_json gains them idempotently."""
        db_path = str(tmp_path / "legacy.db")

        # Simulate pre-Story-B table (no new columns).
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE dependency_map_run_history (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    domain_count INTEGER,
                    total_chars INTEGER,
                    edge_count INTEGER,
                    zero_char_domains INTEGER,
                    repos_analyzed INTEGER,
                    repos_skipped INTEGER,
                    pass1_duration_s REAL,
                    pass2_duration_s REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE dependency_map_tracking (
                    id INTEGER PRIMARY KEY,
                    last_run TEXT,
                    next_run TEXT,
                    status TEXT DEFAULT 'pending',
                    commit_hashes TEXT,
                    error_message TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

        # Opening through the backend must add the missing columns.
        from code_indexer.server.storage.sqlite_backends import (
            DependencyMapTrackingBackend,
        )

        backend = DependencyMapTrackingBackend(db_path)
        try:
            backend._ensure_run_history_table()

            cols = _run_history_column_names(db_path)
            assert "run_type" in cols, "Reopening legacy DB must add run_type column."
            assert "phase_timings_json" in cols, (
                "Reopening legacy DB must add phase_timings_json column."
            )
        finally:
            backend.close()

    def test_second_ensure_call_is_noop(self, tmp_path: Any) -> None:
        """Calling _ensure_run_history_table twice raises no exception."""
        from code_indexer.server.storage.sqlite_backends import (
            DependencyMapTrackingBackend,
        )

        db_path = str(tmp_path / "double.db")
        backend = DependencyMapTrackingBackend(db_path)
        try:
            backend._ensure_run_history_table()
            # Second call must be a no-op — no duplicate-column error.
            backend._ensure_run_history_table()

            cols = _run_history_column_names(db_path)
            assert "run_type" in cols
            assert "phase_timings_json" in cols
        finally:
            backend.close()
