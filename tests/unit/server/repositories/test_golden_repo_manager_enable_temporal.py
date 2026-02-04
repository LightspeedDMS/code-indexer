"""
Integration tests for GoldenRepoManager enable_temporal flag updates.

Bug #131: Temporal index status inconsistency - enable_temporal flag incorrect.

Tests verify that GoldenRepoManager correctly updates the enable_temporal flag
both in-memory and in the SQLite database after successful temporal index creation.
This is an integration test that ensures the full flow works end-to-end.
"""

import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.storage.database_manager import DatabaseSchema


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        db_schema = DatabaseSchema(db_path)
        db_schema.initialize_database()
        yield db_path


@pytest.fixture
def temp_storage_dir():
    """Create a temporary storage directory for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def git_repo_with_commit(temp_storage_dir):
    """Create a git repository with an initial commit."""
    repo_alias = "test-repo"
    # Create repo inside golden-repos directory to satisfy security path validation
    golden_repos_dir = temp_storage_dir / "golden-repos"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)
    repo_path = golden_repos_dir / repo_alias
    repo_path.mkdir(parents=True, exist_ok=True)

    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    test_file = repo_path / "test.py"
    test_file.write_text("# Test file\n")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    yield repo_alias, repo_path


@pytest.fixture
def manager(temp_db, temp_storage_dir):
    """Create a GoldenRepoManager instance with SQLite backend."""
    manager = GoldenRepoManager(
        data_dir=str(temp_storage_dir), db_path=temp_db, use_sqlite=True
    )

    # Inject mock BackgroundJobManager that executes worker synchronously
    def mock_submit_job(operation_type, func, submitter_username, is_admin, repo_alias):
        """Execute the background worker synchronously for testing."""
        func()  # Execute the worker function immediately
        return "test-job-id-12345"

    mock_bg_manager = MagicMock(spec=BackgroundJobManager)
    mock_bg_manager.submit_job.side_effect = mock_submit_job
    manager.background_job_manager = mock_bg_manager
    yield manager
    # Close SQLite backend if it exists
    if manager._sqlite_backend:
        manager._sqlite_backend.close()


def test_add_golden_repo_index_temporal_updates_flag_in_database(
    manager, git_repo_with_commit
):
    """
    Integration test: Verify that add_golden_repo_index updates enable_temporal
    in BOTH in-memory and SQLite database after successful temporal index creation.

    This is the critical bug fix for Bug #131 - the flag must persist across
    server restarts, which means it MUST be written to the database.
    """
    repo_alias, repo_path = git_repo_with_commit

    # AC1: Manually create repo in manager state (bypass async registration for this test)
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
    from datetime import datetime, timezone

    test_repo = GoldenRepo(
        alias=repo_alias,
        repo_url=str(repo_path),
        default_branch="master",
        clone_path=str(repo_path),
        created_at=datetime.now(timezone.utc).isoformat(),
        enable_temporal=False,
    )
    manager.golden_repos[repo_alias] = test_repo

    # Persist to database
    if manager._sqlite_backend:
        manager._sqlite_backend.add_repo(
            alias=repo_alias,
            repo_url=str(repo_path),
            default_branch="master",
            clone_path=str(repo_path),
            created_at=test_repo.created_at,
            enable_temporal=False,
        )

    # AC2: Verify initial state - enable_temporal should be False
    repo = manager.get_golden_repo(repo_alias)
    assert repo is not None
    assert repo.enable_temporal is False

    # AC3: Verify database state - enable_temporal should be False in DB
    if manager._sqlite_backend is not None:
        db_repo = manager._sqlite_backend.get_repo(repo_alias)
        assert db_repo is not None
        assert db_repo["enable_temporal"] is False

    # AC4: Mock the cidx index command to succeed
    with patch("subprocess.run") as mock_run:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Temporal index created successfully"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        job_id = manager.add_index_to_golden_repo(alias=repo_alias, index_type="temporal")
        assert job_id is not None

    # AC5: Verify in-memory state - enable_temporal should be True
    repo = manager.get_golden_repo(repo_alias)
    assert repo is not None
    assert repo.enable_temporal is True, "In-memory enable_temporal flag not updated"

    # AC6: CRITICAL - Verify database state - enable_temporal should be True in DB
    if manager._sqlite_backend is not None:
        db_repo = manager._sqlite_backend.get_repo(repo_alias)
        assert db_repo is not None
        assert (
            db_repo["enable_temporal"] is True
        ), "Database enable_temporal flag not updated - Bug #131 NOT FIXED"


def test_enable_temporal_persists_across_restart(manager, git_repo_with_commit):
    """
    Test that enable_temporal flag persists across server restart.

    This verifies that the database update in Bug #131 fix actually works.
    """
    repo_alias, repo_path = git_repo_with_commit

    # AC1: Manually create repo in manager state
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
    from datetime import datetime, timezone

    test_repo = GoldenRepo(
        alias=repo_alias,
        repo_url=str(repo_path),
        default_branch="master",
        clone_path=str(repo_path),
        created_at=datetime.now(timezone.utc).isoformat(),
        enable_temporal=False,
    )
    manager.golden_repos[repo_alias] = test_repo

    # Persist to database
    if manager._sqlite_backend:
        manager._sqlite_backend.add_repo(
            alias=repo_alias,
            repo_url=str(repo_path),
            default_branch="master",
            clone_path=str(repo_path),
            created_at=test_repo.created_at,
            enable_temporal=False,
        )

    with patch("subprocess.run") as mock_run:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Temporal index created successfully"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        manager.add_index_to_golden_repo(alias=repo_alias, index_type="temporal")

    # AC2: Get DB path before shutdown
    db_path = (
        manager._sqlite_backend._conn_manager.db_path
        if manager._sqlite_backend
        else None
    )
    data_dir = manager.data_dir

    # AC3: Simulate server restart by closing and creating new instance
    if manager._sqlite_backend:
        manager._sqlite_backend.close()

    new_manager = GoldenRepoManager(
        data_dir=data_dir, db_path=db_path, use_sqlite=True
    )

    # Inject mock BackgroundJobManager that executes worker synchronously
    def mock_submit_job_new(operation_type, func, submitter_username, is_admin, repo_alias):
        """Execute the background worker synchronously for testing."""
        func()  # Execute the worker function immediately
        return "test-job-id-12345"

    mock_bg_manager = MagicMock(spec=BackgroundJobManager)
    mock_bg_manager.submit_job.side_effect = mock_submit_job_new
    new_manager.background_job_manager = mock_bg_manager

    # AC4: Verify flag persisted across restart
    repo = new_manager.get_golden_repo(repo_alias)
    assert repo is not None
    assert (
        repo.enable_temporal is True
    ), "enable_temporal flag did not persist - Bug #131 NOT FIXED"

    # Cleanup
    if new_manager._sqlite_backend:
        new_manager._sqlite_backend.close()


def test_add_golden_repo_index_temporal_no_sqlite_updates_memory_only(
    temp_storage_dir, git_repo_with_commit
):
    """
    Test that when SQLite is disabled, enable_temporal is updated in-memory only.

    This verifies the JSON backend path still works correctly.
    """
    repo_alias, repo_path = git_repo_with_commit

    # AC1: Create manager without SQLite backend
    manager = GoldenRepoManager(data_dir=str(temp_storage_dir), use_sqlite=False)

    # Inject mock BackgroundJobManager that executes worker synchronously
    def mock_submit_job_nosql(operation_type, func, submitter_username, is_admin, repo_alias):
        """Execute the background worker synchronously for testing."""
        func()  # Execute the worker function immediately
        return "test-job-id-12345"

    mock_bg_manager = MagicMock(spec=BackgroundJobManager)
    mock_bg_manager.submit_job.side_effect = mock_submit_job_nosql
    manager.background_job_manager = mock_bg_manager

    # AC2: Manually create repo in manager state
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
    from datetime import datetime, timezone

    test_repo = GoldenRepo(
        alias=repo_alias,
        repo_url=str(repo_path),
        default_branch="master",
        clone_path=str(repo_path),
        created_at=datetime.now(timezone.utc).isoformat(),
        enable_temporal=False,
    )
    manager.golden_repos[repo_alias] = test_repo

    # AC3: Verify initial state
    repo = manager.get_golden_repo(repo_alias)
    assert repo is not None
    assert repo.enable_temporal is False

    # AC4: Mock the cidx index command to succeed
    with patch("subprocess.run") as mock_run:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Temporal index created successfully"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        job_id = manager.add_index_to_golden_repo(alias=repo_alias, index_type="temporal")
        assert job_id is not None

    # AC5: Verify in-memory state updated
    repo = manager.get_golden_repo(repo_alias)
    assert repo is not None
    assert repo.enable_temporal is True
