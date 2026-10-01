"""Test Messi Rule #2 (Anti-Fallback) Compliance for Remote Query Client.

This test ensures that the RemoteQueryClient adheres to Messi Rule #2:
- No fallbacks with fake data
- Graceful failure with clear error messages
- No unsafe type casting

Note on HTTP transport stubbing: MockIsolationManager.mock_server_response()
(tests/unit/api_clients/test_isolation_utils.py) is a documented placeholder
that does not actually intercept HTTP traffic -- without a real transport
stub, RemoteQueryClient would attempt a genuine network call to the
(non-listening) test port and fail with a connection error rather than
exercising the real get_repository_statistics() response-handling logic
this test suite is meant to validate. Each test therefore configures its
mock response via self._mock_response(), and _authenticated_request is
patched to return a real httpx.Response built from that configuration --
only the network transport boundary is stubbed; the production parsing/
validation/error logic under test still executes for real.
"""

from pathlib import Path
from typing import Any, Dict
from unittest import TestCase
from unittest.mock import patch

import httpx

from code_indexer.api_clients.remote_query_client import (
    RemoteQueryClient,
    RepositoryAccessError,
)
from tests.unit.api_clients.test_isolation_utils import MockIsolationManager


class TestMessiRule2Compliance(TestCase):
    """Test that RemoteQueryClient follows Anti-Fallback Principle."""

    def setUp(self):
        """Set up isolated test environment."""
        self.isolation = MockIsolationManager()
        self.server_config = self.isolation.start_test_server()
        credentials = {
            "username": "test_user",
            "password": "Test123!Pass",
            "server_url": f"http://localhost:{self.server_config['port']}",
        }
        self.client = RemoteQueryClient(
            server_url=f"http://localhost:{self.server_config['port']}",
            credentials=credentials,
        )
        self._endpoint_responses: Dict[str, Dict[str, Any]] = {}
        self._authenticated_request_patcher = patch.object(
            self.client,
            "_authenticated_request",
            side_effect=self._fake_authenticated_request,
        )
        self._authenticated_request_patcher.start()

    def tearDown(self):
        """Clean up test environment."""
        if hasattr(self, "_authenticated_request_patcher"):
            self._authenticated_request_patcher.stop()
        if hasattr(self, "client"):
            self.client.close()
        if hasattr(self, "isolation"):
            self.isolation.cleanup()

    def _mock_response(
        self, endpoint: str, response_data: dict, status_code: int = 200
    ) -> None:
        """Configure the stubbed HTTP transport to answer `endpoint`.

        Delegates to MockIsolationManager.mock_server_response() to preserve
        its existing bookkeeping, then additionally records the response so
        _fake_authenticated_request() can serve it via a real httpx.Response.
        """
        self.isolation.mock_server_response(
            endpoint, response_data, status_code=status_code
        )
        self._endpoint_responses[endpoint] = {
            "data": response_data,
            "status_code": status_code,
        }

    def _fake_authenticated_request(
        self, method: str, endpoint: str, **kwargs: Any
    ) -> httpx.Response:
        """Stand in for the real network call, using the configured mock."""
        config = self._endpoint_responses[endpoint]
        return httpx.Response(
            status_code=config["status_code"],
            json=config["data"],
            request=httpx.Request(method, f"{self.client.server_url}{endpoint}"),
        )

    def test_no_fake_statistics_fallback(self):
        """Test that missing statistics raise error instead of returning fake data."""
        # Mock a response without statistics
        self._mock_response(
            "/api/repositories/test-repo",
            {"name": "test-repo", "path": "/path/to/repo"},
            status_code=200,
        )

        # Should raise error, not return fake data
        with self.assertRaises(RepositoryAccessError) as ctx:
            self.client.get_repository_statistics("test-repo")

        # Verify error message is helpful
        error_msg = str(ctx.exception)
        self.assertIn("statistics not available", error_msg.lower())
        self.assertIn("test-repo", error_msg)

    def test_invalid_statistics_format_error(self):
        """Test that invalid statistics format raises proper error."""
        # Mock a response with invalid statistics type
        self._mock_response(
            "/api/repositories/test-repo",
            {"name": "test-repo", "statistics": "invalid_string_not_dict"},
            status_code=200,
        )

        # Should raise error about invalid format
        with self.assertRaises(ValueError) as ctx:
            self.client.get_repository_statistics("test-repo")

        # Verify error message is helpful
        error_msg = str(ctx.exception)
        self.assertIn("invalid statistics format", error_msg.lower())
        self.assertIn("expected dict", error_msg.lower())
        self.assertIn("got str", error_msg.lower())

    def test_valid_statistics_returned_correctly(self):
        """Test that valid statistics are returned without modification."""
        expected_stats = {
            "total_files": 42,
            "indexed_files": 40,
            "total_size_bytes": 1024000,
            "embeddings_count": 500,
            "languages": ["python", "javascript"],
        }

        # Mock a response with valid statistics
        self._mock_response(
            "/api/repositories/test-repo",
            {"name": "test-repo", "statistics": expected_stats},
            status_code=200,
        )

        # Should return exact statistics
        stats = self.client.get_repository_statistics("test-repo")
        self.assertEqual(stats, expected_stats)

    def test_no_cast_without_validation(self):
        """Test that no unsafe cast() is used in the codebase."""
        # Read the source file to verify no unsafe casts
        source_file = (
            Path(__file__).parent.parent.parent.parent
            / "src/code_indexer/api_clients/remote_query_client.py"
        )
        content = source_file.read_text()

        # Should not have any cast() calls
        self.assertNotIn(
            "cast(", content, "Found unsafe cast() usage in remote_query_client.py"
        )

        # Verify we validate before returning
        self.assertIn(
            "isinstance(stats, dict)", content, "Missing type validation for statistics"
        )

    def test_error_messages_are_actionable(self):
        """Test that error messages provide actionable information."""
        # Test 404 error
        self._mock_response(
            "/api/repositories/nonexistent",
            {"detail": "Repository not found"},
            status_code=404,
        )

        with self.assertRaises(RepositoryAccessError) as ctx:
            self.client.get_repository_statistics("nonexistent")

        error_msg = str(ctx.exception)
        self.assertIn("not found", error_msg.lower())

        # Test 403 error
        self._mock_response(
            "/api/repositories/forbidden",
            {"detail": "Access denied"},
            status_code=403,
        )

        with self.assertRaises(RepositoryAccessError) as ctx:
            self.client.get_repository_statistics("forbidden")

        error_msg = str(ctx.exception)
        self.assertIn("access denied", error_msg.lower())


if __name__ == "__main__":
    import unittest

    unittest.main()
