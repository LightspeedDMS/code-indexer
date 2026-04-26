"""
Unit tests proving auth_header_provider is wired at the production CodexInvoker
construction site inside DependencyMapAnalyzer._build_pass2_dispatcher().

v9.23.10: auth_header_provider replaces bearer_token_provider. The closure
produces a persistent 'Basic <b64>' string from MCPCredentialManager-issued
credentials — no JWT, no TTL.

Test inventory (4 tests across 2 classes):

  TestPass2DispatcherCodexAuthHeaderProviderWired (2 tests)
    test_codex_invoker_in_pass2_dispatcher_has_auth_header_provider
    test_auth_header_provider_produces_basic_string

  TestPass2DispatcherCodexAuthHeaderProviderAbsentWhenDisabled (2 tests)
    test_codex_invoker_is_none_when_codex_disabled
    test_auth_header_provider_absent_when_no_codex_home
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer
from code_indexer.server.services.mcp_self_registration_service import (
    MCPSelfRegistrationService,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FAKE_CODEX_HOME = "/fake/codex-home"
# Clearly synthetic placeholder — not a real credential.
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
    from code_indexer.server.utils.config_manager import CodexIntegrationConfig

    codex_cfg = CodexIntegrationConfig(
        enabled=codex_enabled,
        codex_weight=codex_weight,
        credential_mode="api_key",
        api_key="placeholder",
    )
    cfg = MagicMock()
    cfg.codex_integration_config = codex_cfg
    return cfg


def _make_analyzer(tmp_path: Path) -> DependencyMapAnalyzer:
    """Build a DependencyMapAnalyzer with no injected dispatcher (tests the builder)."""
    return DependencyMapAnalyzer(
        golden_repos_root=tmp_path,
        cidx_meta_path=tmp_path / "cidx-meta",
        pass_timeout=600,
    )


def _build_dispatcher_with_codex_enabled(tmp_path: Path, real_mcp_singleton):
    """
    Build a dispatcher from _build_pass2_dispatcher() with Codex enabled, then call
    the auth_header_provider closure while the real singleton is registered.

    real_mcp_singleton must be the fixture-provided MCPSelfRegistrationService
    instance already registered via MCPSelfRegistrationService.set_instance().
    This exercises the real get_instance() path — no class-symbol patching.

    Returns:
        Tuple of (dispatcher, header_string) both obtained with the singleton active.
    """
    config = _make_mock_config(codex_enabled=True, codex_weight=0.7)

    with (
        patch(
            "code_indexer.global_repos.dependency_map_analyzer.get_config_service"
        ) as mock_get_cfg_dma,
        patch.dict("os.environ", {"CODEX_HOME": _FAKE_CODEX_HOME}),
    ):
        mock_svc = MagicMock()
        mock_svc.get_config.return_value = config
        mock_get_cfg_dma.return_value = mock_svc

        analyzer = _make_analyzer(tmp_path)
        dispatcher = analyzer._build_pass2_dispatcher()
        provider = dispatcher.codex._auth_header_provider if dispatcher.codex else None
        header = provider() if provider is not None else None
        return dispatcher, header


# ---------------------------------------------------------------------------
# Tests: auth_header_provider wired when Codex is enabled
# ---------------------------------------------------------------------------


class TestPass2DispatcherCodexAuthHeaderProviderWired:
    """_build_pass2_dispatcher wires auth_header_provider when Codex is enabled."""

    def test_codex_invoker_in_pass2_dispatcher_has_auth_header_provider(
        self, tmp_path, real_mcp_singleton
    ):
        """
        CRITICAL WIRING TEST: When Codex is enabled and CODEX_HOME is set,
        _build_pass2_dispatcher must build a CodexInvoker with a non-None
        _auth_header_provider.

        This is the production construction site — not an isolated component test.
        Without this wiring, CIDX_MCP_AUTH_HEADER is never injected and codex
        cannot authenticate against the cidx-local MCP HTTP endpoint.
        """
        dispatcher, _header = _build_dispatcher_with_codex_enabled(
            tmp_path, real_mcp_singleton
        )

        assert dispatcher.codex is not None, (
            "codex invoker must be built when Codex is enabled"
        )
        assert dispatcher.codex._auth_header_provider is not None, (
            "CRITICAL: _auth_header_provider must be non-None at the production "
            "construction site. Without this, CIDX_MCP_AUTH_HEADER is never injected "
            "and codex cannot authenticate against cidx-local MCP."
        )

    def test_auth_header_provider_produces_basic_string(
        self, tmp_path, real_mcp_singleton
    ):
        """
        The auth_header_provider closure wired at the production site must produce
        a string starting with 'Basic ' (persistent MCPCredentialManager credentials,
        no JWT TTL).
        """
        dispatcher, header = _build_dispatcher_with_codex_enabled(
            tmp_path, real_mcp_singleton
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


class TestPass2DispatcherCodexAuthHeaderProviderAbsentWhenDisabled:
    """No CodexInvoker when Codex is disabled or CODEX_HOME is unset."""

    def test_codex_invoker_is_none_when_codex_disabled(self, tmp_path):
        """
        When codex_integration_config.enabled=False, the dispatcher has codex=None.
        No auth_header_provider is needed or built.
        """
        config = _make_mock_config(codex_enabled=False)

        with patch(
            "code_indexer.global_repos.dependency_map_analyzer.get_config_service"
        ) as mock_get_cfg:
            mock_svc = MagicMock()
            mock_svc.get_config.return_value = config
            mock_get_cfg.return_value = mock_svc

            analyzer = _make_analyzer(tmp_path)
            dispatcher = analyzer._build_pass2_dispatcher()

        assert dispatcher.codex is None, (
            "codex invoker must be None when Codex integration is disabled"
        )

    def test_auth_header_provider_absent_when_no_codex_home(self, tmp_path):
        """
        When Codex is enabled but CODEX_HOME is absent from the environment,
        no CodexInvoker is built (the CODEX_HOME guard prevents construction).
        """
        config = _make_mock_config(codex_enabled=True, codex_weight=0.7)

        # Build env without CODEX_HOME
        env_without_codex_home = {
            k: v for k, v in os.environ.items() if k != "CODEX_HOME"
        }

        with (
            patch(
                "code_indexer.global_repos.dependency_map_analyzer.get_config_service"
            ) as mock_get_cfg,
            patch.dict("os.environ", env_without_codex_home, clear=True),
        ):
            mock_svc = MagicMock()
            mock_svc.get_config.return_value = config
            mock_get_cfg.return_value = mock_svc

            analyzer = _make_analyzer(tmp_path)
            dispatcher = analyzer._build_pass2_dispatcher()

        assert dispatcher.codex is None, (
            "codex invoker must be None when CODEX_HOME is not set in environment"
        )
