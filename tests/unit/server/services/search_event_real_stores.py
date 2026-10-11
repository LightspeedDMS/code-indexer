"""Real search_event_log stores for the Query Analytics Export tests (#2105/#2106).

- SQLite: a temp-file database in the given directory.
- PostgreSQL: a live database from TEST_POSTGRES_DSN, isolated in a throwaway
  schema (dropped on exit). Callers skip when PG_AVAILABLE is False.

Each store pairs the search_event_log backend with the query_analytics_exports
history backend of the SAME kind, as wired in production, plus helpers to
bulk-insert rows and to make every read fail by dropping the table.
"""

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, List, NamedTuple

from code_indexer.server.services.query_analytics_export_service import (
    QueryAnalyticsExportPostgresBackend,
    QueryAnalyticsExportSqliteBackend,
)
from code_indexer.server.services.search_event_log_writer import (
    SearchEventLogPostgresBackend,
    SearchEventLogSqliteBackend,
    SearchEventRecord,
)

try:
    import psycopg

    _HAS_PSYCOPG = True
except ImportError:
    _HAS_PSYCOPG = False

TEST_DSN = os.environ.get("TEST_POSTGRES_DSN", "")
PG_AVAILABLE = _HAS_PSYCOPG and bool(TEST_DSN)
PG_SKIP_REASON = "psycopg not installed or TEST_POSTGRES_DSN not set"

_INSERT_COLUMNS = (
    "timestamp, username, repo_alias, search_type, query_text, "
    "voyage_cache_hit, voyage_cache_mode, voyage_latency_ms, "
    "cohere_cache_hit, cohere_cache_mode, cohere_latency_ms, "
    "total_latency_ms, result_count, node_id, correlation_id"
)


class RealStore(NamedTuple):
    backend: Any
    export_backend: Any
    drop_table: Callable[[], None]
    bulk_insert: Callable[[List[SearchEventRecord]], None]
    kind: str
    # Plan lines for (sql, params): SQLite EXPLAIN QUERY PLAN details; PG
    # EXPLAIN with sequential scans disabled (test tables are tiny, so the
    # planner would otherwise never show which index conditions are usable).
    explain: Callable[[str, List[Any]], List[str]]


def make_record(**overrides: Any) -> SearchEventRecord:
    values: dict = dict(
        timestamp=time.time(),
        username="alice",
        repo_alias="example-repo",
        search_type="semantic",
        query_text="example query",
        voyage_cache_hit=None,
        voyage_cache_mode=None,
        voyage_latency_ms=None,
        cohere_cache_hit=None,
        cohere_cache_mode=None,
        cohere_latency_ms=None,
        total_latency_ms=10,
        result_count=1,
        node_id="node-1",
        correlation_id=None,
    )
    values.update(overrides)
    return SearchEventRecord(**values)


def _row_tuple(r: SearchEventRecord, as_int_bool: bool) -> tuple:
    def flag(v: Any) -> Any:
        if v is None or not as_int_bool:
            return v
        return int(v)

    return (
        r.timestamp,
        r.username,
        r.repo_alias,
        r.search_type,
        r.query_text,
        flag(r.voyage_cache_hit),
        r.voyage_cache_mode,
        r.voyage_latency_ms,
        flag(r.cohere_cache_hit),
        r.cohere_cache_mode,
        r.cohere_latency_ms,
        r.total_latency_ms,
        r.result_count,
        r.node_id,
        r.correlation_id,
    )


@contextmanager
def sqlite_store(directory: Path) -> Iterator[RealStore]:
    db_path = str(directory / "search_events.db")

    def run(sql: str, rows: Any = None) -> None:
        conn = sqlite3.connect(db_path)
        try:
            if rows is None:
                conn.execute(sql)
            else:
                conn.executemany(sql, rows)
            conn.commit()
        finally:
            conn.close()

    def bulk_insert(records: List[SearchEventRecord]) -> None:
        run(
            f"INSERT INTO search_event_log ({_INSERT_COLUMNS}) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [_row_tuple(r, as_int_bool=True) for r in records],
        )

    def explain(sql: str, params: List[Any]) -> List[str]:
        conn = sqlite3.connect(db_path)
        try:
            return [
                str(r[3]) for r in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)
            ]
        finally:
            conn.close()

    yield RealStore(
        backend=SearchEventLogSqliteBackend(db_path),
        export_backend=QueryAnalyticsExportSqliteBackend(db_path),
        drop_table=lambda: run("DROP TABLE search_event_log"),
        bulk_insert=bulk_insert,
        kind="sqlite",
        explain=explain,
    )


@contextmanager
def postgres_store() -> Iterator[RealStore]:
    from psycopg.conninfo import make_conninfo

    from code_indexer.server.storage.postgres.connection_pool import (
        ConnectionPool,
    )

    schema = f"t_qae_{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(TEST_DSN, autocommit=True)
    try:
        admin.execute(f"CREATE SCHEMA {schema}")
        try:
            pool = ConnectionPool(
                make_conninfo(TEST_DSN, options=f"-csearch_path={schema}"),
                max_size=4,
            )
            try:

                def drop_table() -> None:
                    with pool.connection() as conn:
                        conn.execute("DROP TABLE search_event_log")
                        conn.commit()

                def bulk_insert(records: List[SearchEventRecord]) -> None:
                    with pool.connection() as conn:
                        with conn.cursor() as cur:
                            cur.executemany(
                                f"INSERT INTO search_event_log ({_INSERT_COLUMNS}) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                                "%s, %s, %s, %s, %s)",
                                [_row_tuple(r, as_int_bool=False) for r in records],
                            )
                        conn.commit()

                def explain(sql: str, params: List[Any]) -> List[str]:
                    with pool.connection() as conn:
                        try:
                            conn.execute("SET LOCAL enable_seqscan = off")
                            return [
                                str(r[0])
                                for r in conn.execute(f"EXPLAIN {sql}", params)
                            ]
                        finally:
                            conn.rollback()

                yield RealStore(
                    backend=SearchEventLogPostgresBackend(pool),
                    export_backend=QueryAnalyticsExportPostgresBackend(pool),
                    drop_table=drop_table,
                    bulk_insert=bulk_insert,
                    kind="postgres",
                    explain=explain,
                )
            finally:
                pool.close()
        finally:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")
    finally:
        admin.close()


@contextmanager
def open_store(kind: str, directory: Path) -> Iterator[RealStore]:
    """Open the real store of the given kind ("sqlite" or "postgres")."""
    if kind == "sqlite":
        with sqlite_store(directory) as store:
            yield store
    else:
        with postgres_store() as store:
            yield store
