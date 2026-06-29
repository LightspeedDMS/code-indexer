"""
Cross-worker dedup tests for Bug #1184.

DataRetentionScheduler and ActivatedReaperScheduler must NOT surface benign
cross-worker/cross-node dedup collisions as ERROR-level logs. The DuplicateJobError
must be caught BEFORE any generic except Exception handler, logged at DEBUG
("already claimed by another worker; skipping"), and must NOT dispatch a second
job or thread.

Tests use REAL SQLite (via DatabaseSchema.initialize_database()) + REAL
JobTracker/BackgroundJobManager dedup — no mocks for the dedup mechanism.

Pattern mirrors tests/unit/server/services/test_description_refresh_circuit_breaker_1096.py
(Story #1162 cross-worker dedup tests) for TestCrossWorkerDedup1162.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from code_indexer.server.storage.database_manager import DatabaseSchema


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _DeferringBGM:
    """
    BackgroundJobManager stand-in that defers the worker function
    (never actually runs it) but performs real dedup via the real
    JobTracker passed in at construction time.

    This keeps the first registered job in 'pending' state in the DB
    while the second scheduler attempts to claim the same operation_type +
    repo_alias — exactly the multi-worker race window.

    Translates job_tracker.DuplicateJobError -> background_jobs.DuplicateJobError
    to mirror the real BGM's translation (Bug #1065).
    """

    def __init__(self, job_tracker: Any, operation_type: str, repo_alias: str) -> None:
        self._job_tracker = job_tracker
        self._operation_type = operation_type
        self._repo_alias = repo_alias
        self.submitted_count = 0
        self.queued: list = []

    def submit_job(
        self,
        operation_type: str,
        func,
        *args,
        submitter_username: str,
        is_admin: bool = False,
        repo_alias: Optional[str] = None,
        **kwargs,
    ) -> str:
        from code_indexer.server.repositories.background_jobs import (
            DuplicateJobError as BGMDuplicateJobError,
        )
        from code_indexer.server.services.job_tracker import (
            DuplicateJobError as TrackerDuplicateJobError,
        )

        job_id = str(uuid.uuid4())

        try:
            # Use register_job_if_no_conflict for real atomic dedup (same as BGM)
            self._job_tracker.register_job_if_no_conflict(
                job_id=job_id,
                operation_type=operation_type,
                username=submitter_username,
                repo_alias=repo_alias or self._repo_alias,
            )
        except TrackerDuplicateJobError as exc:
            # Translate to BGM's DuplicateJobError — mirrors what the real BGM does
            raise BGMDuplicateJobError(
                exc.operation_type, exc.repo_alias, exc.existing_job_id
            ) from exc

        self.submitted_count += 1
        self.queued.append((func, args, kwargs))
        return job_id

    def run_all(self) -> None:
        for fn, args, kwargs in self.queued:
            fn(*args, **kwargs)
        self.queued.clear()


class _PendingJobTracker:
    """
    Job tracker that keeps the first registered job permanently 'pending'
    in a real SQLite DB, so a second registration for the same
    (operation_type, repo_alias) pair hits the partial unique index.

    Used by DataRetentionScheduler dedup tests to simulate the race window
    where worker-2 tries to claim while worker-1's job is still pending.
    """

    def __init__(self, db_path: Path) -> None:
        from code_indexer.server.services.job_tracker import JobTracker

        DatabaseSchema(str(db_path)).initialize_database()
        self._tracker = JobTracker(str(db_path))
        self._first_job_id: Optional[str] = None

    def register_job_if_no_conflict(
        self,
        job_id: str,
        operation_type: str,
        username: str,
        repo_alias: str,
        **kwargs: Any,
    ) -> Any:
        return self._tracker.register_job_if_no_conflict(
            job_id=job_id,
            operation_type=operation_type,
            username=username,
            repo_alias=repo_alias,
        )

    def update_status(self, job_id: str, status: str) -> None:
        # Do NOT update status — keep first job permanently pending
        # so the unique index fires when the second worker tries.
        pass

    def complete_job(self, job_id: str, result: Any = None) -> None:
        # Do NOT complete — keep pending for dedup test.
        pass

    def fail_job(self, job_id: str, error: str = "") -> None:
        # Do NOT fail — keep pending for dedup test.
        pass


def _make_job_tracker(db_path: Path) -> Any:
    """Build a real JobTracker backed by a real SQLite db_path."""
    from code_indexer.server.services.job_tracker import JobTracker

    DatabaseSchema(str(db_path)).initialize_database()
    return JobTracker(str(db_path))


def _make_config_service() -> Any:
    """Minimal config_service stub for DataRetentionScheduler."""

    class _RetentionCfg:
        cleanup_interval_hours = 24
        operational_logs_retention_hours = 720
        audit_logs_retention_hours = 720
        sync_jobs_retention_hours = 720
        dep_map_history_retention_hours = 720
        background_jobs_retention_hours = 720

    class _Cfg:
        data_retention_config = _RetentionCfg()
        jwt_expiration_minutes = 10

    class _ConfigSvc:
        def get_config(self) -> Any:
            return _Cfg()

    return _ConfigSvc()


# ===========================================================================
# DataRetentionScheduler dedup tests
# ===========================================================================


class TestDataRetentionSchedulerCrossWorkerDedup1184:
    """
    Bug #1184: DataRetentionScheduler must use register_job_if_no_conflict and
    catch DuplicateJobError at DEBUG — never ERROR — when two workers race.

    Uses REAL SQLite + REAL JobTracker dedup (no mock of the DB layer).
    """

    def _build_scheduler(self, tmp_path: Path, db_path: Path, job_tracker: Any) -> Any:
        """Build a DataRetentionScheduler with real job_tracker and no-op cleanup."""
        from code_indexer.server.services.data_retention_scheduler import (
            DataRetentionScheduler,
        )

        # Use a real db_path but point to tmp files that don't have any tables
        # (cleanup will return 0 rows but that's fine for dedup testing).
        tmp_path.mkdir(parents=True, exist_ok=True)
        log_db = tmp_path / "logs.db"
        main_db = tmp_path / "main.db"
        groups_db = tmp_path / "groups.db"

        # Ensure SQLite files exist (empty is fine for our test — cleanup
        # handles "no such table" gracefully).
        for p in (log_db, main_db, groups_db):
            p.touch()

        sched = DataRetentionScheduler(
            log_db_path=log_db,
            main_db_path=main_db,
            groups_db_path=groups_db,
            config_service=_make_config_service(),
            job_tracker=job_tracker,
            storage_mode="sqlite",
        )
        return sched

    def _noop_result(self) -> dict:
        """Return a zero-rows cleanup result dict."""
        return {
            "logs_deleted": 0,
            "audit_logs_deleted": 0,
            "sync_jobs_deleted": 0,
            "dep_map_history_deleted": 0,
            "background_jobs_deleted": 0,
            "token_blacklist_deleted": 0,
            "total_deleted": 0,
            "failed_tables": [],
        }

    def _build_with_pending_tracker(self, tmp_path: Path, db_path: Path) -> Any:
        """
        Build a DataRetentionScheduler with a _PendingJobTracker.

        The _PendingJobTracker keeps the first job permanently in 'pending'
        state (never calls through update_status/complete_job), so when
        a second scheduler tries to register the same (operation_type, repo_alias)
        pair, the partial unique index idx_active_job_per_repo fires and
        DuplicateJobError is raised — exactly the multi-worker race window.
        """
        pending_tracker = _PendingJobTracker(db_path)
        return self._build_scheduler(tmp_path, db_path, pending_tracker)

    def test_second_worker_does_not_raise_or_log_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        Two schedulers sharing one DB: second _execute_cleanup must not raise
        and must not emit an ERROR log when the first job is still pending.

        Uses _PendingJobTracker to keep job 1 in 'pending' state so the
        partial unique index fires when job 2 tries to register.
        """
        import types

        db_path = tmp_path / "shared.db"

        sched1 = self._build_with_pending_tracker(tmp_path / "s1", db_path)
        sched2 = self._build_with_pending_tracker(tmp_path / "s2", db_path)

        # Patch out actual SQLite cleanup — we only care about the job-registration path.
        sched1._execute_cleanup_sqlite = types.MethodType(
            lambda s: self._noop_result(), sched1
        )
        sched2._execute_cleanup_sqlite = types.MethodType(
            lambda s: self._noop_result(), sched2
        )

        retention_logger = "code_indexer.server.services.data_retention_scheduler"
        with caplog.at_level(logging.DEBUG, logger=retention_logger):
            # sched1 runs and registers the job (stays pending via _PendingJobTracker)
            sched1._execute_cleanup()
            # sched2 tries to register the same operation_type + repo_alias
            # while sched1's job is still 'pending' — must NOT raise ERROR
            sched2._execute_cleanup()

        error_records = [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR
            and r.name == retention_logger
            and "duplicate" in r.message.lower()
        ]
        assert len(error_records) == 0, (
            f"DuplicateJobError must NOT produce ERROR log in DataRetentionScheduler. "
            f"Got: {[r.message for r in error_records]}"
        )

    def test_second_worker_logs_debug_skip(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        Second _execute_cleanup must log a DEBUG message indicating the job
        was already claimed by another worker.

        Uses _PendingJobTracker to keep job 1 pending so the duplicate is detected.
        """
        import types

        db_path = tmp_path / "shared.db"

        sched1 = self._build_with_pending_tracker(tmp_path / "s1", db_path)
        sched2 = self._build_with_pending_tracker(tmp_path / "s2", db_path)

        sched1._execute_cleanup_sqlite = types.MethodType(
            lambda s: self._noop_result(), sched1
        )
        sched2._execute_cleanup_sqlite = types.MethodType(
            lambda s: self._noop_result(), sched2
        )

        retention_logger = "code_indexer.server.services.data_retention_scheduler"
        with caplog.at_level(logging.DEBUG, logger=retention_logger):
            sched1._execute_cleanup()
            sched2._execute_cleanup()

        debug_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG
            and r.name == retention_logger
            and any(
                kw in r.message.lower()
                for kw in ("already", "claimed", "another worker", "skipping")
            )
        ]
        assert len(debug_records) >= 1, (
            f"Expected DEBUG 'already claimed' log from DataRetentionScheduler. "
            f"DEBUG records: {[r.message for r in caplog.records if r.levelno == logging.DEBUG and r.name == retention_logger]}"
        )

    def test_single_worker_executes_cleanup_once(self, tmp_path: Path) -> None:
        """Single scheduler regression: _execute_cleanup runs without raising."""
        import types

        db_path = tmp_path / "single.db"
        job_tracker = _make_job_tracker(db_path)

        sched = self._build_scheduler(tmp_path / "s1", db_path, job_tracker)

        cleanup_count = [0]

        def _counting_noop(s):
            cleanup_count[0] += 1
            return {
                "logs_deleted": 0,
                "audit_logs_deleted": 0,
                "sync_jobs_deleted": 0,
                "dep_map_history_deleted": 0,
                "background_jobs_deleted": 0,
                "token_blacklist_deleted": 0,
                "total_deleted": 0,
                "failed_tables": [],
            }

        sched._execute_cleanup_sqlite = types.MethodType(_counting_noop, sched)

        # Must not raise
        sched._execute_cleanup()

        assert cleanup_count[0] == 1, (
            f"Single scheduler must run cleanup exactly once, got {cleanup_count[0]}"
        )


# ===========================================================================
# ActivatedReaperScheduler dedup tests
# ===========================================================================


class TestActivatedReaperSchedulerCrossWorkerDedup1184:
    """
    Bug #1184: ActivatedReaperScheduler._loop must catch DuplicateJobError
    from submit_job at DEBUG — never ERROR — when two workers race.

    Uses REAL SQLite + REAL JobTracker dedup (no mock of the DB layer).
    """

    def _build_scheduler_with_deferring_bgm(
        self, db_path: Path, job_tracker: Any
    ) -> Any:
        """
        Build ActivatedReaperScheduler with a _DeferringBGM that performs
        real dedup via job_tracker but defers the worker function.
        """
        from code_indexer.server.services.activated_reaper_scheduler import (
            ActivatedReaperScheduler,
        )

        mock_service = MagicMock()
        mock_service.run_reap_cycle.return_value = MagicMock(
            scanned=0, reaped=[], skipped=[], errors=[]
        )

        bgm = _DeferringBGM(
            job_tracker=job_tracker,
            operation_type="reap_activated_repos",
            repo_alias="server",
        )

        mock_config = MagicMock()
        mock_config.get_config.return_value.activated_reaper_config.cadence_hours = 9999

        sched = ActivatedReaperScheduler(
            service=mock_service,
            background_job_manager=bgm,
            config_service=mock_config,
        )
        return sched, bgm

    def test_second_worker_trigger_now_does_not_log_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        Two schedulers sharing one DB: second trigger_now must not raise
        and must not emit an ERROR log when first job is still pending.
        """
        db_path = tmp_path / "shared.db"
        job_tracker = _make_job_tracker(db_path)

        sched1, bgm1 = self._build_scheduler_with_deferring_bgm(db_path, job_tracker)
        sched2, bgm2 = self._build_scheduler_with_deferring_bgm(db_path, job_tracker)

        reaper_logger = "code_indexer.server.services.activated_reaper_scheduler"
        with caplog.at_level(logging.DEBUG, logger=reaper_logger):
            # sched1 submits a reap job successfully (kept pending by _DeferringBGM)
            sched1.trigger_now()
            # sched2 tries same operation_type + repo_alias while first is pending
            # Must NOT raise and must NOT log ERROR
            sched2.trigger_now()

        error_records = [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR and r.name == reaper_logger
        ]
        assert len(error_records) == 0, (
            f"DuplicateJobError must NOT produce ERROR log in ActivatedReaperScheduler. "
            f"Got: {[r.message for r in error_records]}"
        )

    def test_second_worker_logs_debug_skip(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        Second trigger_now (dupe) must log a DEBUG message mentioning
        'already claimed' or similar.
        """
        db_path = tmp_path / "shared.db"
        job_tracker = _make_job_tracker(db_path)

        sched1, bgm1 = self._build_scheduler_with_deferring_bgm(db_path, job_tracker)
        sched2, bgm2 = self._build_scheduler_with_deferring_bgm(db_path, job_tracker)

        reaper_logger = "code_indexer.server.services.activated_reaper_scheduler"
        with caplog.at_level(logging.DEBUG, logger=reaper_logger):
            sched1.trigger_now()
            sched2.trigger_now()

        debug_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG
            and r.name == reaper_logger
            and any(
                kw in r.message.lower()
                for kw in (
                    "already",
                    "claimed",
                    "another worker",
                    "skipping",
                    "duplicate",
                )
            )
        ]
        assert len(debug_records) >= 1, (
            f"Expected DEBUG skip log for ActivatedReaperScheduler. "
            f"DEBUG records: {[r.message for r in caplog.records if r.levelno == logging.DEBUG and r.name == reaper_logger]}"
        )

    def test_loop_does_not_log_error_on_duplicate(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        When _loop calls trigger_now and gets DuplicateJobError,
        the error must NOT reach the generic except Exception handler.
        Simulates the multi-worker scenario at the _loop level.
        """
        db_path = tmp_path / "loop.db"
        job_tracker = _make_job_tracker(db_path)

        sched1, bgm1 = self._build_scheduler_with_deferring_bgm(db_path, job_tracker)
        sched2, bgm2 = self._build_scheduler_with_deferring_bgm(db_path, job_tracker)

        reaper_logger = "code_indexer.server.services.activated_reaper_scheduler"
        with caplog.at_level(logging.DEBUG, logger=reaper_logger):
            # Simulate _loop calling trigger_now for both schedulers
            # We call trigger_now directly to avoid starting real threads
            sched1.trigger_now()
            # This call goes through _loop's try/except — the second trigger
            # should hit DuplicateJobError; we test the _loop path by calling
            # the loop body directly (trigger_now from within _loop context).
            # Since _loop just calls trigger_now(), the dedup must be caught
            # BEFORE the generic handler.
            try:
                sched2.trigger_now()
            except Exception as exc:
                # If trigger_now raised, the fix is not in place yet —
                # the caller (_loop) would catch it as generic Exception -> ERROR
                pytest.fail(
                    f"trigger_now raised {type(exc).__name__}: {exc} — "
                    f"DuplicateJobError must be caught inside trigger_now or _loop, "
                    f"not propagated to caller."
                )

        error_records = [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR and r.name == reaper_logger
        ]
        assert len(error_records) == 0, (
            f"No ERROR must be logged for ActivatedReaperScheduler on duplicate. "
            f"Got: {[r.message for r in error_records]}"
        )

    def test_single_worker_submits_exactly_once(self, tmp_path: Path) -> None:
        """Single scheduler regression: trigger_now submits exactly one job."""
        db_path = tmp_path / "single.db"
        job_tracker = _make_job_tracker(db_path)

        sched, bgm = self._build_scheduler_with_deferring_bgm(db_path, job_tracker)

        sched.trigger_now()

        assert bgm.submitted_count == 1, (
            f"Single scheduler must submit exactly 1 job, got {bgm.submitted_count}"
        )
