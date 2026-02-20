"""
Advanced tests for WriteLockManager - thread safety and RefreshScheduler delegation (Story #230).

Tests:
- Thread safety: concurrent acquire from N threads, only one succeeds
- RefreshScheduler delegates acquire/release/is_locked/write_lock to WriteLockManager
- RefreshScheduler exposes write_lock_manager attribute

TDD RED phase: Tests written BEFORE production code. All tests expected to FAIL
until WriteLockManager is implemented and RefreshScheduler is updated.
"""

import json
import threading

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lock_dir(tmp_path):
    """Provide a temporary directory for lock files."""
    d = tmp_path / "golden-repos"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def manager(lock_dir):
    """Create a fresh WriteLockManager for each test."""
    from code_indexer.global_repos.write_lock_manager import WriteLockManager

    return WriteLockManager(golden_repos_dir=lock_dir)


@pytest.fixture
def scheduler(lock_dir):
    """Create a RefreshScheduler backed by a real WriteLockManager."""
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
    from code_indexer.global_repos.query_tracker import QueryTracker
    from code_indexer.global_repos.cleanup_manager import CleanupManager
    from code_indexer.global_repos.global_registry import GlobalRegistry
    from code_indexer.config import ConfigManager

    config_mgr = ConfigManager(lock_dir / "config.json")
    query_tracker = QueryTracker()
    cleanup_manager = CleanupManager(query_tracker)
    registry = GlobalRegistry(str(lock_dir))

    return RefreshScheduler(
        golden_repos_dir=str(lock_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        registry=registry,
    )


# ---------------------------------------------------------------------------
# Thread safety — concurrent acquire, only one succeeds
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """Test concurrent acquire from multiple threads: only one succeeds (threading safety)."""

    def test_concurrent_acquire_only_one_succeeds(self, manager):
        """
        When multiple threads try to acquire the same alias simultaneously,
        exactly one must succeed.
        """
        NUM_THREADS = 10
        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(NUM_THREADS)

        def try_acquire():
            barrier.wait()  # All threads start simultaneously
            result = manager.acquire(
                "concurrent-repo",
                f"thread-{threading.current_thread().ident}",
            )
            with results_lock:
                results.append(result)

        threads = [threading.Thread(target=try_acquire) for _ in range(NUM_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert len(results) == NUM_THREADS, f"All {NUM_THREADS} threads must complete"

        successes = [r for r in results if r is True]
        failures = [r for r in results if r is False]

        assert len(successes) == 1, (
            f"Exactly 1 thread must succeed. Got {len(successes)} successes."
        )
        assert len(failures) == NUM_THREADS - 1, (
            f"Exactly {NUM_THREADS - 1} threads must fail. Got {len(failures)} failures."
        )

        # Clean up: release whichever lock was acquired
        lock_file = manager._lock_file("concurrent-repo")
        if lock_file.exists():
            content = json.loads(lock_file.read_text())
            manager.release("concurrent-repo", content["owner"])

    def test_concurrent_acquire_different_aliases_all_succeed(self, manager):
        """
        When threads acquire different aliases simultaneously, all must succeed.
        """
        NUM_THREADS = 10
        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(NUM_THREADS)

        def try_acquire(idx):
            barrier.wait()
            result = manager.acquire(f"repo-{idx}", f"owner-{idx}")
            with results_lock:
                results.append(result)

        threads = [
            threading.Thread(target=try_acquire, args=(i,))
            for i in range(NUM_THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert len(results) == NUM_THREADS, "All threads must complete"
        assert all(results), "All threads must succeed (different aliases)"

        # Clean up
        for i in range(NUM_THREADS):
            manager.release(f"repo-{i}", f"owner-{i}")


# ---------------------------------------------------------------------------
# RefreshScheduler delegation — AC6
# ---------------------------------------------------------------------------


class TestRefreshSchedulerDelegation:
    """
    AC6: RefreshScheduler delegates to WriteLockManager.

    The existing Story #227 API (acquire_write_lock, release_write_lock,
    is_write_locked, write_lock) must continue to work, but now delegate
    to WriteLockManager instead of using in-memory threading.Lock.
    """

    def test_scheduler_acquire_creates_lock_file(self, scheduler, lock_dir):
        """scheduler.acquire_write_lock() delegates to WriteLockManager and creates a file."""
        result = scheduler.acquire_write_lock("cidx-meta")

        assert result is True, "acquire_write_lock() must return True"

        lock_file = lock_dir / ".locks" / "cidx-meta.lock"
        assert lock_file.exists(), (
            f"Lock file must be created at {lock_file} when using WriteLockManager"
        )

        scheduler.release_write_lock("cidx-meta")

    def test_scheduler_is_write_locked_uses_lock_file(self, scheduler, lock_dir):
        """scheduler.is_write_locked() delegates to WriteLockManager (file-based check)."""
        assert scheduler.is_write_locked("cidx-meta") is False

        scheduler.acquire_write_lock("cidx-meta")
        assert scheduler.is_write_locked("cidx-meta") is True

        scheduler.release_write_lock("cidx-meta")
        assert scheduler.is_write_locked("cidx-meta") is False

    def test_scheduler_release_deletes_lock_file(self, scheduler, lock_dir):
        """scheduler.release_write_lock() delegates to WriteLockManager and deletes file."""
        scheduler.acquire_write_lock("cidx-meta")
        scheduler.release_write_lock("cidx-meta")

        lock_file = lock_dir / ".locks" / "cidx-meta.lock"
        assert not lock_file.exists(), "Lock file must be deleted after release"

    def test_scheduler_write_lock_context_manager_works(self, scheduler, lock_dir):
        """scheduler.write_lock() context manager creates and deletes lock file."""
        lock_file = lock_dir / ".locks" / "cidx-meta.lock"

        with scheduler.write_lock("cidx-meta"):
            assert lock_file.exists(), "Lock file must exist inside context manager"

        assert not lock_file.exists(), "Lock file must be deleted on context manager exit"

    def test_scheduler_exposes_write_lock_manager_attribute(self, scheduler):
        """
        RefreshScheduler must expose a write_lock_manager attribute
        so external code can use WriteLockManager directly.
        """
        from code_indexer.global_repos.write_lock_manager import WriteLockManager

        assert hasattr(scheduler, "write_lock_manager"), (
            "RefreshScheduler must have a write_lock_manager attribute"
        )
        assert isinstance(scheduler.write_lock_manager, WriteLockManager), (
            "write_lock_manager must be a WriteLockManager instance"
        )

    def test_scheduler_acquire_uses_owner_name(self, scheduler, lock_dir):
        """
        acquire_write_lock() passes a meaningful owner name to WriteLockManager.
        Default owner when no owner_name argument is given must be "refresh_scheduler".
        """
        scheduler.acquire_write_lock("cidx-meta")

        lock_file = lock_dir / ".locks" / "cidx-meta.lock"
        content = json.loads(lock_file.read_text())

        assert content.get("owner") == "refresh_scheduler", (
            "Default owner must be 'refresh_scheduler'. "
            f"Got: {content.get('owner')!r}"
        )

        scheduler.release_write_lock("cidx-meta")

    def test_scheduler_acquire_with_explicit_owner_name(self, scheduler, lock_dir):
        """
        acquire_write_lock(owner_name=...) passes the caller-supplied identity to the lock file.
        This enables DependencyMapService and LangfuseTraceSyncService to record their own identity.
        """
        scheduler.acquire_write_lock("cidx-meta", owner_name="dependency_map_service")

        lock_file = lock_dir / ".locks" / "cidx-meta.lock"
        content = json.loads(lock_file.read_text())

        assert content.get("owner") == "dependency_map_service", (
            "Lock file must record caller-supplied owner_name. "
            f"Got: {content.get('owner')!r}"
        )

        scheduler.release_write_lock("cidx-meta", owner_name="dependency_map_service")

    def test_scheduler_write_lock_context_manager_accepts_owner_name(self, scheduler, lock_dir):
        """
        write_lock(alias, owner_name=...) context manager passes owner to WriteLockManager.
        """
        lock_file = lock_dir / ".locks" / "cidx-meta.lock"

        with scheduler.write_lock("cidx-meta", owner_name="langfuse_trace_sync"):
            content = json.loads(lock_file.read_text())
            assert content.get("owner") == "langfuse_trace_sync", (
                "write_lock() context manager must pass owner_name to lock file. "
                f"Got: {content.get('owner')!r}"
            )

        assert not lock_file.exists(), "Lock file must be deleted on context manager exit"
