"""
Unit tests for Story #270: Local Repo Indexing Lifecycle.

Tests the new set_refresh_scheduler() setter and trigger_refresh_for_repo() calls
added to meta_description_hook.py as part of Story #270.

Acceptance criteria tested:
- AC3: cidx-meta meta descriptions indexed after golden repo add
  (trigger_refresh_for_repo called on on_repo_added)
- AC4: cidx-meta meta descriptions removed after delete
  (trigger_refresh_for_repo called on on_repo_removed)
- AC6: Refresh trigger is non-blocking (queued via BackgroundJobManager)
"""

import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level state before and after each test."""
    import code_indexer.global_repos.meta_description_hook as hook_module

    # Save original state
    original_refresh_scheduler = getattr(hook_module, "_refresh_scheduler", None)
    original_tracking_backend = getattr(hook_module, "_tracking_backend", None)
    original_scheduler = getattr(hook_module, "_scheduler", None)

    # Clear state before test
    hook_module._refresh_scheduler = None
    hook_module._tracking_backend = None
    hook_module._scheduler = None

    yield

    # Restore original state after test
    hook_module._refresh_scheduler = original_refresh_scheduler
    hook_module._tracking_backend = original_tracking_backend
    hook_module._scheduler = original_scheduler


@pytest.fixture
def temp_golden_repos_dir():
    """Create temporary golden repos directory with cidx-meta subdir."""
    temp_dir = tempfile.mkdtemp()
    cidx_meta = Path(temp_dir) / "cidx-meta"
    cidx_meta.mkdir(parents=True)
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def mock_refresh_scheduler():
    """Create a mock RefreshScheduler."""
    scheduler = MagicMock()
    scheduler.trigger_refresh_for_repo.return_value = "job-id-123"
    return scheduler


class TestSetRefreshScheduler:
    """Tests for the new set_refresh_scheduler() module-level setter."""

    def test_set_refresh_scheduler_stores_scheduler(self, mock_refresh_scheduler):
        """Test that set_refresh_scheduler stores the scheduler in module state."""
        from code_indexer.global_repos.meta_description_hook import (
            set_refresh_scheduler,
        )
        import code_indexer.global_repos.meta_description_hook as hook_module

        set_refresh_scheduler(mock_refresh_scheduler)

        assert hook_module._refresh_scheduler is mock_refresh_scheduler

    def test_set_refresh_scheduler_accepts_none(self):
        """Test that set_refresh_scheduler accepts None to clear the scheduler."""
        from code_indexer.global_repos.meta_description_hook import (
            set_refresh_scheduler,
        )
        import code_indexer.global_repos.meta_description_hook as hook_module

        set_refresh_scheduler(None)

        assert hook_module._refresh_scheduler is None

    def test_set_refresh_scheduler_replaces_previous_scheduler(
        self, mock_refresh_scheduler
    ):
        """Test that set_refresh_scheduler replaces any previously set scheduler."""
        from code_indexer.global_repos.meta_description_hook import (
            set_refresh_scheduler,
        )
        import code_indexer.global_repos.meta_description_hook as hook_module

        first_scheduler = MagicMock()
        set_refresh_scheduler(first_scheduler)
        assert hook_module._refresh_scheduler is first_scheduler

        set_refresh_scheduler(mock_refresh_scheduler)
        assert hook_module._refresh_scheduler is mock_refresh_scheduler

    def test_set_refresh_scheduler_does_not_affect_other_module_state(
        self, mock_refresh_scheduler
    ):
        """Test that set_refresh_scheduler only affects _refresh_scheduler, not _scheduler."""
        from code_indexer.global_repos.meta_description_hook import (
            set_refresh_scheduler,
            set_scheduler,
        )
        import code_indexer.global_repos.meta_description_hook as hook_module

        description_scheduler = MagicMock()
        set_scheduler(description_scheduler)

        set_refresh_scheduler(mock_refresh_scheduler)

        # _scheduler should not be affected
        assert hook_module._scheduler is description_scheduler
        # _refresh_scheduler should be set
        assert hook_module._refresh_scheduler is mock_refresh_scheduler


class TestOnRepoAddedTriggersRefresh:
    """
    Tests that on_repo_added() triggers cidx-meta reindex via RefreshScheduler.

    AC3: cidx-meta meta descriptions indexed after golden repo add
    """

    def test_on_repo_added_triggers_refresh_when_scheduler_set(
        self, temp_golden_repos_dir, mock_refresh_scheduler
    ):
        """
        Test that on_repo_added() calls trigger_refresh_for_repo('cidx-meta-global')
        when _refresh_scheduler is set.

        AC3: trigger_refresh_for_repo called after .md file created.
        """
        from code_indexer.global_repos.meta_description_hook import (
            on_repo_added,
            set_refresh_scheduler,
        )

        set_refresh_scheduler(mock_refresh_scheduler)

        repo_name = "my-golden-repo"
        clone_path = Path(temp_golden_repos_dir) / repo_name
        clone_path.mkdir(parents=True)
        (clone_path / "README.md").write_text("# My Golden Repo\nDescription")

        mock_cli_manager = MagicMock()
        mock_cli_manager.check_cli_available.return_value = False  # Use README fallback

        with patch(
            "code_indexer.global_repos.meta_description_hook.get_claude_cli_manager",
            return_value=mock_cli_manager,
        ):
            on_repo_added(
                repo_name=repo_name,
                repo_url="https://github.com/test/my-golden-repo",
                clone_path=str(clone_path),
                golden_repos_dir=temp_golden_repos_dir,
            )

        mock_refresh_scheduler.trigger_refresh_for_repo.assert_called_once_with(
            "cidx-meta-global"
        )

    def test_on_repo_added_does_not_trigger_refresh_when_scheduler_not_set(
        self, temp_golden_repos_dir
    ):
        """
        Test that on_repo_added() does NOT call trigger_refresh_for_repo when
        _refresh_scheduler is None (backward compatibility).
        """
        from code_indexer.global_repos.meta_description_hook import on_repo_added
        import code_indexer.global_repos.meta_description_hook as hook_module

        # Ensure no refresh scheduler set
        assert hook_module._refresh_scheduler is None

        repo_name = "my-golden-repo"
        clone_path = Path(temp_golden_repos_dir) / repo_name
        clone_path.mkdir(parents=True)
        (clone_path / "README.md").write_text("# My Golden Repo\nDescription")

        mock_cli_manager = MagicMock()
        mock_cli_manager.check_cli_available.return_value = False

        # Should not raise any exception
        with patch(
            "code_indexer.global_repos.meta_description_hook.get_claude_cli_manager",
            return_value=mock_cli_manager,
        ):
            on_repo_added(
                repo_name=repo_name,
                repo_url="https://github.com/test/my-golden-repo",
                clone_path=str(clone_path),
                golden_repos_dir=temp_golden_repos_dir,
            )

    def test_on_repo_added_triggers_refresh_with_correct_alias(
        self, temp_golden_repos_dir, mock_refresh_scheduler
    ):
        """
        Test that the refresh is triggered with 'cidx-meta-global' (the global alias).
        """
        from code_indexer.global_repos.meta_description_hook import (
            on_repo_added,
            set_refresh_scheduler,
        )

        set_refresh_scheduler(mock_refresh_scheduler)

        repo_name = "another-repo"
        clone_path = Path(temp_golden_repos_dir) / repo_name
        clone_path.mkdir(parents=True)
        (clone_path / "README.md").write_text("# Another Repo")

        mock_cli_manager = MagicMock()
        mock_cli_manager.check_cli_available.return_value = False

        with patch(
            "code_indexer.global_repos.meta_description_hook.get_claude_cli_manager",
            return_value=mock_cli_manager,
        ):
            on_repo_added(
                repo_name=repo_name,
                repo_url="https://github.com/test/another-repo",
                clone_path=str(clone_path),
                golden_repos_dir=temp_golden_repos_dir,
            )

        # Must use "cidx-meta-global" not "cidx-meta" or repo_name
        call_args = mock_refresh_scheduler.trigger_refresh_for_repo.call_args
        assert call_args[0][0] == "cidx-meta-global", (
            f"Expected trigger with 'cidx-meta-global', got {call_args[0][0]!r}"
        )

    def test_on_repo_added_trigger_refresh_failure_does_not_crash(
        self, temp_golden_repos_dir
    ):
        """
        Test that if trigger_refresh_for_repo raises, on_repo_added handles it
        gracefully (logs warning, does not propagate).

        AC6: Refresh trigger is non-blocking.
        """
        from code_indexer.global_repos.meta_description_hook import (
            on_repo_added,
            set_refresh_scheduler,
        )

        failing_scheduler = MagicMock()
        failing_scheduler.trigger_refresh_for_repo.side_effect = Exception(
            "Scheduler not ready"
        )
        set_refresh_scheduler(failing_scheduler)

        repo_name = "my-repo"
        clone_path = Path(temp_golden_repos_dir) / repo_name
        clone_path.mkdir(parents=True)
        (clone_path / "README.md").write_text("# My Repo")

        mock_cli_manager = MagicMock()
        mock_cli_manager.check_cli_available.return_value = False

        with patch(
            "code_indexer.global_repos.meta_description_hook.get_claude_cli_manager",
            return_value=mock_cli_manager,
        ):
            # Should NOT raise exception despite scheduler failure
            on_repo_added(
                repo_name=repo_name,
                repo_url="https://github.com/test/my-repo",
                clone_path=str(clone_path),
                golden_repos_dir=temp_golden_repos_dir,
            )

    def test_on_repo_added_skips_refresh_for_cidx_meta_itself(
        self, temp_golden_repos_dir, mock_refresh_scheduler
    ):
        """
        Test that on_repo_added() for 'cidx-meta' itself does NOT trigger a refresh.

        cidx-meta is skipped early in on_repo_added, so no refresh needed.
        """
        from code_indexer.global_repos.meta_description_hook import (
            on_repo_added,
            set_refresh_scheduler,
        )

        set_refresh_scheduler(mock_refresh_scheduler)

        # Call with cidx-meta itself
        on_repo_added(
            repo_name="cidx-meta",
            repo_url="local://cidx-meta",
            clone_path=str(Path(temp_golden_repos_dir) / "cidx-meta"),
            golden_repos_dir=temp_golden_repos_dir,
        )

        # No refresh should be triggered for cidx-meta
        mock_refresh_scheduler.trigger_refresh_for_repo.assert_not_called()


class TestOnRepoRemovedTriggersRefresh:
    """
    Tests that on_repo_removed() triggers cidx-meta reindex via RefreshScheduler.

    AC4: cidx-meta meta descriptions removed after delete
    """

    def test_on_repo_removed_triggers_refresh_when_file_deleted(
        self, temp_golden_repos_dir, mock_refresh_scheduler
    ):
        """
        Test that on_repo_removed() calls trigger_refresh_for_repo('cidx-meta-global')
        when .md file exists and is deleted.

        AC4: trigger_refresh_for_repo called after .md file deletion.
        """
        from code_indexer.global_repos.meta_description_hook import (
            on_repo_removed,
            set_refresh_scheduler,
        )

        set_refresh_scheduler(mock_refresh_scheduler)

        # Create .md file to be deleted
        repo_name = "repo-to-remove"
        cidx_meta_path = Path(temp_golden_repos_dir) / "cidx-meta"
        md_file = cidx_meta_path / f"{repo_name}.md"
        md_file.write_text("# Repo to Remove\nDescription")
        assert md_file.exists()

        on_repo_removed(
            repo_name=repo_name,
            golden_repos_dir=temp_golden_repos_dir,
        )

        # .md file should be gone
        assert not md_file.exists()

        # Refresh should be triggered
        mock_refresh_scheduler.trigger_refresh_for_repo.assert_called_once_with(
            "cidx-meta-global"
        )

    def test_on_repo_removed_does_not_trigger_refresh_when_file_not_found(
        self, temp_golden_repos_dir, mock_refresh_scheduler
    ):
        """
        Test that on_repo_removed() does NOT call trigger_refresh_for_repo
        when no .md file exists (nothing to delete, no index change needed).

        AC4: Only trigger when file was actually deleted.
        """
        from code_indexer.global_repos.meta_description_hook import (
            on_repo_removed,
            set_refresh_scheduler,
        )

        set_refresh_scheduler(mock_refresh_scheduler)

        repo_name = "nonexistent-repo"
        md_file = Path(temp_golden_repos_dir) / "cidx-meta" / f"{repo_name}.md"
        assert not md_file.exists()

        on_repo_removed(
            repo_name=repo_name,
            golden_repos_dir=temp_golden_repos_dir,
        )

        # No refresh should be triggered when no file was deleted
        mock_refresh_scheduler.trigger_refresh_for_repo.assert_not_called()

    def test_on_repo_removed_does_not_trigger_refresh_when_scheduler_not_set(
        self, temp_golden_repos_dir
    ):
        """
        Test that on_repo_removed() works correctly when _refresh_scheduler is None
        (backward compatibility, no crash).
        """
        from code_indexer.global_repos.meta_description_hook import on_repo_removed
        import code_indexer.global_repos.meta_description_hook as hook_module

        assert hook_module._refresh_scheduler is None

        repo_name = "repo-to-remove"
        cidx_meta_path = Path(temp_golden_repos_dir) / "cidx-meta"
        md_file = cidx_meta_path / f"{repo_name}.md"
        md_file.write_text("# Repo\nContent")

        # Should not raise even with no scheduler
        on_repo_removed(
            repo_name=repo_name,
            golden_repos_dir=temp_golden_repos_dir,
        )

        # File should still be deleted
        assert not md_file.exists()

    def test_on_repo_removed_trigger_refresh_failure_does_not_crash(
        self, temp_golden_repos_dir
    ):
        """
        Test that if trigger_refresh_for_repo raises, on_repo_removed handles it
        gracefully (logs warning, does not propagate).

        AC6: Refresh trigger is non-blocking.
        """
        from code_indexer.global_repos.meta_description_hook import (
            on_repo_removed,
            set_refresh_scheduler,
        )

        failing_scheduler = MagicMock()
        failing_scheduler.trigger_refresh_for_repo.side_effect = Exception(
            "Scheduler crashed"
        )
        set_refresh_scheduler(failing_scheduler)

        repo_name = "repo-to-remove"
        cidx_meta_path = Path(temp_golden_repos_dir) / "cidx-meta"
        md_file = cidx_meta_path / f"{repo_name}.md"
        md_file.write_text("# Repo\nContent")

        # Should NOT raise exception
        on_repo_removed(
            repo_name=repo_name,
            golden_repos_dir=temp_golden_repos_dir,
        )

    def test_on_repo_removed_triggers_refresh_with_correct_alias(
        self, temp_golden_repos_dir, mock_refresh_scheduler
    ):
        """
        Test that the refresh for removal is triggered with 'cidx-meta-global'.
        """
        from code_indexer.global_repos.meta_description_hook import (
            on_repo_removed,
            set_refresh_scheduler,
        )

        set_refresh_scheduler(mock_refresh_scheduler)

        repo_name = "some-repo"
        cidx_meta_path = Path(temp_golden_repos_dir) / "cidx-meta"
        md_file = cidx_meta_path / f"{repo_name}.md"
        md_file.write_text("# Some Repo")

        on_repo_removed(
            repo_name=repo_name,
            golden_repos_dir=temp_golden_repos_dir,
        )

        call_args = mock_refresh_scheduler.trigger_refresh_for_repo.call_args
        assert call_args[0][0] == "cidx-meta-global", (
            f"Expected trigger with 'cidx-meta-global', got {call_args[0][0]!r}"
        )
