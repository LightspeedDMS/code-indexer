"""
Test that golden repo job results contain alias field for dashboard display.

This test verifies the fix for the dashboard "Unknown" repository bug.
All background job results should include the repository alias so the
dashboard can display the repository name in the Recent Activity section.
"""

import pytest
import tempfile
import shutil
from pathlib import Path
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.repositories.background_jobs import BackgroundJobManager


@pytest.fixture
def temp_dirs():
    """Create temporary directories for testing."""
    temp_dir = Path(tempfile.mkdtemp())
    golden_repos_dir = temp_dir / "golden-repos"
    job_storage = temp_dir / "jobs"
    golden_repos_dir.mkdir(parents=True)
    job_storage.mkdir(parents=True)

    yield {
        "golden_repos_dir": str(golden_repos_dir),
        "job_storage": str(job_storage / "jobs.json"),
    }

    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def managers(temp_dirs):
    """Create manager instances for testing."""
    job_manager = BackgroundJobManager(storage_path=temp_dirs["job_storage"])
    golden_manager = GoldenRepoManager(data_dir=temp_dirs["golden_repos_dir"])

    # Inject background job manager (pattern from test_golden_repo_manager.py)
    golden_manager.background_job_manager = job_manager

    return {
        "job_manager": job_manager,
        "golden_manager": golden_manager,
    }


def test_add_golden_repo_result_contains_alias(managers, temp_dirs):
    """Test that add_golden_repo job result contains alias field."""
    # Create a test repository
    test_repo_path = Path(temp_dirs["golden_repos_dir"]) / "test-repo"
    test_repo_path.mkdir(parents=True)

    # Initialize as git repo
    import subprocess

    subprocess.run(["git", "init"], cwd=test_repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=test_repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=test_repo_path,
        check=True,
        capture_output=True,
    )

    # Create initial commit
    test_file = test_repo_path / "README.md"
    test_file.write_text("# Test Repo")
    subprocess.run(
        ["git", "add", "."], cwd=test_repo_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=test_repo_path,
        check=True,
        capture_output=True,
    )

    # Submit add_golden_repo job
    job_id = managers["golden_manager"].add_golden_repo(
        repo_url=str(test_repo_path),
        alias="test-repo",
        submitter_username="test-user",
    )

    # Wait for job completion (synchronously execute for testing)
    import time

    max_wait = 10
    waited = 0
    while waited < max_wait:
        job = managers["job_manager"].jobs.get(job_id)
        if job and job.status.value in ["completed", "failed"]:
            break
        time.sleep(0.1)
        waited += 0.1

    # Verify job completed successfully
    job = managers["job_manager"].jobs.get(job_id)
    assert job is not None, f"Job {job_id} not found"
    assert job.status.value == "completed", f"Job failed: {job.error}"

    # CRITICAL: Verify result contains alias field for dashboard display
    assert job.result is not None, "Job result is None"
    assert (
        "alias" in job.result
    ), "Job result missing 'alias' field - dashboard will show 'Unknown'"
    assert (
        job.result["alias"] == "test-repo"
    ), f"Expected alias 'test-repo', got '{job.result['alias']}'"


def test_refresh_golden_repo_result_contains_alias(temp_dirs):
    """Test that RefreshScheduler._execute_refresh() result contains alias field.

    Refreshes now go through RefreshScheduler (not GoldenRepoManager.refresh_golden_repo
    which has been removed). This test verifies the result dict returned by
    _execute_refresh always contains the 'alias' key so the dashboard can display
    the repository name in the Recent Activity section.
    """
    from unittest.mock import patch
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
    from code_indexer.global_repos.query_tracker import QueryTracker
    from code_indexer.global_repos.cleanup_manager import CleanupManager
    from code_indexer.global_repos.alias_manager import AliasManager
    from code_indexer.global_repos.global_registry import GlobalRegistry
    from code_indexer.config import ConfigManager

    golden_repos_dir = Path(temp_dirs["golden_repos_dir"])
    alias_name = "refresh-test-repo-global"
    repo_name = "refresh-test-repo"

    # Set up alias and registry so _execute_refresh can find the repo
    aliases_dir = golden_repos_dir / "aliases"
    aliases_dir.mkdir(parents=True, exist_ok=True)
    alias_manager = AliasManager(str(aliases_dir))

    # Create a local repo directory (simulates the source repo)
    repo_dir = golden_repos_dir / repo_name
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "README.md").write_text("# Refresh Test Repo")

    # Point alias at repo_dir
    alias_manager.create_alias(alias_name, str(repo_dir))

    # Register in registry
    registry = GlobalRegistry(str(golden_repos_dir))
    registry.register_global_repo(
        repo_name,
        alias_name,
        f"local://{repo_name}",
        str(repo_dir),
    )

    config_mgr = ConfigManager(golden_repos_dir / ".code-indexer" / "config.json")
    query_tracker = QueryTracker()
    cleanup_manager = CleanupManager(query_tracker)

    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        registry=registry,
    )

    # Patch out the heavy indexing steps - we only care about the result dict shape
    with (
        patch.object(scheduler, "_has_local_changes", return_value=True),
        patch.object(scheduler, "_index_source"),
        patch.object(scheduler, "_create_snapshot", return_value=str(repo_dir)),
        patch.object(scheduler.alias_manager, "swap_alias"),
        patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
        patch.object(scheduler.registry, "update_refresh_timestamp"),
        patch.object(
            scheduler,
            "_detect_existing_indexes",
            return_value={"semantic": True, "fts": False},
        ),
        patch.object(scheduler, "_reconcile_registry_with_filesystem"),
    ):
        result = scheduler._execute_refresh(alias_name)

    # CRITICAL: Verify result contains alias field for dashboard display
    assert result is not None, "Result is None"
    assert (
        "alias" in result
    ), "Result missing 'alias' field - dashboard will show 'Unknown'"
    assert result["alias"] == alias_name, (
        f"Expected alias '{alias_name}', got '{result['alias']}'"
    )
    assert result["success"] is True, f"Expected success=True, got: {result}"
