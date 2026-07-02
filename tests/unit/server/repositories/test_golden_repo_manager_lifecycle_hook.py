"""
Tests for Story #876 Phase B-1 Deliverable 4 — cluster-atomic lifecycle
registration hook in GoldenRepoManager.

Hook contract (exercised as a testable helper method
GoldenRepoManager._register_lifecycle_after_registration):

  1. Call JobTracker.register_job_if_no_conflict(operation_type=
     "lifecycle_registration", repo_alias=<alias>, ...) as a single atomic
     INSERT guarded by the idx_active_job_per_repo partial unique index.
  2. On DuplicateJobError, log INFO and return silently — never instantiate
     LifecycleBatchRunner.
  3. On successful claim, construct LifecycleBatchRunner with golden_repos_dir
     plus the four injected collaborators (job_tracker, refresh_scheduler,
     debouncer, claude_cli_invoker), then call
     runner.run([alias], parent_job_id=<registered-job-id>).
  4. If runner.run raises, call JobTracker.fail_job(job_id, error=str(e))
     but do NOT re-raise — sidecar discipline keeps the outer registration
     worker alive so the stub .md is still visible to subsequent processes.
  5. If any of job_tracker / lifecycle_invoker / lifecycle_debouncer /
     _refresh_scheduler is None, skip entirely: log a WARNING and return
     without touching the DB or constructing a runner.

Design rationale (why a dedicated helper method):
  The hook fires deep inside `add_golden_repo`'s `background_worker` closure,
  after clone + index + meta_description_hook.  Testing it through
  `add_golden_repo` would force mocking SUT-internal methods
  (_clone_repository, _execute_post_clone_workflow, ...), which is an
  anti-pattern.  Extracting the hook body to a dedicated private method
  gives us a clean unit-test seam with a real contract (alias + username
  in -> DB rows + runner invocation out) while `background_worker` simply
  invokes the helper with a single `try/except Exception` wrapper,
  preserving sidecar discipline.

Collaboration boundary:
  * job_tracker is a REAL JobTracker pointed at a temp SQLite DB that
    mirrors production schema (background_jobs table + idx_active_job_per_repo
    partial unique index).  No mocking of SUT internals.
  * lifecycle_invoker / lifecycle_debouncer / _refresh_scheduler are
    MagicMock stand-ins for external collaborators the SUT does not own.
  * LifecycleBatchRunner is patched at its use site inside golden_repo_manager
    per-test via a single @patch decorator — the same pattern approved for
    D2/D3 in test_dependency_map_lifecycle_gate.py.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.job_tracker import JobTracker


# Use site of LifecycleBatchRunner inside golden_repo_manager (D4 wiring).
_PATCH_RUNNER = (
    "code_indexer.server.repositories.golden_repo_manager.LifecycleBatchRunner"
)


# ---------------------------------------------------------------------------
# DB fixture — background_jobs schema + idx_active_job_per_repo partial unique
# index, mirroring D2/D3's approved pattern in
# tests/unit/server/services/dep_map_tracking/conftest.py.
# ---------------------------------------------------------------------------


@pytest.fixture
def atomic_db_path(tmp_path):
    db = tmp_path / "test_reg_hook.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS background_jobs (
            job_id TEXT PRIMARY KEY NOT NULL,
            operation_type TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            result TEXT,
            error TEXT,
            progress INTEGER NOT NULL DEFAULT 0,
            username TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            cancelled INTEGER NOT NULL DEFAULT 0,
            repo_alias TEXT,
            resolution_attempts INTEGER NOT NULL DEFAULT 0,
            claude_actions TEXT,
            failure_reason TEXT,
            extended_error TEXT,
            language_resolution_status TEXT,
            progress_info TEXT,
            metadata TEXT
        )"""
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_job_per_repo
            ON background_jobs(operation_type, repo_alias)
            WHERE status IN ('pending', 'running')
              AND repo_alias IS NOT NULL
            """
        )
        conn.commit()
    return str(db)


@pytest.fixture
def real_job_tracker(atomic_db_path):
    return JobTracker(atomic_db_path)


def _fetch_rows_for_alias(db_path, alias):
    """Return list of (job_id, status, error) for lifecycle_registration rows."""
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(
            "SELECT job_id, status, error FROM background_jobs "
            "WHERE operation_type = 'lifecycle_registration' AND repo_alias = ?",
            (alias,),
        ).fetchall()


# ---------------------------------------------------------------------------
# Manager fixture — real GoldenRepoManager with NO attributes touched.
# Each test either wires the lifecycle deps (happy/duplicate/runner-raises)
# or leaves them None (skip-when-absent) to exercise the unwired branch.
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(tmp_path):
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    data_dir = tmp_path / "server-data"
    data_dir.mkdir()
    return GoldenRepoManager(data_dir=str(data_dir))


@pytest.fixture
def wired_manager(manager, real_job_tracker):
    """
    Manager with all four lifecycle collaborators wired.  Returns
    (manager, collaborators) so tests can assert on the wired objects.
    """
    invoker = MagicMock()
    debouncer = MagicMock()
    refresh_scheduler = MagicMock()
    manager.job_tracker = real_job_tracker
    manager.lifecycle_invoker = invoker
    manager.lifecycle_debouncer = debouncer
    manager._refresh_scheduler = refresh_scheduler
    return (
        manager,
        {
            "job_tracker": real_job_tracker,
            "invoker": invoker,
            "debouncer": debouncer,
            "refresh_scheduler": refresh_scheduler,
        },
    )


# ---------------------------------------------------------------------------
# Tests — D4 hook contract, exercised directly against the helper method
# ---------------------------------------------------------------------------


@patch(_PATCH_RUNNER)
def test_registers_atomic_job_and_invokes_runner_on_happy_path(
    runner_cls, wired_manager, atomic_db_path
):
    """
    Happy path: atomic INSERT succeeds, LifecycleBatchRunner is constructed
    with all four wired collaborators, and run(...) is called with the alias
    plus the parent_job_id returned by register_job_if_no_conflict.
    """
    mgr, collab = wired_manager
    alias = "my-repo"
    runner_cls.return_value.run = MagicMock()

    mgr._register_lifecycle_after_registration(alias, submitter_username="admin")

    # 1. DB row exists for (operation_type=lifecycle_registration, repo_alias=alias).
    rows = _fetch_rows_for_alias(atomic_db_path, alias)
    assert len(rows) == 1, f"expected one lifecycle_registration row, got {rows}"
    job_id, status, error = rows[0]
    assert job_id, "job_id must be non-empty"
    assert error is None

    # 2. Runner was constructed with the wired collaborators.
    runner_cls.assert_called_once()
    _args, ctor_kwargs = runner_cls.call_args
    assert ctor_kwargs["job_tracker"] is collab["job_tracker"]
    assert ctor_kwargs["refresh_scheduler"] is collab["refresh_scheduler"]
    assert ctor_kwargs["debouncer"] is collab["debouncer"]
    assert ctor_kwargs["claude_cli_invoker"] is collab["invoker"]

    # 3. runner.run was called with [alias] and parent_job_id == registered job_id.
    runner_cls.return_value.run.assert_called_once()
    run_args, run_kwargs = runner_cls.return_value.run.call_args
    aliases = run_args[0] if run_args else run_kwargs["repo_aliases"]
    assert aliases == [alias]
    assert run_kwargs["parent_job_id"] == job_id


@patch(_PATCH_RUNNER)
def test_duplicate_job_conflict_is_swallowed_and_runner_is_not_constructed(
    runner_cls, wired_manager, real_job_tracker, atomic_db_path
):
    """
    Duplicate-claim path: when another cluster node already holds the
    (lifecycle_registration, alias) row, the atomic INSERT raises
    DuplicateJobError inside JobTracker.  The SUT must catch it, log INFO,
    and return.  LifecycleBatchRunner must not be constructed.
    """
    mgr, _collab = wired_manager
    alias = "contested-repo"

    # Seed a conflicting pending row so the SUT's INSERT hits the partial
    # unique index and raises DuplicateJobError internally.
    real_job_tracker.register_job_if_no_conflict(
        job_id="preexisting-lifecycle",
        operation_type="lifecycle_registration",
        username="system",
        repo_alias=alias,
    )

    mgr._register_lifecycle_after_registration(alias, submitter_username="admin")

    # No runner constructed -> DuplicateJobError was swallowed silently.
    runner_cls.assert_not_called()

    # Only the pre-seeded row exists; the SUT did not insert a second row.
    rows = _fetch_rows_for_alias(atomic_db_path, alias)
    assert len(rows) == 1
    job_id, status, _error = rows[0]
    assert job_id == "preexisting-lifecycle"
    assert status == "pending"


@patch(_PATCH_RUNNER)
def test_runner_exception_triggers_fail_job_without_reraising(
    runner_cls, wired_manager, atomic_db_path
):
    """
    Sidecar-discipline path: runner.run raises a synthetic error.  The SUT
    must mark the tracked job as failed (fail_job) and swallow the exception
    so the outer registration worker stays alive.  The DB row must reflect
    status='failed' with the error string captured.
    """
    mgr, _collab = wired_manager
    alias = "boom-repo"
    synthetic_error = RuntimeError("synthetic fleet scan error")
    runner_cls.return_value.run = MagicMock(side_effect=synthetic_error)

    # No exception propagates to the caller.
    mgr._register_lifecycle_after_registration(alias, submitter_username="admin")

    rows = _fetch_rows_for_alias(atomic_db_path, alias)
    assert len(rows) == 1
    _job_id, status, error = rows[0]
    assert status == "failed", f"expected status=failed, got {status}"
    assert error is not None and "synthetic fleet scan error" in error


@patch(_PATCH_RUNNER)
def test_raises_when_mandatory_lifecycle_dependency_is_none(
    runner_cls, manager, atomic_db_path
):
    """
    Hard-error contract (replaces staged-rollout skip guard, Story #876):
    when wiring is incomplete (job_tracker is None here), the SUT raises
    RuntimeError listing the missing collaborators.  LifecycleBatchRunner
    must not be constructed and the DB must remain untouched.
    """
    # Deliberately leave job_tracker unset (None) while the three other
    # lifecycle deps are populated.  All four must be non-None for the
    # hook to proceed.
    manager.job_tracker = None
    manager.lifecycle_invoker = MagicMock()
    manager.lifecycle_debouncer = MagicMock()
    manager._refresh_scheduler = MagicMock()

    with pytest.raises(RuntimeError, match="not wired"):
        manager._register_lifecycle_after_registration(
            "partial-wired-repo", submitter_username="admin"
        )

    runner_cls.assert_not_called()
    # DB was never touched — no lifecycle_registration rows exist.
    with closing(sqlite3.connect(atomic_db_path)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM background_jobs "
            "WHERE operation_type = 'lifecycle_registration'"
        ).fetchone()[0]
    assert count == 0
