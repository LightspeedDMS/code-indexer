"""Story #1083: ApiMetricsSqliteBackend.upsert_buckets_batch writes one transaction.

The background metrics writer used to do one BEGIN EXCLUSIVE transaction PER
metric event (4 upserts/event, one transaction each). Under sustained query load
this stole the single worker's GIL with ~23 DB-wide-exclusive transactions/sec.

The batch method drains a list of (username, metric_type, bucket_map) work items
and writes ALL of them in ONE transaction, coalescing repeated keys into a single
``count + N`` increment so totals are preserved.

Unit-level; real SQLite (no mocks of code under test).
"""

import sqlite3

import pytest

from code_indexer.server.storage.sqlite_backends import ApiMetricsSqliteBackend


def _bucket_rows(db_file: str):
    with sqlite3.connect(db_file) as conn:
        return conn.execute(
            "SELECT username, granularity, bucket_start, metric_type, node_id, count "
            "FROM api_metrics_buckets ORDER BY username, granularity, metric_type"
        ).fetchall()


def _make_event(username: str, metric_type: str, bucket_start: str):
    """One drained metric event: same bucket_start used for all 4 tiers (test-simplified)."""
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


def test_batch_method_exists(tmp_path):
    backend = ApiMetricsSqliteBackend(str(tmp_path / "m.db"))
    assert hasattr(backend, "upsert_buckets_batch")


def test_batch_writes_all_tiers(tmp_path):
    db_file = str(tmp_path / "m.db")
    backend = ApiMetricsSqliteBackend(db_file)

    backend.upsert_buckets_batch(
        [_make_event("alice", "semantic", "2026-06-09T10:00:00+00:00")],
        node_id="node-a",
    )

    rows = _bucket_rows(db_file)
    grans = {r[1] for r in rows}
    assert grans == {"min1", "min5", "hour1", "day1"}
    assert all(
        r[0] == "alice" and r[3] == "semantic" and r[4] == "node-a" for r in rows
    )
    assert all(r[5] == 1 for r in rows)


def test_batch_coalesces_repeated_keys(tmp_path):
    db_file = str(tmp_path / "m.db")
    backend = ApiMetricsSqliteBackend(db_file)

    bucket = "2026-06-09T10:00:00+00:00"
    events = [_make_event("alice", "semantic", bucket) for _ in range(3)]
    backend.upsert_buckets_batch(events, node_id="")

    # min1 row for alice/semantic must carry count == 3 (coalesced, not 3 rows).
    with sqlite3.connect(db_file) as conn:
        row = conn.execute(
            "SELECT count FROM api_metrics_buckets "
            "WHERE username=? AND granularity=? AND metric_type=?",
            ("alice", "min1", "semantic"),
        ).fetchone()
    assert row is not None and row[0] == 3


def test_batch_single_transaction(tmp_path, monkeypatch):
    """All upserts in a batch must run inside exactly ONE execute_atomic call."""
    db_file = str(tmp_path / "m.db")
    backend = ApiMetricsSqliteBackend(db_file)

    calls = {"n": 0}
    original = backend._conn_manager.execute_atomic

    def counting(operation):
        calls["n"] += 1
        return original(operation)

    monkeypatch.setattr(backend._conn_manager, "execute_atomic", counting)

    bucket = "2026-06-09T10:00:00+00:00"
    events = [
        _make_event("alice", "semantic", bucket),
        _make_event("bob", "regex", bucket),
        _make_event("alice", "semantic", bucket),
    ]
    backend.upsert_buckets_batch(events, node_id="")

    assert calls["n"] == 1, f"Batch must use exactly ONE transaction, used {calls['n']}"


def test_batch_empty_is_noop(tmp_path):
    db_file = str(tmp_path / "m.db")
    backend = ApiMetricsSqliteBackend(db_file)
    backend.upsert_buckets_batch([], node_id="")
    assert _bucket_rows(db_file) == []


def test_batch_validates_metric_type(tmp_path):
    backend = ApiMetricsSqliteBackend(str(tmp_path / "m.db"))
    bad = {
        "username": "alice",
        "metric_type": "not_a_metric",
        "buckets": {"min1": "2026-06-09T10:00:00+00:00"},
    }
    with pytest.raises(ValueError):
        backend.upsert_buckets_batch([bad], node_id="")
