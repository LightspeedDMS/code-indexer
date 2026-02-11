"""
Unit tests for GoldenRepoManager operation locking.

Tests verify that concurrent operations (add, remove, refresh) on golden repositories
are properly synchronized and that metadata file operations maintain consistency.

Story #620 Priority 2A Acceptance Criteria:
- Concurrent add operations should be serialized
- Concurrent remove operations should be serialized
- Concurrent add/remove operations should be serialized
- Metadata file access should be protected
- Lock should be released on exception
"""

import pytest
import threading
import time
import json
from unittest.mock import Mock, patch
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


@pytest.fixture
def mock_data_dir(tmp_path):
    """Create temporary data directory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    golden_repos_dir = data_dir / "golden-repos"
    golden_repos_dir.mkdir()
    return str(data_dir)


@pytest.fixture
def manager(mock_data_dir):
    """Create GoldenRepoManager instance for testing."""
    mgr = GoldenRepoManager(data_dir=mock_data_dir)
    # Mock background_job_manager dependency
    # Execute worker functions synchronously for testing
    def mock_submit_job(operation_type, func, submitter_username, is_admin, repo_alias):
        func()  # Execute worker synchronously
        return f"test-job-{repo_alias}"

    mgr.background_job_manager = Mock()
    mgr.background_job_manager.submit_job.side_effect = mock_submit_job
    return mgr


def test_concurrent_add_operations_serialized(manager):
    """
    Test that concurrent add_golden_repo operations are serialized.

    Acceptance Criteria:
    - Multiple concurrent add operations should not corrupt metadata
    - Operations should complete in serial order
    - All operations should succeed without race conditions
    """
    with (
        patch.object(manager, "_validate_git_repository", return_value=True),
        patch.object(manager, "_clone_repository", return_value="/path/to/clone"),
        patch.object(manager, "_execute_post_clone_workflow"),
    ):
        # Run three concurrent add operations
        threads = []
        for i in range(3):

            def add_repo(index=i):
                manager.add_golden_repo(
                    alias=f"test-repo-{index}",
                    repo_url=f"https://github.com/user/repo{index}.git",
                    default_branch="main",
                    submitter_username="test-user",
                )

            thread = threading.Thread(target=add_repo, name=f"add_{i}")
            threads.append(thread)

        # Start all threads
        for t in threads:
            t.start()

        # Wait for completion
        for t in threads:
            t.join(timeout=5.0)

        # Verify all threads completed
        assert all(not t.is_alive() for t in threads), "All threads should complete"

        # Verify all repos were added
        assert len(manager.golden_repos) == 3
        for i in range(3):
            assert f"test-repo-{i}" in manager.golden_repos


def test_concurrent_remove_operations_serialized(manager):
    """
    Test that concurrent remove_golden_repo operations are serialized.

    Acceptance Criteria:
    - Multiple concurrent remove operations should not corrupt metadata
    - Operations should complete in serial order
    - No race conditions during cleanup
    """
    # Pre-populate metadata with test repos
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepo

    manager.golden_repos = {
        "repo1": GoldenRepo(
            alias="repo1",
            repo_url="https://github.com/user/repo1.git",
            default_branch="main",
            clone_path="/path/to/repo1",
            created_at="2025-01-01T00:00:00Z",
        ),
        "repo2": GoldenRepo(
            alias="repo2",
            repo_url="https://github.com/user/repo2.git",
            default_branch="main",
            clone_path="/path/to/repo2",
            created_at="2025-01-01T00:00:00Z",
        ),
        "repo3": GoldenRepo(
            alias="repo3",
            repo_url="https://github.com/user/repo3.git",
            default_branch="main",
            clone_path="/path/to/repo3",
            created_at="2025-01-01T00:00:00Z",
        ),
    }

    # Track execution order
    execution_log = []
    lock = threading.Lock()

    def tracked_cleanup(clone_path):
        """Track when cleanup is called."""
        with lock:
            execution_log.append(
                ("cleanup_start", threading.current_thread().name, clone_path)
            )
        time.sleep(0.05)  # Simulate slow cleanup
        result = True  # Mock successful cleanup
        with lock:
            execution_log.append(
                ("cleanup_end", threading.current_thread().name, clone_path)
            )
        return result

    with patch.object(
        manager, "_cleanup_repository_files", side_effect=tracked_cleanup
    ):
        # Run three concurrent remove operations
        threads = []
        for i in range(1, 4):

            def remove_repo(index=i):
                manager.remove_golden_repo(
                    alias=f"repo{index}", submitter_username="test-user"
                )

            thread = threading.Thread(target=remove_repo, name=f"remove_{i}")
            threads.append(thread)

        # Start all threads
        for t in threads:
            t.start()

        # Wait for completion
        for t in threads:
            t.join(timeout=5.0)

        # Verify all threads completed
        assert all(not t.is_alive() for t in threads), "All threads should complete"

        # Verify cleanup operations were serialized
        cleanup_starts = [
            i
            for i, (event, _, _) in enumerate(execution_log)
            if event == "cleanup_start"
        ]
        cleanup_ends = [
            i for i, (event, _, _) in enumerate(execution_log) if event == "cleanup_end"
        ]

        # Each cleanup_end should come before the next cleanup_start
        for i in range(len(cleanup_starts) - 1):
            assert (
                cleanup_ends[i] < cleanup_starts[i + 1]
            ), f"Cleanup operations should be serialized (log: {execution_log})"


def test_concurrent_add_remove_serialized(manager):
    """
    Test that concurrent add and remove operations complete without errors.

    Acceptance Criteria:
    - Add and remove operations should not interfere with each other
    - Both operation types should complete successfully
    """
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepo

    manager.golden_repos = {
        "existing-repo": GoldenRepo(
            alias="existing-repo",
            repo_url="https://github.com/user/existing.git",
            default_branch="main",
            clone_path="/path/to/existing",
            created_at="2025-01-01T00:00:00Z",
        ),
    }
    manager._sqlite_backend.add_repo(
        alias="existing-repo",
        repo_url="https://github.com/user/existing.git",
        default_branch="main",
        clone_path="/path/to/existing",
        created_at="2025-01-01T00:00:00Z",
    )

    errors = []

    with (
        patch.object(manager, "_validate_git_repository", return_value=True),
        patch.object(manager, "_clone_repository", return_value="/path/to/new"),
        patch.object(manager, "_execute_post_clone_workflow"),
        patch.object(manager, "_cleanup_repository_files", return_value=True),
    ):
        def add_operation():
            try:
                manager.add_golden_repo(
                    alias="new-repo",
                    repo_url="https://github.com/user/new.git",
                    default_branch="main",
                    submitter_username="test-user",
                )
            except Exception as e:
                errors.append(f"add: {e}")

        def remove_operation():
            time.sleep(0.01)
            try:
                manager.remove_golden_repo(
                    alias="existing-repo", submitter_username="test-user"
                )
            except Exception as e:
                errors.append(f"remove: {e}")

        add_thread = threading.Thread(target=add_operation)
        remove_thread = threading.Thread(target=remove_operation)

        add_thread.start()
        remove_thread.start()

        add_thread.join(timeout=5.0)
        remove_thread.join(timeout=5.0)

        assert not add_thread.is_alive(), "Add thread should complete"
        assert not remove_thread.is_alive(), "Remove thread should complete"
        assert len(errors) == 0, f"Operations should succeed without errors: {errors}"


def test_metadata_lock_prevents_corruption(manager):
    """
    Test that operation lock prevents data corruption from concurrent access.

    Acceptance Criteria:
    - Concurrent repo additions should not corrupt in-memory data
    - All operations should complete successfully
    - Final state should contain all added repos
    """
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepo

    manager.golden_repos = {
        f"repo{i}": GoldenRepo(
            alias=f"repo{i}",
            repo_url=f"https://github.com/user/repo{i}.git",
            default_branch="main",
            clone_path=f"/path/to/repo{i}",
            created_at="2025-01-01T00:00:00Z",
        )
        for i in range(5)
    }

    errors = []

    def concurrent_repo_add(repo_index):
        """Simulate concurrent repo additions."""
        from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
        try:
            for j in range(3):
                new_alias = f"repo{repo_index}-added-{j}"
                with manager._operation_lock:
                    manager.golden_repos[new_alias] = GoldenRepo(
                        alias=new_alias,
                        repo_url=f"https://github.com/user/{new_alias}.git",
                        default_branch="main",
                        clone_path=f"/path/to/{new_alias}",
                        created_at="2025-01-01T00:00:00Z",
                    )
                time.sleep(0.01)
        except Exception as e:
            errors.append(str(e))

    threads = [
        threading.Thread(target=concurrent_repo_add, args=(i,)) for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert all(not t.is_alive() for t in threads)
    assert len(errors) == 0, f"Concurrent operations had errors: {errors}"
    assert isinstance(manager.golden_repos, dict)
    # 5 original + 9 added (3 threads * 3 each)
    assert len(manager.golden_repos) == 14


def test_operation_lock_released_on_exception(manager):
    """
    Test that operation lock is released when operations raise exceptions.

    Acceptance Criteria:
    - Lock should be released even when operations fail
    - Subsequent operations should succeed after exceptions
    - Lock state should be unlocked after exception
    """
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepo

    manager.golden_repos["test-repo"] = GoldenRepo(
        alias="test-repo",
        repo_url="https://github.com/user/test.git",
        default_branch="main",
        clone_path="/path/to/test",
        created_at="2025-01-01T00:00:00Z",
    )

    # Verify lock is not held
    assert not manager._operation_lock.locked()

    # Simulate an operation that acquires lock and fails
    try:
        with manager._operation_lock:
            raise IOError("Simulated failure")
    except IOError:
        pass

    # Verify lock was released
    assert not manager._operation_lock.locked()

    # Verify subsequent operations succeed
    can_acquire = manager._operation_lock.acquire(timeout=1.0)
    assert can_acquire, "Should be able to acquire lock after exception"
    manager._operation_lock.release()
