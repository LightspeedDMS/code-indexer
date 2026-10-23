"""
MCP file tools on a write exception repo (cidx-meta-global) start no auto-watch.

Every write to a write exception repo requires write mode, and write-mode exit
owns the refresh, so a write the real write-mode gate admits succeeds and
starts no auto-watch. The handler's watch check and the file service's gate
read the SAME golden-repos directory here, as they do on a server. A refused
write starts no watch either: see
test_write_mode_watch_suppression.py::TestRefusedWriteStartsNoWatch.
"""

import hashlib
import json
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator
from unittest.mock import Mock, patch

from code_indexer.server.auth.user_manager import User, UserRole
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    build_cidx_meta_fixture,
)
from tests.shared.real_write_mode import real_write_mode


@contextmanager
def _admitted_write_mode(tmpdir: str) -> Iterator[Path]:
    """cidx-meta-global registered as a write exception and in real write
    mode, one golden-repos directory shared by the file service and
    app.state, and the real access service installed so the admin caller
    passes the catalog path gate; all singleton state is restored afterwards."""
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
            patch.object(
                app_state, "golden_repos_dir", str(golden_repos_dir), create=True
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


def _watch_manager() -> Any:
    # auto_watch_manager is imported inside the handler, so it is patched at
    # its source module.
    return patch(
        "code_indexer.server.services.auto_watch_manager.auto_watch_manager",
        Mock(),
    )


class TestAdmittedWriteModeWriteStartsNoWatch:
    """An admitted write-mode write on a write exception repo starts no watch."""

    def test_admitted_create_file_starts_no_watch(self):
        from code_indexer.server.mcp.handlers import handle_create_file

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            _admitted_write_mode(tmpdir) as canonical_path,
            _watch_manager() as mock_watch,
        ):
            response = handle_create_file(
                {
                    "repository_alias": "cidx-meta-global",
                    "file_path": "dependency-map/test.md",
                    "content": "test content",
                },
                _admin_user(),
            )

            _assert_succeeded(response)
            assert (canonical_path / "dependency-map" / "test.md").read_text() == (
                "test content"
            )
            mock_watch.start_watch.assert_not_called()

    def test_admitted_edit_file_starts_no_watch(self):
        from code_indexer.server.mcp.handlers import handle_edit_file

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            _admitted_write_mode(tmpdir) as canonical_path,
            _watch_manager() as mock_watch,
        ):
            test_file = canonical_path / "test.md"
            test_file.write_text("original")

            response = handle_edit_file(
                {
                    "repository_alias": "cidx-meta-global",
                    "file_path": "test.md",
                    "old_string": "original",
                    "new_string": "modified",
                    "content_hash": hashlib.sha256(b"original").hexdigest(),
                    "replace_all": False,
                },
                _admin_user(),
            )

            _assert_succeeded(response)
            assert test_file.read_text() == "modified"
            mock_watch.start_watch.assert_not_called()

    def test_admitted_delete_file_starts_no_watch(self):
        from code_indexer.server.mcp.handlers import handle_delete_file

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            _admitted_write_mode(tmpdir) as canonical_path,
            _watch_manager() as mock_watch,
        ):
            test_file = canonical_path / "test.md"
            test_file.write_text("to delete")

            response = handle_delete_file(
                {"repository_alias": "cidx-meta-global", "file_path": "test.md"},
                _admin_user(),
            )

            _assert_succeeded(response)
            assert not test_file.exists()
            mock_watch.start_watch.assert_not_called()
