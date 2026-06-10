"""Story #1083: the ApiMetricsService background writer batches drained events.

Before: the writer popped ONE event and did 4 upsert_bucket calls (one BEGIN
EXCLUSIVE transaction each). After: the writer drains ALL currently-queued events
and writes them via a SINGLE upsert_buckets_batch call (one transaction), and
drains the queue on shutdown so no counts are lost.

Real SQLite backend (no mocks of code under test) for count-preservation; a
recording fake backend for the batching-shape assertions.
"""

import sqlite3
import threading
import time

from code_indexer.server.services.api_metrics_service import ApiMetricsService
from code_indexer.server.storage.sqlite_backends import ApiMetricsSqliteBackend


POLL_TIMEOUT = 3.0
POLL_INTERVAL = 0.02


def _poll_until(cond, timeout=POLL_TIMEOUT, interval=POLL_INTERVAL) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return bool(cond())


def _min1_count(db_file: str, username: str, metric_type: str) -> int:
    with sqlite3.connect(db_file) as conn:
        row = conn.execute(
            "SELECT count FROM api_metrics_buckets "
            "WHERE username=? AND metric_type=? AND granularity='min1'",
            (username, metric_type),
        ).fetchone()
    return int(row[0]) if row else 0


class _RecordingBackend:
    """Records upsert_buckets_batch invocations."""

    def __init__(self) -> None:
        self.batch_calls: list = []
        self.lock = threading.Lock()

    def upsert_buckets_batch(self, events, node_id=""):
        with self.lock:
            self.batch_calls.append((list(events), node_id))

    def cleanup_expired_buckets(self):
        pass


def test_writer_uses_batch_method():
    """The writer must call upsert_buckets_batch (not per-event upsert_bucket)."""
    backend = _RecordingBackend()
    service = ApiMetricsService()
    service.initialize("unused.db", storage_backend=backend, node_id="node-7")
    try:
        service.increment_semantic_search(username="alice")
        assert _poll_until(lambda: len(backend.batch_calls) >= 1), (
            "writer must invoke upsert_buckets_batch"
        )
        # node_id forwarded on the batch call.
        assert backend.batch_calls[0][1] == "node-7"
        # The single event must carry the 4-tier bucket map.
        events, _node = backend.batch_calls[0]
        assert len(events) >= 1
        assert set(events[0]["buckets"].keys()) == {"min1", "min5", "hour1", "day1"}
        assert events[0]["username"] == "alice"
        assert events[0]["metric_type"] == "semantic"
    finally:
        service.stop_writer()


def test_burst_preserves_total_count(tmp_path):
    """A burst of N increments must yield count==N in the min1 bucket (no loss)."""
    db_file = str(tmp_path / "m.db")
    backend = ApiMetricsSqliteBackend(db_file)
    service = ApiMetricsService()
    service.initialize(db_file, storage_backend=backend)
    try:
        for _ in range(25):
            service.increment_semantic_search(username="alice")
        assert _poll_until(lambda: _min1_count(db_file, "alice", "semantic") == 25), (
            f"expected count==25, got {_min1_count(db_file, 'alice', 'semantic')}"
        )
    finally:
        service.stop_writer()


def test_stop_writer_drains_remaining_queue(tmp_path):
    """stop_writer() must flush any still-queued events before returning."""
    db_file = str(tmp_path / "m.db")
    backend = ApiMetricsSqliteBackend(db_file)
    service = ApiMetricsService()
    service.initialize(db_file, storage_backend=backend)

    # Enqueue several events then stop immediately — the final drain must persist
    # every one of them (no counts lost on shutdown).
    for _ in range(10):
        service.increment_regex_search(username="bob")
    service.stop_writer()

    assert _min1_count(db_file, "bob", "regex") == 10, (
        "stop_writer() must drain remaining queued events before exiting"
    )
