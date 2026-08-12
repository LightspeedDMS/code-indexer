"""
Unit tests for Bug #1565: the "Duplicate active job rejected by database"
log line must not flood WARNING on the by-design single-flight path.

``BackgroundJobsPostgresBackend.save_job()`` logs a WARNING whenever the
INSERT violates the ``idx_active_job_per_repo`` partial unique index. Two
call chains reach this exact log statement:

1. ``atomic_claim_insert()`` -- called EXCLUSIVELY by
   ``JobTracker._atomic_insert_impl()``, which is called EXCLUSIVELY by
   ``JobTracker._atomic_insert_or_raise()``, which is called EXCLUSIVELY by
   ``JobTracker.register_job_if_no_conflict()``. Every real caller of
   ``register_job_if_no_conflict`` (schedulers, MCP handlers, REST routers --
   audited via `grep -rn "except DuplicateJobError"`) CATCHES the resulting
   ``DuplicateJobError`` and treats it as an expected, handled no-op (skip
   this tick / return HTTP 409 / log at DEBUG and move on) -- never an
   unhandled crash. This is the designed cross-node single-flight guard
   SUCCEEDING, not a problem. On the measured staging cluster this fired
   1,543+126+36 times in 24h -- ~80% of all WARNING volume.
2. ``save_job()`` called DIRECTLY (``JobTracker.register_job()`` ->
   ``_insert_job()``, and the one-time ``migrate_background_jobs()`` JSON
   import in ``migration_service.py``) -- NEITHER of these callers expects
   or tolerates a duplicate; a violation here is a genuine anomaly (a
   caller-generated job_id collision, or an operation_type/repo_alias
   collision on a path that was never meant to race). This path's WARNING
   must be UNCHANGED.

Per the bug's "move/parameterize the logging so the CALLER decides
severity" guidance: ``save_job()`` gains an optional
``duplicate_violation_log_level`` keyword (default ``logging.WARNING``,
preserving every existing direct caller's behavior byte-for-byte).
``atomic_claim_insert()`` -- the exclusively-by-design path -- passes
``logging.DEBUG``.

Acceptance criteria:
AC1: atomic_claim_insert()'s duplicate-violation log is emitted BELOW
     WARNING (DEBUG), and the original exception still propagates
     unchanged (control flow untouched).
AC2: save_job() called directly (the register_job()/migration path) still
     logs the duplicate-violation at WARNING, unchanged.
"""

from __future__ import annotations

import logging

from unittest.mock import MagicMock


class _FakeUniqueViolation(Exception):
    """Mimics psycopg.errors.UniqueViolation by class name only.

    Both ``background_jobs_backend.save_job``'s except-clause classifier
    (``"UniqueViolation" in type(exc).__name__``) and
    ``job_tracker.is_active_job_unique_violation`` classify a violation by
    exception CLASS NAME substring only -- no real psycopg import is
    needed to exercise this branch faithfully.
    """

    sqlstate = "23505"


def _make_pool_raising_on_insert():
    cur = MagicMock()
    cur.execute.side_effect = _FakeUniqueViolation("duplicate key value")

    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    pool = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    return pool, conn, cur


_LOGGER_NAME = "code_indexer.server.storage.postgres.background_jobs_backend"


class TestAtomicClaimInsertDuplicateLogLevel:
    """AC1: the by-design single-flight path (atomic_claim_insert) must not
    log its expected duplicate rejection at WARNING."""

    def test_atomic_claim_insert_duplicate_logs_below_warning(self, caplog) -> None:
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, _conn, _cur = _make_pool_raising_on_insert()
        backend = BackgroundJobsPostgresBackend(pool)

        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            try:
                backend.atomic_claim_insert(
                    job_id="job-dup-1",
                    operation_type="fleet_migration",
                    status="pending",
                    created_at="2026-01-01T00:00:00+00:00",
                    username="system",
                    progress=0,
                    repo_alias="fleet-migration-scheduler",
                )
                raised = False
            except _FakeUniqueViolation:
                raised = True

        assert raised, (
            "AC1 control-flow guard: atomic_claim_insert() must still "
            "propagate the unique-violation exception unchanged -- only "
            "the log severity may change."
        )

        dup_records = [
            r
            for r in caplog.records
            if "Duplicate active job rejected" in r.getMessage()
        ]
        assert dup_records, (
            "Expected a 'Duplicate active job rejected' log record, found "
            f"none. All records: {[r.getMessage() for r in caplog.records]}"
        )
        offending = [r for r in dup_records if r.levelno >= logging.WARNING]
        assert not offending, (
            "AC1: atomic_claim_insert()'s duplicate-violation log (the "
            "by-design, exclusively register_job_if_no_conflict-driven "
            "single-flight path -- every real caller catches "
            "DuplicateJobError and treats it as an expected no-op) must "
            f"log BELOW WARNING, but found: {[r.levelname for r in offending]}"
        )


class TestSaveJobDirectDuplicateLogLevelUnchanged:
    """AC2: the direct save_job() path (register_job()/migration_service.py
    -- neither caller expects or tolerates a duplicate) must keep logging
    the violation at WARNING."""

    def test_save_job_direct_duplicate_still_logs_at_warning(self, caplog) -> None:
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, _conn, _cur = _make_pool_raising_on_insert()
        backend = BackgroundJobsPostgresBackend(pool)

        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            try:
                backend.save_job(
                    job_id="job-dup-2",
                    operation_type="dep_map_analysis",
                    status="pending",
                    created_at="2026-01-01T00:00:00+00:00",
                    username="alice",
                    progress=0,
                    repo_alias="some-repo",
                )
                raised = False
            except _FakeUniqueViolation:
                raised = True

        assert raised, (
            "AC2 control-flow guard: save_job() must still propagate the "
            "unique-violation exception unchanged."
        )

        dup_records = [
            r
            for r in caplog.records
            if "Duplicate active job rejected" in r.getMessage()
        ]
        assert dup_records, (
            "Expected a 'Duplicate active job rejected' log record, found "
            f"none. All records: {[r.getMessage() for r in caplog.records]}"
        )
        offending = [r for r in dup_records if r.levelno < logging.WARNING]
        assert not offending, (
            "AC2: save_job() called directly (register_job()/"
            "migration_service.py -- neither tolerates a duplicate) must "
            "keep logging the violation at WARNING (unchanged), but found: "
            f"{[r.levelname for r in offending]}"
        )
