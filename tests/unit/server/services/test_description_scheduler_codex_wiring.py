"""
Unit tests proving bearer_token_provider is wired at the production CodexInvoker
construction site inside DescriptionRefreshScheduler._build_cli_dispatcher().

These are WIRING tests — they test the production builder method, not isolated
component construction. The goal is to prove that in production, the CodexInvoker
receives a bearer_token_provider closure that produces valid admin-scope JWTs.

Test inventory (4 tests across 2 classes):

  TestCliDispatcherCodexBearerProviderWired (2 tests)
    test_codex_invoker_in_cli_dispatcher_has_bearer_provider
    test_bearer_provider_produces_valid_admin_jwt

  TestCliDispatcherCodexBearerProviderAbsentWhenDisabled (2 tests)
    test_codex_invoker_is_none_when_codex_disabled
    test_bearer_provider_absent_when_no_codex_home
"""

from __future__ import annotations

import os
import secrets
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.jwt_manager import JWTManager


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Sentinel value used as CODEX_HOME — not a real path, just a recognisable string
# for assertions. Using a non-path sentinel avoids coupling tests to filesystem shapes.
_SENTINEL_CODEX_HOME = "sentinel-codex-home"
_TEST_SECRET = secrets.token_urlsafe(32)


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
    cfg.jwt_expiration_minutes = 10
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


def _build_dispatcher_and_call_provider_with_codex_enabled():
    """
    Build a dispatcher from _build_cli_dispatcher() with Codex enabled, then call
    the bearer_token_provider closure while all patches are still active.

    Patches must remain active when provider() is called because the closure
    references module-level JWTSecretManager / get_config_service names — the
    mock substitutions are only in effect inside the with block.

    Returns:
        Tuple of (dispatcher, token_string) both obtained under active patches.
        token_string is None when dispatcher.codex is None.
    """
    config = _make_mock_config(codex_enabled=True, codex_weight=0.7)
    scheduler = _make_scheduler()

    with (
        patch(
            "code_indexer.server.services.codex_bearer_provider.get_config_service"
        ) as mock_get_cfg,
        patch.dict("os.environ", {"CODEX_HOME": _SENTINEL_CODEX_HOME}),
        patch(
            "code_indexer.server.services.codex_bearer_provider.JWTSecretManager"
        ) as mock_jwt_secret_mgr_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.get_config.return_value = config
        mock_get_cfg.return_value = mock_svc

        mock_secret_mgr = MagicMock()
        mock_secret_mgr.get_or_create_secret.return_value = _TEST_SECRET
        mock_jwt_secret_mgr_cls.return_value = mock_secret_mgr

        dispatcher = scheduler._build_cli_dispatcher(config)
        # Call the provider while patches are still active so JWTSecretManager
        # is still substituted and the token is signed with _TEST_SECRET.
        provider = dispatcher.codex._bearer_token_provider if dispatcher.codex else None
        token = provider() if provider is not None else None
        return dispatcher, token


# ---------------------------------------------------------------------------
# Tests: bearer_token_provider wired when Codex is enabled
# ---------------------------------------------------------------------------


class TestCliDispatcherCodexBearerProviderWired:
    """_build_cli_dispatcher wires bearer_token_provider when Codex is enabled."""

    def test_codex_invoker_in_cli_dispatcher_has_bearer_provider(self):
        """
        CRITICAL WIRING TEST: When Codex is enabled and CODEX_HOME is set,
        _build_cli_dispatcher must build a CodexInvoker with a non-None
        _bearer_token_provider.

        This is the production construction site — not an isolated component test.
        Without this wiring, CIDX_MCP_BEARER_TOKEN is never injected and codex
        cannot authenticate against the cidx-local MCP HTTP endpoint.
        """
        dispatcher, _token = _build_dispatcher_and_call_provider_with_codex_enabled()

        assert dispatcher.codex is not None, (
            "codex invoker must be built when Codex is enabled"
        )
        assert dispatcher.codex._bearer_token_provider is not None, (
            "CRITICAL: _bearer_token_provider must be non-None at the production "
            "construction site. Without this, CIDX_MCP_BEARER_TOKEN is never injected "
            "and codex cannot authenticate against cidx-local MCP."
        )

    def test_bearer_provider_produces_valid_admin_jwt(self):
        """
        The bearer_token_provider closure wired at the production site must produce
        a JWT that JWTManager.validate_token() accepts with admin-scope claims.

        Uses real JWTManager with _TEST_SECRET (injected via patch) to verify
        end-to-end: construction site builds closure -> closure mints token ->
        token validates with role=admin, username=admin.

        The token is obtained inside the patch context (via the helper) because the
        closure references module-level JWTSecretManager which is only substituted
        while the patch is active.
        """
        dispatcher, token = _build_dispatcher_and_call_provider_with_codex_enabled()

        assert dispatcher.codex is not None, "codex invoker must be present"
        assert dispatcher.codex._bearer_token_provider is not None, (
            "bearer_token_provider must be wired"
        )
        assert isinstance(token, str) and token, (
            f"Provider must return a non-empty string token; got {token!r}"
        )

        # Validate using real JWTManager with the same test secret
        jwt_manager = JWTManager(secret_key=_TEST_SECRET, token_expiration_minutes=10)
        payload = jwt_manager.validate_token(token)  # must not raise
        assert payload.get("username") == "admin", (
            f"Token must carry username='admin'; got {payload.get('username')!r}"
        )
        assert payload.get("role") == "admin", (
            f"Token must carry role='admin'; got {payload.get('role')!r}"
        )


# ---------------------------------------------------------------------------
# Tests: bearer_token_provider absent when Codex is disabled / no CODEX_HOME
# ---------------------------------------------------------------------------


class TestCliDispatcherCodexBearerProviderAbsentWhenDisabled:
    """No CodexInvoker when Codex is disabled or CODEX_HOME is unset."""

    def test_codex_invoker_is_none_when_codex_disabled(self):
        """
        When codex_integration_config.enabled=False, the dispatcher has codex=None.
        No bearer_token_provider is needed or built.
        """
        config = _make_mock_config(codex_enabled=False)
        scheduler = _make_scheduler()
        dispatcher = scheduler._build_cli_dispatcher(config)

        assert dispatcher.codex is None, (
            "codex invoker must be None when Codex integration is disabled"
        )

    def test_bearer_provider_absent_when_no_codex_home(self):
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
