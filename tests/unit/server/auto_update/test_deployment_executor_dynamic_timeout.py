"""Unit tests for DeploymentExecutor dynamic drain timeout (Bug #135).

Bug #135: Auto-update drain timeout must be dynamically calculated from server config.

Tests that DeploymentExecutor queries /api/admin/maintenance/drain-timeout endpoint
to get recommended drain timeout instead of using hardcoded value.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile


class TestDeploymentExecutorDynamicTimeout:
    """Test DeploymentExecutor uses dynamic timeout from server API."""

    def test_get_drain_timeout_queries_server_endpoint(self):
        """_get_drain_timeout should query /api/admin/maintenance/drain-timeout."""
        from code_indexer.server.auto_update.deployment_executor import (
            DeploymentExecutor,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            executor = DeploymentExecutor(
                repo_path=Path(tmpdir),
                server_url="http://localhost:8000",
            )

            with patch("requests.get") as mock_get:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "max_job_timeout_seconds": 3600,
                    "recommended_drain_timeout_seconds": 5400,
                }
                mock_get.return_value = mock_response

                timeout = executor._get_drain_timeout()

                # Should return recommended timeout from API
                assert timeout == 5400

                # Verify correct endpoint was called
                mock_get.assert_called_once()
                call_url = mock_get.call_args[0][0]
                assert "/api/admin/maintenance/drain-timeout" in call_url

    def test_get_drain_timeout_uses_fallback_on_connection_error(self):
        """_get_drain_timeout should use fallback timeout if server unreachable."""
        from code_indexer.server.auto_update.deployment_executor import (
            DeploymentExecutor,
        )
        import requests

        with tempfile.TemporaryDirectory() as tmpdir:
            executor = DeploymentExecutor(
                repo_path=Path(tmpdir),
                server_url="http://localhost:8000",
            )

            with patch("requests.get") as mock_get:
                mock_get.side_effect = requests.exceptions.ConnectionError()

                timeout = executor._get_drain_timeout()

                # Should return fallback (2 hours = 7200 seconds)
                assert timeout == 7200

    def test_get_drain_timeout_uses_fallback_on_http_error(self):
        """_get_drain_timeout should use fallback if server returns error."""
        from code_indexer.server.auto_update.deployment_executor import (
            DeploymentExecutor,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            executor = DeploymentExecutor(
                repo_path=Path(tmpdir),
                server_url="http://localhost:8000",
            )

            with patch("requests.get") as mock_get:
                mock_response = MagicMock()
                mock_response.status_code = 500
                mock_get.return_value = mock_response

                timeout = executor._get_drain_timeout()

                # Should return fallback (2 hours = 7200 seconds)
                assert timeout == 7200

    def test_get_drain_timeout_uses_fallback_on_invalid_response(self):
        """_get_drain_timeout should use fallback if response format invalid."""
        from code_indexer.server.auto_update.deployment_executor import (
            DeploymentExecutor,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            executor = DeploymentExecutor(
                repo_path=Path(tmpdir),
                server_url="http://localhost:8000",
            )

            with patch("requests.get") as mock_get:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"invalid": "data"}
                mock_get.return_value = mock_response

                timeout = executor._get_drain_timeout()

                # Should return fallback (2 hours = 7200 seconds)
                assert timeout == 7200

    def test_wait_for_drain_uses_dynamic_timeout(self):
        """_wait_for_drain should use timeout from _get_drain_timeout."""
        from code_indexer.server.auto_update.deployment_executor import (
            DeploymentExecutor,
        )
        import time

        with tempfile.TemporaryDirectory() as tmpdir:
            # Use shorter poll interval for faster test
            executor = DeploymentExecutor(
                repo_path=Path(tmpdir),
                server_url="http://localhost:8000",
                drain_poll_interval=0.1,  # 100ms for fast test
            )

            # Mock _get_drain_timeout to return known value
            with patch.object(executor, "_get_drain_timeout") as mock_get_timeout, \
                 patch("requests.get") as mock_get:
                mock_get_timeout.return_value = 1  # 1 second timeout

                # Mock drain-status to show not drained
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"drained": False}
                mock_get.return_value = mock_response

                # Wait should timeout after approximately 1 second
                start = time.time()
                result = executor._wait_for_drain()
                elapsed = time.time() - start

                # Should timeout (return False)
                assert result is False
                # Should take approximately 1 second (with tolerance for poll intervals)
                assert 0.9 < elapsed < 1.5

                # Verify dynamic timeout was fetched
                mock_get_timeout.assert_called_once()

    def test_restart_server_fetches_dynamic_timeout(self):
        """restart_server should fetch dynamic timeout before drain wait."""
        from code_indexer.server.auto_update.deployment_executor import (
            DeploymentExecutor,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            executor = DeploymentExecutor(
                repo_path=Path(tmpdir),
                server_url="http://localhost:8000",
            )

            with patch.object(executor, "_enter_maintenance_mode") as mock_enter, \
                 patch.object(executor, "_get_drain_timeout") as mock_get_timeout, \
                 patch("requests.get") as mock_get, \
                 patch("subprocess.run") as mock_run:
                mock_enter.return_value = True
                mock_get_timeout.return_value = 5400

                # Mock drain-status to show already drained
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"drained": True}
                mock_get.return_value = mock_response

                mock_run.return_value = MagicMock(returncode=0)

                result = executor.restart_server()

                assert result is True
                # Verify dynamic timeout was fetched by _wait_for_drain
                mock_get_timeout.assert_called_once()
