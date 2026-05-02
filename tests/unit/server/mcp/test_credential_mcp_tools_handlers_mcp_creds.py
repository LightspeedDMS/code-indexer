"""
Unit tests for Credential Management MCP Tools - MCP Credential Handler Tests.

Story #743: Implement 10 new MCP tools for API key and MCP credential management.

TDD Approach: These tests are written FIRST before implementation.

Tests verify MCP credential handler behavior with mocked dependencies.
"""

import contextlib
import pytest
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.mcp.handlers import HANDLER_REGISTRY
from code_indexer.server.auth.user_manager import User, UserRole

# ---------------------------------------------------------------------------
# Elevation bypass helpers (Story #925 AC2)
# create_mcp_credential and delete_mcp_credential are decorated with
# @require_mcp_elevation. Tests that verify handler business logic must
# satisfy the decorator with a real elevation window.
# ---------------------------------------------------------------------------
_ENFORCEMENT_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)
_TOTP_PATH = "code_indexer.server.mcp.auth.elevation_decorator.get_totp_service"
_ESM_PATH = "code_indexer.server.mcp.auth.elevation_decorator.elevated_session_manager"
_TEST_SESSION_KEY = "test-session-mcp-cred-handler-abc"
_IDLE_SECONDS = 300
_MAX_AGE_SECONDS = 1800
_DB_FILENAME = "elev_mcpcred.db"
_ELEV_SCOPE = "full"


@contextlib.contextmanager
def _active_elevation(username: str, tmp_path):
    """Open a real elevation window so decorated handlers pass the gate.

    Uses a real ElevatedSessionManager backed by a temp SQLite DB.
    Only the TOTP external-service boundary is mocked, not internal auth logic.
    """
    mgr = ElevatedSessionManager(
        idle_timeout_seconds=_IDLE_SECONDS,
        max_age_seconds=_MAX_AGE_SECONDS,
        db_path=str(tmp_path / _DB_FILENAME),
    )
    mgr.create(_TEST_SESSION_KEY, username, None, scope=_ELEV_SCOPE)
    totp_mock = MagicMock()
    totp_mock.is_mfa_enabled.return_value = True
    with (
        patch(_ENFORCEMENT_PATH, return_value=True),
        patch(_ESM_PATH, mgr),
        patch(_TOTP_PATH, return_value=totp_mock),
    ):
        yield _TEST_SESSION_KEY


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def normal_user():
    """Create a normal user for testing."""
    return User(
        username="normal_test",
        password_hash="$2b$12$hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )


# =============================================================================
# Handler Functional Tests - list_mcp_credentials
# =============================================================================


class TestListMCPCredentialsHandler:
    """Tests for list_mcp_credentials handler functionality."""

    @pytest.fixture
    def mock_mcp_credential_manager(self):
        """Create mock MCP credential manager for testing."""
        manager = MagicMock()
        manager.get_credentials.return_value = [
            {
                "credential_id": "cred-123",
                "client_id_prefix": "mcp_abcd",
                "name": "Test Credential",
                "created_at": "2024-01-01T00:00:00Z",
            }
        ]
        return manager

    def test_list_mcp_credentials_returns_success_true(
        self, normal_user, mock_mcp_credential_manager
    ):
        """list_mcp_credentials handler returns success=True on valid call."""
        with patch("code_indexer.server.mcp.handlers.dependencies") as mock_deps:
            mock_deps.mcp_credential_manager = mock_mcp_credential_manager

            handler = HANDLER_REGISTRY["list_mcp_credentials"]
            result = handler({}, normal_user)

            assert "content" in result
            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True

    def test_list_mcp_credentials_returns_credentials_array(
        self, normal_user, mock_mcp_credential_manager
    ):
        """list_mcp_credentials handler returns credentials array."""
        with patch("code_indexer.server.mcp.handlers.dependencies") as mock_deps:
            mock_deps.mcp_credential_manager = mock_mcp_credential_manager

            handler = HANDLER_REGISTRY["list_mcp_credentials"]
            result = handler({}, normal_user)

            content = json.loads(result["content"][0]["text"])
            assert "credentials" in content
            assert isinstance(content["credentials"], list)


# =============================================================================
# Handler Functional Tests - create_mcp_credential
# =============================================================================


class TestCreateMCPCredentialHandler:
    """Tests for create_mcp_credential handler functionality."""

    @pytest.fixture
    def mock_mcp_credential_manager(self):
        """Create mock MCP credential manager for testing."""
        manager = MagicMock()
        manager.generate_credential.return_value = {
            "credential_id": "cred-uuid-123",
            "client_id": "mcp_full_client_id",
            "client_secret": "mcp_sec_full_secret_value",
            "client_id_prefix": "mcp_full",
            "name": "Test",
            "created_at": "2024-01-01T00:00:00Z",
        }
        return manager

    def test_create_mcp_credential_returns_success(
        self, normal_user, mock_mcp_credential_manager, tmp_path
    ):
        """create_mcp_credential handler returns success on valid creation."""
        with (
            _active_elevation(normal_user.username, tmp_path),
            patch("code_indexer.server.mcp.handlers.dependencies") as mock_deps,
        ):
            mock_deps.mcp_credential_manager = mock_mcp_credential_manager

            handler = HANDLER_REGISTRY["create_mcp_credential"]
            result = handler(
                {"description": "Test credential"},
                normal_user,
                session_key=_TEST_SESSION_KEY,
            )

            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True

    def test_create_mcp_credential_returns_credential_id(
        self, normal_user, mock_mcp_credential_manager, tmp_path
    ):
        """create_mcp_credential handler returns credential_id."""
        with (
            _active_elevation(normal_user.username, tmp_path),
            patch("code_indexer.server.mcp.handlers.dependencies") as mock_deps,
        ):
            mock_deps.mcp_credential_manager = mock_mcp_credential_manager

            handler = HANDLER_REGISTRY["create_mcp_credential"]
            result = handler({}, normal_user, session_key=_TEST_SESSION_KEY)

            content = json.loads(result["content"][0]["text"])
            assert "credential_id" in content

    def test_create_mcp_credential_returns_full_credential(
        self, normal_user, mock_mcp_credential_manager, tmp_path
    ):
        """create_mcp_credential returns full credential (one-time display)."""
        with (
            _active_elevation(normal_user.username, tmp_path),
            patch("code_indexer.server.mcp.handlers.dependencies") as mock_deps,
        ):
            mock_deps.mcp_credential_manager = mock_mcp_credential_manager

            handler = HANDLER_REGISTRY["create_mcp_credential"]
            result = handler({}, normal_user, session_key=_TEST_SESSION_KEY)

            content = json.loads(result["content"][0]["text"])
            # The credential should contain either 'credential' or 'client_secret'
            assert "credential" in content or "client_secret" in content


# =============================================================================
# Handler Functional Tests - delete_mcp_credential
# =============================================================================


class TestDeleteMCPCredentialHandler:
    """Tests for delete_mcp_credential handler functionality."""

    @pytest.fixture
    def mock_mcp_credential_manager(self):
        """Create mock MCP credential manager for testing."""
        manager = MagicMock()
        manager.revoke_credential.return_value = True
        return manager

    def test_delete_mcp_credential_returns_success(
        self, normal_user, mock_mcp_credential_manager, tmp_path
    ):
        """delete_mcp_credential handler returns success=True on valid deletion."""
        with (
            _active_elevation(normal_user.username, tmp_path),
            patch("code_indexer.server.mcp.handlers.dependencies") as mock_deps,
        ):
            mock_deps.mcp_credential_manager = mock_mcp_credential_manager

            handler = HANDLER_REGISTRY["delete_mcp_credential"]
            result = handler(
                {"credential_id": "cred-123"},
                normal_user,
                session_key=_TEST_SESSION_KEY,
            )

            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True

    def test_delete_mcp_credential_missing_credential_id_fails(
        self, normal_user, tmp_path
    ):
        """delete_mcp_credential fails when credential_id is missing."""
        with _active_elevation(normal_user.username, tmp_path):
            handler = HANDLER_REGISTRY["delete_mcp_credential"]
            result = handler({}, normal_user, session_key=_TEST_SESSION_KEY)

            content = json.loads(result["content"][0]["text"])
            assert content["success"] is False
            assert "error" in content
