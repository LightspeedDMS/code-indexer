"""
Tests for Story #30: Health Check Performance Optimization.

AC1: PRAGMA integrity_check(1) optimization
AC2: Database health check caching with 60-second TTL
"""

import inspect
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch


class TestPragmaIntegrityCheck:
    """AC1: Tests for PRAGMA integrity_check(1) optimization."""

    def test_check_integrity_uses_integrity_check_1_not_quick_check(self):
        """
        AC1: Verify _check_integrity uses PRAGMA integrity_check(1)
        instead of PRAGMA quick_check.

        We verify by inspecting the source code to ensure the correct
        PRAGMA statement is used. This is the fastest reliable way to
        verify a specific SQL string literal in the implementation.
        """
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
        )

        source = inspect.getsource(DatabaseHealthService._check_integrity)

        # Must contain integrity_check(1) - the optimized single-page check
        assert "integrity_check(1)" in source, (
            "AC1: _check_integrity should use PRAGMA integrity_check(1) "
            "which checks only the first page for performance"
        )

        # Must NOT contain quick_check - the slow full-database scan
        assert "quick_check" not in source, (
            "AC1: _check_integrity should NOT use PRAGMA quick_check "
            "as it scans the entire database (85+ seconds for large DBs)"
        )

    def test_check_integrity_returns_ok_for_healthy_database(self):
        """Integrity check should return passed=True for healthy database."""
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()

            result = DatabaseHealthService._check_integrity(db_path)

            assert result.passed is True
            assert result.error_message is None

        finally:
            Path(db_path).unlink(missing_ok=True)


class TestDatabaseHealthCaching:
    """AC2: Tests for database health check caching with 60-second TTL."""

    def test_cache_initialized_as_empty(self):
        """AC2: Cache starts empty when service is instantiated."""
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
        )

        service = DatabaseHealthService()

        assert hasattr(service, "_health_cache")
        assert isinstance(service._health_cache, dict)
        assert len(service._health_cache) == 0

    def test_cache_stores_result_after_first_check(self):
        """AC2: First health check populates the cache."""
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()

            service = DatabaseHealthService()
            result = service.check_database_health_cached(db_path, "Test DB")

            assert result is not None
            assert db_path in service._health_cache
            # Cache entry: (result, timestamp)
            cached_entry = service._health_cache[db_path]
            assert isinstance(cached_entry, tuple)
            assert len(cached_entry) == 2

        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_cache_returns_cached_within_ttl(self):
        """AC2: Requests within 60s return cached result without re-check."""
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()

            service = DatabaseHealthService()

            # First call
            result1 = service.check_database_health_cached(db_path, "Test DB")
            first_timestamp = service._health_cache[db_path][1]

            # Second call within TTL - should return same cached result
            result2 = service.check_database_health_cached(db_path, "Test DB")
            second_timestamp = service._health_cache[db_path][1]

            # Timestamp should be unchanged (no re-check)
            assert first_timestamp == second_timestamp
            assert result1.status == result2.status

        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_get_all_database_health_cached_uses_caching(self):
        """AC6: get_all_database_health_cached should use caching for all DBs."""
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
            DatabaseHealthResult,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            # Create necessary subdirectories
            data_dir = Path(temp_dir) / "data"
            data_dir.mkdir(parents=True)
            golden_cache = Path(temp_dir) / "data" / "golden-repos" / ".cache"
            golden_cache.mkdir(parents=True)

            # Create all databases matching DATABASE_DISPLAY_NAMES
            db_paths = {
                "cidx_server.db": data_dir / "cidx_server.db",
                "oauth.db": Path(temp_dir) / "oauth.db",
                "logs.db": Path(temp_dir) / "logs.db",
                "refresh_tokens.db": Path(temp_dir) / "refresh_tokens.db",
                "groups.db": Path(temp_dir) / "groups.db",
                "scip_audit.db": Path(temp_dir) / "scip_audit.db",
                "payload_cache.db": golden_cache / "payload_cache.db",
            }

            for db_path in db_paths.values():
                conn = sqlite3.connect(str(db_path))
                conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
                conn.commit()
                conn.close()

            service = DatabaseHealthService(server_dir=temp_dir)

            # First call - should populate cache
            results1 = service.get_all_database_health_cached()

            assert results1 is not None
            assert isinstance(results1, list)
            assert len(results1) == len(db_paths)
            assert all(isinstance(r, DatabaseHealthResult) for r in results1)

            # Verify cache was populated
            assert len(service._health_cache) == len(db_paths)

            # Record timestamps from cache
            timestamps1 = {
                path: ts for path, (_, ts) in service._health_cache.items()
            }

            # Second call - should return cached results
            _ = service.get_all_database_health_cached()

            # Timestamps should be unchanged (cache hit)
            timestamps2 = {
                path: ts for path, (_, ts) in service._health_cache.items()
            }
            assert timestamps1 == timestamps2


class TestSingletonCacheBehavior:
    """
    Tests for Story #30 critical bug fix: singleton cache pattern.

    The original bug: Creating new DatabaseHealthService() instances on each
    request means the instance-level cache is always empty. The fix uses a
    singleton pattern via get_database_health_service() to ensure the cache
    is shared across all callers.
    """

    def test_get_database_health_service_returns_singleton(self):
        """get_database_health_service() should return the same instance."""
        from code_indexer.server.services.database_health_service import (
            get_database_health_service,
            _reset_singleton_for_testing,
        )

        # Reset singleton to ensure clean state
        _reset_singleton_for_testing()

        try:
            service1 = get_database_health_service()
            service2 = get_database_health_service()

            # Must be the exact same instance
            assert service1 is service2, (
                "get_database_health_service() should return singleton instance "
                "to ensure cache is shared across all callers"
            )
        finally:
            _reset_singleton_for_testing()

    def test_singleton_cache_shared_across_calls(self):
        """Cache should be shared when using singleton pattern."""
        from code_indexer.server.services.database_health_service import (
            get_database_health_service,
            _reset_singleton_for_testing,
        )

        _reset_singleton_for_testing()

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()

            # First call - populates cache
            service1 = get_database_health_service()
            result1 = service1.check_database_health_cached(db_path, "Test DB")
            timestamp1 = service1._health_cache[db_path][1]

            # Second call via singleton - should see same cache
            service2 = get_database_health_service()
            result2 = service2.check_database_health_cached(db_path, "Test DB")
            timestamp2 = service2._health_cache[db_path][1]

            # Cache should be shared - same timestamp means cache hit
            assert timestamp1 == timestamp2, (
                "Singleton pattern should share cache across calls. "
                "Different timestamps indicate cache was not shared."
            )
            assert result1.status == result2.status

        finally:
            Path(db_path).unlink(missing_ok=True)
            _reset_singleton_for_testing()

    def test_cache_expiry_after_ttl(self):
        """
        AC2: Cache should expire after 60 seconds.

        Uses time.time() mocking to verify cache expiry without waiting.
        """
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
            CACHE_TTL_SECONDS,
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()

            service = DatabaseHealthService()
            base_time = 1000000.0

            # First call at t=0
            with patch("time.time", return_value=base_time):
                _ = service.check_database_health_cached(db_path, "Test DB")
                timestamp1 = service._health_cache[db_path][1]

            # Second call at t=30s (within TTL) - should be cached
            with patch("time.time", return_value=base_time + 30):
                _ = service.check_database_health_cached(db_path, "Test DB")
                timestamp2 = service._health_cache[db_path][1]

            assert timestamp1 == timestamp2, "Cache should still be valid at 30s"

            # Third call at t=61s (after TTL) - should refresh
            with patch("time.time", return_value=base_time + CACHE_TTL_SECONDS + 1):
                _ = service.check_database_health_cached(db_path, "Test DB")
                timestamp3 = service._health_cache[db_path][1]

            # After TTL, timestamp should be updated to new time
            assert timestamp3 == base_time + CACHE_TTL_SECONDS + 1, (
                f"Cache should refresh after {CACHE_TTL_SECONDS}s TTL. "
                f"Expected timestamp {base_time + CACHE_TTL_SECONDS + 1}, got {timestamp3}"
            )
            assert timestamp3 != timestamp1, "Cache should have been refreshed"

        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_direct_instantiation_does_not_share_cache(self):
        """
        Demonstrates the bug: direct instantiation creates separate caches.

        This test documents why the singleton pattern is necessary.
        Direct DatabaseHealthService() instantiation creates independent
        instances with their own empty caches.
        """
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()

            # First instance - populate its cache
            service1 = DatabaseHealthService()
            service1.check_database_health_cached(db_path, "Test DB")
            assert db_path in service1._health_cache

            # Second instance - has its own empty cache
            service2 = DatabaseHealthService()
            assert db_path not in service2._health_cache, (
                "New instance should have empty cache - "
                "this demonstrates why singleton is needed"
            )

            # They are different instances
            assert service1 is not service2

        finally:
            Path(db_path).unlink(missing_ok=True)


class TestDatabaseHealthErrorCases:
    """
    Tests for error handling and edge cases in DatabaseHealthService.

    These tests cover the uncovered error paths to achieve >90% coverage.
    """

    def test_check_connect_fails_when_file_not_found(self):
        """_check_connect should fail when database file doesn't exist."""
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
        )

        result = DatabaseHealthService._check_connect("/nonexistent/path/db.db")

        assert result.passed is False
        assert "file not found" in result.error_message.lower()

    def test_check_connect_fails_on_exception(self):
        """_check_connect should handle exceptions gracefully."""
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
        )

        # A directory path will cause sqlite3.connect to raise an error
        result = DatabaseHealthService._check_connect("/tmp")

        assert result.passed is False
        assert "Connection failed" in result.error_message

    def test_check_database_health_fails_all_when_connect_fails(self):
        """When connect fails, all other checks should also fail."""
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
            DatabaseHealthStatus,
        )

        result = DatabaseHealthService.check_database_health(
            "/nonexistent/path/db.db", "Test DB"
        )

        assert result.status == DatabaseHealthStatus.ERROR
        assert result.checks["connect"].passed is False
        assert result.checks["read"].passed is False
        assert result.checks["read"].error_message == "Connection required"
        assert result.checks["write"].passed is False
        assert result.checks["write"].error_message == "Connection required"
        assert result.checks["integrity"].passed is False
        assert result.checks["integrity"].error_message == "Connection required"
        assert result.checks["not_locked"].passed is False
        assert result.checks["not_locked"].error_message == "Connection required"

    def test_check_read_fails_on_invalid_db(self):
        """_check_read should fail when database is corrupted."""
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
        )

        # Create a file that's not a valid SQLite database
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            f.write(b"This is not a valid SQLite database")
            db_path = f.name

        try:
            result = DatabaseHealthService._check_read(db_path)

            assert result.passed is False
            assert "Read failed" in result.error_message
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_check_write_fails_on_readonly_db(self):
        """_check_write should fail when database is read-only."""
        import os
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # Create valid database then make it read-only
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()

            # Make file read-only
            os.chmod(db_path, 0o444)

            result = DatabaseHealthService._check_write(db_path)

            assert result.passed is False
            assert "Write failed" in result.error_message
        finally:
            # Restore write permission for cleanup
            os.chmod(db_path, 0o644)
            Path(db_path).unlink(missing_ok=True)

    def test_check_integrity_fails_on_corrupted_db(self):
        """_check_integrity should handle corrupted databases."""
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
        )

        # Create a file that's not a valid SQLite database
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            f.write(b"This is not a valid SQLite database at all")
            db_path = f.name

        try:
            result = DatabaseHealthService._check_integrity(db_path)

            assert result.passed is False
            assert "Integrity check failed" in result.error_message
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_check_not_locked_fails_on_invalid_db(self):
        """_check_not_locked should handle errors gracefully."""
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
        )

        # Create a file that's not a valid SQLite database
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            f.write(b"This is not a valid SQLite database")
            db_path = f.name

        try:
            result = DatabaseHealthService._check_not_locked(db_path)

            assert result.passed is False
            assert "Lock check failed" in result.error_message
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_determine_status_returns_error_when_read_fails(self):
        """_determine_status should return ERROR when read check fails."""
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
            DatabaseHealthStatus,
            CheckResult,
        )

        checks = {
            "connect": CheckResult(passed=True),
            "read": CheckResult(passed=False, error_message="Read failed"),
            "write": CheckResult(passed=True),
            "integrity": CheckResult(passed=True),
            "not_locked": CheckResult(passed=True),
        }

        status = DatabaseHealthService._determine_status(checks)

        assert status == DatabaseHealthStatus.ERROR

    def test_determine_status_returns_warning_when_write_fails(self):
        """_determine_status should return WARNING when non-critical check fails."""
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
            DatabaseHealthStatus,
            CheckResult,
        )

        checks = {
            "connect": CheckResult(passed=True),
            "read": CheckResult(passed=True),
            "write": CheckResult(passed=False, error_message="Write failed"),
            "integrity": CheckResult(passed=True),
            "not_locked": CheckResult(passed=True),
        }

        status = DatabaseHealthService._determine_status(checks)

        assert status == DatabaseHealthStatus.WARNING

    def test_get_all_database_health_uncached(self):
        """get_all_database_health() should return health for all 7 databases."""
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
            DATABASE_DISPLAY_NAMES,
            DatabaseHealthResult,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            # Create necessary subdirectories
            data_dir = Path(temp_dir) / "data"
            data_dir.mkdir(parents=True)
            golden_cache = Path(temp_dir) / "data" / "golden-repos" / ".cache"
            golden_cache.mkdir(parents=True)

            # Create all databases matching DATABASE_DISPLAY_NAMES
            db_paths = {
                "cidx_server.db": data_dir / "cidx_server.db",
                "oauth.db": Path(temp_dir) / "oauth.db",
                "logs.db": Path(temp_dir) / "logs.db",
                "refresh_tokens.db": Path(temp_dir) / "refresh_tokens.db",
                "groups.db": Path(temp_dir) / "groups.db",
                "scip_audit.db": Path(temp_dir) / "scip_audit.db",
                "payload_cache.db": golden_cache / "payload_cache.db",
            }

            for db_path in db_paths.values():
                conn = sqlite3.connect(str(db_path))
                conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
                conn.commit()
                conn.close()

            service = DatabaseHealthService(server_dir=temp_dir)

            # Call uncached method
            results = service.get_all_database_health()

            assert results is not None
            assert isinstance(results, list)
            assert len(results) == len(DATABASE_DISPLAY_NAMES)
            assert all(isinstance(r, DatabaseHealthResult) for r in results)

            # Verify cache is NOT populated (uncached method)
            assert len(service._health_cache) == 0
