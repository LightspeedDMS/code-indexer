"""Tests for CLI remote command base utilities - Story #735.

Tests the remote error handling and client factory utilities.
"""

import pytest
from unittest.mock import patch


class TestRemoteErrorHandling:
    """Tests for remote command error handling utilities."""

    def test_handle_remote_error_network_connection(self):
        """Test network connection errors are formatted correctly."""
        from code_indexer.cli_utils.remote_command_base import handle_remote_error
        from code_indexer.api_clients.network_error_handler import (
            NetworkConnectionError,
        )

        error = NetworkConnectionError("Connection refused")
        result = handle_remote_error(error, verbose=False)

        assert "connection" in result.lower() or "network" in result.lower()

    def test_handle_remote_error_auth_suggests_login(self):
        """Test auth errors suggest running cidx auth login."""
        from code_indexer.cli_utils.remote_command_base import handle_remote_error
        from code_indexer.api_clients.base_client import AuthenticationError

        error = AuthenticationError("Token expired")
        result = handle_remote_error(error, verbose=False)

        assert "auth" in result.lower() or "login" in result.lower()

    def test_handle_remote_error_api_shows_message(self):
        """Test API errors show the error message."""
        from code_indexer.cli_utils.remote_command_base import handle_remote_error
        from code_indexer.api_clients.base_client import APIClientError

        error = APIClientError("Resource not found", status_code=404)
        result = handle_remote_error(error, verbose=False)

        assert "not found" in result.lower() or "404" in result

    def test_handle_remote_error_verbose_mode(self):
        """Test verbose mode includes additional details."""
        from code_indexer.cli_utils.remote_command_base import handle_remote_error
        from code_indexer.api_clients.base_client import APIClientError

        error = APIClientError("Resource not found", status_code=404)
        result_verbose = handle_remote_error(error, verbose=True)
        result_normal = handle_remote_error(error, verbose=False)

        # Verbose should have at least as much info
        assert len(result_verbose) >= len(result_normal)

    def test_handle_remote_error_timeout(self):
        """Test timeout errors are handled correctly."""
        from code_indexer.cli_utils.remote_command_base import handle_remote_error
        from code_indexer.api_clients.network_error_handler import NetworkTimeoutError

        error = NetworkTimeoutError("Request timed out")
        result = handle_remote_error(error, verbose=False)

        assert "timeout" in result.lower() or "timed out" in result.lower()

    def test_handle_remote_error_generic_exception(self):
        """Test generic exceptions are handled gracefully."""
        from code_indexer.cli_utils.remote_command_base import handle_remote_error

        error = Exception("Something unexpected happened")
        result = handle_remote_error(error, verbose=False)

        assert "unexpected" in result.lower() or "error" in result.lower()


class TestGetRemoteClient:
    """Tests for get_remote_client factory function."""

    def test_get_remote_client_repos_domain(self):
        """Test getting repos domain client."""
        from code_indexer.cli_utils.remote_command_base import get_remote_client

        with patch(
            "code_indexer.cli_utils.remote_command_base._load_remote_config"
        ) as mock_config:
            mock_config.return_value = {
                "server_url": "https://test.example.com",
                "credentials": {"username": "test", "password": "test"},
            }

            client = get_remote_client("repos")
            assert client is not None

    def test_get_remote_client_jobs_domain(self):
        """Test getting jobs domain client."""
        from code_indexer.cli_utils.remote_command_base import get_remote_client

        with patch(
            "code_indexer.cli_utils.remote_command_base._load_remote_config"
        ) as mock_config:
            mock_config.return_value = {
                "server_url": "https://test.example.com",
                "credentials": {"username": "test", "password": "test"},
            }

            client = get_remote_client("jobs")
            assert client is not None

    def test_get_remote_client_admin_domain(self):
        """Test getting admin domain client."""
        from code_indexer.cli_utils.remote_command_base import get_remote_client

        with patch(
            "code_indexer.cli_utils.remote_command_base._load_remote_config"
        ) as mock_config:
            mock_config.return_value = {
                "server_url": "https://test.example.com",
                "credentials": {"username": "test", "password": "test"},
            }

            client = get_remote_client("admin")
            assert client is not None

    def test_get_remote_client_unknown_domain_raises(self):
        """Test unknown domain raises ValueError."""
        from code_indexer.cli_utils.remote_command_base import get_remote_client

        with patch(
            "code_indexer.cli_utils.remote_command_base._load_remote_config"
        ) as mock_config:
            mock_config.return_value = {
                "server_url": "https://test.example.com",
                "credentials": {"username": "test", "password": "test"},
            }

            with pytest.raises(ValueError, match="Unknown domain"):
                get_remote_client("unknown_domain")
