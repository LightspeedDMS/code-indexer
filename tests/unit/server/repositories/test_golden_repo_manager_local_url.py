"""
Unit tests for GoldenRepoManager local:// URL auto-folder creation (Story #163 AC4).

Tests that golden_repo_manager automatically creates target folders for local://
URLs that point to non-existent paths.
"""

import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

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


class TestGoldenRepoManagerSameDirClone:
    """Test cases for source == clone_path index-in-place (EVO-64228)."""

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

    def test_source_equals_clone_path_indexes_in_place(self):
        """
        EVO-64228: Given a file:// URL whose source resolves to the SAME
        directory as clone_path (workspace-mcp materialized the golden repo
        directly into golden_repos_dir), when
        _clone_local_repository_with_regular_copy runs, then it should return
        clone_path in place WITHOUT copying a directory onto itself.
        """
        # Arrange
        base_dir = self._create_temp_dir()
        golden_repos_dir = base_dir / "golden_repos"
        golden_repos_dir.mkdir()

        # The repo already exists AT clone_path (materialized in place)
        clone_path = golden_repos_dir / "repos" / "in-place-repo"
        clone_path.mkdir(parents=True)
        existing_file = clone_path / "readme.txt"
        existing_file.write_text("in-place content")

        manager = GoldenRepoManager(str(golden_repos_dir))

        # Act - source IS clone_path (same directory)
        repo_url = f"file://{clone_path}"

        with patch.object(shutil, "copytree") as mock_copytree:
            result_path = manager._clone_local_repository_with_regular_copy(
                repo_url, str(clone_path)
            )

        # Assert - returns clone_path, never copied onto itself, dir untouched
        assert result_path == str(clone_path)
        mock_copytree.assert_not_called()
        assert clone_path.is_dir()
        assert (clone_path / "readme.txt").exists()
        assert (clone_path / "readme.txt").read_text() == "in-place content"

    def test_in_place_registration_source_not_deleted_on_failure(self):
        """
        EVO-64228 (review MAJOR): the background worker registers clone_path for
        failure cleanup so a partial clone/index gets rmtree'd. In index-in-place
        mode clone_path IS the caller's source on the shared volume -- cidx did
        not create it and must NOT delete it. The worker gates that registration
        on `not _is_in_place_registration(...)`; this asserts the predicate that
        gate depends on so a later indexing failure cannot destroy the source.
        """
        base_dir = self._create_temp_dir()
        golden_repos_dir = base_dir / "golden_repos"
        golden_repos_dir.mkdir()

        # Source materialized directly at clone_path (workspace-mcp in-place).
        clone_path = golden_repos_dir / "repos" / "in-place-repo"
        clone_path.mkdir(parents=True)
        (clone_path / "keep.txt").write_text("must survive")

        # A DIFFERENT source dir that cidx really copied FROM (out-of-tree).
        external_source = base_dir / "external-src"
        external_source.mkdir()

        manager = GoldenRepoManager(str(golden_repos_dir))

        # In-place: source realpath == clone_path -> guarded (no cleanup).
        assert (
            manager._is_in_place_registration(f"file://{clone_path}", str(clone_path))
            is True
        )
        assert (
            manager._is_in_place_registration(str(clone_path), str(clone_path)) is True
        )

        # Real copy from an external source -> NOT in place, cleanup allowed.
        assert (
            manager._is_in_place_registration(
                f"file://{external_source}", str(clone_path)
            )
            is False
        )
        # Remote and local:// scheme have no external source to protect.
        assert (
            manager._is_in_place_registration(
                "https://example.com/x/y.git", str(clone_path)
            )
            is False
        )
        assert (
            manager._is_in_place_registration("local://cidx-meta", str(clone_path))
            is False
        )


class TestGoldenRepoManagerCleanupSymlink:
    """Test cases for cleanup unlinking symlinks instead of rmtree (EVO-64228)."""

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

    def test_cleanup_failed_clone_unlinks_symlink(self):
        """
        EVO-64228: When the failed-clone path is a symlink, cleanup should
        unlink the symlink itself and leave the symlink's target intact
        (rmtree would raise on a symlink or delete its target).
        """
        # Arrange - a real target directory and a symlink pointing at it
        base_dir = self._create_temp_dir()
        golden_repos_dir = base_dir / "golden_repos"
        golden_repos_dir.mkdir()

        target_dir = base_dir / "real_target"
        target_dir.mkdir()
        target_file = target_dir / "keep.txt"
        target_file.write_text("must survive")

        clone_link = golden_repos_dir / "orphan-link"
        os.symlink(str(target_dir), str(clone_link))
        assert os.path.islink(str(clone_link))

        manager = GoldenRepoManager(str(golden_repos_dir))

        # Act - reach the nested _cleanup_failed_clone closure via add_golden_repo
        # by invoking it directly is not exposed, so exercise the same guarded
        # cleanup through _cleanup_filesystem which shares the unlink-not-rmtree
        # behavior for the symlink case.
        manager._cleanup_filesystem(clone_link)

        # Assert - symlink removed, target and its contents survive
        assert not os.path.islink(str(clone_link))
        assert not clone_link.exists()
        assert target_dir.is_dir()
        assert target_file.exists()
        assert target_file.read_text() == "must survive"
