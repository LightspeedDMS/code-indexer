"""
Unit tests for Bug #1393 fix: RefreshScheduler.check_refresh_not_in_progress().

Bug #1393: repository activation's CoW clone raced a concurrently-running
global_repo_refresh on the same golden repo with zero coordination.
WriteLockManager alone cannot signal "a refresh is ALREADY executing" because
_execute_refresh() only CHECKS is_write_locked() -- it never HOLDS the write
lock itself while running (Story #227 reserves that lock for the enumerated
external-writer set). The cluster-visible signal for "currently executing"
is JobTracker, since _execute_refresh() registers itself into it (Bug #935)
under the alias_name form (bare alias + "-global").

check_refresh_not_in_progress(alias) is the new primitive that lets other
writers (activation) fail fast instead of racing an in-flight refresh,
mirroring the register_job_if_no_conflict/DuplicateJobError convention used
elsewhere in this codebase for conflicting operations.

Real JobTracker backed by a real SQLite background_jobs table is used
throughout (anti-mock rule for storage primitives) -- mirrors the pattern in
test_refresh_scheduler_job_tracker_935.py.
"""

import sqlite3

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.config import ConfigManager
from code_indexer.server.services.job_tracker import DuplicateJobError, JobTracker


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


@pytest.fixture
def golden_repos_dir(tmp_path):
    d = tmp_path / ".code-indexer" / "golden_repos"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def config_mgr(tmp_path):
    return ConfigManager(tmp_path / ".code-indexer" / "config.json")


@pytest.fixture
def query_tracker():
    return QueryTracker()


@pytest.fixture
def cleanup_manager(query_tracker):
    return CleanupManager(query_tracker)


@pytest.fixture
def job_tracker_db(tmp_path):
    """Create a real JobTracker backed by a real SQLite DB."""
    db_path = str(tmp_path / "tracker.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_BACKGROUND_JOBS_DDL)
        conn.commit()
    finally:
        conn.close()
    return JobTracker(db_path)


def _make_scheduler(
    golden_repos_dir, config_mgr, query_tracker, cleanup_manager, job_tracker=None
):
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        job_tracker=job_tracker,
    )


class TestCheckRefreshNotInProgress:
    def test_no_active_refresh_job_does_not_raise(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        job_tracker_db,
    ):
        """No global_repo_refresh job registered for the alias -- must be a no-op."""
        scheduler = _make_scheduler(
            golden_repos_dir,
            config_mgr,
            query_tracker,
            cleanup_manager,
            job_tracker=job_tracker_db,
        )

        # Must not raise.
        scheduler.check_refresh_not_in_progress("evolution")

    def test_active_refresh_job_raises_duplicate_job_error(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        job_tracker_db,
    ):
        """A running global_repo_refresh job for '{alias}-global' must fail fast.

        Registered exactly like _execute_refresh() registers itself
        (Bug #935): repo_alias is the FULL '-global' suffixed alias, job_id
        follows the 'refresh-{alias_name}' convention.
        """
        scheduler = _make_scheduler(
            golden_repos_dir,
            config_mgr,
            query_tracker,
            cleanup_manager,
            job_tracker=job_tracker_db,
        )

        job_tracker_db.register_job(
            "refresh-evolution-global",
            operation_type="global_repo_refresh",
            username="system",
            repo_alias="evolution-global",
        )
        job_tracker_db.update_status("refresh-evolution-global", status="running")

        with pytest.raises(DuplicateJobError) as exc_info:
            scheduler.check_refresh_not_in_progress("evolution")

        assert exc_info.value.existing_job_id == "refresh-evolution-global"

    def test_different_alias_refresh_job_does_not_raise(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        job_tracker_db,
    ):
        """A refresh job for a DIFFERENT golden repo alias must not block this one."""
        scheduler = _make_scheduler(
            golden_repos_dir,
            config_mgr,
            query_tracker,
            cleanup_manager,
            job_tracker=job_tracker_db,
        )

        job_tracker_db.register_job(
            "refresh-other-repo-global",
            operation_type="global_repo_refresh",
            username="system",
            repo_alias="other-repo-global",
        )
        job_tracker_db.update_status("refresh-other-repo-global", status="running")

        # Must not raise -- "evolution" != "other-repo".
        scheduler.check_refresh_not_in_progress("evolution")

    def test_no_job_tracker_is_noop(
        self, golden_repos_dir, config_mgr, query_tracker, cleanup_manager
    ):
        """CLI/solo mode (no job_tracker wired) must be a safe no-op."""
        scheduler = _make_scheduler(
            golden_repos_dir,
            config_mgr,
            query_tracker,
            cleanup_manager,
            job_tracker=None,
        )

        # Must not raise (and must not AttributeError on self._job_tracker).
        scheduler.check_refresh_not_in_progress("evolution")
