"""
Unit tests for build_codex_mcp_auth_header_provider.

v9.23.10: Codex MCP authentication now uses persistent MCPCredentialManager-issued
Basic auth credentials (same path as Claude). No JWT, no TTL.

The provider calls MCPSelfRegistrationService.get_cached_auth_header_value() first.
If that returns a non-None string, it is returned directly (cached path — service
already completed Claude registration). If None (cache miss — server has not yet
registered with Claude in this process), the provider falls back to calling
get_or_create_credentials() and building the header from the returned creds dict
via the service's build_auth_header_from_creds() method. If credentials are also
None, RuntimeError is raised (Foundation 13: no silent failure).

Test inventory (6 tests across 3 classes):

  TestAuthHeaderProviderBuildsSuccessfully (1 test)
    test_closure_builds_without_raising

  TestAuthHeaderProviderValue (4 tests)
    test_returned_string_starts_with_basic
    test_base64_portion_decodes_to_client_id_colon_client_secret
    test_none_credentials_raises_runtime_error
    test_fallback_to_credentials_when_cache_miss

  TestAuthHeaderProviderIdempotency (1 test)
    test_two_calls_return_same_string
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FAKE_CLIENT_ID = "test-client-id-abc"
_FAKE_CLIENT_SECRET = "test-client-secret-xyz"
_FAKE_CREDS = {"client_id": _FAKE_CLIENT_ID, "client_secret": _FAKE_CLIENT_SECRET}
# Pre-formed Basic auth header value (as the service would cache it after Claude registration)
_FAKE_B64 = base64.b64encode(
    f"{_FAKE_CLIENT_ID}:{_FAKE_CLIENT_SECRET}".encode()
).decode()
_FAKE_BASIC_HEADER = f"Basic {_FAKE_B64}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_mcp_service(creds, cached_header=None):
    """Return a mock MCPSelfRegistrationService.

    Args:
        creds: Return value for get_or_create_credentials().
        cached_header: Return value for get_cached_auth_header_value(). When None,
            the service has not yet completed Claude registration (cache miss path).
    """
    mock_service = MagicMock()
    mock_service.get_or_create_credentials.return_value = creds
    mock_service.get_cached_auth_header_value.return_value = cached_header
    mock_service.build_auth_header_from_creds.return_value = (
        _FAKE_BASIC_HEADER if creds else None
    )
    return mock_service


# ---------------------------------------------------------------------------
# Tests: closure builds without raising
# ---------------------------------------------------------------------------


class TestAuthHeaderProviderBuildsSuccessfully:
    """build_codex_mcp_auth_header_provider() constructs and returns a callable."""

    def test_closure_builds_without_raising(self):
        """
        build_codex_mcp_auth_header_provider() must return a callable without
        raising, even before the closure is invoked.
        """
        from code_indexer.server.services.codex_mcp_auth_header_provider import (
            build_codex_mcp_auth_header_provider,
        )

        provider = build_codex_mcp_auth_header_provider()
        assert callable(provider), (
            f"build_codex_mcp_auth_header_provider() must return a callable; "
            f"got {type(provider)!r}"
        )


# ---------------------------------------------------------------------------
# Tests: header value format
# ---------------------------------------------------------------------------


class TestAuthHeaderProviderValue:
    """The closure returns a correctly formatted 'Basic <b64>' header value."""

    def test_returned_string_starts_with_basic(self):
        """
        When called with a mocked service returning a cached header,
        the provider must return a string starting with 'Basic '.
        """
        from code_indexer.server.services.codex_mcp_auth_header_provider import (
            build_codex_mcp_auth_header_provider,
        )

        mock_service = _make_mock_mcp_service(
            creds=_FAKE_CREDS, cached_header=_FAKE_BASIC_HEADER
        )

        with patch(
            "code_indexer.server.services.codex_mcp_auth_header_provider.MCPSelfRegistrationService"
        ) as mock_cls:
            mock_cls.get_instance.return_value = mock_service
            provider = build_codex_mcp_auth_header_provider()
            result = provider()

        assert result.startswith("Basic "), (
            f"Provider must return a string starting with 'Basic '; got {result!r}"
        )

    def test_base64_portion_decodes_to_client_id_colon_client_secret(self):
        """
        The base64 portion after 'Basic ' must decode to '<client_id>:<client_secret>'.
        Uses the cached header path (service already registered with Claude).
        """
        from code_indexer.server.services.codex_mcp_auth_header_provider import (
            build_codex_mcp_auth_header_provider,
        )

        mock_service = _make_mock_mcp_service(
            creds=_FAKE_CREDS, cached_header=_FAKE_BASIC_HEADER
        )

        with patch(
            "code_indexer.server.services.codex_mcp_auth_header_provider.MCPSelfRegistrationService"
        ) as mock_cls:
            mock_cls.get_instance.return_value = mock_service
            provider = build_codex_mcp_auth_header_provider()
            result = provider()

        assert result.startswith("Basic "), f"Expected 'Basic ' prefix; got {result!r}"
        b64_part = result[len("Basic ") :]
        decoded = base64.b64decode(b64_part).decode("ascii")
        expected = f"{_FAKE_CLIENT_ID}:{_FAKE_CLIENT_SECRET}"
        assert decoded == expected, (
            f"Base64 portion must decode to {expected!r}; got {decoded!r}"
        )

    def test_none_credentials_raises_runtime_error(self):
        """
        When the service has no cached header AND get_or_create_credentials()
        returns None, the provider must raise RuntimeError (Foundation 13: no
        silent failure — broken credential state must surface immediately).
        """
        import pytest

        from code_indexer.server.services.codex_mcp_auth_header_provider import (
            build_codex_mcp_auth_header_provider,
        )

        # No cached header, no credentials
        mock_service = _make_mock_mcp_service(creds=None, cached_header=None)

        with patch(
            "code_indexer.server.services.codex_mcp_auth_header_provider.MCPSelfRegistrationService"
        ) as mock_cls:
            mock_cls.get_instance.return_value = mock_service
            provider = build_codex_mcp_auth_header_provider()
            with pytest.raises(RuntimeError, match="credentials"):
                provider()

    def test_fallback_to_credentials_when_cache_miss(self):
        """
        When get_cached_auth_header_value() returns None (cache miss — Claude not
        yet registered in this process), the provider must fall back to calling
        get_or_create_credentials() and build the header via build_auth_header_from_creds().
        The returned value must match the expected 'Basic <b64>' string.
        """
        from code_indexer.server.services.codex_mcp_auth_header_provider import (
            build_codex_mcp_auth_header_provider,
        )

        # Cache miss but credentials available
        mock_service = _make_mock_mcp_service(creds=_FAKE_CREDS, cached_header=None)

        with patch(
            "code_indexer.server.services.codex_mcp_auth_header_provider.MCPSelfRegistrationService"
        ) as mock_cls:
            mock_cls.get_instance.return_value = mock_service
            provider = build_codex_mcp_auth_header_provider()
            result = provider()

        # Must have used the fallback path: get_cached (returned None), then build_auth_header
        mock_service.get_cached_auth_header_value.assert_called()
        mock_service.build_auth_header_from_creds.assert_called()
        assert result == _FAKE_BASIC_HEADER, (
            f"Fallback path must return {_FAKE_BASIC_HEADER!r}; got {result!r}"
        )


# ---------------------------------------------------------------------------
# Tests: idempotency (persistent credentials, not fresh-per-call)
# ---------------------------------------------------------------------------


class TestAuthHeaderProviderIdempotency:
    """Two successive calls to the provider return the same string (persistent creds)."""

    def test_two_calls_return_same_string(self):
        """
        MCPCredentialManager-issued credentials are persistent. Two successive calls
        to the same provider closure must return the SAME 'Basic <b64>' string.
        This confirms no per-call re-minting (unlike the old JWT approach).
        """
        from code_indexer.server.services.codex_mcp_auth_header_provider import (
            build_codex_mcp_auth_header_provider,
        )

        mock_service = _make_mock_mcp_service(
            creds=_FAKE_CREDS, cached_header=_FAKE_BASIC_HEADER
        )

        with patch(
            "code_indexer.server.services.codex_mcp_auth_header_provider.MCPSelfRegistrationService"
        ) as mock_cls:
            mock_cls.get_instance.return_value = mock_service
            provider = build_codex_mcp_auth_header_provider()
            result_a = provider()
            result_b = provider()

        assert result_a == result_b, (
            f"Two successive provider calls must return the same string "
            f"(persistent MCPCredentialManager creds); "
            f"got {result_a!r} vs {result_b!r}"
        )
