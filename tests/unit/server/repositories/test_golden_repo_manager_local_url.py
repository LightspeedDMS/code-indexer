"""
Unit tests for GoldenRepoManager local:// URL auto-folder creation (Story #163 AC4).

Tests that golden_repo_manager automatically creates target folders for local://
URLs that point to non-existent paths.
"""

import pytest
import tempfile
import shutil
from pathlib import Path

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


class TestGoldenRepoManagerLocalURLAutoCreate:
    """Test cases for local:// URL auto-folder creation (AC4)."""

    def setup_method(self):
        """Set up test environment."""
        self.temp_dirs = []

    def teardown_method(self):
        """Clean up test directories."""
        for temp_dir in self.temp_dirs:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)

    def _create_temp_dir(self) -> Path:
        """Create a temporary directory and track it for cleanup."""
        temp_dir = Path(tempfile.mkdtemp())
        self.temp_dirs.append(temp_dir)
        return temp_dir

    def test_local_url_creates_folder_if_not_exists(self):
        """
        AC4: Given a local:// URL pointing to a non-existent folder,
        when GoldenRepoManager processes the registration,
        then it should automatically create the target folder
        and registration should succeed.
        """
        # Arrange
        base_dir = self._create_temp_dir()
        golden_repos_dir = base_dir / "golden_repos"
        golden_repos_dir.mkdir()

        target_folder_name = "my-local-repo"
        target_folder_path = golden_repos_dir / "repos" / target_folder_name

        # Ensure target folder does NOT exist initially
        assert not target_folder_path.exists()

        manager = GoldenRepoManager(str(golden_repos_dir))

        # Act - Register with local:// URL pointing to non-existent path
        # The _clone_or_copy_repo method should create the folder
        repo_url = f"local://{target_folder_name}"
        clone_path = golden_repos_dir / "repos" / target_folder_name

        # Call the method that handles local:// URLs
        result_path = manager._clone_local_repository_with_regular_copy(
            repo_url, str(clone_path)
        )

        # Assert
        assert Path(result_path).exists()
        assert Path(result_path) == clone_path
        assert clone_path.is_dir()

    def test_local_url_uses_existing_folder_if_exists(self):
        """
        AC4 Extension: If local:// folder already exists, use it without error.
        """
        # Arrange
        base_dir = self._create_temp_dir()
        golden_repos_dir = base_dir / "golden_repos"
        golden_repos_dir.mkdir()

        target_folder_name = "existing-local-repo"
        target_folder_path = golden_repos_dir / "repos" / target_folder_name

        # Create the folder and add a file to it
        target_folder_path.mkdir(parents=True)
        test_file = target_folder_path / "test.txt"
        test_file.write_text("existing content")

        manager = GoldenRepoManager(str(golden_repos_dir))

        # Act - Register with local:// URL pointing to existing path
        repo_url = f"local://{target_folder_name}"
        clone_path = golden_repos_dir / "repos" / target_folder_name

        result_path = manager._clone_local_repository_with_regular_copy(
            repo_url, str(clone_path)
        )

        # Assert
        assert Path(result_path).exists()
        assert Path(result_path) == clone_path
        assert (clone_path / "test.txt").exists()
        assert (clone_path / "test.txt").read_text() == "existing content"

    def test_file_url_still_works(self):
        """
        AC4 Regression: Ensure file:// URLs still work correctly.
        """
        # Arrange
        base_dir = self._create_temp_dir()
        golden_repos_dir = base_dir / "golden_repos"
        golden_repos_dir.mkdir()

        # Create source directory with content
        source_dir = base_dir / "source_repo"
        source_dir.mkdir()
        source_file = source_dir / "readme.txt"
        source_file.write_text("source content")

        manager = GoldenRepoManager(str(golden_repos_dir))

        # Act - Register with file:// URL
        repo_url = f"file://{source_dir}"
        clone_path = golden_repos_dir / "repos" / "copied-repo"

        result_path = manager._clone_local_repository_with_regular_copy(
            repo_url, str(clone_path)
        )

        # Assert - Should copy from source to target
        assert Path(result_path).exists()
        assert (clone_path / "readme.txt").exists()
        assert (clone_path / "readme.txt").read_text() == "source content"
