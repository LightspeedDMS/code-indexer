"""Tests for Bug #274: Watch handler suppression during write mode.

Bug 1: handle_edit_file / handle_create_file / handle_delete_file call
       auto_watch_manager.start_watch() unconditionally, even during write mode.
       During write mode the watch handler should NOT be started.

Bug 3: _write_mode_run_refresh() releases the write lock and calls
       _execute_refresh() but never stops the auto-watch first.
       The auto-watch races with the exit_write_mode refresh.

Fix for Bug 1: In handle_edit/create/delete_file, check if write mode marker
               exists for the repo before calling auto_watch_manager.start_watch().

Fix for Bug 3: _write_mode_run_refresh() must call
               auto_watch_manager.stop_watch(repo_path) before _execute_refresh().

Write mode marker location: golden_repos_dir/.write_mode/{alias}.json
(same marker used by _write_mode_create_marker / _write_mode_run_refresh)

Patch note: auto_watch_manager is imported locally inside each handler function
from code_indexer.server.services.auto_watch_manager. We patch the singleton
object at its source location so the local imports get the patched version.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(username: str = "testuser") -> User:
    return User(
        username=username,
        role=UserRole.POWER_USER,
        password_hash="dummy_hash",
        created_at=datetime.now(),
    )


def _extract_response_data(mcp_response: dict) -> dict:
    import json as _json
    content = mcp_response["content"][0]
    return cast(dict, _json.loads(content["text"]))


def _create_write_mode_marker(golden_repos_dir: Path, alias: str) -> Path:
    """Create a write-mode marker file for testing."""
    write_mode_dir = golden_repos_dir / ".write_mode"
    write_mode_dir.mkdir(parents=True, exist_ok=True)
    marker = write_mode_dir / f"{alias}.json"
    marker.write_text(
        json.dumps({
            "alias": alias,
            "source_path": str(golden_repos_dir / alias),
            "entered_at": datetime.now(timezone.utc).isoformat(),
        })
    )
    return marker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_repos_dir(tmp_path):
    """Real temporary golden_repos_dir."""
    d = tmp_path / "golden-repos"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def mock_auto_watch_manager():
    """Mock AutoWatchManager instance."""
    mgr = MagicMock()
    mgr.start_watch.return_value = {"status": "success", "message": "Watch started"}
    mgr.stop_watch.return_value = {"status": "success", "message": "Watch stopped"}
    return mgr


@pytest.fixture
def mock_file_crud_service(golden_repos_dir):
    """Mock FileCRUDService configured as write-exception repo."""
    svc = MagicMock()
    svc.is_write_exception.return_value = True
    svc.get_write_exception_path.return_value = golden_repos_dir / "cidx-meta"
    svc.create_file.return_value = {"success": True, "file_path": "test.md"}
    svc.edit_file.return_value = {"success": True, "file_path": "test.md"}
    svc.delete_file.return_value = {"success": True, "file_path": "test.md"}
    return svc


@pytest.fixture
def mock_refresh_scheduler(golden_repos_dir):
    """Mock RefreshScheduler with real write_lock_manager."""
    from code_indexer.global_repos.write_lock_manager import WriteLockManager

    scheduler = MagicMock()
    scheduler.write_lock_manager = WriteLockManager(golden_repos_dir=golden_repos_dir)

    def _acquire(alias, owner_name="refresh_scheduler"):
        return scheduler.write_lock_manager.acquire(alias, owner_name=owner_name)

    def _release(alias, owner_name="refresh_scheduler"):
        return scheduler.write_lock_manager.release(alias, owner_name=owner_name)

    scheduler.acquire_write_lock.side_effect = _acquire
    scheduler.release_write_lock.side_effect = _release
    scheduler._execute_refresh.return_value = {
        "success": True,
        "alias": "cidx-meta-global",
        "message": "Refresh complete",
    }
    return scheduler


# ---------------------------------------------------------------------------
# Bug 1 Tests: Watch suppression during write mode
# ---------------------------------------------------------------------------


class TestHandleEditFileWriteModeSuppression:
    """Bug 1: handle_edit_file must not start auto-watch during write mode."""

    def _call_handler(
        self,
        params,
        user,
        golden_repos_dir,
        mock_auto_watch_manager,
        mock_file_crud_service,
    ):
        from code_indexer.server.mcp import handlers
        from code_indexer.server import app as app_module

        # auto_watch_manager is imported locally inside handle_edit_file, so we patch
        # the singleton at the source module level.
        with patch(
            "code_indexer.server.services.auto_watch_manager.auto_watch_manager",
            mock_auto_watch_manager,
        ), patch(
            "code_indexer.server.services.file_crud_service.file_crud_service",
            mock_file_crud_service,
        ), patch.object(
            app_module.app.state,
            "golden_repos_dir",
            str(golden_repos_dir),
            create=True,
        ):
            return handlers.handle_edit_file(params, user)

    def test_start_watch_not_called_when_write_mode_active(
        self, golden_repos_dir, mock_auto_watch_manager, mock_file_crud_service
    ):
        """Bug 1: handle_edit_file must NOT call start_watch when write mode active."""
        # Create write-mode marker for cidx-meta
        _create_write_mode_marker(golden_repos_dir, "cidx-meta")

        user = _make_user()
        params = {
            "repository_alias": "cidx-meta-global",
            "file_path": "test.md",
            "old_string": "old",
            "new_string": "new",
            "content_hash": "abc123",
        }

        self._call_handler(
            params, user, golden_repos_dir, mock_auto_watch_manager, mock_file_crud_service
        )

        # start_watch must NOT be called when write mode is active
        mock_auto_watch_manager.start_watch.assert_not_called()

    def test_start_watch_called_when_write_mode_not_active(
        self, golden_repos_dir, mock_auto_watch_manager, mock_file_crud_service
    ):
        """Bug 1 negative: handle_edit_file MUST call start_watch when NOT in write mode."""
        # No write-mode marker created
        user = _make_user()
        params = {
            "repository_alias": "cidx-meta-global",
            "file_path": "test.md",
            "old_string": "old",
            "new_string": "new",
            "content_hash": "abc123",
        }

        self._call_handler(
            params, user, golden_repos_dir, mock_auto_watch_manager, mock_file_crud_service
        )

        # start_watch MUST be called when write mode is NOT active
        mock_auto_watch_manager.start_watch.assert_called_once()


class TestHandleCreateFileWriteModeSuppression:
    """Bug 1: handle_create_file must not start auto-watch during write mode."""

    def _call_handler(
        self,
        params,
        user,
        golden_repos_dir,
        mock_auto_watch_manager,
        mock_file_crud_service,
    ):
        from code_indexer.server.mcp import handlers
        from code_indexer.server import app as app_module

        with patch(
            "code_indexer.server.services.auto_watch_manager.auto_watch_manager",
            mock_auto_watch_manager,
        ), patch(
            "code_indexer.server.services.file_crud_service.file_crud_service",
            mock_file_crud_service,
        ), patch.object(
            app_module.app.state,
            "golden_repos_dir",
            str(golden_repos_dir),
            create=True,
        ):
            return handlers.handle_create_file(params, user)

    def test_start_watch_not_called_when_write_mode_active(
        self, golden_repos_dir, mock_auto_watch_manager, mock_file_crud_service
    ):
        """Bug 1: handle_create_file must NOT call start_watch when write mode active."""
        _create_write_mode_marker(golden_repos_dir, "cidx-meta")

        user = _make_user()
        params = {
            "repository_alias": "cidx-meta-global",
            "file_path": "new_file.md",
            "content": "hello",
        }

        self._call_handler(
            params, user, golden_repos_dir, mock_auto_watch_manager, mock_file_crud_service
        )

        mock_auto_watch_manager.start_watch.assert_not_called()

    def test_start_watch_called_when_write_mode_not_active(
        self, golden_repos_dir, mock_auto_watch_manager, mock_file_crud_service
    ):
        """Bug 1 negative: handle_create_file MUST call start_watch when NOT in write mode."""
        user = _make_user()
        params = {
            "repository_alias": "cidx-meta-global",
            "file_path": "new_file.md",
            "content": "hello",
        }

        self._call_handler(
            params, user, golden_repos_dir, mock_auto_watch_manager, mock_file_crud_service
        )

        mock_auto_watch_manager.start_watch.assert_called_once()


class TestHandleDeleteFileWriteModeSuppression:
    """Bug 1: handle_delete_file must not start auto-watch during write mode."""

    def _call_handler(
        self,
        params,
        user,
        golden_repos_dir,
        mock_auto_watch_manager,
        mock_file_crud_service,
    ):
        from code_indexer.server.mcp import handlers
        from code_indexer.server import app as app_module

        with patch(
            "code_indexer.server.services.auto_watch_manager.auto_watch_manager",
            mock_auto_watch_manager,
        ), patch(
            "code_indexer.server.services.file_crud_service.file_crud_service",
            mock_file_crud_service,
        ), patch.object(
            app_module.app.state,
            "golden_repos_dir",
            str(golden_repos_dir),
            create=True,
        ):
            return handlers.handle_delete_file(params, user)

    def test_start_watch_not_called_when_write_mode_active(
        self, golden_repos_dir, mock_auto_watch_manager, mock_file_crud_service
    ):
        """Bug 1: handle_delete_file must NOT call start_watch when write mode active."""
        _create_write_mode_marker(golden_repos_dir, "cidx-meta")

        user = _make_user()
        params = {
            "repository_alias": "cidx-meta-global",
            "file_path": "old_file.md",
        }

        self._call_handler(
            params, user, golden_repos_dir, mock_auto_watch_manager, mock_file_crud_service
        )

        mock_auto_watch_manager.start_watch.assert_not_called()

    def test_start_watch_called_when_write_mode_not_active(
        self, golden_repos_dir, mock_auto_watch_manager, mock_file_crud_service
    ):
        """Bug 1 negative: handle_delete_file MUST call start_watch when NOT in write mode."""
        user = _make_user()
        params = {
            "repository_alias": "cidx-meta-global",
            "file_path": "old_file.md",
        }

        self._call_handler(
            params, user, golden_repos_dir, mock_auto_watch_manager, mock_file_crud_service
        )

        mock_auto_watch_manager.start_watch.assert_called_once()


# ---------------------------------------------------------------------------
# Bug 3 Tests: stop auto-watch before exit_write_mode refresh
# ---------------------------------------------------------------------------


class TestWriteModeRunRefreshStopsWatch:
    """Bug 3: _write_mode_run_refresh must stop auto-watch before _execute_refresh."""

    def test_stop_watch_called_before_execute_refresh(
        self, golden_repos_dir, mock_refresh_scheduler, mock_auto_watch_manager
    ):
        """Bug 3: stop_watch must be called before _execute_refresh.

        The auto-watch races with the refresh if not stopped first.
        This test verifies the ordering: stop_watch → _execute_refresh.
        """
        # Create write mode marker (will be deleted by _write_mode_run_refresh)
        _create_write_mode_marker(golden_repos_dir, "cidx-meta")
        # Acquire lock (will be released by _write_mode_run_refresh)
        mock_refresh_scheduler.write_lock_manager.acquire(
            "cidx-meta", owner_name="mcp_write_mode"
        )

        call_order = []

        def _record_stop(repo_path):
            call_order.append(("stop_watch", repo_path))
            return {"status": "success", "message": "Watch stopped"}

        def _record_refresh(alias):
            call_order.append(("execute_refresh", alias))
            return {"success": True}

        mock_auto_watch_manager.stop_watch.side_effect = _record_stop
        mock_refresh_scheduler._execute_refresh.side_effect = _record_refresh

        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.services.auto_watch_manager.auto_watch_manager",
            mock_auto_watch_manager,
        ):
            handlers._write_mode_run_refresh(
                mock_refresh_scheduler,
                "cidx-meta-global",
                golden_repos_dir,
                "cidx-meta",
            )

        # stop_watch must have been called before execute_refresh
        assert len(call_order) >= 2, (
            f"Expected at least 2 calls (stop_watch + execute_refresh). Got: {call_order}"
        )
        assert call_order[0][0] == "stop_watch", (
            f"stop_watch must be called FIRST. Call order: {call_order}"
        )
        assert call_order[1][0] == "execute_refresh", (
            f"execute_refresh must be called SECOND. Call order: {call_order}"
        )

    def test_stop_watch_called_even_when_execute_refresh_raises(
        self, golden_repos_dir, mock_refresh_scheduler, mock_auto_watch_manager
    ):
        """Bug 3: stop_watch must be called even when _execute_refresh raises.

        If refresh fails, the watch must still be stopped to prevent racing on retry.
        """
        _create_write_mode_marker(golden_repos_dir, "cidx-meta")
        mock_refresh_scheduler.write_lock_manager.acquire(
            "cidx-meta", owner_name="mcp_write_mode"
        )
        mock_refresh_scheduler._execute_refresh.side_effect = RuntimeError("refresh failed")

        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.services.auto_watch_manager.auto_watch_manager",
            mock_auto_watch_manager,
        ):
            with pytest.raises(RuntimeError, match="refresh failed"):
                handlers._write_mode_run_refresh(
                    mock_refresh_scheduler,
                    "cidx-meta-global",
                    golden_repos_dir,
                    "cidx-meta",
                )

        # stop_watch must have been called even though refresh raised
        mock_auto_watch_manager.stop_watch.assert_called()

    def test_stop_watch_called_with_correct_repo_path(
        self, golden_repos_dir, mock_refresh_scheduler, mock_auto_watch_manager
    ):
        """Bug 3: stop_watch must be called with the source_path from the write-mode marker.

        The marker contains source_path which is the canonical path to the repo.
        _write_mode_run_refresh must read this path from the marker and pass it to stop_watch.
        """
        _create_write_mode_marker(golden_repos_dir, "cidx-meta")
        mock_refresh_scheduler.write_lock_manager.acquire(
            "cidx-meta", owner_name="mcp_write_mode"
        )

        # The marker contains source_path = golden_repos_dir / "cidx-meta"
        expected_repo_path = str(golden_repos_dir / "cidx-meta")

        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.services.auto_watch_manager.auto_watch_manager",
            mock_auto_watch_manager,
        ):
            handlers._write_mode_run_refresh(
                mock_refresh_scheduler,
                "cidx-meta-global",
                golden_repos_dir,
                "cidx-meta",
            )

        # Verify stop_watch was called with the source_path from the marker
        mock_auto_watch_manager.stop_watch.assert_called_once_with(expected_repo_path)
