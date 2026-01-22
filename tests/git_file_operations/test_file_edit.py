"""
Integration tests for edit_file Operation.

Story #7 - AC3: edit_file Operation with Optimistic Locking

Uses REAL file system operations - NO Python mocks for git/file operations.
"""

import hashlib
from pathlib import Path

import pytest

from code_indexer.server.services.file_crud_service import (
    FileCRUDService,
    HashMismatchError,
)


class TestEditFile:
    """Tests for edit_file operation with optimistic locking (AC3)."""

    def test_edit_file_success_with_hash_match(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC3: Given a file with known content_hash,
        When I call edit_file with matching hash,
        Then the edit succeeds and content is updated.
        """
        service = FileCRUDService()

        # Create file first
        original_content = "Hello World"
        create_result = service.create_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            content=original_content,
            username="testuser",
        )

        # Get the hash from create
        original_hash = create_result["content_hash"]

        # Edit with correct hash
        result = service.edit_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            old_string="World",
            new_string="Universe",
            content_hash=original_hash,
            replace_all=False,
            username="testuser",
        )

        assert result["success"] is True
        assert result["changes_made"] == 1

        # Verify file content changed
        edited_file = local_test_repo / unique_filename
        assert edited_file.read_text() == "Hello Universe"

    def test_edit_file_hash_mismatch_error(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC3: Given a file that has been modified,
        When I call edit_file with stale hash,
        Then HashMismatchError is raised.
        """
        service = FileCRUDService()

        # Create file
        service.create_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            content="Original content",
            username="testuser",
        )

        # Use a fake/stale hash
        stale_hash = hashlib.sha256(b"wrong content").hexdigest()

        with pytest.raises(HashMismatchError) as exc_info:
            service.edit_file(
                repo_alias=activated_local_repo,
                file_path=unique_filename,
                old_string="Original",
                new_string="Modified",
                content_hash=stale_hash,
                replace_all=False,
                username="testuser",
            )

        assert "mismatch" in str(exc_info.value).lower()
        assert "modified" in str(exc_info.value).lower()

    def test_edit_file_returns_new_hash(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC3: edit_file returns the new content_hash after edit.
        """
        service = FileCRUDService()

        # Create file
        create_result = service.create_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            content="foo bar baz",
            username="testuser",
        )

        original_hash = create_result["content_hash"]

        # Edit file
        result = service.edit_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            old_string="bar",
            new_string="qux",
            content_hash=original_hash,
            replace_all=False,
            username="testuser",
        )

        # Verify new hash is returned and is different
        assert "content_hash" in result
        assert result["content_hash"] != original_hash

        # Verify new hash matches actual content
        expected_hash = hashlib.sha256(b"foo qux baz").hexdigest()
        assert result["content_hash"] == expected_hash

    def test_edit_file_replace_all(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC3: replace_all=True replaces all occurrences.
        """
        service = FileCRUDService()

        # Create file with multiple occurrences
        create_result = service.create_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            content="apple banana apple cherry apple",
            username="testuser",
        )

        # Edit with replace_all=True
        result = service.edit_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            old_string="apple",
            new_string="orange",
            content_hash=create_result["content_hash"],
            replace_all=True,
            username="testuser",
        )

        assert result["success"] is True
        assert result["changes_made"] == 3

        # Verify all occurrences replaced
        edited_file = local_test_repo / unique_filename
        assert edited_file.read_text() == "orange banana orange cherry orange"

    def test_edit_file_non_unique_string_error(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC3: Non-unique string with replace_all=False raises ValueError.
        """
        service = FileCRUDService()

        # Create file with duplicate string
        create_result = service.create_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            content="test test test",
            username="testuser",
        )

        with pytest.raises(ValueError) as exc_info:
            service.edit_file(
                repo_alias=activated_local_repo,
                file_path=unique_filename,
                old_string="test",
                new_string="replaced",
                content_hash=create_result["content_hash"],
                replace_all=False,
                username="testuser",
            )

        assert "not unique" in str(exc_info.value).lower()
        assert "3 times" in str(exc_info.value)

    def test_edit_file_git_path_blocked(
        self,
        activated_local_repo: str,
        captured_state,
    ):
        """
        AC3: edit_file blocks .git/ directory access.
        """
        service = FileCRUDService()

        with pytest.raises(PermissionError) as exc_info:
            service.edit_file(
                repo_alias=activated_local_repo,
                file_path=".git/config",
                old_string="old",
                new_string="new",
                content_hash="fakehash",
                replace_all=False,
                username="testuser",
            )

        assert ".git" in str(exc_info.value).lower()
        assert "blocked" in str(exc_info.value).lower()

    def test_edit_file_string_not_found(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC3: String not found raises ValueError.
        """
        service = FileCRUDService()

        # Create file
        create_result = service.create_file(
            repo_alias=activated_local_repo,
            file_path=unique_filename,
            content="hello world",
            username="testuser",
        )

        with pytest.raises(ValueError) as exc_info:
            service.edit_file(
                repo_alias=activated_local_repo,
                file_path=unique_filename,
                old_string="nonexistent",
                new_string="replacement",
                content_hash=create_result["content_hash"],
                replace_all=False,
                username="testuser",
            )

        assert "not found" in str(exc_info.value).lower()

    def test_edit_file_file_not_found(
        self,
        activated_local_repo: str,
        captured_state,
    ):
        """
        AC3: Editing non-existent file raises FileNotFoundError.
        """
        service = FileCRUDService()

        with pytest.raises(FileNotFoundError) as exc_info:
            service.edit_file(
                repo_alias=activated_local_repo,
                file_path="nonexistent_file.txt",
                old_string="old",
                new_string="new",
                content_hash="fakehash",
                replace_all=False,
                username="testuser",
            )

        assert "nonexistent_file.txt" in str(exc_info.value)
