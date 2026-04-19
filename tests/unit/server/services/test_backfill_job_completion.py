"""
Unit tests for Bug #853 Fix 3 & 4: aggregate lifecycle_backfill job_id must be
stored and propagated so complete_job can be called when all repos finish.

TDD Red phase: tests written BEFORE the fix.

Covered scenarios:
1. _backfill_register_aggregate_job returns the exact job_id it passed to register_job
2. _backfill_register_aggregate_job returns None when job_tracker is None
3. _backfill_register_aggregate_job returns None when total is 0
4. _active_backfill_job_id initialised to None in __init__
5. _queue_lifecycle_backfill_if_needed stores the exact job_id in _active_backfill_job_id
6. scheduler.set_active_backfill_job_id receives the exact registered job_id
7. non-owner nodes leave _active_backfill_job_id as None
"""

import sqlite3
from pathlib import Path
from typing import Optional, Tuple
from unittest.mock import MagicMock, Mock

from code_indexer.global_repos.lifecycle_schema import LIFECYCLE_SCHEMA_VERSION
from code_indexer.server.services.dependency_map_service import DependencyMapService
from code_indexer.server.services.description_refresh_scheduler import (
    DescriptionRefreshScheduler,
)
from code_indexer.server.services.job_tracker import DuplicateJobError
from code_indexer.server.storage.sqlite_backends import (
    DescriptionRefreshTrackingBackend,
)

_CLUSTER_WIDE_TOTAL = 5


def _make_conn_manager_as_owner(total: int = _CLUSTER_WIDE_TOTAL):
    """Return a conn_manager where the count query returns a non-zero total."""
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (total,)
    conn.execute.return_value.fetchall.return_value = []
    conn.execute.return_value.rowcount = 0

    conn_manager = MagicMock()
    conn_manager.execute_atomic.side_effect = lambda fn: fn(conn)
    return conn_manager


def _make_tracker_as_owner():
    """Return a mock JobTracker that does not raise DuplicateJobError (this node is owner)."""
    mock_tracker = MagicMock()
    mock_tracker.check_operation_conflict.return_value = None
    return mock_tracker


def _make_tracker_as_non_owner():
    """Return a mock JobTracker that raises DuplicateJobError (another node is owner)."""
    mock_tracker = MagicMock()
    mock_tracker.check_operation_conflict.side_effect = DuplicateJobError(
        "lifecycle_backfill", None, "existing-job-id"
    )
    return mock_tracker


def _make_service(
    tmp_path, job_tracker=None, refresh_scheduler=None, conn_manager=None
):
    """Build a real DependencyMapService with controllable external dependencies."""
    effective_conn_manager = conn_manager or _make_conn_manager_as_owner()

    tracking = Mock()
    tracking._conn_manager = effective_conn_manager
    tracking.get_tracking.return_value = {"status": "pending", "commit_hashes": None}

    gm = Mock()
    gm.golden_repos_dir = str(tmp_path)

    return DependencyMapService(
        gm,
        Mock(),
        tracking,
        Mock(),
        refresh_scheduler=refresh_scheduler,
        job_tracker=job_tracker,
    )


class TestBackfillRegisterAggregateJobReturnsId:
    """Fix 3: _backfill_register_aggregate_job must return the exact job_id string."""

    def test_returns_exact_job_id_passed_to_register_job(self, tmp_path):
        """_backfill_register_aggregate_job returns the same job_id it passed to register_job."""
        mock_tracker = _make_tracker_as_owner()
        service = _make_service(tmp_path, job_tracker=mock_tracker)

        result = service._backfill_register_aggregate_job(_CLUSTER_WIDE_TOTAL)

        assert result is not None, (
            "_backfill_register_aggregate_job must return job_id, got None"
        )
        registered_job_id = mock_tracker.register_job.call_args[0][0]
        assert result == registered_job_id, (
            f"Returned job_id {result!r} must match the id passed to register_job "
            f"{registered_job_id!r}"
        )

    def test_returns_none_when_job_tracker_is_none(self, tmp_path):
        """Returns None (no-op) when job_tracker is not configured."""
        service = _make_service(tmp_path, job_tracker=None)

        result = service._backfill_register_aggregate_job(_CLUSTER_WIDE_TOTAL)

        assert result is None

    def test_returns_none_when_total_is_zero(self, tmp_path):
        """Returns None (no-op) when cluster_wide_total is 0."""
        mock_tracker = _make_tracker_as_owner()
        service = _make_service(tmp_path, job_tracker=mock_tracker)

        result = service._backfill_register_aggregate_job(0)

        assert result is None
        call_count = mock_tracker.register_job.call_count
        assert call_count == 0, (
            f"register_job must not be called when total=0, but was called {call_count} times"
        )


class TestActiveBackfillJobIdInitialised:
    """Fix 3: _active_backfill_job_id must be initialised to None in __init__."""

    def test_active_backfill_job_id_starts_as_none(self, tmp_path):
        """DependencyMapService.__init__ sets _active_backfill_job_id = None."""
        service = _make_service(tmp_path)

        has_attr = hasattr(service, "_active_backfill_job_id")
        assert has_attr is True, (
            "DependencyMapService must have _active_backfill_job_id attribute"
        )
        assert service._active_backfill_job_id is None


class TestQueueLifecycleBackfillStoresJobId:
    """Fix 3: _queue_lifecycle_backfill_if_needed stores exact returned job_id."""

    def _make_owner_service(self, tmp_path, scheduler):
        """Build service that acts as aggregate owner via real collaborator mocks."""
        mock_tracker = _make_tracker_as_owner()
        conn_manager = _make_conn_manager_as_owner(total=_CLUSTER_WIDE_TOTAL)
        return _make_service(
            tmp_path,
            job_tracker=mock_tracker,
            refresh_scheduler=scheduler,
            conn_manager=conn_manager,
        ), mock_tracker

    def test_stores_exact_job_id_in_active_backfill_job_id(self, tmp_path):
        """_queue_lifecycle_backfill_if_needed stores the exact job_id from registration."""
        scheduler = Mock()
        scheduler.set_active_backfill_job_id = Mock()
        service, mock_tracker = self._make_owner_service(tmp_path, scheduler)

        service._queue_lifecycle_backfill_if_needed()

        registered_job_id = mock_tracker.register_job.call_args[0][0]
        stored_job_id = service._active_backfill_job_id
        assert stored_job_id == registered_job_id, (
            f"_active_backfill_job_id {stored_job_id!r} must equal "
            f"the job_id passed to register_job {registered_job_id!r}"
        )

    def test_scheduler_receives_exact_registered_job_id(self, tmp_path):
        """scheduler.set_active_backfill_job_id receives the exact registered job_id."""
        scheduler = Mock()
        scheduler.set_active_backfill_job_id = Mock()
        service, mock_tracker = self._make_owner_service(tmp_path, scheduler)

        service._queue_lifecycle_backfill_if_needed()

        registered_job_id = mock_tracker.register_job.call_args[0][0]
        passed_to_scheduler = scheduler.set_active_backfill_job_id.call_args[0][0]
        assert passed_to_scheduler == registered_job_id, (
            f"Scheduler received {passed_to_scheduler!r} but expected "
            f"registered job_id {registered_job_id!r}"
        )

    def test_leaves_job_id_none_when_not_owner(self, tmp_path):
        """Non-owner nodes do not register the aggregate job and leave job_id None."""
        scheduler = Mock()
        non_owner_tracker = _make_tracker_as_non_owner()
        service = _make_service(
            tmp_path,
            job_tracker=non_owner_tracker,
            refresh_scheduler=scheduler,
        )

        service._queue_lifecycle_backfill_if_needed()

        stored_job_id = service._active_backfill_job_id
        assert stored_job_id is None, (
            f"Non-owner must leave _active_backfill_job_id as None, got {stored_job_id!r}"
        )
        call_count = non_owner_tracker.register_job.call_count
        assert call_count == 0, (
            f"register_job must not be called for non-owner, was called {call_count} times"
        )


# ---------------------------------------------------------------------------
# Codex Review Issue 2a — constants and test helpers
# ---------------------------------------------------------------------------

# One version behind current — repo needs lifecycle backfill
_OLD_LIFECYCLE_VERSION = LIFECYCLE_SCHEMA_VERSION - 1

_BACKFILL_JOB_ID_2A = "lifecycle-backfill-test-2a-001"
_ALIAS_SINGLE = "repo-single"
_ALIAS_A = "repo-a"
_ALIAS_B = "repo-b"

# Sentinel to distinguish "not provided" from "explicitly None"
_UNSET = object()


def _make_tracking_db_with_repos(
    tmp_path: Path, aliases: list, lifecycle_version
) -> str:
    """
    Normal function returning db_path. Uses a context manager internally to
    guarantee the SQLite connection is always closed. Inserts tracking rows for
    each alias at the given lifecycle_schema_version.
    """
    db_path = str(tmp_path / "tracking.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS description_refresh_tracking (
                repo_alias TEXT PRIMARY KEY NOT NULL,
                last_run TEXT,
                next_run TEXT,
                status TEXT DEFAULT 'pending',
                error TEXT,
                last_known_commit TEXT,
                last_known_files_processed INTEGER,
                last_known_indexed_at TEXT,
                created_at TEXT,
                updated_at TEXT,
                lifecycle_schema_version INTEGER DEFAULT NULL
            )"""
        )
        for alias in aliases:
            conn.execute(
                "INSERT OR REPLACE INTO description_refresh_tracking "
                "(repo_alias, status, lifecycle_schema_version) VALUES (?, ?, ?)",
                (alias, "pending", lifecycle_version),
            )
    return db_path


def _make_scheduler_with_tracking_db(
    tmp_path: Path,
    job_tracker=None,
    db_path: str = None,
) -> DescriptionRefreshScheduler:
    """Build a DescriptionRefreshScheduler backed by a real tracking DB."""
    effective_db_path = db_path or str(tmp_path / "tracking.db")
    tracking_backend = DescriptionRefreshTrackingBackend(effective_db_path)
    golden_backend = Mock()
    golden_backend.get_repo.return_value = None
    config_manager = Mock()
    config_manager.load_config.return_value = None
    return DescriptionRefreshScheduler(
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        golden_backend=golden_backend,
        job_tracker=job_tracker,
    )


def _make_test_scheduler_with_backfill_job(
    tmp_path: Path,
    aliases: list,
    job_tracker=_UNSET,
    optional_job_id: Optional[str] = _BACKFILL_JOB_ID_2A,
) -> Tuple[DescriptionRefreshScheduler, object]:
    """
    Central setup helper for Issue 2a tests.

    job_tracker behaviour:
    - Not provided (_UNSET): helper creates a MagicMock() internally.
    - Explicitly None: None is passed through — scheduler gets no tracker.
    - Anything else: used as-is.

    Sets optional_job_id as active backfill job when provided (not None).
    Returns (scheduler, effective_tracker).
    """
    effective_tracker = MagicMock() if job_tracker is _UNSET else job_tracker
    db_path = _make_tracking_db_with_repos(
        tmp_path, aliases, lifecycle_version=_OLD_LIFECYCLE_VERSION
    )
    scheduler = _make_scheduler_with_tracking_db(
        tmp_path, job_tracker=effective_tracker, db_path=db_path
    )
    if optional_job_id is not None:
        scheduler.set_active_backfill_job_id(optional_job_id)
    return scheduler, effective_tracker


# ---------------------------------------------------------------------------
# Codex Review Issue 2a: complete_job called on happy path
# ---------------------------------------------------------------------------


class TestSelfCloseBackfillLastRepoCompletesJob:
    """
    Issue 2a: When the last repo self-closes its backfill, complete_job must be
    called on the aggregate job, and _active_backfill_job_id must be cleared.
    """

    def test_complete_job_called_with_correct_id_when_last_repo_closes(self, tmp_path):
        """complete_job called with active_backfill_job_id when last repo finishes."""
        scheduler, mock_tracker = _make_test_scheduler_with_backfill_job(
            tmp_path, [_ALIAS_SINGLE]
        )

        scheduler._self_close_backfill(_ALIAS_SINGLE)

        assert mock_tracker.complete_job.call_count == 1, (
            f"complete_job must be called once when last repo self-closes. "
            f"Was called {mock_tracker.complete_job.call_count} times."
        )
        assert mock_tracker.complete_job.call_args[0][0] == _BACKFILL_JOB_ID_2A, (
            f"complete_job called with wrong job_id: "
            f"{mock_tracker.complete_job.call_args[0][0]!r}"
        )

    def test_active_backfill_job_id_cleared_after_complete_job(self, tmp_path):
        """_active_backfill_job_id cleared to None after complete_job is called."""
        scheduler, _ = _make_test_scheduler_with_backfill_job(tmp_path, [_ALIAS_SINGLE])

        scheduler._self_close_backfill(_ALIAS_SINGLE)

        with scheduler._backfill_job_id_lock:
            current_id = scheduler._active_backfill_job_id
        assert current_id is None, (
            f"_active_backfill_job_id must be None after last repo completes. "
            f"Got {current_id!r}."
        )

    def test_complete_job_not_called_when_second_repo_still_needs_backfill(
        self, tmp_path
    ):
        """complete_job not called when a second repo still needs backfill."""
        scheduler, mock_tracker = _make_test_scheduler_with_backfill_job(
            tmp_path, [_ALIAS_A, _ALIAS_B]
        )

        scheduler._self_close_backfill(_ALIAS_A)

        assert mock_tracker.complete_job.call_count == 0, (
            f"complete_job must NOT be called when repos still need backfill. "
            f"Was called {mock_tracker.complete_job.call_count} times."
        )


class TestSelfCloseBackfillGuards:
    """Issue 2a guards: complete_job skipped when no tracker or no active job_id."""

    def test_complete_job_not_called_when_no_active_backfill_job_id(self, tmp_path):
        """No aggregate job_id set — complete_job must not be called."""
        scheduler, mock_tracker = _make_test_scheduler_with_backfill_job(
            tmp_path, [_ALIAS_SINGLE], optional_job_id=None
        )

        scheduler._self_close_backfill(_ALIAS_SINGLE)

        assert mock_tracker.complete_job.call_count == 0, (
            "complete_job must NOT be called when _active_backfill_job_id is None."
        )

    def test_no_crash_when_job_tracker_is_none(self, tmp_path):
        """job_tracker explicitly None — _self_close_backfill must not crash."""
        scheduler, _ = _make_test_scheduler_with_backfill_job(
            tmp_path, [_ALIAS_SINGLE], job_tracker=None
        )

        # Must not raise — no job_tracker means no complete_job call possible
        scheduler._self_close_backfill(_ALIAS_SINGLE)
