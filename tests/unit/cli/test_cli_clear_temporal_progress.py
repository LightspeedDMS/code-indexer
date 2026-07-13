"""
Test that CLI --clear flag removes temporal progress tracking file.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from click.testing import CliRunner

from src.code_indexer.cli import cli


class TestCLIClearTemporalProgress(unittest.TestCase):
    """Test that --clear flag properly cleans up temporal progress file."""

    def setUp(self):
        """Create temporary directory for testing."""
        self.temp_dir = tempfile.mkdtemp()
        self.project_dir = Path(self.temp_dir) / "test_project"
        self.project_dir.mkdir(parents=True, exist_ok=True)

        # Create git repository
        import subprocess

        subprocess.run(["git", "init"], cwd=self.project_dir, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"], cwd=self.project_dir
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"], cwd=self.project_dir
        )

        # Create a test file and commit
        test_file = self.project_dir / "test.py"
        test_file.write_text("def test():\n    pass")
        subprocess.run(["git", "add", "."], cwd=self.project_dir)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.project_dir)

    def tearDown(self):
        """Clean up temporary directory."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_clear_flag_removes_temporal_progress_file(self):
        """
        Test that --clear flag removes temporal_progress.json when used with --index-commits.

        This ensures that when users want a fresh temporal index, all progress
        tracking is also cleared to avoid inconsistencies.
        """
        # Create the temporal progress file manually.
        #
        # Current on-disk convention (post Story #1171 quarterly sharding):
        # temporal bookkeeping files live directly inside an embedder-named
        # collection directory under .code-indexer/index/ (e.g.
        # "code-indexer-temporal-voyage_context_4"), NOT under an
        # intermediate "temporal/" subdirectory. clear_all_temporal_collections()
        # (temporal_collection_naming.py) iterates index_dir.iterdir() directly
        # and matches subdirs via is_temporal_collection(), so the fixture must
        # use a "code-indexer-temporal-*" prefixed directory name to fall
        # within scope of the real removal logic.
        temporal_dir = (
            self.project_dir
            / ".code-indexer/index/code-indexer-temporal-voyage_context_4"
        )
        temporal_dir.mkdir(parents=True, exist_ok=True)

        progress_file = temporal_dir / "temporal_progress.json"
        progress_data = {
            "completed_commits": ["commit1", "commit2"],
            "status": "in_progress",
        }
        with open(progress_file, "w") as f:
            json.dump(progress_data, f)

        # Also create temporal_meta.json to simulate existing temporal index
        meta_file = temporal_dir / "temporal_meta.json"
        meta_data = {"last_commit": "commit2"}
        with open(meta_file, "w") as f:
            json.dump(meta_data, f)

        # Verify files exist
        self.assertTrue(
            progress_file.exists(), "Progress file should exist before clear"
        )
        self.assertTrue(meta_file.exists(), "Meta file should exist before clear")

        # CommandModeDetector checks for a REAL .code-indexer/config.json on
        # disk to gate the "index" command to "local" mode -- it does not go
        # through the mocked ConfigManager below. Without this, the CLI
        # rejects the command as "uninitialized" before ever reaching the
        # mocked --clear logic.
        config_file = self.project_dir / ".code-indexer" / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "codebase_dir": str(self.project_dir),
                    "embedding_provider": "voyage-ai",
                }
            )
        )

        runner = CliRunner()

        # Mock the necessary components
        with patch("src.code_indexer.cli.ConfigManager") as MockConfig:
            with patch(
                "src.code_indexer.storage.filesystem_vector_store.FilesystemVectorStore"
            ) as MockVectorStore:
                with patch(
                    "src.code_indexer.services.temporal.temporal_indexer.TemporalIndexer"
                ) as MockTemporal:
                    # Setup mocks
                    mock_config = MagicMock()
                    mock_config.codebase_dir = self.project_dir
                    mock_config.embedding_provider = "voyage-ai"
                    MockConfig.create_with_backtrack.return_value.get_config.return_value = mock_config

                    mock_vector_store = MagicMock()
                    MockVectorStore.return_value = mock_vector_store
                    mock_vector_store.clear_collection.return_value = True

                    # Mock temporal indexer to avoid actual indexing
                    mock_temporal = MagicMock()
                    MockTemporal.return_value = mock_temporal
                    mock_temporal.index_commits.return_value = MagicMock(
                        total_commits=0,
                        files_processed=0,
                        vectors_created=0,
                        skip_ratio=1.0,
                        branches_indexed=[],
                        commits_per_branch={},
                    )

                    # Run the command with --clear and --index-commits.
                    # click's CliRunner.invoke() does not accept a "cwd" kwarg
                    # in the installed click version -- passing one silently
                    # raised TypeError inside catch_exceptions=True, so the
                    # CLI command never actually ran. Use os.chdir() around
                    # the invoke instead (matches the established pattern in
                    # tests/unit/cli/test_cli_temporal_initialization_bug.py).
                    old_cwd = os.getcwd()
                    os.chdir(str(self.project_dir))
                    try:
                        result = runner.invoke(
                            cli,
                            ["index", "--index-commits", "--clear"],
                        )
                    finally:
                        os.chdir(old_cwd)

                    # Check that the command succeeded
                    if result.exit_code != 0:
                        print(f"Command output: {result.output}")

                    # Verify that temporal_meta.json was removed (existing behavior)
                    self.assertFalse(
                        meta_file.exists(), "Meta file should be removed after clear"
                    )

                    # Verify that temporal_progress.json was also removed (Bug #8 fix)
                    # This will FAIL because we haven't implemented this yet
                    self.assertFalse(
                        progress_file.exists(),
                        "Progress file should be removed after clear to ensure clean restart",
                    )


if __name__ == "__main__":
    unittest.main()
