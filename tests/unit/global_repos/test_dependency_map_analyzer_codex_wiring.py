"""
Unit tests proving bearer_token_provider is wired at the production CodexInvoker
construction site inside DependencyMapAnalyzer._build_pass2_dispatcher().

These are WIRING tests — they test the production builder method, not isolated
component construction. The goal is to prove that in production, the CodexInvoker
receives a bearer_token_provider closure that produces valid admin-scope JWTs.

Test inventory (4 tests across 2 classes):

  TestPass2DispatcherCodexBearerProviderWired (2 tests)
    test_codex_invoker_in_pass2_dispatcher_has_bearer_provider
    test_bearer_provider_produces_valid_admin_jwt

  TestPass2DispatcherCodexBearerProviderAbsentWhenDisabled (2 tests)
    test_codex_invoker_is_none_when_codex_disabled
    test_bearer_provider_absent_when_no_codex_home
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer
from code_indexer.server.auth.jwt_manager import JWTManager


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FAKE_CODEX_HOME = "/fake/codex-home"
_TEST_SECRET = secrets.token_urlsafe(32)


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
    cfg.jwt_expiration_minutes = 10
    return cfg


def _make_analyzer(tmp_path: Path) -> DependencyMapAnalyzer:
    """Build a DependencyMapAnalyzer with no injected dispatcher (tests the builder)."""
    return DependencyMapAnalyzer(
        golden_repos_root=tmp_path,
        cidx_meta_path=tmp_path / "cidx-meta",
        pass_timeout=600,
    )


def _build_dispatcher_with_codex_enabled(tmp_path: Path):
    """
    Build a dispatcher from _build_pass2_dispatcher() with Codex enabled, then call
    the bearer_token_provider closure while all patches are still active.

    Patches target codex_bearer_provider (the shared module) because
    JWTSecretManager and get_config_service are now module-level names there.
    The provider is called inside the with block so the token is signed with
    _TEST_SECRET while JWTSecretManager is still patched.

    Returns:
        Tuple of (dispatcher, token_string) both obtained under active patches.
    """
    config = _make_mock_config(codex_enabled=True, codex_weight=0.7)

    with (
        patch(
            "code_indexer.global_repos.dependency_map_analyzer.get_config_service"
        ) as mock_get_cfg_dma,
        patch(
            "code_indexer.server.services.codex_bearer_provider.get_config_service"
        ) as mock_get_cfg_bearer,
        patch.dict("os.environ", {"CODEX_HOME": _FAKE_CODEX_HOME}),
        patch(
            "code_indexer.server.services.codex_bearer_provider.JWTSecretManager"
        ) as mock_jwt_secret_mgr_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.get_config.return_value = config
        mock_get_cfg_dma.return_value = mock_svc
        mock_get_cfg_bearer.return_value = mock_svc

        mock_secret_mgr = MagicMock()
        mock_secret_mgr.get_or_create_secret.return_value = _TEST_SECRET
        mock_jwt_secret_mgr_cls.return_value = mock_secret_mgr

        analyzer = _make_analyzer(tmp_path)
        dispatcher = analyzer._build_pass2_dispatcher()
        # Call the provider while patches are still active so JWTSecretManager
        # is still substituted and the token is signed with _TEST_SECRET.
        provider = dispatcher.codex._bearer_token_provider if dispatcher.codex else None
        token = provider() if provider is not None else None
        return dispatcher, token


# ---------------------------------------------------------------------------
# Tests: bearer_token_provider wired when Codex is enabled
# ---------------------------------------------------------------------------


class TestPass2DispatcherCodexBearerProviderWired:
    """_build_pass2_dispatcher wires bearer_token_provider when Codex is enabled."""

    def test_codex_invoker_in_pass2_dispatcher_has_bearer_provider(self, tmp_path):
        """
        CRITICAL WIRING TEST: When Codex is enabled and CODEX_HOME is set,
        _build_pass2_dispatcher must build a CodexInvoker with a non-None
        _bearer_token_provider.

        This is the production construction site — not an isolated component test.
        Without this wiring, CIDX_MCP_BEARER_TOKEN is never injected and codex
        cannot authenticate against the cidx-local MCP HTTP endpoint.
        """
        dispatcher, _token = _build_dispatcher_with_codex_enabled(tmp_path)

        assert dispatcher.codex is not None, (
            "codex invoker must be built when Codex is enabled"
        )
        assert dispatcher.codex._bearer_token_provider is not None, (
            "CRITICAL: _bearer_token_provider must be non-None at the production "
            "construction site. Without this, CIDX_MCP_BEARER_TOKEN is never injected "
            "and codex cannot authenticate against cidx-local MCP."
        )

    def test_bearer_provider_produces_valid_admin_jwt(self, tmp_path):
        """
        The bearer_token_provider closure wired at the production site must produce
        a JWT that JWTManager.validate_token() accepts with admin-scope claims.

        Uses real JWTManager with _TEST_SECRET (injected via patch) to verify
        end-to-end: construction site builds closure -> closure mints token ->
        token validates with role=admin, username=admin.

        The token is obtained inside the patch context (via the helper) because the
        closure references module-level JWTSecretManager in codex_bearer_provider
        which is only substituted while the patch is active.
        """
        dispatcher, token = _build_dispatcher_with_codex_enabled(tmp_path)

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


class TestPass2DispatcherCodexBearerProviderAbsentWhenDisabled:
    """No CodexInvoker when Codex is disabled or CODEX_HOME is unset."""

    def test_codex_invoker_is_none_when_codex_disabled(self, tmp_path):
        """
        When codex_integration_config.enabled=False, the dispatcher has codex=None.
        No bearer_token_provider is needed or built.
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

    def test_bearer_provider_absent_when_no_codex_home(self, tmp_path):
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
