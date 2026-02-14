"""
Tests for MCP handlers auto-watch with write exceptions (Story #197 AC3).

Verifies that auto-watch starts on the correct path (canonical golden repo path)
when editing write exception repos like cidx-meta-global.
"""

import pytest
import tempfile
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock
from code_indexer.server.auth.user_manager import User, UserRole


class TestAutoWatchWithExceptions:
    """Test auto-watch path resolution for write exceptions."""

    def test_handle_create_file_starts_watch_on_exception_path(self):
        """Test that create_file starts auto-watch on canonical path for exceptions."""
        from code_indexer.server.mcp.handlers import handle_create_file
        from code_indexer.server.services.file_crud_service import file_crud_service

        with tempfile.TemporaryDirectory() as tmpdir:
            canonical_path = Path(tmpdir) / "cidx-meta"
            canonical_path.mkdir(parents=True, exist_ok=True)

            # Register exception
            file_crud_service.register_write_exception("cidx-meta-global", canonical_path)

            # Mock auto_watch_manager (imported inside handler)
            with patch("code_indexer.server.services.auto_watch_manager.auto_watch_manager") as mock_watch:
                mock_watch.start_watch = Mock()

                # Create power user
                user = User(
                    username="power_user",
                    password_hash="hashed_password",
                    role=UserRole.POWER_USER,
                    email="power@example.com",
                    created_at=datetime.now(),
                )

                # Call handler
                params = {
                    "repository_alias": "cidx-meta-global",
                    "file_path": "dependency-map/test.md",
                    "content": "test content",
                }

                result = handle_create_file(params, user)

                # Verify auto-watch was called with canonical path
                mock_watch.start_watch.assert_called_once()
                called_path = str(mock_watch.start_watch.call_args[0][0])
                assert called_path == str(canonical_path)

    def test_handle_edit_file_starts_watch_on_exception_path(self):
        """Test that edit_file starts auto-watch on canonical path for exceptions."""
        from code_indexer.server.mcp.handlers import handle_edit_file
        from code_indexer.server.services.file_crud_service import file_crud_service
        import hashlib

        with tempfile.TemporaryDirectory() as tmpdir:
            canonical_path = Path(tmpdir) / "cidx-meta"
            canonical_path.mkdir(parents=True, exist_ok=True)
            test_file = canonical_path / "test.md"
            original_content = "original"
            test_file.write_text(original_content)

            # Register exception
            file_crud_service.register_write_exception("cidx-meta-global", canonical_path)

            # Mock auto_watch_manager (imported inside handler)
            with patch("code_indexer.server.services.auto_watch_manager.auto_watch_manager") as mock_watch:
                mock_watch.start_watch = Mock()

                # Create power user
                user = User(
                    username="power_user",
                    password_hash="hashed_password",
                    role=UserRole.POWER_USER,
                    email="power@example.com",
                    created_at=datetime.now(),
                )

                # Compute hash
                content_hash = hashlib.sha256(original_content.encode()).hexdigest()

                # Call handler
                params = {
                    "repository_alias": "cidx-meta-global",
                    "file_path": "test.md",
                    "old_string": "original",
                    "new_string": "modified",
                    "content_hash": content_hash,
                    "replace_all": False,
                }

                result = handle_edit_file(params, user)

                # Verify auto-watch was called with canonical path
                mock_watch.start_watch.assert_called_once()
                called_path = str(mock_watch.start_watch.call_args[0][0])
                assert called_path == str(canonical_path)

    def test_handle_delete_file_starts_watch_on_exception_path(self):
        """Test that delete_file starts auto-watch on canonical path for exceptions."""
        from code_indexer.server.mcp.handlers import handle_delete_file
        from code_indexer.server.services.file_crud_service import file_crud_service

        with tempfile.TemporaryDirectory() as tmpdir:
            canonical_path = Path(tmpdir) / "cidx-meta"
            canonical_path.mkdir(parents=True, exist_ok=True)
            test_file = canonical_path / "test.md"
            test_file.write_text("to delete")

            # Register exception
            file_crud_service.register_write_exception("cidx-meta-global", canonical_path)

            # Mock auto_watch_manager (imported inside handler)
            with patch("code_indexer.server.services.auto_watch_manager.auto_watch_manager") as mock_watch:
                mock_watch.start_watch = Mock()

                # Create power user
                user = User(
                    username="power_user",
                    password_hash="hashed_password",
                    role=UserRole.POWER_USER,
                    email="power@example.com",
                    created_at=datetime.now(),
                )

                # Call handler
                params = {
                    "repository_alias": "cidx-meta-global",
                    "file_path": "test.md",
                }

                result = handle_delete_file(params, user)

                # Verify auto-watch was called with canonical path
                mock_watch.start_watch.assert_called_once()
                called_path = str(mock_watch.start_watch.call_args[0][0])
                assert called_path == str(canonical_path)
