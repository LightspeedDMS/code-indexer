"""
Unit tests for Bug #1618: activation clone-phase orphan cleanup (Bug #1349)
runs -- and logs a false "late-materializing async clone" WARNING plus
~12s of real time.sleep() calls -- even when the clone step was NEVER
attempted because write-lock acquisition failed or a refresh was already
in flight for the golden repo.

Root cause: `_clone_with_copy_on_write` raises `ActivatedRepoError` from two
guard branches (`scheduler.check_refresh_not_in_progress` ->
`DuplicateJobError`, and a failed `scheduler.acquire_write_lock(...)`) that
both execute BEFORE `self._clone_backend.create_clone_at_path(...)` is ever
reached -- no clone backend call, no directory creation is even possible.
`_do_activate_repository`'s `except ActivatedRepoError:` handler is blanket:
it cannot distinguish this "clone never started" case from a genuine
mid-flight clone failure, so it unconditionally invokes
`_cleanup_orphaned_clone_after_failure`, which runs the Bug #1349 bounded
retry loop (up to 12 real `time.sleep(1.0)` calls) and, on exhaustion, logs
a WARNING claiming "a late-materializing async clone may still be in
flight" -- which is definitionally false here.

Fix under test: the two pre-clone-attempt guard branches raise a distinct
`ActivatedRepoCloneNotStartedError(ActivatedRepoError)` subtype, and
`_do_activate_repository` re-raises it immediately without invoking the
cleanup loop at all.

Mocking policy (anti-mock): golden_repo_manager and clone_backend are the
established test doubles already used by the sibling
test_activated_repo_manager_write_lock_coordination_1393.py suite. The
RefreshScheduler, WriteLockManager, and JobTracker are ALL REAL -- backed
by a real temp golden_repos_dir and a real SQLite background_jobs table --
because the write-lock-acquisition-failure and refresh-in-progress paths
under test are exactly what these real components produce; mocking them
would prove nothing about the bug being fixed. `_do_activate_repository`
(the real production caller with the real cleanup bug) is exercised
directly, not just the lower-level `_clone_with_copy_on_write`.

Import note: every production symbol here is imported via the plain
`code_indexer...` path (never `src.code_indexer...`), mirroring
test_activated_repo_manager_write_lock_coordination_1393.py -- mixing
prefixes makes Python load two separate copies of shared modules like
job_tracker.py, breaking `except SameName` isinstance checks.
"""

import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
    ActivatedRepoManager,
)
from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
from code_indexer.server.utils.config_manager import ServerResourceConfig
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.config import ConfigManager
from code_indexer.server.services.job_tracker import JobTracker


_BACKGROUND_JOBS_DDL = """
    CREATE TABLE IF NOT EXISTS background_jobs (
        job_id TEXT PRIMARY KEY,
        operation_type TEXT,
        status TEXT,
        created_at TEXT,
        started_at TEXT,
        completed_at TEXT,
        result TEXT,
        error TEXT,
        progress INTEGER DEFAULT 0,
        username TEXT,
        is_admin INTEGER DEFAULT 0,
        cancelled INTEGER DEFAULT 0,
        repo_alias TEXT,
        resolution_attempts INTEGER DEFAULT 0,
        progress_info TEXT,
        metadata TEXT,
        actor_username TEXT
    )
"""

# Real-clock ceiling proving the ~12s Bug #1349 cleanup loop did NOT run.
# The bug's window is (13 - 1) * 1.0s = 12s; any fast-fail well under that
# proves the cleanup loop was skipped entirely, not just shortened.
_FAST_FAIL_CEILING_SECONDS = 2.0


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def job_tracker_real(tmp_path):
    """Real JobTracker backed by a real SQLite background_jobs table."""
    db_path = str(tmp_path / "tracker.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_BACKGROUND_JOBS_DDL)
        conn.commit()
    finally:
        conn.close()
    return JobTracker(db_path)


@pytest.fixture
def real_refresh_scheduler(tmp_path, job_tracker_real):
    """Real RefreshScheduler with a real WriteLockManager (file-based locks
    under a real temp golden_repos_dir) and a real JobTracker."""
    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)
    config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
    query_tracker = QueryTracker()
    cleanup_manager = CleanupManager(query_tracker)
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        job_tracker=job_tracker_real,
    )


@pytest.fixture
def golden_repo_manager_mock(real_refresh_scheduler):
    """Mock golden repo manager wired to a REAL RefreshScheduler."""
    mock = MagicMock()
    golden_repo = GoldenRepo(
        alias="test-repo",
        repo_url="https://github.com/example/test-repo.git",
        default_branch="main",
        clone_path="/path/to/golden/test-repo",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    golden_repos_dict = {"test-repo": golden_repo}
    mock.golden_repos = golden_repos_dict
    mock.get_golden_repo.side_effect = lambda alias: golden_repos_dict.get(alias)
    mock.get_actual_repo_path.return_value = "/path/to/golden/test-repo"
    mock.resource_config = ServerResourceConfig()
    mock._refresh_scheduler = real_refresh_scheduler
    return mock


@pytest.fixture
def background_job_manager_mock():
    mock = MagicMock()
    mock.submit_job.return_value = "job-123"
    return mock


@pytest.fixture
def mock_clone_backend():
    backend = MagicMock()
    backend.create_clone_at_path.return_value = "/dest/path"
    return backend


@pytest.fixture
def activated_repo_manager(
    temp_data_dir,
    golden_repo_manager_mock,
    background_job_manager_mock,
    mock_clone_backend,
):
    return ActivatedRepoManager(
        data_dir=temp_data_dir,
        golden_repo_manager=golden_repo_manager_mock,
        background_job_manager=background_job_manager_mock,
        clone_backend=mock_clone_backend,
    )


def _find_records(caplog_records, substring):
    return [r for r in caplog_records if substring in r.message]


class TestCloneNotStartedSkipsOrphanCleanup1618:
    def test_write_lock_already_held_activation_fails_fast_no_cleanup_loop(
        self,
        activated_repo_manager,
        real_refresh_scheduler,
        mock_clone_backend,
        caplog,
    ):
        """When another writer already holds the golden repo's write lock,
        _do_activate_repository must fail FAST -- zero time.sleep() calls,
        no false "exhausted"/"late-materializing" cleanup WARNING, and no
        clone backend call at all -- because the clone step was never
        reached."""
        acquired = real_refresh_scheduler.acquire_write_lock(
            "test-repo", owner_name="external_writer"
        )
        assert acquired is True

        with (
            patch(
                "code_indexer.server.repositories.activated_repo_manager.time.sleep"
            ) as mock_sleep,
            caplog.at_level("WARNING"),
        ):
            start = time.monotonic()
            with pytest.raises(ActivatedRepoError, match="write lock"):
                activated_repo_manager._do_activate_repository(
                    username="testuser",
                    golden_repo_alias="test-repo",
                    branch_name="main",
                    user_alias="my-activated-repo",
                )
            elapsed = time.monotonic() - start

        mock_clone_backend.create_clone_at_path.assert_not_called()
        mock_sleep.assert_not_called()
        assert elapsed < _FAST_FAIL_CEILING_SECONDS, (
            f"activation took {elapsed:.2f}s -- expected an immediate "
            "fail-fast with no Bug #1349 cleanup grace loop"
        )

        exhaustion_warnings = _find_records(caplog.records, "exhausted")
        late_materializing_warnings = _find_records(
            caplog.records, "late-materializing"
        )
        assert not exhaustion_warnings, (
            "no clone was ever attempted -- the exhaustion WARNING is "
            f"definitionally false here, got: {[r.message for r in caplog.records]}"
        )
        assert not late_materializing_warnings, (
            "no clone was ever attempted -- the 'late-materializing async "
            "clone' WARNING is definitionally false here, got: "
            f"{[r.message for r in caplog.records]}"
        )

    def test_refresh_in_progress_activation_fails_fast_no_cleanup_loop(
        self,
        activated_repo_manager,
        real_refresh_scheduler,
        job_tracker_real,
        mock_clone_backend,
        caplog,
    ):
        """When a global_repo_refresh is ALREADY in flight for the golden
        repo (JobTracker-registered), _do_activate_repository must fail
        FAST with zero sleeps and no false cleanup WARNING -- the clone
        step is never reached because check_refresh_not_in_progress raises
        before acquire_write_lock is even attempted."""
        job_tracker_real.register_job(
            "refresh-test-repo-global",
            operation_type="global_repo_refresh",
            username="system",
            repo_alias="test-repo-global",
        )
        job_tracker_real.update_status("refresh-test-repo-global", status="running")

        with (
            patch(
                "code_indexer.server.repositories.activated_repo_manager.time.sleep"
            ) as mock_sleep,
            caplog.at_level("WARNING"),
        ):
            start = time.monotonic()
            with pytest.raises(ActivatedRepoError):
                activated_repo_manager._do_activate_repository(
                    username="testuser",
                    golden_repo_alias="test-repo",
                    branch_name="main",
                    user_alias="my-activated-repo",
                )
            elapsed = time.monotonic() - start

        mock_clone_backend.create_clone_at_path.assert_not_called()
        mock_sleep.assert_not_called()
        assert elapsed < _FAST_FAIL_CEILING_SECONDS, (
            f"activation took {elapsed:.2f}s -- expected an immediate "
            "fail-fast with no Bug #1349 cleanup grace loop"
        )
        assert real_refresh_scheduler.is_write_locked("test-repo") is False, (
            "no lock should ever be acquired -- the JobTracker check runs "
            "before lock acquisition"
        )

        exhaustion_warnings = _find_records(caplog.records, "exhausted")
        late_materializing_warnings = _find_records(
            caplog.records, "late-materializing"
        )
        assert not exhaustion_warnings, (
            "no clone was ever attempted -- the exhaustion WARNING is "
            f"definitionally false here, got: {[r.message for r in caplog.records]}"
        )
        assert not late_materializing_warnings, (
            "no clone was ever attempted -- the 'late-materializing async "
            "clone' WARNING is definitionally false here, got: "
            f"{[r.message for r in caplog.records]}"
        )
