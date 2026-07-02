"""
Unit tests for bug #937: CodexInvoker spawns without CIDX_MCP_AUTH_HEADER.

Root cause: MCPSelfRegistrationService._cached_auth_header is only populated
by register_in_claude_code(), which requires Claude CLI to be available.
On a staging server where Claude CLI is absent or registration fails,
build_auth_header_from_creds() -> ensure_registered() -> register_in_claude_code()
never runs, so _cached_auth_header stays None. The provider raises RuntimeError
and CodexInvoker logs WARNING and spawns without CIDX_MCP_AUTH_HEADER.

Fix: MCPSelfRegistrationService must expose a method that builds the Basic auth
header directly from stored credentials (config.mcp_self_registration.client_id +
client_secret stored by get_or_create_credentials()) WITHOUT requiring Claude CLI.
The provider closure should use this direct-from-credentials path as a third
fallback before giving up, so Codex MCP auth works independently of Claude CLI.

Additionally, when credentials are genuinely missing (empty store), the provider
must log ERROR (not stay silent) identifying the specific missing entry, per
MESSI Rule 13 (Anti-Silent-Failure).

Test inventory (8 tests across 4 classes):

  TestProviderWithDirectCredentials (3 tests)
    test_provider_succeeds_via_direct_creds_when_claude_cli_unavailable
    test_provider_returns_correct_basic_header_from_stored_credentials
    test_provider_direct_path_does_not_require_ensure_registered

  TestProviderErrorLoggingWhenEmpty (2 tests)
    test_error_logged_when_credentials_missing_not_warning
    test_error_message_identifies_missing_cidx_local_entry

  TestMCPSelfRegistrationDirectHeader (2 tests)
    test_build_header_directly_from_stored_creds_returns_basic_string
    test_build_header_directly_returns_none_when_no_stored_creds

  TestProviderFallbackChain (1 test)
    test_provider_uses_direct_creds_before_raising
"""

from __future__ import annotations

import base64
import logging
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FAKE_CLIENT_ID = "mcp_abcdef1234567890"
_FAKE_CLIENT_SECRET = "mcp_sec_abcdef1234567890abcdef1234567890abcdef"
_EXPECTED_B64 = base64.b64encode(
    f"{_FAKE_CLIENT_ID}:{_FAKE_CLIENT_SECRET}".encode()
).decode()
_EXPECTED_BASIC_HEADER = f"Basic {_EXPECTED_B64}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service_with_stored_creds(
    client_id: str = _FAKE_CLIENT_ID,
    client_secret: str = _FAKE_CLIENT_SECRET,
) -> MagicMock:
    """Return a mock MCPSelfRegistrationService that has stored credentials but no
    cached auth header (simulates Claude CLI unavailable — cache never populated).

    cached_auth_header=None simulates Claude registration never ran in this process
    (Claude CLI absent or registration failed). Stored credentials are available
    because get_or_create_credentials() runs independently of Claude CLI.
    """
    mock_service = MagicMock()
    mock_service.get_cached_auth_header_value.return_value = (
        None  # Claude not registered
    )
    mock_service.build_auth_header_from_creds.return_value = (
        None  # ensure_registered fails
    )

    # The new method the fix adds: builds header directly from stored credentials
    expected_b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    mock_service.build_header_from_stored_credentials.return_value = (
        f"Basic {expected_b64}"
    )
    return mock_service


def _make_service_with_no_creds() -> MagicMock:
    """Return a mock MCPSelfRegistrationService with no stored credentials at all."""
    mock_service = MagicMock()
    mock_service.get_cached_auth_header_value.return_value = None
    mock_service.build_auth_header_from_creds.return_value = None
    mock_service.build_header_from_stored_credentials.return_value = None
    return mock_service


# ---------------------------------------------------------------------------
# Tests: provider succeeds via direct credentials (no Claude CLI needed)
# ---------------------------------------------------------------------------


class TestProviderWithDirectCredentials:
    """Provider returns non-None header via stored credentials even without Claude CLI."""

    def test_provider_succeeds_via_direct_creds_when_claude_cli_unavailable(self):
        """
        Bug #937 core scenario: Claude CLI is unavailable (or registration failed),
        so get_cached_auth_header_value() and build_auth_header_from_creds() both
        return None. The provider must succeed by calling build_header_from_stored_credentials()
        which reads credentials directly from MCPCredentialManager config.

        RED: This test fails before the fix because the third fallback path does not exist.
        """
        from code_indexer.server.services.codex_mcp_auth_header_provider import (
            build_codex_mcp_auth_header_provider,
        )

        mock_service = _make_service_with_stored_creds()

        with patch(
            "code_indexer.server.services.codex_mcp_auth_header_provider.MCPSelfRegistrationService"
        ) as mock_cls:
            mock_cls.get_instance.return_value = mock_service
            provider = build_codex_mcp_auth_header_provider()
            result = provider()

        assert result is not None, (
            "Provider must return a non-None header when stored credentials exist, "
            "even when Claude CLI is unavailable (get_cached returns None, "
            "build_auth_header_from_creds returns None)"
        )
        assert result.startswith("Basic "), (
            f"Provider must return 'Basic <b64>'; got {result!r}"
        )

    def test_provider_returns_correct_basic_header_from_stored_credentials(self):
        """
        The Basic auth header built from stored credentials must encode
        '<client_id>:<client_secret>' in base64 — same format as the Claude path.

        RED: Fails before fix because the direct-credentials path does not exist.
        """
        from code_indexer.server.services.codex_mcp_auth_header_provider import (
            build_codex_mcp_auth_header_provider,
        )

        mock_service = _make_service_with_stored_creds(
            client_id=_FAKE_CLIENT_ID,
            client_secret=_FAKE_CLIENT_SECRET,
        )

        with patch(
            "code_indexer.server.services.codex_mcp_auth_header_provider.MCPSelfRegistrationService"
        ) as mock_cls:
            mock_cls.get_instance.return_value = mock_service
            provider = build_codex_mcp_auth_header_provider()
            result = provider()

        assert result == _EXPECTED_BASIC_HEADER, (
            f"Expected {_EXPECTED_BASIC_HEADER!r}; got {result!r}"
        )

    def test_provider_direct_path_does_not_require_ensure_registered(self):
        """
        When build_header_from_stored_credentials() succeeds, ensure_registered()
        must NOT be called (it invokes Claude CLI which may be unavailable).

        RED: Fails before fix because the direct path does not bypass ensure_registered.
        """
        from code_indexer.server.services.codex_mcp_auth_header_provider import (
            build_codex_mcp_auth_header_provider,
        )

        mock_service = _make_service_with_stored_creds()

        with patch(
            "code_indexer.server.services.codex_mcp_auth_header_provider.MCPSelfRegistrationService"
        ) as mock_cls:
            mock_cls.get_instance.return_value = mock_service
            provider = build_codex_mcp_auth_header_provider()
            provider()

        mock_service.ensure_registered.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: ERROR log when credentials genuinely missing (not WARNING)
# ---------------------------------------------------------------------------


class TestProviderErrorLoggingWhenEmpty:
    """When ALL credential paths return None, provider logs ERROR (not WARNING)."""

    def test_error_logged_when_credentials_missing_not_warning(self, caplog):
        """
        When no credentials exist (empty store, fresh install misconfiguration),
        the provider must log at ERROR level (MESSI Rule 13: Anti-Silent-Failure).
        Today it raises RuntimeError which CodexInvoker catches and logs WARNING.
        The provider itself must emit ERROR identifying the root cause.

        RED: Fails before fix because the provider raises without logging ERROR itself.
        """
        from code_indexer.server.services.codex_mcp_auth_header_provider import (
            build_codex_mcp_auth_header_provider,
        )

        mock_service = _make_service_with_no_creds()

        with patch(
            "code_indexer.server.services.codex_mcp_auth_header_provider.MCPSelfRegistrationService"
        ) as mock_cls:
            mock_cls.get_instance.return_value = mock_service
            provider = build_codex_mcp_auth_header_provider()

            with caplog.at_level(
                logging.ERROR,
                logger="code_indexer.server.services.codex_mcp_auth_header_provider",
            ):
                with pytest.raises(RuntimeError):
                    provider()

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, (
            "Provider must log at ERROR level when all credential paths return None; "
            "got no ERROR records (only WARNING is emitted at the CodexInvoker call site, "
            "which is insufficient for diagnosing the root cause)"
        )

    def test_error_message_identifies_missing_cidx_local_entry(self, caplog):
        """
        The ERROR log message must identify that cidx-local MCP credentials are
        missing from the credential store, per MESSI Rule 13 (Anti-Silent-Failure).
        Operators must be able to diagnose the issue from the log alone.

        RED: Fails before fix because no ERROR is logged in the provider.
        """
        from code_indexer.server.services.codex_mcp_auth_header_provider import (
            build_codex_mcp_auth_header_provider,
        )

        mock_service = _make_service_with_no_creds()

        with patch(
            "code_indexer.server.services.codex_mcp_auth_header_provider.MCPSelfRegistrationService"
        ) as mock_cls:
            mock_cls.get_instance.return_value = mock_service
            provider = build_codex_mcp_auth_header_provider()

            with caplog.at_level(
                logging.ERROR,
                logger="code_indexer.server.services.codex_mcp_auth_header_provider",
            ):
                with pytest.raises(RuntimeError):
                    provider()

        error_messages = [
            r.message for r in caplog.records if r.levelno >= logging.ERROR
        ]
        assert any(
            "cidx-local" in msg.lower()
            or "credential" in msg.lower()
            or "mcp" in msg.lower()
            for msg in error_messages
        ), (
            f"ERROR log must identify missing cidx-local MCP credentials; "
            f"got messages: {error_messages}"
        )


# ---------------------------------------------------------------------------
# Tests: MCPSelfRegistrationService.build_header_from_stored_credentials
# ---------------------------------------------------------------------------


class TestMCPSelfRegistrationDirectHeader:
    """MCPSelfRegistrationService exposes build_header_from_stored_credentials()."""

    def test_build_header_directly_from_stored_creds_returns_basic_string(self):
        """
        build_header_from_stored_credentials() must return 'Basic <b64>' built
        from the credentials stored in config.mcp_self_registration (set by
        get_or_create_credentials()) WITHOUT calling ensure_registered() or
        claude CLI.

        RED: Fails before fix because this method does not exist on the service.
        """
        from code_indexer.server.services.mcp_self_registration_service import (
            MCPSelfRegistrationService,
        )

        # Build a service with a mock config_manager that returns stored credentials
        mock_config = MagicMock()
        mock_config.mcp_self_registration.client_id = _FAKE_CLIENT_ID
        mock_config.mcp_self_registration.client_secret = _FAKE_CLIENT_SECRET
        mock_config_manager = MagicMock()
        mock_config_manager.load_config.return_value = mock_config

        mock_cred_manager = MagicMock()

        service = MCPSelfRegistrationService(
            config_manager=mock_config_manager,
            mcp_credential_manager=mock_cred_manager,
        )

        result = service.build_header_from_stored_credentials()

        assert result is not None, (
            "build_header_from_stored_credentials() must return non-None when "
            "config.mcp_self_registration has client_id and client_secret"
        )
        assert result.startswith("Basic "), f"Expected 'Basic <b64>'; got {result!r}"
        # Verify the b64 decodes correctly
        b64_part = result[len("Basic ") :]
        decoded = base64.b64decode(b64_part).decode("ascii")
        assert decoded == f"{_FAKE_CLIENT_ID}:{_FAKE_CLIENT_SECRET}", (
            f"Decoded b64 must be '<client_id>:<client_secret>'; got {decoded!r}"
        )

    def test_build_header_directly_returns_none_when_no_stored_creds(self):
        """
        build_header_from_stored_credentials() returns None when
        config.mcp_self_registration.client_id is empty/missing.

        RED: Fails before fix because this method does not exist.
        """
        from code_indexer.server.services.mcp_self_registration_service import (
            MCPSelfRegistrationService,
        )

        mock_config = MagicMock()
        mock_config.mcp_self_registration.client_id = ""  # No stored credentials
        mock_config.mcp_self_registration.client_secret = ""
        mock_config_manager = MagicMock()
        mock_config_manager.load_config.return_value = mock_config

        # Simulate credential store empty: generate_credential raises because
        # there is no admin user. get_or_create_credentials() catches this and
        # returns None, so build_header_from_stored_credentials() must return None.
        mock_cred_manager = MagicMock()
        mock_cred_manager.generate_credential.side_effect = ValueError(
            "User not found: admin"
        )

        service = MCPSelfRegistrationService(
            config_manager=mock_config_manager,
            mcp_credential_manager=mock_cred_manager,
        )

        result = service.build_header_from_stored_credentials()

        assert result is None, (
            f"build_header_from_stored_credentials() must return None when "
            f"no stored credentials exist; got {result!r}"
        )


# ---------------------------------------------------------------------------
# Tests: full fallback chain in the provider closure
# ---------------------------------------------------------------------------


class TestProviderFallbackChain:
    """Provider uses direct-credentials path as third fallback before raising."""

    def test_provider_uses_direct_creds_before_raising(self):
        """
        The three-step fallback chain in the provider closure must be:
          1. get_cached_auth_header_value() — fast path (Claude already registered)
          2. build_auth_header_from_creds() — triggers ensure_registered() flow
          3. build_header_from_stored_credentials() — NEW: reads stored creds directly
          4. raise RuntimeError — only if all three return None

        When step 3 succeeds, step 4 (RuntimeError) must NOT be reached.

        RED: Fails before fix because step 3 does not exist in the provider.
        """
        from code_indexer.server.services.codex_mcp_auth_header_provider import (
            build_codex_mcp_auth_header_provider,
        )

        # All three paths: cached=None, build_auth=None, but direct_creds succeeds
        mock_service = MagicMock()
        mock_service.get_cached_auth_header_value.return_value = None
        mock_service.build_auth_header_from_creds.return_value = None
        mock_service.build_header_from_stored_credentials.return_value = (
            _EXPECTED_BASIC_HEADER
        )

        with patch(
            "code_indexer.server.services.codex_mcp_auth_header_provider.MCPSelfRegistrationService"
        ) as mock_cls:
            mock_cls.get_instance.return_value = mock_service
            provider = build_codex_mcp_auth_header_provider()
            # Must NOT raise — direct creds path succeeds
            result = provider()

        assert result == _EXPECTED_BASIC_HEADER, (
            f"Provider must return direct-creds header when steps 1+2 fail but "
            f"step 3 succeeds; got {result!r}"
        )
        mock_service.build_header_from_stored_credentials.assert_called_once()
