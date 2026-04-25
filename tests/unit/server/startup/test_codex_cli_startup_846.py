"""
Unit tests for codex_cli_startup.initialize_codex_manager_on_startup (Story #846).

Covers: disabled config no-op, api_key mode env var + no auth.json write,
subscription mode lease checkout verified + auth.json write, none mode full
no-op, exact CODEX_HOME path from server_data_dir, codex-home dir auto-creation,
and shutdown hook verifying both lease return and auth.json deletion.
No live network calls — HTTP mocked via httpx.MockTransport.
"""

from __future__ import annotations

import os
from typing import List, Tuple
from unittest.mock import MagicMock, patch

import httpx
import pytest

from code_indexer.server.startup.codex_cli_startup import (
    initialize_codex_manager_on_startup,
)
from code_indexer.server.utils.config_manager import CodexIntegrationConfig


# ---------------------------------------------------------------------------
# Neutral test constants
# ---------------------------------------------------------------------------

TEST_API_KEY = "test_openai_api_key"
TEST_LEASE_ID = "test_lease_id"
TEST_CREDENTIAL_ID = "test_credential_id"
TEST_ACCESS_TOKEN = "test_access_token"
TEST_REFRESH_TOKEN = "test_refresh_token"
TEST_LCP_URL = "http://test-lcp-provider"
TEST_LCP_API_KEY = "test_lcp_api_key"


# ---------------------------------------------------------------------------
# HTTP mock helpers
# ---------------------------------------------------------------------------


def _checkout_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "lease_id": TEST_LEASE_ID,
            "credential_id": TEST_CREDENTIAL_ID,
            "access_token": TEST_ACCESS_TOKEN,
            "refresh_token": TEST_REFRESH_TOKEN,
            "custom_fields": {},
        },
    )


def _checkin_ok() -> httpx.Response:
    return httpx.Response(200, json={"status": "ok"})


def _make_capturing_transport() -> Tuple[httpx.MockTransport, List[httpx.Request]]:
    """Return (transport, captured_requests) — transport records every request."""
    captured: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if "/checkout" in str(request.url):
            return _checkout_ok()
        return _checkin_ok()

    return httpx.MockTransport(handler), captured


# ---------------------------------------------------------------------------
# Config factories
# ---------------------------------------------------------------------------


def _disabled_config() -> CodexIntegrationConfig:
    return CodexIntegrationConfig(enabled=False, credential_mode="none")


def _api_key_config() -> CodexIntegrationConfig:
    return CodexIntegrationConfig(
        enabled=True,
        credential_mode="api_key",
        api_key=TEST_API_KEY,
    )


def _subscription_config() -> CodexIntegrationConfig:
    return CodexIntegrationConfig(
        enabled=True,
        credential_mode="subscription",
        lcp_url=TEST_LCP_URL,
        api_key=TEST_LCP_API_KEY,
    )


def _none_config() -> CodexIntegrationConfig:
    return CodexIntegrationConfig(enabled=True, credential_mode="none")


def _make_server_config(codex_config: CodexIntegrationConfig) -> MagicMock:
    cfg = MagicMock()
    cfg.codex_integration_config = codex_config
    return cfg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_env():
    """Remove OPENAI_API_KEY and CODEX_HOME from env before and after each test."""
    for var in ("OPENAI_API_KEY", "CODEX_HOME"):
        os.environ.pop(var, None)
    yield
    for var in ("OPENAI_API_KEY", "CODEX_HOME"):
        os.environ.pop(var, None)


# ---------------------------------------------------------------------------
# disabled config — no-op
# ---------------------------------------------------------------------------


class TestDisabledConfig:
    def test_disabled_sets_no_env_vars(self, tmp_path):
        """When enabled=False, no env vars must be set."""
        server_config = _make_server_config(_disabled_config())
        initialize_codex_manager_on_startup(
            server_config=server_config,
            server_data_dir=str(tmp_path),
        )
        assert "OPENAI_API_KEY" not in os.environ
        assert "CODEX_HOME" not in os.environ

    def test_disabled_writes_no_auth_json(self, tmp_path):
        server_config = _make_server_config(_disabled_config())
        initialize_codex_manager_on_startup(
            server_config=server_config,
            server_data_dir=str(tmp_path),
        )
        assert not (tmp_path / "codex-home" / "auth.json").exists()


# ---------------------------------------------------------------------------
# api_key mode
# ---------------------------------------------------------------------------


class TestApiKeyMode:
    def test_sets_openai_api_key_env_var(self, tmp_path):
        server_config = _make_server_config(_api_key_config())
        initialize_codex_manager_on_startup(
            server_config=server_config,
            server_data_dir=str(tmp_path),
        )
        assert os.environ.get("OPENAI_API_KEY") == TEST_API_KEY

    def test_delegates_auth_json_to_codex_login(self, tmp_path):
        """Per fix: api_key mode must delegate auth.json to `codex login --with-api-key`
        rather than writing the OAuth-style schema directly via CodexCredentialsFileManager."""
        from unittest.mock import patch as _patch

        with _patch(
            "code_indexer.server.startup.codex_cli_startup._login_codex_with_api_key",
            return_value=True,
        ) as mock_login:
            server_config = _make_server_config(_api_key_config())
            initialize_codex_manager_on_startup(
                server_config=server_config,
                server_data_dir=str(tmp_path),
            )
        mock_login.assert_called_once()
        call_kwargs = mock_login.call_args[1]
        assert call_kwargs["api_key"] == TEST_API_KEY

    def test_sets_codex_home_to_exact_expected_path(self, tmp_path):
        """CODEX_HOME must be exactly <server_data_dir>/codex-home."""
        expected = str(tmp_path / "codex-home")
        server_config = _make_server_config(_api_key_config())
        initialize_codex_manager_on_startup(
            server_config=server_config,
            server_data_dir=str(tmp_path),
        )
        assert os.environ.get("CODEX_HOME") == expected


# ---------------------------------------------------------------------------
# none mode
# ---------------------------------------------------------------------------


class TestNoneMode:
    def test_none_mode_sets_no_openai_api_key(self, tmp_path):
        server_config = _make_server_config(_none_config())
        initialize_codex_manager_on_startup(
            server_config=server_config,
            server_data_dir=str(tmp_path),
        )
        assert "OPENAI_API_KEY" not in os.environ

    def test_none_mode_sets_no_codex_home(self, tmp_path):
        """none mode must not set CODEX_HOME — machine credentials assumed."""
        server_config = _make_server_config(_none_config())
        initialize_codex_manager_on_startup(
            server_config=server_config,
            server_data_dir=str(tmp_path),
        )
        assert "CODEX_HOME" not in os.environ

    def test_none_mode_writes_no_auth_json(self, tmp_path):
        server_config = _make_server_config(_none_config())
        initialize_codex_manager_on_startup(
            server_config=server_config,
            server_data_dir=str(tmp_path),
        )
        assert not (tmp_path / "codex-home" / "auth.json").exists()


# ---------------------------------------------------------------------------
# subscription mode
# ---------------------------------------------------------------------------


class TestSubscriptionMode:
    def test_subscription_performs_checkout_request(self, tmp_path):
        """subscription mode must issue a checkout to llm-creds-provider."""
        transport, captured = _make_capturing_transport()
        server_config = _make_server_config(_subscription_config())
        with patch(
            "code_indexer.server.startup.codex_cli_startup._make_transport",
            return_value=transport,
        ):
            initialize_codex_manager_on_startup(
                server_config=server_config,
                server_data_dir=str(tmp_path),
            )
        checkout_reqs = [r for r in captured if "/checkout" in str(r.url)]
        assert len(checkout_reqs) >= 1, "Expected at least one checkout request"

    def test_subscription_writes_auth_json(self, tmp_path):
        transport, _ = _make_capturing_transport()
        server_config = _make_server_config(_subscription_config())
        with patch(
            "code_indexer.server.startup.codex_cli_startup._make_transport",
            return_value=transport,
        ):
            initialize_codex_manager_on_startup(
                server_config=server_config,
                server_data_dir=str(tmp_path),
            )
        assert (tmp_path / "codex-home" / "auth.json").exists()

    def test_subscription_does_not_set_openai_api_key_from_config(self, tmp_path):
        """In subscription mode, OPENAI_API_KEY must NOT be set from config."""
        transport, _ = _make_capturing_transport()
        server_config = _make_server_config(_subscription_config())
        with patch(
            "code_indexer.server.startup.codex_cli_startup._make_transport",
            return_value=transport,
        ):
            initialize_codex_manager_on_startup(
                server_config=server_config,
                server_data_dir=str(tmp_path),
            )
        assert os.environ.get("OPENAI_API_KEY") != TEST_LCP_API_KEY


# ---------------------------------------------------------------------------
# CODEX_HOME env var — honors server_data_dir (Bug #879 pattern)
# ---------------------------------------------------------------------------


class TestCodexHomeEnvVar:
    def test_codex_home_is_exact_server_data_dir_subpath(self, tmp_path):
        """CODEX_HOME must equal <server_data_dir>/codex-home exactly."""
        expected = str(tmp_path / "codex-home")
        server_config = _make_server_config(_api_key_config())
        initialize_codex_manager_on_startup(
            server_config=server_config,
            server_data_dir=str(tmp_path),
        )
        assert os.environ.get("CODEX_HOME") == expected

    def test_codex_home_dir_is_created_if_absent(self, tmp_path):
        """codex-home/ directory must be created automatically if absent."""
        codex_home_dir = tmp_path / "codex-home"
        assert not codex_home_dir.exists()
        server_config = _make_server_config(_api_key_config())
        initialize_codex_manager_on_startup(
            server_config=server_config,
            server_data_dir=str(tmp_path),
        )
        assert codex_home_dir.is_dir()


# ---------------------------------------------------------------------------
# Shutdown hook — subscription mode cleanup
# ---------------------------------------------------------------------------


class TestShutdownHook:
    def test_shutdown_hook_calls_checkin(self, tmp_path):
        """Shutdown hook must return the lease (call /checkin)."""
        transport, captured = _make_capturing_transport()
        server_config = _make_server_config(_subscription_config())
        with patch(
            "code_indexer.server.startup.codex_cli_startup._make_transport",
            return_value=transport,
        ):
            shutdown_fn = initialize_codex_manager_on_startup(
                server_config=server_config,
                server_data_dir=str(tmp_path),
                return_shutdown_hook=True,
            )
        if callable(shutdown_fn):
            shutdown_fn()
        checkin_reqs = [r for r in captured if "/checkin" in str(r.url)]
        assert len(checkin_reqs) >= 1, "Expected at least one checkin request from shutdown"

    def test_shutdown_hook_removes_auth_json(self, tmp_path):
        """Shutdown hook must delete auth.json after returning the lease."""
        transport, _ = _make_capturing_transport()
        server_config = _make_server_config(_subscription_config())
        with patch(
            "code_indexer.server.startup.codex_cli_startup._make_transport",
            return_value=transport,
        ):
            shutdown_fn = initialize_codex_manager_on_startup(
                server_config=server_config,
                server_data_dir=str(tmp_path),
                return_shutdown_hook=True,
            )
        auth_path = tmp_path / "codex-home" / "auth.json"
        assert auth_path.exists(), "auth.json must exist before shutdown"
        if callable(shutdown_fn):
            shutdown_fn()
        assert not auth_path.exists(), "auth.json must be removed by shutdown hook"


# ---------------------------------------------------------------------------
# Validation — entry-point checks
# ---------------------------------------------------------------------------


class TestValidation:
    def test_raises_on_none_server_config(self, tmp_path):
        """server_config=None must raise ValueError immediately."""
        with pytest.raises(ValueError, match="server_config"):
            initialize_codex_manager_on_startup(
                server_config=None,
                server_data_dir=str(tmp_path),
            )

    def test_raises_on_empty_server_data_dir(self, tmp_path):
        """Empty server_data_dir must raise ValueError immediately."""
        server_config = _make_server_config(_api_key_config())
        with pytest.raises(ValueError, match="server_data_dir"):
            initialize_codex_manager_on_startup(
                server_config=server_config,
                server_data_dir="",
            )

    def test_raises_on_whitespace_server_data_dir(self, tmp_path):
        """Whitespace-only server_data_dir must raise ValueError."""
        server_config = _make_server_config(_api_key_config())
        with pytest.raises(ValueError, match="server_data_dir"):
            initialize_codex_manager_on_startup(
                server_config=server_config,
                server_data_dir="   ",
            )


# ---------------------------------------------------------------------------
# Validation — mode-specific config argument checks
# ---------------------------------------------------------------------------


class TestModeValidation:
    def test_raises_on_empty_api_key_in_api_key_mode(self, tmp_path):
        """api_key mode with empty api_key must raise ValueError."""
        bad_config = CodexIntegrationConfig(
            enabled=True, credential_mode="api_key", api_key=""
        )
        server_config = _make_server_config(bad_config)
        with pytest.raises(ValueError, match="api_key"):
            initialize_codex_manager_on_startup(
                server_config=server_config,
                server_data_dir=str(tmp_path),
            )

    def test_raises_on_empty_lcp_url_in_subscription_mode(self, tmp_path):
        """subscription mode with empty lcp_url must raise ValueError."""
        bad_config = CodexIntegrationConfig(
            enabled=True,
            credential_mode="subscription",
            lcp_url="",
            api_key=TEST_LCP_API_KEY,
        )
        server_config = _make_server_config(bad_config)
        with pytest.raises(ValueError, match="lcp_url"):
            initialize_codex_manager_on_startup(
                server_config=server_config,
                server_data_dir=str(tmp_path),
            )

    def test_raises_on_empty_lcp_api_key_in_subscription_mode(self, tmp_path):
        """subscription mode with empty lcp api_key must raise ValueError."""
        bad_config = CodexIntegrationConfig(
            enabled=True,
            credential_mode="subscription",
            lcp_url=TEST_LCP_URL,
            api_key="",
        )
        server_config = _make_server_config(bad_config)
        with pytest.raises(ValueError, match="api_key"):
            initialize_codex_manager_on_startup(
                server_config=server_config,
                server_data_dir=str(tmp_path),
            )


# ---------------------------------------------------------------------------
# CRIT-3 regression — state manager filename wiring
# ---------------------------------------------------------------------------


class TestStateMgrFilenameWiring:
    def test_codex_startup_state_mgr_uses_codex_state_filename(self, tmp_path):
        """CRIT-3 regression: codex startup must construct LlmLeaseStateManager
        with state_filename='codex_lease_state.json' to avoid colliding with the
        Claude lease loop on the default 'llm_lease_state.json'."""
        from code_indexer.server.services.codex_lease_loop import _CODEX_STATE_FILENAME

        transport, _ = _make_capturing_transport()
        server_config = _make_server_config(_subscription_config())
        with patch(
            "code_indexer.server.startup.codex_cli_startup._make_transport",
            return_value=transport,
        ):
            initialize_codex_manager_on_startup(
                server_config=server_config,
                server_data_dir=str(tmp_path),
            )

        codex_state_file = tmp_path / _CODEX_STATE_FILENAME
        claude_state_file = tmp_path / "llm_lease_state.json"

        assert codex_state_file.exists(), (
            f"Expected Codex state file '{_CODEX_STATE_FILENAME}' to be written to "
            f"{tmp_path}, but it was not found. "
            "Production startup is using the wrong (default) state filename."
        )
        assert not claude_state_file.exists(), (
            "Codex startup must NOT write to the default 'llm_lease_state.json' "
            "(that file belongs to the Claude lease loop)."
        )
