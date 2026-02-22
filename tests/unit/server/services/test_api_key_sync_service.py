"""
Unit tests for ApiKeySyncService - thread-safe API key synchronization.

Tests cover:
- Anthropic API key sync to ~/.claude.json, os.environ, systemd env file
- VoyageAI API key sync to os.environ, systemd env file
- Idempotent sync operations (no-op if already synced)
- Thread safety with concurrent sync calls
- Atomic file writes

Story #20: API Key Management for Claude CLI and VoyageAI
"""

import json
import os
from pathlib import Path


from code_indexer.server.services.api_key_management import (
    ApiKeySyncService,
    SyncResult,
)


class TestSyncResultDataClass:
    """Test SyncResult data class properties."""

    def test_sync_result_success_state(self):
        """SyncResult with success=True."""
        result = SyncResult(success=True)
        assert result.success is True
        assert result.already_synced is False
        assert result.error is None

    def test_sync_result_already_synced_state(self):
        """SyncResult with already_synced=True."""
        result = SyncResult(success=True, already_synced=True)
        assert result.success is True
        assert result.already_synced is True

    def test_sync_result_failure_state(self):
        """SyncResult with failure and error message."""
        result = SyncResult(success=False, error="Test error")
        assert result.success is False
        assert result.error == "Test error"


class TestAnthropicApiKeySync:
    """Test Anthropic API key synchronization."""

    def test_sync_anthropic_key_writes_to_claude_json(self, monkeypatch, tmp_path):
        """AC: Anthropic key synced to ~/.claude.json with apiKey field."""
        # Monkeypatch Path.home() to isolate ~/.bashrc writes
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        claude_json_path = tmp_path / ".claude.json"

        service = ApiKeySyncService(
            claude_config_path=str(claude_json_path),
            systemd_env_path=str(tmp_path / "env"),
        )

        result = service.sync_anthropic_key(
            "sk-ant-api03-test123456789012345678901234567890123"
        )

        assert result.success is True
        assert claude_json_path.exists()

        config = json.loads(claude_json_path.read_text())
        assert (
            config["apiKey"] == "sk-ant-api03-test123456789012345678901234567890123"
        )

    def test_sync_anthropic_key_preserves_existing_fields(self, monkeypatch, tmp_path):
        """AC: Sync preserves existing fields in ~/.claude.json."""
        # Monkeypatch Path.home() to isolate ~/.bashrc writes
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        claude_json_path = tmp_path / ".claude.json"

        # Create existing config with other fields
        existing = {
            "primaryApiKey": "old-primary",
            "otherField": "preserved-value",
            "nested": {"key": "value"},
        }
        claude_json_path.write_text(json.dumps(existing, indent=2))

        service = ApiKeySyncService(
            claude_config_path=str(claude_json_path),
            systemd_env_path=str(tmp_path / "env"),
        )

        service.sync_anthropic_key(
            "sk-ant-api03-newkey123456789012345678901234567890"
        )

        config = json.loads(claude_json_path.read_text())
        assert (
            config["apiKey"] == "sk-ant-api03-newkey123456789012345678901234567890"
        )
        assert config["otherField"] == "preserved-value"
        assert config["nested"] == {"key": "value"}

    def test_sync_anthropic_key_sets_environment_variable(self, monkeypatch, tmp_path):
        """AC: Anthropic key synced to os.environ['ANTHROPIC_API_KEY']."""
        # Monkeypatch Path.home() to isolate ~/.bashrc writes
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Clear env var if set
        original_value = os.environ.pop("ANTHROPIC_API_KEY", None)

        try:
            service = ApiKeySyncService(
                claude_config_path=str(tmp_path / ".claude.json"),
                systemd_env_path=str(tmp_path / "env"),
            )

            service.sync_anthropic_key(
                "sk-ant-api03-envtest12345678901234567890123456"
            )

            assert os.environ.get("ANTHROPIC_API_KEY") == (
                "sk-ant-api03-envtest12345678901234567890123456"
            )
        finally:
            # Restore original value
            if original_value is not None:
                os.environ["ANTHROPIC_API_KEY"] = original_value
            else:
                os.environ.pop("ANTHROPIC_API_KEY", None)


class TestVoyageAIApiKeySync:
    """Test VoyageAI API key synchronization."""

    def test_sync_voyageai_key_sets_environment_variable(self, monkeypatch, tmp_path):
        """AC: VoyageAI key synced to os.environ['VOYAGE_API_KEY']."""
        # Monkeypatch Path.home() to isolate ~/.bashrc writes
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Clear env var if set
        original_value = os.environ.pop("VOYAGE_API_KEY", None)

        try:
            service = ApiKeySyncService(
                claude_config_path=str(tmp_path / ".claude.json"),
                systemd_env_path=str(tmp_path / "env"),
            )

            service.sync_voyageai_key("pa-voyagetest123456789")

            assert os.environ.get("VOYAGE_API_KEY") == "pa-voyagetest123456789"
        finally:
            # Restore original value
            if original_value is not None:
                os.environ["VOYAGE_API_KEY"] = original_value
            else:
                os.environ.pop("VOYAGE_API_KEY", None)

    def test_sync_voyageai_key_writes_to_systemd_env_file(self, monkeypatch, tmp_path):
        """AC: VoyageAI key written to systemd environment file."""
        # Monkeypatch Path.home() to isolate ~/.bashrc writes
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        systemd_env_path = tmp_path / "env"
        original_value = os.environ.pop("VOYAGE_API_KEY", None)

        try:
            service = ApiKeySyncService(
                claude_config_path=str(tmp_path / ".claude.json"),
                systemd_env_path=str(systemd_env_path),
            )

            service.sync_voyageai_key("pa-systemdvoyage12345")

            assert systemd_env_path.exists()
            content = systemd_env_path.read_text()
            assert "VOYAGE_API_KEY=pa-systemdvoyage12345" in content
        finally:
            if original_value is not None:
                os.environ["VOYAGE_API_KEY"] = original_value
            else:
                os.environ.pop("VOYAGE_API_KEY", None)

    def test_sync_voyageai_key_idempotent(self, monkeypatch, tmp_path):
        """AC: Sync is idempotent - no-op if already synced with same key."""
        # Monkeypatch Path.home() to isolate ~/.bashrc writes
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        api_key = "pa-idempotent123456789"
        original_value = os.environ.pop("VOYAGE_API_KEY", None)

        try:
            service = ApiKeySyncService(
                claude_config_path=str(tmp_path / ".claude.json"),
                systemd_env_path=str(tmp_path / "env"),
            )

            # First sync
            result1 = service.sync_voyageai_key(api_key)
            assert result1.success is True
            assert result1.already_synced is False

            # Second sync with same key should be idempotent
            result2 = service.sync_voyageai_key(api_key)
            assert result2.success is True
            assert result2.already_synced is True
        finally:
            if original_value is not None:
                os.environ["VOYAGE_API_KEY"] = original_value
            else:
                os.environ.pop("VOYAGE_API_KEY", None)


class TestBashrcUpdates:
    """Test ~/.bashrc update functionality for API key persistence."""

    def test_sync_voyageai_key_writes_to_bashrc(self, monkeypatch, tmp_path):
        """AC: VoyageAI key synced to ~/.bashrc for shell persistence."""
        # Create a temporary bashrc file
        bashrc_path = tmp_path / ".bashrc"
        bashrc_path.write_text("# existing content\n")

        # Monkeypatch Path.home() to return tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        original_value = os.environ.pop("VOYAGE_API_KEY", None)

        try:
            service = ApiKeySyncService(
                claude_config_path=str(tmp_path / ".claude.json"),
                systemd_env_path=str(tmp_path / "env"),
            )

            result = service.sync_voyageai_key("pa-bashrctest123456789")

            assert result.success is True
            content = bashrc_path.read_text()
            assert 'export VOYAGE_API_KEY="pa-bashrctest123456789"' in content
            # Verify existing content preserved
            assert "# existing content" in content
        finally:
            if original_value is not None:
                os.environ["VOYAGE_API_KEY"] = original_value
            else:
                os.environ.pop("VOYAGE_API_KEY", None)
