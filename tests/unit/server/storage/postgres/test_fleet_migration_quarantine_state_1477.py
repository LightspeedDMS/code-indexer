"""
Unit tests for GoldenRepoMetadataPostgresBackend's fleet-migration failure
quarantine persistence methods (Issue #1477).

Mirrors the mocked-pool convention used across
test_golden_repo_reconcile_breaker_state_1382.py: a MagicMock connection
pool exercises SQL text + parameterization + psycopg v3 API correctness
(conn.cursor()/conn.commit(), %s placeholders -- never sqlite's "?"),
matching the project's faithful-DB-mock discipline
(feedback_faithful_db_mocks). No real PostgreSQL required.

The migration file's DDL is checked separately (static, no live PG needed),
mirroring test_consumer_rate_limit_migration_1332.py.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

# This test file lives at:
#   tests/unit/server/storage/postgres/test_fleet_migration_quarantine_state_1477.py
# parents[5] walks up 5 directories from this file to the repo root
# (postgres -> storage -> server -> unit -> tests -> <repo root>), matching
# the identical convention already established by
# test_golden_repo_reconcile_breaker_state_1382.py in this same directory.
_REPO_ROOT_PARENTS_UP = 5


def _make_mock_pool(fetchone_return=None):
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = fetchone_return

    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    mock_pool = MagicMock()

    @contextmanager
    def _connection():
        yield mock_conn

    mock_pool.connection.side_effect = _connection

    return mock_pool, mock_conn, mock_cursor


class TestMigrationFile:
    _SQL_DIR = (
        Path(__file__).parents[_REPO_ROOT_PARENTS_UP]
        / "src"
        / "code_indexer"
        / "server"
        / "storage"
        / "postgres"
        / "migrations"
        / "sql"
    )
    _MIGRATION_FILE = _SQL_DIR / "040_fleet_migration_quarantine_state.sql"

    def test_migration_file_exists(self) -> None:
        assert self._MIGRATION_FILE.exists()

    def test_migration_creates_table_if_not_exists(self) -> None:
        content = self._MIGRATION_FILE.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS fleet_migration_quarantine_state" in content
        assert "DROP TABLE" not in content
        assert "DROP COLUMN" not in content


class TestGoldenRepoMetadataPostgresBackendFleetMigrationQuarantine:
    """Finding L (HIGH, Codex round-6 review, live-reproduced with a REAL
    concurrent-PostgreSQL repro): the old SELECT-then-INSERT/UPDATE pattern
    was a genuine lost-update race under real concurrency (two connections
    both read count=N, both compute N+1, both write N+1 -- one increment is
    lost). record_fleet_migration_failure is now a SINGLE atomic
    `INSERT ... ON CONFLICT (golden_alias) DO UPDATE ... RETURNING
    consecutive_failure_count` statement -- PostgreSQL's own row-level
    atomicity performs the increment, never a Python-computed count+1 after
    a separate SELECT. Every test below therefore asserts EXACTLY ONE
    `cursor.execute` call total (no separate SELECT, no separate
    INSERT-vs-UPDATE branching)."""

    def test_first_failure_inserts_and_returns_one(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        # The atomic statement's RETURNING clause supplies the post-write
        # count directly -- for a brand-new row that is 1.
        pool, conn, cursor = _make_mock_pool(fetchone_return=(1,))
        backend = GoldenRepoMetadataPostgresBackend(pool)

        count = backend.record_fleet_migration_failure("click", "sig-1")

        assert count == 1
        assert cursor.execute.call_count == 1
        sql_text = str(cursor.execute.call_args_list[0][0][0]).upper()
        assert "INSERT" in sql_text
        assert "ON CONFLICT" in sql_text
        assert "RETURNING" in sql_text
        # psycopg v3 uses %s placeholders, never sqlite's "?".
        assert "?" not in sql_text
        params = cursor.execute.call_args_list[0][0][1]
        assert "click" in params
        assert "sig-1" in params
        conn.commit.assert_called()

    def test_first_failure_also_stamps_signature_checked_at(self) -> None:
        """Finding C (Codex round-3 review): the atomic upsert must also
        set `signature_checked_at` -- the throttle bookkeeping
        `is_quarantined()` relies on."""
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool, conn, cursor = _make_mock_pool(fetchone_return=(1,))
        backend = GoldenRepoMetadataPostgresBackend(pool)

        backend.record_fleet_migration_failure("click", "sig-1")

        assert cursor.execute.call_count == 1
        assert "signature_checked_at" in str(cursor.execute.call_args_list[0][0][0])

    def test_failure_cause_is_persisted_on_insert(self) -> None:
        """Finding I (Codex round-5 review): the atomic upsert must accept
        and persist an optional `failure_cause`, so `is_quarantined()`
        can distinguish a disk-headroom-caused quarantine from a
        corrupt-data-caused one."""
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool, conn, cursor = _make_mock_pool(fetchone_return=(1,))
        backend = GoldenRepoMetadataPostgresBackend(pool)

        backend.record_fleet_migration_failure(
            "click", "sig-1", failure_cause="disk_headroom"
        )

        assert cursor.execute.call_count == 1
        sql_call = cursor.execute.call_args_list[0]
        assert "failure_cause" in str(sql_call[0][0])
        assert "disk_headroom" in sql_call[0][1]

    def test_repeated_failure_atomically_increments_count_via_on_conflict(
        self,
    ) -> None:
        """Finding L: a repeat failure for an already-tracked alias must
        route through the SAME single ON CONFLICT DO UPDATE statement --
        never a second, separate UPDATE issued after a prior SELECT. The
        mocked RETURNING value (3) represents what PostgreSQL itself
        computed server-side (`consecutive_failure_count + 1`); this test
        proves the driver-facing contract (one atomic round-trip), while
        the real increment arithmetic is proven for real against
        PostgreSQL by the live concurrency test."""
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool, conn, cursor = _make_mock_pool(fetchone_return=(3,))
        backend = GoldenRepoMetadataPostgresBackend(pool)

        count = backend.record_fleet_migration_failure("click", "sig-2")

        assert count == 3
        assert cursor.execute.call_count == 1
        sql_text = str(cursor.execute.call_args_list[0][0][0]).upper()
        assert "ON CONFLICT" in sql_text
        assert "CONSECUTIVE_FAILURE_COUNT" in sql_text
        assert "+ 1" in sql_text
        assert "?" not in sql_text
        params = cursor.execute.call_args_list[0][0][1]
        assert "sig-2" in params
        assert "click" in params
        conn.commit.assert_called()

    def test_reset_issues_delete(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool, conn, cursor = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(pool)

        backend.reset_fleet_migration_failure("click")

        delete_calls = [
            c for c in cursor.execute.call_args_list if "DELETE" in str(c[0][0]).upper()
        ]
        assert len(delete_calls) == 1
        assert "?" not in str(delete_calls[0][0][0])
        assert delete_calls[0][0][1] == ("click",)
        conn.commit.assert_called()

    def test_get_state_returns_none_when_no_row(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool, conn, cursor = _make_mock_pool(fetchone_return=None)
        backend = GoldenRepoMetadataPostgresBackend(pool)

        assert backend.get_fleet_migration_failure_state("click") is None
        select_calls = [
            c for c in cursor.execute.call_args_list if "SELECT" in str(c[0][0]).upper()
        ]
        assert len(select_calls) == 1
        assert "?" not in str(select_calls[0][0][0])
        assert select_calls[0][0][1] == ("click",)

    def test_get_state_returns_dict_when_row_present(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool, conn, cursor = _make_mock_pool(
            fetchone_return=(
                "click",
                3,
                "sig-3",
                "2026-01-01T00:00:00",
                "2026-01-02T00:00:00",
                "2026-01-02T00:00:00",
                "generic",
            )
        )
        backend = GoldenRepoMetadataPostgresBackend(pool)

        state = backend.get_fleet_migration_failure_state("click")

        assert state == {
            "golden_alias": "click",
            "consecutive_failure_count": 3,
            "state_signature": "sig-3",
            "first_failed_at": "2026-01-01T00:00:00",
            "last_failed_at": "2026-01-02T00:00:00",
            "signature_checked_at": "2026-01-02T00:00:00",
            "failure_cause": "generic",
        }

    def test_list_states_returns_all_rows(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool, conn, cursor = _make_mock_pool()
        cursor.fetchall.return_value = [
            (
                "click",
                3,
                "sig-3",
                "2026-01-01T00:00:00",
                "2026-01-02T00:00:00",
                "2026-01-02T00:00:00",
                "generic",
            ),
            (
                "evolution",
                1,
                "sig-e",
                "2026-01-03T00:00:00",
                "2026-01-03T00:00:00",
                "2026-01-03T00:00:00",
                "disk_headroom",
            ),
        ]
        backend = GoldenRepoMetadataPostgresBackend(pool)

        rows = backend.list_fleet_migration_failure_states()

        assert len(rows) == 2
        assert rows[0]["golden_alias"] == "click"
        assert rows[1]["golden_alias"] == "evolution"
        select_calls = [
            c for c in cursor.execute.call_args_list if "SELECT" in str(c[0][0]).upper()
        ]
        assert len(select_calls) == 1
        assert "fleet_migration_quarantine_state" in select_calls[0][0][0]


class TestGoldenRepoMetadataPostgresBackendSoftResetFleetMigrationFailureCount:
    """Finding N (Codex round-7 review): a fallback used when the full
    reset (DELETE) fails but a plain UPDATE still works -- zeroes
    `consecutive_failure_count` while KEEPING the row."""

    def test_soft_reset_issues_update_not_delete(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool, conn, cursor = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(pool)

        backend.soft_reset_fleet_migration_failure_count("click")

        assert cursor.execute.call_count == 1
        sql_text = str(cursor.execute.call_args_list[0][0][0]).upper()
        assert "UPDATE" in sql_text
        assert "DELETE" not in sql_text
        assert "CONSECUTIVE_FAILURE_COUNT" in sql_text
        assert "?" not in sql_text
        params = cursor.execute.call_args_list[0][0][1]
        assert 0 in params
        assert "click" in params
        conn.commit.assert_called()


class TestGoldenRepoMetadataPostgresBackendTouchFleetMigrationFailureCheck:
    """Finding C (Codex round-3 review): the drop-in PostgreSQL
    (cluster-mode) mirror of the SQLite `touch_fleet_migration_failure_check`
    throttle-bookkeeping method."""

    def test_touch_issues_update_on_signature_checked_at(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool, conn, cursor = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(pool)
        golden_alias = "click"

        backend.touch_fleet_migration_failure_check(golden_alias)

        update_calls = [
            c for c in cursor.execute.call_args_list if "UPDATE" in str(c[0][0]).upper()
        ]
        assert len(update_calls) == 1
        assert "signature_checked_at" in str(update_calls[0][0][0])
        # psycopg v3 uses %s placeholders, never sqlite's "?".
        assert "%s" in str(update_calls[0][0][0])
        assert "?" not in str(update_calls[0][0][0])
        assert golden_alias in update_calls[0][0][1]
        conn.commit.assert_called()
