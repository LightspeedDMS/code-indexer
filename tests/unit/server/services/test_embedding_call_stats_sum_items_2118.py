"""Story #2118 slice 1b: per-alias refresh spend aggregate on both stats
backends.

``sum_items_since(purposes, since, golden_repo_alias=None)`` returns
``{alias_or_None: sum(item_count)}`` for rows of the given purposes at or
after ``since``. It RAISES on a store error (the volume alarm must report
"blind", never "0 spend"). Real stores: SQLite always; PostgreSQL when
``TEST_POSTGRES_DSN`` is set (a throwaway database migrated by the real
``MigrationRunner``, so migration 067's index is exercised too).
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest

from code_indexer.server.services.embedding_call_stats import (
    EmbeddingCallRecord,
    EmbeddingCallStatsSqliteBackend,
)

PG_DSN = os.environ.get("TEST_POSTGRES_DSN", "")
ALIAS_A = "repo-a-global"
ALIAS_B = "repo-b-global"
INDEX_NAME = "idx_ecs_alias_purpose_time"


def _record(purpose: str, items: int, alias: Any, occurred_at: float):
    return EmbeddingCallRecord(
        provider="voyageai",
        call_type="embed",
        model="voyage-code-3",
        item_count=items,
        token_count=items * 10,
        batch_size=items,
        purpose=purpose,
        success=True,
        latency_ms=5,
        occurred_at=occurred_at,
        golden_repo_alias=alias,
        job_id="job-1",
        node_id="node-1",
    )


def _pg_dsn_for(dbname: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(PG_DSN)
    params["dbname"] = dbname
    return make_conninfo(**params)  # type: ignore[arg-type]


@contextlib.contextmanager
def _pg_backend() -> Iterator[Any]:
    if not PG_DSN:
        pytest.skip("TEST_POSTGRES_DSN not set; PostgreSQL parity not run")
    import psycopg

    from code_indexer.server.services.embedding_call_stats import (
        EmbeddingCallStatsPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    name = f"story2118_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    pool: Any = None
    try:
        with MigrationRunner(_pg_dsn_for(name)) as runner:
            runner.run()
        pool = ConnectionPool(_pg_dsn_for(name), min_size=1, max_size=2)
        yield EmbeddingCallStatsPostgresBackend(pool)
    finally:
        if pool is not None:
            pool.close()
        with psycopg.connect(PG_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture(params=("sqlite", "postgres"))
def backend(request, tmp_path: Path) -> Iterator[Any]:
    if request.param == "sqlite":
        yield EmbeddingCallStatsSqliteBackend(str(tmp_path / "stats.db"))
        return
    with _pg_backend() as pg:
        yield pg


REFRESH = ("refresh", "temporal_refresh")


def test_sums_refresh_and_temporal_refresh_per_alias_since_cutoff(backend):
    now = time.time()
    backend.insert_batch(
        [
            _record("refresh", 100, ALIAS_A, now - 60),
            _record("temporal_refresh", 40, ALIAS_A, now - 30),
            _record("refresh", 7, ALIAS_B, now - 10),
            # Registration and add-index spend never counts as refresh spend.
            _record("index", 5000, ALIAS_A, now - 20),
            _record("temporal", 3000, ALIAS_A, now - 20),
            _record("query", 900, ALIAS_A, now - 20),
            # Older than the cutoff.
            _record("refresh", 999, ALIAS_A, now - 7200),
        ]
    )

    sums = backend.sum_items_since(REFRESH, now - 3600)

    assert sums == {ALIAS_A: 140, ALIAS_B: 7}


def test_unattributed_rows_are_reported_under_none(backend):
    now = time.time()
    backend.insert_batch(
        [
            _record("index", 12, None, now - 5),
            _record("refresh", 3, ALIAS_A, now - 5),
        ]
    )

    sums = backend.sum_items_since(("index", "refresh", "temporal_refresh"), now - 60)

    assert sums == {None: 12, ALIAS_A: 3}


def test_alias_filter_returns_only_that_alias(backend):
    now = time.time()
    backend.insert_batch(
        [
            _record("refresh", 11, ALIAS_A, now - 5),
            _record("refresh", 22, ALIAS_B, now - 5),
        ]
    )

    assert backend.sum_items_since(REFRESH, now - 60, golden_repo_alias=ALIAS_A) == {
        ALIAS_A: 11
    }
    assert (
        backend.sum_items_since(REFRESH, now - 60, golden_repo_alias="absent-global")
        == {}
    )


def test_unknown_purpose_is_rejected(backend):
    with pytest.raises(ValueError):
        backend.sum_items_since(("refresh", "not-a-purpose"), 0.0)
    with pytest.raises(ValueError):
        backend.sum_items_since((), 0.0)


def test_sqlite_read_failure_raises_never_reports_zero(tmp_path: Path):
    stats = EmbeddingCallStatsSqliteBackend(str(tmp_path / "stats.db"))
    Path(stats._db_path).write_bytes(b"not a valid sqlite file")

    with pytest.raises(sqlite3.DatabaseError):
        stats.sum_items_since(REFRESH, 0.0)


def test_sqlite_schema_has_alias_purpose_time_index(tmp_path: Path):
    stats = EmbeddingCallStatsSqliteBackend(str(tmp_path / "stats.db"))
    conn = sqlite3.connect(stats._db_path)
    try:
        columns = [row[2] for row in conn.execute(f"PRAGMA index_info({INDEX_NAME})")]
    finally:
        conn.close()

    assert columns == ["golden_repo_alias", "purpose", "occurred_at"]


def test_postgres_migration_creates_alias_purpose_time_index():
    with _pg_backend() as pg:
        with pg._pool.connection() as conn:
            row = conn.execute(
                "SELECT indexdef FROM pg_indexes WHERE indexname = %s",
                (INDEX_NAME,),
            ).fetchone()

    assert row is not None
    assert "(golden_repo_alias, purpose, occurred_at)" in row[0]


FLEET_INDEX = "idx_ecs_purpose_time_alias"


def _fleet_sql(placeholder: str):
    from code_indexer.server.services.embedding_call_stats import (
        _build_sum_items_sql,
    )

    return _build_sum_items_sql(placeholder, REFRESH, 0.0, None)


def test_sqlite_fleet_query_searches_the_purpose_time_index(tmp_path: Path):
    stats = EmbeddingCallStatsSqliteBackend(str(tmp_path / "stats.db"))
    sql, params = _fleet_sql("?")
    conn = sqlite3.connect(stats._db_path)
    try:
        plan = " | ".join(
            row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)
        )
    finally:
        conn.close()

    assert plan.startswith("SEARCH"), plan
    assert (
        f"USING COVERING INDEX {FLEET_INDEX} (purpose=? AND occurred_at>?)" in plan
    ), plan


def test_postgres_migration_creates_the_purpose_time_index():
    with _pg_backend() as pg:
        with pg._pool.connection() as conn:
            row = conn.execute(
                "SELECT indexdef FROM pg_indexes WHERE indexname = %s",
                (FLEET_INDEX,),
            ).fetchone()
            conn.execute("SET enable_seqscan = off")
            sql, params = _fleet_sql("%s")
            plan = " ".join(
                r[0] for r in conn.execute(f"EXPLAIN {sql}", params).fetchall()
            )

    assert row is not None
    assert "(purpose, occurred_at, golden_repo_alias, item_count)" in row[0]
    assert FLEET_INDEX in plan


def test_postgres_read_failure_raises_never_reports_zero():
    with _pg_backend() as pg:
        with pg._pool.connection() as conn:
            conn.execute("ALTER TABLE embedding_call_stats RENAME TO ecs_gone")
            conn.commit()

        with pytest.raises(Exception) as exc_info:
            pg.sum_items_since(REFRESH, 0.0)

    assert "embedding_call_stats" in str(exc_info.value)
