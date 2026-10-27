import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Dict, List, Tuple, cast

import pytest

from code_indexer.server.services.api_metrics_service import ApiMetricsService
from code_indexer.server.services.dashboard_service import DashboardService

if TYPE_CHECKING:
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

_PERIOD = 900
_TIER = "min1"
_OLD_TYPES = ("semantic", "other_index", "regex", "other_api")
_SUMMARY_SQL = (
    "SELECT metric_type, SUM(count) AS total FROM api_metrics_buckets "
    "WHERE granularity = {p} GROUP BY metric_type"
)
# ^ coupled with src/code_indexer/server/storage/postgres/api_metrics_backend.py:upsert_buckets_batch; INSERT parameter order
# ^ coupled with src/code_indexer/server/storage/postgres/api_metrics_backend.py:upsert_bucket; INSERT parameter order
_USER, _GRANULARITY, _BUCKET, _TYPE = 0, 1, 2, 3
_KEY_COLUMNS = 5


# ^ invariant _old_summary, _old_chart_totals, _old_per_user_row = pre-2213 readers; they keep four categories, never xray
def _old_summary(rows: List[Tuple[str, int]]) -> Dict[str, int]:
    counts = {row[0]: int(row[1]) for row in rows}
    return {
        "semantic_searches": counts.get("semantic", 0),
        "other_index_searches": counts.get("other_index", 0),
        "regex_searches": counts.get("regex", 0),
        "other_api_calls": counts.get("other_api", 0),
    }


def _old_chart_totals(timeseries: List[Tuple[str, str, int]]) -> Dict[str, int]:
    totals = {mt: 0 for mt in _OLD_TYPES}
    for _bucket, metric_type, count in timeseries:
        if metric_type in totals:
            totals[metric_type] += count
    return totals


def _old_per_user_row(counts: Dict[str, int]) -> Dict[str, int]:
    row = {mt: counts.get(mt, 0) for mt in _OLD_TYPES}
    row["total"] = sum(row.values())
    return row


class _StatefulPgConn:
    def __init__(self, store: Dict[Tuple[Any, ...], int]) -> None:
        self._store = store
        self._rows: List[Tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> "_StatefulPgConn":
        self._rows = []
        if "INSERT INTO api_metrics_buckets" in sql:
            key = tuple(params[:_KEY_COLUMNS])
            increment = params[_KEY_COLUMNS] if len(params) > _KEY_COLUMNS else 1
            self._store[key] = self._store.get(key, 0) + increment
        elif "FROM api_metrics_buckets" in sql and "GROUP BY" in sql:
            self._rows = self._group(sql, params[0])
        return self

    def _group(self, sql: str, tier: str) -> List[Tuple[Any, ...]]:
        if "GROUP BY username, metric_type" in sql:
            columns: Tuple[int, ...] = (_USER, _TYPE)
        elif "GROUP BY bucket_start, metric_type" in sql:
            columns = (_BUCKET, _TYPE)
        else:
            columns = (_TYPE,)
        grouped: Dict[Tuple[Any, ...], int] = defaultdict(int)
        for key, count in self._store.items():
            if key[_GRANULARITY] == tier:
                grouped[tuple(key[i] for i in columns)] += count
        return [(*group, total) for group, total in sorted(grouped.items())]

    def commit(self) -> None:
        pass

    def fetchone(self):
        return None

    def fetchall(self):
        return self._rows


class _StatefulPgPool:
    def __init__(self) -> None:
        self.store: Dict[Tuple[Any, ...], int] = {}

    @contextmanager
    def connection(self):
        yield _StatefulPgConn(self.store)


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "sqlite":
        from code_indexer.server.storage.sqlite_backends import (
            ApiMetricsSqliteBackend,
        )

        db_file = str(tmp_path / "metrics.db")

        def sqlite_rows() -> List[Tuple[str, int]]:
            with sqlite3.connect(db_file) as conn:
                return conn.execute(_SUMMARY_SQL.format(p="?"), (_TIER,)).fetchall()

        yield ApiMetricsSqliteBackend(db_file), sqlite_rows
        return

    from code_indexer.server.storage.postgres.api_metrics_backend import (
        ApiMetricsPostgresBackend,
    )

    pool = _StatefulPgPool()

    def pg_rows() -> List[Tuple[str, int]]:
        with pool.connection() as conn:
            return cast(
                List[Tuple[str, int]],
                conn.execute(_SUMMARY_SQL.format(p="%s"), [_TIER]).fetchall(),
            )

    # ^ why cast, not a ConnectionPool subclass; the backend only calls pool.connection()
    yield ApiMetricsPostgresBackend(cast("ConnectionPool", pool)), pg_rows


def _write(backend: Any, tmp_path: Any, calls: List[Tuple[str, str]]) -> None:
    service = ApiMetricsService()
    service.initialize(str(tmp_path / "writer.db"), storage_backend=backend)
    for method, username in calls:
        getattr(service, method)(username=username)
    service.stop_writer()


_NEW_WRITER_CALLS = [
    ("increment_xray_search", "alice"),
    ("increment_xray_search", "alice"),
    ("increment_xray_search", "bob"),
    ("increment_semantic_search", "alice"),
    ("increment_regex_search", "bob"),
    ("increment_other_api_call", "alice"),
]
_OLD_WRITER_CALLS = [
    ("increment_semantic_search", "alice"),
    ("increment_other_index_search", "alice"),
    ("increment_regex_search", "bob"),
    ("increment_other_api_call", "bob"),
]


class TestOldReaderNewWriter:
    def test_old_summary_ignores_xray_rows(self, store, tmp_path):
        backend, raw_rows = store
        _write(backend, tmp_path, _NEW_WRITER_CALLS)

        assert _old_summary(raw_rows()) == {
            "semantic_searches": 1,
            "other_index_searches": 0,
            "regex_searches": 1,
            "other_api_calls": 1,
        }
        assert backend.get_metrics_bucketed(_PERIOD)["xray_searches"] == 3

    def test_old_chart_and_per_user_ignore_xray_rows(self, store, tmp_path):
        backend, _raw = store
        _write(backend, tmp_path, _NEW_WRITER_CALLS)

        totals = _old_chart_totals(backend.get_metrics_timeseries(_PERIOD))
        by_user = backend.get_metrics_by_user(_PERIOD)

        assert totals == {"semantic": 1, "other_index": 0, "regex": 1, "other_api": 1}
        assert by_user["alice"]["xray"] == 2
        assert _old_per_user_row(by_user["alice"]) == {
            "semantic": 1,
            "other_index": 0,
            "regex": 0,
            "other_api": 1,
            "total": 2,
        }
        assert _old_per_user_row(by_user["bob"])["total"] == 1


class TestNewReaderOldWriter:
    def test_new_readers_report_zero_xray(self, store, tmp_path):
        backend, _raw = store
        _write(backend, tmp_path, _OLD_WRITER_CALLS)
        dashboard = DashboardService()

        summary = backend.get_metrics_bucketed(_PERIOD)
        chart = dashboard.get_chart_data(_PERIOD, api_metrics_backend=backend)
        rows = dashboard.get_per_user_stats(_PERIOD, api_metrics_backend=backend)

        assert summary["xray_searches"] == 0
        assert summary["regex_searches"] == 1
        series = {ds["label"]: ds["data"] for ds in chart["datasets"]}
        assert len(chart["labels"]) >= 1
        assert series["Xray"] == [0] * len(chart["labels"])
        assert sum(series["Regex"]) == 1
        assert {row["username"]: row["xray"] for row in rows} == {"alice": 0, "bob": 0}
        assert {row["username"]: row["total"] for row in rows} == {"alice": 2, "bob": 2}
