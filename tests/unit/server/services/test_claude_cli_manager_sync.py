"""
Unit tests for ClaudeCliManager pre-use sync with ApiKeySyncService.

Tests cover:
- Pre-use sync triggers before CLI invocations
- Integration with ApiKeySyncService for API key sync
- Ensuring sync happens before each CLI invocation

Story #20: API Key Management for Claude CLI and VoyageAI
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_home_dir(monkeypatch, tmp_path):
    """
    Fixture to mock Path.home() and backup real credentials.

    This fixture:
    1. Backs up real ~/.claude/.credentials.json if it exists
    2. Backs up real ~/.claude.json if it exists
    3. Backs up real ~/.bashrc if it exists
    4. Mocks Path.home() to return a temp directory
    5. Restores all backups in teardown

    This prevents tests from deleting or modifying real user credentials.
    """
    # Get real home directory BEFORE mocking
    real_home = Path(os.path.expanduser("~"))

    # Backup real credentials if they exist
    creds_path = real_home / ".claude" / ".credentials.json"
    creds_backup = None
    if creds_path.exists():
        creds_backup = creds_path.read_bytes()

    claude_json_path = real_home / ".claude.json"
    claude_json_backup = None
    if claude_json_path.exists():
        claude_json_backup = claude_json_path.read_bytes()

    bashrc_path = real_home / ".bashrc"
    bashrc_backup = None
    if bashrc_path.exists():
        bashrc_backup = bashrc_path.read_bytes()

    # Mock Path.home() to use tmp_path
    def mock_home():
        return tmp_path

    monkeypatch.setattr(Path, "home", staticmethod(mock_home))

    # Create .claude directory in tmp_path
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)

    yield tmp_path

    # Restore real credentials if they were backed up
    # Must use os.path.expanduser since Path.home is still mocked during teardown
    real_home_str = os.path.expanduser("~")
    real_home_path = Path(real_home_str)

    if creds_backup is not None:
        restore_creds_path = real_home_path / ".claude" / ".credentials.json"
        restore_creds_path.parent.mkdir(parents=True, exist_ok=True)
        restore_creds_path.write_bytes(creds_backup)

    if claude_json_backup is not None:
        restore_claude_json_path = real_home_path / ".claude.json"
        restore_claude_json_path.write_bytes(claude_json_backup)

    if bashrc_backup is not None:
        restore_bashrc_path = real_home_path / ".bashrc"
        restore_bashrc_path.write_bytes(bashrc_backup)


class TestClaudeCliManagerPreUseSync:
    """Test pre-use sync triggers in ClaudeCliManager."""

    def test_ensure_api_key_synced_method_exists(self, mock_home_dir):
        """AC: ClaudeCliManager has _ensure_api_key_synced method."""
        from code_indexer.server.services.claude_cli_manager import ClaudeCliManager

        manager = ClaudeCliManager(api_key=None, max_workers=1)
        try:
            assert hasattr(manager, "_ensure_api_key_synced")
            assert callable(manager._ensure_api_key_synced)
        finally:
            manager.shutdown(timeout=1.0)

    def test_ensure_api_key_synced_calls_sync_service(self, mock_home_dir):
        """AC: _ensure_api_key_synced uses ApiKeySyncService."""
        from code_indexer.server.services.claude_cli_manager import ClaudeCliManager

        # Clear env var if set
        original_value = os.environ.pop("ANTHROPIC_API_KEY", None)

        try:
            test_key = "sk-ant-api03-preuse12345678901234567890123456"

            manager = ClaudeCliManager(api_key=test_key, max_workers=1)
            try:
                # Call the sync method
                manager._ensure_api_key_synced()

                # Verify key is synced to environment
                assert os.environ.get("ANTHROPIC_API_KEY") == test_key
            finally:
                manager.shutdown(timeout=1.0)
        finally:
            if original_value is not None:
                os.environ["ANTHROPIC_API_KEY"] = original_value
            else:
                os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_process_work_calls_ensure_synced(self, mock_home_dir):
        """AC: _process_work calls _ensure_api_key_synced before CLI invocation."""
        from code_indexer.server.services.claude_cli_manager import ClaudeCliManager

        original_value = os.environ.pop("ANTHROPIC_API_KEY", None)

        try:
            test_key = "sk-ant-api03-processwork1234567890123456"
            manager = ClaudeCliManager(api_key=test_key, max_workers=1)

            try:
                # Mock check_cli_available to return False to avoid actual CLI call
                manager.check_cli_available = MagicMock(return_value=False)

                # Track if _ensure_api_key_synced was called
                original_ensure = manager._ensure_api_key_synced
                ensure_called = []

                def tracking_ensure():
                    ensure_called.append(True)
                    return original_ensure()

                manager._ensure_api_key_synced = tracking_ensure

                # Call _process_work - use mock_home_dir as tmpdir
                callback = MagicMock()
                manager._process_work(mock_home_dir, callback)

                # Verify _ensure_api_key_synced was called
                assert len(ensure_called) > 0, (
                    "_ensure_api_key_synced should be called during _process_work"
                )
            finally:
                manager.shutdown(timeout=1.0)
        finally:
            if original_value is not None:
                os.environ["ANTHROPIC_API_KEY"] = original_value
            else:
                os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_sync_api_key_uses_sync_service(self, mock_home_dir):
        """AC: sync_api_key method delegates to ApiKeySyncService."""
        from code_indexer.server.services.claude_cli_manager import ClaudeCliManager

        original_value = os.environ.pop("ANTHROPIC_API_KEY", None)

        try:
            test_key = "sk-ant-api03-delegate12345678901234567890"
            manager = ClaudeCliManager(api_key=test_key, max_workers=1)

            try:
                # Call sync_api_key
                manager.sync_api_key()

                # Verify key is synced to environment (via ApiKeySyncService)
                assert os.environ.get("ANTHROPIC_API_KEY") == test_key
            finally:
                manager.shutdown(timeout=1.0)
        finally:
            if original_value is not None:
                os.environ["ANTHROPIC_API_KEY"] = original_value
            else:
                os.environ.pop("ANTHROPIC_API_KEY", None)
