"""X-Ray's migration 1: the former core migration 051, now the feature's own.

Backward compatibility (phase 4c): a PostgreSQL deployment whose core runner
already applied ``051_xray_graph_k_calibration_samples.sql`` must not run it
again -- version 1 sees 051 in ``schema_migrations``, executes no DDL, and the
runner records version 1. A fresh database gets the table from version 1.
SQLite (solo) never had 051 and gets the SQLite form of the table.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence, Tuple

import pytest

from cidx_xray.server.migrations import CORE_MIGRATION_051, XRAY_MIGRATIONS
from code_indexer.server.features.migrations import (
    feature_schema_versions_sqlite,
    run_feature_migrations_sqlite,
)

_TABLE = "xray_graph_k_calibration_samples"


# ── SQLite (solo) ────────────────────────────────────────────────────────────


def _tables(db: Path) -> List[str]:
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        )
        return sorted(str(row[0]) for row in rows)
    finally:
        conn.close()


def test_sqlite_fresh_database_gets_the_table_and_version_1(tmp_path: Path) -> None:
    db = tmp_path / "cidx_server.db"
    applied = run_feature_migrations_sqlite(str(db), [("xray", XRAY_MIGRATIONS)])
    assert applied == {"xray": 1}
    assert feature_schema_versions_sqlite(str(db)) == {"xray": 1}
    assert _TABLE in _tables(db)
    assert "idx_xray_k_calibration_language" in _tables(db)


def test_sqlite_second_run_applies_nothing(tmp_path: Path) -> None:
    db = tmp_path / "cidx_server.db"
    run_feature_migrations_sqlite(str(db), [("xray", XRAY_MIGRATIONS)])
    assert run_feature_migrations_sqlite(str(db), [("xray", XRAY_MIGRATIONS)]) == {
        "xray": 0
    }


def test_sqlite_store_reads_and_writes_through_a_connection_factory(
    tmp_path: Path,
) -> None:
    from contextlib import contextmanager

    from cidx_xray.server.graph_governor.k_calibration_store import (
        KCalibrationSample,
        SqliteKCalibrationBackend,
    )

    db = tmp_path / "cidx_server.db"
    run_feature_migrations_sqlite(str(db), [("xray", XRAY_MIGRATIONS)])

    @contextmanager
    def connect() -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(db)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    store = SqliteKCalibrationBackend(connect)
    assert store.get_k("python") is None
    store.record_sample(KCalibrationSample("python", 100, 1, 1, 1, 450))
    store.record_sample(KCalibrationSample("python", 100, 1, 1, 1, 300))
    assert store.get_k("python") == pytest.approx(4.5)


# ── PostgreSQL: the 051 import, against a recording connection ───────────────


class _Cursor:
    def __init__(self, conn: "_RecordingPg") -> None:
        self._conn = conn
        self._row: Optional[Tuple[Any, ...]] = None

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self._conn.executed.append(sql.strip())
        if "to_regclass('schema_migrations')" in sql:
            self._row = (self._conn.has_ledger,)
        elif "FROM schema_migrations" in sql:
            applied = self._conn.has_ledger and params[0] in self._conn.ledger
            self._row = (1,) if applied else None
        else:
            self._row = None

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        return self._row


class _RecordingPg:
    """The part of a psycopg connection the migration uses, recording SQL."""

    def __init__(self, ledger: Sequence[str], has_ledger: bool = True) -> None:
        self.ledger = list(ledger)
        self.has_ledger = has_ledger
        self.executed: List[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def ddl(self) -> List[str]:
        return [
            sql for sql in self.executed if sql.upper().startswith(("CREATE", "--"))
        ]


def _apply(conn: Any) -> None:
    (migration,) = XRAY_MIGRATIONS
    assert migration.version == 1
    migration.apply(conn)


def test_pg_with_core_051_applied_runs_no_ddl() -> None:
    conn = _RecordingPg(ledger=["050_x.sql", CORE_MIGRATION_051, "052_y.sql"])
    _apply(conn)
    assert conn.ddl() == []
    assert any("schema_migrations" in sql for sql in conn.executed)


def test_pg_fresh_database_creates_the_table() -> None:
    conn = _RecordingPg(ledger=["050_x.sql"])
    _apply(conn)
    (ddl,) = conn.ddl()
    assert f"CREATE TABLE IF NOT EXISTS {_TABLE}" in ddl
    assert "CREATE INDEX IF NOT EXISTS idx_xray_k_calibration_language" in ddl


def test_pg_without_a_ledger_creates_the_table() -> None:
    conn = _RecordingPg(ledger=[], has_ledger=False)
    _apply(conn)
    assert len(conn.ddl()) == 1


def test_core_no_longer_ships_migration_051() -> None:
    sql_dir = (
        Path(__file__).resolve().parents[3]
        / "src/code_indexer/server/storage/postgres/migrations/sql"
    )
    assert not (sql_dir / CORE_MIGRATION_051).exists()


# ── PostgreSQL, live (TEST_POSTGRES_DSN): through the core's runner ──────────


@pytest.fixture
def live_pg_dsn() -> str:
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    psycopg = pytest.importorskip("psycopg")
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    return dsn


def _reset_live(dsn: str, with_core_051: bool) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {_TABLE}")
        conn.execute("DELETE FROM feature_schema_versions WHERE feature = 'xray'")
        conn.execute(
            "DELETE FROM schema_migrations WHERE filename = %s", (CORE_MIGRATION_051,)
        )
        if with_core_051:
            conn.execute(
                f"CREATE TABLE {_TABLE} (id BIGSERIAL PRIMARY KEY, sentinel TEXT)"
            )
            conn.execute(
                "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, 'x')",
                (CORE_MIGRATION_051,),
            )


@pytest.mark.parametrize("with_core_051", [False, True])
def test_live_pg_runner_imports_or_applies(
    live_pg_dsn: str, with_core_051: bool
) -> None:
    import psycopg

    from code_indexer.server.storage.postgres.migrations.runner import MigrationRunner

    with MigrationRunner(live_pg_dsn) as runner:
        runner.run()  # the core schema, ledger and feature version table
    _reset_live(live_pg_dsn, with_core_051)
    with MigrationRunner(live_pg_dsn) as runner:
        applied = runner.run_feature_migrations([("xray", XRAY_MIGRATIONS)])
    assert applied == {"xray": 1}
    with psycopg.connect(live_pg_dsn) as conn:
        version = conn.execute(
            "SELECT version FROM feature_schema_versions WHERE feature = 'xray'"
        ).fetchone()
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s",
                (_TABLE,),
            )
        }
    assert version == (1,)
    # With 051 applied, the existing table (a sentinel shape) was left as is.
    assert ("sentinel" in columns) is with_core_051
