"""Bug #1393 follow-up: GlobalReposLifecycleManager must forward the
job_tracker it receives into the RefreshScheduler it constructs.

Root cause: GlobalReposLifecycleManager.__init__ stores the caller-supplied
job_tracker on self._job_tracker (used for startup-reconcile job dashboard
visibility) AND forwards it to CleanupManager, but the RefreshScheduler(...)
construction call a few lines above never passed job_tracker=job_tracker.
RefreshScheduler.check_refresh_not_in_progress()'s guard
(`if self._job_tracker is None: return`) turns into a permanent no-op in
every real server process, since the manager is always constructed with
job_tracker=None from RefreshScheduler's point of view -- direction 1 of
Bug #1393's fix (fail-fast on an already-in-flight refresh) never actually
fires in production, even though startup/lifespan.py DOES pass
job_tracker=job_tracker into GlobalReposLifecycleManager correctly.

The pre-existing test_refresh_scheduler_activation_coordination_1393.py
unit-tests RefreshScheduler.check_refresh_not_in_progress() by injecting
job_tracker DIRECTLY into RefreshScheduler's constructor, bypassing
GlobalReposLifecycleManager entirely -- it could not, and did not, catch
this wiring gap. These tests construct GlobalReposLifecycleManager itself
(the real production construction path) and prove the tracker instance
actually reaches the scheduler, mirroring the precedent set by
test_global_repos_lifecycle_golden_repo_metadata_wiring_1390.py for the
golden_repo_metadata_backend forwarding bug.
"""

import sqlite3

import pytest

from code_indexer.server.lifecycle.global_repos_lifecycle import (
    GlobalReposLifecycleManager,
)
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
def real_job_tracker(tmp_path):
    """Real JobTracker backed by a real SQLite background_jobs table.

    Anti-mock rule: this is the same primitive _execute_refresh() registers
    itself into in production (Bug #935), and the same one
    activated_repo_manager's activation-clone path checks via
    check_refresh_not_in_progress() (Bug #1393).
    """
    db_path = str(tmp_path / "tracker.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_BACKGROUND_JOBS_DDL)
        conn.commit()
    finally:
        conn.close()
    return JobTracker(db_path)


def test_lifecycle_manager_forwards_job_tracker_to_refresh_scheduler(
    tmp_path, real_job_tracker
):
    """GlobalReposLifecycleManager's own job_tracker param must reach the
    RefreshScheduler it constructs -- the exact instance, not merely "a"
    tracker."""
    lifecycle = GlobalReposLifecycleManager(
        golden_repos_dir=str(tmp_path / "golden-repos"),
        job_tracker=real_job_tracker,
    )

    assert lifecycle.refresh_scheduler._job_tracker is real_job_tracker


def test_lifecycle_manager_without_job_tracker_leaves_scheduler_none(tmp_path):
    """CLI/solo mode (no job_tracker wired) must stay a safe no-op -- the
    RefreshScheduler must not receive a stray tracker from nowhere."""
    lifecycle = GlobalReposLifecycleManager(
        golden_repos_dir=str(tmp_path / "golden-repos"),
        job_tracker=None,
    )

    assert lifecycle.refresh_scheduler._job_tracker is None


def test_concurrent_activation_rejected_when_refresh_already_in_progress(
    tmp_path, real_job_tracker
):
    """End-to-end reproduction of the actual production race (refresh
    already running, then a concurrent same-repo activation attempt),
    driven ENTIRELY through GlobalReposLifecycleManager's real construction
    path -- exactly how startup/lifespan.py builds it. This is the
    regression test that should have caught the original wiring gap: it is
    NOT satisfiable via direct injection into RefreshScheduler alone.
    """
    lifecycle = GlobalReposLifecycleManager(
        golden_repos_dir=str(tmp_path / "golden-repos"),
        job_tracker=real_job_tracker,
    )

    # Simulate an ALREADY in-flight global_repo_refresh for "evolution",
    # registered exactly like _execute_refresh() registers itself (Bug #935):
    # repo_alias is the full "-global" suffixed alias.
    real_job_tracker.register_job(
        "refresh-evolution-global",
        operation_type="global_repo_refresh",
        username="system",
        repo_alias="evolution-global",
    )
    real_job_tracker.update_status("refresh-evolution-global", status="running")

    # A concurrent activation attempt for the SAME golden repo must now be
    # rejected via check_refresh_not_in_progress -- reached through the
    # manager's own refresh_scheduler, not a test-only injected one.
    with pytest.raises(DuplicateJobError) as exc_info:
        lifecycle.refresh_scheduler.check_refresh_not_in_progress("evolution")

    assert exc_info.value.existing_job_id == "refresh-evolution-global"
