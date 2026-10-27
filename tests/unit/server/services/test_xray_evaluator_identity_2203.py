"""Story #2203: which X-Ray evaluator code ran is recorded.

Store level: the evaluator source is stored once per SHA-256 in a table next
to search_event_log, written by the search event writer; the row carries the
hash and the stored pattern name. Real stores only (SQLite and PostgreSQL).
"""

import hashlib
import sqlite3
import uuid
from pathlib import Path
from typing import Iterator

import pytest

from code_indexer.server.services.xray_evaluator_sources import (
    EVALUATOR_SOURCE_MAX_CHARS,
    evaluator_source,
)
from tests.unit.server.services.search_event_real_stores import (
    PG_AVAILABLE,
    PG_SKIP_REASON,
    TEST_DSN,
    RealStore,
    make_record,
    open_store,
)

_KINDS = [
    "sqlite",
    pytest.param(
        "postgres", marks=pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON)
    ),
]

_SOURCE_A = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }\n"
_SOURCE_B = "fn evaluate_node(_n: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }\n"
_NEW_QUERY = "def example_"
_LEGACY_QUERY = "legacy example"


@pytest.fixture(params=_KINDS)
def store(request, tmp_path) -> Iterator[RealStore]:
    with open_store(request.param, tmp_path) as real_store:
        yield real_store


def _xray_record(*sources: str, pattern_name=None, query_text=_NEW_QUERY):
    stored = tuple(evaluator_source(s) for s in sources)
    return make_record(
        search_type="xray",
        query_text=query_text,
        evaluator_sha256=",".join(s.sha256 for s in stored) or None,
        xray_pattern_name=pattern_name,
        evaluator_sources=stored,
    )


class TestEvaluatorSourceTable:
    def test_same_source_is_stored_once_a_new_source_adds_a_row(self, store):
        store.backend.insert_batch([_xray_record(_SOURCE_A)])
        store.backend.insert_batch([_xray_record(_SOURCE_A), _xray_record(_SOURCE_B)])

        hash_a = hashlib.sha256(_SOURCE_A.encode("utf-8")).hexdigest()
        hash_b = hashlib.sha256(_SOURCE_B.encode("utf-8")).hexdigest()
        assert store.backend.evaluator_sources([hash_a, hash_b, "0" * 64]) == {
            hash_a: (_SOURCE_A, False),
            hash_b: (_SOURCE_B, False),
        }
        assert store.evaluator_source_rows() == 2

    def test_a_source_over_the_cap_is_stored_cut_and_flagged(self, store):
        long_source = "/" * (EVALUATOR_SOURCE_MAX_CHARS + 7)
        store.backend.insert_batch([_xray_record(long_source)])

        full_hash = hashlib.sha256(long_source.encode("utf-8")).hexdigest()
        stored, truncated = store.backend.evaluator_sources([full_hash])[full_hash]
        assert truncated is True
        assert stored == long_source[:EVALUATOR_SOURCE_MAX_CHARS]

    def test_row_carries_hash_and_pattern_name_legacy_rows_read_null(self, store):
        store.bulk_insert([make_record(search_type="xray", query_text=_LEGACY_QUERY)])
        store.backend.insert_batch(
            [_xray_record(_SOURCE_A, pattern_name="catch-rethrow")]
        )

        events, _ = store.backend.query(search_type="xray", limit=10)
        by_query = {e["query_text"]: e for e in events}
        new_row = by_query[_NEW_QUERY]
        assert new_row["evaluator_sha256"] == evaluator_source(_SOURCE_A).sha256
        assert new_row["xray_pattern_name"] == "catch-rethrow"
        legacy = by_query[_LEGACY_QUERY]
        assert (legacy["evaluator_sha256"], legacy["xray_pattern_name"]) == (None, None)

    def test_retention_prune_drops_sources_no_kept_row_names(self, store):
        old = _xray_record(_SOURCE_A)
        old.timestamp = 1_000.0
        kept = _xray_record(_SOURCE_B)
        kept.timestamp = 5_000.0
        store.backend.insert_batch([old, kept])

        store.backend.prune_older_than(2_000.0)

        hash_a = evaluator_source(_SOURCE_A).sha256
        hash_b = evaluator_source(_SOURCE_B).sha256
        assert store.backend.evaluator_sources([hash_a, hash_b]) == {
            hash_b: (_SOURCE_B, False)
        }


_MIGRATIONS = (
    Path(__file__).resolve().parents[4]
    / "src/code_indexer/server/storage/postgres/migrations/sql"
)
_OLD_ROW = (
    "INSERT INTO search_event_log (timestamp, username, search_type, query_text,"
    " total_latency_ms, result_count, node_id) VALUES"
    f" (1.0, 'alice', 'xray', '{_LEGACY_QUERY}', 5, 1, 'node-1')"
)


def _identity(rows: list) -> dict:
    return {
        r["query_text"]: (r["evaluator_sha256"], r["xray_pattern_name"]) for r in rows
    }


class TestSchemaUpgrade:
    def test_sqlite_table_from_before_2203_gains_columns_and_source_table(
        self, tmp_path
    ):
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogSqliteBackend,
        )
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = str(tmp_path / "cidx_server.db")
        DatabaseSchema(db_path).initialize_database()
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(_OLD_ROW)
            conn.commit()
        finally:
            conn.close()

        backend = SearchEventLogSqliteBackend(db_path)
        backend.insert_batch([_xray_record(_SOURCE_A, pattern_name="deep-nesting")])
        events, _ = backend.query(limit=10)

        hash_a = evaluator_source(_SOURCE_A).sha256
        assert _identity(events) == {
            _LEGACY_QUERY: (None, None),
            _NEW_QUERY: (hash_a, "deep-nesting"),
        }
        assert backend.evaluator_sources([hash_a]) == {hash_a: (_SOURCE_A, False)}

    @pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON)
    def test_postgres_migration_078_applies_on_a_populated_table(self):
        import psycopg
        from psycopg.conninfo import make_conninfo

        [migration] = sorted(_MIGRATIONS.glob("078_*.sql"))
        schema = f"t_xes_{uuid.uuid4().hex[:12]}"
        dsn = make_conninfo(TEST_DSN, options=f"-csearch_path={schema}")
        with psycopg.connect(TEST_DSN, autocommit=True) as admin:
            admin.execute(f"CREATE SCHEMA {schema}")
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                for earlier in ("030", "073", "074"):
                    [path] = sorted(_MIGRATIONS.glob(f"{earlier}_*.sql"))
                    conn.execute(path.read_text())
                conn.execute(_OLD_ROW)
                conn.execute(migration.read_text())
                conn.execute(migration.read_text())  # idempotent re-run
                columns = {
                    r[0]
                    for r in conn.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = %s AND table_name = 'search_event_log'",
                        (schema,),
                    ).fetchall()
                }
                legacy = conn.execute(
                    "SELECT evaluator_sha256, xray_pattern_name FROM search_event_log"
                ).fetchall()
                conn.execute(
                    "INSERT INTO xray_evaluator_sources (evaluator_sha256, source,"
                    " source_truncated, first_seen, last_seen)"
                    " VALUES ('ab', 'fn x() {}', FALSE, 1.0, 1.0)"
                )
        finally:
            with psycopg.connect(TEST_DSN, autocommit=True) as admin:
                admin.execute(f"DROP SCHEMA {schema} CASCADE")

        assert {"evaluator_sha256", "xray_pattern_name"} <= columns
        assert legacy == [(None, None)]


_XLSX_CELL_MAX = 32_767


def _export_rows(store: RealStore, directory: Path) -> dict:
    import openpyxl

    from code_indexer.server.services.query_analytics_export_service import (
        QueryAnalyticsExportService,
    )

    service = QueryAnalyticsExportService(
        backend=store.export_backend, golden_repos_dir=str(directory)
    )
    export_id = str(uuid.uuid4())  # uuid: imported at module top
    service.run_export(
        export_id=export_id,
        filters={"search_type": "xray"},
        initiated_by="admin",
        export_retention_days=7,
        search_event_log_backend=store.backend,
    )
    [record] = service.list_exports(export_id=export_id)
    sheet = openpyxl.load_workbook(record["file_path"]).worksheets[0]
    header, *data = [list(r) for r in sheet.iter_rows(values_only=True)]
    return {row["query_text"]: row for row in (dict(zip(header, d)) for d in data)}


class TestExport:
    def test_export_has_source_cut_at_the_cell_limit_and_the_pattern_name(
        self, store, tmp_path
    ):
        long_source = "x" * 40_000
        store.bulk_insert([make_record(search_type="xray", query_text=_LEGACY_QUERY)])
        store.backend.insert_batch(
            [
                _xray_record(_SOURCE_A, pattern_name="catch-rethrow"),
                _xray_record(long_source, query_text="long"),
            ]
        )

        rows = _export_rows(store, tmp_path)

        short = rows[_NEW_QUERY]
        assert short["evaluator_sha256"] == evaluator_source(_SOURCE_A).sha256
        assert short["xray_pattern_name"] == "catch-rethrow"
        assert short["evaluator_source"] == _SOURCE_A
        assert short["evaluator_source_truncated"] is False
        cut = rows["long"]
        assert cut["evaluator_source"] == long_source[:_XLSX_CELL_MAX]
        assert cut["evaluator_source_truncated"] is True
        legacy = rows[_LEGACY_QUERY]
        assert legacy["evaluator_source"] is None
        assert legacy["evaluator_source_truncated"] is None


def _orphaned_hashes(backend) -> list:
    """Hashes named by a stored row whose source is not stored."""
    events, _ = backend.query(search_type="xray", limit=1000)
    named = {h for e in events for h in (e["evaluator_sha256"] or "").split(",") if h}
    return sorted(named - set(backend.evaluator_sources(named)))


def _timed(record, timestamp: float):
    record.timestamp = timestamp
    return record


def _wait_for_lock_waiters(conn, count: int) -> None:
    """Bounded poll until `count` lock requests are waiting."""
    import time

    for _ in range(200):
        waiting = conn.execute("SELECT count(*) FROM pg_locks WHERE NOT granted")
        if waiting.fetchone()[0] >= count:
            return
        time.sleep(0.05)
    raise AssertionError(f"fewer than {count} lock waiters after 10 s")


class TestRetentionNeverOrphansASource:
    def test_old_event_written_after_prune_keeps_its_source(self, store):
        store.backend.insert_batch([_timed(_xray_record(_SOURCE_A), 1_000.0)])
        store.backend.prune_older_than(2_000.0)

        store.backend.insert_batch(
            [_timed(_xray_record(_SOURCE_A, query_text="late"), 1_500.0)]
        )

        assert _orphaned_hashes(store.backend) == []

    @pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON)
    def test_postgres_prune_racing_a_delayed_old_write_never_orphans_its_source(
        self,
    ):
        """A write with an older-than-cutoff timestamp (node clock skew, a
        delayed queue) commits between prune's two deletes: a row lock on
        the source holds both until the write is ahead of the prune."""
        import threading

        import psycopg
        from psycopg.conninfo import make_conninfo

        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogPostgresBackend,
        )
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )

        schema = f"t_xes_race_{uuid.uuid4().hex[:12]}"
        dsn = make_conninfo(TEST_DSN, options=f"-csearch_path={schema}")
        with psycopg.connect(TEST_DSN, autocommit=True) as admin:
            admin.execute(f"CREATE SCHEMA {schema}")
        pool = ConnectionPool(dsn, max_size=4)
        try:
            backend = SearchEventLogPostgresBackend(pool)
            backend.insert_batch([_timed(_xray_record(_SOURCE_A), 1_000.0)])
            delayed = _timed(_xray_record(_SOURCE_A, query_text="delayed"), 1_500.0)
            with (
                psycopg.connect(dsn) as holder,
                psycopg.connect(dsn, autocommit=True) as observer,
            ):
                holder.execute(
                    "SELECT 1 FROM xray_evaluator_sources "
                    "WHERE evaluator_sha256 = %s FOR UPDATE",
                    (evaluator_source(_SOURCE_A).sha256,),
                )
                writer = threading.Thread(
                    target=backend.insert_batch, args=([delayed],), daemon=True
                )
                writer.start()
                _wait_for_lock_waiters(observer, 1)
                pruner = threading.Thread(
                    target=backend.prune_older_than, args=(2_000.0,), daemon=True
                )
                pruner.start()
                _wait_for_lock_waiters(observer, 2)
                holder.rollback()
                writer.join(timeout=30)
                pruner.join(timeout=30)
            assert not writer.is_alive() and not pruner.is_alive()
            orphans = _orphaned_hashes(backend)
        finally:
            pool.close()
            with psycopg.connect(TEST_DSN, autocommit=True) as admin:
                admin.execute(f"DROP SCHEMA {schema} CASCADE")

        assert orphans == []
