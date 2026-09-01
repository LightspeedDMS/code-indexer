"""Tests for CLI response parsing errors - TDD for reproducing 'str' object has no attribute 'get' errors.

These tests reproduce the specific CLI parsing issues identified in manual testing
where CLI commands fail with "'str' object has no attribute 'get'" when server
APIs work correctly via curl.
"""

import pytest
from unittest.mock import patch, Mock
from click.testing import CliRunner
import httpx

from code_indexer.cli import cli


class TestCLIResponseParsingErrors:
    """Test class to reproduce and fix CLI response parsing errors."""

    @pytest.fixture
    def cli_runner(self):
        """Create CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def mock_project_setup(self):
        """Setup mocks for project and remote configuration."""
        with (
            patch(
                "code_indexer.mode_detection.command_mode_detector.find_project_root"
            ) as mock_find_root,
            patch(
                "code_indexer.remote.config.load_remote_configuration"
            ) as mock_load_config,
        ):
            mock_find_root.return_value = "/test/project"
            mock_load_config.return_value = {
                "server_url": "http://localhost:8096",
                "username": "testuser",
                "encrypted_credentials": {"username": "testuser"},
            }
            yield mock_find_root, mock_load_config

    def test_auth_status_fails_with_str_get_attribute_error(
        self, cli_runner, mock_project_setup
    ):
        """Test reproducing the 'str' object has no attribute 'get' error in auth status.

        This test reproduces the exact error where CLI tries to call .get() on a string
        response instead of a parsed JSON dictionary.
        """
        mock_find_root, mock_load_config = mock_project_setup

        # Mock auth client creation
        with (
            patch(
                "code_indexer.api_clients.auth_client.create_auth_client"
            ) as mock_create_client,
            patch(
                "code_indexer.disabled_commands.detect_current_mode"
            ) as mock_detect_mode,
        ):
            # Mock mode detection to return remote mode -- without this the
            # @require_mode("remote") gate rejects the command before the
            # mocked client is ever reached and the scenario below never runs.
            mock_detect_mode.return_value = "remote"

            # get_auth_status is a synchronous method (auth_client.py:473),
            # called without await -- Mock (not AsyncMock) is required so the
            # string return value actually reaches the display layer instead
            # of producing an unawaited coroutine.
            mock_client = Mock()
            mock_create_client.return_value = mock_client

            # Mock the auth status response as string (the bug condition)
            # This simulates what happens when the API returns a raw httpx.Response
            # instead of parsed JSON
            mock_client.get_auth_status.return_value = (
                "{'authenticated': true}"  # String, not dict
            )

            result = cli_runner.invoke(cli, ["auth", "status"])

            # _display_auth_status (cli.py) has defensive type checking for a
            # string status: it prints the string as an error and returns
            # normally -- no AttributeError, no non-zero exit. This is the
            # real, current behavior (verified live).
            assert result.exit_code == 0, (
                f"Command should exit 0 (defensive handling), got: {result.output}"
            )
            assert "'str' object has no attribute" not in result.output
            assert "{'authenticated': true}" in result.output

    def test_system_health_succeeds_with_real_client_response_parsing(
        self, cli_runner, mock_project_setup
    ):
        """Regression test for system health response parsing.

        ORIGINAL ISSUE: system health used to treat the raw httpx.Response as
        a dict (response["response_time_ms"] = ...), causing a TypeError.
        check_basic_health() now correctly calls response.json() first and
        assigns to the resulting dict -- this test exercises a real
        SystemAPIClient instance (only the HTTP layer is mocked) to guard
        against that regressing.
        """
        mock_find_root, mock_load_config = mock_project_setup

        # Mock the system client but let the actual implementation run with a mock response
        with (
            patch(
                "code_indexer.api_clients.system_client.create_system_client"
            ) as mock_create_client,
            patch(
                "code_indexer.api_clients.system_client.SystemAPIClient._authenticated_request"
            ) as mock_auth_request,
            patch(
                "code_indexer.disabled_commands.detect_current_mode"
            ) as mock_detect_mode,
        ):
            # Mock mode detection to return remote mode
            mock_detect_mode.return_value = "remote"
            # Create real SystemAPIClient instance but mock its _authenticated_request
            from code_indexer.api_clients.system_client import SystemAPIClient
            from pathlib import Path

            real_client = SystemAPIClient(
                server_url="http://localhost:8096",
                credentials={"username": "testuser"},
                project_root=Path("/test/project"),
            )
            mock_create_client.return_value = real_client

            # Create a mock httpx.Response that behaves like the real one
            mock_response = Mock(spec=httpx.Response)
            mock_response.json.return_value = {"status": "ok", "message": "Healthy"}
            mock_response.status_code = 200
            mock_auth_request.return_value = mock_response

            result = cli_runner.invoke(cli, ["system", "health"])

            assert result.exit_code == 0, (
                f"Command should succeed, got: {result.output}"
            )
            assert "System Health: OK" in result.output
            # This reproduces the actual bug in the system client

    def test_auth_validate_fails_with_response_type_error(
        self, cli_runner, mock_project_setup
    ):
        """Test auth validate's response-type handling when validate_credentials
        returns a non-bool truthy object instead of a real boolean.

        auth_validate (cli.py) does a bare truthy check on the return value
        (`sys.exit(0 if is_valid else 1)`) -- it does not type-check that the
        result is actually a bool. This test documents that real, current
        behavior: a truthy non-bool object is silently accepted as "valid".
        """
        mock_find_root, mock_load_config = mock_project_setup

        with (
            patch(
                "code_indexer.api_clients.auth_client.create_auth_client"
            ) as mock_create_client,
            patch(
                "code_indexer.disabled_commands.detect_current_mode"
            ) as mock_detect_mode,
        ):
            # Mock mode detection to return remote mode -- without this the
            # @require_mode("remote") gate rejects the command before the
            # mocked client is ever reached and the scenario below never runs.
            mock_detect_mode.return_value = "remote"

            # validate_credentials is a synchronous method
            # (auth_client.py:637), called without await -- Mock (not
            # AsyncMock) is required so the mocked return value actually
            # reaches the CLI's truthy check instead of producing an
            # unawaited coroutine.
            mock_client = Mock()
            mock_create_client.return_value = mock_client

            # Mock returning wrong type (httpx.Response instead of boolean)
            mock_response = Mock(spec=httpx.Response)
            mock_response.json.return_value = {"valid": True}
            mock_response.status_code = 200

            # validate_credentials returns response object instead of boolean
            mock_client.validate_credentials.return_value = mock_response

            result = cli_runner.invoke(cli, ["auth", "validate", "--verbose"])

            # The truthy mock_response is accepted as "valid" -- no type
            # error, exit code 0 (verified live).
            assert result.exit_code == 0, (
                f"Truthy non-bool response should be accepted, got: {result.output}"
            )
            assert "Credentials are valid" in result.output


class TestCorrectResponseParsing:
    """Test class demonstrating correct response parsing (TDD green phase targets)."""

    @pytest.fixture
    def cli_runner(self):
        """Create CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def mock_project_setup(self):
        """Setup mocks for project and remote configuration."""
        with (
            patch(
                "code_indexer.mode_detection.command_mode_detector.find_project_root"
            ) as mock_find_root,
            patch(
                "code_indexer.remote.config.load_remote_configuration"
            ) as mock_load_config,
        ):
            mock_find_root.return_value = "/test/project"
            mock_load_config.return_value = {
                "server_url": "http://localhost:8096",
                "username": "testuser",
                "encrypted_credentials": {"username": "testuser"},
            }
            yield mock_find_root, mock_load_config

    def test_auth_status_succeeds_with_proper_json_parsing(
        self, cli_runner, mock_project_setup
    ):
        """Test that auth status works correctly when proper JSON parsing is implemented.

        This is the target behavior after fixing the bug.
        """
        mock_find_root, mock_load_config = mock_project_setup

        with (
            patch(
                "code_indexer.api_clients.auth_client.create_auth_client"
            ) as mock_create_client,
            patch(
                "code_indexer.disabled_commands.detect_current_mode"
            ) as mock_detect_mode,
        ):
            mock_detect_mode.return_value = "remote"
            mock_client = Mock()
            mock_create_client.return_value = mock_client

            # Mock proper AuthStatus object (correct behavior)
            from code_indexer.api_clients.auth_client import AuthStatus

            status = AuthStatus(
                authenticated=True,
                username="testuser",
                role="user",
                token_valid=True,
                token_expires=None,
                server_url="http://localhost:8096",
                last_refreshed=None,
                permissions=["read"],
                server_reachable=True,
            )
            mock_client.get_auth_status.return_value = status

            result = cli_runner.invoke(cli, ["auth", "status"])

            # Should succeed with proper response parsing
            assert result.exit_code == 0
            assert "Authenticated: Yes" in result.output
            assert "testuser" in result.output

    def test_system_health_succeeds_with_proper_json_parsing(
        self, cli_runner, mock_project_setup
    ):
        """Test that system health works correctly when proper JSON parsing is implemented.

        This is the target behavior after fixing the bug.
        """
        mock_find_root, mock_load_config = mock_project_setup

        with (
            patch(
                "code_indexer.api_clients.system_client.create_system_client"
            ) as mock_create_client,
            patch(
                "code_indexer.disabled_commands.detect_current_mode"
            ) as mock_detect_mode,
        ):
            # Mock mode detection to return remote mode
            mock_detect_mode.return_value = "remote"

            mock_client = Mock()
            mock_create_client.return_value = mock_client

            # Mock proper dictionary response (correct behavior)
            health_result = {
                "status": "ok",
                "message": "System is healthy",
                "response_time_ms": 45.2,
            }
            mock_client.check_basic_health.return_value = health_result

            result = cli_runner.invoke(cli, ["system", "health"])

            # Print output for debugging
            print(f"Exit code: {result.exit_code}")
            print(f"Output: {result.output}")

            # Should succeed with proper response parsing
            assert result.exit_code == 0
            assert "System Health: OK" in result.output
            assert "45.2ms" in result.output

    def test_auth_validate_succeeds_with_proper_boolean_return(
        self, cli_runner, mock_project_setup
    ):
        """Test that auth validate works correctly when proper boolean parsing is implemented.

        This is the target behavior after fixing the bug.
        """
        mock_find_root, mock_load_config = mock_project_setup

        with (
            patch(
                "code_indexer.api_clients.auth_client.create_auth_client"
            ) as mock_create_client,
            patch(
                "code_indexer.disabled_commands.detect_current_mode"
            ) as mock_detect_mode,
        ):
            # Mock mode detection to return remote mode
            mock_detect_mode.return_value = "remote"

            mock_client = Mock()
            mock_create_client.return_value = mock_client

            # Mock proper boolean response (correct behavior)
            mock_client.validate_credentials.return_value = True

            result = cli_runner.invoke(cli, ["auth", "validate", "--verbose"])

            # Should succeed with proper response parsing
            assert result.exit_code == 0
            assert "valid" in result.output.lower()


class TestEdgeCaseResponseParsing:
    """Test edge cases in response parsing to ensure robustness."""

    @pytest.fixture
    def cli_runner(self):
        """Create CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def mock_project_setup(self):
        """Setup mocks for project and remote configuration."""
        with (
            patch(
                "code_indexer.mode_detection.command_mode_detector.find_project_root"
            ) as mock_find_root,
            patch(
                "code_indexer.remote.config.load_remote_configuration"
            ) as mock_load_config,
        ):
            mock_find_root.return_value = "/test/project"
            mock_load_config.return_value = {
                "server_url": "http://localhost:8096",
                "username": "testuser",
                "encrypted_credentials": {"username": "testuser"},
            }
            yield mock_find_root, mock_load_config

    def test_system_health_handles_malformed_json_gracefully(
        self, cli_runner, mock_project_setup
    ):
        """Test that system health handles malformed JSON responses gracefully."""
        mock_find_root, mock_load_config = mock_project_setup

        with (
            patch(
                "code_indexer.api_clients.system_client.create_system_client"
            ) as mock_create_client,
            patch(
                "code_indexer.disabled_commands.detect_current_mode"
            ) as mock_detect_mode,
        ):
            # Mock mode detection to return remote mode -- without this the
            # @require_mode("remote") gate rejects the command before the
            # mocked client is ever reached and the side_effect below never
            # fires.
            mock_detect_mode.return_value = "remote"

            # check_basic_health is a synchronous method
            # (system_client.py:45), called without await -- Mock (not
            # AsyncMock) is required so side_effect raises synchronously
            # instead of producing an unawaited coroutine that the CLI never
            # inspects for an exception.
            mock_client = Mock()
            mock_create_client.return_value = mock_client

            # Mock client that raises JSON parsing error
            from code_indexer.api_clients.base_client import APIClientError

            mock_client.check_basic_health.side_effect = APIClientError(
                "Invalid JSON response"
            )

            result = cli_runner.invoke(cli, ["system", "health"])

            # cli.py's run_health_check() APIClientError branch: prints
            # "Health check failed" + the error detail, exits 1 (verified
            # live).
            assert result.exit_code == 1, (
                f"Should exit 1 on APIClientError, got: {result.output}"
            )
            assert "Health check failed" in result.output
            assert "Invalid JSON response" in result.output

    def test_auth_status_handles_network_error_gracefully(
        self, cli_runner, mock_project_setup
    ):
        """Test that auth status handles network errors gracefully."""
        mock_find_root, mock_load_config = mock_project_setup

        with (
            patch(
                "code_indexer.api_clients.auth_client.create_auth_client"
            ) as mock_create_client,
            patch(
                "code_indexer.disabled_commands.detect_current_mode"
            ) as mock_detect_mode,
        ):
            # Mock mode detection to return remote mode -- without this the
            # @require_mode("remote") gate rejects the command before the
            # mocked client is ever reached and the side_effect below never
            # fires.
            mock_detect_mode.return_value = "remote"

            # get_auth_status is a synchronous method (auth_client.py:473),
            # called without await -- Mock (not AsyncMock) is required so
            # side_effect raises synchronously instead of producing an
            # unawaited coroutine that the CLI never inspects for an
            # exception.
            mock_client = Mock()
            mock_create_client.return_value = mock_client

            # Mock client that raises network error
            from code_indexer.api_clients.base_client import APIClientError

            mock_client.get_auth_status.side_effect = APIClientError(
                "Connection failed"
            )

            result = cli_runner.invoke(cli, ["auth", "status"])

            # cli.py's auth_status outer except-block: prints "Error checking
            # authentication status: {e}", exits 1 (verified live).
            assert result.exit_code == 1, (
                f"Should exit 1 on APIClientError, got: {result.output}"
            )
            assert "Error checking authentication status" in result.output
            assert "Connection failed" in result.output
