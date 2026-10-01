"""
Unit tests for Bug #1842 AC4: honest write-lock-holder diagnostics in
meta_description_hook.py.

Before this fix, atomic_write_description()'s LifecycleLockUnavailableError
message and on_repo_removed()'s "write lock not acquired" warning both
hardcoded "(owner='lifecycle_writer')" -- the FAILED CALLER's own attempted
identity -- regardless of who actually held the lock. This made the
production log line

    Meta description hook failed for 'markupsafe': Could not acquire write
    lock for 'cidx-meta' (owner='lifecycle_writer'); another writer ...

actively misleading: 'lifecycle_writer' names the caller, not the holder.

These tests use a REAL WriteLockManager (file-based, Messi Rule #1
anti-mock) to hold the lock under a DIFFERENT owner
('dependency_map_service'), then assert the resulting message/log names
that REAL holder.
"""

import logging
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.write_lock_manager import WriteLockManager


@pytest.fixture
def temp_golden_repos_dir():
    """Create temporary golden repos directory with cidx-meta subdir."""
    temp_dir = tempfile.mkdtemp()
    cidx_meta = Path(temp_dir) / "cidx-meta"
    cidx_meta.mkdir(parents=True)
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level state before and after each test (mirrors the
    established pattern in test_meta_description_hook_story270.py)."""
    import code_indexer.global_repos.meta_description_hook as hook_module

    original_refresh_scheduler = getattr(hook_module, "_refresh_scheduler", None)
    hook_module._refresh_scheduler = None
    yield
    hook_module._refresh_scheduler = original_refresh_scheduler


def _make_scheduler_with_real_lock_held(lock_dir, owner_name, ttl_seconds=3600):
    """Build a MagicMock refresh_scheduler whose acquire_write_lock()
    mimics a failed acquisition attempt (as the real RefreshScheduler
    would report when another owner holds the file lock), while
    scheduler.write_lock_manager is a REAL WriteLockManager reporting
    that real holder via get_lock_info() -- no mocking of the lock-info
    lookup itself (Messi Rule #1)."""
    lock_manager = WriteLockManager(lock_dir)
    acquired = lock_manager.acquire(
        "cidx-meta", owner_name=owner_name, ttl_seconds=ttl_seconds
    )
    assert acquired is True

    scheduler = MagicMock()
    scheduler.acquire_write_lock.return_value = False
    scheduler.write_lock_manager = lock_manager
    return scheduler, lock_manager


class TestAtomicWriteDescriptionNamesRealHolder:
    def test_error_message_names_real_holder_not_hardcoded_caller(self, tmp_path):
        """The exception raised when the cidx-meta lock cannot be acquired
        must name the REAL holder ('dependency_map_service'), not just the
        failed caller's own hardcoded 'lifecycle_writer' identity."""
        from code_indexer.global_repos.lifecycle_batch_runner import (
            LifecycleLockUnavailableError,
        )
        from code_indexer.global_repos.meta_description_hook import (
            atomic_write_description,
        )

        scheduler, lock_manager = _make_scheduler_with_real_lock_held(
            tmp_path, owner_name="dependency_map_service"
        )
        target = tmp_path / "markupsafe.md"

        try:
            with pytest.raises(LifecycleLockUnavailableError) as exc_info:
                atomic_write_description(target, "content", refresh_scheduler=scheduler)

            message = str(exc_info.value)
            assert "dependency_map_service" in message, message
            assert not target.exists()
        finally:
            lock_manager.release("cidx-meta", owner_name="dependency_map_service")


class TestOnRepoRemovedNamesRealHolder:
    def test_warning_names_real_holder_not_hardcoded_caller(
        self, temp_golden_repos_dir, caplog
    ):
        from code_indexer.global_repos.meta_description_hook import (
            on_repo_removed,
            set_refresh_scheduler,
        )

        repo_name = "markupsafe"
        cidx_meta_path = Path(temp_golden_repos_dir) / "cidx-meta"
        md_file = cidx_meta_path / f"{repo_name}.md"
        md_file.write_text("# markupsafe\nDescription")

        scheduler, lock_manager = _make_scheduler_with_real_lock_held(
            Path(temp_golden_repos_dir), owner_name="dependency_map_service"
        )
        set_refresh_scheduler(scheduler)
        try:
            with caplog.at_level(logging.WARNING):
                on_repo_removed(
                    repo_name=repo_name, golden_repos_dir=temp_golden_repos_dir
                )

            assert md_file.exists(), "file must not be deleted when lock unavailable"
            assert any(
                "dependency_map_service" in record.message for record in caplog.records
            ), [r.message for r in caplog.records]
        finally:
            set_refresh_scheduler(None)
            lock_manager.release("cidx-meta", owner_name="dependency_map_service")
