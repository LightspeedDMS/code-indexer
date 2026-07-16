"""Tests for xray job exclusion from dashboard recent-jobs display.

xray_search and xray_explore jobs should be invisible on the dashboard
but still tracked by JobTracker so cancel still works.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.storage.database_manager import DatabaseSchema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_path(temp_dir):
    path = str(Path(temp_dir) / "test_dashboard_exclusion.db")
    DatabaseSchema(path).initialize_database()
    return path


@pytest.fixture
def tracker(db_path) -> JobTracker:
    return JobTracker(db_path)


# ---------------------------------------------------------------------------
# Test 1: active (in-memory) xray_search job is excluded, non-xray kept
# ---------------------------------------------------------------------------


def test_exclude_active_xray_search_job_keeps_non_xray(tracker: JobTracker):
    """Active xray_search job excluded from get_recent_jobs when filtered;
    non-xray job still appears."""
    # Register one xray_search and one non-xray job
    tracker.register_job(
        job_id="xray-job-001",
        operation_type="xray_search",
        username="alice",
        repo_alias=None,
    )
    tracker.register_job(
        job_id="index-job-002",
        operation_type="index_repo",
        username="alice",
        repo_alias="myrepo",
    )

    result = tracker.get_recent_jobs(
        limit=20,
        time_filter="all",
        exclude_operation_types=["xray_search"],
    )

    job_ids = [j["job_id"] for j in result]
    assert "xray-job-001" not in job_ids, "xray_search job should be excluded"
    assert "index-job-002" in job_ids, "non-xray job should remain"


# ---------------------------------------------------------------------------
# Test 2: SQLite historical path excludes both xray_search and xray_explore
# ---------------------------------------------------------------------------


def test_exclude_historical_xray_jobs_sqlite(tracker: JobTracker):
    """Historical jobs of type xray_search and xray_explore are excluded
    from SQLite path results when exclude_operation_types is set."""
    # Register jobs and move them out of active (simulate completed)
    tracker.register_job(
        job_id="xray-search-hist-001",
        operation_type="xray_search",
        username="alice",
        repo_alias=None,
    )
    tracker.register_job(
        job_id="xray-explore-hist-002",
        operation_type="xray_explore",
        username="alice",
        repo_alias=None,
    )
    tracker.register_job(
        job_id="dep-map-hist-003",
        operation_type="dep_map_analysis",
        username="alice",
        repo_alias="myrepo",
    )

    # Complete all jobs so they fall out of active and into historical
    for jid in ("xray-search-hist-001", "xray-explore-hist-002", "dep-map-hist-003"):
        tracker.update_status(jid, status="running")
        tracker.complete_job(jid, result={})

    result = tracker.get_recent_jobs(
        limit=20,
        time_filter="all",
        exclude_operation_types=["xray_search", "xray_explore"],
    )

    job_ids = [j["job_id"] for j in result]
    assert "xray-search-hist-001" not in job_ids, "xray_search should be excluded"
    assert "xray-explore-hist-002" not in job_ids, "xray_explore should be excluded"
    assert "dep-map-hist-003" in job_ids, "dep_map_analysis should remain"


# ---------------------------------------------------------------------------
# Test 3: Default (None) returns all types — no regression
# ---------------------------------------------------------------------------


def test_no_exclusion_by_default_returns_all_types(tracker: JobTracker):
    """When exclude_operation_types is None (default), all job types appear."""
    tracker.register_job(
        job_id="xray-job-default-001",
        operation_type="xray_search",
        username="alice",
        repo_alias=None,
    )
    tracker.register_job(
        job_id="index-job-default-002",
        operation_type="index_repo",
        username="alice",
        repo_alias="myrepo",
    )

    # Complete them so they're in history too
    for jid in ("xray-job-default-001", "index-job-default-002"):
        tracker.update_status(jid, status="running")
        tracker.complete_job(jid, result={})

    result = tracker.get_recent_jobs(limit=20, time_filter="all")  # default None

    job_ids = [j["job_id"] for j in result]
    assert "xray-job-default-001" in job_ids, (
        "xray_search should appear when no exclusion"
    )
    assert "index-job-default-002" in job_ids, (
        "index_repo should appear when no exclusion"
    )


# ---------------------------------------------------------------------------
# Test 4: PG backend path also filters
# ---------------------------------------------------------------------------


def _make_job_dict(
    job_id: str,
    operation_type: str,
    repo_alias: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a minimal job dict as returned by BackgroundJobsBackend.list_jobs."""
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "job_id": job_id,
        "operation_type": operation_type,
        "status": "completed",
        "username": "alice",
        "repo_alias": repo_alias,
        "progress": 100,
        "progress_info": None,
        "metadata": None,
        "created_at": now_iso,
        "started_at": now_iso,
        "completed_at": now_iso,
        "error": None,
        "result": None,
    }


def test_pg_backend_path_excludes_xray_jobs(temp_dir: str):
    """When a PG backend is set, get_recent_jobs passes exclude_operation_types
    to the backend so SQL-level exclusion is applied before LIMIT.
    The mock simulates the fixed backend that filters at the DB level."""
    all_jobs = [
        _make_job_dict("xray-pg-001", "xray_search"),
        _make_job_dict("xray-pg-002", "xray_explore"),
        _make_job_dict("index-pg-003", "index_repo", repo_alias="myrepo"),
    ]

    def _list_jobs_with_sql_exclusion(**kwargs: Any) -> list:
        excl = set(kwargs.get("exclude_operation_types") or [])
        return [j for j in all_jobs if j.get("operation_type") not in excl]

    mock_backend = MagicMock()
    mock_backend.list_jobs.side_effect = _list_jobs_with_sql_exclusion

    db_path = str(Path(temp_dir) / "pg_test.db")
    DatabaseSchema(db_path).initialize_database()

    tracker = JobTracker(db_path=db_path, storage_backend=mock_backend)

    result = tracker.get_recent_jobs(
        limit=20,
        time_filter="all",
        exclude_operation_types=["xray_search", "xray_explore"],
    )

    job_ids = [j["job_id"] for j in result]
    assert "xray-pg-001" not in job_ids, "xray_search should be excluded (PG path)"
    assert "xray-pg-002" not in job_ids, "xray_explore should be excluded (PG path)"
    assert "index-pg-003" in job_ids, "index_repo should remain (PG path)"


# ---------------------------------------------------------------------------
# Test 5: Dashboard _get_recent_jobs passes exclude_operation_types
# ---------------------------------------------------------------------------


def test_dashboard_passes_exclusion_to_job_tracker():
    """DashboardService._get_recent_jobs passes exclude_operation_types
    ['xray_search', 'xray_explore'] when calling job_tracker.get_recent_jobs."""
    from code_indexer.server.services.dashboard_service import DashboardService

    mock_tracker = MagicMock()
    mock_tracker.get_recent_jobs.return_value = []

    mock_bjm = MagicMock()

    dashboard = DashboardService()

    with (
        patch.object(dashboard, "_get_job_tracker", return_value=mock_tracker),
        patch.object(dashboard, "_get_background_job_manager", return_value=mock_bjm),
    ):
        dashboard._get_recent_jobs(username="alice", time_filter="24h")

    mock_tracker.get_recent_jobs.assert_called_once()
    call_kwargs = mock_tracker.get_recent_jobs.call_args

    # Accept both positional and keyword argument forms
    exclude = call_kwargs.kwargs.get("exclude_operation_types") or (
        call_kwargs.args[2] if len(call_kwargs.args) > 2 else None
    )
    assert exclude is not None, "exclude_operation_types must be passed"
    assert set(exclude) == {
        "xray_search",
        "xray_explore",
        "xray_search_batch",
        # Story #1400 Phase 9: async-hybrid temporal query jobs are also
        # dashboard-hidden, sharing this same exclusion list.
        "temporal_query",
    }, (
        f"Expected xray_search, xray_explore, and xray_search_batch in exclusion list, got: {exclude}"
    )


# ---------------------------------------------------------------------------
# Test 6 — PG backend receives exclude_operation_types kwarg
# ---------------------------------------------------------------------------


def test_pg_backend_list_jobs_receives_exclude_kwarg(temp_dir: str):
    """The PG backend's list_jobs must be called with exclude_operation_types kwarg.
    This is the test that would have caught the original bug: LIMIT fires in SQL
    before Python exclusion, starving the dashboard when xray jobs dominate."""
    mock_backend = MagicMock()
    mock_backend.list_jobs.return_value = []

    db_path = str(Path(temp_dir) / "pg_kwarg_test.db")
    DatabaseSchema(db_path).initialize_database()

    tracker = JobTracker(db_path=db_path, storage_backend=mock_backend)

    tracker.get_recent_jobs(
        limit=20,
        time_filter="all",
        exclude_operation_types=["xray_search", "xray_explore"],
    )

    mock_backend.list_jobs.assert_called_once()
    call_kwargs = mock_backend.list_jobs.call_args.kwargs
    assert "exclude_operation_types" in call_kwargs, (
        "list_jobs must receive exclude_operation_types kwarg so SQL LIMIT fires "
        "AFTER exclusion, not before"
    )
    assert call_kwargs["exclude_operation_types"] == ["xray_search", "xray_explore"], (
        f"Wrong value passed: {call_kwargs['exclude_operation_types']}"
    )


# ---------------------------------------------------------------------------
# Test 7 — Dashboard does not under-fill when PG is xray-heavy
# ---------------------------------------------------------------------------


def test_pg_backend_dashboard_not_underfilled_when_xray_heavy(temp_dir: str):
    """When the PG backend correctly excludes xray rows at the SQL level,
    get_recent_jobs returns all available non-xray entries without being starved
    by the LIMIT firing on xray-dominated rows."""
    # Simulate a fixed PG backend that already filtered xray at SQL level
    non_xray_jobs = [
        _make_job_dict(f"index-pg-{i:03d}", "index_repo", repo_alias="myrepo")
        for i in range(15)
    ]
    mock_backend = MagicMock()
    # Correctly-fixed backend returns only non-xray rows
    mock_backend.list_jobs.return_value = non_xray_jobs

    db_path = str(Path(temp_dir) / "pg_underfill_test.db")
    DatabaseSchema(db_path).initialize_database()

    tracker = JobTracker(db_path=db_path, storage_backend=mock_backend)

    # Register 5 active non-xray jobs
    for i in range(5):
        tracker.register_job(
            job_id=f"active-index-{i:03d}",
            operation_type="index_repo",
            username="alice",
            repo_alias="myrepo",
        )

    result = tracker.get_recent_jobs(
        limit=20,
        time_filter="all",
        exclude_operation_types=["xray_search", "xray_explore"],
    )

    # Should have 5 active + 15 historical = 20 non-xray entries
    assert len(result) == 20, (
        f"Expected 20 non-xray entries, got {len(result)} — dashboard is under-filled"
    )
    # Zero xray entries
    xray_entries = [
        j for j in result if j.get("operation_type") in ("xray_search", "xray_explore")
    ]
    assert len(xray_entries) == 0, (
        f"No xray entries should appear but got: {[j['job_id'] for j in xray_entries]}"
    )


# ---------------------------------------------------------------------------
# Test 8 — Empty exclude list behaves like None
# ---------------------------------------------------------------------------


def test_empty_exclude_list_behaves_like_none(tracker: JobTracker):
    """Passing exclude_operation_types=[] returns the same results as None —
    no ValueError raised, all operation types returned."""
    tracker.register_job(
        job_id="xray-empty-001",
        operation_type="xray_search",
        username="alice",
        repo_alias=None,
    )
    tracker.register_job(
        job_id="index-empty-002",
        operation_type="index_repo",
        username="alice",
        repo_alias="myrepo",
    )
    for jid in ("xray-empty-001", "index-empty-002"):
        tracker.update_status(jid, status="running")
        tracker.complete_job(jid, result={})

    result_empty = tracker.get_recent_jobs(
        limit=20, time_filter="all", exclude_operation_types=[]
    )
    result_none = tracker.get_recent_jobs(
        limit=20, time_filter="all", exclude_operation_types=None
    )

    ids_empty = {j["job_id"] for j in result_empty}
    ids_none = {j["job_id"] for j in result_none}
    assert ids_empty == ids_none, (
        f"Empty list should behave like None. Empty: {ids_empty}, None: {ids_none}"
    )
    # Both should contain xray_search (no filtering applied)
    assert "xray-empty-001" in ids_empty, (
        "xray_search should appear when empty exclude list passed"
    )


# ---------------------------------------------------------------------------
# Test 9a — None operation_type job survives Python-side exclusion (PG path)
# ---------------------------------------------------------------------------


def test_none_operation_type_survives_exclusion_pg_path(temp_dir: str):
    """A job dict with operation_type=None (as may arrive from the PG backend
    for legacy rows) must NOT be dropped by the Python-side exclusion filter.
    `None in excl_set` evaluates to False, so the row is correctly kept.
    This validates the Python-side None handling for the PG backend path."""
    null_job = _make_job_dict("null-op-type-001", "some_type")
    null_job["operation_type"] = None  # simulate a NULL-origin row from the DB

    mock_backend = MagicMock()
    mock_backend.list_jobs.return_value = [null_job]

    db_path = str(Path(temp_dir) / "null_op_type_test.db")
    DatabaseSchema(db_path).initialize_database()

    tracker = JobTracker(db_path=db_path, storage_backend=mock_backend)

    result = tracker.get_recent_jobs(
        limit=20,
        time_filter="all",
        exclude_operation_types=["xray_search"],
    )

    job_ids = [j["job_id"] for j in result]
    assert "null-op-type-001" in job_ids, (
        "Job with operation_type=None must survive exclusion filter — "
        "None is not in the exclusion set and must be kept"
    )


# ---------------------------------------------------------------------------
# Test 9b — NULL operation_type SQL guard in SQLite _query_sqlite_recent_jobs
# ---------------------------------------------------------------------------


def test_null_operation_type_sql_guard_sqlite(temp_dir: str):
    """Validates that the SQLite exclusion WHERE clause uses the NULL guard:
    `(operation_type IS NULL OR operation_type NOT IN (...))`.
    Without this guard, `NULL NOT IN (...)` evaluates to UNKNOWN in SQL
    3-value logic and silently drops NULL-operation_type rows.

    The application schema enforces NOT NULL, so this test creates a minimal
    table without that constraint to insert a NULL and exercise the guard."""
    import sqlite3

    db_path = str(Path(temp_dir) / "null_sql_guard_test.db")

    # Create a minimal background_jobs table without NOT NULL on operation_type
    # so we can insert NULL and exercise the SQL guard in the tracker query
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE background_jobs (
               job_id TEXT PRIMARY KEY NOT NULL,
               operation_type TEXT,
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
               executing_node TEXT,
               claimed_at TEXT,
               progress_info TEXT,
               metadata TEXT,
               current_phase TEXT,
               phase_detail TEXT,
               actor_username TEXT
            )"""
        )
        conn.execute(
            """INSERT INTO background_jobs
               (job_id, operation_type, status, username, created_at, progress)
               VALUES (?, NULL, 'completed', 'alice', datetime('now'), 100)""",
            ("null-sql-guard-001",),
        )
        conn.execute(
            """INSERT INTO background_jobs
               (job_id, operation_type, status, username, created_at, progress)
               VALUES (?, 'index_repo', 'completed', 'alice', datetime('now'), 100)""",
            ("normal-job-002",),
        )

    tracker = JobTracker(db_path=db_path)

    result = tracker.get_recent_jobs(
        limit=20,
        time_filter="all",
        exclude_operation_types=["xray_search"],
    )

    job_ids = [j["job_id"] for j in result]
    assert "null-sql-guard-001" in job_ids, (
        "Row with NULL operation_type must survive exclusion filter — "
        "SQL NULL guard `(operation_type IS NULL OR operation_type NOT IN (...))` "
        "is required; plain `NULL NOT IN (...)` evaluates to UNKNOWN and drops the row"
    )
    assert "normal-job-002" in job_ids, "Normal job must also be present"


# ---------------------------------------------------------------------------
# Test 10 — Active xray_search_batch job is excluded, non-xray kept
# ---------------------------------------------------------------------------


def test_exclude_active_xray_search_batch_job_keeps_non_xray(tracker: JobTracker):
    """Active xray_search_batch job excluded from get_recent_jobs when filtered;
    non-xray job still appears."""
    # Register one xray_search_batch and one non-xray job
    tracker.register_job(
        job_id="xray-batch-job-001",
        operation_type="xray_search_batch",
        username="alice",
        repo_alias=None,
    )
    tracker.register_job(
        job_id="index-job-010",
        operation_type="index_repo",
        username="alice",
        repo_alias="myrepo",
    )

    result = tracker.get_recent_jobs(
        limit=20,
        time_filter="all",
        exclude_operation_types=["xray_search_batch"],
    )

    job_ids = [j["job_id"] for j in result]
    assert "xray-batch-job-001" not in job_ids, (
        "xray_search_batch job should be excluded"
    )
    assert "index-job-010" in job_ids, "non-xray job should remain"
