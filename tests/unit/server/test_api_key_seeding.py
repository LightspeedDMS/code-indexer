"""
Unit tests for seed_api_keys_on_startup() — config is single source of truth.

Verifies pure unidirectional behavior (config → env):
- Config has key  → os.environ receives the key
- Config is blank → os.environ key is actively CLEARED
- save_config is NEVER called (we never write to config)
- No subscription mode guard here (that lives in ApiKeySyncService)

Story: Remove bi-directional API key auto-seeding (Bug fix).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch


from code_indexer.server.startup.api_key_seeding import seed_api_keys_on_startup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config_service(
    anthropic_api_key: str = "",
    voyageai_api_key: str = "",
) -> MagicMock:
    """Build a minimal mock config_service for testing."""
    claude_cfg = MagicMock()
    claude_cfg.anthropic_api_key = anthropic_api_key
    claude_cfg.voyageai_api_key = voyageai_api_key

    config = MagicMock()
    config.claude_integration_config = claude_cfg

    config_service = MagicMock()
    config_service.get_config.return_value = config
    return config_service


def _make_sync_service(success: bool = True) -> MagicMock:
    """Build a mock ApiKeySyncService that returns success."""
    sync_result = MagicMock()
    sync_result.success = success

    svc = MagicMock()
    svc.sync_anthropic_key.return_value = sync_result
    svc.sync_voyageai_key.return_value = sync_result
    return svc


# ---------------------------------------------------------------------------
# TestBlankConfigClearsEnv
# ---------------------------------------------------------------------------


class TestBlankConfigClearsEnv:
    """When config is blank, corresponding env vars must be actively cleared."""

    def test_blank_config_clears_anthropic_env(self, tmp_path):
        """Config blank → ANTHROPIC_API_KEY removed from os.environ."""
        config_service = _make_config_service(anthropic_api_key="")
        mock_sync_svc = _make_sync_service()

        with (
            patch(
                "code_indexer.server.services.api_key_management.ApiKeySyncService",
                return_value=mock_sync_svc,
            ),
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-was-in-env"}),
        ):
            seed_api_keys_on_startup(
                config_service=config_service,
                claude_config_path=str(tmp_path / "claude.json"),
            )

        assert "ANTHROPIC_API_KEY" not in os.environ
        mock_sync_svc.sync_anthropic_key.assert_not_called()

    def test_blank_config_clears_voyageai_env(self, tmp_path):
        """Config blank → VOYAGE_API_KEY removed from os.environ."""
        config_service = _make_config_service(voyageai_api_key="")
        mock_sync_svc = _make_sync_service()

        # Assert inside the patch.dict scope so the real env value (if any)
        # is not restored yet — we verify what the function left in the env.
        with (
            patch(
                "code_indexer.server.services.api_key_management.ApiKeySyncService",
                return_value=mock_sync_svc,
            ),
            patch.dict(os.environ, {"VOYAGE_API_KEY": "pa-voyage-was-in-env"}),
        ):
            seed_api_keys_on_startup(
                config_service=config_service,
                claude_config_path=str(tmp_path / "claude.json"),
            )
            # Assert inside the scope: production code must have popped the key
            assert "VOYAGE_API_KEY" not in os.environ

    def test_blank_config_stays_blank_with_env_key_present(self, tmp_path):
        """Even if env has a key, blank config stays blank AND env key is cleared."""
        config_service = _make_config_service(anthropic_api_key="")
        mock_sync_svc = _make_sync_service()

        with (
            patch(
                "code_indexer.server.services.api_key_management.ApiKeySyncService",
                return_value=mock_sync_svc,
            ),
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-from-env-key"}),
        ):
            result = seed_api_keys_on_startup(
                config_service=config_service,
                claude_config_path=str(tmp_path / "claude.json"),
            )

        # Nothing seeded into config
        assert result["anthropic_seeded"] is False
        # sync must NOT have been called from blank config
        mock_sync_svc.sync_anthropic_key.assert_not_called()
        # env key must have been cleared (not left stale)
        assert "ANTHROPIC_API_KEY" not in os.environ
        # Config must not have been written
        config_service.config_manager.save_config.assert_not_called()

    def test_no_error_when_env_key_absent_and_config_blank(self, tmp_path):
        """Clearing an already-absent env var must not raise."""
        config_service = _make_config_service(
            anthropic_api_key="",
            voyageai_api_key="",
        )
        mock_sync_svc = _make_sync_service()

        env_copy = {
            k: v
            for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "VOYAGE_API_KEY")
        }
        with (
            patch(
                "code_indexer.server.services.api_key_management.ApiKeySyncService",
                return_value=mock_sync_svc,
            ),
            patch.dict(os.environ, env_copy, clear=True),
        ):
            result = seed_api_keys_on_startup(
                config_service=config_service,
                claude_config_path=str(tmp_path / "claude.json"),
            )

        assert result["anthropic_seeded"] is False
        assert result["voyageai_seeded"] is False


# ---------------------------------------------------------------------------
# TestConfigKeysSyncedToEnv
# ---------------------------------------------------------------------------


class TestConfigKeysSyncedToEnv:
    """Config keys must be synced one-way to os.environ."""

    def test_anthropic_config_key_synced_to_env(self, tmp_path):
        """When config has anthropic key, sync_anthropic_key() is called with it."""
        existing_key = "sk-ant-from-config"
        config_service = _make_config_service(anthropic_api_key=existing_key)
        mock_sync_svc = _make_sync_service()

        env_before = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with patch(
                "code_indexer.server.services.api_key_management.ApiKeySyncService",
                return_value=mock_sync_svc,
            ):
                seed_api_keys_on_startup(
                    config_service=config_service,
                    claude_config_path=str(tmp_path / "claude.json"),
                )

            mock_sync_svc.sync_anthropic_key.assert_called_once_with(existing_key)
        finally:
            if env_before is not None:
                os.environ["ANTHROPIC_API_KEY"] = env_before
            elif "ANTHROPIC_API_KEY" in os.environ:
                del os.environ["ANTHROPIC_API_KEY"]

    def test_voyageai_config_key_synced_to_env(self, tmp_path):
        """When config has voyageai key, os.environ[VOYAGE_API_KEY] is set."""
        voyage_key = "pa-voyage-from-config"
        config_service = _make_config_service(voyageai_api_key=voyage_key)
        mock_sync_svc = _make_sync_service()

        env_before = os.environ.pop("VOYAGE_API_KEY", None)
        try:
            with patch(
                "code_indexer.server.services.api_key_management.ApiKeySyncService",
                return_value=mock_sync_svc,
            ):
                seed_api_keys_on_startup(
                    config_service=config_service,
                    claude_config_path=str(tmp_path / "claude.json"),
                )

            assert os.environ.get("VOYAGE_API_KEY") == voyage_key
        finally:
            if env_before is not None:
                os.environ["VOYAGE_API_KEY"] = env_before
            elif "VOYAGE_API_KEY" in os.environ:
                del os.environ["VOYAGE_API_KEY"]

    def test_config_not_saved_on_startup(self, tmp_path):
        """save_config must NEVER be called — we never write back to config."""
        config_service = _make_config_service(
            anthropic_api_key="sk-ant-existing",
            voyageai_api_key="pa-voyage-existing",
        )
        mock_sync_svc = _make_sync_service()

        with patch(
            "code_indexer.server.services.api_key_management.ApiKeySyncService",
            return_value=mock_sync_svc,
        ):
            seed_api_keys_on_startup(
                config_service=config_service,
                claude_config_path=str(tmp_path / "claude.json"),
            )

        config_service.config_manager.save_config.assert_not_called()

    def test_result_dict_always_returns_false(self, tmp_path):
        """voyageai_seeded is True when key present (Bug #599 fix), anthropic always False."""
        config_service = _make_config_service(
            anthropic_api_key="sk-ant-existing",
            voyageai_api_key="pa-voyage-existing",
        )
        mock_sync_svc = _make_sync_service()

        with patch(
            "code_indexer.server.services.api_key_management.ApiKeySyncService",
            return_value=mock_sync_svc,
        ):
            result = seed_api_keys_on_startup(
                config_service=config_service,
                claude_config_path=str(tmp_path / "claude.json"),
            )

        assert result["anthropic_seeded"] is False
        assert result["voyageai_seeded"] is True
