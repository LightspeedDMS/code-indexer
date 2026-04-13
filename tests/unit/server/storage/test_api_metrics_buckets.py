"""
Tests for api_metrics_buckets table and UPSERT aggregation (Story #672).

Tests bucket creation, UPSERT counting, cleanup, and all 4 granularity tiers
for the ApiMetricsSqliteBackend.
"""

import sqlite3
import time
from datetime import datetime, timezone, timedelta

import pytest


def _poll_until(
    condition_fn, timeout_secs: float = 2.0, interval_secs: float = 0.05
) -> bool:
    """Poll condition_fn up to timeout_secs. Returns True when condition satisfied, False on timeout."""
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        if condition_fn():
            return True
        time.sleep(interval_secs)
    return condition_fn()  # One final check


@pytest.fixture
def backend_and_db(tmp_path):
    """Return (backend, db_file_path) for use in tests."""
    from code_indexer.server.storage.sqlite_backends import ApiMetricsSqliteBackend

    db_file = str(tmp_path / "test_metrics.db")
    backend = ApiMetricsSqliteBackend(db_file)
    return backend, db_file


class TestApiMetricsBucketsSchemaCreation:
    """Test that api_metrics_buckets table is created on backend init."""

    def test_buckets_table_created_on_init(self, backend_and_db):
        """api_metrics_buckets table must exist after backend initialization."""
        _backend, db_file = backend_and_db

        with sqlite3.connect(db_file) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='api_metrics_buckets'"
            )
            row = cursor.fetchone()

        assert row is not None, "api_metrics_buckets table must exist after init"

    def test_old_api_metrics_table_still_exists(self, backend_and_db):
        """Old api_metrics table must NOT be dropped — required for rolling restarts."""
        _backend, db_file = backend_and_db

        with sqlite3.connect(db_file) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='api_metrics'"
            )
            row = cursor.fetchone()

        assert row is not None, (
            "Old api_metrics table must still exist (backward compat)"
        )

    def test_buckets_table_has_correct_columns(self, backend_and_db):
        """api_metrics_buckets must have username, granularity, bucket_start, metric_type, count."""
        _backend, db_file = backend_and_db

        with sqlite3.connect(db_file) as conn:
            cursor = conn.execute("PRAGMA table_info(api_metrics_buckets)")
            columns = {row[1] for row in cursor.fetchall()}

        assert "username" in columns
        assert "granularity" in columns
        assert "bucket_start" in columns
        assert "metric_type" in columns
        assert "count" in columns

    def test_buckets_table_primary_key_is_composite(self, backend_and_db):
        """Primary key must be (username, granularity, bucket_start, metric_type)."""
        _backend, db_file = backend_and_db

        with sqlite3.connect(db_file) as conn:
            cursor = conn.execute("PRAGMA table_info(api_metrics_buckets)")
            rows = cursor.fetchall()

        # pk column index is row[5] — nonzero means part of primary key
        pk_columns = {row[1] for row in rows if row[5] > 0}
        assert "username" in pk_columns
        assert "granularity" in pk_columns
        assert "bucket_start" in pk_columns
        assert "metric_type" in pk_columns


class TestApiMetricsBucketsUpsert:
    """Test UPSERT aggregation behavior."""

    def test_upsert_creates_row_on_first_insert(self, backend_and_db):
        """First upsert_bucket call must create a row with count=1."""
        backend, db_file = backend_and_db

        backend.upsert_bucket("alice", "min1", "2026-04-11T10:00:00", "semantic")

        with sqlite3.connect(db_file) as conn:
            cursor = conn.execute(
                "SELECT count FROM api_metrics_buckets "
                "WHERE username=? AND granularity=? AND bucket_start=? AND metric_type=?",
                ("alice", "min1", "2026-04-11T10:00:00", "semantic"),
            )
            row = cursor.fetchone()

        assert row is not None
        assert row[0] == 1

    def test_upsert_increments_count_on_second_insert(self, backend_and_db):
        """Second upsert_bucket for same key must increment count to 2, not create a new row."""
        backend, db_file = backend_and_db

        backend.upsert_bucket("alice", "min1", "2026-04-11T10:00:00", "semantic")
        backend.upsert_bucket("alice", "min1", "2026-04-11T10:00:00", "semantic")

        with sqlite3.connect(db_file) as conn:
            row = conn.execute(
                "SELECT count FROM api_metrics_buckets "
                "WHERE username=? AND granularity=? AND bucket_start=? AND metric_type=?",
                ("alice", "min1", "2026-04-11T10:00:00", "semantic"),
            ).fetchone()
            total_rows = conn.execute(
                "SELECT COUNT(*) FROM api_metrics_buckets WHERE username=? AND metric_type=?",
                ("alice", "semantic"),
            ).fetchone()[0]

        assert row is not None
        assert row[0] == 2, "count must be 2 after two identical upserts"
        assert total_rows == 1, "must remain exactly 1 row (no duplicate)"

    def test_upsert_ten_times_gives_count_ten(self, backend_and_db):
        """Ten upsert_bucket calls for same key must give count=10."""
        backend, db_file = backend_and_db

        for _ in range(10):
            backend.upsert_bucket("bob", "hour1", "2026-04-11T10:00:00", "other_api")

        with sqlite3.connect(db_file) as conn:
            row = conn.execute(
                "SELECT count FROM api_metrics_buckets "
                "WHERE username=? AND granularity=? AND bucket_start=? AND metric_type=?",
                ("bob", "hour1", "2026-04-11T10:00:00", "other_api"),
            ).fetchone()

        assert row[0] == 10

    def test_upsert_different_users_are_independent(self, backend_and_db):
        """Upserts for different usernames must create separate rows."""
        backend, db_file = backend_and_db

        backend.upsert_bucket("alice", "min1", "2026-04-11T10:00:00", "semantic")
        backend.upsert_bucket("bob", "min1", "2026-04-11T10:00:00", "semantic")

        with sqlite3.connect(db_file) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM api_metrics_buckets "
                "WHERE granularity=? AND bucket_start=? AND metric_type=?",
                ("min1", "2026-04-11T10:00:00", "semantic"),
            ).fetchone()[0]

        assert total == 2

    def test_upsert_different_metric_types_are_independent(self, backend_and_db):
        """Upserts for different metric_types must create separate rows."""
        backend, db_file = backend_and_db

        backend.upsert_bucket("alice", "min1", "2026-04-11T10:00:00", "semantic")
        backend.upsert_bucket("alice", "min1", "2026-04-11T10:00:00", "regex")

        with sqlite3.connect(db_file) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM api_metrics_buckets "
                "WHERE username=? AND granularity=? AND bucket_start=?",
                ("alice", "min1", "2026-04-11T10:00:00"),
            ).fetchone()[0]

        assert total == 2


class TestFourGranularityTiersWrittenTogether:
    """Test that all 4 granularity tiers are written when recording a metric."""

    def test_four_tiers_written_for_single_metric_via_service(self, tmp_path):
        """Recording one metric via the service must write rows for all 4 tiers."""
        from code_indexer.server.storage.sqlite_backends import ApiMetricsSqliteBackend
        from code_indexer.server.services.api_metrics_service import ApiMetricsService

        db_file = str(tmp_path / "test_metrics.db")
        backend = ApiMetricsSqliteBackend(db_file)

        service = ApiMetricsService()
        service.initialize(db_file, storage_backend=backend)
        service.increment_semantic_search(username="alice")

        # Poll with bounded retry until all 4 tiers appear (max 2 seconds)
        def _all_four_tiers_present() -> bool:
            with sqlite3.connect(db_file) as conn:
                rows = conn.execute(
                    "SELECT granularity FROM api_metrics_buckets "
                    "WHERE username=? AND metric_type=?",
                    ("alice", "semantic"),
                ).fetchall()
            return len({row[0] for row in rows}) == 4

        assert _poll_until(_all_four_tiers_present), (
            "All 4 granularity tiers (min1, min5, hour1, day1) must be written within 2 seconds"
        )

        with sqlite3.connect(db_file) as conn:
            rows = conn.execute(
                "SELECT granularity FROM api_metrics_buckets "
                "WHERE username=? AND metric_type=?",
                ("alice", "semantic"),
            ).fetchall()

        granularities = {row[0] for row in rows}
        assert "min1" in granularities, "min1 tier must be written"
        assert "min5" in granularities, "min5 tier must be written"
        assert "hour1" in granularities, "hour1 tier must be written"
        assert "day1" in granularities, "day1 tier must be written"

    def test_anonymous_default_when_no_username_provided(self, tmp_path):
        """Calling increment_semantic_search() without username must store '_anonymous'."""
        from code_indexer.server.storage.sqlite_backends import ApiMetricsSqliteBackend
        from code_indexer.server.services.api_metrics_service import ApiMetricsService

        db_file = str(tmp_path / "test_metrics.db")
        backend = ApiMetricsSqliteBackend(db_file)

        service = ApiMetricsService()
        service.initialize(db_file, storage_backend=backend)

        # Call WITHOUT username — must default to "_anonymous"
        service.increment_semantic_search()

        # Poll with bounded retry until a row for _anonymous appears
        def _anonymous_row_present() -> bool:
            with sqlite3.connect(db_file) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM api_metrics_buckets "
                    "WHERE username=? AND metric_type=?",
                    ("_anonymous", "semantic"),
                ).fetchone()
            return row[0] > 0

        assert _poll_until(_anonymous_row_present), (
            "A row with username='_anonymous' must be written when no username is given"
        )


class TestBucketTruncationFormulas:
    """Test the bucket truncation helper functions."""

    def test_truncate_min1_zeroes_seconds_and_microseconds(self):
        """_truncate_min1 must zero out seconds and microseconds."""
        from code_indexer.server.services.api_metrics_service import _truncate_min1

        dt = datetime(2026, 4, 11, 10, 7, 43, 123456, tzinfo=timezone.utc)
        result = _truncate_min1(dt)
        assert result == "2026-04-11T10:07:00+00:00"

    def test_truncate_min5_rounds_down_to_5_min_boundary(self):
        """_truncate_min5 must round minute down to nearest multiple of 5."""
        from code_indexer.server.services.api_metrics_service import _truncate_min5

        dt = datetime(2026, 4, 11, 10, 7, 43, 0, tzinfo=timezone.utc)
        result = _truncate_min5(dt)
        assert result == "2026-04-11T10:05:00+00:00"

    def test_truncate_min5_minute_0_stays_0(self):
        """_truncate_min5 for minute=0 must give :00."""
        from code_indexer.server.services.api_metrics_service import _truncate_min5

        dt = datetime(2026, 4, 11, 10, 0, 59, 0, tzinfo=timezone.utc)
        result = _truncate_min5(dt)
        assert result == "2026-04-11T10:00:00+00:00"

    def test_truncate_min5_minute_5_stays_5(self):
        """_truncate_min5 for minute=5 must give :05."""
        from code_indexer.server.services.api_metrics_service import _truncate_min5

        dt = datetime(2026, 4, 11, 10, 5, 0, 0, tzinfo=timezone.utc)
        result = _truncate_min5(dt)
        assert result == "2026-04-11T10:05:00+00:00"

    def test_truncate_min5_minute_9_gives_5(self):
        """_truncate_min5 for minute=9 must give :05."""
        from code_indexer.server.services.api_metrics_service import _truncate_min5

        dt = datetime(2026, 4, 11, 10, 9, 59, 0, tzinfo=timezone.utc)
        result = _truncate_min5(dt)
        assert result == "2026-04-11T10:05:00+00:00"

    def test_truncate_min5_minute_10_gives_10(self):
        """_truncate_min5 for minute=10 must give :10."""
        from code_indexer.server.services.api_metrics_service import _truncate_min5

        dt = datetime(2026, 4, 11, 10, 10, 0, 0, tzinfo=timezone.utc)
        result = _truncate_min5(dt)
        assert result == "2026-04-11T10:10:00+00:00"

    def test_truncate_min5_minute_55_gives_55(self):
        """_truncate_min5 for minute=55 must give :55."""
        from code_indexer.server.services.api_metrics_service import _truncate_min5

        dt = datetime(2026, 4, 11, 10, 55, 30, 0, tzinfo=timezone.utc)
        result = _truncate_min5(dt)
        assert result == "2026-04-11T10:55:00+00:00"

    def test_truncate_hour1_zeroes_minutes_seconds_microseconds(self):
        """_truncate_hour1 must zero out minutes, seconds, microseconds."""
        from code_indexer.server.services.api_metrics_service import _truncate_hour1

        dt = datetime(2026, 4, 11, 10, 37, 22, 999999, tzinfo=timezone.utc)
        result = _truncate_hour1(dt)
        assert result == "2026-04-11T10:00:00+00:00"

    def test_truncate_day1_zeroes_time_component(self):
        """_truncate_day1 must zero out hour, minute, second, microsecond."""
        from code_indexer.server.services.api_metrics_service import _truncate_day1

        dt = datetime(2026, 4, 11, 23, 59, 59, 999999, tzinfo=timezone.utc)
        result = _truncate_day1(dt)
        assert result == "2026-04-11T00:00:00+00:00"


class TestUpsertBucketValidation:
    """Test that upsert_bucket validates granularity and metric_type inputs."""

    def test_invalid_granularity_raises_value_error(self, backend_and_db):
        """upsert_bucket with unknown granularity must raise ValueError."""
        backend, _db_file = backend_and_db

        with pytest.raises(ValueError, match="granularity"):
            backend.upsert_bucket("alice", "sec30", "2026-04-11T10:00:00", "semantic")

    def test_invalid_metric_type_raises_value_error(self, backend_and_db):
        """upsert_bucket with unknown metric_type must raise ValueError."""
        backend, _db_file = backend_and_db

        with pytest.raises(ValueError, match="metric_type"):
            backend.upsert_bucket(
                "alice", "min1", "2026-04-11T10:00:00", "unknown_type"
            )

    def test_empty_username_raises_value_error(self, backend_and_db):
        """upsert_bucket with empty string username must raise ValueError."""
        backend, _db_file = backend_and_db

        with pytest.raises(ValueError, match="username"):
            backend.upsert_bucket("", "min1", "2026-04-11T10:00:00", "semantic")

    def test_whitespace_only_username_raises_value_error(self, backend_and_db):
        """upsert_bucket with whitespace-only username must raise ValueError."""
        backend, _db_file = backend_and_db

        with pytest.raises(ValueError, match="username"):
            backend.upsert_bucket("   ", "min1", "2026-04-11T10:00:00", "semantic")

    def test_invalid_bucket_start_raises_value_error(self, backend_and_db):
        """upsert_bucket with non-ISO 8601 bucket_start must raise ValueError."""
        backend, _db_file = backend_and_db

        with pytest.raises(ValueError, match="bucket_start"):
            backend.upsert_bucket("alice", "min1", "not-a-date", "semantic")

    def test_valid_granularities_do_not_raise(self, backend_and_db):
        """All four valid granularities must be accepted without error."""
        backend, _db_file = backend_and_db

        for gran in ("min1", "min5", "hour1", "day1"):
            backend.upsert_bucket("alice", gran, "2026-04-11T10:00:00", "semantic")

    def test_valid_metric_types_do_not_raise(self, backend_and_db):
        """All four valid metric types must be accepted without error."""
        backend, _db_file = backend_and_db

        for metric in ("semantic", "other_index", "regex", "other_api"):
            backend.upsert_bucket("alice", "min1", "2026-04-11T10:00:00", metric)


class TestApiMetricsBucketsCleanup:
    """Test cleanup_expired_buckets retention logic."""

    def test_cleanup_removes_expired_min1_rows(self, backend_and_db):
        """Rows older than 15 minutes in min1 tier must be deleted."""
        backend, db_file = backend_and_db

        # Insert an expired min1 bucket (20 minutes ago)
        expired_bucket = (
            (datetime.now(timezone.utc) - timedelta(minutes=20))
            .replace(second=0, microsecond=0)
            .isoformat()
        )
        backend.upsert_bucket("alice", "min1", expired_bucket, "semantic")

        backend.cleanup_expired_buckets()

        with sqlite3.connect(db_file) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM api_metrics_buckets WHERE granularity='min1'"
            ).fetchone()[0]

        assert count == 0, "Expired min1 row must be deleted"

    def test_cleanup_keeps_current_min1_rows(self, backend_and_db):
        """Rows within 15 minutes in min1 tier must NOT be deleted."""
        backend, db_file = backend_and_db

        # Insert a current min1 bucket (5 minutes ago)
        current_bucket = (
            (datetime.now(timezone.utc) - timedelta(minutes=5))
            .replace(second=0, microsecond=0)
            .isoformat()
        )
        backend.upsert_bucket("alice", "min1", current_bucket, "semantic")

        backend.cleanup_expired_buckets()

        with sqlite3.connect(db_file) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM api_metrics_buckets WHERE granularity='min1'"
            ).fetchone()[0]

        assert count == 1, "Current min1 row must be preserved"

    def test_cleanup_removes_expired_min5_rows(self, backend_and_db):
        """Rows older than 1 hour in min5 tier must be deleted."""
        backend, db_file = backend_and_db

        # Insert an expired min5 bucket (2 hours ago)
        expired_bucket = (
            (datetime.now(timezone.utc) - timedelta(hours=2))
            .replace(minute=0, second=0, microsecond=0)
            .isoformat()
        )
        backend.upsert_bucket("alice", "min5", expired_bucket, "semantic")

        backend.cleanup_expired_buckets()

        with sqlite3.connect(db_file) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM api_metrics_buckets WHERE granularity='min5'"
            ).fetchone()[0]

        assert count == 0, "Expired min5 row must be deleted"

    def test_cleanup_removes_expired_hour1_rows(self, backend_and_db):
        """Rows older than 24 hours in hour1 tier must be deleted."""
        backend, db_file = backend_and_db

        # Insert an expired hour1 bucket (30 hours ago)
        expired_bucket = (
            (datetime.now(timezone.utc) - timedelta(hours=30))
            .replace(minute=0, second=0, microsecond=0)
            .isoformat()
        )
        backend.upsert_bucket("alice", "hour1", expired_bucket, "semantic")

        backend.cleanup_expired_buckets()

        with sqlite3.connect(db_file) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM api_metrics_buckets WHERE granularity='hour1'"
            ).fetchone()[0]

        assert count == 0, "Expired hour1 row must be deleted"

    def test_cleanup_removes_expired_day1_rows(self, backend_and_db):
        """Rows older than 15 days in day1 tier must be deleted."""
        backend, db_file = backend_and_db

        # Insert an expired day1 bucket (20 days ago)
        expired_bucket = (
            (datetime.now(timezone.utc) - timedelta(days=20))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .isoformat()
        )
        backend.upsert_bucket("alice", "day1", expired_bucket, "semantic")

        backend.cleanup_expired_buckets()

        with sqlite3.connect(db_file) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM api_metrics_buckets WHERE granularity='day1'"
            ).fetchone()[0]

        assert count == 0, "Expired day1 row must be deleted"

    def test_cleanup_keeps_current_day1_rows(self, backend_and_db):
        """Rows within 15 days in day1 tier must NOT be deleted."""
        backend, db_file = backend_and_db

        # Insert a current day1 bucket (5 days ago)
        current_bucket = (
            (datetime.now(timezone.utc) - timedelta(days=5))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .isoformat()
        )
        backend.upsert_bucket("alice", "day1", current_bucket, "semantic")

        backend.cleanup_expired_buckets()

        with sqlite3.connect(db_file) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM api_metrics_buckets WHERE granularity='day1'"
            ).fetchone()[0]

        assert count == 1, "Current day1 row must be preserved"

    def test_cleanup_mixed_expired_and_current(self, backend_and_db):
        """Cleanup must only delete expired rows and keep current rows."""
        backend, db_file = backend_and_db

        # Insert expired min1 bucket (20 minutes ago)
        expired = (
            (datetime.now(timezone.utc) - timedelta(minutes=20))
            .replace(second=0, microsecond=0)
            .isoformat()
        )
        backend.upsert_bucket("alice", "min1", expired, "semantic")

        # Insert current min1 bucket (5 minutes ago)
        current = (
            (datetime.now(timezone.utc) - timedelta(minutes=5))
            .replace(second=0, microsecond=0)
            .isoformat()
        )
        backend.upsert_bucket("alice", "min1", current, "semantic")

        backend.cleanup_expired_buckets()

        with sqlite3.connect(db_file) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM api_metrics_buckets WHERE granularity='min1'"
            ).fetchone()[0]

        assert count == 1, "Only current row must remain after cleanup"


class TestApiMetricsBucketsNodeId:
    """Test node_id column support in api_metrics_buckets table."""

    def test_node_id_column_exists_after_migration(self, backend_and_db):
        """api_metrics_buckets must have a node_id column after schema init."""
        _backend, db_file = backend_and_db

        with sqlite3.connect(db_file) as conn:
            cursor = conn.execute("PRAGMA table_info(api_metrics_buckets)")
            columns = {row[1] for row in cursor.fetchall()}

        assert "node_id" in columns, "node_id column must exist in api_metrics_buckets"

    def test_upsert_bucket_with_node_id(self, backend_and_db):
        """upsert_bucket with node_id must store the node_id in the row."""
        backend, db_file = backend_and_db

        backend.upsert_bucket(
            "alice", "min1", "2026-04-11T10:00:00", "semantic", node_id="node-1"
        )

        with sqlite3.connect(db_file) as conn:
            row = conn.execute(
                "SELECT node_id FROM api_metrics_buckets "
                "WHERE username=? AND granularity=? AND bucket_start=? AND metric_type=?",
                ("alice", "min1", "2026-04-11T10:00:00", "semantic"),
            ).fetchone()

        assert row is not None
        assert row[0] == "node-1", f"node_id must be 'node-1', got {row[0]!r}"

    def test_upsert_bucket_same_bucket_different_nodes_separate_rows(
        self, backend_and_db
    ):
        """Two nodes upserting into the same bucket must produce 2 separate rows."""
        backend, db_file = backend_and_db

        backend.upsert_bucket(
            "alice", "min1", "2026-04-11T10:00:00", "semantic", node_id="node-1"
        )
        backend.upsert_bucket(
            "alice", "min1", "2026-04-11T10:00:00", "semantic", node_id="node-2"
        )

        with sqlite3.connect(db_file) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM api_metrics_buckets "
                "WHERE username=? AND granularity=? AND bucket_start=? AND metric_type=?",
                ("alice", "min1", "2026-04-11T10:00:00", "semantic"),
            ).fetchone()[0]

        assert count == 2, (
            f"node-1 and node-2 must each have their own row, got {count} rows"
        )

    def test_get_metrics_bucketed_filters_by_node_id(self, backend_and_db):
        """get_metrics_bucketed with node_id must return only that node's counts."""
        backend, db_file = backend_and_db

        now = datetime.now(timezone.utc)
        bucket = now.replace(second=0, microsecond=0).isoformat()

        # node-1: 3 semantic calls, node-2: 5 semantic calls
        for _ in range(3):
            backend.upsert_bucket("alice", "min1", bucket, "semantic", node_id="node-1")
        for _ in range(5):
            backend.upsert_bucket("alice", "min1", bucket, "semantic", node_id="node-2")

        result = backend.get_metrics_bucketed(period_seconds=900, node_id="node-1")
        assert result["semantic_searches"] == 3, (
            f"node-1 must report 3 semantic searches, got {result['semantic_searches']}"
        )

    def test_get_metrics_bucketed_without_node_id_aggregates_all(self, backend_and_db):
        """get_metrics_bucketed without node_id must aggregate across all nodes."""
        backend, db_file = backend_and_db

        now = datetime.now(timezone.utc)
        bucket = now.replace(second=0, microsecond=0).isoformat()

        # node-1: 3 semantic, node-2: 5 semantic → total 8
        for _ in range(3):
            backend.upsert_bucket("alice", "min1", bucket, "semantic", node_id="node-1")
        for _ in range(5):
            backend.upsert_bucket("alice", "min1", bucket, "semantic", node_id="node-2")

        result = backend.get_metrics_bucketed(period_seconds=900, node_id=None)
        assert result["semantic_searches"] == 8, (
            f"Without node_id filter, must aggregate all nodes → 8, got {result['semantic_searches']}"
        )
