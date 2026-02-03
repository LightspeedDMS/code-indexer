"""
Unit tests for Database-Backed ApiMetricsService.

Tests for the SQLite database storage implementation that allows
multiple uvicorn workers to share API metrics data.

Following TDD methodology: Write failing tests FIRST, then implement.
"""

import sqlite3
from pathlib import Path
from unittest.mock import patch


class TestApiMetricsServiceDatabaseInit:
    """Test database initialization for ApiMetricsService."""

    def test_initialize_creates_table(self, tmp_path: Path):
        """Test that initialize() creates the api_metrics table."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        # Verify table exists by connecting directly
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='api_metrics'"
        )
        result = cursor.fetchone()
        conn.close()

        assert result is not None, "api_metrics table should exist"
        assert result[0] == "api_metrics"

    def test_initialize_creates_index(self, tmp_path: Path):
        """Test that initialize() creates the composite index."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        # Verify index exists
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_api_metrics_type_timestamp'"
        )
        result = cursor.fetchone()
        conn.close()

        assert result is not None, "Index idx_api_metrics_type_timestamp should exist"

    def test_initialize_is_idempotent(self, tmp_path: Path):
        """Test that initialize() can be called multiple times safely."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()

        # Call initialize multiple times
        service.initialize(str(db_path))
        service.initialize(str(db_path))
        service.initialize(str(db_path))

        # Should not raise any errors and table should exist
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='api_metrics'"
        )
        result = cursor.fetchone()
        conn.close()

        assert result is not None


class TestApiMetricsServiceDatabaseInsert:
    """Test that increment methods insert records into database."""

    def test_increment_semantic_search_inserts_record(self, tmp_path: Path):
        """Test that increment_semantic_search() inserts a record."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        service.increment_semantic_search()

        # Verify record exists in database
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM api_metrics WHERE metric_type = 'semantic'"
        )
        count = cursor.fetchone()[0]
        conn.close()

        assert count == 1, "Should have 1 semantic record"

    def test_increment_other_index_search_inserts_record(self, tmp_path: Path):
        """Test that increment_other_index_search() inserts a record."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        service.increment_other_index_search()
        service.increment_other_index_search()

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM api_metrics WHERE metric_type = 'other_index'"
        )
        count = cursor.fetchone()[0]
        conn.close()

        assert count == 2, "Should have 2 other_index records"

    def test_increment_regex_and_other_api_inserts_records(self, tmp_path: Path):
        """Test that increment_regex_search and increment_other_api_call insert records."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        service.increment_regex_search()
        service.increment_other_api_call()
        service.increment_other_api_call()
        service.increment_other_api_call()

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM api_metrics WHERE metric_type = 'regex'")
        regex_count = cursor.fetchone()[0]
        cursor.execute(
            "SELECT COUNT(*) FROM api_metrics WHERE metric_type = 'other_api'"
        )
        other_api_count = cursor.fetchone()[0]
        conn.close()

        assert regex_count == 1, "Should have 1 regex record"
        assert other_api_count == 3, "Should have 3 other_api records"


class TestApiMetricsServiceDatabaseQuery:
    """Test get_metrics() counts correctly from database."""

    def test_get_metrics_counts_within_window(self, tmp_path: Path):
        """Test that get_metrics counts only records within the time window."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        # Add some records
        service.increment_semantic_search()
        service.increment_semantic_search()
        service.increment_other_index_search()
        service.increment_regex_search()
        service.increment_other_api_call()
        service.increment_other_api_call()
        service.increment_other_api_call()

        # Get metrics with default window (60 seconds)
        metrics = service.get_metrics()

        assert metrics["semantic_searches"] == 2
        assert metrics["other_index_searches"] == 1
        assert metrics["regex_searches"] == 1
        assert metrics["other_api_calls"] == 3

    def test_get_metrics_respects_window_parameter(self, tmp_path: Path):
        """Test that get_metrics respects the window_seconds parameter."""
        from datetime import datetime, timedelta, timezone
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        # Manually insert old records (2 hours ago)
        old_timestamp = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO api_metrics (metric_type, timestamp) VALUES (?, ?)",
            ("semantic", old_timestamp),
        )
        cursor.execute(
            "INSERT INTO api_metrics (metric_type, timestamp) VALUES (?, ?)",
            ("semantic", old_timestamp),
        )
        conn.commit()
        conn.close()

        # Add recent records
        service.increment_semantic_search()

        # Get metrics with 1 hour window - should only count recent
        metrics = service.get_metrics(window_seconds=3600)
        assert metrics["semantic_searches"] == 1, "Should only count recent record"

        # Get metrics with 24 hour window - should count all
        metrics = service.get_metrics(window_seconds=86400)
        assert metrics["semantic_searches"] == 3, "Should count all records"

    def test_get_metrics_empty_database(self, tmp_path: Path):
        """Test that get_metrics returns zeros for empty database."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        metrics = service.get_metrics()

        assert metrics["semantic_searches"] == 0
        assert metrics["other_index_searches"] == 0
        assert metrics["regex_searches"] == 0
        assert metrics["other_api_calls"] == 0


class TestApiMetricsServiceDatabaseCleanupReset:
    """Test cleanup removes old records and reset clears all records."""

    def test_cleanup_removes_old_records(self, tmp_path: Path):
        """Test that _cleanup_old removes records older than 24 hours."""
        from datetime import datetime, timedelta, timezone
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        # Insert old record (25 hours ago)
        old_timestamp = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO api_metrics (metric_type, timestamp) VALUES (?, ?)",
            ("semantic", old_timestamp),
        )
        conn.commit()
        conn.close()

        # Add a new record
        service.increment_semantic_search()

        # Explicitly call cleanup (cleanup now runs periodically, not on every insert)
        service._cleanup_old()

        # Check that old record is removed but new one exists
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM api_metrics WHERE metric_type = 'semantic'"
        )
        count = cursor.fetchone()[0]
        conn.close()

        assert (
            count == 1
        ), "Should only have the new record, old one should be cleaned up"

    def test_reset_clears_all_records(self, tmp_path: Path):
        """Test that reset() removes all records from database."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        # Add records
        service.increment_semantic_search()
        service.increment_other_index_search()
        service.increment_regex_search()
        service.increment_other_api_call()

        # Verify records exist
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM api_metrics")
        count_before = cursor.fetchone()[0]
        conn.close()
        assert count_before == 4, "Should have 4 records before reset"

        # Reset
        service.reset()

        # Verify all records removed
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM api_metrics")
        count_after = cursor.fetchone()[0]
        conn.close()
        assert count_after == 0, "Should have 0 records after reset"


class TestApiMetricsServiceMultiWorker:
    """Test that multiple workers (separate connections) see same data."""

    def test_multiple_connections_share_data(self, tmp_path: Path):
        """Test that multiple service instances see the same database data."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        db_path = tmp_path / "test_metrics.db"

        # Simulate worker 1
        service1 = ApiMetricsService()
        service1.initialize(str(db_path))
        service1.increment_semantic_search()
        service1.increment_semantic_search()

        # Simulate worker 2 (new instance, same DB)
        service2 = ApiMetricsService()
        service2.initialize(str(db_path))
        service2.increment_semantic_search()

        # Both workers should see 3 semantic searches
        metrics1 = service1.get_metrics()
        metrics2 = service2.get_metrics()

        assert metrics1["semantic_searches"] == 3
        assert metrics2["semantic_searches"] == 3

    def test_concurrent_inserts_from_multiple_workers(self, tmp_path: Path):
        """Test that concurrent inserts from multiple workers are all recorded."""
        import concurrent.futures
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        db_path = tmp_path / "test_metrics.db"

        # Initialize database first
        init_service = ApiMetricsService()
        init_service.initialize(str(db_path))
        init_service.reset()

        num_workers = 4
        increments_per_worker = 50
        expected_total = num_workers * increments_per_worker

        def worker_task(worker_id: int):
            """Each worker creates own service instance and increments."""
            service = ApiMetricsService()
            service.initialize(str(db_path))
            for _ in range(increments_per_worker):
                service.increment_semantic_search()

        # Run workers concurrently
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(worker_task, i) for i in range(num_workers)]
            for future in concurrent.futures.as_completed(futures):
                future.result()

        # Verify total count
        verify_service = ApiMetricsService()
        verify_service.initialize(str(db_path))
        metrics = verify_service.get_metrics()

        assert (
            metrics["semantic_searches"] == expected_total
        ), f"Expected {expected_total}, got {metrics['semantic_searches']}"


class TestApiMetricsServiceUninitializedGraceful:
    """Test graceful handling when service is not initialized."""

    def test_increment_without_init_does_not_crash(self):
        """Test that increment methods don't crash when not initialized."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()
        # Do NOT call initialize

        # These should not raise exceptions - they should just log and skip
        service.increment_semantic_search()
        service.increment_other_index_search()
        service.increment_regex_search()
        service.increment_other_api_call()

    def test_get_metrics_without_init_returns_zeros(self):
        """Test that get_metrics returns zeros when not initialized."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()
        # Do NOT call initialize

        metrics = service.get_metrics()

        # Should return zeros, not crash
        assert metrics["semantic_searches"] == 0
        assert metrics["other_index_searches"] == 0
        assert metrics["regex_searches"] == 0
        assert metrics["other_api_calls"] == 0

    def test_reset_without_init_does_not_crash(self):
        """Test that reset doesn't crash when not initialized."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()
        # Do NOT call initialize

        # Should not raise
        service.reset()


class TestApiMetricsServicePeriodicCleanup:
    """Test that cleanup only runs periodically, not on every insert (Issue #1)."""

    def test_cleanup_only_runs_every_cleanup_interval_inserts(self, tmp_path: Path):
        """Test that _cleanup_old is only called every CLEANUP_INTERVAL inserts."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
            CLEANUP_INTERVAL,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        cleanup_call_count = 0
        original_cleanup = service._cleanup_old

        def tracking_cleanup():
            nonlocal cleanup_call_count
            cleanup_call_count += 1
            original_cleanup()

        service._cleanup_old = tracking_cleanup

        # Insert fewer than CLEANUP_INTERVAL records - cleanup should NOT run
        for _ in range(CLEANUP_INTERVAL - 1):
            service.increment_semantic_search()

        assert cleanup_call_count == 0, (
            f"Cleanup should not run before {CLEANUP_INTERVAL} inserts, "
            f"but ran {cleanup_call_count} times"
        )

        # Insert one more record - cleanup SHOULD run now
        service.increment_semantic_search()

        assert cleanup_call_count == 1, (
            f"Cleanup should run once at {CLEANUP_INTERVAL} inserts, "
            f"but ran {cleanup_call_count} times"
        )

    def test_cleanup_runs_again_after_next_interval(self, tmp_path: Path):
        """Test that cleanup runs again after another CLEANUP_INTERVAL inserts."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
            CLEANUP_INTERVAL,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        cleanup_call_count = 0
        original_cleanup = service._cleanup_old

        def tracking_cleanup():
            nonlocal cleanup_call_count
            cleanup_call_count += 1
            original_cleanup()

        service._cleanup_old = tracking_cleanup

        # Insert CLEANUP_INTERVAL * 2 records - cleanup should run twice
        for _ in range(CLEANUP_INTERVAL * 2):
            service.increment_semantic_search()

        assert cleanup_call_count == 2, (
            f"Cleanup should run twice after {CLEANUP_INTERVAL * 2} inserts, "
            f"but ran {cleanup_call_count} times"
        )

    def test_insert_count_is_thread_safe(self, tmp_path: Path):
        """Test that insert counter is thread-safe with concurrent inserts."""
        import concurrent.futures
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
            CLEANUP_INTERVAL,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        cleanup_call_count = 0
        original_cleanup = service._cleanup_old

        def tracking_cleanup():
            nonlocal cleanup_call_count
            cleanup_call_count += 1
            original_cleanup()

        service._cleanup_old = tracking_cleanup

        num_workers = 4
        inserts_per_worker = CLEANUP_INTERVAL  # Total = 4 * CLEANUP_INTERVAL

        def worker_task():
            for _ in range(inserts_per_worker):
                service.increment_semantic_search()

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(worker_task) for _ in range(num_workers)]
            for future in concurrent.futures.as_completed(futures):
                future.result()

        # With 4 workers * CLEANUP_INTERVAL inserts = 4 cleanups expected
        # Allow some variance due to race conditions (3-5 is acceptable)
        assert 3 <= cleanup_call_count <= 5, (
            f"Cleanup should run approximately 4 times after {num_workers * inserts_per_worker} "
            f"inserts, but ran {cleanup_call_count} times"
        )


class TestApiMetricsServiceRetryLogic:
    """Test retry logic for database lock errors (Issues #2 and #3)."""

    def test_retry_on_database_locked_error(self, tmp_path: Path):
        """Test that _insert_metric retries on database locked error."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        # Track connection attempts
        connection_attempts = 0
        original_connect = sqlite3.connect

        def mock_connect(*args, **kwargs):
            nonlocal connection_attempts
            connection_attempts += 1
            if connection_attempts < 3:
                # Simulate database lock on first two attempts
                raise sqlite3.OperationalError("database is locked")
            return original_connect(*args, **kwargs)

        with patch("sqlite3.connect", side_effect=mock_connect):
            service.increment_semantic_search()

        # Should have tried 3 times (2 failures + 1 success)
        assert (
            connection_attempts == 3
        ), f"Expected 3 connection attempts, got {connection_attempts}"

        # Verify record was inserted after retry succeeded
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM api_metrics WHERE metric_type = 'semantic'"
        )
        count = cursor.fetchone()[0]
        conn.close()

        assert count == 1, "Record should be inserted after retry succeeds"

    def test_exponential_backoff_on_retry(self, tmp_path: Path):
        """Test that retries use exponential backoff with jitter."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
            RETRY_BASE_DELAY,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        sleep_calls = []

        def mock_sleep(duration):
            sleep_calls.append(duration)
            # Don't actually sleep in tests

        connection_attempts = 0
        original_connect = sqlite3.connect

        def mock_connect(*args, **kwargs):
            nonlocal connection_attempts
            connection_attempts += 1
            if connection_attempts < 4:
                raise sqlite3.OperationalError("database is locked")
            return original_connect(*args, **kwargs)

        with patch("sqlite3.connect", side_effect=mock_connect):
            with patch("time.sleep", side_effect=mock_sleep):
                service.increment_semantic_search()

        # Should have 3 sleep calls (before retries 2, 3, 4)
        assert (
            len(sleep_calls) == 3
        ), f"Expected 3 sleep calls for exponential backoff, got {len(sleep_calls)}"

        # Verify exponential progression (each delay should roughly double)
        # Base delay = 0.01, so delays should be approximately:
        # 0.01 * (2^0) + jitter, 0.01 * (2^1) + jitter, 0.01 * (2^2) + jitter
        for i, delay in enumerate(sleep_calls):
            expected_base = RETRY_BASE_DELAY * (2**i)
            # Allow for jitter up to 0.01
            assert expected_base <= delay <= expected_base + 0.01, (
                f"Delay {i} should be between {expected_base} and {expected_base + 0.01}, "
                f"got {delay}"
            )


class TestApiMetricsServiceGracefulDegradation:
    """Test graceful degradation on persistent database errors (Issue #3)."""

    def test_graceful_degradation_after_max_retries(self, tmp_path: Path):
        """Test that service continues after all retries fail (no crash)."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
            MAX_RETRIES,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        connection_attempts = 0

        def always_fail_connect(*args, **kwargs):
            nonlocal connection_attempts
            connection_attempts += 1
            raise sqlite3.OperationalError("database is locked")

        with patch("sqlite3.connect", side_effect=always_fail_connect):
            # Should NOT raise exception - graceful degradation
            service.increment_semantic_search()

        # Should have tried MAX_RETRIES times
        assert (
            connection_attempts == MAX_RETRIES
        ), f"Expected {MAX_RETRIES} retry attempts, got {connection_attempts}"

    def test_logs_warning_on_persistent_failure(self, tmp_path: Path, caplog):
        """Test that a warning is logged when all retries fail."""
        import logging
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        def always_fail_connect(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        with patch("sqlite3.connect", side_effect=always_fail_connect):
            with caplog.at_level(logging.WARNING):
                service.increment_semantic_search()

        # Check that warning was logged
        assert any(
            "Failed to insert metric" in record.message for record in caplog.records
        ), "Expected warning log about failed metric insert"

    def test_other_error_types_do_not_retry(self, tmp_path: Path):
        """Test that non-lock errors fail fast without retrying."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        db_path = tmp_path / "test_metrics.db"
        service = ApiMetricsService()
        service.initialize(str(db_path))

        connection_attempts = 0

        def fail_with_other_error(*args, **kwargs):
            nonlocal connection_attempts
            connection_attempts += 1
            raise sqlite3.OperationalError("disk I/O error")

        with patch("sqlite3.connect", side_effect=fail_with_other_error):
            # Should NOT raise exception - graceful degradation
            service.increment_semantic_search()

        # Should only try once for non-lock errors
        assert (
            connection_attempts == 1
        ), f"Non-lock errors should not retry, but tried {connection_attempts} times"
