"""
Unit tests for Story #224: C9 removal from description_refresh_scheduler.py.

C9: Remove the reindex_cidx_meta() call from _update_description_file()
    in DescriptionRefreshScheduler. RefreshScheduler handles indexing now.

Tests:
- test_reindex_removed_from_desc_scheduler: _update_description_file does not
  call cidx after updating a description file.
"""

from unittest.mock import MagicMock, patch


class TestReindexRemovedFromDescriptionRefreshScheduler:
    """C9: _update_description_file() must not call reindex_cidx_meta."""

    def test_reindex_removed_from_desc_scheduler(self, tmp_path):
        """
        _update_description_file() must NOT call reindex_cidx_meta after C9 removal.

        Previously this method imported and called reindex_cidx_meta() after
        writing the .md file content. After C9 removal that call is gone.

        Verification approach: patch subprocess.run to capture any cidx calls.
        If reindex_cidx_meta() is still called, it runs cidx index via subprocess.
        """
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )
        from code_indexer.server.utils.config_manager import (
            ServerConfig,
            ClaudeIntegrationConfig,
        )
        from code_indexer.server.storage.database_manager import DatabaseConnectionManager

        # Create minimal database with required schema
        db_file = tmp_path / "test.db"
        conn_manager = DatabaseConnectionManager(str(db_file))
        conn = conn_manager.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS description_refresh_tracking (
                repo_alias TEXT PRIMARY KEY,
                last_run TEXT,
                next_run TEXT,
                status TEXT DEFAULT 'pending',
                error TEXT,
                last_known_commit TEXT,
                last_known_files_processed INTEGER,
                last_known_indexed_at TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        conn.commit()
        conn_manager.close_all()

        config = ServerConfig(server_dir=str(tmp_path))
        config.claude_integration_config = ClaudeIntegrationConfig()
        config.claude_integration_config.description_refresh_enabled = True
        config.claude_integration_config.description_refresh_interval_hours = 24

        mock_config_manager = MagicMock()
        mock_config_manager.load_config.return_value = config

        # Create cidx-meta directory
        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()

        scheduler = DescriptionRefreshScheduler(
            db_path=str(db_file),
            config_manager=mock_config_manager,
        )
        scheduler._meta_dir = meta_dir

        cidx_calls = []

        def capture_subprocess(cmd, **kwargs):
            if isinstance(cmd, list) and "cidx" in cmd:
                cidx_calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            return result

        content = "# Test Repo\nDescription content."
        with patch("subprocess.run", side_effect=capture_subprocess):
            scheduler._update_description_file("test-repo", content)

        # Verify the .md file was written (core functionality preserved)
        md_file = meta_dir / "test-repo.md"
        assert md_file.exists(), "_update_description_file must still write the .md file"
        assert md_file.read_text() == content

        # Verify NO cidx calls were made
        assert cidx_calls == [], (
            "_update_description_file() must NOT call cidx after C9 removal. "
            f"Got cidx calls: {cidx_calls}"
        )
