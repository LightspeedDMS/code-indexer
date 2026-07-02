"""
Unit tests for Bug #895 — provider API key resolution reads from
ClaudeIntegrationConfig, not top-level ServerConfig.

Tests:
  TestResolveProviderApiKey — cohere + voyage-ai keys come from nested config;
      unknown provider returns None; None when key unset (parametrized);
      regression guard against top-level ServerConfig attribute reads.
  TestBuildProviderApiKeyEnvUsesHelper — _build_provider_api_key_env sets
      CO_API_KEY and VOYAGE_API_KEY from ClaudeIntegrationConfig.
"""

import pytest
from unittest.mock import MagicMock, patch

from code_indexer.server.utils.config_manager import (
    ClaudeIntegrationConfig,
    ServerConfig,
)


def _make_patched_service(voyageai_api_key=None, cohere_api_key=None):
    """Return (config, mock_service) with ClaudeIntegrationConfig pre-populated."""
    ci_config = ClaudeIntegrationConfig(
        voyageai_api_key=voyageai_api_key,
        cohere_api_key=cohere_api_key,
    )
    config = MagicMock(spec=ServerConfig)
    config.claude_integration_config = ci_config
    mock_service = MagicMock()
    mock_service.get_config.return_value = config
    return config, mock_service


_PATCH_TARGET = "code_indexer.server.mcp.handlers.repos.get_config_service"


class TestResolveProviderApiKey:
    """Bug #895 Sites 2-4: API keys must be read from ClaudeIntegrationConfig."""

    def test_cohere_key_returned_from_nested_config(self):
        """_resolve_provider_api_key('cohere') returns key from ClaudeIntegrationConfig."""
        from code_indexer.server.mcp.handlers.repos import _resolve_provider_api_key

        _, svc = _make_patched_service(cohere_api_key="ck-test")
        with patch(_PATCH_TARGET, return_value=svc):
            result = _resolve_provider_api_key("cohere")

        assert result == "ck-test", (
            f"Expected 'ck-test', got {result!r}. "
            "Bug #895: cohere_api_key must come from claude_integration_config."
        )

    def test_voyageai_key_returned_from_nested_config(self):
        """_resolve_provider_api_key('voyage-ai') returns key from ClaudeIntegrationConfig."""
        from code_indexer.server.mcp.handlers.repos import _resolve_provider_api_key

        _, svc = _make_patched_service(voyageai_api_key="vk-test")
        with patch(_PATCH_TARGET, return_value=svc):
            result = _resolve_provider_api_key("voyage-ai")

        assert result == "vk-test", (
            f"Expected 'vk-test', got {result!r}. "
            "Bug #895: voyageai_api_key must come from claude_integration_config."
        )

    def test_unknown_provider_returns_none(self):
        """_resolve_provider_api_key for an unrecognised provider returns None."""
        from code_indexer.server.mcp.handlers.repos import _resolve_provider_api_key

        _, svc = _make_patched_service(voyageai_api_key="vk", cohere_api_key="ck")
        with patch(_PATCH_TARGET, return_value=svc):
            result = _resolve_provider_api_key("openai")

        assert result is None, f"Expected None for unknown provider, got {result!r}."

    @pytest.mark.parametrize(
        "provider,key_kwarg",
        [
            ("cohere", "cohere_api_key"),
            ("voyage-ai", "voyageai_api_key"),
        ],
    )
    def test_returns_none_when_key_not_configured(self, provider, key_kwarg):
        """_resolve_provider_api_key returns None when the key is not set in config."""
        from code_indexer.server.mcp.handlers.repos import _resolve_provider_api_key

        _, svc = _make_patched_service(**{key_kwarg: None})
        with patch(_PATCH_TARGET, return_value=svc):
            result = _resolve_provider_api_key(provider)

        assert result is None, (
            f"Expected None for {provider} when key unset, got {result!r}."
        )

    def test_does_not_read_from_top_level_server_config(self):
        """Regression: key must NOT come from a top-level ServerConfig attribute."""
        from code_indexer.server.mcp.handlers.repos import _resolve_provider_api_key

        ci_config = ClaudeIntegrationConfig(cohere_api_key="nested-ck")
        config = MagicMock()
        config.claude_integration_config = ci_config
        # Deliberately add a top-level attribute — the buggy code would read this.
        config.cohere_api_key = "toplevel-ck"

        svc = MagicMock()
        svc.get_config.return_value = config
        with patch(_PATCH_TARGET, return_value=svc):
            result = _resolve_provider_api_key("cohere")

        assert result == "nested-ck", (
            f"Expected 'nested-ck' from ClaudeIntegrationConfig, got {result!r}. "
            "Bug #895: must not fall back to top-level ServerConfig attribute."
        )


class TestBuildProviderApiKeyEnvUsesHelper:
    """_build_provider_api_key_env must inject keys from ClaudeIntegrationConfig."""

    def test_cohere_key_injected_into_env(self):
        """_build_provider_api_key_env sets CO_API_KEY from ClaudeIntegrationConfig."""
        from code_indexer.server.mcp.handlers.repos import _build_provider_api_key_env

        _, svc = _make_patched_service(cohere_api_key="ck-env-test")
        with patch(_PATCH_TARGET, return_value=svc):
            env = _build_provider_api_key_env("cohere")

        assert env.get("CO_API_KEY") == "ck-env-test", (
            f"Expected CO_API_KEY='ck-env-test', got {env.get('CO_API_KEY')!r}. "
            "Bug #895: _build_provider_api_key_env must read from ClaudeIntegrationConfig."
        )

    def test_voyage_key_injected_into_env(self):
        """_build_provider_api_key_env sets VOYAGE_API_KEY from ClaudeIntegrationConfig."""
        from code_indexer.server.mcp.handlers.repos import _build_provider_api_key_env

        _, svc = _make_patched_service(voyageai_api_key="vk-env-test")
        with patch(_PATCH_TARGET, return_value=svc):
            env = _build_provider_api_key_env("voyage-ai")

        assert env.get("VOYAGE_API_KEY") == "vk-env-test", (
            f"Expected VOYAGE_API_KEY='vk-env-test', got {env.get('VOYAGE_API_KEY')!r}. "
            "Bug #895: _build_provider_api_key_env must read from ClaudeIntegrationConfig."
        )
