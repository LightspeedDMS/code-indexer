"""Tests for OIDC provider error handling and validation."""

import pytest
from unittest.mock import Mock, patch
import httpx


class TestOIDCProviderErrorHandling:
    """Test error handling in OIDC provider methods."""

    @pytest.mark.asyncio
    async def test_discover_metadata_handles_http_404(self):
        """Test that discover_metadata handles 404 errors gracefully."""
        from code_indexer.server.auth.oidc.oidc_provider import OIDCProvider
        from code_indexer.server.utils.config_manager import OIDCProviderConfig

        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="https://nonexistent.example.com",
            client_id="test-client",
            client_secret="test-secret",
        )

        provider = OIDCProvider(config)

        # Mock HTTP client to return 404
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"
        mock_response.raise_for_status = Mock(
            side_effect=httpx.HTTPStatusError(
                "404 Not Found", request=Mock(), response=mock_response
            )
        )

        async def mock_get(*args, **kwargs):
            return mock_response

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = mock_client.return_value.__aenter__.return_value
            mock_instance.get = mock_get

            with pytest.raises(Exception) as exc_info:
                await provider.discover_metadata()

            # Should raise an exception with helpful error message
            error_msg = str(exc_info.value).lower()
            assert (
                "404" in error_msg
                or "not found" in error_msg
                or "discovery" in error_msg
            )

    @pytest.mark.asyncio
    async def test_discover_metadata_handles_network_error(self):
        """Test that discover_metadata handles network errors gracefully."""
        from code_indexer.server.auth.oidc.oidc_provider import OIDCProvider
        from code_indexer.server.utils.config_manager import OIDCProviderConfig

        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="https://unreachable.example.com",
            client_id="test-client",
            client_secret="test-secret",
        )

        provider = OIDCProvider(config)

        # Mock HTTP client to raise network error
        async def mock_get(*args, **kwargs):
            raise httpx.ConnectError("Connection refused")

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = mock_client.return_value.__aenter__.return_value
            mock_instance.get = mock_get

            with pytest.raises(Exception) as exc_info:
                await provider.discover_metadata()

            # Should raise an exception mentioning network/connection
            error_msg = str(exc_info.value).lower()
            assert (
                "connect" in error_msg
                or "unreachable" in error_msg
                or "failed" in error_msg
            )

    @pytest.mark.asyncio
    async def test_exchange_code_for_token_handles_invalid_code(self):
        """Test that exchange_code_for_token handles invalid authorization code."""
        from code_indexer.server.auth.oidc.oidc_provider import (
            OIDCProvider,
            OIDCMetadata,
        )
        from code_indexer.server.utils.config_manager import OIDCProviderConfig

        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="https://example.com",
            client_id="test-client",
            client_secret="test-secret",
        )

        provider = OIDCProvider(config)
        provider._metadata = OIDCMetadata(
            issuer="https://example.com",
            authorization_endpoint="https://example.com/auth",
            token_endpoint="https://example.com/token",
        )

        # Mock HTTP client to return 400 error
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.text = '{"error": "invalid_grant"}'
        mock_response.raise_for_status = Mock(
            side_effect=httpx.HTTPStatusError(
                "400 Bad Request", request=Mock(), response=mock_response
            )
        )

        async def mock_post(*args, **kwargs):
            return mock_response

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = mock_client.return_value.__aenter__.return_value
            mock_instance.post = mock_post

            with pytest.raises(Exception) as exc_info:
                await provider.exchange_code_for_token(
                    "invalid_code", "verifier", "http://callback"
                )

            # Should raise exception with error details
            error_msg = str(exc_info.value).lower()
            assert "400" in error_msg or "invalid" in error_msg or "token" in error_msg

    def test_get_user_info_handles_invalid_id_token_format(self):
        """Test that get_user_info handles invalid/malformed ID token.

        NOTE: get_user_info is now a sync function that parses JWT tokens directly
        (no HTTP calls). It should raise an exception for malformed tokens.
        """
        from code_indexer.server.auth.oidc.oidc_provider import (
            OIDCProvider,
            OIDCMetadata,
        )
        from code_indexer.server.utils.config_manager import OIDCProviderConfig

        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="https://example.com",
            client_id="test-client",
            client_secret="test-secret",
        )

        provider = OIDCProvider(config)
        provider._metadata = OIDCMetadata(
            issuer="https://example.com",
            authorization_endpoint="https://example.com/auth",
            token_endpoint="https://example.com/token",
            userinfo_endpoint="https://example.com/userinfo",
        )

        # Test with malformed ID token (not a valid JWT - should have 3 parts)
        with pytest.raises(Exception) as exc_info:
            provider.get_user_info("access_token", "invalid-token-not-jwt")

        # Should raise exception about invalid token format
        error_msg = str(exc_info.value).lower()
        assert "id token" in error_msg or "invalid" in error_msg or "parse" in error_msg

    @pytest.mark.asyncio
    async def test_exchange_code_validates_token_response(self):
        """Test that exchange_code_for_token validates token response has access_token."""
        from code_indexer.server.auth.oidc.oidc_provider import (
            OIDCProvider,
            OIDCMetadata,
        )
        from code_indexer.server.utils.config_manager import OIDCProviderConfig

        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="https://example.com",
            client_id="test-client",
            client_secret="test-secret",
        )

        provider = OIDCProvider(config)
        provider._metadata = OIDCMetadata(
            issuer="https://example.com",
            authorization_endpoint="https://example.com/auth",
            token_endpoint="https://example.com/token",
        )

        # Mock HTTP client to return token response without access_token
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json = Mock(
            return_value={"token_type": "Bearer"}
        )  # Missing access_token
        mock_response.raise_for_status = Mock()

        async def mock_post(*args, **kwargs):
            return mock_response

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = mock_client.return_value.__aenter__.return_value
            mock_instance.post = mock_post

            with pytest.raises(Exception) as exc_info:
                await provider.exchange_code_for_token(
                    "code", "verifier", "http://callback"
                )

            # Should raise exception about missing access_token
            error_msg = str(exc_info.value).lower()
            assert (
                "access_token" in error_msg
                or "invalid" in error_msg
                or "missing" in error_msg
            )

    def test_get_user_info_validates_id_token_has_sub_claim(self):
        """Test that get_user_info validates ID token has required sub claim.

        NOTE: get_user_info is now a sync function that parses JWT tokens directly.
        It should raise an exception when the 'sub' (subject) claim is missing.
        """
        import base64
        import json

        from code_indexer.server.auth.oidc.oidc_provider import (
            OIDCProvider,
            OIDCMetadata,
        )
        from code_indexer.server.utils.config_manager import OIDCProviderConfig

        config = OIDCProviderConfig(
            enabled=True,
            issuer_url="https://example.com",
            client_id="test-client",
            client_secret="test-secret",
        )

        provider = OIDCProvider(config)
        provider._metadata = OIDCMetadata(
            issuer="https://example.com",
            authorization_endpoint="https://example.com/auth",
            token_endpoint="https://example.com/token",
            userinfo_endpoint="https://example.com/userinfo",
        )

        # Create a mock ID token WITHOUT 'sub' claim
        header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).decode().rstrip("=")
        payload = base64.urlsafe_b64encode(json.dumps({
            "email": "user@example.com",
            "email_verified": True,
            # Missing 'sub' claim
        }).encode()).decode().rstrip("=")
        signature = "fake_signature"
        id_token_without_sub = f"{header}.{payload}.{signature}"

        with pytest.raises(Exception) as exc_info:
            provider.get_user_info("valid_access_token", id_token_without_sub)

        # Should raise exception about missing sub claim
        error_msg = str(exc_info.value).lower()
        assert (
            "sub" in error_msg
            or "subject" in error_msg
            or "missing" in error_msg
            or "invalid" in error_msg
        )
