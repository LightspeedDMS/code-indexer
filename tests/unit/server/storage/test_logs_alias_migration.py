"""
Tests for Story #876 Phase C: logs table `alias` column (SQLite path).

The lifecycle-runner (Phase B) emits ERROR log rows tagged with the repo alias
that produced the error so operators can filter logs-by-repo in the admin UI.
This test suite drives the schema addition and the insert_log/query_logs
plumbing needed to carry that tag end-to-end for SQLite-backed deployments.

Migration contract (CLAUDE.md "DATABASE MIGRATIONS MUST BE BACKWARD COMPATIBLE"):
- New column `alias TEXT DEFAULT NULL`.
- ALTER TABLE ADD COLUMN IF-MISSING (backfills existing rows to NULL).
- Old code that does not pass `alias` must still work unchanged (NULL default).
"""

from __future__ import annotations

import sqlite3
from typing import Any


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestLogsAliasColumnSchema:
    """The SQLite logs table must carry an `alias TEXT` column after init."""

    def test_fresh_logs_db_has_alias_column(self, tmp_path: Any) -> None:
        """A newly-initialised LogsSqliteBackend exposes an `alias` column."""
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        db_path = str(tmp_path / "logs_fresh.db")
        backend = LogsSqliteBackend(db_path)
        try:
            # Use a plain sqlite3 connection so we don't leak the backend's
            # internal connection caching into the assertion.
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.execute("PRAGMA table_info(logs)")
                columns = {row[1] for row in cursor.fetchall()}
            finally:
                conn.close()

            assert "alias" in columns, (
                "LogsSqliteBackend._ensure_schema must provision an `alias` column. "
                f"Found columns: {sorted(columns)}"
            )
        finally:
            backend.close()

    def test_existing_db_without_alias_is_migrated_in_place(
        self, tmp_path: Any
    ) -> None:
        """An older logs.db missing `alias` must gain it on reopen."""
        db_path = str(tmp_path / "logs_legacy.db")

        # Simulate a pre-Story-#876 logs.db (no alias column).
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    source TEXT,
                    message TEXT,
                    correlation_id TEXT,
                    user_id TEXT,
                    request_path TEXT,
                    extra_data TEXT,
                    node_id TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                INSERT INTO logs (timestamp, level, source, message)
                VALUES ('2026-04-20T12:00:00Z', 'INFO', 'legacy', 'old row')
                """
            )
            conn.commit()
        finally:
            conn.close()

        # Opening through the backend should ADD COLUMN alias and leave the
        # pre-existing row intact (alias = NULL).
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        backend = LogsSqliteBackend(db_path)
        try:
            conn = sqlite3.connect(db_path)
            try:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(logs)").fetchall()
                }
                assert "alias" in columns, (
                    "Reopening a legacy logs.db must add the `alias` column in place."
                )

                legacy_row = conn.execute(
                    "SELECT message, alias FROM logs WHERE source = 'legacy'"
                ).fetchone()
                assert legacy_row is not None
                assert legacy_row[0] == "old row"
                assert legacy_row[1] is None, (
                    "Pre-existing rows must be back-filled to NULL alias."
                )
            finally:
                conn.close()
        finally:
            backend.close()


# ---------------------------------------------------------------------------
# insert_log() + query_logs() round-trip for the new column
# ---------------------------------------------------------------------------


class TestLogsAliasRoundTrip:
    """insert_log must accept and persist `alias`; query_logs must return it."""

    def test_insert_log_persists_alias_field(self, tmp_path: Any) -> None:
        """A log row inserted with alias='my-repo' comes back with alias='my-repo'."""
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        db_path = str(tmp_path / "logs_roundtrip.db")
        backend = LogsSqliteBackend(db_path)
        try:
            backend.insert_log(
                timestamp="2026-04-20T12:00:00Z",
                level="ERROR",
                source="lifecycle-runner",
                message="write_meta_md failed for alias=my-repo",
                alias="my-repo",
            )

            rows, total = backend.query_logs(source="lifecycle-runner", limit=10)
            assert total == 1
            assert len(rows) == 1
            assert rows[0]["alias"] == "my-repo", (
                f"alias must round-trip; got row={rows[0]}"
            )
        finally:
            backend.close()

    def test_insert_log_without_alias_defaults_to_null(self, tmp_path: Any) -> None:
        """Callers that don't pass alias must still work; the stored value is NULL."""
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        db_path = str(tmp_path / "logs_default.db")
        backend = LogsSqliteBackend(db_path)
        try:
            backend.insert_log(
                timestamp="2026-04-20T12:05:00Z",
                level="INFO",
                source="unrelated.module",
                message="no alias on this row",
            )

            rows, total = backend.query_logs(source="unrelated.module", limit=10)
            assert total == 1
            assert rows[0]["alias"] is None, (
                "A row inserted without alias must serialise alias=None, not raise."
            )
        finally:
            backend.close()
