"""
Unit tests for Story #231: enter_write_mode / exit_write_mode MCP Tools.

Tests cover:
- C1: handle_enter_write_mode handler
- C2: handle_exit_write_mode handler
- C3: AliasManager.read_alias write-mode marker redirection
- C4: FileCRUDService write-mode enforcement

All tests use real filesystem operations (temp dirs) wherever possible.
Mocking is limited to external dependencies (refresh_scheduler._execute_refresh,
app.state, api_metrics_service).
"""

import json
from datetime import datetime, timezone
from typing import cast
from unittest.mock import MagicMock, patch
import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.services.file_crud_service import FileCRUDService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_response_data(mcp_response: dict) -> dict:
    """Extract actual response data from MCP content wrapper."""
    content = mcp_response["content"][0]
    return cast(dict, json.loads(content["text"]))


def _make_user(username: str = "testuser") -> User:
    return User(
        username=username,
        role=UserRole.POWER_USER,
        password_hash="dummy_hash",
        created_at=datetime.now(),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_repos_dir(tmp_path):
    """Provide a real temporary golden_repos_dir for file-based tests."""
    d = tmp_path / "golden-repos"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def write_mode_dir(golden_repos_dir):
    """Provide the .write_mode sub-directory."""
    d = golden_repos_dir / ".write_mode"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def cidx_meta_source(golden_repos_dir):
    """Create a fake cidx-meta source directory."""
    src = golden_repos_dir / "cidx-meta"
    src.mkdir(parents=True)
    return src


@pytest.fixture
def alias_manager(golden_repos_dir):
    """AliasManager pointing at a real aliases directory."""
    aliases_dir = golden_repos_dir / "aliases"
    aliases_dir.mkdir(parents=True)
    return AliasManager(str(aliases_dir))


@pytest.fixture
def mock_refresh_scheduler(golden_repos_dir):
    """A mock RefreshScheduler with a real write_lock_manager backed by temp dir."""
    from code_indexer.global_repos.write_lock_manager import WriteLockManager

    scheduler = MagicMock()
    scheduler.write_lock_manager = WriteLockManager(golden_repos_dir=golden_repos_dir)

    def _acquire(alias, owner_name="refresh_scheduler"):
        return scheduler.write_lock_manager.acquire(alias, owner_name=owner_name)

    def _release(alias, owner_name="refresh_scheduler"):
        return scheduler.write_lock_manager.release(alias, owner_name=owner_name)

    def _is_locked(alias):
        return scheduler.write_lock_manager.is_locked(alias)

    scheduler.acquire_write_lock.side_effect = _acquire
    scheduler.release_write_lock.side_effect = _release
    scheduler.is_write_locked.side_effect = _is_locked
    # _execute_refresh is mocked by default (returns success dict)
    scheduler._execute_refresh.return_value = {
        "success": True,
        "alias": "cidx-meta-global",
        "message": "Refresh complete",
    }
    return scheduler


@pytest.fixture
def file_crud_service_with_exception(golden_repos_dir, cidx_meta_source):
    """FileCRUDService with cidx-meta-global registered as write exception."""
    service = FileCRUDService()
    service.register_write_exception("cidx-meta-global", cidx_meta_source)
    # Expose golden_repos_dir so enforcement logic can find write_mode markers
    service._golden_repos_dir = golden_repos_dir
    return service


# ===========================================================================
# C1: handle_enter_write_mode
# ===========================================================================


class TestHandleEnterWriteMode:
    """Tests for handle_enter_write_mode MCP handler."""

    def _call_handler(self, params, user, refresh_scheduler, golden_repos_dir):
        """Invoke handler with patched app state."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._get_app_refresh_scheduler",
            return_value=refresh_scheduler,
        ), patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=str(golden_repos_dir),
        ):
            return handlers.handle_enter_write_mode(params, user)

    def test_creates_marker_file_for_write_exception_repo(
        self, mock_refresh_scheduler, golden_repos_dir, cidx_meta_source
    ):
        """AC1: enter_write_mode creates marker file for write-exception repo."""
        user = _make_user()
        params = {"repo_alias": "cidx-meta-global"}

        result = self._call_handler(params, user, mock_refresh_scheduler, golden_repos_dir)
        data = _extract_response_data(result)

        assert data["success"] is True
        assert data["alias"] == "cidx-meta-global"

        # Marker file must exist
        marker = golden_repos_dir / ".write_mode" / "cidx-meta.json"
        assert marker.exists(), "Marker file must be created"

        content = json.loads(marker.read_text())
        assert content["alias"] == "cidx-meta"
        assert "source_path" in content
        assert "entered_at" in content

    def test_acquires_write_lock_for_write_exception_repo(
        self, mock_refresh_scheduler, golden_repos_dir, cidx_meta_source
    ):
        """AC1: Lock must be acquired on enter for write-exception repo."""
        user = _make_user()
        params = {"repo_alias": "cidx-meta-global"}

        self._call_handler(params, user, mock_refresh_scheduler, golden_repos_dir)

        mock_refresh_scheduler.acquire_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="mcp_write_mode"
        )

    def test_noop_for_activated_repo(
        self, mock_refresh_scheduler, golden_repos_dir
    ):
        """AC2: enter_write_mode is silent no-op for activated (non-exception) repos."""
        user = _make_user()
        params = {"repo_alias": "some-normal-repo-global"}

        result = self._call_handler(params, user, mock_refresh_scheduler, golden_repos_dir)
        data = _extract_response_data(result)

        assert data["success"] is True
        assert "no-op" in data["message"].lower()

        # No lock must be acquired
        mock_refresh_scheduler.acquire_write_lock.assert_not_called()

        # No marker file must be created
        write_mode_dir = golden_repos_dir / ".write_mode"
        if write_mode_dir.exists():
            assert list(write_mode_dir.iterdir()) == [], "No marker files must be created for no-op"

    def test_returns_error_when_lock_already_held(
        self, mock_refresh_scheduler, golden_repos_dir, cidx_meta_source
    ):
        """AC9: Returns error when lock is already held by another owner."""
        # Pre-acquire the lock as a different owner
        mock_refresh_scheduler.write_lock_manager.acquire("cidx-meta", owner_name="other_service")

        user = _make_user()
        params = {"repo_alias": "cidx-meta-global"}

        result = self._call_handler(params, user, mock_refresh_scheduler, golden_repos_dir)
        data = _extract_response_data(result)

        assert data["success"] is False
        assert "lock" in data["message"].lower() or "held" in data["message"].lower()

        # No marker file must be created
        marker = golden_repos_dir / ".write_mode" / "cidx-meta.json"
        assert not marker.exists(), "No marker must be created when lock fails"

    def test_missing_repo_alias_returns_error(
        self, mock_refresh_scheduler, golden_repos_dir
    ):
        """Returns error when repo_alias is missing."""
        user = _make_user()
        params = {}

        result = self._call_handler(params, user, mock_refresh_scheduler, golden_repos_dir)
        data = _extract_response_data(result)

        assert data["success"] is False
        assert "repo_alias" in data["error"].lower()


# ===========================================================================
# C2: handle_exit_write_mode
# ===========================================================================


class TestHandleExitWriteMode:
    """Tests for handle_exit_write_mode MCP handler."""

    def _call_handler(self, params, user, refresh_scheduler, golden_repos_dir):
        """Invoke handler with patched app state."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._get_app_refresh_scheduler",
            return_value=refresh_scheduler,
        ), patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=str(golden_repos_dir),
        ):
            return handlers.handle_exit_write_mode(params, user)

    def _create_marker(self, golden_repos_dir, alias_name, source_path):
        """Helper: create a valid write-mode marker file."""
        write_mode_dir = golden_repos_dir / ".write_mode"
        write_mode_dir.mkdir(parents=True, exist_ok=True)
        marker = write_mode_dir / f"{alias_name}.json"
        marker.write_text(
            json.dumps(
                {
                    "alias": alias_name,
                    "source_path": str(source_path),
                    "entered_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
        return marker

    def test_calls_execute_refresh_synchronously(
        self, mock_refresh_scheduler, golden_repos_dir, cidx_meta_source
    ):
        """AC5: exit_write_mode calls _execute_refresh synchronously."""
        self._create_marker(golden_repos_dir, "cidx-meta", cidx_meta_source)
        # Pre-acquire lock
        mock_refresh_scheduler.write_lock_manager.acquire("cidx-meta", owner_name="mcp_write_mode")

        user = _make_user()
        params = {"repo_alias": "cidx-meta-global"}

        result = self._call_handler(params, user, mock_refresh_scheduler, golden_repos_dir)
        data = _extract_response_data(result)

        assert data["success"] is True
        # _execute_refresh must have been called directly (synchronously)
        mock_refresh_scheduler._execute_refresh.assert_called_once_with("cidx-meta-global")

    def test_removes_marker_file_after_refresh(
        self, mock_refresh_scheduler, golden_repos_dir, cidx_meta_source
    ):
        """AC5: Marker file is deleted after refresh completes."""
        marker = self._create_marker(golden_repos_dir, "cidx-meta", cidx_meta_source)
        mock_refresh_scheduler.write_lock_manager.acquire("cidx-meta", owner_name="mcp_write_mode")

        user = _make_user()
        params = {"repo_alias": "cidx-meta-global"}

        self._call_handler(params, user, mock_refresh_scheduler, golden_repos_dir)

        assert not marker.exists(), "Marker file must be deleted after successful exit"

    def test_releases_write_lock_after_refresh(
        self, mock_refresh_scheduler, golden_repos_dir, cidx_meta_source
    ):
        """AC5: Write lock is released after exit."""
        self._create_marker(golden_repos_dir, "cidx-meta", cidx_meta_source)
        mock_refresh_scheduler.write_lock_manager.acquire("cidx-meta", owner_name="mcp_write_mode")

        user = _make_user()
        params = {"repo_alias": "cidx-meta-global"}

        self._call_handler(params, user, mock_refresh_scheduler, golden_repos_dir)

        mock_refresh_scheduler.release_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="mcp_write_mode"
        )

    def test_noop_for_activated_repo(
        self, mock_refresh_scheduler, golden_repos_dir
    ):
        """AC6: exit_write_mode is silent no-op for activated (non-exception) repos."""
        user = _make_user()
        params = {"repo_alias": "some-normal-repo-global"}

        result = self._call_handler(params, user, mock_refresh_scheduler, golden_repos_dir)
        data = _extract_response_data(result)

        assert data["success"] is True
        assert "no-op" in data["message"].lower()

        # No refresh should be triggered
        mock_refresh_scheduler._execute_refresh.assert_not_called()
        mock_refresh_scheduler.release_write_lock.assert_not_called()

    def test_warning_when_no_marker_file(
        self, mock_refresh_scheduler, golden_repos_dir
    ):
        """Returns warning when marker file is missing (write mode was not entered)."""
        user = _make_user()
        params = {"repo_alias": "cidx-meta-global"}

        result = self._call_handler(params, user, mock_refresh_scheduler, golden_repos_dir)
        data = _extract_response_data(result)

        # Should return success but with a warning
        assert data["success"] is True
        assert "warning" in data or "not in write mode" in data.get("message", "").lower()

        # No refresh should be triggered when no marker
        mock_refresh_scheduler._execute_refresh.assert_not_called()

    def test_lock_released_before_execute_refresh(
        self, mock_refresh_scheduler, golden_repos_dir, cidx_meta_source
    ):
        """AC5 ordering: write lock is released BEFORE _execute_refresh is called.

        _execute_refresh checks is_write_locked() for local repos and skips
        if the lock is held (Story #227 guard).  The fix ensures we release
        the lock first so the refresh is not silently skipped.
        """
        self._create_marker(golden_repos_dir, "cidx-meta", cidx_meta_source)
        mock_refresh_scheduler.write_lock_manager.acquire("cidx-meta", owner_name="mcp_write_mode")

        lock_held_during_refresh: list[bool] = []

        def _capture_lock_state(alias_name):
            # At the moment _execute_refresh is called, lock must already be released
            is_locked = mock_refresh_scheduler.write_lock_manager.is_locked("cidx-meta")
            lock_held_during_refresh.append(is_locked)
            return {"success": True, "alias": alias_name, "message": "Refresh complete"}

        mock_refresh_scheduler._execute_refresh.side_effect = _capture_lock_state

        user = _make_user()
        params = {"repo_alias": "cidx-meta-global"}
        result = self._call_handler(params, user, mock_refresh_scheduler, golden_repos_dir)
        data = _extract_response_data(result)

        assert data["success"] is True
        assert lock_held_during_refresh, "_execute_refresh must have been called"
        assert lock_held_during_refresh[0] is False, (
            "Write lock must be released BEFORE _execute_refresh is called, "
            "otherwise _execute_refresh skips the refresh (Story #227 guard)"
        )

    def test_refresh_failure_releases_lock_and_removes_marker(
        self, mock_refresh_scheduler, golden_repos_dir, cidx_meta_source
    ):
        """Lock is released and marker is removed even when _execute_refresh raises."""
        marker = self._create_marker(golden_repos_dir, "cidx-meta", cidx_meta_source)
        mock_refresh_scheduler.write_lock_manager.acquire("cidx-meta", owner_name="mcp_write_mode")

        # Make refresh raise
        mock_refresh_scheduler._execute_refresh.side_effect = RuntimeError("refresh exploded")

        user = _make_user()
        params = {"repo_alias": "cidx-meta-global"}

        result = self._call_handler(params, user, mock_refresh_scheduler, golden_repos_dir)
        data = _extract_response_data(result)

        # Response must signal failure
        assert data["success"] is False
        assert data.get("error"), "Error message must be present"

        # Marker file must be removed despite the refresh failure
        assert not marker.exists(), "Marker must be cleaned up even when refresh fails"

        # Lock must be released despite the refresh failure
        assert not mock_refresh_scheduler.write_lock_manager.is_locked("cidx-meta"), (
            "Write lock must be released even when refresh fails"
        )


# ===========================================================================
# C3: AliasManager.read_alias write-mode marker redirection
# ===========================================================================


class TestAliasManagerReadAliasWithWriteMode:
    """Tests for AliasManager.read_alias write-mode redirection (C3)."""

    def test_returns_versioned_path_when_no_marker(self, alias_manager, golden_repos_dir):
        """AC4: Returns normal versioned path when not in write mode."""
        alias_manager.create_alias("cidx-meta-global", "/versioned/path/cidx-meta/v_123")
        result = alias_manager.read_alias("cidx-meta-global")
        assert result == "/versioned/path/cidx-meta/v_123"

    def test_returns_source_path_when_marker_present(self, alias_manager, golden_repos_dir):
        """AC3: Returns source_path from marker when write-mode marker exists."""
        alias_manager.create_alias("cidx-meta-global", "/versioned/path/cidx-meta/v_123")

        # Create marker file
        write_mode_dir = golden_repos_dir / ".write_mode"
        write_mode_dir.mkdir(parents=True, exist_ok=True)
        source_path = str(golden_repos_dir / "cidx-meta")
        marker_file = write_mode_dir / "cidx-meta.json"
        marker_file.write_text(
            json.dumps(
                {
                    "alias": "cidx-meta",
                    "source_path": source_path,
                    "entered_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )

        result = alias_manager.read_alias("cidx-meta-global")
        assert result == source_path, "Should return source_path from write-mode marker"

    def test_falls_back_to_versioned_path_on_corrupt_marker(
        self, alias_manager, golden_repos_dir
    ):
        """AC3 fallback: Falls back to versioned path on corrupt marker (JSONDecodeError)."""
        alias_manager.create_alias("cidx-meta-global", "/versioned/path/cidx-meta/v_123")

        # Create corrupt marker file
        write_mode_dir = golden_repos_dir / ".write_mode"
        write_mode_dir.mkdir(parents=True, exist_ok=True)
        marker_file = write_mode_dir / "cidx-meta.json"
        marker_file.write_text("{ this is not valid json !!!")

        result = alias_manager.read_alias("cidx-meta-global")
        assert result == "/versioned/path/cidx-meta/v_123", "Should fall back on corrupt marker"

    def test_falls_back_when_marker_missing_source_path(
        self, alias_manager, golden_repos_dir
    ):
        """AC3 fallback: Falls back to versioned path when marker has no source_path."""
        alias_manager.create_alias("cidx-meta-global", "/versioned/path/cidx-meta/v_123")

        # Create marker without source_path
        write_mode_dir = golden_repos_dir / ".write_mode"
        write_mode_dir.mkdir(parents=True, exist_ok=True)
        marker_file = write_mode_dir / "cidx-meta.json"
        marker_file.write_text(
            json.dumps({"alias": "cidx-meta", "entered_at": "2026-01-01T00:00:00Z"})
        )

        result = alias_manager.read_alias("cidx-meta-global")
        assert result == "/versioned/path/cidx-meta/v_123", "Should fall back when source_path missing"

    def test_no_marker_dir_does_not_break_read_alias(self, alias_manager, golden_repos_dir):
        """C3: read_alias works even when .write_mode directory doesn't exist."""
        alias_manager.create_alias("cidx-meta-global", "/versioned/path/cidx-meta/v_123")
        # Do NOT create the write_mode dir
        result = alias_manager.read_alias("cidx-meta-global")
        assert result == "/versioned/path/cidx-meta/v_123"

    def test_non_global_alias_not_redirected(self, alias_manager, golden_repos_dir):
        """C3: Only -global suffix aliases are checked for write-mode markers."""
        alias_manager.create_alias("some-other-alias", "/other/path")

        # Even if a marker exists, non-global aliases should not be redirected
        write_mode_dir = golden_repos_dir / ".write_mode"
        write_mode_dir.mkdir(parents=True, exist_ok=True)
        # Create a marker for "some-other-alias" (without -global)
        marker_file = write_mode_dir / "some-other-alias.json"
        marker_file.write_text(
            json.dumps({"alias": "some-other-alias", "source_path": "/other/source"})
        )

        # AliasManager for non-global alias: normal resolution
        result = alias_manager.read_alias("some-other-alias")
        assert result == "/other/path"


# ===========================================================================
# C4: FileCRUDService write-mode enforcement
# ===========================================================================


class TestFileCRUDServiceWriteModeEnforcement:
    """Tests for FileCRUDService write-mode enforcement (C4)."""

    def test_create_file_raises_permission_error_without_write_mode(
        self, file_crud_service_with_exception, golden_repos_dir
    ):
        """AC7: create_file raises PermissionError for write-exception repo without marker."""
        service = file_crud_service_with_exception
        # Ensure no marker exists
        write_mode_dir = golden_repos_dir / ".write_mode"
        if write_mode_dir.exists():
            for f in write_mode_dir.iterdir():
                f.unlink()

        with patch(
            "code_indexer.server.services.api_metrics_service.api_metrics_service"
        ) as _mock_metrics:
            _mock_metrics.increment_other_api_call.return_value = None
            with pytest.raises(PermissionError) as exc_info:
                service.create_file(
                    repo_alias="cidx-meta-global",
                    file_path="test.md",
                    content="hello",
                    username="testuser",
                )

        assert "write mode" in str(exc_info.value).lower() or "enter_write_mode" in str(exc_info.value)

    def test_create_file_succeeds_with_valid_write_mode_marker(
        self, file_crud_service_with_exception, golden_repos_dir, cidx_meta_source
    ):
        """AC8: create_file succeeds for write-exception repo when write-mode marker exists."""
        service = file_crud_service_with_exception

        # Create valid marker
        write_mode_dir = golden_repos_dir / ".write_mode"
        write_mode_dir.mkdir(parents=True, exist_ok=True)
        marker = write_mode_dir / "cidx-meta.json"
        marker.write_text(
            json.dumps(
                {
                    "alias": "cidx-meta",
                    "source_path": str(cidx_meta_source),
                    "entered_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )

        with patch(
            "code_indexer.server.services.api_metrics_service.api_metrics_service"
        ) as mock_metrics:
            mock_metrics.increment_other_api_call.return_value = None
            result = service.create_file(
                repo_alias="cidx-meta-global",
                file_path="new_test_file.md",
                content="hello world",
                username="testuser",
            )

        assert result["success"] is True
        assert (cidx_meta_source / "new_test_file.md").exists()

    def test_edit_file_raises_permission_error_without_write_mode(
        self, file_crud_service_with_exception, golden_repos_dir, cidx_meta_source
    ):
        """AC7: edit_file raises PermissionError for write-exception repo without marker."""
        service = file_crud_service_with_exception

        # Create a file to edit
        test_file = cidx_meta_source / "existing.md"
        test_file.write_text("original content")

        # Ensure no marker
        write_mode_dir = golden_repos_dir / ".write_mode"
        if write_mode_dir.exists():
            for f in write_mode_dir.iterdir():
                f.unlink()

        with patch(
            "code_indexer.server.services.api_metrics_service.api_metrics_service"
        ) as mock_metrics:
            mock_metrics.increment_other_api_call.return_value = None
            with pytest.raises(PermissionError):
                service.edit_file(
                    repo_alias="cidx-meta-global",
                    file_path="existing.md",
                    old_string="original",
                    new_string="changed",
                    content_hash="dummy_hash",
                    replace_all=True,
                    username="testuser",
                )

    def test_delete_file_raises_permission_error_without_write_mode(
        self, file_crud_service_with_exception, golden_repos_dir, cidx_meta_source
    ):
        """AC7: delete_file raises PermissionError for write-exception repo without marker."""
        service = file_crud_service_with_exception

        # Create a file to delete
        test_file = cidx_meta_source / "to_delete.md"
        test_file.write_text("delete me")

        # Ensure no marker
        write_mode_dir = golden_repos_dir / ".write_mode"
        if write_mode_dir.exists():
            for f in write_mode_dir.iterdir():
                f.unlink()

        with patch(
            "code_indexer.server.services.api_metrics_service.api_metrics_service"
        ) as mock_metrics:
            mock_metrics.increment_other_api_call.return_value = None
            with pytest.raises(PermissionError):
                service.delete_file(
                    repo_alias="cidx-meta-global",
                    file_path="to_delete.md",
                    content_hash=None,
                    username="testuser",
                )

    def test_non_write_exception_repo_not_affected(
        self, file_crud_service_with_exception, golden_repos_dir
    ):
        """Non-write-exception repos are not gated by write mode."""
        service = file_crud_service_with_exception

        # Ensure no marker exists
        write_mode_dir = golden_repos_dir / ".write_mode"
        if write_mode_dir.exists():
            for f in write_mode_dir.iterdir():
                f.unlink()

        # For a normal repo (not a write exception), the gating should not apply.
        # The call will fail because there's no activated repo either,
        # but it should NOT raise PermissionError about write mode.
        with patch(
            "code_indexer.server.services.api_metrics_service.api_metrics_service"
        ) as mock_metrics:
            mock_metrics.increment_other_api_call.return_value = None
            with patch.object(
                service.activated_repo_manager,
                "get_activated_repo_path",
                return_value=str(golden_repos_dir / "some-other-repo"),
            ):
                # Create target dir
                (golden_repos_dir / "some-other-repo").mkdir(parents=True, exist_ok=True)
                # This should not raise PermissionError about write mode
                # (it may raise FileExistsError or succeed depending on state)
                try:
                    service.create_file(
                        repo_alias="some-other-repo",
                        file_path="test.txt",
                        content="hello",
                        username="testuser",
                    )
                except PermissionError as e:
                    # Ensure it's NOT the write-mode PermissionError
                    assert "write mode" not in str(e).lower(), (
                        f"Non-write-exception repo should not be gated by write mode, got: {e}"
                    )
                except Exception:
                    pass  # Other errors are acceptable
