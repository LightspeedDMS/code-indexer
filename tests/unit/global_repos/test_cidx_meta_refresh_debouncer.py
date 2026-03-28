"""
Unit tests for Story #345: Debounced cidx-meta Refresh on Batch Repository Registration.

Tests for CidxMetaRefreshDebouncer class and the updated on_repo_added/on_repo_removed
behavior that uses the debouncer when DuplicateJobError is raised.

Acceptance criteria tested:
- AC1: Single repo registration triggers immediate refresh (no debouncer needed)
- AC2: Batch registration coalesces into one deferred refresh via debouncer
- AC3: Debounce timer resets on each new registration signal
- AC4: Deferred refresh retries if still blocked by DuplicateJobError
- AC5: Server shutdown cancels debounce timer cleanly
"""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_refresh_scheduler():
    """Create a mock RefreshScheduler."""
    scheduler = MagicMock()
    scheduler.trigger_refresh_for_repo.return_value = "job-id-123"
    return scheduler


@pytest.fixture
def duplicate_job_error():
    """Create a DuplicateJobError instance for testing."""
    from code_indexer.server.repositories.background_jobs import DuplicateJobError

    return DuplicateJobError(
        operation_type="refresh",
        repo_alias="cidx-meta-global",
        existing_job_id="existing-job-456",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests for CidxMetaRefreshDebouncer class
# ─────────────────────────────────────────────────────────────────────────────


class TestCidxMetaRefreshDebouncerInit:
    """Tests for debouncer initialization and defaults."""

    def test_debouncer_importable(self):
        """Test that CidxMetaRefreshDebouncer can be imported."""
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        assert CidxMetaRefreshDebouncer is not None

    def test_debouncer_creates_with_scheduler(self, mock_refresh_scheduler):
        """Test debouncer can be created with a refresh scheduler."""
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        debouncer = CidxMetaRefreshDebouncer(
            refresh_scheduler=mock_refresh_scheduler,
            debounce_seconds=30,
        )

        assert debouncer is not None

    def test_debouncer_default_debounce_seconds(self, mock_refresh_scheduler):
        """Test that debouncer default debounce interval is 30 seconds."""
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        debouncer = CidxMetaRefreshDebouncer(refresh_scheduler=mock_refresh_scheduler)

        assert debouncer._debounce_seconds == 30

    def test_debouncer_custom_debounce_seconds(self, mock_refresh_scheduler):
        """Test that debouncer accepts custom debounce interval."""
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        debouncer = CidxMetaRefreshDebouncer(
            refresh_scheduler=mock_refresh_scheduler,
            debounce_seconds=5,
        )

        assert debouncer._debounce_seconds == 5

    def test_debouncer_initial_state_is_clean(self, mock_refresh_scheduler):
        """Test that debouncer starts with clean state: not dirty, no timer, not shutdown."""
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        debouncer = CidxMetaRefreshDebouncer(refresh_scheduler=mock_refresh_scheduler)

        assert debouncer._dirty is False
        assert debouncer._timer is None
        assert debouncer._shutdown is False


class TestCidxMetaRefreshDebouncerSignalDirty:
    """Tests for signal_dirty() method behavior."""

    def test_signal_dirty_marks_dirty(self, mock_refresh_scheduler):
        """Test that signal_dirty() sets _dirty to True."""
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        debouncer = CidxMetaRefreshDebouncer(
            refresh_scheduler=mock_refresh_scheduler,
            debounce_seconds=60,  # Long interval so timer doesn't fire
        )

        debouncer.signal_dirty()

        assert debouncer._dirty is True

        # Cleanup
        debouncer.shutdown()

    def test_signal_dirty_starts_timer(self, mock_refresh_scheduler):
        """Test that signal_dirty() starts a timer."""
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        debouncer = CidxMetaRefreshDebouncer(
            refresh_scheduler=mock_refresh_scheduler,
            debounce_seconds=60,  # Long interval so timer doesn't fire
        )

        debouncer.signal_dirty()

        assert debouncer._timer is not None
        assert debouncer._timer.is_alive()

        # Cleanup
        debouncer.shutdown()

    def test_signal_dirty_twice_cancels_first_timer(self, mock_refresh_scheduler):
        """Test that calling signal_dirty() twice cancels the first timer and starts a new one."""
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        debouncer = CidxMetaRefreshDebouncer(
            refresh_scheduler=mock_refresh_scheduler,
            debounce_seconds=60,
        )

        debouncer.signal_dirty()
        first_timer = debouncer._timer

        debouncer.signal_dirty()
        second_timer = debouncer._timer

        # Timer should have been replaced
        assert first_timer is not second_timer
        # First timer should be cancelled - join briefly to let the thread finish
        # threading.Timer.cancel() stops the callback but the thread may still
        # be alive momentarily; join(timeout) eliminates the race condition.
        first_timer.join(timeout=0.5)
        assert not first_timer.is_alive()
        # Second timer should still be alive
        assert second_timer.is_alive()

        # Cleanup
        debouncer.shutdown()

    def test_signal_dirty_does_not_start_timer_after_shutdown(
        self, mock_refresh_scheduler
    ):
        """Test that signal_dirty() does NOT start a timer after shutdown()."""
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        debouncer = CidxMetaRefreshDebouncer(
            refresh_scheduler=mock_refresh_scheduler,
            debounce_seconds=60,
        )

        debouncer.shutdown()
        debouncer.signal_dirty()

        # After shutdown, timer should NOT be started
        assert debouncer._timer is None

    def test_signal_dirty_fires_refresh_after_interval(self, mock_refresh_scheduler):
        """
        Test that after signal_dirty(), a refresh is triggered when the timer expires.

        AC1: deferred refresh fires after debounce interval expires.
        Uses a very short debounce interval (0.05s) for test speed.
        """
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        debouncer = CidxMetaRefreshDebouncer(
            refresh_scheduler=mock_refresh_scheduler,
            debounce_seconds=0.05,  # 50ms for fast testing
        )

        debouncer.signal_dirty()

        # Wait for timer to fire (0.05s + buffer)
        time.sleep(0.2)

        mock_refresh_scheduler.trigger_refresh_for_repo.assert_called_once_with(
            "cidx-meta-global"
        )

        # State should be clean after successful refresh
        assert debouncer._dirty is False
        assert debouncer._timer is None


class TestCidxMetaRefreshDebouncerTimerCoalescing:
    """Tests for timer coalescing behavior (AC2, AC3)."""

    def test_multiple_signals_coalesce_into_single_refresh(
        self, mock_refresh_scheduler
    ):
        """
        Test that multiple signal_dirty() calls within debounce interval result in
        exactly ONE refresh call.

        AC2: Batch registration coalesces into one deferred refresh.
        """
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        debouncer = CidxMetaRefreshDebouncer(
            refresh_scheduler=mock_refresh_scheduler,
            debounce_seconds=0.1,  # 100ms debounce
        )

        # Signal 5 times in rapid succession
        for _ in range(5):
            debouncer.signal_dirty()
            time.sleep(0.01)  # 10ms between signals (within debounce window)

        # Wait for debounce to complete
        time.sleep(0.3)  # Wait longer than debounce interval

        # Exactly ONE refresh should have been triggered
        assert mock_refresh_scheduler.trigger_refresh_for_repo.call_count == 1
        mock_refresh_scheduler.trigger_refresh_for_repo.assert_called_once_with(
            "cidx-meta-global"
        )

    def test_timer_resets_on_each_signal(self, mock_refresh_scheduler):
        """
        Test that each signal_dirty() call resets the debounce timer.

        AC3: Debounce timer resets on each new registration.
        """
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        debouncer = CidxMetaRefreshDebouncer(
            refresh_scheduler=mock_refresh_scheduler,
            debounce_seconds=0.15,  # 150ms debounce
        )

        # Signal at t=0
        debouncer.signal_dirty()
        first_timer = debouncer._timer

        # Signal at t=100ms (before timer fires at t=150ms)
        time.sleep(0.10)
        debouncer.signal_dirty()
        second_timer = debouncer._timer

        # First timer should be replaced; wait for cancelled thread to fully terminate
        assert first_timer is not second_timer
        first_timer.join(timeout=1.0)
        assert not first_timer.is_alive()
        assert second_timer.is_alive()

        # Wait for second timer to fire (started at t=100ms, fires at t=250ms)
        time.sleep(0.25)

        # Only ONE refresh should have happened (after second timer)
        assert mock_refresh_scheduler.trigger_refresh_for_repo.call_count == 1

        # Cleanup
        debouncer.shutdown()


class TestCidxMetaRefreshDebouncerRetryOnDuplicateJobError:
    """Tests for retry behavior when DuplicateJobError is raised (AC4)."""

    def test_duplicate_job_error_causes_re_dirty_and_retry(
        self, mock_refresh_scheduler, duplicate_job_error
    ):
        """
        Test that when trigger_refresh_for_repo raises DuplicateJobError,
        the debouncer re-marks itself dirty and schedules another retry.

        AC4: Deferred refresh retries if still blocked.
        """
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        # First call raises DuplicateJobError, second succeeds
        mock_refresh_scheduler.trigger_refresh_for_repo.side_effect = [
            duplicate_job_error,
            "job-id-retry",
        ]

        debouncer = CidxMetaRefreshDebouncer(
            refresh_scheduler=mock_refresh_scheduler,
            debounce_seconds=0.05,
        )

        debouncer.signal_dirty()

        # Wait for first timer to fire and retry timer to start
        time.sleep(0.15)

        # Should have been called at least once (the DuplicateJobError attempt)
        assert mock_refresh_scheduler.trigger_refresh_for_repo.call_count >= 1

        # Wait for retry timer to fire
        time.sleep(0.15)

        # Should have been called twice total
        assert mock_refresh_scheduler.trigger_refresh_for_repo.call_count == 2

    def test_duplicate_job_error_re_marks_dirty(
        self, mock_refresh_scheduler, duplicate_job_error
    ):
        """
        Test that after DuplicateJobError, debouncer re-marks _dirty=True.
        """
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        def raise_once(*args, **kwargs):
            raise duplicate_job_error

        mock_refresh_scheduler.trigger_refresh_for_repo.side_effect = raise_once

        debouncer = CidxMetaRefreshDebouncer(
            refresh_scheduler=mock_refresh_scheduler,
            debounce_seconds=0.05,
        )

        debouncer.signal_dirty()

        # Wait for timer to fire and raise DuplicateJobError
        time.sleep(0.15)

        # After DuplicateJobError, dirty should be True again
        assert debouncer._dirty is True

        # Cleanup
        debouncer.shutdown()

    def test_generic_exception_does_not_retry(self, mock_refresh_scheduler):
        """
        Test that a generic Exception from trigger_refresh_for_repo does NOT
        cause a retry (only DuplicateJobError retries).
        """
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        mock_refresh_scheduler.trigger_refresh_for_repo.side_effect = Exception(
            "Network error"
        )

        debouncer = CidxMetaRefreshDebouncer(
            refresh_scheduler=mock_refresh_scheduler,
            debounce_seconds=0.05,
        )

        debouncer.signal_dirty()

        # Wait for timer to fire
        time.sleep(0.15)

        # Should have been called once (no retry for generic exception)
        assert mock_refresh_scheduler.trigger_refresh_for_repo.call_count == 1

        # Dirty should remain False (it was cleared before the attempt)
        # and no retry timer should be running
        assert debouncer._timer is None


class TestCidxMetaRefreshDebouncerShutdown:
    """Tests for shutdown behavior (AC5)."""

    def test_shutdown_cancels_pending_timer(self, mock_refresh_scheduler):
        """
        Test that shutdown() cancels any pending timer.

        AC5: Server shutdown cancels debounce timer cleanly.
        """
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        debouncer = CidxMetaRefreshDebouncer(
            refresh_scheduler=mock_refresh_scheduler,
            debounce_seconds=60,  # Long interval
        )

        debouncer.signal_dirty()
        assert debouncer._timer is not None
        assert debouncer._timer.is_alive()

        debouncer.shutdown()

        # Timer should be None and no longer alive
        assert debouncer._timer is None
        assert debouncer._shutdown is True

    def test_shutdown_prevents_refresh_after_shutdown(self, mock_refresh_scheduler):
        """
        Test that no refresh is attempted after shutdown() is called.

        AC5: No refresh is attempted after shutdown begins.
        """
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        debouncer = CidxMetaRefreshDebouncer(
            refresh_scheduler=mock_refresh_scheduler,
            debounce_seconds=0.05,  # Short interval
        )

        debouncer.signal_dirty()
        debouncer.shutdown()  # Shutdown immediately

        # Wait longer than debounce interval
        time.sleep(0.2)

        # No refresh should have been triggered
        mock_refresh_scheduler.trigger_refresh_for_repo.assert_not_called()

    def test_shutdown_is_idempotent(self, mock_refresh_scheduler):
        """Test that calling shutdown() multiple times does not crash."""
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
        )

        debouncer = CidxMetaRefreshDebouncer(
            refresh_scheduler=mock_refresh_scheduler,
            debounce_seconds=60,
        )

        # Should not raise
        debouncer.shutdown()
        debouncer.shutdown()
        debouncer.shutdown()


# ─────────────────────────────────────────────────────────────────────────────
# Tests for on_repo_added() integration with debouncer (AC1, AC2)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level state before and after each test."""
    import code_indexer.global_repos.meta_description_hook as hook_module

    # Save original state
    original_refresh_scheduler = getattr(hook_module, "_refresh_scheduler", None)
    original_debouncer = getattr(hook_module, "_debouncer", None)
    original_tracking_backend = getattr(hook_module, "_tracking_backend", None)
    original_scheduler = getattr(hook_module, "_scheduler", None)

    # Clear state before test
    hook_module._refresh_scheduler = None
    hook_module._debouncer = None
    hook_module._tracking_backend = None
    hook_module._scheduler = None

    yield

    # Restore original state after test
    hook_module._refresh_scheduler = original_refresh_scheduler
    hook_module._debouncer = original_debouncer
    hook_module._tracking_backend = original_tracking_backend
    hook_module._scheduler = original_scheduler


@pytest.fixture
def temp_golden_repos_dir(tmp_path):
    """Create temp golden repos dir with cidx-meta subdir."""
    cidx_meta = tmp_path / "cidx-meta"
    cidx_meta.mkdir(parents=True)
    return str(tmp_path)


@pytest.fixture
def mock_cli_manager():
    """Create a mock Claude CLI manager that is unavailable (uses README fallback)."""
    manager = MagicMock()
    manager.check_cli_available.return_value = False
    return manager


class TestOnRepoAddedWithDebouncer:
    """
    Tests for on_repo_added() using debouncer for DuplicateJobError handling.

    AC1: Single repo registration triggers immediate refresh (no debouncer involved).
    AC2: Batch registration uses debouncer when DuplicateJobError is raised.
    """

    def test_on_repo_added_succeeds_immediately_no_debouncer_needed(
        self, temp_golden_repos_dir, mock_refresh_scheduler, mock_cli_manager
    ):
        """
        AC1: When trigger_refresh_for_repo succeeds immediately, debouncer is NOT signaled.
        """
        from code_indexer.global_repos.meta_description_hook import (
            on_repo_added,
            set_refresh_scheduler,
        )
        import code_indexer.global_repos.meta_description_hook as hook_module

        set_refresh_scheduler(mock_refresh_scheduler)

        # Create a mock debouncer to verify it's NOT signaled
        mock_debouncer = MagicMock()
        hook_module._debouncer = mock_debouncer

        repo_name = "test-repo"
        clone_path = Path(temp_golden_repos_dir) / repo_name
        clone_path.mkdir(parents=True)
        (clone_path / "README.md").write_text("# Test Repo")

        with patch(
            "code_indexer.global_repos.meta_description_hook.get_claude_cli_manager",
            return_value=mock_cli_manager,
        ):
            on_repo_added(
                repo_name=repo_name,
                repo_url="https://github.com/test/repo",
                clone_path=str(clone_path),
                golden_repos_dir=temp_golden_repos_dir,
            )

        # Direct refresh succeeded - debouncer should NOT be signaled
        mock_debouncer.signal_dirty.assert_not_called()
        # Direct refresh should have been called
        mock_refresh_scheduler.trigger_refresh_for_repo.assert_called_once_with(
            "cidx-meta-global"
        )

    def test_on_repo_added_signals_debouncer_on_duplicate_job_error(
        self,
        temp_golden_repos_dir,
        mock_refresh_scheduler,
        mock_cli_manager,
        duplicate_job_error,
    ):
        """
        AC2: When trigger_refresh_for_repo raises DuplicateJobError,
        the debouncer is signaled.
        """
        from code_indexer.global_repos.meta_description_hook import (
            on_repo_added,
            set_refresh_scheduler,
        )
        import code_indexer.global_repos.meta_description_hook as hook_module

        mock_refresh_scheduler.trigger_refresh_for_repo.side_effect = (
            duplicate_job_error
        )
        set_refresh_scheduler(mock_refresh_scheduler)

        mock_debouncer = MagicMock()
        hook_module._debouncer = mock_debouncer

        repo_name = "test-repo"
        clone_path = Path(temp_golden_repos_dir) / repo_name
        clone_path.mkdir(parents=True)
        (clone_path / "README.md").write_text("# Test Repo")

        with patch(
            "code_indexer.global_repos.meta_description_hook.get_claude_cli_manager",
            return_value=mock_cli_manager,
        ):
            on_repo_added(
                repo_name=repo_name,
                repo_url="https://github.com/test/repo",
                clone_path=str(clone_path),
                golden_repos_dir=temp_golden_repos_dir,
            )

        # Debouncer should have been signaled
        mock_debouncer.signal_dirty.assert_called_once()

    def test_on_repo_added_no_debouncer_logs_warning_on_duplicate_job_error(
        self,
        temp_golden_repos_dir,
        mock_refresh_scheduler,
        mock_cli_manager,
        duplicate_job_error,
        caplog,
    ):
        """
        Test that when DuplicateJobError occurs but no debouncer is set,
        a warning is logged.
        """
        import logging

        from code_indexer.global_repos.meta_description_hook import (
            on_repo_added,
            set_refresh_scheduler,
        )
        import code_indexer.global_repos.meta_description_hook as hook_module

        mock_refresh_scheduler.trigger_refresh_for_repo.side_effect = (
            duplicate_job_error
        )
        set_refresh_scheduler(mock_refresh_scheduler)

        # No debouncer set
        assert hook_module._debouncer is None

        repo_name = "test-repo"
        clone_path = Path(temp_golden_repos_dir) / repo_name
        clone_path.mkdir(parents=True)
        (clone_path / "README.md").write_text("# Test Repo")

        with caplog.at_level(logging.WARNING):
            with patch(
                "code_indexer.global_repos.meta_description_hook.get_claude_cli_manager",
                return_value=mock_cli_manager,
            ):
                on_repo_added(
                    repo_name=repo_name,
                    repo_url="https://github.com/test/repo",
                    clone_path=str(clone_path),
                    golden_repos_dir=temp_golden_repos_dir,
                )

        # Should log a warning about no debouncer
        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "debouncer" in msg.lower() or "skipped" in msg.lower()
            for msg in warning_messages
        ), f"Expected warning about missing debouncer, got: {warning_messages}"

    def test_on_repo_added_batch_scenario_md_files_all_created(
        self,
        temp_golden_repos_dir,
        mock_refresh_scheduler,
        mock_cli_manager,
        duplicate_job_error,
    ):
        """
        AC2: When 5 repos are registered in rapid succession, all .md files are created
        on disk regardless of DuplicateJobError.
        """
        from code_indexer.global_repos.meta_description_hook import (
            on_repo_added,
            set_refresh_scheduler,
        )
        import code_indexer.global_repos.meta_description_hook as hook_module

        # First call succeeds, rest raise DuplicateJobError
        mock_refresh_scheduler.trigger_refresh_for_repo.side_effect = [
            "job-id-1",
            duplicate_job_error,
            duplicate_job_error,
            duplicate_job_error,
            duplicate_job_error,
        ]
        set_refresh_scheduler(mock_refresh_scheduler)

        mock_debouncer = MagicMock()
        hook_module._debouncer = mock_debouncer

        # Register 5 repos
        repo_names = [f"batch-repo-{i}" for i in range(5)]
        for repo_name in repo_names:
            clone_path = Path(temp_golden_repos_dir) / repo_name
            clone_path.mkdir(parents=True)
            (clone_path / "README.md").write_text(f"# {repo_name}")

            with patch(
                "code_indexer.global_repos.meta_description_hook.get_claude_cli_manager",
                return_value=mock_cli_manager,
            ):
                on_repo_added(
                    repo_name=repo_name,
                    repo_url=f"https://github.com/test/{repo_name}",
                    clone_path=str(clone_path),
                    golden_repos_dir=temp_golden_repos_dir,
                )

        # All 5 .md files should exist
        cidx_meta_path = Path(temp_golden_repos_dir) / "cidx-meta"
        for repo_name in repo_names:
            md_file = cidx_meta_path / f"{repo_name}_README.md"
            assert md_file.exists(), f"Expected .md file for {repo_name} at {md_file}"

        # Debouncer should have been signaled 4 times (for the 4 DuplicateJobErrors)
        assert mock_debouncer.signal_dirty.call_count == 4


class TestOnRepoRemovedWithDebouncer:
    """
    Tests for on_repo_removed() using debouncer for DuplicateJobError handling.
    """

    def test_on_repo_removed_signals_debouncer_on_duplicate_job_error(
        self,
        temp_golden_repos_dir,
        mock_refresh_scheduler,
        duplicate_job_error,
    ):
        """
        Test that on_repo_removed() signals the debouncer when DuplicateJobError raised.
        """
        from code_indexer.global_repos.meta_description_hook import (
            on_repo_removed,
            set_refresh_scheduler,
        )
        import code_indexer.global_repos.meta_description_hook as hook_module

        mock_refresh_scheduler.trigger_refresh_for_repo.side_effect = (
            duplicate_job_error
        )
        set_refresh_scheduler(mock_refresh_scheduler)

        mock_debouncer = MagicMock()
        hook_module._debouncer = mock_debouncer

        repo_name = "repo-to-remove"
        cidx_meta_path = Path(temp_golden_repos_dir) / "cidx-meta"
        md_file = cidx_meta_path / f"{repo_name}.md"
        md_file.write_text("# Repo to Remove")

        on_repo_removed(
            repo_name=repo_name,
            golden_repos_dir=temp_golden_repos_dir,
        )

        # File should be deleted
        assert not md_file.exists()
        # Debouncer should be signaled
        mock_debouncer.signal_dirty.assert_called_once()

    def test_on_repo_removed_no_debouncer_logs_warning_on_duplicate_job_error(
        self,
        temp_golden_repos_dir,
        mock_refresh_scheduler,
        duplicate_job_error,
        caplog,
    ):
        """
        Test that when DuplicateJobError occurs on remove but no debouncer is set,
        a warning is logged.
        """
        import logging

        from code_indexer.global_repos.meta_description_hook import (
            on_repo_removed,
            set_refresh_scheduler,
        )
        import code_indexer.global_repos.meta_description_hook as hook_module

        mock_refresh_scheduler.trigger_refresh_for_repo.side_effect = (
            duplicate_job_error
        )
        set_refresh_scheduler(mock_refresh_scheduler)

        # No debouncer set
        assert hook_module._debouncer is None

        repo_name = "repo-to-remove"
        cidx_meta_path = Path(temp_golden_repos_dir) / "cidx-meta"
        md_file = cidx_meta_path / f"{repo_name}.md"
        md_file.write_text("# Repo to Remove")

        with caplog.at_level(logging.WARNING):
            on_repo_removed(
                repo_name=repo_name,
                golden_repos_dir=temp_golden_repos_dir,
            )

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "debouncer" in msg.lower() or "skipped" in msg.lower()
            for msg in warning_messages
        ), f"Expected warning about missing debouncer, got: {warning_messages}"


# ─────────────────────────────────────────────────────────────────────────────
# Tests for set_debouncer() module-level setter
# ─────────────────────────────────────────────────────────────────────────────


class TestSetDebouncer:
    """Tests for the new set_debouncer() module-level setter."""

    def test_set_debouncer_importable(self):
        """Test that set_debouncer is importable."""
        from code_indexer.global_repos.meta_description_hook import set_debouncer

        assert set_debouncer is not None

    def test_set_debouncer_stores_debouncer(self, mock_refresh_scheduler):
        """Test that set_debouncer() stores the debouncer in module state."""
        from code_indexer.global_repos.meta_description_hook import (
            CidxMetaRefreshDebouncer,
            set_debouncer,
        )
        import code_indexer.global_repos.meta_description_hook as hook_module

        debouncer = CidxMetaRefreshDebouncer(
            refresh_scheduler=mock_refresh_scheduler,
            debounce_seconds=60,
        )

        set_debouncer(debouncer)

        assert hook_module._debouncer is debouncer

        # Cleanup
        debouncer.shutdown()

    def test_set_debouncer_accepts_none(self):
        """Test that set_debouncer() accepts None to clear the debouncer."""
        from code_indexer.global_repos.meta_description_hook import set_debouncer
        import code_indexer.global_repos.meta_description_hook as hook_module

        set_debouncer(None)

        assert hook_module._debouncer is None
