"""
Unit tests for Bug #952: update_status WARNING flood on cleared jobs.

Bug #952: JobTracker.update_status logs WARNING every time it is called for a
job that has already been removed from _active_jobs.  Producers (background
workers) keep calling update_status for a short window after a job completes
and is cleared — this is normal concurrent behaviour, not an error.

Fix: downgrade the WARNING at line 439 to DEBUG so operators are not flooded.

Acceptance criteria:
  AC1 - update_status on a completed/cleared job: no WARNING, returns None.
  AC2 - update_status on a failed/cleared job: no WARNING, returns None.
  AC3 - update_status on a completely unknown job_id: no WARNING, returns None.
  AC4 - update_status on an unknown job_id does emit a DEBUG entry (info
        preserved for debug sessions, just not polluting operator logs).
"""

import logging


def _assert_no_warning(caplog) -> None:
    """Raise AssertionError if any WARNING (or higher) records exist."""
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [], (
        f"Expected no WARNING entries, got: {[r.message for r in warnings]}"
    )


class TestUpdateStatusClearedJobNoWarning:
    """AC1 + AC2: update_status on a cleared job must not emit WARNING."""

    def test_update_status_cleared_job_no_warning(self, tracker, caplog):
        """
        Calling update_status for a completed (cleared) job must not emit
        a WARNING and must return None gracefully.

        Given a registered job that has been completed (removed from memory)
        When update_status is called for that job_id
        Then no WARNING-level entry is emitted and result is None.
        """
        tracker.register_job("job-bug952-ac1", "dep_map_analysis", "admin")
        tracker.complete_job("job-bug952-ac1")

        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.server.services.job_tracker",
        ):
            result = tracker.update_status(
                "job-bug952-ac1", status="running", progress=50
            )

        _assert_no_warning(caplog)
        assert result is None

    def test_update_status_failed_job_no_warning(self, tracker, caplog):
        """
        Calling update_status for a failed (cleared) job must not emit
        a WARNING and must return None gracefully.

        Given a registered job that has been failed (removed from memory)
        When update_status is called for that job_id
        Then no WARNING-level entry is emitted and result is None.
        """
        tracker.register_job("job-bug952-ac2", "dep_map_analysis", "admin")
        tracker.fail_job("job-bug952-ac2", error="intentional failure")

        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.server.services.job_tracker",
        ):
            result = tracker.update_status("job-bug952-ac2", progress=75)

        _assert_no_warning(caplog)
        assert result is None


class TestUpdateStatusUnknownJobNoWarning:
    """AC3 + AC4: update_status on an unknown job_id."""

    def test_update_status_unknown_job_no_warning(self, tracker, caplog):
        """
        Calling update_status for a job_id never registered must not emit
        a WARNING and must return None gracefully.

        Given a job_id that was never registered
        When update_status is called
        Then no WARNING-level entry is emitted and result is None.
        """
        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.server.services.job_tracker",
        ):
            result = tracker.update_status("job-never-existed-952", status="running")

        _assert_no_warning(caplog)
        assert result is None

    def test_update_status_unknown_job_emits_debug(self, tracker, caplog):
        """
        Calling update_status for an unknown job_id SHOULD emit a DEBUG entry
        so the information is available when debug logging is enabled.

        Given a job_id that was never registered
        When update_status is called with DEBUG logging enabled
        Then at least one DEBUG entry mentioning the job_id is present.
        """
        job_id = "job-debug-check-952"
        with caplog.at_level(
            logging.DEBUG,
            logger="code_indexer.server.services.job_tracker",
        ):
            tracker.update_status(job_id, status="running")

        debug_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and job_id in r.getMessage()
        ]
        assert debug_records, (
            "Expected at least one DEBUG entry mentioning the job_id, got none"
        )
