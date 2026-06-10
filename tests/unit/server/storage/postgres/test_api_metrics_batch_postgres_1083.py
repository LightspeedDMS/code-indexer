"""Story #1083: ApiMetricsPostgresBackend.upsert_buckets_batch — one connection/commit.

Mirrors the SQLite batching: the background writer drains the whole queue and
calls one batch upsert instead of one connection+commit per metric event. The
Postgres backend must issue all coalesced upserts on a SINGLE pooled connection
and commit ONCE.

Uses a fake ConnectionPool/connection (no live DB), matching the structural
unit-test convention for the postgres backends.
"""

from contextlib import contextmanager
from typing import Any, List, Tuple

import pytest

from code_indexer.server.storage.postgres.api_metrics_backend import (
    ApiMetricsPostgresBackend,
)


class _FakeConn:
    def __init__(self, recorder: "List[Tuple[str, Any]]", commits: List[int]) -> None:
        self._recorder = recorder
        self._commits = commits

    def execute(self, sql: str, params: Any = None) -> "_FakeConn":
        self._recorder.append((sql, params))
        return self

    def commit(self) -> None:
        self._commits.append(1)

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _FakePool:
    """Records every .connection() acquisition + the executes/commits per conn."""

    def __init__(self) -> None:
        self.acquisitions = 0
        self.executes: List[Tuple[str, Any]] = []
        self.commits: List[int] = []

    @contextmanager
    def connection(self):
        self.acquisitions += 1
        yield _FakeConn(self.executes, self.commits)


def _make_backend() -> Tuple[ApiMetricsPostgresBackend, _FakePool]:
    pool = _FakePool()
    backend = ApiMetricsPostgresBackend(pool)  # __init__ runs schema DDL on the pool
    # Reset counters so the test only observes the batch call.
    pool.acquisitions = 0
    pool.executes.clear()
    pool.commits.clear()
    return backend, pool


def _event(username: str, metric_type: str, bucket_start: str):
    return {
        "username": username,
        "metric_type": metric_type,
        "buckets": {
            "min1": bucket_start,
            "min5": bucket_start,
            "hour1": bucket_start,
            "day1": bucket_start,
        },
    }


def test_batch_method_exists() -> None:
    assert hasattr(ApiMetricsPostgresBackend, "upsert_buckets_batch")


def test_batch_single_connection_single_commit() -> None:
    backend, pool = _make_backend()
    bucket = "2026-06-09T10:00:00+00:00"
    events = [
        _event("alice", "semantic", bucket),
        _event("bob", "regex", bucket),
        _event("alice", "semantic", bucket),
    ]

    backend.upsert_buckets_batch(events, node_id="node-x")

    assert pool.acquisitions == 1, (
        f"Batch must use ONE pooled connection, used {pool.acquisitions}"
    )
    assert len(pool.commits) == 1, (
        f"Batch must commit exactly once, committed {len(pool.commits)} time(s)"
    )
    # alice/semantic (4 tiers, coalesced from 2 events) + bob/regex (4 tiers)
    # = 8 distinct coalesced upserts.
    assert len(pool.executes) == 8


def test_batch_empty_is_noop() -> None:
    backend, pool = _make_backend()
    backend.upsert_buckets_batch([], node_id="")
    assert pool.acquisitions == 0
    assert pool.executes == []
    assert pool.commits == []


def test_batch_validates_metric_type() -> None:
    backend, _pool = _make_backend()
    bad = {
        "username": "alice",
        "metric_type": "not_a_metric",
        "buckets": {"min1": "2026-06-09T10:00:00+00:00"},
    }
    with pytest.raises(ValueError):
        backend.upsert_buckets_batch([bad], node_id="")
