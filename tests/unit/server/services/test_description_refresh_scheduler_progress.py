"""
Unit tests for lifecycle_backfill aggregate job intermediate progress reporting.

TDD Red phase: tests written BEFORE the fix.

Bug: lifecycle_backfill aggregate job shows progress=0 throughout its entire
running period, snapping directly to terminal status.

Root cause: _maybe_complete_backfill_job and _maybe_fail_backfill_job never
call update_status with intermediate progress.

Fix required: both methods must call update_status(progress=N, progress_info=...)
before their terminal complete_job / fail_job call.
"""

import sqlite3
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, Mock

from code_indexer.global_repos.lifecycle_schema import LIFECYCLE_SCHEMA_VERSION
from code_indexer.server.services.description_refresh_scheduler import (
    DescriptionRefreshScheduler,
)
from code_indexer.server.storage.sqlite_backends import (
    DescriptionRefreshTrackingBackend,
)

# ---------------------------------------------------------------------------
# Named constants — no magic numbers
# ---------------------------------------------------------------------------

REPOS_TOTAL = 4
REPOS_COMPLETED_PARTIAL = 2
EXPECTED_PROGRESS_PARTIAL = 50  # int(2 * 100 / 4)
EXPECTED_PROGRESS_CAP = 99  # progress capped before terminal call

_BACKFILL_JOB_ID = "lifecycle-backfill-progress-test"
_OLD_LIFECYCLE_VERSION = LIFECYCLE_SCHEMA_VERSION - 1

_ALIAS_A = "repo-alpha"
_ALIAS_B = "repo-beta"
_ALIAS_C = "repo-gamma"
_ALIAS_D = "repo-delta"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tracking_db(tmp_path: Path, aliases: list) -> str:
    """Create a real SQLite tracking DB with repos at old lifecycle version."""
    db_path = str(tmp_path / "tracking.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS description_refresh_tracking (
                repo_alias TEXT PRIMARY KEY NOT NULL,
                last_run TEXT, next_run TEXT,
                status TEXT DEFAULT 'pending', error TEXT,
                last_known_commit TEXT, last_known_files_processed INTEGER,
                last_known_indexed_at TEXT, created_at TEXT, updated_at TEXT,
                lifecycle_schema_version INTEGER DEFAULT NULL
            )"""
        )
        for alias in aliases:
            conn.execute(
                "INSERT OR REPLACE INTO description_refresh_tracking "
                "(repo_alias, status, lifecycle_schema_version) VALUES (?, ?, ?)",
                (alias, "pending", _OLD_LIFECYCLE_VERSION),
            )
    return db_path


def _make_mock_tracker(cluster_wide_total: int) -> MagicMock:
    """Build a MagicMock tracker whose get_job returns cluster_wide_total in metadata."""
    mock_job = Mock()
    mock_job.metadata = {"cluster_wide_total": cluster_wide_total}
    mock_job.status = "running"
    tracker = MagicMock()
    tracker.get_job.return_value = mock_job
    return tracker


def _make_scheduler(
    tmp_path: Path,
    aliases: list,
    tracker,
    active_job_id: Optional[str] = _BACKFILL_JOB_ID,
    processed_count: int = 0,
) -> DescriptionRefreshScheduler:
    """Build a DescriptionRefreshScheduler with a real tracking DB."""
    db_path = _make_tracking_db(tmp_path, aliases)
    tracking_backend = DescriptionRefreshTrackingBackend(db_path)
    config_manager = Mock()
    config_manager.load_config.return_value = None
    golden_backend = Mock()
    golden_backend.get_repo.return_value = None

    scheduler = DescriptionRefreshScheduler(
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        golden_backend=golden_backend,
        job_tracker=tracker,
    )
    if active_job_id is not None:
        scheduler.set_active_backfill_job_id(active_job_id)
    scheduler._backfill_processed_count = processed_count
    return scheduler


def _extract_progress_values(tracker: MagicMock) -> list:
    """Extract all progress values passed to tracker.update_status calls."""
    values = []
    for args, kw in tracker.update_status.call_args_list:
        p = kw.get("progress") if kw else (args[1] if len(args) > 1 else None)
        if p is not None:
            values.append(p)
    return values


def _close_repos_in_db(db_path: str, aliases_to_close: list) -> None:
    """Mark repos as done (current lifecycle version) in the tracking DB."""
    with sqlite3.connect(db_path) as conn:
        for alias in aliases_to_close:
            conn.execute(
                "UPDATE description_refresh_tracking "
                "SET lifecycle_schema_version = ? WHERE repo_alias = ?",
                (LIFECYCLE_SCHEMA_VERSION, alias),
            )


# ---------------------------------------------------------------------------
# Class 1: Success path (_maybe_complete_backfill_job) emits intermediate progress
# ---------------------------------------------------------------------------


class TestMaybeCompleteBackfillJobEmitsProgress:
    """
    _maybe_complete_backfill_job must call update_status with progress > 0
    when some repos have completed (remaining < total) but not all.
    """

    def test_update_status_called_with_positive_progress_when_partial_repos_done(
        self, tmp_path
    ):
        """
        update_status is called with progress > 0 when REPOS_COMPLETED_PARTIAL
        of REPOS_TOTAL repos have completed (remaining > 0, so complete_job
        is NOT called yet).

        Bug: this test fails before the fix because update_status is never called.
        """
        aliases = [_ALIAS_A, _ALIAS_B, _ALIAS_C, _ALIAS_D]
        tracker = _make_mock_tracker(REPOS_TOTAL)
        scheduler = _make_scheduler(tmp_path, aliases, tracker)

        db_path = str(tmp_path / "tracking.db")
        _close_repos_in_db(db_path, [_ALIAS_A, _ALIAS_B])

        scheduler._maybe_complete_backfill_job()

        progress_values = _extract_progress_values(tracker)
        assert len(progress_values) >= 1, (
            "update_status must be called when intermediate repos complete. "
            f"update_status calls: {tracker.update_status.call_args_list}. "
            "Bug: _maybe_complete_backfill_job never emits progress updates."
        )
        assert any(p >= 1 for p in progress_values), (
            f"update_status must be called with progress >= 1. Got: {progress_values}"
        )

    def test_update_status_emits_correct_percentage(self, tmp_path):
        """
        Progress percentage = int(processed * 100 / total).
        With REPOS_COMPLETED_PARTIAL={} of REPOS_TOTAL={}, expected = {}.
        """.format(REPOS_COMPLETED_PARTIAL, REPOS_TOTAL, EXPECTED_PROGRESS_PARTIAL)
        aliases = [_ALIAS_A, _ALIAS_B, _ALIAS_C, _ALIAS_D]
        tracker = _make_mock_tracker(REPOS_TOTAL)
        scheduler = _make_scheduler(tmp_path, aliases, tracker)

        db_path = str(tmp_path / "tracking.db")
        _close_repos_in_db(db_path, [_ALIAS_A, _ALIAS_B])

        scheduler._maybe_complete_backfill_job()

        progress_values = _extract_progress_values(tracker)
        assert EXPECTED_PROGRESS_PARTIAL in progress_values, (
            f"Expected progress={EXPECTED_PROGRESS_PARTIAL} in update_status calls. "
            f"Got: {progress_values}"
        )

    def test_update_status_not_called_when_no_repos_done(self, tmp_path):
        """
        When all repos still need backfill (remaining == total, processed == 0),
        update_status must NOT be called (nothing to report yet).
        """
        aliases = [_ALIAS_A, _ALIAS_B, _ALIAS_C, _ALIAS_D]
        tracker = _make_mock_tracker(REPOS_TOTAL)
        scheduler = _make_scheduler(tmp_path, aliases, tracker)
        # No repos closed — remaining == REPOS_TOTAL, processed == 0

        scheduler._maybe_complete_backfill_job()

        assert tracker.update_status.call_count == 0, (
            f"update_status must NOT be called when no repos have completed. "
            f"Called {tracker.update_status.call_count} times."
        )


# ---------------------------------------------------------------------------
# Helper for failure-path tests
# ---------------------------------------------------------------------------


def _make_fail_path_scheduler(tmp_path: Path, processed_count: int):
    """
    Build a (scheduler, tracker) pair pre-configured for _maybe_fail_backfill_job tests.

    The method increments _backfill_processed_count internally, so callers pass
    processed_count = desired_final_count - 1.  cluster_wide_total is always REPOS_TOTAL.
    """
    aliases = [_ALIAS_A, _ALIAS_B, _ALIAS_C, _ALIAS_D]
    tracker = _make_mock_tracker(REPOS_TOTAL)
    scheduler = _make_scheduler(
        tmp_path,
        aliases,
        tracker,
        processed_count=processed_count,
    )
    return scheduler, tracker


# ---------------------------------------------------------------------------
# Class 2: Failure path (_maybe_fail_backfill_job) emits intermediate progress
# ---------------------------------------------------------------------------


class TestMaybeFailBackfillJobEmitsProgress:
    """
    _maybe_fail_backfill_job must call update_status with progress > 0
    when some repos have been processed (processed > 0) but not all
    (processed < cluster_wide_total).

    The method increments _backfill_processed_count internally, so callers
    set the counter to N-1 before invoking to simulate N repos processed.
    """

    def test_update_status_called_with_positive_progress_when_partial_repos_processed(
        self, tmp_path
    ):
        """
        update_status is called with progress > 0 when REPOS_COMPLETED_PARTIAL
        of REPOS_TOTAL repos have been processed via the failure path
        (processed < cluster_wide_total, so fail_job is NOT called yet).

        Bug: this test fails before the fix because update_status is never called.
        """
        # REPOS_COMPLETED_PARTIAL - 1 pre-set; method increments to REPOS_COMPLETED_PARTIAL
        scheduler, tracker = _make_fail_path_scheduler(
            tmp_path, processed_count=REPOS_COMPLETED_PARTIAL - 1
        )

        scheduler._maybe_fail_backfill_job(alias=_ALIAS_A)

        progress_values = _extract_progress_values(tracker)
        assert len(progress_values) >= 1, (
            "update_status must be called when intermediate repos are processed via failure path. "
            f"update_status calls: {tracker.update_status.call_args_list}. "
            "Bug: _maybe_fail_backfill_job never emits progress updates."
        )
        assert any(p >= 1 for p in progress_values), (
            f"update_status must be called with progress >= 1. Got: {progress_values}"
        )

    def test_update_status_emits_correct_percentage_failure_path(self, tmp_path):
        """
        Progress percentage = int(processed * 100 / total).
        With REPOS_COMPLETED_PARTIAL={} of REPOS_TOTAL={}, expected = {}.
        """.format(REPOS_COMPLETED_PARTIAL, REPOS_TOTAL, EXPECTED_PROGRESS_PARTIAL)
        # REPOS_COMPLETED_PARTIAL - 1 pre-set; method increments to REPOS_COMPLETED_PARTIAL
        scheduler, tracker = _make_fail_path_scheduler(
            tmp_path, processed_count=REPOS_COMPLETED_PARTIAL - 1
        )

        scheduler._maybe_fail_backfill_job(alias=_ALIAS_A)

        progress_values = _extract_progress_values(tracker)
        assert EXPECTED_PROGRESS_PARTIAL in progress_values, (
            f"Expected progress={EXPECTED_PROGRESS_PARTIAL} in update_status calls. "
            f"Got: {progress_values}"
        )

    def test_update_status_not_called_when_all_repos_processed_terminal_path(
        self, tmp_path
    ):
        """
        When processed == cluster_wide_total after increment, fail_job is called
        (terminal path) and update_status must NOT be called — the terminal
        transition itself conveys completion.
        """
        # REPOS_TOTAL - 1 pre-set; method increments to REPOS_TOTAL (terminal)
        scheduler, tracker = _make_fail_path_scheduler(
            tmp_path, processed_count=REPOS_TOTAL - 1
        )

        scheduler._maybe_fail_backfill_job(alias=_ALIAS_D)

        assert tracker.update_status.call_count == 0, (
            "update_status must NOT be called on the terminal fail_job path. "
            f"Called {tracker.update_status.call_count} times."
        )
