"""
Tests for FileCRUDService write exceptions map (Story #197).

Tests the write exceptions mechanism that allows direct editing of golden repos
like cidx-meta without requiring activation in activated repos.
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from code_indexer.server.services.file_crud_service import (
    FileCRUDService,
    CRUDOperationError,
    HashMismatchError,
)


class TestWriteExceptionsRegistration:
    """Test registration and checking of write exceptions."""

    def test_register_write_exception_stores_mapping(self):
        """Test that register_write_exception stores alias-to-path mapping."""
        service = FileCRUDService()
        canonical_path = Path("/fake/path/to/cidx-meta")

        service.register_write_exception("cidx-meta-global", canonical_path)

        assert service.is_write_exception("cidx-meta-global")

    def test_is_write_exception_returns_false_for_unregistered(self):
        """Test that is_write_exception returns False for non-registered aliases."""
        service = FileCRUDService()

        assert not service.is_write_exception("cidx-meta-global")
        assert not service.is_write_exception("other-repo-global")

    def test_multiple_exceptions_can_be_registered(self):
        """Test that multiple write exceptions can be registered."""
        service = FileCRUDService()
        path1 = Path("/fake/cidx-meta")
        path2 = Path("/fake/other-repo")

        service.register_write_exception("cidx-meta-global", path1)
        service.register_write_exception("other-global", path2)

        assert service.is_write_exception("cidx-meta-global")
        assert service.is_write_exception("other-global")
        assert not service.is_write_exception("unknown-global")


class TestPathResolution:
    """Test path resolution for write exceptions vs activated repos."""

    def test_resolve_path_uses_exception_for_registered_alias(self):
        """Test that path resolution uses exception path for registered alias."""
        # Create real temporary directory
        with tempfile.TemporaryDirectory() as tmpdir:
            canonical_path = Path(tmpdir) / "cidx-meta"
            canonical_path.mkdir(parents=True, exist_ok=True)

            service = FileCRUDService()
            service.register_write_exception("cidx-meta-global", canonical_path)

            # Mock activated_repo_manager to ensure it's NOT called
            with patch.object(
                service.activated_repo_manager,
                "get_activated_repo_path",
                side_effect=AssertionError("Should not call activated_repo_manager"),
            ):
                # Test create_file uses exception path
                result = service.create_file(
                    repo_alias="cidx-meta-global",
                    file_path="test.md",
                    content="test content",
                    username="testuser",
                )

                assert result["success"]
                # Verify file was created in canonical path, not activated repo
                assert (canonical_path / "test.md").exists()

    def test_resolve_path_falls_through_to_activated_for_non_exception(self):
        """Test that path resolution uses activated repo for non-exception alias."""
        with tempfile.TemporaryDirectory() as tmpdir:
            activated_path = Path(tmpdir) / "activated-repo"
            activated_path.mkdir(parents=True, exist_ok=True)

            service = FileCRUDService()

            # Mock activated_repo_manager to return activated path
            with patch.object(
                service.activated_repo_manager,
                "get_activated_repo_path",
                return_value=str(activated_path),
            ):
                result = service.create_file(
                    repo_alias="normal-repo",
                    file_path="test.md",
                    content="test content",
                    username="testuser",
                )

                assert result["success"]
                assert (activated_path / "test.md").exists()


class TestCreateFileWithExceptions:
    """Test create_file with write exceptions."""

    def test_create_file_writes_to_canonical_path(self):
        """Test that create_file writes to canonical golden repo path for exception."""
        with tempfile.TemporaryDirectory() as tmpdir:
            canonical_path = Path(tmpdir) / "cidx-meta"
            canonical_path.mkdir(parents=True, exist_ok=True)

            service = FileCRUDService()
            service.register_write_exception("cidx-meta-global", canonical_path)

            result = service.create_file(
                repo_alias="cidx-meta-global",
                file_path="dependency-map/new-domain.md",
                content="---\ntitle: New Domain\n---\nContent here",
                username="power_user",
            )

            assert result["success"]
            assert result["file_path"] == "dependency-map/new-domain.md"
            created_file = canonical_path / "dependency-map/new-domain.md"
            assert created_file.exists()
            assert created_file.read_text() == "---\ntitle: New Domain\n---\nContent here"

    def test_create_file_applies_security_checks(self):
        """Test that security checks still apply to exception paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            canonical_path = Path(tmpdir) / "cidx-meta"
            canonical_path.mkdir(parents=True, exist_ok=True)

            service = FileCRUDService()
            service.register_write_exception("cidx-meta-global", canonical_path)

            # Test .git/ blocking
            with pytest.raises(PermissionError, match=".git"):
                service.create_file(
                    repo_alias="cidx-meta-global",
                    file_path=".git/config",
                    content="malicious",
                    username="power_user",
                )

            # Test path traversal blocking
            with pytest.raises(PermissionError, match="traversal"):
                service.create_file(
                    repo_alias="cidx-meta-global",
                    file_path="../etc/passwd",
                    content="malicious",
                    username="power_user",
                )


class TestEditFileWithExceptions:
    """Test edit_file with write exceptions."""

    def test_edit_file_modifies_canonical_path(self):
        """Test that edit_file modifies file in canonical golden repo path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            canonical_path = Path(tmpdir) / "cidx-meta"
            canonical_path.mkdir(parents=True, exist_ok=True)
            test_file = canonical_path / "test.md"
            original_content = "original content"
            test_file.write_text(original_content)

            service = FileCRUDService()
            service.register_write_exception("cidx-meta-global", canonical_path)

            # Compute hash
            import hashlib

            content_hash = hashlib.sha256(original_content.encode()).hexdigest()

            result = service.edit_file(
                repo_alias="cidx-meta-global",
                file_path="test.md",
                old_string="original",
                new_string="modified",
                content_hash=content_hash,
                replace_all=False,
                username="power_user",
            )

            assert result["success"]
            assert result["changes_made"] == 1
            assert test_file.read_text() == "modified content"

    def test_edit_file_validates_hash(self):
        """Test that optimistic locking still works with exceptions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            canonical_path = Path(tmpdir) / "cidx-meta"
            canonical_path.mkdir(parents=True, exist_ok=True)
            test_file = canonical_path / "test.md"
            test_file.write_text("content")

            service = FileCRUDService()
            service.register_write_exception("cidx-meta-global", canonical_path)

            # Use wrong hash
            with pytest.raises(HashMismatchError):
                service.edit_file(
                    repo_alias="cidx-meta-global",
                    file_path="test.md",
                    old_string="content",
                    new_string="modified",
                    content_hash="wrong_hash",
                    replace_all=False,
                    username="power_user",
                )


class TestDeleteFileWithExceptions:
    """Test delete_file with write exceptions."""

    def test_delete_file_removes_from_canonical_path(self):
        """Test that delete_file removes file from canonical golden repo path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            canonical_path = Path(tmpdir) / "cidx-meta"
            canonical_path.mkdir(parents=True, exist_ok=True)
            test_file = canonical_path / "test.md"
            test_file.write_text("to be deleted")

            service = FileCRUDService()
            service.register_write_exception("cidx-meta-global", canonical_path)

            result = service.delete_file(
                repo_alias="cidx-meta-global",
                file_path="test.md",
                content_hash=None,
                username="power_user",
            )

            assert result["success"]
            assert not test_file.exists()


class TestNonExceptionReposRemainReadOnly:
    """Test that non-exception golden repos cannot be written (AC7)."""

    def test_non_exception_golden_repo_edit_fails(self):
        """Test that editing non-exception golden repo fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Register only cidx-meta as exception
            cidx_meta_path = Path(tmpdir) / "cidx-meta"
            cidx_meta_path.mkdir(parents=True, exist_ok=True)

            service = FileCRUDService()
            service.register_write_exception("cidx-meta-global", cidx_meta_path)

            # Try to edit a different golden repo (not in exceptions)
            # This should fail because activated_repo_manager won't find it
            with patch.object(
                service.activated_repo_manager,
                "get_activated_repo_path",
                side_effect=ValueError("Repository not found in activated repos"),
            ):
                with pytest.raises(ValueError, match="not found"):
                    service.create_file(
                        repo_alias="other-repo-global",
                        file_path="test.md",
                        content="content",
                        username="power_user",
                    )

    def test_exceptions_map_not_bypassed_for_unlisted_repos(self):
        """Test that the exceptions map is only used for explicitly registered aliases."""
        service = FileCRUDService()

        # Register one exception
        service.register_write_exception("cidx-meta-global", Path("/fake/cidx-meta"))

        # Other repos should not be exceptions
        assert not service.is_write_exception("other-global")
        assert not service.is_write_exception("normal-repo")
