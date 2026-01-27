"""Tests for OIDCProvider HTTP call handling."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from code_indexer.server.auth.oidc.oidc_provider import OIDCProvider
from code_indexer.server.utils.config_manager import OIDCProviderConfig


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

    @pytest.mark.asyncio
    async def test_get_user_info_does_not_await_json(self):
        """Test that get_user_info calls response.json() without await."""
        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="http://localhost:8180/realms/test",
            client_id="test-client",
            client_secret="test-secret",
        )
        provider = OIDCProvider(config)

        # Mock httpx.AsyncClient
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "sub": "test-user-id",
            "email": "test@example.com",
            "email_verified": True,
        }

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            user_info = await provider.get_user_info("test-access-token")

            # Verify json() was called (not awaited)
            mock_response.json.assert_called_once()
            assert user_info.subject == "test-user-id"
            assert user_info.email == "test@example.com"

    @pytest.mark.asyncio
    async def test_get_user_info_extracts_groups_from_claim(self):
        """Test that get_user_info extracts groups from the configured groups_claim."""
        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="http://localhost:8180/realms/test",
            client_id="test-client",
            client_secret="test-secret",
            groups_claim="groups",  # Default groups claim
        )
        provider = OIDCProvider(config)

        # Mock httpx.AsyncClient with groups in response
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "sub": "test-user-id",
            "email": "test@example.com",
            "email_verified": True,
            "groups": ["SSOAdmins", "SSODevelopers"],  # Groups from SSO provider
        }

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            user_info = await provider.get_user_info("test-access-token")

            # Verify groups were extracted
            assert user_info.groups is not None
            assert len(user_info.groups) == 2
            assert "SSOAdmins" in user_info.groups
            assert "SSODevelopers" in user_info.groups

    @pytest.mark.asyncio
    async def test_get_user_info_handles_missing_groups_claim(self):
        """Test that get_user_info handles missing groups claim gracefully."""
        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="http://localhost:8180/realms/test",
            client_id="test-client",
            client_secret="test-secret",
            groups_claim="groups",
        )
        provider = OIDCProvider(config)

        # Mock response without groups claim
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "sub": "test-user-id",
            "email": "test@example.com",
            "email_verified": True,
            # No groups claim
        }

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            user_info = await provider.get_user_info("test-access-token")

            # Groups should be None (not an empty list, as per implementation)
            assert user_info.groups is None

    @pytest.mark.asyncio
    async def test_get_user_info_handles_non_list_groups_claim(self):
        """Test that get_user_info handles non-list groups claim gracefully."""
        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="http://localhost:8180/realms/test",
            client_id="test-client",
            client_secret="test-secret",
            groups_claim="groups",
        )
        provider = OIDCProvider(config)

        # Mock response with non-list groups claim (string instead of list)
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "sub": "test-user-id",
            "email": "test@example.com",
            "email_verified": True,
            "groups": "single-group-string",  # Not a list
        }

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            user_info = await provider.get_user_info("test-access-token")

            # Groups should be None when claim value is not a list
            assert user_info.groups is None

    @pytest.mark.asyncio
    async def test_get_user_info_custom_groups_claim(self):
        """Test that get_user_info uses custom groups_claim setting."""
        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="http://localhost:8180/realms/test",
            client_id="test-client",
            client_secret="test-secret",
            groups_claim="roles",  # Custom claim name
        )
        provider = OIDCProvider(config)

        # Mock response with groups in "roles" claim
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "sub": "test-user-id",
            "email": "test@example.com",
            "email_verified": True,
            "roles": ["Admin", "Developer"],  # Groups in custom "roles" claim
        }

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            user_info = await provider.get_user_info("test-access-token")

            # Verify groups were extracted from custom "roles" claim
            assert user_info.groups is not None
            assert len(user_info.groups) == 2
            assert "Admin" in user_info.groups
            assert "Developer" in user_info.groups
