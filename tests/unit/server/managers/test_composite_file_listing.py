"""
Unit tests for composite repository file listing.

Tests the FileInfo model, directory walking, and composite file aggregation
for the Server Composite Repository Activation epic (Story 3.3).
"""

import pytest
from pathlib import Path
from datetime import datetime
from pydantic import ValidationError


class TestFileInfoModel:
    """Test FileInfo model for composite repository file listing."""

    def test_fileinfo_model_creation(self):
        """Test FileInfo model can be created with all required fields."""
        from code_indexer.server.models.composite_file_models import FileInfo

        file_info = FileInfo(
            full_path="backend-api/src/main.py",
            name="main.py",
            size=2456,
            modified=datetime.now(),
            is_directory=False,
            component_repo="backend-api",
        )

        assert file_info.full_path == "backend-api/src/main.py"
        assert file_info.name == "main.py"
        assert file_info.size == 2456
        assert file_info.is_directory is False
        assert file_info.component_repo == "backend-api"

    def test_fileinfo_model_requires_all_fields(self):
        """Test FileInfo model validation requires all fields."""
        from code_indexer.server.models.composite_file_models import FileInfo

        with pytest.raises(ValidationError):
            FileInfo(
                full_path="test.py",
                name="test.py",
                # Missing size, modified, is_directory, component_repo
            )

    def test_fileinfo_model_datetime_serialization(self):
        """Test FileInfo model properly handles datetime serialization."""
        from code_indexer.server.models.composite_file_models import FileInfo

        now = datetime.now()
        file_info = FileInfo(
            full_path="test.py",
            name="test.py",
            size=100,
            modified=now,
            is_directory=False,
            component_repo="repo1",
        )

        # Should be able to serialize to dict; @field_serializer converts datetime to ISO string
        data = file_info.model_dump()
        assert "modified" in data
        assert isinstance(data["modified"], str)

    def test_fileinfo_model_directory_flag(self):
        """Test FileInfo model handles both files and directories."""
        from code_indexer.server.models.composite_file_models import FileInfo

        # File
        file_info = FileInfo(
            full_path="repo/file.py",
            name="file.py",
            size=100,
            modified=datetime.now(),
            is_directory=False,
            component_repo="repo",
        )
        assert file_info.is_directory is False

        # Directory
        dir_info = FileInfo(
            full_path="repo/src",
            name="src",
            size=0,
            modified=datetime.now(),
            is_directory=True,
            component_repo="repo",
        )
        assert dir_info.is_directory is True


class TestWalkDirectory:
    """Test _walk_directory function for filesystem traversal."""

    def test_walk_directory_recursive_mode(self, tmp_path):
        """Test _walk_directory in recursive mode finds nested files."""
        from code_indexer.server.managers.composite_file_listing import _walk_directory

        # Create test directory structure
        repo = tmp_path / "test-repo"
        repo.mkdir()
        (repo / "file1.py").write_text("test")
        (repo / "src").mkdir()
        (repo / "src" / "file2.py").write_text("test")
        (repo / "src" / "nested").mkdir()
        (repo / "src" / "nested" / "file3.py").write_text("test")

        # Walk recursively
        files = _walk_directory(directory=repo, repo_prefix="test-repo", recursive=True)

        # Should find all nested files
        file_paths = [f.full_path for f in files]
        assert "test-repo/file1.py" in file_paths
        assert "test-repo/src/file2.py" in file_paths
        assert "test-repo/src/nested/file3.py" in file_paths

    def test_walk_directory_non_recursive_mode(self, tmp_path):
        """Test _walk_directory in non-recursive mode only finds top-level items."""
        from code_indexer.server.managers.composite_file_listing import _walk_directory

        # Create test directory structure
        repo = tmp_path / "test-repo"
        repo.mkdir()
        (repo / "file1.py").write_text("test")
        (repo / "src").mkdir()
        (repo / "src" / "file2.py").write_text("test")

        # Walk non-recursively
        files = _walk_directory(
            directory=repo, repo_prefix="test-repo", recursive=False
        )

        # Should only find top-level items
        file_paths = [f.full_path for f in files]
        assert "test-repo/file1.py" in file_paths
        assert "test-repo/src" in file_paths  # Directory itself
        assert "test-repo/src/file2.py" not in file_paths  # Nested file excluded

    def test_walk_directory_excludes_git_directory(self, tmp_path):
        """Test _walk_directory excludes .git directories."""
        from code_indexer.server.managers.composite_file_listing import _walk_directory

        # Create test directory with .git
        repo = tmp_path / "test-repo"
        repo.mkdir()
        (repo / "file.py").write_text("test")
        git_dir = repo / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("test")

        # Walk recursively
        files = _walk_directory(directory=repo, repo_prefix="test-repo", recursive=True)

        # Should not include .git files
        file_paths = [f.full_path for f in files]
        assert "test-repo/file.py" in file_paths
        assert not any(".git" in path for path in file_paths)

    def test_walk_directory_excludes_code_indexer_directory(self, tmp_path):
        """Test _walk_directory excludes .code-indexer directories."""
        from code_indexer.server.managers.composite_file_listing import _walk_directory

        # Create test directory with .code-indexer
        repo = tmp_path / "test-repo"
        repo.mkdir()
        (repo / "file.py").write_text("test")
        indexer_dir = repo / ".code-indexer"
        indexer_dir.mkdir()
        (indexer_dir / "config.json").write_text("{}")

        # Walk recursively
        files = _walk_directory(directory=repo, repo_prefix="test-repo", recursive=True)

        # Should not include .code-indexer files
        file_paths = [f.full_path for f in files]
        assert "test-repo/file.py" in file_paths
        assert not any(".code-indexer" in path for path in file_paths)

    def test_walk_directory_collects_file_metadata(self, tmp_path):
        """Test _walk_directory collects proper file metadata."""
        from code_indexer.server.managers.composite_file_listing import _walk_directory

        # Create test file
        repo = tmp_path / "test-repo"
        repo.mkdir()
        test_file = repo / "test.py"
        test_file.write_text("hello world")

        # Walk directory
        files = _walk_directory(directory=repo, repo_prefix="test-repo", recursive=True)

        # Verify metadata
        assert len(files) == 1
        file_info = files[0]
        assert file_info.name == "test.py"
        assert file_info.size > 0
        assert isinstance(file_info.modified, datetime)
        assert file_info.is_directory is False
        assert file_info.component_repo == "test-repo"


def _composite(tmp_path, components):
    """A composite directory whose proxy config names *components* (name ->
    {relative file: content})."""
    import json

    composite_path = tmp_path / "composite"
    composite_path.mkdir()
    for name, files in components.items():
        (composite_path / name).mkdir()
        for relative, content in files.items():
            target = composite_path / name / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    config_dir = composite_path / ".code-indexer"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"proxy_mode": True, "discovered_repos": list(components)})
    )
    return composite_path


def _visible_listing(repo, path="", recursive=False):
    """The production aggregation: every component's confined listing, kept
    and sorted by ``visible_composite_entries`` (unrestricted judgement)."""
    from code_indexer.server.managers.composite_file_listing import (
        list_composite_components,
    )
    from code_indexer.server.services.cidx_meta_access.file_doors import (
        UNRESTRICTED,
        visible_composite_entries,
    )

    return visible_composite_entries(
        list_composite_components(repo, path, recursive), lambda _name: UNRESTRICTED
    )


class TestVisibleCompositeListing:
    """The composite listing aggregates every component repository."""

    def test_aggregates_multiple_repos(self, tmp_path, mock_activated_repo):
        path = _composite(
            tmp_path,
            {"backend-api": {"main.py": "test"}, "frontend-app": {"app.js": "test"}},
        )
        repo = mock_activated_repo(path=path, is_composite=True)

        file_paths = [f.full_path for f in _visible_listing(repo)]

        assert "backend-api/main.py" in file_paths
        assert "frontend-app/app.js" in file_paths

    def test_sorts_by_path(self, tmp_path, mock_activated_repo):
        path = _composite(
            tmp_path, {"zeta-repo": {"file.py": "test"}, "alpha-repo": {"file.py": "t"}}
        )
        repo = mock_activated_repo(path=path, is_composite=True)

        file_paths = [f.full_path for f in _visible_listing(repo)]

        assert file_paths == sorted(file_paths)
        assert file_paths[0].startswith("alpha-repo")

    def test_handles_path_filter(self, tmp_path, mock_activated_repo):
        path = _composite(
            tmp_path, {"backend": {"root.py": "test", "src/main.py": "test"}}
        )
        repo = mock_activated_repo(path=path, is_composite=True)

        file_paths = [f.full_path for f in _visible_listing(repo, path="src")]

        assert any("main.py" in p for p in file_paths)
        assert not any("root.py" in p for p in file_paths)

    def test_handles_nonexistent_path(self, tmp_path, mock_activated_repo):
        path = _composite(tmp_path, {"backend": {}})
        repo = mock_activated_repo(path=path, is_composite=True)

        assert _visible_listing(repo, path="nonexistent") == []


@pytest.fixture
def mock_activated_repo():
    """Create mock ActivatedRepository for testing."""
    from code_indexer.server.models.activated_repository import ActivatedRepository
    from datetime import datetime

    def _create_repo(path, is_composite=False):
        return ActivatedRepository(
            user_alias="test-alias",
            username="testuser",
            path=Path(path),
            activated_at=datetime.now(),
            last_accessed=datetime.now(),
            is_composite=is_composite,
            golden_repo_aliases=["repo1", "repo2"] if is_composite else [],
            discovered_repos=["backend", "frontend"] if is_composite else [],
        )

    return _create_repo
