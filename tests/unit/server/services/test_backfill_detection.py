"""
Tests for Story #728 AC2/AC4/AC6/AC8: Backfill detection + JobTracker integration.

6 tests:
1. test_backfill_queues_stale_repos           — repos with version=0 get status='pending'
2. test_backfill_skips_already_pending         — repos already pending NOT re-queued
3. test_backfill_skips_current_version         — repos at current version NOT queued
4. test_version_bump_requeues_all              — simulated bump re-queues all
5. test_backfill_count_from_rowcount           — count from cursor.rowcount, not SELECT
6. test_backfill_runs_unconditionally          — called at start of run_delta_analysis,
                                                 before detect_changes short-circuit
"""

import sys
import threading
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

SRC_ROOT = Path(__file__).parent.parent.parent.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CURRENT_VERSION = 1  # matches LIFECYCLE_SCHEMA_VERSION from lifecycle_schema.py


def _make_db(tmp_path):
    """Create a real SQLite DB with description_refresh_tracking table."""
    from code_indexer.server.storage.database_manager import DatabaseConnectionManager

    db = tmp_path / "test.db"
    mgr = DatabaseConnectionManager(str(db))
    conn = mgr.get_connection()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS description_refresh_tracking (
               repo_alias TEXT PRIMARY KEY,
               last_run TEXT,
               next_run TEXT,
               status TEXT DEFAULT 'pending',
               error TEXT,
               last_known_commit TEXT,
               last_known_files_processed INTEGER,
               last_known_indexed_at TEXT,
               created_at TEXT,
               updated_at TEXT,
               lifecycle_schema_version INTEGER DEFAULT 0)"""
    )
    conn.commit()
    mgr.close_all()
    return db


def _insert_repo(db, alias, status="completed", lifecycle_schema_version=0):
    """Insert a tracking row directly."""
    from code_indexer.server.storage.database_manager import DatabaseConnectionManager

    mgr = DatabaseConnectionManager(str(db))
    conn = mgr.get_connection()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO description_refresh_tracking
           (repo_alias, last_run, next_run, status, lifecycle_schema_version, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (alias, now, now, status, lifecycle_schema_version, now, now),
    )
    conn.commit()
    mgr.close_all()


def _get_status(db, alias):
    """Read status and lifecycle_schema_version for a repo."""
    from code_indexer.server.storage.database_manager import DatabaseConnectionManager

    mgr = DatabaseConnectionManager(str(db))
    conn = mgr.get_connection()
    row = conn.execute(
        "SELECT status, lifecycle_schema_version FROM description_refresh_tracking WHERE repo_alias = ?",
        (alias,),
    ).fetchone()
    mgr.close_all()
    return (row[0], row[1]) if row else None


def _make_service(db, job_tracker=None):
    """Build a minimal DependencyMapService with real tracking backends.

    Two distinct backends are wired:
    - svc._tracking_backend: DependencyMapTrackingBackend — used by detect_changes()
      (has get_tracking / update_tracking for commit-hash comparisons)
    - svc._refresh_scheduler._tracking_backend: DescriptionRefreshTrackingBackend —
      used by _queue_lifecycle_backfill_if_needed() (has get_stale_repos / upsert_tracking)
    """
    from code_indexer.server.services.dependency_map_service import DependencyMapService
    from code_indexer.server.storage.sqlite_backends import (
        DescriptionRefreshTrackingBackend,
        DependencyMapTrackingBackend,
    )

    svc = object.__new__(DependencyMapService)

    svc._job_tracker = job_tracker
    svc._lock = threading.Lock()
    svc._config_manager = MagicMock()
    svc._analyzer = MagicMock()
    svc._golden_repos_manager = MagicMock()
    svc._activity_journal = MagicMock()
    svc._stop_event = threading.Event()
    svc._daemon_thread = None

    # DependencyMapTrackingBackend: used by detect_changes() via get_tracking().
    dep_map_tracking = DependencyMapTrackingBackend(str(db))
    svc._tracking_backend = dep_map_tracking

    # DescriptionRefreshTrackingBackend: used by _queue_lifecycle_backfill_if_needed()
    # via get_stale_repos / upsert_tracking.
    desc_refresh_tracking = DescriptionRefreshTrackingBackend(str(db))
    refresh_scheduler = MagicMock()
    refresh_scheduler._tracking_backend = desc_refresh_tracking
    refresh_scheduler._db_path = str(db)
    svc._refresh_scheduler = refresh_scheduler

    return svc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBackfillQueuesStaleRepos:
    def test_backfill_queues_stale_repos(self, tmp_path):
        """
        (1) repos with lifecycle_schema_version=0 (stale) are transitioned to
        status='pending' by _queue_lifecycle_backfill_if_needed().
        """
        db = _make_db(tmp_path)
        _insert_repo(db, "repo-a", status="completed", lifecycle_schema_version=0)
        _insert_repo(db, "repo-b", status="completed", lifecycle_schema_version=0)

        svc = _make_service(db)
        count = svc._queue_lifecycle_backfill_if_needed()

        assert count == 2, f"Expected 2 queued, got {count}"
        status_a, _ = _get_status(db, "repo-a")
        status_b, _ = _get_status(db, "repo-b")
        assert status_a == "pending", f"repo-a should be 'pending', got {status_a!r}"
        assert status_b == "pending", f"repo-b should be 'pending', got {status_b!r}"


class TestBackfillSkipsAlreadyPending:
    def test_backfill_skips_already_pending(self, tmp_path):
        """
        (2) repos already in status='pending', 'queued', or 'running' are NOT
        re-queued — guarded UPDATE excludes those statuses.
        """
        db = _make_db(tmp_path)
        _insert_repo(db, "repo-pending", status="pending", lifecycle_schema_version=0)
        _insert_repo(db, "repo-queued", status="queued", lifecycle_schema_version=0)
        _insert_repo(db, "repo-running", status="running", lifecycle_schema_version=0)

        svc = _make_service(db)
        count = svc._queue_lifecycle_backfill_if_needed()

        assert count == 0, f"Expected 0 re-queued, got {count}"
        assert _get_status(db, "repo-pending")[0] == "pending"
        assert _get_status(db, "repo-queued")[0] == "queued"
        assert _get_status(db, "repo-running")[0] == "running"


class TestBackfillSkipsCurrentVersion:
    def test_backfill_skips_current_version(self, tmp_path):
        """
        (3) repos at lifecycle_schema_version = LIFECYCLE_SCHEMA_VERSION are NOT queued.
        """
        db = _make_db(tmp_path)
        _insert_repo(
            db,
            "repo-current",
            status="completed",
            lifecycle_schema_version=CURRENT_VERSION,
        )

        svc = _make_service(db)
        count = svc._queue_lifecycle_backfill_if_needed()

        assert count == 0, f"Expected 0 queued for up-to-date repo, got {count}"
        assert _get_status(db, "repo-current")[0] == "completed"


class TestVersionBumpRequeuesAll:
    def test_version_bump_requeues_all(self, tmp_path):
        """
        (4) When LIFECYCLE_SCHEMA_VERSION is bumped (simulated by patching the constant),
        repos that were at the old version are re-queued.
        """
        db = _make_db(tmp_path)
        _insert_repo(db, "repo-a", status="completed", lifecycle_schema_version=1)
        _insert_repo(db, "repo-b", status="completed", lifecycle_schema_version=1)

        svc = _make_service(db)

        target = "code_indexer.server.services.dependency_map_service.LIFECYCLE_SCHEMA_VERSION"
        with patch(target, 2):
            count = svc._queue_lifecycle_backfill_if_needed()

        assert count == 2, f"Expected 2 re-queued after version bump, got {count}"
        assert _get_status(db, "repo-a")[0] == "pending"
        assert _get_status(db, "repo-b")[0] == "pending"


class TestBackfillCountFromRowcount:
    def test_backfill_count_from_rowcount(self, tmp_path):
        """
        (5) The returned count is the SUM of cursor.rowcount values from each
        per-alias UPDATE, NOT the count of candidates from the SELECT.

        Simulation: 2 stale repos appear in the candidate SELECT. For 'repo-loses',
        execute_atomic intercepts the callback BEFORE the real SQL runs and returns
        a fake cursor with rowcount=0 (simulating another cluster node having won the
        race before this node's UPDATE ran). For 'repo-wins', the real execute_atomic
        runs normally and returns rowcount=1.

        Both appear in candidate SELECT (version<1), so a naive len(candidates)
        count would be 2. The correct rowcount-based sum must be 1.
        """
        db = _make_db(tmp_path)
        _insert_repo(db, "repo-wins", status="completed", lifecycle_schema_version=0)
        _insert_repo(db, "repo-loses", status="completed", lifecycle_schema_version=0)

        svc = _make_service(db)
        conn_manager = svc._refresh_scheduler._tracking_backend._conn_manager

        class _ZeroRowcountCursor:
            rowcount = 0

        def _intercepting_execute_atomic(fn):
            """
            Wrap the connection object passed to fn. If fn tries to UPDATE for
            'repo-loses', return a zero-rowcount cursor WITHOUT calling real execute,
            simulating the concurrent-node race loss.
            """

            class _InspectingConn:
                def __init__(self, real_conn):
                    self._real = real_conn
                    self._intercepted = False

                def execute(self, sql, params=()):
                    if (
                        "UPDATE description_refresh_tracking" in sql
                        and "repo-loses" in params
                    ):
                        # Intercept BEFORE executing — no real SQL runs for this alias.
                        self._intercepted = True
                        return _ZeroRowcountCursor()
                    return self._real.execute(sql, params)

                def commit(self):
                    if not self._intercepted:
                        self._real.commit()

            real_conn = conn_manager.get_connection()
            return fn(_InspectingConn(real_conn))

        with patch.object(
            conn_manager, "execute_atomic", side_effect=_intercepting_execute_atomic
        ):
            count = svc._queue_lifecycle_backfill_if_needed()

        # Both repos in candidate SELECT; only repo-wins UPDATE returns rowcount=1
        assert count == 1, (
            f"Count must come from rowcount sum (1), not SELECT count (2). Got {count}"
        )


class TestBackfillRunsUnconditionally:
    def test_backfill_runs_unconditionally(self, tmp_path):
        """
        (6) _queue_lifecycle_backfill_if_needed() is called BEFORE detect_changes()
        inside run_delta_analysis(). Proves unconditional early execution.

        External-collaborator-only strategy (no SUT methods patched):
        - Backfill observable: conn_manager.execute_atomic fires when a stale repo
          is queued (the guarded UPDATE call). Seeding one stale repo guarantees this.
        - detect_changes() observable: detect_changes() calls
          self._tracking_backend.get_tracking() as its very first statement (line 1289
          of dependency_map_service.py). We spy on that external collaborator via
          patch.object.
        - Both must appear in call_order; backfill_execute_atomic must precede
          detect_changes_entry.
        """
        db = _make_db(tmp_path)
        _insert_repo(db, "repo-stale", status="completed", lifecycle_schema_version=0)

        svc = _make_service(db)
        svc._job_tracker = None

        mock_config = MagicMock()
        mock_config.dependency_map_enabled = True
        mock_config.dependency_map_interval_hours = 24
        svc._config_manager.get_claude_integration_config.return_value = mock_config

        call_order = []
        conn_manager = svc._refresh_scheduler._tracking_backend._conn_manager
        real_execute_atomic = conn_manager.execute_atomic

        def spy_execute_atomic(fn):
            call_order.append("backfill_execute_atomic")
            return real_execute_atomic(fn)

        def spy_get_tracking():
            # detect_changes() calls this as its first statement
            call_order.append("detect_changes_entry")
            return {"commit_hashes": None}

        with (
            patch.object(
                conn_manager, "execute_atomic", side_effect=spy_execute_atomic
            ),
            patch.object(
                svc._tracking_backend, "get_tracking", side_effect=spy_get_tracking
            ),
            patch.object(svc._tracking_backend, "update_tracking"),
        ):
            svc.run_delta_analysis()

        assert "backfill_execute_atomic" in call_order, (
            "_queue_lifecycle_backfill_if_needed never issued a DB update — "
            "either it was not called or the stale repo was not processed. "
            f"call_order: {call_order}"
        )
        assert "detect_changes_entry" in call_order, (
            "detect_changes() never called _tracking_backend.get_tracking — "
            "run_delta_analysis may have exited before reaching detect_changes. "
            f"call_order: {call_order}"
        )

        last_backfill_idx = max(
            i for i, v in enumerate(call_order) if v == "backfill_execute_atomic"
        )
        first_detect_idx = call_order.index("detect_changes_entry")
        assert last_backfill_idx < first_detect_idx, (
            f"All backfill DB updates must precede detect_changes entry. "
            f"Last backfill at index {last_backfill_idx}, detect_changes_entry at "
            f"{first_detect_idx}. Full order: {call_order}"
        )
