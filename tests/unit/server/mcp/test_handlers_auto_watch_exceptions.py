"""
Tests for MCP handlers auto-watch with write exceptions (Story #197 AC3).

Verifies that auto-watch starts on the correct path (canonical golden repo path)
when a write to a write exception repo like cidx-meta-global is admitted and
succeeds. A refused write starts no watch: see
test_write_mode_watch_suppression.py::TestRefusedWriteStartsNoWatch.
"""

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Iterator
from unittest.mock import Mock, patch

from code_indexer.server.auth.user_manager import User, UserRole
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    build_cidx_meta_fixture,
)
from tests.shared.real_write_mode import real_write_mode


@contextmanager
def _admitted_exception_repo(tmpdir: str) -> Iterator[Path]:
    """cidx-meta-global registered as a write exception and in real write
    mode, with the real access service installed so the admin caller passes
    the catalog path gate; all singleton state is restored afterwards."""
    from code_indexer.server.mcp.handlers import _utils
    from code_indexer.server.services.file_crud_service import file_crud_service

    # ^ order app construction re-registers the write exception and golden dir => resolve it before overriding them
    app_state = _utils.app_module.app.state
    fx = build_cidx_meta_fixture(Path(tmpdir) / "data")
    golden_repos_dir = Path(tmpdir) / "data" / "golden-repos"
    canonical_path = fx.root
    saved_exceptions = dict(file_crud_service._global_write_exceptions)
    saved_golden = file_crud_service._golden_repos_dir
    file_crud_service.register_write_exception("cidx-meta-global", canonical_path)
    file_crud_service.set_golden_repos_dir(golden_repos_dir)
    try:
        with (
            patch.object(
                app_state,
                "access_filtering_service",
                fx.access_service,
                create=True,
            ),
            real_write_mode(
                file_crud_service,
                golden_repos_dir,
                "cidx-meta",
                canonical_path,
                Path(tmpdir) / "write-mode-locks",
            ),
        ):
            yield canonical_path
    finally:
        file_crud_service._global_write_exceptions = saved_exceptions
        file_crud_service._golden_repos_dir = saved_golden


def _admin_user() -> User:
    return User(
        username=ADMIN_USER,
        password_hash="hashed_password",
        role=UserRole.ADMIN,
        email="admin@example.com",
        created_at=datetime.now(),
    )


def _assert_succeeded(response: Dict[str, Any]) -> None:
    payload = json.loads(response["content"][0]["text"])
    assert payload["success"] is True, payload


class TestAutoWatchWithExceptions:
    """Test auto-watch path resolution for write exceptions."""

    # ^ why the handler's write-mode suppression probe reads app.state.golden_repos_dir, never this tmp dir => watch not suppressed; suppression covered in test_write_mode_watch_suppression.py

    def test_handle_create_file_starts_watch_on_exception_path(self):
        """Test that create_file starts auto-watch on canonical path for exceptions."""
        from code_indexer.server.mcp.handlers import handle_create_file

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            _admitted_exception_repo(tmpdir) as canonical_path,
        ):
            # Mock auto_watch_manager (imported inside handler)
            with patch(
                "code_indexer.server.services.auto_watch_manager.auto_watch_manager"
            ) as mock_watch:
                mock_watch.start_watch = Mock()

                params = {
                    "repository_alias": "cidx-meta-global",
                    "file_path": "dependency-map/test.md",
                    "content": "test content",
                }

                response = handle_create_file(params, _admin_user())

                _assert_succeeded(response)
                assert (canonical_path / "dependency-map" / "test.md").read_text() == (
                    "test content"
                )
                # Verify auto-watch was called with canonical path
                mock_watch.start_watch.assert_called_once()
                called_path = str(mock_watch.start_watch.call_args[0][0])
                assert called_path == str(canonical_path)

    def test_handle_edit_file_starts_watch_on_exception_path(self):
        """Test that edit_file starts auto-watch on canonical path for exceptions."""
        from code_indexer.server.mcp.handlers import handle_edit_file
        import hashlib

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            _admitted_exception_repo(tmpdir) as canonical_path,
        ):
            test_file = canonical_path / "test.md"
            original_content = "original"
            test_file.write_text(original_content)

            # Mock auto_watch_manager (imported inside handler)
            with patch(
                "code_indexer.server.services.auto_watch_manager.auto_watch_manager"
            ) as mock_watch:
                mock_watch.start_watch = Mock()

                content_hash = hashlib.sha256(original_content.encode()).hexdigest()
                params = {
                    "repository_alias": "cidx-meta-global",
                    "file_path": "test.md",
                    "old_string": "original",
                    "new_string": "modified",
                    "content_hash": content_hash,
                    "replace_all": False,
                }

                response = handle_edit_file(params, _admin_user())

                _assert_succeeded(response)
                assert test_file.read_text() == "modified"
                # Verify auto-watch was called with canonical path
                mock_watch.start_watch.assert_called_once()
                called_path = str(mock_watch.start_watch.call_args[0][0])
                assert called_path == str(canonical_path)

    def test_handle_delete_file_starts_watch_on_exception_path(self):
        """Test that delete_file starts auto-watch on canonical path for exceptions."""
        from code_indexer.server.mcp.handlers import handle_delete_file

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            _admitted_exception_repo(tmpdir) as canonical_path,
        ):
            test_file = canonical_path / "test.md"
            test_file.write_text("to delete")

            # Mock auto_watch_manager (imported inside handler)
            with patch(
                "code_indexer.server.services.auto_watch_manager.auto_watch_manager"
            ) as mock_watch:
                mock_watch.start_watch = Mock()

                params = {
                    "repository_alias": "cidx-meta-global",
                    "file_path": "test.md",
                }

                response = handle_delete_file(params, _admin_user())

                _assert_succeeded(response)
                assert not test_file.exists()
                # Verify auto-watch was called with canonical path
                mock_watch.start_watch.assert_called_once()
                called_path = str(mock_watch.start_watch.call_args[0][0])
                assert called_path == str(canonical_path)
