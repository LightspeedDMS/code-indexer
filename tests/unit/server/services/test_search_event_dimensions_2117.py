"""Story #2117: tool, door and outcome dimensions of the search event log.

Store level, against the REAL stores (SQLite and PostgreSQL):
- the four nullable columns (tool, door, outcome, error_class) round-trip;
- a table created before they existed gains them in place, old rows NULL;
- PostgreSQL migration 074 adds them idempotently;
- the export read filters on door, outcome, tool and correlation id in SQL.
"""

import sqlite3
import uuid
from pathlib import Path
from typing import Iterator, List

import pytest

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

_MIGRATIONS = (
    Path(__file__).resolve().parents[4]
    / "src/code_indexer/server/storage/postgres/migrations/sql"
)

_NEW_COLUMNS = ("tool", "door", "outcome", "error_class")


@pytest.fixture(params=_KINDS)
def store(request, tmp_path: Path) -> Iterator[RealStore]:
    with open_store(request.param, tmp_path) as real_store:
        yield real_store


def _dimensions(row: dict) -> tuple:
    return tuple(row[c] for c in _NEW_COLUMNS)


class TestRoundTrip:
    def test_written_dimensions_are_read_back_by_both_reads(self, store):
        store.backend.insert_batch(
            [
                make_record(
                    query_text="ok",
                    tool="search_code",
                    door="mcp",
                    outcome="success",
                    timestamp=1.0,
                ),
                make_record(
                    query_text="bad",
                    tool="POST /api/query",
                    door="rest",
                    outcome="failure",
                    error_class="timeout",
                    timestamp=2.0,
                ),
            ]
        )

        events, _ = store.backend.query(limit=10)
        exported = store.backend.query_for_export({}, limit=10)

        expected = [
            ("POST /api/query", "rest", "failure", "timeout"),
            ("search_code", "mcp", "success", None),
        ]
        assert [_dimensions(e) for e in events] == expected
        assert [_dimensions(e) for e in exported] == list(reversed(expected))


def _export_texts(store: RealStore, **filters) -> List[str]:
    return [r["query_text"] for r in store.backend.query_for_export(filters, limit=50)]


class TestExportFilters:
    @pytest.fixture
    def seeded(self, store: RealStore) -> RealStore:
        store.backend.insert_batch(
            [
                make_record(
                    query_text="mcp ok",
                    tool="search_code",
                    door="mcp",
                    outcome="success",
                    correlation_id="corr-1",
                    timestamp=1.0,
                ),
                make_record(
                    query_text="rest failed",
                    tool="POST /api/query",
                    door="rest",
                    outcome="failure",
                    error_class="timeout",
                    correlation_id="corr-2",
                    timestamp=2.0,
                ),
                make_record(query_text="legacy", timestamp=3.0),
            ]
        )
        return store

    def test_door_filter(self, seeded):
        assert _export_texts(seeded, door="rest") == ["rest failed"]

    def test_outcome_filter(self, seeded):
        assert _export_texts(seeded, outcome="success") == ["mcp ok"]
        assert _export_texts(seeded, outcome="failure") == ["rest failed"]

    def test_tool_filter_is_an_exact_match(self, seeded):
        assert _export_texts(seeded, tool="search_code") == ["mcp ok"]
        assert _export_texts(seeded, tool="search") == []

    def test_correlation_id_filter(self, seeded):
        assert _export_texts(seeded, correlation_id="corr-2") == ["rest failed"]

    def test_no_dimension_filter_keeps_legacy_rows(self, seeded):
        assert _export_texts(seeded) == ["mcp ok", "rest failed", "legacy"]

    def test_over_long_tool_or_correlation_id_is_refused(self, seeded):
        with pytest.raises(ValueError):
            _export_texts(seeded, tool="t" * 101)
        with pytest.raises(ValueError):
            _export_texts(seeded, correlation_id="c" * 129)
        with pytest.raises(ValueError):
            seeded.backend.query(limit=10, tool="t" * 101)

    def test_unknown_door_or_outcome_is_refused(self, seeded):
        with pytest.raises(ValueError):
            _export_texts(seeded, door="ftp")
        with pytest.raises(ValueError):
            _export_texts(seeded, outcome="partial")


class TestReaders:
    def test_hit_rate_counts_ignore_failed_searches_and_keep_legacy_rows(self, store):
        """The dashboard hit rate counts searches: a failed search is not one
        (it would skew the rate); a row from before the outcome column existed
        (NULL) is counted as it always was."""
        store.backend.insert_batch(
            [
                make_record(
                    voyage_cache_hit=True, voyage_cache_mode="on", outcome="success"
                ),
                make_record(
                    voyage_cache_hit=True,
                    voyage_cache_mode="on",
                    outcome="failure",
                    error_class="timeout",
                ),
                make_record(voyage_cache_hit=False, voyage_cache_mode="on"),
            ]
        )
        assert store.backend.get_hit_rate_counts("on") == {"hits": 1, "requests": 2}

    def test_hit_rate_counts_rows(self, store):
        """Each row is one searched repository's own cache lookup (Bug
        #2111), so the hit rate counts ROWS. Rows sharing a correlation id
        are never merged: the id comes from a client header and can be
        reused across separate searches."""
        one_request = [
            make_record(
                voyage_cache_hit=hit,
                voyage_cache_mode="on",
                correlation_id="req-1",
                repo_alias=alias,
            )
            for alias, hit in (("a-repo", True), ("b-repo", True), ("c-repo", False))
        ]
        legacy = [
            make_record(voyage_cache_hit=True, voyage_cache_mode="on"),
            make_record(voyage_cache_hit=False, voyage_cache_mode="on"),
        ]
        store.backend.insert_batch(one_request + legacy)

        assert store.backend.get_hit_rate_counts("on") == {"hits": 3, "requests": 5}
        assert store.backend.get_hit_rate_counts(
            "on", from_ts=0.0, to_ts=10_000_000_000.0
        ) == {"hits": 3, "requests": 5}

    def test_windowed_hit_rate_read_seeks_the_timestamp_index(self, store):
        from code_indexer.server.services.search_event_log_writer import _hit_rate_sql

        ph, true_lit = ("?", "1") if store.kind == "sqlite" else ("%s", "TRUE")
        sql = _hit_rate_sql(ph, true_lit, windowed=True)
        plan = " ".join(store.explain(sql, ["on", "on", "on", "on", 1.0, 2.0]))
        assert "idx_sel_timestamp" in plan, plan

    def test_oldest_timestamp_is_the_earliest_row(self, store):
        assert store.backend.oldest_timestamp() is None
        store.backend.insert_batch(
            [make_record(timestamp=50.0), make_record(timestamp=20.0)]
        )
        assert store.backend.oldest_timestamp() == 20.0

    def test_oldest_timestamp_reads_the_timestamp_index(self, store):
        from code_indexer.server.services.search_event_log_writer import (
            OLDEST_TIMESTAMP_SQL,
        )

        plan = " ".join(store.explain(OLDEST_TIMESTAMP_SQL, []))
        assert "idx_sel_timestamp" in plan, plan


_OLD_SQLITE_TABLE = """
CREATE TABLE search_event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
    username TEXT NOT NULL, repo_alias TEXT, search_type TEXT NOT NULL,
    query_text TEXT NOT NULL, voyage_cache_hit INTEGER, voyage_cache_mode TEXT,
    voyage_latency_ms INTEGER, cohere_cache_hit INTEGER, cohere_cache_mode TEXT,
    cohere_latency_ms INTEGER, total_latency_ms INTEGER NOT NULL,
    result_count INTEGER NOT NULL, node_id TEXT NOT NULL, correlation_id TEXT,
    query_text_truncated INTEGER
)
"""
_OLD_ROW = (
    "INSERT INTO search_event_log (timestamp, username, search_type, query_text,"
    " total_latency_ms, result_count, node_id) VALUES"
    " (1.0, 'alice', 'fts', 'old query', 5, 1, 'node-1')"
)


class TestOldTablesGainTheColumns:
    def test_sqlite_table_from_before_the_columns_gains_them(self, tmp_path):
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogSqliteBackend,
        )

        db_path = str(tmp_path / "old.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(_OLD_SQLITE_TABLE)
            conn.execute(_OLD_ROW)
            conn.commit()
        finally:
            conn.close()

        backend = SearchEventLogSqliteBackend(db_path)
        backend.insert_batch(
            [
                make_record(
                    query_text="new",
                    tool="regex_search",
                    door="mcp",
                    outcome="success",
                    timestamp=2.0,
                )
            ]
        )
        events, _ = backend.query(limit=10)

        assert {e["query_text"]: _dimensions(e) for e in events} == {
            "old query": (None, None, None, None),
            "new": ("regex_search", "mcp", "success", None),
        }

    def test_database_schema_creates_the_columns(self, tmp_path):
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = str(tmp_path / "cidx_server.db")
        DatabaseSchema(db_path).initialize_database()
        conn = sqlite3.connect(db_path)
        try:
            names = [r[1] for r in conn.execute("PRAGMA table_info(search_event_log)")]
        finally:
            conn.close()
        assert names[-4:] == list(_NEW_COLUMNS)

    @pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON)
    def test_postgres_migration_074_adds_the_columns(self):
        import psycopg
        from psycopg.conninfo import make_conninfo

        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogPostgresBackend,
        )
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )

        [migration] = sorted(_MIGRATIONS.glob("074_*.sql"))
        schema = f"t_sel_{uuid.uuid4().hex[:12]}"
        dsn = make_conninfo(TEST_DSN, options=f"-csearch_path={schema}")
        with psycopg.connect(TEST_DSN, autocommit=True) as admin:
            admin.execute(f"CREATE SCHEMA {schema}")
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute((_MIGRATIONS / "030_search_event_log.sql").read_text())
                conn.execute(_OLD_ROW)
                conn.execute(migration.read_text())
                conn.execute(migration.read_text())  # idempotent re-run
            pool = ConnectionPool(dsn, max_size=2)
            try:
                backend = SearchEventLogPostgresBackend(pool)
                backend.insert_batch(
                    [
                        make_record(
                            query_text="new",
                            tool="xray_search",
                            door="mcp",
                            outcome="failure",
                            error_class="timeout",
                        )
                    ]
                )
                events, _ = backend.query(limit=10)
            finally:
                pool.close()
        finally:
            with psycopg.connect(TEST_DSN, autocommit=True) as admin:
                admin.execute(f"DROP SCHEMA {schema} CASCADE")

        assert {e["query_text"]: _dimensions(e) for e in events} == {
            "old query": (None, None, None, None),
            "new": ("xray_search", "mcp", "failure", "timeout"),
        }
