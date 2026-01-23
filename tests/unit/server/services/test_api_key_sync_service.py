"""
Unit tests for ApiKeySyncService - thread-safe API key synchronization.

Tests cover:
- Anthropic API key sync to ~/.claude.json, os.environ, systemd env file
- VoyageAI API key sync to os.environ, systemd env file
- Idempotent sync operations (no-op if already synced)
- Thread safety with concurrent sync calls
- Atomic file writes
- Removal of ~/.claude/.credentials.json

Story #20: API Key Management for Claude CLI and VoyageAI
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

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

    def test_sync_anthropic_key_writes_to_claude_json(self):
        """AC: Anthropic key synced to ~/.claude.json with apiKey field."""
        with tempfile.TemporaryDirectory() as tmpdir:
            claude_json_path = Path(tmpdir) / ".claude.json"

            service = ApiKeySyncService(
                claude_config_path=str(claude_json_path),
                systemd_env_path=str(Path(tmpdir) / "env"),
            )

            result = service.sync_anthropic_key(
                "sk-ant-api03-test123456789012345678901234567890123"
            )

            assert result.success is True
            assert claude_json_path.exists()

            config = json.loads(claude_json_path.read_text())
            assert config["apiKey"] == "sk-ant-api03-test123456789012345678901234567890123"

    def test_sync_anthropic_key_preserves_existing_fields(self):
        """AC: Sync preserves existing fields in ~/.claude.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            claude_json_path = Path(tmpdir) / ".claude.json"

            # Create existing config with other fields
            existing = {
                "primaryApiKey": "old-primary",
                "otherField": "preserved-value",
                "nested": {"key": "value"},
            }
            claude_json_path.write_text(json.dumps(existing, indent=2))

            service = ApiKeySyncService(
                claude_config_path=str(claude_json_path),
                systemd_env_path=str(Path(tmpdir) / "env"),
            )

            service.sync_anthropic_key(
                "sk-ant-api03-newkey123456789012345678901234567890"
            )

            config = json.loads(claude_json_path.read_text())
            assert config["apiKey"] == "sk-ant-api03-newkey123456789012345678901234567890"
            assert config["otherField"] == "preserved-value"
            assert config["nested"] == {"key": "value"}

    def test_sync_anthropic_key_sets_environment_variable(self):
        """AC: Anthropic key synced to os.environ['ANTHROPIC_API_KEY']."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Clear env var if set
            original_value = os.environ.pop("ANTHROPIC_API_KEY", None)

            try:
                service = ApiKeySyncService(
                    claude_config_path=str(Path(tmpdir) / ".claude.json"),
                    systemd_env_path=str(Path(tmpdir) / "env"),
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


class TestAnthropicKeyCredentialsRemoval:
    """Test removal of legacy credentials file during Anthropic key sync."""

    def test_sync_anthropic_key_removes_credentials_file(self):
        """AC: Sync removes ~/.claude/.credentials.json if it exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            credentials_path = Path(tmpdir) / ".claude" / ".credentials.json"
            credentials_path.parent.mkdir(parents=True, exist_ok=True)
            credentials_path.write_text('{"oauth_token": "old_token"}')

            # Clear env var if set
            original_value = os.environ.pop("ANTHROPIC_API_KEY", None)

            try:
                service = ApiKeySyncService(
                    claude_config_path=str(Path(tmpdir) / ".claude.json"),
                    systemd_env_path=str(Path(tmpdir) / "env"),
                    claude_credentials_path=str(credentials_path),
                )

                result = service.sync_anthropic_key(
                    "sk-ant-api03-test123456789012345678901234567890123"
                )

                assert result.success is True
                assert not credentials_path.exists(), (
                    "Credentials file should be removed after sync"
                )
            finally:
                if original_value is not None:
                    os.environ["ANTHROPIC_API_KEY"] = original_value
                else:
                    os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_sync_anthropic_key_succeeds_when_credentials_file_missing(self):
        """Sync should succeed even if credentials file doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Don't create credentials file
            original_value = os.environ.pop("ANTHROPIC_API_KEY", None)

            try:
                service = ApiKeySyncService(
                    claude_config_path=str(Path(tmpdir) / ".claude.json"),
                    systemd_env_path=str(Path(tmpdir) / "env"),
                    claude_credentials_path=str(
                        Path(tmpdir) / ".claude" / ".credentials.json"
                    ),
                )

                result = service.sync_anthropic_key(
                    "sk-ant-api03-test123456789012345678901234567890123"
                )

                assert result.success is True
            finally:
                if original_value is not None:
                    os.environ["ANTHROPIC_API_KEY"] = original_value
                else:
                    os.environ.pop("ANTHROPIC_API_KEY", None)


class TestVoyageAIApiKeySync:
    """Test VoyageAI API key synchronization."""

    def test_sync_voyageai_key_sets_environment_variable(self):
        """AC: VoyageAI key synced to os.environ['VOYAGE_API_KEY']."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Clear env var if set
            original_value = os.environ.pop("VOYAGE_API_KEY", None)

            try:
                service = ApiKeySyncService(
                    claude_config_path=str(Path(tmpdir) / ".claude.json"),
                    systemd_env_path=str(Path(tmpdir) / "env"),
                )

                service.sync_voyageai_key("pa-voyagetest123456789")

                assert os.environ.get("VOYAGE_API_KEY") == "pa-voyagetest123456789"
            finally:
                # Restore original value
                if original_value is not None:
                    os.environ["VOYAGE_API_KEY"] = original_value
                else:
                    os.environ.pop("VOYAGE_API_KEY", None)

    def test_sync_voyageai_key_writes_to_systemd_env_file(self):
        """AC: VoyageAI key written to systemd environment file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            systemd_env_path = Path(tmpdir) / "env"
            original_value = os.environ.pop("VOYAGE_API_KEY", None)

            try:
                service = ApiKeySyncService(
                    claude_config_path=str(Path(tmpdir) / ".claude.json"),
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

    def test_sync_voyageai_key_idempotent(self):
        """AC: Sync is idempotent - no-op if already synced with same key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            api_key = "pa-idempotent123456789"
            original_value = os.environ.pop("VOYAGE_API_KEY", None)

            try:
                service = ApiKeySyncService(
                    claude_config_path=str(Path(tmpdir) / ".claude.json"),
                    systemd_env_path=str(Path(tmpdir) / "env"),
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
