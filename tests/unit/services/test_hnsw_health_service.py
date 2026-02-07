"""
Unit tests for HNSWHealthService.

Tests the core health checking logic for HNSW indexes including:
- Progressive error handling (file exists -> readable -> loadable -> integrity)
- Two-tier caching (TTL + mtime invalidation)
- Comprehensive health check results

Story #56: HNSWHealthService Core Logic
"""

import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

# Module under test will be imported after implementation
# from code_indexer.services.hnsw_health_service import (
#     HNSWHealthService,
#     HealthCheckResult,
#     check_health_async,
# )


class TestHealthCheckResultModel:
    """Test HealthCheckResult Pydantic model structure and validation."""

    def test_model_creation_with_all_fields(self):
        """
        AC #8: Health check returns comprehensive metadata.

        Test that HealthCheckResult model can be created with all required fields.
        This test will FAIL until we implement the Pydantic model.
        """
        from code_indexer.services.hnsw_health_service import HealthCheckResult

        # Create result with all fields
        result = HealthCheckResult(
            valid=True,
            file_exists=True,
            readable=True,
            loadable=True,
            element_count=1000,
            connections_checked=5000,
            min_inbound=2,
            max_inbound=10,
            index_path="/path/to/index.bin",
            file_size_bytes=1024000,
            last_modified=datetime.now(timezone.utc),
            errors=[],
            check_duration_ms=45.5,
            from_cache=False,
        )

        # Verify all fields present
        assert result.valid is True
        assert result.file_exists is True
        assert result.readable is True
        assert result.loadable is True
        assert result.element_count == 1000
        assert result.connections_checked == 5000
        assert result.min_inbound == 2
        assert result.max_inbound == 10
        assert result.index_path == "/path/to/index.bin"
        assert result.file_size_bytes == 1024000
        assert result.last_modified is not None
        assert result.errors == []
        assert result.check_duration_ms == 45.5
        assert result.from_cache is False

    def test_model_creation_with_minimal_fields(self):
        """
        Test model with only required fields (defaults for optional).

        This test will FAIL until we implement the Pydantic model with correct defaults.
        """
        from code_indexer.services.hnsw_health_service import HealthCheckResult

        result = HealthCheckResult(
            valid=False,
            file_exists=False,
            readable=False,
            loadable=False,
            index_path="/missing/index.bin",
            check_duration_ms=0.5,
        )

        # Required fields
        assert result.valid is False
        assert result.file_exists is False
        assert result.index_path == "/missing/index.bin"
        assert result.check_duration_ms == 0.5

        # Optional fields should have defaults
        assert result.element_count is None
        assert result.connections_checked is None
        assert result.min_inbound is None
        assert result.max_inbound is None
        assert result.file_size_bytes is None
        assert result.last_modified is None
        assert result.errors == []
        assert result.from_cache is False


class TestHNSWHealthServiceInit:
    """Test HNSWHealthService initialization."""

    def test_service_creation_with_default_ttl(self):
        """
        Test that service can be created with default TTL of 300 seconds.

        This test will FAIL until we implement HNSWHealthService class.
        """
        from code_indexer.services.hnsw_health_service import HNSWHealthService

        service = HNSWHealthService()

        # Default TTL should be 300 seconds (5 minutes)
        assert hasattr(service, '_cache_ttl')
        assert service._cache_ttl == 300

    def test_service_creation_with_custom_ttl(self):
        """
        Test that service can be created with custom TTL.

        This test will FAIL until we implement HNSWHealthService class.
        """
        from code_indexer.services.hnsw_health_service import HNSWHealthService

        service = HNSWHealthService(cache_ttl_seconds=600)

        assert service._cache_ttl == 600

    def test_service_has_empty_cache_on_init(self):
        """
        Test that service initializes with empty cache.

        This test will FAIL until we implement HNSWHealthService class.
        """
        from code_indexer.services.hnsw_health_service import HNSWHealthService

        service = HNSWHealthService()

        assert hasattr(service, '_cache')
        assert len(service._cache) == 0


class TestHealthCheckMissingFile:
    """Test health check behavior when index file is missing."""

    def test_missing_file_returns_appropriate_result(self):
        """
        AC #3: Health check on missing index file returns file_exists=False, valid=False, appropriate error.

        This test will FAIL until we implement check_health method with file existence check.
        """
        from code_indexer.services.hnsw_health_service import HNSWHealthService

        service = HNSWHealthService()

        # Check health of non-existent file
        result = service.check_health("/nonexistent/index.bin")

        # Should indicate file doesn't exist
        assert result.file_exists is False
        assert result.valid is False
        assert result.readable is False
        assert result.loadable is False

        # Should have error message
        assert len(result.errors) > 0
        assert "not found" in result.errors[0].lower() or "does not exist" in result.errors[0].lower()

        # Other metrics should be None
        assert result.element_count is None
        assert result.connections_checked is None
        assert result.file_size_bytes is None
        assert result.last_modified is None

        # Should have execution time
        assert result.check_duration_ms >= 0
        assert result.from_cache is False


class TestHealthCheckUnreadableFile:
    """Test health check behavior when index file exists but is not readable."""

    @pytest.mark.skipif(os.name == 'nt', reason="Permission tests unreliable on Windows")
    def test_unreadable_file_returns_appropriate_result(self):
        """
        AC #4: Health check on unreadable index file returns readable=False, valid=False, permission error.

        This test will FAIL until we implement check_health method with readability check.
        """
        from code_indexer.services.hnsw_health_service import HNSWHealthService

        service = HNSWHealthService()

        # Create a file with no read permissions
        with tempfile.NamedTemporaryFile(delete=False) as f:
            index_path = f.name
            f.write(b"dummy index data")

        try:
            # Remove read permissions
            os.chmod(index_path, 0o000)

            # Check health
            result = service.check_health(index_path)

            # Should indicate file is not readable
            assert result.file_exists is True
            assert result.readable is False
            assert result.valid is False
            assert result.loadable is False

            # Should have permission error
            assert len(result.errors) > 0
            assert "permission" in result.errors[0].lower() or "not readable" in result.errors[0].lower()

            # Should have file size (can stat without reading)
            assert result.file_size_bytes is not None
            assert result.last_modified is not None

            # Other metrics should be None
            assert result.element_count is None
            assert result.connections_checked is None
        finally:
            # Restore permissions and cleanup
            os.chmod(index_path, 0o644)
            os.unlink(index_path)


class TestHealthCheckValidIndex:
    """Test health check behavior with valid HNSW index."""

    def test_valid_index_returns_success_result(self):
        """
        AC #1: Health check on valid index returns HealthCheckResult with valid=True,
        element_count, connections_checked, empty errors.

        This test will FAIL until we implement check_health method with hnswlib integration.
        """
        from code_indexer.services.hnsw_health_service import HNSWHealthService

        service = HNSWHealthService()

        # Create a temporary file to represent index
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            index_path = f.name
            f.write(b"dummy index data")

        try:
            # Mock hnswlib.Index to simulate valid index
            with patch('hnswlib.Index') as mock_index_class:
                mock_index = Mock()
                # check_integrity() returns a dictionary
                mock_index.check_integrity.return_value = {
                    "valid": True,
                    "connections_checked": 5000,
                    "element_count": 1000,
                    "min_inbound": 2,
                    "max_inbound": 10,
                    "errors": [],
                }
                mock_index_class.return_value = mock_index

                # Check health
                result = service.check_health(index_path)

                # Should indicate success
                assert result.valid is True
                assert result.file_exists is True
                assert result.readable is True
                assert result.loadable is True

                # Should have integrity metrics
                assert result.element_count == 1000
                assert result.connections_checked == 5000
                assert result.min_inbound == 2
                assert result.max_inbound == 10

                # Should have no errors
                assert len(result.errors) == 0

                # Should have file metadata
                assert result.file_size_bytes is not None
                assert result.last_modified is not None
                assert result.index_path == index_path

                # Should have timing
                assert result.check_duration_ms >= 0
                assert result.from_cache is False
        finally:
            os.unlink(index_path)


class TestHealthCheckCorruptedIndex:
    """Test health check behavior with corrupted HNSW index."""

    def test_corrupted_index_returns_failure_result(self):
        """
        AC #2: Health check on corrupted index returns valid=False with errors array
        containing corruption details.

        This test will FAIL until we implement check_health method with hnswlib integration.
        """
        from code_indexer.services.hnsw_health_service import HNSWHealthService

        service = HNSWHealthService()

        # Create a temporary file to represent index
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            index_path = f.name
            f.write(b"corrupted index data")

        try:
            # Mock hnswlib.Index to simulate corrupted index
            with patch('hnswlib.Index') as mock_index_class:
                mock_index = Mock()
                # check_integrity() returns a dictionary
                mock_index.check_integrity.return_value = {
                    "valid": False,
                    "connections_checked": 500,
                    "element_count": 100,
                    "min_inbound": 0,
                    "max_inbound": 1,
                    "errors": [
                        "Node 42 has broken connections",
                        "Node 87 missing in graph",
                    ],
                }
                mock_index_class.return_value = mock_index

                # Check health
                result = service.check_health(index_path)

                # Should indicate failure
                assert result.valid is False
                assert result.file_exists is True
                assert result.readable is True
                assert result.loadable is True

                # Should have integrity metrics
                assert result.element_count == 100
                assert result.connections_checked == 500
                assert result.min_inbound == 0
                assert result.max_inbound == 1

                # Should have errors from integrity check
                assert len(result.errors) == 2
                assert "Node 42" in result.errors[0]
                assert "Node 87" in result.errors[1]

                # Should have file metadata
                assert result.file_size_bytes is not None
                assert result.last_modified is not None
        finally:
            os.unlink(index_path)


class TestHealthCheckCaching:
    """Test health check caching behavior (TTL and mtime invalidation)."""

    def test_cache_hit_returns_cached_result(self):
        """
        AC #5: Health check with cache hit returns cached result (no integrity check performed).

        This test will FAIL until we implement caching in check_health method.
        """
        from code_indexer.services.hnsw_health_service import HNSWHealthService

        service = HNSWHealthService(cache_ttl_seconds=300)

        # Create a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            index_path = f.name
            f.write(b"index data")

        try:
            # Mock hnswlib.Index
            with patch('hnswlib.Index') as mock_index_class:
                mock_index = Mock()
                # check_integrity() returns a dictionary
                mock_index.check_integrity.return_value = {
                    "valid": True,
                    "connections_checked": 1000,
                    "element_count": 200,
                    "min_inbound": 2,
                    "max_inbound": 8,
                    "errors": [],
                }
                mock_index_class.return_value = mock_index

                # First call - should perform check
                result1 = service.check_health(index_path)
                assert result1.from_cache is False
                assert mock_index.check_integrity.call_count == 1

                # Second call - should return cached result
                result2 = service.check_health(index_path)
                assert result2.from_cache is True
                assert mock_index.check_integrity.call_count == 1  # Not called again

                # Results should be identical (except from_cache flag)
                assert result2.valid == result1.valid
                assert result2.element_count == result1.element_count
                assert result2.connections_checked == result1.connections_checked
        finally:
            os.unlink(index_path)

    def test_cache_invalidation_by_mtime(self):
        """
        AC #6: Health check with cache invalidation by mtime (file modified) performs fresh check.

        This test will FAIL until we implement mtime-based cache invalidation.
        """
        from code_indexer.services.hnsw_health_service import HNSWHealthService

        service = HNSWHealthService(cache_ttl_seconds=300)

        # Create a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            index_path = f.name
            f.write(b"index data v1")

        try:
            # Mock hnswlib.Index
            with patch('hnswlib.Index') as mock_index_class:
                mock_index = Mock()
                # check_integrity() returns a dictionary
                mock_index.check_integrity.return_value = {
                    "valid": True,
                    "connections_checked": 1000,
                    "element_count": 200,
                    "min_inbound": 2,
                    "max_inbound": 8,
                    "errors": [],
                }
                mock_index_class.return_value = mock_index

                # First call
                result1 = service.check_health(index_path)
                assert result1.from_cache is False
                first_mtime = result1.last_modified

                # Modify the file (change mtime)
                time.sleep(0.01)  # Ensure different mtime
                with open(index_path, 'ab') as f:
                    f.write(b"more data")

                # Second call - should detect mtime change and refresh
                result2 = service.check_health(index_path)
                assert result2.from_cache is False  # Fresh check due to mtime change
                assert result2.last_modified != first_mtime
                assert mock_index.check_integrity.call_count == 2  # Called again
        finally:
            os.unlink(index_path)

    def test_cache_invalidation_by_ttl(self):
        """
        AC #7: Health check with cache invalidation by TTL performs fresh check.

        This test will FAIL until we implement TTL-based cache invalidation.
        """
        from code_indexer.services.hnsw_health_service import HNSWHealthService

        # Use very short TTL for testing
        service = HNSWHealthService(cache_ttl_seconds=1)

        # Create a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            index_path = f.name
            f.write(b"index data")

        try:
            # Mock hnswlib.Index
            with patch('hnswlib.Index') as mock_index_class:
                mock_index = Mock()
                # check_integrity() returns a dictionary
                mock_index.check_integrity.return_value = {
                    "valid": True,
                    "connections_checked": 1000,
                    "element_count": 200,
                    "min_inbound": 2,
                    "max_inbound": 8,
                    "errors": [],
                }
                mock_index_class.return_value = mock_index

                # First call
                result1 = service.check_health(index_path)
                assert result1.from_cache is False

                # Wait for TTL to expire
                time.sleep(1.1)

                # Second call - should detect TTL expiry and refresh
                result2 = service.check_health(index_path)
                assert result2.from_cache is False  # Fresh check due to TTL expiry
                assert mock_index.check_integrity.call_count == 2  # Called again
        finally:
            os.unlink(index_path)

    def test_force_refresh_bypasses_cache(self):
        """
        Test that force_refresh parameter bypasses cache.

        This test will FAIL until we implement force_refresh parameter.
        """
        from code_indexer.services.hnsw_health_service import HNSWHealthService

        service = HNSWHealthService(cache_ttl_seconds=300)

        # Create a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            index_path = f.name
            f.write(b"index data")

        try:
            # Mock hnswlib.Index
            with patch('hnswlib.Index') as mock_index_class:
                mock_index = Mock()
                # check_integrity() returns a dictionary
                mock_index.check_integrity.return_value = {
                    "valid": True,
                    "connections_checked": 1000,
                    "element_count": 200,
                    "min_inbound": 2,
                    "max_inbound": 8,
                    "errors": [],
                }
                mock_index_class.return_value = mock_index

                # First call - populate cache
                result1 = service.check_health(index_path)
                assert result1.from_cache is False

                # Second call with force_refresh - should bypass cache
                result2 = service.check_health(index_path, force_refresh=True)
                assert result2.from_cache is False
                assert mock_index.check_integrity.call_count == 2  # Called again
        finally:
            os.unlink(index_path)


class TestHealthCheckAsync:
    """Test async wrapper for health check."""

    @pytest.mark.asyncio
    async def test_async_wrapper_executes_in_executor(self):
        """
        Test that check_health_async executes sync check_health in thread pool executor.

        This test will FAIL until we implement check_health_async function.
        """
        from concurrent.futures import ThreadPoolExecutor
        from code_indexer.services.hnsw_health_service import (
            HNSWHealthService,
            check_health_async,
        )

        service = HNSWHealthService()
        executor = ThreadPoolExecutor(max_workers=2)

        # Create a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            index_path = f.name
            f.write(b"index data")

        try:
            # Mock hnswlib.Index
            with patch('hnswlib.Index') as mock_index_class:
                mock_index = Mock()
                # check_integrity() returns a dictionary
                mock_index.check_integrity.return_value = {
                    "valid": True,
                    "connections_checked": 1000,
                    "element_count": 200,
                    "min_inbound": 2,
                    "max_inbound": 8,
                    "errors": [],
                }
                mock_index_class.return_value = mock_index

                # Call async wrapper
                result = await check_health_async(service, index_path, executor)

                # Should return HealthCheckResult
                assert result.valid is True
                assert result.element_count == 200
                assert mock_index.check_integrity.called
        finally:
            os.unlink(index_path)
            executor.shutdown(wait=True)
