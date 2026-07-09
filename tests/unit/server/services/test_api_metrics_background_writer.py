"""
Tests for ApiMetricsService background writer thread (Story #672).

Tests non-blocking hot path, queue-full behavior, and increment_* username
propagation.
"""

import queue
import sqlite3
import time

import pytest


# Polling constants — avoid magic numbers
POLL_TIMEOUT = 2.0  # seconds — standard wait for background writer
LONG_TIMEOUT = 3.0  # seconds — for accumulation tests
POLL_INTERVAL = 0.05  # seconds — check frequency


def _poll_until(
    condition_fn,
    timeout_secs: float = POLL_TIMEOUT,
    interval_secs: float = POLL_INTERVAL,
) -> bool:
    """Poll condition_fn up to timeout_secs. Returns True when satisfied, False on timeout."""
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        if condition_fn():
            return True
        time.sleep(interval_secs)
    return bool(condition_fn())  # Final check


def bucket_count(db_file: str, username: str, metric_type: str) -> int:
    """Return total row count in api_metrics_buckets for given username and metric_type."""
    with sqlite3.connect(db_file) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM api_metrics_buckets WHERE username=? AND metric_type=?",
                (username, metric_type),
            ).fetchone()[0]
        )


@pytest.fixture
def service_and_db(tmp_path):
    """Return (service, db_file) with a live backend and background writer."""
    from code_indexer.server.storage.sqlite_backends import ApiMetricsSqliteBackend
    from code_indexer.server.services.api_metrics_service import ApiMetricsService

    db_file = str(tmp_path / "test_metrics.db")
    backend = ApiMetricsSqliteBackend(db_file)
    service = ApiMetricsService()
    service.initialize(db_file, storage_backend=backend)
    return service, db_file


class TestBackgroundWriterThread:
    """Test that the background writer drains the queue and writes to DB."""

    def test_increment_semantic_search_writes_to_buckets(self, service_and_db):
        """increment_semantic_search must result in bucket rows in the database."""
        service, db_file = service_and_db

        service.increment_semantic_search(username="alice")

        assert _poll_until(lambda: bucket_count(db_file, "alice", "semantic") > 0), (
            "semantic bucket row must appear within timeout"
        )

    def test_increment_other_index_search_writes_to_buckets(self, service_and_db):
        """increment_other_index_search must write other_index bucket rows."""
        service, db_file = service_and_db

        service.increment_other_index_search(username="bob")

        assert _poll_until(lambda: bucket_count(db_file, "bob", "other_index") > 0), (
            "other_index bucket row must appear within timeout"
        )

    def test_increment_regex_search_writes_to_buckets(self, service_and_db):
        """increment_regex_search must write regex bucket rows."""
        service, db_file = service_and_db

        service.increment_regex_search(username="carol")

        assert _poll_until(lambda: bucket_count(db_file, "carol", "regex") > 0), (
            "regex bucket row must appear within timeout"
        )

    def test_increment_other_api_call_writes_to_buckets(self, service_and_db):
        """increment_other_api_call must write other_api bucket rows."""
        service, db_file = service_and_db

        service.increment_other_api_call(username="dave")

        assert _poll_until(lambda: bucket_count(db_file, "dave", "other_api") > 0), (
            "other_api bucket row must appear within timeout"
        )

    def test_multiple_increments_accumulate_count(self, service_and_db):
        """Three increments for same user must accumulate count=3 in the min1 bucket."""
        service, db_file = service_and_db

        service.increment_semantic_search(username="alice")
        service.increment_semantic_search(username="alice")
        service.increment_semantic_search(username="alice")

        def _count_is_three() -> bool:
            with sqlite3.connect(db_file) as conn:
                row = conn.execute(
                    "SELECT MAX(count) FROM api_metrics_buckets "
                    "WHERE username=? AND metric_type=? AND granularity=?",
                    ("alice", "semantic", "min1"),
                ).fetchone()
            return row is not None and row[0] == 3

        assert _poll_until(_count_is_three, timeout_secs=LONG_TIMEOUT), (
            "Three increments must accumulate count=3 in min1 bucket"
        )


class TestQueueFullBehavior:
    """Test that queue-full metrics are dropped without raising errors."""

    def test_queue_full_metric_dropped_and_no_row_created(self, tmp_path, monkeypatch):
        """When queue is full, metric must be silently dropped — no row created in DB."""
        from code_indexer.server.storage.sqlite_backends import ApiMetricsSqliteBackend
        from code_indexer.server.services.api_metrics_service import ApiMetricsService

        db_file = str(tmp_path / "test_metrics.db")
        backend = ApiMetricsSqliteBackend(db_file)
        service = ApiMetricsService()
        service.initialize(db_file, storage_backend=backend)

        # Monkeypatch put_nowait on the service's queue instance to raise queue.Full
        # deterministically — avoids race with the background writer draining the queue
        monkeypatch.setattr(
            service._queue,
            "put_nowait",
            lambda item: (_ for _ in ()).throw(queue.Full()),
        )  # type: ignore[attr-defined]

        # Capture pre-call row count for _anonymous / semantic
        count_before = bucket_count(db_file, "_anonymous", "semantic")

        # Must not raise despite full queue
        service._insert_metric("semantic", "_anonymous")  # type: ignore[attr-defined]

        # Wait briefly then confirm no new row was written (metric was dropped)
        time.sleep(POLL_INTERVAL * 3)
        count_after = bucket_count(db_file, "_anonymous", "semantic")

        assert count_after == count_before, (
            "Queue-full metric must be dropped — no DB row should be created"
        )

    def test_queue_maxsize_is_ten_thousand(self, tmp_path):
        """ApiMetricsService queue must have maxsize=10000."""
        from code_indexer.server.storage.sqlite_backends import ApiMetricsSqliteBackend
        from code_indexer.server.services.api_metrics_service import ApiMetricsService

        db_file = str(tmp_path / "test_metrics.db")
        backend = ApiMetricsSqliteBackend(db_file)
        service = ApiMetricsService()
        service.initialize(db_file, storage_backend=backend)

        assert service._queue.maxsize == 10_000  # type: ignore[attr-defined]


class TestUsernameDefaultAnonymous:
    """Test that increment_* methods default to '_anonymous' when no username given."""

    def test_increment_semantic_search_defaults_to_anonymous(self, service_and_db):
        """increment_semantic_search() without username must use '_anonymous'."""
        service, db_file = service_and_db

        service.increment_semantic_search()

        assert _poll_until(
            lambda: bucket_count(db_file, "_anonymous", "semantic") > 0
        ), "increment_semantic_search() must default to username='_anonymous'"

    def test_increment_other_index_search_defaults_to_anonymous(self, service_and_db):
        """increment_other_index_search() without username must use '_anonymous'."""
        service, db_file = service_and_db

        service.increment_other_index_search()

        assert _poll_until(
            lambda: bucket_count(db_file, "_anonymous", "other_index") > 0
        ), "increment_other_index_search() must default to username='_anonymous'"

    def test_increment_regex_search_defaults_to_anonymous(self, service_and_db):
        """increment_regex_search() without username must use '_anonymous'."""
        service, db_file = service_and_db

        service.increment_regex_search()

        assert _poll_until(lambda: bucket_count(db_file, "_anonymous", "regex") > 0), (
            "increment_regex_search() must default to username='_anonymous'"
        )

    def test_increment_other_api_call_defaults_to_anonymous(self, service_and_db):
        """increment_other_api_call() without username must use '_anonymous'."""
        service, db_file = service_and_db

        service.increment_other_api_call()

        assert _poll_until(
            lambda: bucket_count(db_file, "_anonymous", "other_api") > 0
        ), "increment_other_api_call() must default to username='_anonymous'"


class TestWriterLoopNodeId:
    """Test that the batched writer forwards node_id to upsert_buckets_batch.

    Story #1083: the writer now drains queued events and writes them via a single
    upsert_buckets_batch(events, node_id=...) call instead of per-event
    upsert_bucket — so node_id is forwarded once per batch.
    """

    def test_writer_loop_passes_node_id_to_batch_upsert(self, tmp_path):
        """When _node_id is set, the batched write must receive that node_id."""
        from unittest.mock import MagicMock
        from code_indexer.server.services.api_metrics_service import ApiMetricsService

        db_file = str(tmp_path / "test_metrics.db")

        # Build a mock backend that records every upsert_buckets_batch invocation
        mock_backend = MagicMock()
        mock_backend.upsert_buckets_batch = MagicMock()

        service = ApiMetricsService()
        service.initialize(
            db_file, storage_backend=mock_backend, node_id="test-node-42"
        )
        try:
            service.increment_semantic_search(username="alice")

            # Wait for the background writer to drain the queue
            assert _poll_until(lambda: mock_backend.upsert_buckets_batch.called), (
                "upsert_buckets_batch must be called within timeout"
            )

            # Every batched write must carry node_id="test-node-42"
            for actual_call in mock_backend.upsert_buckets_batch.call_args_list:
                _, kwargs = actual_call
                assert kwargs.get("node_id") == "test-node-42", (
                    f"Expected node_id='test-node-42' but got {kwargs!r}"
                )
        finally:
            service.stop_writer()
