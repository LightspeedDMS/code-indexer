"""
Integration tests for get_file_content Operation.

Story #7 - AC2: get_file_content Operation

Uses REAL file system operations - NO Python mocks for git/file operations.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from code_indexer.server.services.file_service import FileListingService
from code_indexer.server.repositories.activated_repo_manager import ActivatedRepoManager


class TestGetFileContent:
    """Tests for get_file_content operation (AC2)."""

    def test_get_file_content_full_file(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Given a file in repository,
        When I call get_file_content without pagination params,
        Then I receive the file content.
        """
        # Create a test file with known content
        test_file = local_test_repo / "test_read.txt"
        content = "Line 1\nLine 2\nLine 3\n"
        test_file.write_text(content)

        service = FileListingService()

        # Patch to return local test repo path
        with patch.object(
            ActivatedRepoManager,
            "get_activated_repo_path",
            return_value=str(local_test_repo),
        ):
            result = service.get_file_content(
                repository_alias=activated_local_repo,
                file_path="test_read.txt",
                username="testuser",
            )

        assert "content" in result
        assert result["content"] == content

    def test_get_file_content_includes_total_lines(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Response includes total_lines count.
        """
        # Create a file with exact line count
        test_file = local_test_repo / "lines_test.txt"
        lines = [f"Line {i+1}\n" for i in range(50)]
        test_file.write_text("".join(lines))

        service = FileListingService()

        with patch.object(
            ActivatedRepoManager,
            "get_activated_repo_path",
            return_value=str(local_test_repo),
        ):
            result = service.get_file_content(
                repository_alias=activated_local_repo,
                file_path="lines_test.txt",
                username="testuser",
            )

        assert "metadata" in result
        assert result["metadata"]["total_lines"] == 50

    def test_get_file_content_with_pagination(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Given a file with 200 lines,
        When I call get_file_content with offset=100, limit=50,
        Then I receive lines 100-149.
        """
        # Create file with 200 numbered lines
        test_file = local_test_repo / "paginated.txt"
        lines = [f"Content line {i+1}\n" for i in range(200)]
        test_file.write_text("".join(lines))

        service = FileListingService()

        with patch.object(
            ActivatedRepoManager,
            "get_activated_repo_path",
            return_value=str(local_test_repo),
        ):
            result = service.get_file_content(
                repository_alias=activated_local_repo,
                file_path="paginated.txt",
                username="testuser",
                offset=100,
                limit=50,
            )

        # Verify we got 50 lines starting from line 100
        content_lines = result["content"].strip().split("\n")
        assert len(content_lines) == 50
        assert content_lines[0] == "Content line 100"
        assert content_lines[-1] == "Content line 149"

        # Verify metadata
        metadata = result["metadata"]
        assert metadata["offset"] == 100
        assert metadata["returned_lines"] == 50
        assert metadata["total_lines"] == 200
        assert metadata["has_more"] is True

    def test_get_file_content_has_more_flag(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: has_more is True when more content exists, False otherwise.
        """
        # Create small file
        test_file = local_test_repo / "small_file.txt"
        lines = [f"Line {i+1}\n" for i in range(10)]
        test_file.write_text("".join(lines))

        service = FileListingService()

        with patch.object(
            ActivatedRepoManager,
            "get_activated_repo_path",
            return_value=str(local_test_repo),
        ):
            # Read entire file - has_more should be False
            result = service.get_file_content(
                repository_alias=activated_local_repo,
                file_path="small_file.txt",
                username="testuser",
            )

        assert result["metadata"]["has_more"] is False
        assert result["metadata"]["total_lines"] == 10

    def test_get_file_content_next_offset(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: next_offset provides the offset for the next page.
        """
        # Create file with 100 lines
        test_file = local_test_repo / "next_offset_test.txt"
        lines = [f"Line {i+1}\n" for i in range(100)]
        test_file.write_text("".join(lines))

        service = FileListingService()

        with patch.object(
            ActivatedRepoManager,
            "get_activated_repo_path",
            return_value=str(local_test_repo),
        ):
            # Read first 30 lines
            result = service.get_file_content(
                repository_alias=activated_local_repo,
                file_path="next_offset_test.txt",
                username="testuser",
                offset=1,
                limit=30,
            )

        # next_offset should be 31 (next line after returned 30)
        assert result["metadata"]["next_offset"] == 31
        assert result["metadata"]["has_more"] is True

    def test_get_file_content_file_not_found(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: FileNotFoundError raised for non-existent file.
        """
        service = FileListingService()

        with patch.object(
            ActivatedRepoManager,
            "get_activated_repo_path",
            return_value=str(local_test_repo),
        ):
            with pytest.raises(FileNotFoundError) as exc_info:
                service.get_file_content(
                    repository_alias=activated_local_repo,
                    file_path="nonexistent.txt",
                    username="testuser",
                )

        assert "nonexistent.txt" in str(exc_info.value)

    def test_get_file_content_blocks_path_traversal(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Path traversal attempts are blocked with PermissionError.
        """
        service = FileListingService()

        with patch.object(
            ActivatedRepoManager,
            "get_activated_repo_path",
            return_value=str(local_test_repo),
        ):
            with pytest.raises(PermissionError) as exc_info:
                service.get_file_content(
                    repository_alias=activated_local_repo,
                    file_path="../../../etc/passwd",
                    username="testuser",
                )

        assert "denied" in str(exc_info.value).lower()

    def test_get_file_content_metadata_complete(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Response metadata includes all required fields.
        """
        # Create test file
        test_file = local_test_repo / "metadata_test.py"
        test_file.write_text("# Python file\nprint('hello')\n")

        service = FileListingService()

        with patch.object(
            ActivatedRepoManager,
            "get_activated_repo_path",
            return_value=str(local_test_repo),
        ):
            result = service.get_file_content(
                repository_alias=activated_local_repo,
                file_path="metadata_test.py",
                username="testuser",
            )

        metadata = result["metadata"]

        # Core file metadata
        assert "size" in metadata
        assert "modified_at" in metadata
        assert "language" in metadata
        assert metadata["language"] == "python"
        assert "path" in metadata
        assert metadata["path"] == "metadata_test.py"

        # Pagination metadata
        assert "total_lines" in metadata
        assert "returned_lines" in metadata
        assert "offset" in metadata
        assert "limit" in metadata
        assert "has_more" in metadata
        assert "next_offset" in metadata

        # Token enforcement metadata
        assert "estimated_tokens" in metadata
        assert "truncated" in metadata
        assert "requires_pagination" in metadata

    def test_get_file_content_includes_content_hash(
        self,
        activated_local_repo: str,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Response includes content_hash for optimistic locking.
        The content_hash is a SHA-256 hash of the FULL file content (not paginated).
        """
        import hashlib

        # Create test file with known content
        test_file = local_test_repo / "hash_test.txt"
        content = "content for hash verification\nline two\nline three\n"
        test_file.write_text(content)

        # Calculate expected hash of full content
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        service = FileListingService()

        with patch.object(
            ActivatedRepoManager,
            "get_activated_repo_path",
            return_value=str(local_test_repo),
        ):
            result = service.get_file_content(
                repository_alias=activated_local_repo,
                file_path="hash_test.txt",
                username="testuser",
            )

        # Verify content_hash is in metadata
        assert "content_hash" in result["metadata"], (
            "content_hash must be present in metadata for optimistic locking"
        )

        # Verify hash matches expected SHA-256 of full file content
        assert result["metadata"]["content_hash"] == expected_hash, (
            f"content_hash mismatch: expected {expected_hash}, "
            f"got {result['metadata']['content_hash']}"
        )
