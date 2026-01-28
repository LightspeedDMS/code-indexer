"""Tests for OIDC username_claim extraction and JIT provisioning."""

import pytest
import base64
import json


def create_mock_id_token(claims):
    """Create a mock ID token JWT for testing.

    Args:
        claims: Dictionary of claims to include in the token

    Returns:
        JWT string (header.payload.signature)
    """
    # Create header (standard JWT header)
    header = {"alg": "RS256", "typ": "JWT"}
    header_b64 = base64.urlsafe_b64encode(
        json.dumps(header).encode()
    ).decode().rstrip("=")

    # Create payload with claims
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(claims).encode()
    ).decode().rstrip("=")

    # Signature is not validated in tests, so just use placeholder
    signature = "mock-signature"

    return f"{header_b64}.{payload_b64}.{signature}"


class TestUsernameClaimExtraction:
    """Test username extraction from OIDC userinfo using username_claim."""

    def test_extracts_username_using_default_claim(self, monkeypatch):
        """Test that username is extracted using default username_claim (preferred_username).

        NOTE: get_user_info is a sync function (no async I/O - just JWT parsing).
        """
        from code_indexer.server.auth.oidc.oidc_provider import (
            OIDCProvider,
            OIDCMetadata,
        )
        from code_indexer.server.utils.config_manager import OIDCProviderConfig

        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="https://example.com",
            username_claim="preferred_username",  # Default
        )

        provider = OIDCProvider(config)
        provider._metadata = OIDCMetadata(
            issuer="https://example.com",
            authorization_endpoint="https://example.com/authorize",
            token_endpoint="https://example.com/token",
        )

        # Create ID token with preferred_username claim
        claims = {
            "sub": "oidc-user-12345",
            "email": "user@example.com",
            "email_verified": True,
            "preferred_username": "jdoe",
        }
        id_token = create_mock_id_token(claims)

        user_info = provider.get_user_info("test-access-token", id_token)

        assert user_info.subject == "oidc-user-12345"
        assert user_info.email == "user@example.com"
        assert user_info.username == "jdoe"

    def test_extracts_username_using_custom_claim(self, monkeypatch):
        """Test that username is extracted using custom username_claim."""
        from code_indexer.server.auth.oidc.oidc_provider import (
            OIDCProvider,
            OIDCMetadata,
        )
        from code_indexer.server.utils.config_manager import OIDCProviderConfig

        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="https://example.com",
            username_claim="custom_username",  # Custom claim
        )

        provider = OIDCProvider(config)
        provider._metadata = OIDCMetadata(
            issuer="https://example.com",
            authorization_endpoint="https://example.com/authorize",
            token_endpoint="https://example.com/token",
        )

        # Create ID token with custom username claim
        claims = {
            "sub": "oidc-user-12345",
            "email": "user@example.com",
            "email_verified": True,
            "custom_username": "custom_user",
        }
        id_token = create_mock_id_token(claims)

        user_info = provider.get_user_info("test-access-token", id_token)

        assert user_info.username == "custom_user"

    def test_username_is_none_when_claim_not_present(self, monkeypatch):
        """Test that username is None when username_claim is not in ID token."""
        from code_indexer.server.auth.oidc.oidc_provider import (
            OIDCProvider,
            OIDCMetadata,
        )
        from code_indexer.server.utils.config_manager import OIDCProviderConfig

        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="https://example.com",
            username_claim="preferred_username",
        )

        provider = OIDCProvider(config)
        provider._metadata = OIDCMetadata(
            issuer="https://example.com",
            authorization_endpoint="https://example.com/authorize",
            token_endpoint="https://example.com/token",
        )

        # Create ID token WITHOUT preferred_username
        claims = {
            "sub": "oidc-user-12345",
            "email": "user@example.com",
            "email_verified": True,
        }
        id_token = create_mock_id_token(claims)

        user_info = provider.get_user_info("test-access-token", id_token)

        assert user_info.username is None


class TestJITProvisioningWithUsernameClaim:
    """Test JIT provisioning using username_claim."""

    @pytest.mark.asyncio
    async def test_jit_creates_user_with_username_from_claim(self, tmp_path):
        """Test that JIT provisioning creates user with username from username_claim."""
        from code_indexer.server.auth.oidc.oidc_manager import OIDCManager
        from code_indexer.server.auth.oidc.oidc_provider import OIDCUserInfo
        from code_indexer.server.auth.user_manager import User, UserRole
        from code_indexer.server.utils.config_manager import OIDCProviderConfig
        from datetime import datetime, timezone
        from unittest.mock import Mock

        config = OIDCProviderConfig(
            enabled=True,
            enable_jit_provisioning=True,
            default_role="normal_user",
            username_claim="preferred_username",
        )

        # Mock user_manager
        user_manager = Mock()
        user_manager.get_user.return_value = None  # No existing user with this username
        user_manager.get_user_by_email.return_value = None  # No existing user by email

        # Mock create_oidc_user to return a new user
        new_user = User(
            username="jdoe",
            password_hash="",
            role=UserRole.NORMAL_USER,
            created_at=datetime.now(timezone.utc),
        )
        user_manager.create_oidc_user.return_value = new_user

        manager = OIDCManager(config, user_manager, None)
        manager.db_path = str(tmp_path / "test_oidc.db")
        await manager._init_db()

        # User info with username from username_claim
        user_info = OIDCUserInfo(
            subject="new-subject-123",
            email="jdoe@example.com",
            email_verified=True,
            username="jdoe",  # From username_claim
        )

        user = await manager.match_or_create_user(user_info)

        assert user.username == "jdoe"
        # Verify create_oidc_user was called with username from claim
        user_manager.create_oidc_user.assert_called_once()
        call_args = user_manager.create_oidc_user.call_args
        assert call_args.kwargs["username"] == "jdoe"

    @pytest.mark.asyncio
    async def test_jit_fails_when_username_claim_not_in_userinfo(self, tmp_path):
        """Test that JIT provisioning fails when username_claim is not in userinfo."""
        from code_indexer.server.auth.oidc.oidc_manager import OIDCManager
        from code_indexer.server.auth.oidc.oidc_provider import OIDCUserInfo
        from code_indexer.server.utils.config_manager import OIDCProviderConfig
        from unittest.mock import Mock

        config = OIDCProviderConfig(
            enabled=True,
            enable_jit_provisioning=True,
            default_role="normal_user",
            username_claim="preferred_username",
        )

        user_manager = Mock()
        user_manager.get_user_by_email.return_value = None

        manager = OIDCManager(config, user_manager, None)
        manager.db_path = str(tmp_path / "test_oidc.db")
        await manager._init_db()

        # User info WITHOUT username (claim not in userinfo)
        user_info = OIDCUserInfo(
            subject="new-subject-123",
            email="jdoe@example.com",
            email_verified=True,
            username=None,  # Missing!
        )

        user = await manager.match_or_create_user(user_info)

        # Should fail and return None
        assert user is None
        # Should NOT call create_oidc_user
        user_manager.create_oidc_user.assert_not_called()

    @pytest.mark.asyncio
    async def test_jit_fails_when_username_already_exists(self, tmp_path):
        """Test that JIT provisioning fails when username already exists (collision)."""
        from code_indexer.server.auth.oidc.oidc_manager import OIDCManager
        from code_indexer.server.auth.oidc.oidc_provider import OIDCUserInfo
        from code_indexer.server.auth.user_manager import User, UserRole
        from code_indexer.server.utils.config_manager import OIDCProviderConfig
        from datetime import datetime, timezone
        from unittest.mock import Mock

        config = OIDCProviderConfig(
            enabled=True,
            enable_jit_provisioning=True,
            default_role="normal_user",
            username_claim="preferred_username",
        )

        # Mock user_manager with existing user
        user_manager = Mock()
        existing_user = User(
            username="jdoe",  # This username already exists
            password_hash="hashed_password",
            role=UserRole.NORMAL_USER,
            created_at=datetime.now(timezone.utc),
        )
        user_manager.get_user.return_value = existing_user  # Username collision!
        user_manager.get_user_by_email.return_value = None  # Email doesn't match

        manager = OIDCManager(config, user_manager, None)
        manager.db_path = str(tmp_path / "test_oidc.db")
        await manager._init_db()

        # User info with username that already exists
        user_info = OIDCUserInfo(
            subject="new-subject-123",
            email="different@example.com",
            email_verified=True,
            username="jdoe",  # Collision!
        )

        user = await manager.match_or_create_user(user_info)

        # Should fail and return None
        assert user is None
        # Should NOT call create_oidc_user
        user_manager.create_oidc_user.assert_not_called()
