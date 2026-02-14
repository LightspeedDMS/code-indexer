"""
Unit tests for MCPSelfRegistrationService (Story #203).

Tests the auto-registration of CIDX server as an MCP server in Claude Code
configuration, ensuring Claude CLI explorations can leverage CIDX tools.
"""

import base64
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest


@pytest.fixture
def mock_config_manager():
    """Mock ServerConfigManager with config store."""
    from code_indexer.server.utils.config_manager import MCPSelfRegistrationConfig

    manager = MagicMock()
    manager.config_file_path = Path("/home/test/.cidx-server/config.json")

    # Mock load_config to return a config with port and mcp_self_registration
    mock_config = MagicMock()
    mock_config.port = 8000
    mock_config.mcp_self_registration = MCPSelfRegistrationConfig()
    manager.load_config.return_value = mock_config

    return manager


@pytest.fixture
def mock_mcp_credential_manager():
    """Mock MCPCredentialManager."""
    manager = MagicMock()
    return manager


@pytest.fixture
def service(mock_config_manager, mock_mcp_credential_manager):
    """Create MCPSelfRegistrationService instance with mocked dependencies."""
    from code_indexer.server.services.mcp_self_registration_service import (
        MCPSelfRegistrationService,
    )

    return MCPSelfRegistrationService(
        config_manager=mock_config_manager,
        mcp_credential_manager=mock_mcp_credential_manager,
    )


class TestEnsureRegistered:
    """Tests for ensure_registered() method."""

    def test_fast_path_when_already_checked(self, service):
        """AC1: Fast-path skip when _registration_checked is True."""
        # Set flag to simulate prior successful registration
        service._registration_checked = True

        # Should return True immediately without any subprocess calls
        with patch("subprocess.run") as mock_run:
            result = service.ensure_registered()

            assert result is True
            mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_full_registration_flow(
        self, mock_run, service, mock_mcp_credential_manager, mock_config_manager
    ):
        """AC1: Full registration flow when not registered."""
        from code_indexer.server.utils.config_manager import MCPSelfRegistrationConfig

        # Setup: Claude CLI available
        mock_run.side_effect = [
            Mock(returncode=0),  # claude --version succeeds
            Mock(returncode=1),  # claude mcp get cidx-local fails (not registered)
            Mock(returncode=0),  # claude mcp add succeeds
        ]

        # Setup: Credential generation
        mock_mcp_credential_manager.generate_credential.return_value = {
            "credential_id": "cred-123",
            "client_id": "mcp_abc123",
            "client_secret": "mcp_sec_xyz789",
            "created_at": "2026-02-13T10:00:00Z",
            "name": "cidx-local-auto",
        }

        # Setup: Config manager with no stored credentials (Story #203 Finding 1: empty dataclass)
        mock_config = MagicMock()
        mock_config.port = 8000
        mock_config.mcp_self_registration = MCPSelfRegistrationConfig()
        mock_config_manager.load_config.return_value = mock_config

        result = service.ensure_registered()

        assert result is True
        assert service._registration_checked is True

        # Verify credential was generated
        mock_mcp_credential_manager.generate_credential.assert_called_once_with(
            user_id="admin", name="cidx-local-auto"
        )

        # Verify config was saved
        assert mock_config_manager.save_config.called


class TestClaudeCliAvailable:
    """Tests for claude_cli_available() method."""

    @patch("subprocess.run")
    def test_returns_false_when_cli_not_installed(self, mock_run, service):
        """AC3: Returns False when Claude CLI not installed."""
        mock_run.return_value = Mock(returncode=1)

        result = service.claude_cli_available()

        assert result is False
        mock_run.assert_called_once()
        assert "claude" in mock_run.call_args[0][0]
        assert "--version" in mock_run.call_args[0][0]

    @patch("subprocess.run")
    def test_returns_true_when_cli_installed(self, mock_run, service):
        """AC4: Returns True when Claude CLI installed."""
        mock_run.return_value = Mock(returncode=0)

        result = service.claude_cli_available()

        assert result is True


class TestIsAlreadyRegistered:
    """Tests for is_already_registered() method."""

    @patch("subprocess.run")
    def test_detects_existing_registration(self, mock_run, service):
        """AC5: Detects existing registration."""
        mock_run.return_value = Mock(returncode=0)

        result = service.is_already_registered()

        assert result is True
        mock_run.assert_called_once()
        # Verify correct command
        args = mock_run.call_args[0][0]
        assert args == ["claude", "mcp", "get", "cidx-local"]

    @patch("subprocess.run")
    def test_detects_missing_registration(self, mock_run, service):
        """AC6: Detects missing registration."""
        mock_run.return_value = Mock(returncode=1)

        result = service.is_already_registered()

        assert result is False


class TestGetOrCreateCredentials:
    """Tests for get_or_create_credentials() method."""

    def test_reuses_stored_valid_credentials(
        self, service, mock_mcp_credential_manager, mock_config_manager
    ):
        """AC7: Reuses stored valid credentials."""
        from code_indexer.server.utils.config_manager import MCPSelfRegistrationConfig

        # Setup: Stored credentials in config (Story #203 Finding 1: use dataclass)
        mock_config = MagicMock()
        mock_config.port = 8000
        mock_config.mcp_self_registration = MCPSelfRegistrationConfig(
            client_id="mcp_stored123",
            client_secret="mcp_sec_stored456",
        )
        mock_config_manager.load_config.return_value = mock_config

        # Setup: Credential still valid in manager
        mock_mcp_credential_manager.get_credential_by_client_id.return_value = (
            "admin",
            {"client_id": "mcp_stored123"},
        )

        result = service.get_or_create_credentials()

        assert result == {
            "client_id": "mcp_stored123",
            "client_secret": "mcp_sec_stored456",
        }

        # Should NOT generate new credential
        mock_mcp_credential_manager.generate_credential.assert_not_called()

    def test_creates_new_when_no_stored_credentials(
        self, service, mock_mcp_credential_manager, mock_config_manager
    ):
        """AC8: Creates new when no stored credentials."""
        from code_indexer.server.utils.config_manager import MCPSelfRegistrationConfig

        # Setup: No stored credentials (Story #203 Finding 1: empty dataclass)
        mock_config = MagicMock()
        mock_config.port = 8000
        mock_config.mcp_self_registration = MCPSelfRegistrationConfig()
        mock_config_manager.load_config.return_value = mock_config

        # Setup: Generate new credential
        mock_mcp_credential_manager.generate_credential.return_value = {
            "credential_id": "cred-new",
            "client_id": "mcp_new123",
            "client_secret": "mcp_sec_new456",
            "created_at": "2026-02-13T10:00:00Z",
            "name": "cidx-local-auto",
        }

        result = service.get_or_create_credentials()

        assert result["client_id"] == "mcp_new123"
        assert result["client_secret"] == "mcp_sec_new456"

        # Verify credential was generated
        mock_mcp_credential_manager.generate_credential.assert_called_once_with(
            user_id="admin", name="cidx-local-auto"
        )

    def test_recreates_when_stored_credential_invalidated(
        self, service, mock_mcp_credential_manager, mock_config_manager
    ):
        """AC9: Recreates when stored credential invalidated."""
        from code_indexer.server.utils.config_manager import MCPSelfRegistrationConfig

        # Setup: Stored credentials exist (Story #203 Finding 1: use dataclass)
        mock_config = MagicMock()
        mock_config.port = 8000
        mock_config.mcp_self_registration = MCPSelfRegistrationConfig(
            client_id="mcp_old123",
            client_secret="mcp_sec_old456",
        )
        mock_config_manager.load_config.return_value = mock_config

        # Setup: Credential no longer valid in manager
        mock_mcp_credential_manager.get_credential_by_client_id.return_value = None

        # Setup: Generate new credential
        mock_mcp_credential_manager.generate_credential.return_value = {
            "credential_id": "cred-replacement",
            "client_id": "mcp_replacement123",
            "client_secret": "mcp_sec_replacement456",
            "created_at": "2026-02-13T10:00:00Z",
            "name": "cidx-local-auto",
        }

        result = service.get_or_create_credentials()

        assert result["client_id"] == "mcp_replacement123"
        assert result["client_secret"] == "mcp_sec_replacement456"

        # Verify new credential was generated
        mock_mcp_credential_manager.generate_credential.assert_called_once()


class TestRegisterInClaudeCode:
    """Tests for register_in_claude_code() method."""

    @patch("subprocess.run")
    def test_constructs_correct_subprocess_command(
        self, mock_run, service, mock_config_manager
    ):
        """AC10: Constructs correct subprocess command."""
        mock_run.return_value = Mock(returncode=0)

        # Setup config with port
        mock_config = MagicMock()
        mock_config.port = 8000
        mock_config_manager.load_config.return_value = mock_config

        creds = {
            "client_id": "mcp_test123",
            "client_secret": "mcp_sec_test456",
        }

        result = service.register_in_claude_code(creds)

        assert result is True

        # Verify subprocess command
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]

        # Verify command structure
        assert args[0] == "claude"
        assert args[1] == "mcp"
        assert args[2] == "add"
        assert "--transport" in args
        assert "http" in args
        assert "--header" in args

        # Verify Basic auth header format
        expected_auth = base64.b64encode(
            b"mcp_test123:mcp_sec_test456"
        ).decode("ascii")
        assert f"Authorization: Basic {expected_auth}" in args

        # Verify scope and name
        assert "--scope" in args
        assert "user" in args
        assert "cidx-local" in args

        # Verify URL
        assert "http://localhost:8000/mcp" in args

    @patch("subprocess.run")
    def test_handles_subprocess_failure(self, mock_run, service, mock_config_manager):
        """AC11: Handles subprocess failure."""
        mock_run.return_value = Mock(returncode=1)

        mock_config = MagicMock()
        mock_config.port = 8000
        mock_config_manager.load_config.return_value = mock_config

        creds = {
            "client_id": "mcp_test123",
            "client_secret": "mcp_sec_test456",
        }

        result = service.register_in_claude_code(creds)

        assert result is False


class TestGracefulDegradation:
    """Tests for graceful degradation scenarios."""

    @patch("subprocess.run")
    def test_graceful_failure_when_cli_unavailable(self, mock_run, service):
        """AC12: Warning logged and False returned when Claude CLI unavailable."""
        # Claude CLI not available
        mock_run.return_value = Mock(returncode=1)

        result = service.ensure_registered()

        assert result is False
        # Story #203 Finding 4: Flag should NOT be set on CLI unavailable to allow retry
        assert service._registration_checked is False

    @patch("subprocess.run")
    def test_graceful_failure_when_mcp_add_fails(
        self, mock_run, service, mock_mcp_credential_manager, mock_config_manager
    ):
        """AC13: Warning logged and False returned when claude mcp add fails."""
        # Setup: CLI available, not registered, but mcp add fails
        mock_run.side_effect = [
            Mock(returncode=0),  # claude --version succeeds
            Mock(returncode=1),  # claude mcp get fails (not registered)
            Mock(returncode=1),  # claude mcp add FAILS
        ]

        # Setup credential generation
        mock_mcp_credential_manager.generate_credential.return_value = {
            "credential_id": "cred-123",
            "client_id": "mcp_abc",
            "client_secret": "mcp_sec_xyz",
            "created_at": "2026-02-13T10:00:00Z",
            "name": "cidx-local-auto",
        }

        mock_config = MagicMock()
        mock_config.port = 8000
        mock_config_manager.load_config.return_value = mock_config

        result = service.ensure_registered()

        assert result is False
