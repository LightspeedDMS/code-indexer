"""
Tests for Bug #1063 Part 4: Dashboard bounded fetch (no 10000-row merge).

Problem: list_jobs() and get_jobs_for_display() fetch up to 10000 rows from
SQLite for client-side pagination.  With a large job history this:
  - Reads 10000 rows from disk for every dashboard refresh
  - Holds them all in memory for a client-side sort + slice
  - Returns only page_size (50) rows to the client

Fix:
  1. list_jobs():  pass limit=page_size + enough to merge in-memory active jobs,
     NOT a flat 10000.  Hard-cap page_size at MAX_PAGE_SIZE=50.
  2. get_jobs_for_display():  pass limit+offset down to list_jobs_filtered()
     so the DB query itself is bounded.
  3. total_count is still returned accurately (from the COUNT(*) query) so
     the UI can show "N more jobs" in the footer.
  4. page_size > 50 is clamped silently to 50.

MAX_PAGE_SIZE constant is expected at:
  code_indexer.server.repositories.background_jobs.MAX_PAGE_SIZE == 50
"""

import logging
import threading
import time
from typing import Any, Dict
from unittest.mock import patch, MagicMock
import pytest

from code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    JobStatus,
)
from code_indexer.server.utils.config_manager import BackgroundJobsConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MAX_PAGE_SIZE = 50  # matches the expected constant in production code


def _make_manager(tmp_path, request) -> BackgroundJobManager:
    """Create a schema-initialized, auto-shutdown BackgroundJobManager.

    Bug #1447: every manager built here spawns real daemon worker-pool
    threads at __init__ time (Bug #1063 Part 3) and, since use_sqlite=True,
    needs its background_jobs table to exist BEFORE any submitted job can
    terminate -- otherwise the terminal-status persist fails with
    "no such table: background_jobs" (fail-open logging.error in
    BackgroundJobManager._persist_job_to_sqlite). Before this fix, neither
    the schema was initialized nor was the manager ever shut down, leaking
    live daemon threads and log noise into whatever unrelated test ran next
    in the same single-process pytest chunk (GitHub issue #1447).
    """
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = str(tmp_path / "jobs.db")
    DatabaseSchema(db_path).initialize_database()
    mgr = BackgroundJobManager(
        background_jobs_config=BackgroundJobsConfig(max_concurrent_background_jobs=2),
        db_path=db_path,
        use_sqlite=True,
    )
    request.addfinalizer(mgr.shutdown)
    return mgr


# ===========================================================================
# Part 4A: MAX_PAGE_SIZE constant exposed
# ===========================================================================


class TestMaxPageSizeConstant:
    """MAX_PAGE_SIZE = 50 must be importable from background_jobs."""

    def test_max_page_size_constant_exists(self):
        from code_indexer.server.repositories import background_jobs

        assert hasattr(background_jobs, "MAX_PAGE_SIZE"), (
            "background_jobs module must expose MAX_PAGE_SIZE constant"
        )

    def test_max_page_size_is_50(self):
        from code_indexer.server.repositories.background_jobs import MAX_PAGE_SIZE

        assert MAX_PAGE_SIZE == 50, f"MAX_PAGE_SIZE must be 50, got {MAX_PAGE_SIZE}"


# ===========================================================================
# Part 4B: list_jobs() hard-caps page_size at 50
# ===========================================================================


class TestListJobsPageSizeCap:
    """list_jobs() must silently cap page_size at MAX_PAGE_SIZE."""

    def test_page_size_above_50_is_capped(self, tmp_path, request):
        """
        Requesting page_size=200 must return at most 50 results.
        The DB query must have been issued with LIMIT=50 not LIMIT=200.
        """
        mgr = _make_manager(tmp_path, request)

        if mgr._sqlite_backend is None:
            pytest.skip("No SQLite backend available")

        # Spy on list_jobs to capture the limit kwarg
        original_list = mgr._sqlite_backend.list_jobs
        captured_limits = []

        def spy_list_jobs(*args, **kwargs):
            captured_limits.append(
                kwargs.get("limit", args[3] if len(args) > 3 else None)
            )
            return original_list(*args, **kwargs)

        with patch.object(mgr._sqlite_backend, "list_jobs", side_effect=spy_list_jobs):
            result = mgr.list_jobs(
                username="admin",
                limit=200,  # requesting 200 — must be capped to 50
                offset=0,
                is_admin=True,
            )

        # The result's "limit" field must be capped at 50
        assert result.get("limit", 200) <= MAX_PAGE_SIZE, (
            f"list_jobs() returned limit={result.get('limit')} but must cap at {MAX_PAGE_SIZE}. "
            f"DB queries used limits: {captured_limits}"
        )

    def test_page_size_at_50_is_accepted(self, tmp_path, request):
        """Requesting exactly 50 must be allowed unchanged."""
        mgr = _make_manager(tmp_path, request)

        result = mgr.list_jobs(
            username="admin",
            limit=50,
            offset=0,
            is_admin=True,
        )

        assert result.get("limit", 0) == 50

    def test_page_size_below_50_is_accepted(self, tmp_path, request):
        """Requesting fewer than 50 results must be allowed."""
        mgr = _make_manager(tmp_path, request)

        result = mgr.list_jobs(
            username="admin",
            limit=10,
            offset=0,
            is_admin=True,
        )

        assert result.get("limit", 0) == 10


# ===========================================================================
# Part 4C: list_jobs() does NOT fetch 10000 rows
# ===========================================================================


class TestListJobsNoBulkFetch:
    """list_jobs() must not issue a DB query with limit=10000."""

    def test_db_limit_matches_requested_page_size(self, tmp_path, request):
        """
        When page_size=20 is requested, the DB query limit must be close to 20,
        NOT the old 10000 sentinel.  The exact limit may be slightly higher
        (e.g. page_size + len(active_jobs)) but must be < 10000.
        """
        mgr = _make_manager(tmp_path, request)

        if mgr._sqlite_backend is None:
            pytest.skip("No SQLite backend available")

        captured_limits = []
        original_list = mgr._sqlite_backend.list_jobs

        def spy_list_jobs(*args, **kwargs):
            lim = kwargs.get("limit")
            if lim is None and len(args) > 3:
                lim = args[3]
            if lim is not None:
                captured_limits.append(lim)
            return original_list(*args, **kwargs)

        with patch.object(mgr._sqlite_backend, "list_jobs", side_effect=spy_list_jobs):
            mgr.list_jobs(
                username="admin",
                limit=20,
                offset=0,
                is_admin=True,
            )

        # All DB fetches must be bounded — none should be 10000
        unbounded = [lim for lim in captured_limits if lim >= 10000]
        assert not unbounded, (
            f"list_jobs() issued DB query(s) with limit >= 10000: {unbounded}. "
            f"The 10000-row bulk fetch must be replaced with page_size-bounded queries."
        )


# ===========================================================================
# Part 4D: get_jobs_for_display() passes limit to list_jobs_filtered
# ===========================================================================


class TestGetJobsForDisplayBoundedFetch:
    """get_jobs_for_display() must pass limit+offset to the DB, not fetch all rows."""

    def test_list_jobs_filtered_receives_limit(self, tmp_path, request):
        """
        get_jobs_for_display(page=1, page_size=10) must call list_jobs_filtered
        with limit=10 (or close to it), not with limit=None or limit=10000.
        """
        mgr = _make_manager(tmp_path, request)

        if mgr._sqlite_backend is None:
            pytest.skip("No SQLite backend available")

        captured_kwargs: list = []
        original = mgr._sqlite_backend.list_jobs_filtered

        def spy_filtered(*args, **kwargs):
            captured_kwargs.append(kwargs.copy())
            return original(*args, **kwargs)

        with patch.object(
            mgr._sqlite_backend, "list_jobs_filtered", side_effect=spy_filtered
        ):
            jobs, total, pages = mgr.get_jobs_for_display(
                page=1,
                page_size=10,
                is_admin=True,
            )

        # list_jobs_filtered must have been called
        assert len(captured_kwargs) >= 1, (
            "list_jobs_filtered was never called — something is wrong with the call path"
        )

        # The limit passed to the DB must be bounded (not None and not 10000)
        for kw in captured_kwargs:
            lim = kw.get("limit")
            assert lim is not None, (
                f"list_jobs_filtered was called without a limit: {kw}. "
                f"This would fetch ALL rows."
            )
            assert lim < 10000, (
                f"list_jobs_filtered was called with limit={lim} (>= 10000). "
                f"Expected page_size-bounded query."
            )

    def test_total_count_reflects_full_set_not_page(self, tmp_path, request):
        """
        total_count returned by get_jobs_for_display must reflect the full
        matching row count (from COUNT(*)), not just the rows on this page.
        This enables the "N more jobs" footer.
        """
        mgr = _make_manager(tmp_path, request)

        if mgr._sqlite_backend is None:
            pytest.skip("No SQLite backend available")

        # Mock list_jobs_filtered to simulate 100 total but only return 10 rows
        mock_rows = [
            {
                "job_id": f"job-{i}",
                "status": "completed",
                "operation_type": "test",
                "created_at": "2024-01-01",
                "started_at": None,
                "completed_at": None,
                "result": None,
                "error": None,
                "progress": 100,
                "username": "admin",
                "is_admin": False,
                "cancelled": False,
                "repo_alias": f"repo-{i}",
                "resolution_attempts": 0,
                "claude_actions": None,
                "failure_reason": None,
                "extended_error": None,
                "language_resolution_status": None,
                "current_phase": None,
                "phase_detail": None,
                "progress_info": None,
                "metadata": None,
                "executing_node": None,
                "claimed_at": None,
                "actor_username": "admin",
            }
            for i in range(10)
        ]

        with patch.object(
            mgr._sqlite_backend,
            "list_jobs_filtered",
            return_value=(mock_rows, 100),  # 100 total rows, 10 returned
        ):
            jobs, total_count, total_pages = mgr.get_jobs_for_display(
                page=1,
                page_size=10,
                is_admin=True,
            )

        # total_count must be 100 (from COUNT(*)), not 10 (page size)
        assert total_count == 100, (
            f"total_count={total_count} but expected 100 (full matching count). "
            f"The 'N more jobs' footer needs the full count."
        )

    def test_page_size_capped_at_50_in_display(self, tmp_path, request):
        """get_jobs_for_display with page_size > 50 must silently cap at 50."""
        mgr = _make_manager(tmp_path, request)

        if mgr._sqlite_backend is None:
            pytest.skip("No SQLite backend available")

        captured: list = []
        original = mgr._sqlite_backend.list_jobs_filtered

        def spy(*args, **kwargs):
            captured.append(kwargs.get("limit"))
            return original(*args, **kwargs)

        with patch.object(mgr._sqlite_backend, "list_jobs_filtered", side_effect=spy):
            mgr.get_jobs_for_display(
                page=1,
                page_size=200,  # requesting 200, must be capped
                is_admin=True,
            )

        # DB limit must be <= 50 (MAX_PAGE_SIZE)
        for lim in captured:
            if lim is not None:
                assert lim <= MAX_PAGE_SIZE, (
                    f"DB query issued with limit={lim} > MAX_PAGE_SIZE ({MAX_PAGE_SIZE}). "
                    f"page_size cap not enforced."
                )


# ===========================================================================
# Part 4F: get_jobs_for_display() pagination correctness for page > 1
# ===========================================================================


class TestPaginationCorrectness:
    """
    Reproduces the pagination bug from Bug #1063 review:
    3 active jobs + 120 historical jobs, page_size=50.
    Page 2 and page 3 must NOT re-show active jobs and must NOT drop any
    historical jobs.

    Strategy: use submit_job() with a threading.Event barrier to keep 3 jobs
    in RUNNING state (they stay in mgr.jobs as active), then mock
    list_jobs_filtered to supply historical rows from the DB side.
    """

    def _make_hist_row(self, job_id: str) -> Dict[str, Any]:
        return {
            "job_id": job_id,
            "status": "completed",
            "operation_type": "test_op",
            "created_at": "2024-01-01T00:00:00",
            "started_at": None,
            "completed_at": None,
            "result": None,
            "error": None,
            "progress": 100,
            "username": "admin",
            "is_admin": False,
            "cancelled": False,
            "repo_alias": f"repo-{job_id}",
            "resolution_attempts": 0,
            "claude_actions": None,
            "failure_reason": None,
            "extended_error": None,
            "language_resolution_status": None,
            "current_phase": None,
            "phase_detail": None,
            "progress_info": None,
            "metadata": None,
            "executing_node": None,
            "claimed_at": None,
            "actor_username": "admin",
        }

    def test_no_active_job_duplication_across_pages(self, tmp_path, request):
        """
        With 3 active (RUNNING) jobs + 120 historical jobs and page_size=50:
        - page 1: active-0, active-1, active-2, hist-000..hist-046 (50 total)
        - page 2: hist-047..hist-096 (50 total, NO active jobs)
        - page 3: hist-097..hist-119 (23 total, NO active jobs)
        Active jobs must appear exactly once across all pages (page 1 only).
        """
        # Bug #1447: schema must exist before jobs submitted below can
        # terminate, and the manager must be shut down so its worker
        # threads don't leak into later tests in this single-process chunk.
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = str(tmp_path / "jobs.db")
        DatabaseSchema(db_path).initialize_database()
        # Use a small pool (3 workers = 3 concurrent running jobs)
        mgr = BackgroundJobManager(
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=3
            ),
            db_path=db_path,
            use_sqlite=True,
        )
        request.addfinalizer(mgr.shutdown)

        # Build 120 historical rows for the mock
        n_historical = 120
        historical_ids = [f"hist-{i:03d}" for i in range(n_historical)]
        hist_rows = [self._make_hist_row(jid) for jid in historical_ids]

        # Submit 3 jobs that block until we release them
        barriers = []
        active_job_ids = []
        for i in range(3):
            barrier = threading.Event()
            barriers.append(barrier)

            def make_worker(b):
                def worker():
                    b.wait(timeout=10)
                    return {"success": True}

                return worker

            jid = mgr.submit_job(
                operation_type="test_op",
                func=make_worker(barrier),
                submitter_username="admin",
                is_admin=True,
            )
            active_job_ids.append(jid)

        # Wait until all 3 jobs are RUNNING (picked up by pool workers)
        import time as _time

        deadline = _time.monotonic() + 5.0
        while _time.monotonic() < deadline:
            with mgr._lock:
                running = [
                    j for j in mgr.jobs.values() if j.status == JobStatus.RUNNING
                ]
            if len(running) == 3:
                break
            _time.sleep(0.05)

        active_set = set(active_job_ids)

        def mock_list_jobs_filtered(
            status=None,
            operation_type=None,
            search_text=None,
            exclude_ids=None,
            username=None,
            limit=None,
            offset=None,
        ):
            rows = hist_rows
            if exclude_ids:
                rows = [r for r in rows if r["job_id"] not in exclude_ids]
            start = offset or 0
            end = start + (limit or len(rows))
            return rows[start:end], len(rows)

        seen_active: Dict[str, int] = {}
        seen_hist: Dict[str, int] = {}

        page_size = 50
        try:
            for page in range(1, 4):
                with patch.object(
                    mgr._sqlite_backend,
                    "list_jobs_filtered",
                    side_effect=mock_list_jobs_filtered,
                ):
                    jobs, total_count, total_pages = mgr.get_jobs_for_display(
                        page=page,
                        page_size=page_size,
                        is_admin=True,
                    )

                for j in jobs:
                    jid = j["job_id"]
                    if jid in active_set:
                        seen_active[jid] = seen_active.get(jid, 0) + 1
                    else:
                        seen_hist[jid] = seen_hist.get(jid, 0) + 1
        finally:
            # Release all blocking workers so threads can finish
            for b in barriers:
                b.set()

        # (a) Zero active-job duplication across pages
        duplicated = {k: v for k, v in seen_active.items() if v > 1}
        assert not duplicated, (
            f"Active jobs appeared on multiple pages (duplication): {duplicated}. "
            f"Active jobs must only appear on the first page where their global index falls."
        )

        # (b) All active jobs appeared exactly once (on page 1)
        assert set(seen_active.keys()) == active_set, (
            f"Not all active jobs appeared: seen={set(seen_active.keys())}, expected={active_set}"
        )

        # (c) No historical jobs dropped — all 120 must appear exactly once
        dropped = [jid for jid in historical_ids if seen_hist.get(jid, 0) == 0]
        assert not dropped, (
            f"{len(dropped)} historical jobs never appeared on any page: {dropped[:10]}..."
        )
        duplicated_hist = {k: v for k, v in seen_hist.items() if v > 1}
        assert not duplicated_hist, (
            f"Historical jobs appeared on multiple pages: {duplicated_hist}"
        )

    def test_total_count_correct_with_active_and_historical(self, tmp_path, request):
        """
        total_count must equal active_count + db_total (from COUNT(*)),
        not just the rows returned on this page.
        """
        # Bug #1447: this exact test (schema never initialized + manager
        # never shut down) was confirmed, via a live reproduction run, to be
        # the source of the "no such table: background_jobs" log lines and
        # the leaked bgm-worker-*/bgm-temporal-worker-* daemon threads that
        # made the unrelated sibling test
        # test_inline_repos_sync_job_params.py::test_submit_job_without_params_kwarg_succeeds
        # intermittently observe its own job stuck at "pending" under
        # full-chunk load (GitHub issue #1447).
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = str(tmp_path / "jobs.db")
        DatabaseSchema(db_path).initialize_database()
        mgr = BackgroundJobManager(
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=3
            ),
            db_path=db_path,
            use_sqlite=True,
        )
        request.addfinalizer(mgr.shutdown)

        # Submit 3 blocking jobs to have RUNNING active jobs
        barriers = []
        for i in range(3):
            b = threading.Event()
            barriers.append(b)

            def make_worker(ev):
                def worker():
                    ev.wait(timeout=10)
                    return {"success": True}

                return worker

            mgr.submit_job(
                operation_type="test_op",
                func=make_worker(b),
                submitter_username="admin",
                is_admin=True,
            )

        import time as _time

        deadline = _time.monotonic() + 5.0
        while _time.monotonic() < deadline:
            with mgr._lock:
                running = [
                    j for j in mgr.jobs.values() if j.status == JobStatus.RUNNING
                ]
            if len(running) == 3:
                break
            _time.sleep(0.05)

        try:
            # DB reports 120 total rows, returns none on this mock
            with patch.object(
                mgr._sqlite_backend,
                "list_jobs_filtered",
                return_value=([], 120),
            ):
                _, total_count, total_pages = mgr.get_jobs_for_display(
                    page=1,
                    page_size=50,
                    is_admin=True,
                )
        finally:
            for b in barriers:
                b.set()

        assert total_count == 123, (
            f"total_count={total_count}, expected 123 (3 active + 120 DB)"
        )
        assert total_pages == 3, f"total_pages={total_pages}, expected 3 (ceil(123/50))"


# ===========================================================================
# Part 4G: _get_all_jobs full multi-page reachability (Bug #736 / BLOCKING 3)
# ===========================================================================


def _make_bg_job_dict(job_id: str) -> Dict[str, Any]:
    return {
        "job_id": job_id,
        "status": "completed",
        "operation_type": "index",
        "created_at": "2024-01-01T00:00:00",
        "started_at": None,
        "completed_at": None,
        "result": None,
        "error": None,
        "progress": 100,
        "username": "admin",
        "is_admin": False,
        "cancelled": False,
        "repo_alias": f"repo-{job_id}",
        "resolution_attempts": 0,
        "claude_actions": None,
        "failure_reason": None,
        "extended_error": None,
        "language_resolution_status": None,
        "current_phase": None,
        "phase_detail": None,
        "progress_info": None,
        "metadata": None,
        "executing_node": None,
        "claimed_at": None,
        "actor_username": "admin",
    }


class TestGetAllJobsMergeReachability:
    """
    Bug #736 / BLOCKING 3: _get_all_jobs must make every BG job AND every
    tracker-only job reachable exactly once across all pages, with
    total_count == distinct reachable count.

    Scenario: 55 BG jobs (NOT a multiple of page_size=50) + 8 tracker-only
    dependency_map jobs.  total_count must be 63, total_pages must be 2, and
    iterating pages 1..2 must yield all 63 distinct job IDs.

    The test calls routes._get_all_jobs directly (not a mock of the merge
    logic) so the real boundary-handling code is exercised.
    """

    def test_all_jobs_reachable_exactly_once_with_partial_bg_page(self) -> None:
        """
        55 BG + 8 tracker-only, page_size=50:
          - total_count == 63
          - total_pages == 2
          - Iterating pages 1..total_pages yields all 63 IDs exactly once
          - Zero duplicates
        """
        from code_indexer.server.web import routes

        page_size = 50
        n_bg = 55  # NOT a multiple of page_size — triggers the partial-last-page bug
        n_tracker_only = 8

        bg_rows = [_make_bg_job_dict(f"bg-{i:03d}") for i in range(n_bg)]
        tracker_only_rows = [
            {
                "job_id": f"dep-{i}",
                "operation_type": "dependency_map_full",
                "status": "completed",
            }
            for i in range(n_tracker_only)
        ]

        def fake_get_jobs_for_display(
            status_filter=None,
            type_filter=None,
            search_text=None,
            page: int = 1,
            page_size: int = 50,
            is_admin: bool = False,
            username=None,
        ):
            capped = min(page_size, MAX_PAGE_SIZE)
            offset = (page - 1) * capped
            rows = bg_rows[offset : offset + capped]
            total = n_bg
            total_pages_val = max(1, (total + capped - 1) // capped)
            return rows, total, total_pages_val

        def fake_get_job_status(job_id: str, username: str, is_admin: bool = False):
            # Tracker-only jobs are NOT in the BG manager — return None
            if job_id.startswith("dep-"):
                return None
            # BG jobs ARE in the BG manager
            for row in bg_rows:
                if row["job_id"] == job_id:
                    return row
            return None

        mock_mgr = MagicMock()
        mock_mgr.get_jobs_for_display.side_effect = fake_get_jobs_for_display
        mock_mgr.get_job_status.side_effect = fake_get_job_status
        # list_display_job_ids returns the full BG id set in one query.
        # Tracker-only jobs (dep-*) are NOT in bg_rows so they appear as extras.
        mock_mgr.list_display_job_ids.return_value = {r["job_id"] for r in bg_rows}

        mock_tracker = MagicMock()
        mock_tracker.get_recent_jobs.return_value = list(tracker_only_rows)

        all_seen: list = []
        last_total_count = 0
        last_total_pages = 0

        with (
            patch.object(routes, "_get_background_job_manager", return_value=mock_mgr),
            patch.object(routes, "_get_job_tracker", return_value=mock_tracker),
            patch.object(
                routes, "_apply_job_filters", side_effect=lambda jobs, *a, **k: jobs
            ),
        ):
            p = 1
            while True:
                jobs, total_count, total_pages = routes._get_all_jobs(
                    page=p, page_size=page_size, is_admin=True
                )
                last_total_count = total_count
                last_total_pages = total_pages
                all_seen.extend(j["job_id"] for j in jobs)
                if p >= total_pages:
                    break
                p += 1
                assert p <= 50, "Safety: too many pages, infinite loop guard"

        # (i) total_count correct
        assert last_total_count == n_bg + n_tracker_only, (
            f"total_count={last_total_count}, expected {n_bg + n_tracker_only}. "
            f"BG={n_bg}, tracker-only={n_tracker_only}."
        )

        # (ii) total_pages correct
        expected_pages = max(1, (last_total_count + page_size - 1) // page_size)
        assert last_total_pages == expected_pages, (
            f"total_pages={last_total_pages}, expected {expected_pages}."
        )

        # (iii) zero duplicates
        seen_set = set(all_seen)
        assert len(all_seen) == len(seen_set), (
            f"Duplicates found: {len(all_seen) - len(seen_set)} duplicate IDs. "
            f"Counts: {[jid for jid in seen_set if all_seen.count(jid) > 1]}"
        )

        # (iv) every BG job reachable exactly once
        bg_ids = {r["job_id"] for r in bg_rows}
        missing_bg = bg_ids - seen_set
        assert not missing_bg, (
            f"{len(missing_bg)} BG jobs never appeared on any page: "
            f"{sorted(missing_bg)[:10]}"
        )

        # (v) every tracker-only job reachable exactly once
        tracker_ids = {r["job_id"] for r in tracker_only_rows}
        missing_tracker = tracker_ids - seen_set
        assert not missing_tracker, (
            f"{len(missing_tracker)} tracker-only jobs never appeared: "
            f"{sorted(missing_tracker)}"
        )


# ===========================================================================
# Part 4H: list_job_ids_filtered — SQLite backend id-only query
# ===========================================================================


def _make_sqlite_backend(tmp_path):
    """Create a BackgroundJobsSqliteBackend with the schema initialized."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend

    db_path = str(tmp_path / "jobs.db")
    DatabaseSchema(db_path).initialize_database()
    return BackgroundJobsSqliteBackend(db_path)


def _insert_job(backend, job_id: str, **kwargs):
    """Insert a minimal job row into the backend."""
    from datetime import datetime, timezone

    defaults = dict(
        operation_type="sync_repository",
        status="completed",
        created_at=datetime.now(timezone.utc).isoformat(),
        username="admin",
        progress=100,
    )
    defaults.update(kwargs)
    backend.save_job(job_id=job_id, **defaults)


class TestListJobIdsFilteredSqlite:
    """
    BackgroundJobsSqliteBackend.list_job_ids_filtered() must:
    - Return correct job_ids for status/type/search_text/username filters.
    - Respect the cap (LIMIT) parameter.
    - Return a set that is a subset of what list_jobs_filtered returns for
      the same filter arguments (no drift between the two methods).
    """

    def test_returns_correct_ids_no_filter(self, tmp_path):
        """All job_ids returned when no filter applied."""
        backend = _make_sqlite_backend(tmp_path)
        _insert_job(backend, "job-a")
        _insert_job(backend, "job-b")
        _insert_job(backend, "job-c")

        ids = backend.list_job_ids_filtered()
        assert {"job-a", "job-b", "job-c"} == ids

    def test_filters_by_status(self, tmp_path):
        """Only ids matching the status filter are returned."""
        backend = _make_sqlite_backend(tmp_path)
        _insert_job(backend, "run-1", status="running")
        _insert_job(backend, "done-1", status="completed")
        _insert_job(backend, "done-2", status="completed")

        ids = backend.list_job_ids_filtered(status="running")
        assert ids == {"run-1"}

    def test_filters_by_operation_type(self, tmp_path):
        """Only ids matching the operation_type filter are returned."""
        backend = _make_sqlite_backend(tmp_path)
        _insert_job(backend, "idx-1", operation_type="index")
        _insert_job(backend, "sync-1", operation_type="sync_repository")

        ids = backend.list_job_ids_filtered(operation_type="index")
        assert ids == {"idx-1"}

    def test_filters_by_search_text_repo_alias(self, tmp_path):
        """search_text matches repo_alias (same columns as list_jobs_filtered)."""
        backend = _make_sqlite_backend(tmp_path)
        _insert_job(backend, "job-match", repo_alias="my-special-repo")
        _insert_job(backend, "job-other", repo_alias="other-repo")

        ids = backend.list_job_ids_filtered(search_text="special")
        assert ids == {"job-match"}

    def test_filters_by_username(self, tmp_path):
        """username filter scopes results to that user's jobs."""
        backend = _make_sqlite_backend(tmp_path)
        _insert_job(backend, "job-alice", username="alice")
        _insert_job(backend, "job-bob", username="bob")

        ids = backend.list_job_ids_filtered(username="alice")
        assert ids == {"job-alice"}

    def test_cap_respected(self, tmp_path):
        """The cap parameter limits the number of ids returned."""
        backend = _make_sqlite_backend(tmp_path)
        for i in range(10):
            _insert_job(backend, f"job-{i:02d}")

        ids = backend.list_job_ids_filtered(cap=5)
        assert len(ids) <= 5

    def test_no_drift_from_list_jobs_filtered(self, tmp_path):
        """
        list_job_ids_filtered must return a superset or exact set of the
        job_ids that list_jobs_filtered would return for the same filter,
        proving the WHERE clause is built from the same logic (no drift).
        """
        backend = _make_sqlite_backend(tmp_path)
        _insert_job(backend, "match-1", status="running", repo_alias="repo-x")
        _insert_job(backend, "match-2", status="running", repo_alias="repo-x")
        _insert_job(backend, "no-match-status", status="completed", repo_alias="repo-x")
        _insert_job(backend, "no-match-type", status="running", repo_alias="other")

        # list_jobs_filtered with status="running" and search_text="repo-x"
        full_jobs, full_count = backend.list_jobs_filtered(
            status="running", search_text="repo-x"
        )
        full_ids = {j["job_id"] for j in full_jobs}

        # list_job_ids_filtered with same filters — must return same ids
        ids = backend.list_job_ids_filtered(status="running", search_text="repo-x")
        assert ids == full_ids, (
            f"Drift detected: list_job_ids_filtered returned {ids!r}, "
            f"but list_jobs_filtered returned {full_ids!r} for same filter. "
            f"The WHERE clause must be built from shared logic."
        )


# ===========================================================================
# Part 4I: list_display_job_ids — BackgroundJobManager delegation
# ===========================================================================


class TestListDisplayJobIds:
    """
    BackgroundJobManager.list_display_job_ids() must delegate to
    _sqlite_backend.list_job_ids_filtered() with the correct arguments.
    """

    def test_delegates_to_backend(self, tmp_path, request):
        """list_display_job_ids must call list_job_ids_filtered on the backend."""
        from unittest.mock import patch

        mgr = _make_manager(tmp_path, request)
        if mgr._sqlite_backend is None:
            pytest.skip("No SQLite backend available")

        expected_ids = {"job-x", "job-y"}
        with patch.object(
            mgr._sqlite_backend,
            "list_job_ids_filtered",
            return_value=expected_ids,
        ) as mock_fn:
            result = mgr.list_display_job_ids(
                status_filter="running",
                type_filter="sync_repository",
                search_text="my-repo",
                username=None,
            )

        mock_fn.assert_called_once()
        assert result == expected_ids

    def test_passes_filters_correctly(self, tmp_path, request):
        """Filters must be forwarded verbatim to list_job_ids_filtered."""
        from unittest.mock import patch

        mgr = _make_manager(tmp_path, request)
        if mgr._sqlite_backend is None:
            pytest.skip("No SQLite backend available")

        captured_kwargs: list = []
        original = mgr._sqlite_backend.list_job_ids_filtered

        def spy(*args, **kwargs):
            captured_kwargs.append(kwargs.copy())
            return original(*args, **kwargs)

        with patch.object(
            mgr._sqlite_backend, "list_job_ids_filtered", side_effect=spy
        ):
            mgr.list_display_job_ids(
                status_filter="completed",
                type_filter="index",
                search_text="needle",
                username="bob",
            )

        assert len(captured_kwargs) == 1
        kw = captured_kwargs[0]
        assert kw.get("status") == "completed"
        assert kw.get("operation_type") == "index"
        assert kw.get("search_text") == "needle"
        assert kw.get("username") == "bob"

    def test_returns_empty_set_when_no_backend(self, tmp_path, request):
        """When no backend is present, must return empty set (not raise)."""
        mgr = BackgroundJobManager(
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=2
            ),
            db_path=str(tmp_path / "jobs.db"),
            use_sqlite=False,
        )
        request.addfinalizer(mgr.shutdown)
        result = mgr.list_display_job_ids(
            status_filter=None, type_filter=None, search_text=None, username=None
        )
        assert result == set()


# ===========================================================================
# Part 4J: _get_all_jobs single-query perf guard
# ===========================================================================


class TestGetAllJobsSingleQueryPerfGuard:
    """
    _get_all_jobs must resolve the BG id set with EXACTLY ONE call to
    list_display_job_ids, regardless of bg_total_pages.

    This prevents the old N-page loop (which did ~bg_total_pages DB round-trips
    per render) from silently returning.  With 420,000 rows at 30-day retention
    and page_size=50, the loop would issue ~8,400 DB fetches per render.
    """

    def _run_get_all_jobs_with_spy(self, n_bg: int, page: int = 1, page_size: int = 50):
        """
        Run _get_all_jobs with bg_total=n_bg and spy on:
          - list_display_job_ids (must be called exactly once)
          - get_jobs_for_display (must be called exactly once — current page only)
        Returns (jobs, total_count, total_pages, display_ids_call_count, display_call_count).
        """
        from code_indexer.server.web import routes
        from unittest.mock import patch, MagicMock

        bg_rows = [_make_bg_job_dict(f"bg-{i:03d}") for i in range(n_bg)]

        def fake_get_jobs_for_display(
            status_filter=None,
            type_filter=None,
            search_text=None,
            page: int = 1,
            page_size: int = 50,
            is_admin: bool = False,
            username=None,
        ):
            offset = (page - 1) * page_size
            rows = bg_rows[offset : offset + page_size]
            total = n_bg
            total_pages_val = max(1, (total + page_size - 1) // page_size)
            return rows, total, total_pages_val

        mock_mgr = MagicMock()
        mock_mgr.get_jobs_for_display.side_effect = fake_get_jobs_for_display
        # list_display_job_ids returns all bg job ids
        bg_id_set = {f"bg-{i:03d}" for i in range(n_bg)}
        mock_mgr.list_display_job_ids.return_value = bg_id_set

        mock_tracker = MagicMock()
        mock_tracker.get_recent_jobs.return_value = []

        with (
            patch.object(routes, "_get_background_job_manager", return_value=mock_mgr),
            patch.object(routes, "_get_job_tracker", return_value=mock_tracker),
            patch.object(
                routes, "_apply_job_filters", side_effect=lambda jobs, *a, **k: jobs
            ),
        ):
            jobs, total_count, total_pages = routes._get_all_jobs(
                page=page, page_size=page_size, is_admin=True
            )

        display_ids_call_count = mock_mgr.list_display_job_ids.call_count
        display_call_count = mock_mgr.get_jobs_for_display.call_count
        return (
            jobs,
            total_count,
            total_pages,
            display_ids_call_count,
            display_call_count,
        )

    def test_list_display_job_ids_called_exactly_once_small_bg(self):
        """With 10 BG jobs (1 page), list_display_job_ids called exactly once."""
        _, _, _, id_calls, display_calls = self._run_get_all_jobs_with_spy(n_bg=10)
        assert id_calls == 1, (
            f"list_display_job_ids called {id_calls} times (expected 1). "
            f"N-page loop must be replaced with single id-only query."
        )
        assert display_calls == 1, (
            f"get_jobs_for_display called {display_calls} times (expected 1 — current page only)."
        )

    def test_list_display_job_ids_called_exactly_once_large_bg(self):
        """With 420,000 BG jobs (8,400 pages), list_display_job_ids still called exactly once."""
        # We use a smaller proxy (200 rows = 4 pages) to keep the test fast;
        # the key invariant is call_count == 1 regardless of bg_total_pages.
        _, _, _, id_calls, display_calls = self._run_get_all_jobs_with_spy(n_bg=200)
        assert id_calls == 1, (
            f"list_display_job_ids called {id_calls} times for 200 BG jobs (4 pages). "
            f"This must be 1 — never proportional to bg_total_pages."
        )
        assert display_calls == 1, (
            f"get_jobs_for_display called {display_calls} times (expected 1 — current page only)."
        )

    def test_extra_jobs_excluded_correctly_via_single_query(self):
        """
        Tracker jobs whose job_id is in the BG id set must be excluded as extras,
        even if they are on a different page than the current one.
        """
        from code_indexer.server.web import routes
        from unittest.mock import patch, MagicMock

        n_bg = 60  # 2 pages at page_size=50
        bg_rows = [_make_bg_job_dict(f"bg-{i:03d}") for i in range(n_bg)]
        # Tracker has bg-000 (a BG job on page 1) plus a tracker-only dep-map job
        tracker_job_dup = {
            "job_id": "bg-000",
            "operation_type": "dependency_map_full",
            "status": "running",
        }
        tracker_job_only = {
            "job_id": "dep-map-only",
            "operation_type": "dependency_map_full",
            "status": "running",
        }

        def fake_get_jobs_for_display(
            status_filter=None,
            type_filter=None,
            search_text=None,
            page: int = 1,
            page_size: int = 50,
            is_admin: bool = False,
            username=None,
        ):
            offset = (page - 1) * page_size
            rows = bg_rows[offset : offset + page_size]
            return rows, n_bg, max(1, (n_bg + page_size - 1) // page_size)

        mock_mgr = MagicMock()
        mock_mgr.get_jobs_for_display.side_effect = fake_get_jobs_for_display
        # BG id set includes bg-000 but NOT dep-map-only
        bg_id_set = {f"bg-{i:03d}" for i in range(n_bg)}
        mock_mgr.list_display_job_ids.return_value = bg_id_set

        mock_tracker = MagicMock()
        mock_tracker.get_recent_jobs.return_value = [tracker_job_dup, tracker_job_only]

        all_seen: list = []
        with (
            patch.object(routes, "_get_background_job_manager", return_value=mock_mgr),
            patch.object(routes, "_get_job_tracker", return_value=mock_tracker),
            patch.object(
                routes, "_apply_job_filters", side_effect=lambda jobs, *a, **k: jobs
            ),
        ):
            for p in range(1, 3):
                jobs, _, _ = routes._get_all_jobs(page=p, page_size=50, is_admin=True)
                all_seen.extend(j["job_id"] for j in jobs)

        # bg-000 must appear exactly once (from BG, not as tracker extra)
        assert all_seen.count("bg-000") == 1, (
            f"bg-000 appeared {all_seen.count('bg-000')} times; "
            f"must appear exactly once (from BG)."
        )
        # dep-map-only must appear exactly once (as a tracker extra)
        assert all_seen.count("dep-map-only") == 1, (
            f"dep-map-only appeared {all_seen.count('dep-map-only')} times; "
            f"must appear exactly once (as tracker extra)."
        )


# ===========================================================================
# Bug #1447: this file's own BackgroundJobManager instances must not leak
# live daemon worker threads or produce "no such table: background_jobs"
# persist errors -- both were confirmed (via a real combined chunk run) to
# pollute later, unrelated tests in the same single-process pytest chunk
# (tests/unit/server/web/ + repositories/ + routers/ run together with no
# xdist isolation in server-fast-automation.sh).
# ===========================================================================


class TestManagerLifecycleAndSchemaHygiene:
    """Bug #1447 regression: every manager built by _make_manager() must have
    its SQLite schema initialized before use AND must be shut down via a
    registered pytest finalizer, so its daemon worker-pool threads never
    outlive this file's own tests.
    """

    def test_make_manager_helper_initializes_schema_and_avoids_no_such_table(
        self, request, tmp_path, caplog
    ):
        """Fix verification: _make_manager() initializes the schema before
        constructing the manager, so a submitted job's terminal persist
        succeeds -- zero 'no such table' errors logged.
        """
        mgr = _make_manager(tmp_path, request)

        with caplog.at_level(logging.ERROR):
            job_id = mgr.submit_job(
                operation_type="test_op",
                func=lambda: {"success": True},
                submitter_username="admin",
                is_admin=True,
            )
            # Use the public get_job_status() API (not raw mgr.jobs access):
            # a successful terminal persist evicts the job from in-memory
            # mgr.jobs, and get_job_status() correctly falls back to reading
            # the SQLite row in that case.
            deadline = time.monotonic() + 5.0
            status: Any = None
            while time.monotonic() < deadline:
                status = mgr.get_job_status(job_id, username="admin", is_admin=True)
                if status is not None and status.get("status") == "completed":
                    break
                time.sleep(0.05)

        assert status is not None and status.get("status") == "completed", (
            f"Job should reach 'completed'; got status={status}"
        )
        assert not any("no such table" in r.getMessage() for r in caplog.records), (
            "Fixed _make_manager() must never log a 'no such table' error"
        )

    def test_uninitialized_schema_manager_logs_no_such_table_on_terminal_persist(
        self, tmp_path, caplog
    ):
        """Characterization: reproduces the exact log line from GitHub issue
        #1447 ("Failed to persist job ... to SQLite: no such table:
        background_jobs").

        A BackgroundJobManager(use_sqlite=True, ...) whose schema was never
        initialized -- the pattern this file used everywhere before the
        Bug #1447 fix -- logs exactly that error when a submitted job
        completes and the pool worker tries to persist its terminal status.
        This proves the root cause: a missing
        DatabaseSchema(...).initialize_database() call in THIS test file,
        not a shared/global persistence path reachable from an unrelated
        BackgroundJobManager(use_sqlite=False) instance.
        """
        mgr = BackgroundJobManager(
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=1
            ),
            db_path=str(tmp_path / "uninitialized.db"),
            use_sqlite=True,
        )
        try:
            with caplog.at_level(logging.ERROR):
                mgr.submit_job(
                    operation_type="test_op",
                    func=lambda: {"success": True},
                    submitter_username="admin",
                    is_admin=True,
                )
                deadline = time.monotonic() + 5.0
                found = False
                while time.monotonic() < deadline:
                    found = any(
                        "no such table: background_jobs" in r.getMessage()
                        for r in caplog.records
                    )
                    if found:
                        break
                    time.sleep(0.05)

            assert found, (
                "Expected the uninitialized-schema manager to log "
                "'no such table: background_jobs' on terminal persist, "
                "reproducing GitHub issue #1447's exact symptom."
            )
        finally:
            mgr.shutdown()

    def test_make_manager_helper_registers_shutdown_finalizer(self, request, tmp_path):
        """Fix verification: _make_manager() registers request.addfinalizer
        (mgr.shutdown), so its bgm-worker-*/bgm-temporal-worker-* daemon
        threads are guaranteed to stop instead of leaking for the rest of
        the pytest process (Bug #1447: this exact leak, across many
        un-shutdown managers in this file, was the mechanism that starved
        an unrelated sibling test's freshly submitted job of CPU/scheduling
        time under full-chunk load).
        """
        registered_finalizers = []
        original_addfinalizer = request.addfinalizer

        def capturing_addfinalizer(func):
            registered_finalizers.append(func)
            return original_addfinalizer(func)

        request.addfinalizer = capturing_addfinalizer
        try:
            mgr = _make_manager(tmp_path, request)
        finally:
            request.addfinalizer = original_addfinalizer

        assert registered_finalizers, (
            "_make_manager() must register a finalizer via request.addfinalizer"
        )

        marker = str(id(mgr))
        alive_before = [
            t for t in threading.enumerate() if marker in t.name and t.is_alive()
        ]
        assert alive_before, "Manager should have live worker threads before shutdown"

        # Fire the captured finalizer(s) ourselves, simulating pytest's real
        # end-of-test teardown running early.
        for fn in registered_finalizers:
            fn()

        alive_after = [
            t for t in threading.enumerate() if marker in t.name and t.is_alive()
        ]
        assert alive_after == [], (
            f"Manager worker threads must all stop once the finalizer runs; "
            f"still alive: {[t.name for t in alive_after]}"
        )
