"""
Source inspection tests for Bug #381: repair job status transition fixes.

Verifies three surgical changes to the repair job pipeline in
dependency_map_routes.py:

1. After register_job(), the job transitions from pending→running via
   job_tracker.update_status(job_id, status="running").

2. The update_tracking(status="running") call includes error_message=None
   to clear any stale error from a prior orphaned run.

3. The finally block's update_tracking call includes error_message handling:
   None on success, "Repair failed" on failure.

Note (Story #927 Pass 2): status-tracking logic was extracted from
_run_repair_with_feedback into _execute_repair_body. Tests scan the
combined source of both functions so the assertions remain valid.
"""

import inspect


def _get_repair_source() -> str:
    """Return combined source of _run_repair_with_feedback and _execute_repair_body.

    After Story #927 Pass 2, status-tracking logic lives in _execute_repair_body.
    _run_repair_with_feedback is the registration wrapper that calls into it.
    Scanning both functions together preserves the logical ordering invariant:
    register_job (wrapper) precedes update_status (body) in the combined text.
    """
    from code_indexer.server.web import dependency_map_routes

    wrapper_source = inspect.getsource(dependency_map_routes._run_repair_with_feedback)
    body_source = inspect.getsource(dependency_map_routes._execute_repair_body)
    return wrapper_source + body_source


class TestRepairJobStatusTransition:
    """Verify Bug #381 fixes are present across the repair job pipeline source."""

    def test_repair_registers_then_transitions_to_running(self):
        """
        _run_repair_with_feedback must call register_job AND then
        update_status(job_id, status="running") — matching the pattern
        used by full/delta/refinement dep map operations.

        After Story #927 Pass 2, register_job lives in _run_repair_with_feedback
        and update_status lives in _execute_repair_body. The combined source
        preserves the ordering invariant.
        """
        combined = _get_repair_source()

        assert "register_job(" in combined, (
            "_run_repair_with_feedback must call job_tracker.register_job()"
        )
        assert 'update_status(job_id, status="running")' in combined, (
            "repair pipeline must call job_tracker.update_status("
            'job_id, status="running") after register_job to transition '
            "from pending to running (Bug #381)"
        )

        # update_status must appear after register_job in the combined source.
        # _run_repair_with_feedback (wrapper, has register_job) is prepended,
        # so its text always precedes _execute_repair_body (body, has update_status).
        register_pos = combined.index("register_job(")
        update_pos = combined.index('update_status(job_id, status="running")')
        assert update_pos > register_pos, (
            "update_status(status='running') must come AFTER register_job() "
            "in the repair pipeline (Bug #381)"
        )

    def test_repair_clears_error_on_running(self):
        """
        The update_tracking(status="running") call must include
        error_message=None to clear stale errors from prior orphaned runs.
        """
        combined = _get_repair_source()

        assert 'update_tracking(status="running", error_message=None)' in combined, (
            'repair pipeline must call update_tracking(status="running", '
            "error_message=None) to clear stale error messages (Bug #381)"
        )

    def test_repair_sets_error_on_failure(self):
        """
        The update_tracking call must set error_message=None
        on success and error_message="Repair failed" on failure.
        """
        combined = _get_repair_source()

        assert "error_message=None if success else" in combined, (
            "repair pipeline update_tracking must use "
            "'error_message=None if success else ...' pattern (Bug #381)"
        )
        assert '"Repair failed"' in combined, (
            'repair pipeline must set error_message="Repair failed" on failure (Bug #381)'
        )
