"""
Integration tests for create_file Operation.

Story #7 - AC1: create_file Operation

Uses REAL file system operations - NO Python mocks for git/file operations.
"""

import hashlib
from pathlib import Path

import pytest

from code_indexer.server.services.file_crud_service import FileCRUDService


class TestCreateFile:
    """Tests for create_file operation (AC1)."""

    def test_create_file_success(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC1: Given an activated repository with a valid path,
        When I call create_file with path and content,
        Then the file is created and content matches exactly.
        """
        service = FileCRUDService()
        content = "# New file created by test\nprint('hello')\n"
        file_path = f"src/{unique_filename}"

        result = service.create_file(
            repo_alias=activated_local_repo,
            file_path=file_path,
            content=content,
            username="testuser",
        )

        assert result["success"] is True
        assert result["file_path"] == file_path

        created_file = local_test_repo / file_path
        assert created_file.exists()
        assert created_file.read_text() == content

    def test_create_file_returns_content_hash(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """AC1: Response includes the content_hash."""
        service = FileCRUDService()
        content = "content for hash verification"

        result = service.create_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            content=content,
            username="testuser",
        )

        assert "content_hash" in result
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert result["content_hash"] == expected_hash

    def test_create_file_returns_metadata(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """create_file returns size_bytes and created_at metadata."""
        service = FileCRUDService()
        content = "metadata test content"

        result = service.create_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            content=content,
            username="testuser",
        )

        assert "size_bytes" in result
        assert result["size_bytes"] == len(content.encode("utf-8"))
        assert "created_at" in result

    def test_create_file_git_path_blocked(
        self,
        activated_local_repo: str,
        captured_state,
    ):
        """
        AC1: When I call create_file with path ".git/config",
        Then the operation is rejected with "path blocked" error.
        """
        service = FileCRUDService()

        with pytest.raises(PermissionError) as exc_info:
            service.create_file(
                repo_alias=activated_local_repo,
                file_path=".git/config",
                content="malicious content",
                username="testuser",
            )

        assert "blocked" in str(exc_info.value).lower()
        assert ".git" in str(exc_info.value).lower()

    def test_create_file_nested_git_path_blocked(
        self,
        activated_local_repo: str,
        captured_state,
    ):
        """create_file blocks .git/ even when nested."""
        service = FileCRUDService()

        with pytest.raises(PermissionError) as exc_info:
            service.create_file(
                repo_alias=activated_local_repo,
                file_path="subdir/.git/objects/abc123",
                content="malicious content",
                username="testuser",
            )

        assert ".git" in str(exc_info.value).lower()

    def test_create_file_blocks_path_traversal(
        self,
        activated_local_repo: str,
        captured_state,
    ):
        """create_file blocks path traversal attempts."""
        service = FileCRUDService()

        with pytest.raises(PermissionError) as exc_info:
            service.create_file(
                repo_alias=activated_local_repo,
                file_path="../../etc/passwd",
                content="malicious",
                username="testuser",
            )

        assert "traversal" in str(exc_info.value).lower()

    def test_create_file_allows_gitignore(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
    ):
        """create_file allows .gitignore files."""
        service = FileCRUDService()

        result = service.create_file(
            repo_alias=activated_local_repo,
            file_path=".gitignore",
            content="*.pyc\n__pycache__/\n",
            username="testuser",
        )

        assert result["success"] is True
        assert (local_test_repo / ".gitignore").exists()

    def test_create_file_allows_github_directory(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
    ):
        """create_file allows .github/ directory files."""
        service = FileCRUDService()

        result = service.create_file(
            repo_alias=activated_local_repo,
            file_path=".github/workflows/ci.yml",
            content="name: CI\non: [push]\n",
            username="testuser",
        )

        assert result["success"] is True
        assert (local_test_repo / ".github" / "workflows" / "ci.yml").exists()

    def test_create_file_creates_parent_directories(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """create_file creates parent directories if needed."""
        service = FileCRUDService()
        file_path = f"deep/nested/directory/{unique_filename}"

        result = service.create_file(
            repo_alias=activated_local_repo,
            file_path=file_path,
            content="# Nested file\n",
            username="testuser",
        )

        assert result["success"] is True
        assert (local_test_repo / file_path).exists()

    def test_create_file_rejects_existing_file(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """create_file raises FileExistsError if file exists."""
        service = FileCRUDService()

        service.create_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            content="original",
            username="testuser",
        )

        with pytest.raises(FileExistsError) as exc_info:
            service.create_file(
                repo_alias=activated_local_repo,
                file_path=unique_filename,
                content="duplicate",
                username="testuser",
            )

        assert unique_filename in str(exc_info.value)
