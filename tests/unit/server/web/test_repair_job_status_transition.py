"""
Source inspection tests for Bug #381: repair job status transition fixes.

Verifies three surgical changes to _run_repair_with_feedback in
dependency_map_routes.py:

1. After register_job(), the job transitions from pending→running via
   job_tracker.update_status(job_id, status="running").

2. The update_tracking(status="running") call includes error_message=None
   to clear any stale error from a prior orphaned run.

3. The finally block's update_tracking call includes error_message handling:
   None on success, "Repair failed" on failure.
"""

import inspect


def _get_repair_source() -> str:
    """Return source of _run_repair_with_feedback."""
    from code_indexer.server.web import dependency_map_routes

    return inspect.getsource(dependency_map_routes._run_repair_with_feedback)


class TestRepairJobStatusTransition:
    """Verify Bug #381 fixes are present in _run_repair_with_feedback source."""

    def test_repair_registers_then_transitions_to_running(self):
        """
        _run_repair_with_feedback must call register_job AND then
        update_status(job_id, status="running") — matching the pattern
        used by full/delta/refinement dep map operations.
        """
        source = _get_repair_source()

        assert (
            "register_job(" in source
        ), "_run_repair_with_feedback must call job_tracker.register_job()"
        assert 'update_status(job_id, status="running")' in source, (
            "_run_repair_with_feedback must call job_tracker.update_status("
            'job_id, status="running") after register_job to transition '
            "from pending to running (Bug #381)"
        )

        # update_status must appear after register_job in the source
        register_pos = source.index("register_job(")
        update_pos = source.index('update_status(job_id, status="running")')
        assert update_pos > register_pos, (
            "update_status(status='running') must come AFTER register_job() "
            "in _run_repair_with_feedback"
        )

    def test_repair_clears_error_on_running(self):
        """
        The update_tracking(status="running") call must include
        error_message=None to clear stale errors from prior orphaned runs.
        """
        source = _get_repair_source()

        assert 'update_tracking(status="running", error_message=None)' in source, (
            '_run_repair_with_feedback must call update_tracking(status="running", '
            "error_message=None) to clear stale error messages (Bug #381)"
        )

    def test_repair_sets_error_on_failure(self):
        """
        The finally block's update_tracking call must set error_message=None
        on success and error_message="Repair failed" on failure.
        """
        source = _get_repair_source()

        assert "error_message=None if success else" in source, (
            "finally block update_tracking must use "
            "'error_message=None if success else ...' pattern (Bug #381)"
        )
        assert (
            '"Repair failed"' in source
        ), 'finally block must set error_message="Repair failed" on failure (Bug #381)'
