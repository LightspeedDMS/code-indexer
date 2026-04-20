"""
Bug #851: aggregate lifecycle_backfill job never closes when ALL repos fail.

_maybe_complete_backfill_job fires from _self_close_backfill, which only runs
after a successful Phase 2 refresh (phase2_outcome == "success"). When 100% of
repos fail (as in Bug #871), no success path fires, the aggregate stays running.

Fix: add _maybe_fail_backfill_job(alias) — a failure-path sweeper that:
  - Atomically increments _backfill_processed_count.
  - When processed_count >= cluster_wide_total (from job metadata via get_job),
    calls fail_job (NOT complete_job) on the active aggregate job.
  - Clears _active_backfill_job_id using the conditional-id check pattern.
Called from _run_two_phase_task when phase2_outcome != "success".

Test classes (max 3 methods each):
  TestMaybeFailBackfillJobFiresOnLastRepo
    — fail_job called/not-called based on processed count vs total.
  TestMaybeFailBackfillJobGuards
    — no-op behavior when job_id or tracker absent or get_job returns None.
  TestMaybeFailBackfillJobDistinguishesFromSuccess
    — fail_job (not complete_job) used; _active_backfill_job_id cleared;
      fail_job receives non-empty error string.
  TestRunTwoPhaseTaskCallsFailureSweeper
    — _run_two_phase_task calls _maybe_fail_backfill_job when phase2 fails,
      exercised via mocked subprocess.run (external OS dependency).
"""

import sqlite3
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, Mock, patch

from code_indexer.global_repos.lifecycle_schema import LIFECYCLE_SCHEMA_VERSION
from code_indexer.server.services.description_refresh_scheduler import (
    DescriptionRefreshScheduler,
)
from code_indexer.server.storage.sqlite_backends import (
    DescriptionRefreshTrackingBackend,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALIAS_A = "repo-alpha"
_ALIAS_B = "repo-beta"
_BACKFILL_JOB_ID = "lifecycle-backfill-bug851-test"
_OLD_LIFECYCLE_VERSION = LIFECYCLE_SCHEMA_VERSION - 1


def _make_tracking_db(tmp_path: Path, aliases: list) -> str:
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


def _make_mock_tracker(cluster_wide_total: int):
    mock_job = Mock()
    mock_job.metadata = {"cluster_wide_total": cluster_wide_total, "processed": 0}
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
    db_path = _make_tracking_db(tmp_path, aliases)
    tracking_backend = DescriptionRefreshTrackingBackend(db_path)
    golden_backend = Mock()
    golden_backend.get_repo.return_value = None
    config_manager = Mock()
    config_manager.load_config.return_value = None

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


# ---------------------------------------------------------------------------
# Class 1: fail_job fires or not based on count vs total
# ---------------------------------------------------------------------------


class TestMaybeFailBackfillJobFiresOnLastRepo:
    """fail_job is called when processed_count reaches cluster_wide_total."""

    def test_fail_job_called_when_last_repo_fails(self, tmp_path):
        """fail_job called on aggregate when this is the last failing repo."""
        total = 1
        tracker = _make_mock_tracker(total)
        scheduler = _make_scheduler(
            tmp_path, [_ALIAS_A], tracker, processed_count=total - 1
        )

        scheduler._maybe_fail_backfill_job(_ALIAS_A)

        assert tracker.fail_job.call_count == 1, (
            f"fail_job must fire when processed_count reaches {total}. "
            f"Called {tracker.fail_job.call_count} times."
        )
        assert tracker.fail_job.call_args[0][0] == _BACKFILL_JOB_ID

    def test_fail_job_not_called_when_more_repos_remain(self, tmp_path):
        """fail_job NOT called when processed_count < cluster_wide_total."""
        total = 2
        tracker = _make_mock_tracker(total)
        scheduler = _make_scheduler(
            tmp_path, [_ALIAS_A, _ALIAS_B], tracker, processed_count=0
        )

        scheduler._maybe_fail_backfill_job(_ALIAS_A)

        assert tracker.fail_job.call_count == 0

    def test_fail_job_called_on_second_failure_of_two(self, tmp_path):
        """fail_job fires on the second failure when cluster_wide_total is 2."""
        total = 2
        tracker = _make_mock_tracker(total)
        scheduler = _make_scheduler(
            tmp_path, [_ALIAS_A, _ALIAS_B], tracker, processed_count=total - 1
        )

        scheduler._maybe_fail_backfill_job(_ALIAS_B)

        assert tracker.fail_job.call_count == 1
        assert tracker.fail_job.call_args[0][0] == _BACKFILL_JOB_ID


# ---------------------------------------------------------------------------
# Class 2: guard behavior when prerequisites are absent
# ---------------------------------------------------------------------------


class TestMaybeFailBackfillJobGuards:
    """_maybe_fail_backfill_job is a no-op when prerequisites are absent."""

    def test_no_op_when_no_active_job_id(self, tmp_path):
        """No active job_id: must not crash or call fail_job."""
        tracker = _make_mock_tracker(1)
        scheduler = _make_scheduler(
            tmp_path, [_ALIAS_A], tracker, active_job_id=None, processed_count=0
        )

        scheduler._maybe_fail_backfill_job(_ALIAS_A)

        assert tracker.fail_job.call_count == 0

    def test_no_op_when_job_tracker_is_none(self, tmp_path):
        """job_tracker=None: must not raise."""
        scheduler = _make_scheduler(tmp_path, [_ALIAS_A], tracker=None)
        scheduler._maybe_fail_backfill_job(_ALIAS_A)  # must not raise

    def test_no_op_when_get_job_returns_none(self, tmp_path):
        """get_job returns None (job already gone): no fail_job call."""
        tracker = MagicMock()
        tracker.get_job.return_value = None
        scheduler = _make_scheduler(tmp_path, [_ALIAS_A], tracker, processed_count=0)

        scheduler._maybe_fail_backfill_job(_ALIAS_A)

        assert tracker.fail_job.call_count == 0


# ---------------------------------------------------------------------------
# Class 3: failure is distinguishable from success; job_id cleared
# ---------------------------------------------------------------------------


class TestMaybeFailBackfillJobDistinguishesFromSuccess:
    """fail_job (not complete_job) used; _active_backfill_job_id cleared after."""

    def test_uses_fail_job_not_complete_job(self, tmp_path):
        """Failure path must call fail_job so status != 'completed'."""
        total = 1
        tracker = _make_mock_tracker(total)
        scheduler = _make_scheduler(
            tmp_path, [_ALIAS_A], tracker, processed_count=total - 1
        )

        scheduler._maybe_fail_backfill_job(_ALIAS_A)

        assert tracker.complete_job.call_count == 0
        assert tracker.fail_job.call_count == 1

    def test_active_backfill_job_id_cleared_after_fail_job(self, tmp_path):
        """_active_backfill_job_id must be None after fail_job."""
        total = 1
        tracker = _make_mock_tracker(total)
        scheduler = _make_scheduler(
            tmp_path, [_ALIAS_A], tracker, processed_count=total - 1
        )

        scheduler._maybe_fail_backfill_job(_ALIAS_A)

        with scheduler._backfill_job_id_lock:
            current_id = scheduler._active_backfill_job_id
        assert current_id is None

    def test_fail_job_receives_non_empty_error_string(self, tmp_path):
        """fail_job must be called with a non-empty error description."""
        total = 1
        tracker = _make_mock_tracker(total)
        scheduler = _make_scheduler(
            tmp_path, [_ALIAS_A], tracker, processed_count=total - 1
        )

        scheduler._maybe_fail_backfill_job(_ALIAS_A)

        args, kwargs = tracker.fail_job.call_args
        error_arg = kwargs.get("error") or (args[1] if len(args) > 1 else None)
        assert isinstance(error_arg, str) and len(error_arg) > 0


# ---------------------------------------------------------------------------
# Class 4: _run_two_phase_task calls failure sweeper via subprocess (external)
# ---------------------------------------------------------------------------


class TestRunTwoPhaseTaskCallsFailureSweeper:
    """
    _run_two_phase_task must call _maybe_fail_backfill_job when phase2 fails.

    External dependency: subprocess.run (OS-level call) is mocked so the
    Claude CLI and lifecycle detection calls return controlled responses.
    No SUT internal methods are patched.

    Phase 1 subprocess returns valid YAML frontmatter so execution proceeds
    to Phase 2.  Phase 2 subprocess returns content that fails YAML parse
    (bare CSI noise without valid lifecycle key), causing invoke_lifecycle_detection
    to return None — the production failure scenario from Bug #871.
    """

    def _make_scheduler_for_task(self, tmp_path: Path):
        """
        Build scheduler with meta_dir and active backfill job for one repo.

        Creates a real {alias}.md file in tmp_path with a valid last_analyzed
        field so _get_refresh_prompt can proceed past the guard check.
        """
        total = 1
        tracker = _make_mock_tracker(total)
        scheduler = _make_scheduler(
            tmp_path, [_ALIAS_A], tracker, processed_count=total - 1
        )
        scheduler._meta_dir = tmp_path
        scheduler._analysis_model = "opus"

        # Create a minimal description file so _read_existing_description succeeds
        md_file = tmp_path / f"{_ALIAS_A}.md"
        md_file.write_text(
            "---\n"
            "name: test-repo\n"
            "last_analyzed: 2026-01-01T00:00:00Z\n"
            "---\n"
            "Test repository description.\n"
        )
        return scheduler, tracker

    def test_fail_job_called_after_phase2_cli_returns_garbage(self, tmp_path):
        """
        When Phase 1 returns valid frontmatter and Phase 2 Claude CLI returns
        garbage (no lifecycle key), fail_job is called on the aggregate.

        subprocess.run is patched to return controlled responses — a legitimate
        external OS dependency mock.
        """
        scheduler, tracker = self._make_scheduler_for_task(tmp_path)

        call_responses = []

        def fake_subprocess_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if not call_responses:
                # Phase 1: valid frontmatter for repo description
                result.stdout = "---\nname: test-repo\n---\nBody text.\n"
            else:
                # Phase 2: garbage output, no lifecycle key — causes parse failure
                result.stdout = "just some text without lifecycle key\n"
            call_responses.append(result.stdout)
            return result

        with patch("subprocess.run", side_effect=fake_subprocess_run):
            scheduler._run_two_phase_task(_ALIAS_A, str(tmp_path))

        assert tracker.fail_job.call_count == 1, (
            f"fail_job must be called after Phase 2 fails to parse lifecycle. "
            f"Called {tracker.fail_job.call_count} times. "
            "Bug #851: failure-path sweeper missing from _run_two_phase_task."
        )

    def test_fail_job_not_called_when_phase1_fails(self, tmp_path):
        """
        When Phase 1 fails (CLI returns error), _run_two_phase_task returns
        early without reaching Phase 2, so fail_job is NOT called.
        """
        scheduler, tracker = self._make_scheduler_for_task(tmp_path)

        def fake_subprocess_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "Error: something went wrong"
            return result

        with patch("subprocess.run", side_effect=fake_subprocess_run):
            scheduler._run_two_phase_task(_ALIAS_A, str(tmp_path))

        assert tracker.fail_job.call_count == 0, (
            f"fail_job must NOT be called when Phase 1 fails (early return). "
            f"Called {tracker.fail_job.call_count} times."
        )
