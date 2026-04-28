"""
Unit tests for bug #937: startup credential seeding before auth header provider use.

Bug #937 root cause: MCPSelfRegistrationService._cached_auth_header is only
populated by register_in_claude_code(), which requires Claude CLI. On a staging
server where Claude CLI is absent, _cached_auth_header stays None and the
build_codex_mcp_auth_header_provider() closure raises RuntimeError.

The fix adds build_header_from_stored_credentials() to MCPSelfRegistrationService,
which reads credentials directly from config.mcp_self_registration (seeded by
get_or_create_credentials()) without needing Claude CLI.

These startup tests assert the invariant described in the CLAUDE.md A10 boundary:
"MCPSelfRegistrationService.set_instance() is called by service_init before
CodexInvoker is constructed with build_codex_mcp_auth_header_provider()."

Specifically:
  1. After service_init completes, the singleton is set and the provider closure
     can be successfully invoked using stored credentials.
  2. When get_or_create_credentials() seeds credentials into the config,
     build_header_from_stored_credentials() returns a valid Basic auth header
     WITHOUT requiring Claude CLI.
  3. Consequently, the closure returned by build_codex_mcp_auth_header_provider()
     succeeds after the singleton is seeded — even when Claude CLI is unavailable.

Test inventory (4 tests across 2 classes):

  TestSingletonAssignmentAndCredentialSeeding (2 tests)
    test_singleton_assigned_and_provider_invocable_after_set_instance
    test_get_or_create_credentials_seeds_config_so_direct_header_works

  TestProviderSucceedsAfterStartupSeeding (2 tests)
    test_provider_returns_header_after_seeding_when_claude_cli_absent
    test_provider_raises_when_singleton_not_set
"""

from __future__ import annotations

import base64
import secrets
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _runtime_test_id() -> str:
    """Return a runtime-generated opaque string suitable as a test identifier.

    Uses secrets.token_urlsafe() to produce a non-deterministic value with no
    credential semantics or fixed prefixes.
    """
    return secrets.token_urlsafe(18)


class _MutableMcpSelfRegistrationConfig:
    """Minimal mutable config for mcp_self_registration state."""

    def __init__(self, client_id: str = "", client_secret: str = ""):
        self.client_id = client_id
        self.client_secret = client_secret


class _StubServerConfig:
    """Minimal server config carrying a mutable mcp_self_registration sub-config."""

    def __init__(self, client_id: str = "", client_secret: str = ""):
        self.mcp_self_registration = _MutableMcpSelfRegistrationConfig(
            client_id=client_id, client_secret=client_secret
        )
        self.port = 8000


def _make_config_manager_stub(client_id: str = "", client_secret: str = "") -> tuple:
    """Return (config_manager_stub, server_config).

    The stub's load_config() returns the mutable server_config on every call.
    save_config() is a no-op because the caller mutates the same shared instance.
    """
    server_config = _StubServerConfig(client_id=client_id, client_secret=client_secret)
    config_manager = MagicMock()
    config_manager.load_config.return_value = server_config
    config_manager.save_config.return_value = None
    return config_manager, server_config


def _make_cred_manager_stub(client_id: str, client_secret: str) -> MagicMock:
    """Return an MCPCredentialManager stub that yields the given ids on generate.

    Stubs only the external credential-storage boundary, not the service under test.
    get_credential_by_client_id returns None to force credential generation.
    """
    stub = MagicMock()
    stub.generate_credential.return_value = {
        "client_id": client_id,
        "client_secret": client_secret,
        "credential_id": secrets.token_urlsafe(8),
        "name": "cidx-local-auto",
        "created_at": "2026-04-28T00:00:00+00:00",
    }
    stub.get_credential_by_client_id.return_value = None
    return stub


# ---------------------------------------------------------------------------
# Tests: singleton assignment and credential seeding
# ---------------------------------------------------------------------------


class TestSingletonAssignmentAndCredentialSeeding:
    """set_instance and get_or_create_credentials form the startup seeding invariant."""

    def test_singleton_assigned_and_provider_invocable_after_set_instance(self):
        """
        Startup invariant: after MCPSelfRegistrationService.set_instance(service),
        the provider closure built by build_codex_mcp_auth_header_provider() can be
        successfully invoked and returns a non-None 'Basic <b64>' header via stored
        credentials — confirming singleton visibility at invocation time.

        This verifies the ordering requirement from the CLAUDE.md A10 boundary:
        set_instance() must run before any provider closure is invoked.

        RED: Fails before fix because the direct-credentials path does not exist.
        """
        from code_indexer.server.services.mcp_self_registration_service import (
            MCPSelfRegistrationService,
        )
        from code_indexer.server.services.codex_mcp_auth_header_provider import (
            build_codex_mcp_auth_header_provider,
        )

        client_id = _runtime_test_id()
        client_secret = _runtime_test_id()

        config_manager, _ = _make_config_manager_stub(
            client_id=client_id, client_secret=client_secret
        )
        service = MCPSelfRegistrationService(
            config_manager=config_manager,
            mcp_credential_manager=MagicMock(),
        )
        MCPSelfRegistrationService.set_instance(service)

        try:
            provider = build_codex_mcp_auth_header_provider()
            result = provider()

            assert result is not None, (
                "Provider must return non-None after set_instance() with stored creds"
            )
            assert result.startswith("Basic "), (
                f"Provider must return 'Basic <b64>' header; got {result!r}"
            )
        finally:
            MCPSelfRegistrationService.set_instance(None)

    def test_get_or_create_credentials_seeds_config_so_direct_header_works(self):
        """
        After get_or_create_credentials() runs (as it does in service_init),
        build_header_from_stored_credentials() can build the Basic auth header
        directly from the seeded config without Claude CLI.

        This tests the full seeding pipeline:
          generate_credential() stub -> saved to config ->
          readable by build_header_from_stored_credentials().

        RED: Fails before fix because build_header_from_stored_credentials() does not exist.
        """
        from code_indexer.server.services.mcp_self_registration_service import (
            MCPSelfRegistrationService,
        )

        client_id = _runtime_test_id()
        client_secret = _runtime_test_id()

        # No credentials seeded yet — get_or_create_credentials will seed them
        config_manager, server_config = _make_config_manager_stub(
            client_id="", client_secret=""
        )
        cred_manager = _make_cred_manager_stub(
            client_id=client_id, client_secret=client_secret
        )

        service = MCPSelfRegistrationService(
            config_manager=config_manager,
            mcp_credential_manager=cred_manager,
        )

        creds = service.get_or_create_credentials()

        assert creds is not None, (
            "get_or_create_credentials() must return a credentials dict"
        )

        header = service.build_header_from_stored_credentials()

        assert header is not None, (
            "build_header_from_stored_credentials() must return non-None after "
            "get_or_create_credentials() seeded the config"
        )
        assert header.startswith("Basic "), (
            f"Header must be 'Basic <b64>'; got {header!r}"
        )
        b64_part = header[len("Basic ") :]
        decoded = base64.b64decode(b64_part).decode("ascii")
        assert decoded == f"{client_id}:{client_secret}", (
            f"Decoded b64 must be '<client_id>:<client_secret>'; got {decoded!r}"
        )


# ---------------------------------------------------------------------------
# Tests: provider succeeds after startup seeding
# ---------------------------------------------------------------------------


class TestProviderSucceedsAfterStartupSeeding:
    """build_codex_mcp_auth_header_provider() succeeds when singleton is properly seeded."""

    def test_provider_returns_header_after_seeding_when_claude_cli_absent(self):
        """
        End-to-end invariant for bug #937: after service_init seeds the singleton,
        build_codex_mcp_auth_header_provider() returns a non-None 'Basic <b64>'
        header even when Claude CLI is unavailable.

        Claude CLI unavailability is simulated by patching subprocess.run at the
        external boundary — the call inside claude_cli_available() that checks
        for the claude binary. This drives the failure through the real service
        code path without monkey-patching the service under test.

        RED: Fails before fix because the direct-credentials fallback does not exist.
        """
        from code_indexer.server.services.mcp_self_registration_service import (
            MCPSelfRegistrationService,
        )
        from code_indexer.server.services.codex_mcp_auth_header_provider import (
            build_codex_mcp_auth_header_provider,
        )

        client_id = _runtime_test_id()
        client_secret = _runtime_test_id()

        config_manager, _ = _make_config_manager_stub(
            client_id=client_id, client_secret=client_secret
        )
        service = MCPSelfRegistrationService(
            config_manager=config_manager,
            mcp_credential_manager=MagicMock(),
        )
        MCPSelfRegistrationService.set_instance(service)

        try:
            # Patch subprocess.run at the external boundary so Claude CLI appears absent.
            # This causes claude_cli_available() -> False -> register_in_claude_code()
            # never runs -> _cached_auth_header stays None. The fix must succeed via
            # the build_header_from_stored_credentials() path instead.
            with patch(
                "code_indexer.server.services.mcp_self_registration_service.subprocess.run",
                side_effect=FileNotFoundError("claude: command not found"),
            ):
                provider = build_codex_mcp_auth_header_provider()
                result = provider()

            assert result is not None, (
                "Provider must succeed via stored credentials when Claude CLI absent; "
                "got None. This is bug #937 — Codex spawns without CIDX_MCP_AUTH_HEADER."
            )
            assert result.startswith("Basic "), (
                f"Provider must return 'Basic <b64>'; got {result!r}"
            )
        finally:
            MCPSelfRegistrationService.set_instance(None)

    def test_provider_raises_when_singleton_not_set(self):
        """
        When MCPSelfRegistrationService singleton is None (startup incomplete),
        the provider must raise RuntimeError immediately (Foundation 13: fail fast).

        This verifies the existing behaviour is preserved after the fix.
        """
        from code_indexer.server.services.mcp_self_registration_service import (
            MCPSelfRegistrationService,
        )
        from code_indexer.server.services.codex_mcp_auth_header_provider import (
            build_codex_mcp_auth_header_provider,
        )

        MCPSelfRegistrationService.set_instance(None)

        try:
            provider = build_codex_mcp_auth_header_provider()
            with pytest.raises(RuntimeError, match="singleton"):
                provider()
        finally:
            MCPSelfRegistrationService.set_instance(None)
