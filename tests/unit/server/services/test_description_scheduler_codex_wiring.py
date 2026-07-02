"""
Unit tests proving auth_header_provider is wired at the production CodexInvoker
construction site inside DescriptionRefreshScheduler._build_cli_dispatcher().

v9.23.10: auth_header_provider replaces bearer_token_provider. The closure
produces a persistent 'Basic <b64>' string from MCPCredentialManager-issued
credentials — no JWT, no TTL.

Test inventory (4 tests across 2 classes):

  TestCliDispatcherCodexAuthHeaderProviderWired (2 tests)
    test_codex_invoker_in_cli_dispatcher_has_auth_header_provider
    test_auth_header_provider_produces_basic_string

  TestCliDispatcherCodexAuthHeaderProviderAbsentWhenDisabled (2 tests)
    test_codex_invoker_is_none_when_codex_disabled
    test_auth_header_provider_absent_when_no_codex_home
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.mcp_self_registration_service import (
    MCPSelfRegistrationService,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Clearly synthetic placeholder — not a real credential.
_SENTINEL_CODEX_HOME = "sentinel-codex-home"
_FAKE_BASIC_HEADER = "Basic abc"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def real_mcp_singleton():
    """Build a real MCPSelfRegistrationService with stubbed dependencies, register as singleton.

    Yields the service instance. Always clears the singleton on teardown so
    tests don't leak state.
    """
    fake_config_manager = MagicMock()
    fake_mcp_credential_manager = MagicMock()

    fake_config = MagicMock()
    fake_config.mcp_self_registration.client_id = "test-client-id"
    fake_config.mcp_self_registration.client_secret = "test-client-secret"
    fake_config_manager.load_config.return_value = fake_config

    fake_mcp_credential_manager.get_credential_by_client_id.return_value = {
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
    }

    real_service = MCPSelfRegistrationService(
        config_manager=fake_config_manager,
        mcp_credential_manager=fake_mcp_credential_manager,
    )
    # Pre-populate the cached header so get_cached_auth_header_value() returns
    # the expected value without invoking ensure_registered() (which would call
    # subprocess).
    real_service._cached_auth_header = _FAKE_BASIC_HEADER
    MCPSelfRegistrationService.set_instance(real_service)
    yield real_service
    MCPSelfRegistrationService.set_instance(None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_config(codex_enabled: bool, codex_weight: float = 0.5):
    """Build a minimal mock ServerConfig with the given Codex settings."""
    from code_indexer.server.utils.config_manager import (
        ClaudeIntegrationConfig,
        CodexIntegrationConfig,
    )

    claude_cfg = ClaudeIntegrationConfig(
        description_refresh_enabled=True,
        max_concurrent_claude_cli=1,
        description_refresh_interval_hours=24,
    )
    codex_cfg = CodexIntegrationConfig(
        enabled=codex_enabled,
        codex_weight=codex_weight,
        credential_mode="api_key",
        api_key="placeholder",
    )
    cfg = MagicMock()
    cfg.claude_integration_config = claude_cfg
    cfg.codex_integration_config = codex_cfg
    return cfg


def _make_scheduler():
    """Build a DescriptionRefreshScheduler with no injected dispatcher (tests the builder)."""
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )

    tracking_backend = MagicMock()
    golden_backend = MagicMock()
    config_manager = MagicMock()
    config_manager.load_config.return_value = _make_mock_config(codex_enabled=True)

    return DescriptionRefreshScheduler(
        tracking_backend=tracking_backend,
        golden_backend=golden_backend,
        config_manager=config_manager,
        analysis_model="opus",
    )


def _build_dispatcher_and_call_provider_with_codex_enabled(real_mcp_singleton):
    """
    Build a dispatcher from _build_cli_dispatcher() with Codex enabled, then call
    the auth_header_provider closure while the real singleton is registered.

    real_mcp_singleton must be the fixture-provided MCPSelfRegistrationService
    instance already registered via MCPSelfRegistrationService.set_instance().
    This exercises the real get_instance() path — no class-symbol patching.

    Returns:
        Tuple of (dispatcher, header_string) both obtained with the singleton active.
        header_string is None when dispatcher.codex is None.
    """
    config = _make_mock_config(codex_enabled=True, codex_weight=0.7)
    scheduler = _make_scheduler()

    with patch.dict("os.environ", {"CODEX_HOME": _SENTINEL_CODEX_HOME}):
        dispatcher = scheduler._build_cli_dispatcher(config)
        provider = dispatcher.codex._auth_header_provider if dispatcher.codex else None
        header = provider() if provider is not None else None
        return dispatcher, header


# ---------------------------------------------------------------------------
# Tests: auth_header_provider wired when Codex is enabled
# ---------------------------------------------------------------------------


class TestCliDispatcherCodexAuthHeaderProviderWired:
    """_build_cli_dispatcher wires auth_header_provider when Codex is enabled."""

    def test_codex_invoker_in_cli_dispatcher_has_auth_header_provider(
        self, real_mcp_singleton
    ):
        """
        CRITICAL WIRING TEST: When Codex is enabled and CODEX_HOME is set,
        _build_cli_dispatcher must build a CodexInvoker with a non-None
        _auth_header_provider.

        This is the production construction site — not an isolated component test.
        Without this wiring, CIDX_MCP_AUTH_HEADER is never injected and codex
        cannot authenticate against the cidx-local MCP HTTP endpoint.
        """
        dispatcher, _header = _build_dispatcher_and_call_provider_with_codex_enabled(
            real_mcp_singleton
        )

        assert dispatcher.codex is not None, (
            "codex invoker must be built when Codex is enabled"
        )
        assert dispatcher.codex._auth_header_provider is not None, (
            "CRITICAL: _auth_header_provider must be non-None at the production "
            "construction site. Without this, CIDX_MCP_AUTH_HEADER is never injected "
            "and codex cannot authenticate against cidx-local MCP."
        )

    def test_auth_header_provider_produces_basic_string(self, real_mcp_singleton):
        """
        The auth_header_provider closure wired at the production site must produce
        a string starting with 'Basic ' (persistent MCPCredentialManager credentials,
        no JWT TTL).
        """
        dispatcher, header = _build_dispatcher_and_call_provider_with_codex_enabled(
            real_mcp_singleton
        )

        assert dispatcher.codex is not None, "codex invoker must be present"
        assert dispatcher.codex._auth_header_provider is not None, (
            "auth_header_provider must be wired"
        )
        assert isinstance(header, str) and header.startswith("Basic "), (
            f"Provider must return a string starting with 'Basic '; got {header!r}"
        )


# ---------------------------------------------------------------------------
# Tests: auth_header_provider absent when Codex is disabled / no CODEX_HOME
# ---------------------------------------------------------------------------


class TestCliDispatcherCodexAuthHeaderProviderAbsentWhenDisabled:
    """No CodexInvoker when Codex is disabled or CODEX_HOME is unset."""

    def test_codex_invoker_is_none_when_codex_disabled(self):
        """
        When codex_integration_config.enabled=False, the dispatcher has codex=None.
        No auth_header_provider is needed or built.
        """
        config = _make_mock_config(codex_enabled=False)
        scheduler = _make_scheduler()
        dispatcher = scheduler._build_cli_dispatcher(config)

        assert dispatcher.codex is None, (
            "codex invoker must be None when Codex integration is disabled"
        )

    def test_auth_header_provider_absent_when_no_codex_home(self):
        """
        When Codex is enabled but CODEX_HOME is absent from the environment,
        no CodexInvoker is built (the CODEX_HOME guard prevents construction).
        """
        config = _make_mock_config(codex_enabled=True, codex_weight=0.7)
        scheduler = _make_scheduler()

        # Build env without CODEX_HOME
        env_without_codex_home = {
            k: v for k, v in os.environ.items() if k != "CODEX_HOME"
        }

        with patch.dict("os.environ", env_without_codex_home, clear=True):
            dispatcher = scheduler._build_cli_dispatcher(config)

        assert dispatcher.codex is None, (
            "codex invoker must be None when CODEX_HOME is not set in environment"
        )
