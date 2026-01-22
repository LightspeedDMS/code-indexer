"""
Integration tests for delete_file Operation.

Story #7 - AC4: delete_file Operation with Hash Validation

Uses REAL file system operations - NO Python mocks for git/file operations.
"""

import hashlib
from pathlib import Path

import pytest

from code_indexer.server.services.file_crud_service import (
    FileCRUDService,
    HashMismatchError,
)


class TestDeleteFile:
    """Tests for delete_file operation with hash validation (AC4)."""

    def test_delete_file_success(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC4: Given an existing file,
        When I call delete_file,
        Then the file is deleted.
        """
        service = FileCRUDService()

        # Create file first
        service.create_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            content="File to delete",
            username="testuser",
        )

        # Verify file exists
        file_path = local_test_repo / unique_filename
        assert file_path.exists()

        # Delete without hash validation
        result = service.delete_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            content_hash=None,
            username="testuser",
        )

        assert result["success"] is True
        assert result["file_path"] == unique_filename
        assert "deleted_at" in result

        # Verify file is deleted
        assert not file_path.exists()

    def test_delete_file_with_hash_validation_success(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC4: Given a file with known hash,
        When I call delete_file with matching hash,
        Then deletion succeeds.
        """
        service = FileCRUDService()

        # Create file
        content = "Content for hash validation"
        create_result = service.create_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            content=content,
            username="testuser",
        )

        content_hash = create_result["content_hash"]

        # Delete with correct hash
        result = service.delete_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            content_hash=content_hash,
            username="testuser",
        )

        assert result["success"] is True

        # Verify file is deleted
        file_path = local_test_repo / unique_filename
        assert not file_path.exists()

    def test_delete_file_hash_mismatch_preserves_file(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC4: Given a file that has been modified,
        When I call delete_file with stale hash,
        Then HashMismatchError is raised and file is preserved.
        """
        service = FileCRUDService()

        # Create file
        service.create_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            content="Original content",
            username="testuser",
        )

        # Use wrong hash
        wrong_hash = hashlib.sha256(b"different content").hexdigest()

        with pytest.raises(HashMismatchError) as exc_info:
            service.delete_file(
                repo_alias=activated_local_repo,
                file_path=unique_filename,
                content_hash=wrong_hash,
                username="testuser",
            )

        assert "mismatch" in str(exc_info.value).lower()

        # CRITICAL: File should still exist (not deleted on hash mismatch)
        file_path = local_test_repo / unique_filename
        assert file_path.exists()
        assert file_path.read_text() == "Original content"

    def test_delete_file_not_found(
        self,
        activated_local_repo: str,
        captured_state,
    ):
        """
        AC4: Deleting non-existent file raises FileNotFoundError.
        """
        service = FileCRUDService()

        with pytest.raises(FileNotFoundError) as exc_info:
            service.delete_file(
                repo_alias=activated_local_repo,
                file_path="nonexistent_file.txt",
                content_hash=None,
                username="testuser",
            )

        assert "nonexistent_file.txt" in str(exc_info.value)

    def test_delete_file_git_path_blocked(
        self,
        activated_local_repo: str,
        captured_state,
    ):
        """
        AC4: delete_file blocks .git/ directory access.
        """
        service = FileCRUDService()

        with pytest.raises(PermissionError) as exc_info:
            service.delete_file(
                repo_alias=activated_local_repo,
                file_path=".git/config",
                content_hash=None,
                username="testuser",
            )

        assert ".git" in str(exc_info.value).lower()
        assert "blocked" in str(exc_info.value).lower()

    def test_delete_file_returns_timestamp(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC4: delete_file returns deleted_at timestamp.
        """
        service = FileCRUDService()

        # Create and delete file
        service.create_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            content="Temporary file",
            username="testuser",
        )

        result = service.delete_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            content_hash=None,
            username="testuser",
        )

        assert "deleted_at" in result
        # Verify it's an ISO timestamp format
        assert "T" in result["deleted_at"]
        assert ":" in result["deleted_at"]
