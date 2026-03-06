"""
Unit tests for ApiKeySyncService subscription mode guard (Story #366).

Verifies that sync_anthropic_key() returns a no-op SyncResult when
the server is in subscription mode, and continues normal behavior
in api_key mode.

The mode guard reads from the live ConfigService singleton. We patch
the config by temporarily setting the claude_auth_mode field on
the in-memory config object returned by get_config_service().
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.api_key_management import ApiKeySyncService, SyncResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sync_service(tmp_path: Path) -> ApiKeySyncService:
    """Create ApiKeySyncService with tmp_path to avoid writing real files."""
    return ApiKeySyncService(
        claude_config_path=str(tmp_path / "claude.json"),
        systemd_env_path=str(tmp_path / "env"),
    )


def _make_subscription_config():
    """Create a mock config whose claude_integration_config has subscription mode."""
    claude_cfg = MagicMock()
    claude_cfg.claude_auth_mode = "subscription"

    config = MagicMock()
    config.claude_integration_config = claude_cfg
    return config


def _make_api_key_config():
    """Create a mock config whose claude_integration_config has api_key mode."""
    claude_cfg = MagicMock()
    claude_cfg.claude_auth_mode = "api_key"

    config = MagicMock()
    config.claude_integration_config = claude_cfg
    return config


# ---------------------------------------------------------------------------
# TestSubscriptionModeGuard
# ---------------------------------------------------------------------------


class TestSubscriptionModeGuard:
    """sync_anthropic_key() must be a no-op in subscription mode."""

    def test_returns_success_true_in_subscription_mode(self, tmp_path):
        svc = _make_sync_service(tmp_path)
        mock_config_svc = MagicMock()
        mock_config_svc.get_config.return_value = _make_subscription_config()

        with patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=mock_config_svc,
        ):
            result = svc.sync_anthropic_key("sk-ant-api03-some-key")

        assert result.success is True

    def test_returns_already_synced_true_in_subscription_mode(self, tmp_path):
        svc = _make_sync_service(tmp_path)
        mock_config_svc = MagicMock()
        mock_config_svc.get_config.return_value = _make_subscription_config()

        with patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=mock_config_svc,
        ):
            result = svc.sync_anthropic_key("sk-ant-api03-some-key")

        assert result.already_synced is True

    def test_no_file_written_in_subscription_mode(self, tmp_path):
        """Subscription mode guard must not touch ~/.claude.json."""
        claude_json = tmp_path / "claude.json"
        svc = _make_sync_service(tmp_path)
        mock_config_svc = MagicMock()
        mock_config_svc.get_config.return_value = _make_subscription_config()

        with patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=mock_config_svc,
        ):
            svc.sync_anthropic_key("sk-ant-api03-some-key")

        assert not claude_json.exists()


# ---------------------------------------------------------------------------
# TestApiKeyModeNormalBehavior
# ---------------------------------------------------------------------------


class TestApiKeyModeNormalBehavior:
    """sync_anthropic_key() continues normal behavior in api_key mode."""

    def test_returns_success_true_in_api_key_mode(self, tmp_path):
        svc = _make_sync_service(tmp_path)
        mock_config_svc = MagicMock()
        mock_config_svc.get_config.return_value = _make_api_key_config()

        with patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=mock_config_svc,
        ):
            result = svc.sync_anthropic_key("sk-ant-api03-valid-key")

        assert result.success is True

    def test_writes_claude_json_in_api_key_mode(self, tmp_path):
        """In api_key mode, the key must be written to claude.json."""
        claude_json = tmp_path / "claude.json"
        svc = _make_sync_service(tmp_path)
        mock_config_svc = MagicMock()
        mock_config_svc.get_config.return_value = _make_api_key_config()

        with patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=mock_config_svc,
        ):
            svc.sync_anthropic_key("sk-ant-api03-valid-key")

        assert claude_json.exists()
