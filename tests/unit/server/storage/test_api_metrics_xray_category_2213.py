import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, List, Tuple, cast

import pytest

if TYPE_CHECKING:
    from code_indexer.server.storage.postgres.api_metrics_backend import (
        ApiMetricsPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool


def _min1_bucket() -> str:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()


@pytest.fixture
def sqlite_backend(tmp_path):
    from code_indexer.server.storage.sqlite_backends import ApiMetricsSqliteBackend

    return ApiMetricsSqliteBackend(str(tmp_path / "metrics.db"))


class TestSqliteBackendXrayCategory:
    def test_xray_rows_are_summed_into_xray_searches(self, sqlite_backend):
        bucket = _min1_bucket()
        sqlite_backend.upsert_bucket("alice", "min1", bucket, "xray")
        sqlite_backend.upsert_bucket("bob", "min1", bucket, "xray")
        sqlite_backend.upsert_bucket("alice", "min1", bucket, "regex")

        result = sqlite_backend.get_metrics_bucketed(period_seconds=900)

        assert result["xray_searches"] == 2
        assert result["regex_searches"] == 1
        assert result["other_api_calls"] == 0


class _FakePgConn:
    def __init__(self, pool: "_FakePgPool") -> None:
        self._pool = pool
        self._rows: List[Tuple[str, int]] = []

    def execute(self, sql: str, params: Any = None) -> "_FakePgConn":
        self._pool.executes.append((sql, params))
        self._rows = self._pool.summary_rows if "GROUP BY" in sql else []
        return self

    def commit(self) -> None:
        pass

    def fetchone(self):
        return None

    def fetchall(self):
        return self._rows


class _FakePgPool:
    def __init__(self, summary_rows: List[Tuple[str, int]]) -> None:
        self.summary_rows = summary_rows
        self.executes: List[Tuple[str, Any]] = []

    @contextmanager
    def connection(self):
        yield _FakePgConn(self)


def _pg_backend(pool: _FakePgPool) -> "ApiMetricsPostgresBackend":
    from code_indexer.server.storage.postgres.api_metrics_backend import (
        ApiMetricsPostgresBackend,
    )

    # ^ why cast, not a ConnectionPool subclass; the backend only calls pool.connection(), a real pool needs live psycopg
    return ApiMetricsPostgresBackend(cast("ConnectionPool", pool))


class TestPostgresBackendXrayCategory:
    def test_upsert_bucket_writes_an_xray_row(self):
        pool = _FakePgPool(summary_rows=[])
        backend = _pg_backend(pool)
        pool.executes.clear()

        backend.upsert_bucket("alice", "min1", _min1_bucket(), "xray")

        inserts = [params for sql, params in pool.executes if "INSERT INTO" in sql]
        assert len(inserts) == 1
        assert inserts[0][3] == "xray"

    def test_xray_rows_are_returned_as_xray_searches(self):
        pool = _FakePgPool(summary_rows=[("xray", 3), ("regex", 1)])
        backend = _pg_backend(pool)

        result = backend.get_metrics_bucketed(period_seconds=900)

        assert result["xray_searches"] == 3
        assert result["regex_searches"] == 1
        assert result["other_api_calls"] == 0


class TestApiMetricsServiceXray:
    def test_no_backend_default_summary_has_zero_xray_searches(self):
        from code_indexer.server.services.api_metrics_service import ApiMetricsService

        result = ApiMetricsService().get_metrics_bucketed(period_seconds=900)

        assert result == {
            "semantic_searches": 0,
            "other_index_searches": 0,
            "regex_searches": 0,
            "xray_searches": 0,
            "other_api_calls": 0,
        }

    def test_increment_xray_search_stores_xray_rows_in_every_tier(self, tmp_path):
        from code_indexer.server.services.api_metrics_service import ApiMetricsService
        from code_indexer.server.storage.sqlite_backends import ApiMetricsSqliteBackend

        db_file = str(tmp_path / "metrics.db")
        service = ApiMetricsService()
        service.initialize(db_file, storage_backend=ApiMetricsSqliteBackend(db_file))
        service.increment_xray_search(username="alice")
        service.stop_writer()

        with sqlite3.connect(db_file) as conn:
            rows = conn.execute(
                "SELECT granularity, metric_type FROM api_metrics_buckets "
                "WHERE username = ?",
                ("alice",),
            ).fetchall()
        assert {metric for _, metric in rows} == {"xray"}
        assert {tier for tier, _ in rows} == {"min1", "min5", "hour1", "day1"}
