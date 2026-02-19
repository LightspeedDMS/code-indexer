"""
Unit tests for write-lock registry API on RefreshScheduler (Story #227).

Tests the acquire/release/is_locked/context manager/multi-repo isolation API.

RED phase: Tests written BEFORE production code. All tests expected to FAIL
until acquire_write_lock, release_write_lock, is_write_locked, and write_lock
are implemented on RefreshScheduler.
"""

import threading

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.config import ConfigManager


@pytest.fixture
def golden_repos_dir(tmp_path):
    d = tmp_path / ".code-indexer" / "golden_repos"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def scheduler(golden_repos_dir, tmp_path):
    config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
    query_tracker = QueryTracker()
    cleanup_manager = CleanupManager(query_tracker)
    registry = GlobalRegistry(str(golden_repos_dir))
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        registry=registry,
    )


class TestWriteLockRegistry:
    """Tests for the write-lock registry API on RefreshScheduler."""

    def test_acquire_write_lock_returns_true_first_time(self, scheduler):
        """acquire_write_lock() returns True when called the first time for an alias."""
        result = scheduler.acquire_write_lock("cidx-meta")

        assert result is True, (
            "acquire_write_lock() must return True on first acquisition. "
            "The lock was not held by anyone."
        )

        scheduler.release_write_lock("cidx-meta")

    def test_acquire_write_lock_returns_false_when_held(self, scheduler):
        """acquire_write_lock() returns False when lock is already held."""
        first = scheduler.acquire_write_lock("cidx-meta")
        assert first is True, "Precondition: first acquire must succeed"

        second = scheduler.acquire_write_lock("cidx-meta")

        assert second is False, (
            "acquire_write_lock() must return False when lock is already held. "
            "Non-reentrant: second acquire on held lock must fail."
        )

        scheduler.release_write_lock("cidx-meta")

    def test_release_write_lock_allows_reacquisition(self, scheduler):
        """After release_write_lock(), acquire_write_lock() returns True again."""
        scheduler.acquire_write_lock("cidx-meta")
        scheduler.release_write_lock("cidx-meta")

        result = scheduler.acquire_write_lock("cidx-meta")

        assert result is True, (
            "acquire_write_lock() must return True after release_write_lock(). "
            "The lock must be fully released."
        )

        scheduler.release_write_lock("cidx-meta")

    def test_is_write_locked_returns_false_when_not_held(self, scheduler):
        """is_write_locked() returns False when no one holds the lock."""
        result = scheduler.is_write_locked("cidx-meta")

        assert result is False, "is_write_locked() must return False when lock is not held."

    def test_is_write_locked_returns_true_when_held(self, scheduler):
        """is_write_locked() returns True when the lock is held."""
        scheduler.acquire_write_lock("cidx-meta")

        result = scheduler.is_write_locked("cidx-meta")

        assert result is True, "is_write_locked() must return True when lock is currently held."

        scheduler.release_write_lock("cidx-meta")

    def test_is_write_locked_returns_false_after_release(self, scheduler):
        """is_write_locked() returns False after the lock is released."""
        scheduler.acquire_write_lock("cidx-meta")
        scheduler.release_write_lock("cidx-meta")

        result = scheduler.is_write_locked("cidx-meta")

        assert result is False, "is_write_locked() must return False after release_write_lock()."

    def test_write_lock_context_manager_acquires_and_releases(self, scheduler):
        """write_lock() context manager acquires on entry and releases on exit."""
        lock_held_inside = None

        with scheduler.write_lock("cidx-meta"):
            lock_held_inside = scheduler.is_write_locked("cidx-meta")

        lock_held_after = scheduler.is_write_locked("cidx-meta")

        assert lock_held_inside is True, (
            "write_lock() context manager must hold the lock inside the 'with' block."
        )
        assert lock_held_after is False, (
            "write_lock() context manager must release the lock on exit."
        )

    def test_write_lock_context_manager_releases_on_exception(self, scheduler):
        """write_lock() context manager releases the lock even when body raises exception."""
        try:
            with scheduler.write_lock("cidx-meta"):
                raise ValueError("Simulated exception inside write_lock context")
        except ValueError:
            pass  # Expected

        result = scheduler.is_write_locked("cidx-meta")

        assert result is False, (
            "write_lock() context manager must release lock even when exception occurs. "
            "AC5: Lock must be released in finally block."
        )

    def test_multiple_repo_aliases_independent_locks(self, scheduler):
        """Lock on 'cidx-meta' does NOT block lock on 'langfuse_proj_user'."""
        result_cidx = scheduler.acquire_write_lock("cidx-meta")
        result_langfuse = scheduler.acquire_write_lock("langfuse_proj_user")

        assert result_cidx is True, "Lock on 'cidx-meta' must succeed"
        assert result_langfuse is True, (
            "Lock on 'langfuse_proj_user' must succeed even while 'cidx-meta' is locked. "
            "Aliases must have independent locks."
        )

        scheduler.release_write_lock("cidx-meta")
        scheduler.release_write_lock("langfuse_proj_user")

    def test_write_lock_thread_safety(self, scheduler):
        """Only one thread can acquire the same lock simultaneously (non-blocking)."""
        results = []
        barrier = threading.Barrier(10)

        def try_acquire():
            barrier.wait()  # All threads start simultaneously
            result = scheduler.acquire_write_lock("concurrent-repo")
            results.append(result)

        threads = [threading.Thread(target=try_acquire) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(results) == 10, "All threads must complete"
        successful = [r for r in results if r is True]
        failed = [r for r in results if r is False]

        assert len(successful) == 1, (
            f"Exactly 1 thread must acquire the lock. Got {len(successful)} successes."
        )
        assert len(failed) == 9, (
            f"Exactly 9 threads must fail to acquire. Got {len(failed)} failures."
        )

        scheduler.release_write_lock("concurrent-repo")
