"""Tests for OIDCProvider HTTP call handling."""

import pytest
import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch
from code_indexer.server.auth.oidc.oidc_provider import OIDCProvider
from code_indexer.server.utils.config_manager import OIDCProviderConfig


def create_mock_id_token(claims):
    """Create a mock ID token JWT for testing.

    Args:
        claims: Dictionary of claims to include in the token

    Returns:
        JWT string (header.payload.signature)
    """
    # Create header (standard JWT header)
    header = {"alg": "RS256", "typ": "JWT"}
    header_b64 = (
        base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    )

    # Create payload with claims
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    )

    # Signature is not validated in tests, so just use placeholder
    signature = "mock-signature"

    return f"{header_b64}.{payload_b64}.{signature}"


class TestOIDCProviderHttpCalls:
    """Test that OIDCProvider correctly handles httpx response.json()."""

    @pytest.mark.asyncio
    async def test_exchange_code_for_token_does_not_await_json(self):
        """Test that exchange_code_for_token calls response.json() without await."""
        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="http://localhost:8180/realms/test",
            client_id="test-client",
            client_secret="test-secret",
        )
        provider = OIDCProvider(config)

        # Mock metadata
        provider._metadata = MagicMock()
        provider._metadata.token_endpoint = "http://localhost:8180/token"

        # Mock httpx.AsyncClient
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "test-access-token",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            tokens = await provider.exchange_code_for_token(
                code="test-code",
                code_verifier="test-verifier",
                redirect_uri="http://localhost:8090/callback",
            )

            # Verify json() was called (not awaited)
            mock_response.json.assert_called_once()
            assert tokens["access_token"] == "test-access-token"

    def test_get_user_info_parses_id_token(self):
        """Test that get_user_info parses ID token correctly.

        NOTE: get_user_info is a sync function (no async I/O - just JWT parsing).
        """
        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="http://localhost:8180/realms/test",
            client_id="test-client",
            client_secret="test-secret",
        )
        provider = OIDCProvider(config)

        # Create mock ID token with user claims
        claims = {
            "sub": "test-user-id",
            "email": "test@example.com",
            "email_verified": True,
        }
        id_token = create_mock_id_token(claims)

        user_info = provider.get_user_info("test-access-token", id_token)

        assert user_info.subject == "test-user-id"
        assert user_info.email == "test@example.com"

    def test_get_user_info_extracts_groups_from_claim(self):
        """Test that get_user_info extracts groups from the configured groups_claim."""
        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="http://localhost:8180/realms/test",
            client_id="test-client",
            client_secret="test-secret",
            groups_claim="groups",  # Default groups claim
        )
        provider = OIDCProvider(config)

        # Create ID token with groups claim
        claims = {
            "sub": "test-user-id",
            "email": "test@example.com",
            "email_verified": True,
            "groups": ["SSOAdmins", "SSODevelopers"],  # Groups from SSO provider
        }
        id_token = create_mock_id_token(claims)

        user_info = provider.get_user_info("test-access-token", id_token)

        # Verify groups were extracted
        assert user_info.groups is not None
        assert len(user_info.groups) == 2
        assert "SSOAdmins" in user_info.groups
        assert "SSODevelopers" in user_info.groups

    def test_get_user_info_handles_missing_groups_claim(self):
        """Test that get_user_info handles missing groups claim gracefully."""
        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="http://localhost:8180/realms/test",
            client_id="test-client",
            client_secret="test-secret",
            groups_claim="groups",
        )
        provider = OIDCProvider(config)

        # Create ID token without groups claim
        claims = {
            "sub": "test-user-id",
            "email": "test@example.com",
            "email_verified": True,
            # No groups claim
        }
        id_token = create_mock_id_token(claims)

        user_info = provider.get_user_info("test-access-token", id_token)

        # Groups should be None (not an empty list, as per implementation)
        assert user_info.groups is None

    def test_get_user_info_handles_non_list_groups_claim(self):
        """Test that get_user_info handles non-list groups claim gracefully."""
        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="http://localhost:8180/realms/test",
            client_id="test-client",
            client_secret="test-secret",
            groups_claim="groups",
        )
        provider = OIDCProvider(config)

        # Create ID token with non-list groups claim (string instead of list)
        claims = {
            "sub": "test-user-id",
            "email": "test@example.com",
            "email_verified": True,
            "groups": "single-group-string",  # Not a list
        }
        id_token = create_mock_id_token(claims)

        user_info = provider.get_user_info("test-access-token", id_token)

        # Groups should be None when claim value is not a list
        assert user_info.groups is None

    def test_get_user_info_custom_groups_claim(self):
        """Test that get_user_info uses custom groups_claim setting."""
        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="http://localhost:8180/realms/test",
            client_id="test-client",
            client_secret="test-secret",
            groups_claim="roles",  # Custom claim name
        )
        provider = OIDCProvider(config)

        # Create ID token with groups in "roles" claim
        claims = {
            "sub": "test-user-id",
            "email": "test@example.com",
            "email_verified": True,
            "roles": ["Admin", "Developer"],  # Groups in custom "roles" claim
        }
        id_token = create_mock_id_token(claims)

        user_info = provider.get_user_info("test-access-token", id_token)

        # Verify groups were extracted from custom "roles" claim
        assert user_info.groups is not None
        assert len(user_info.groups) == 2
        assert "Admin" in user_info.groups
        assert "Developer" in user_info.groups
