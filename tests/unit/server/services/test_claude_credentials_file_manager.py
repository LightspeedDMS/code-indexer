"""
Unit tests for ClaudeCredentialsFileManager (Story #365).

Uses tmp_path fixture for full test isolation.
"""

import json
import time
from pathlib import Path

import pytest

from code_indexer.server.services.claude_credentials_file_manager import (
    ClaudeCredentialsFileManager,
)


# ---------------------------------------------------------------------------
# write_credentials()
# ---------------------------------------------------------------------------

class TestWriteCredentials:
    def test_write_creates_file(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        manager.write_credentials(
            access_token="sk-ant-oat01-access",
            refresh_token="sk-ant-ort01-refresh",
        )
        assert creds_path.exists()

    def test_write_creates_correct_top_level_structure(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        manager.write_credentials(
            access_token="sk-ant-oat01-access",
            refresh_token="sk-ant-ort01-refresh",
        )
        data = json.loads(creds_path.read_text())
        assert "claudeAiOauth" in data

    def test_write_stores_access_token(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        manager.write_credentials(
            access_token="sk-ant-oat01-my-access",
            refresh_token="sk-ant-ort01-my-refresh",
        )
        data = json.loads(creds_path.read_text())
        assert data["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-my-access"

    def test_write_stores_refresh_token(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        manager.write_credentials(
            access_token="sk-ant-oat01-access",
            refresh_token="sk-ant-ort01-my-refresh",
        )
        data = json.loads(creds_path.read_text())
        assert data["claudeAiOauth"]["refreshToken"] == "sk-ant-ort01-my-refresh"

    def test_write_includes_expires_at_when_provided(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        manager.write_credentials(
            access_token="sk-ant-oat01-access",
            refresh_token="sk-ant-ort01-refresh",
            expires_at=1772755168455,
        )
        data = json.loads(creds_path.read_text())
        assert data["claudeAiOauth"]["expiresAt"] == 1772755168455

    def test_write_sets_default_expires_at_when_not_provided(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        before_ms = int(time.time() * 1000)
        manager.write_credentials(
            access_token="sk-ant-oat01-access",
            refresh_token="sk-ant-ort01-refresh",
        )
        after_ms = int(time.time() * 1000)

        data = json.loads(creds_path.read_text())
        expires_at = data["claudeAiOauth"]["expiresAt"]

        # Should be approximately now + 1 hour (3600000ms)
        one_hour_ms = 3600000
        assert expires_at >= before_ms + one_hour_ms
        assert expires_at <= after_ms + one_hour_ms + 1000  # 1s tolerance

    def test_write_includes_hardcoded_scopes(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        manager.write_credentials(
            access_token="sk-ant-oat01-access",
            refresh_token="sk-ant-ort01-refresh",
        )
        data = json.loads(creds_path.read_text())
        scopes = data["claudeAiOauth"]["scopes"]
        assert isinstance(scopes, list)
        assert "user:inference" in scopes
        assert "user:profile" in scopes

    def test_write_includes_subscription_type(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        manager.write_credentials(
            access_token="sk-ant-oat01-access",
            refresh_token="sk-ant-ort01-refresh",
        )
        data = json.loads(creds_path.read_text())
        assert data["claudeAiOauth"]["subscriptionType"] == "enterprise"

    def test_write_includes_rate_limit_tier(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        manager.write_credentials(
            access_token="sk-ant-oat01-access",
            refresh_token="sk-ant-ort01-refresh",
        )
        data = json.loads(creds_path.read_text())
        assert data["claudeAiOauth"]["rateLimitTier"] == "default_claude_max_5x"

    def test_write_sets_0o600_permissions(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        manager.write_credentials(
            access_token="sk-ant-oat01-access",
            refresh_token="sk-ant-ort01-refresh",
        )
        file_mode = creds_path.stat().st_mode & 0o777
        assert file_mode == 0o600, f"Expected 0o600, got {oct(file_mode)}"

    def test_write_creates_parent_directory_if_missing(self, tmp_path):
        parent = tmp_path / "missing_parent" / "deep_dir"
        creds_path = parent / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        manager.write_credentials(
            access_token="sk-ant-oat01-access",
            refresh_token="sk-ant-ort01-refresh",
        )
        assert creds_path.exists()

    def test_write_overwrites_existing_file(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        manager.write_credentials(
            access_token="old-access",
            refresh_token="old-refresh",
        )
        manager.write_credentials(
            access_token="new-access",
            refresh_token="new-refresh",
        )
        data = json.loads(creds_path.read_text())
        assert data["claudeAiOauth"]["accessToken"] == "new-access"
        assert data["claudeAiOauth"]["refreshToken"] == "new-refresh"


# ---------------------------------------------------------------------------
# read_credentials()
# ---------------------------------------------------------------------------

class TestReadCredentials:
    def test_read_returns_none_when_file_missing(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        result = manager.read_credentials()
        assert result is None

    def test_read_returns_access_token(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        manager.write_credentials(
            access_token="sk-ant-oat01-read-access",
            refresh_token="sk-ant-ort01-read-refresh",
        )
        result = manager.read_credentials()
        assert result is not None
        assert result["access_token"] == "sk-ant-oat01-read-access"

    def test_read_returns_refresh_token(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        manager.write_credentials(
            access_token="sk-ant-oat01-access",
            refresh_token="sk-ant-ort01-read-refresh",
        )
        result = manager.read_credentials()
        assert result is not None
        assert result["refresh_token"] == "sk-ant-ort01-read-refresh"

    def test_read_returns_none_on_missing_claude_ai_oauth_key(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        creds_path.write_text('{"someOtherKey": {}}')
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        result = manager.read_credentials()
        assert result is None

    def test_read_roundtrip_after_write(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        manager.write_credentials(
            access_token="sk-ant-oat01-roundtrip",
            refresh_token="sk-ant-ort01-roundtrip",
        )
        result = manager.read_credentials()
        assert result is not None
        assert result["access_token"] == "sk-ant-oat01-roundtrip"
        assert result["refresh_token"] == "sk-ant-ort01-roundtrip"


# ---------------------------------------------------------------------------
# delete_credentials()
# ---------------------------------------------------------------------------

class TestDeleteCredentials:
    def test_delete_removes_file(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        manager.write_credentials(
            access_token="sk-ant-oat01-access",
            refresh_token="sk-ant-ort01-refresh",
        )
        assert creds_path.exists()

        manager.delete_credentials()
        assert not creds_path.exists()

    def test_delete_when_file_missing_does_not_raise(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        # No file exists — should not raise
        manager.delete_credentials()

    def test_read_after_delete_returns_none(self, tmp_path):
        creds_path = tmp_path / ".credentials.json"
        manager = ClaudeCredentialsFileManager(credentials_path=creds_path)
        manager.write_credentials(
            access_token="sk-ant-oat01-access",
            refresh_token="sk-ant-ort01-refresh",
        )
        manager.delete_credentials()
        result = manager.read_credentials()
        assert result is None


# ---------------------------------------------------------------------------
# Default path
# ---------------------------------------------------------------------------

class TestDefaultPath:
    def test_default_path_is_claude_credentials_json(self):
        manager = ClaudeCredentialsFileManager()
        expected = Path.home() / ".claude" / ".credentials.json"
        assert manager.credentials_path == expected
