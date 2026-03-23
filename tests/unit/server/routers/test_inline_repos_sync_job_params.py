"""Unit tests for Bug #479: TypeError in sync_job_wrapper when BackgroundJobManager
passes unexpected 'params' kwarg.

Bug: TypeError: sync_job_wrapper() got an unexpected keyword argument 'params'
Root cause: inline_repos.py:889 passes params={"repo_id": cleaned_repo_id} to
submit_job(). BackgroundJobManager forwards all **kwargs to the wrapped function,
but sync_job_wrapper() is a zero-argument closure that does not accept 'params'.

Fix: Remove the params= kwarg from the submit_job() call. The repo_id is already
captured in the sync_job_wrapper closure and tracked via repo_alias kwarg.
"""

import pathlib
import time
import pytest
from unittest.mock import patch
from code_indexer.server.repositories.background_jobs import BackgroundJobManager

# Named constants for sleep durations used in async job polling
FIXTURE_CLEANUP_WAIT_SEC = 0.1
JOB_EXECUTION_WAIT_SEC = 0.3
JOB_COMPLETION_WAIT_SEC = 0.5


@pytest.fixture
def job_manager():
    """Create a BackgroundJobManager for testing."""
    manager = BackgroundJobManager()
    yield manager
    # Allow background threads to complete
    time.sleep(FIXTURE_CLEANUP_WAIT_SEC)


def make_zero_arg_wrapper():
    """Create a zero-argument wrapper function simulating sync_job_wrapper."""
    captured_repo_id = "test-repo-123"  # Simulating closure capture

    def sync_job_wrapper():
        """Zero-arg wrapper that captures repo_id in closure."""
        return {"status": "completed", "repo_id": captured_repo_id}

    return sync_job_wrapper


class TestSyncJobWrapperParamsKwarg:
    """Test Bug #479: params kwarg must not be forwarded to zero-arg wrapper."""

    def test_submit_job_with_params_kwarg_causes_job_failure(self, job_manager):
        """Bug #479: submit_job with params= kwarg causes TypeError in zero-arg wrapper.

        When params= is passed to submit_job(), BackgroundJobManager forwards it
        to the function as a kwarg. A zero-arg sync_job_wrapper() rejects it with
        TypeError: sync_job_wrapper() got an unexpected keyword argument 'params'.
        The BackgroundJobManager catches the exception and marks the job as failed.
        """
        wrapper = make_zero_arg_wrapper()

        with patch(
            "code_indexer.server.services.maintenance_service.get_maintenance_state"
        ) as mock_maint:
            mock_maint.return_value.is_maintenance_mode.return_value = False

            # Submit job WITH params kwarg (the buggy call pattern)
            job_id = job_manager.submit_job(
                "sync_repository",
                wrapper,
                params={"repo_id": "test-repo-123"},  # This is the buggy pattern
                submitter_username="testuser",
                repo_alias="test-repo-123",
            )

        assert job_id is not None

        # Wait for job to execute and fail
        time.sleep(JOB_EXECUTION_WAIT_SEC)

        # The job should have FAILED because params= was forwarded to zero-arg wrapper
        # BackgroundJobManager catches TypeError and marks job as failed
        with job_manager._lock:
            job = job_manager.jobs.get(job_id)

        # Job should still be in memory (failed jobs remain until retrieved)
        assert job is not None, (
            "Job should be in memory after failure (BackgroundJobManager keeps failed jobs)"
        )
        assert job.status.value == "failed", (
            f"Expected job status 'failed' due to TypeError from unexpected 'params' kwarg, "
            f"but got '{job.status.value}'"
        )
        assert job.error is not None, "Failed job should have an error message"
        assert "params" in job.error.lower() or "unexpected keyword" in job.error.lower(), (
            f"Expected TypeError about 'params' kwarg in error message, got: {job.error}"
        )

    def test_submit_job_without_params_kwarg_succeeds(self, job_manager):
        """Fix verification: removing params= from submit_job() call allows wrapper to succeed."""
        wrapper = make_zero_arg_wrapper()

        with patch(
            "code_indexer.server.services.maintenance_service.get_maintenance_state"
        ) as mock_maint:
            mock_maint.return_value.is_maintenance_mode.return_value = False

            # Submit job WITHOUT params kwarg (the fixed call pattern)
            job_id = job_manager.submit_job(
                "sync_repository",
                wrapper,
                # NO params= kwarg - this is the fix
                submitter_username="testuser",
                repo_alias="test-repo-123",
            )

        assert job_id is not None

        # Wait for job to complete
        time.sleep(JOB_COMPLETION_WAIT_SEC)

        # The job should complete successfully without TypeError
        with job_manager._lock:
            job = job_manager.jobs.get(job_id)

        # Job should be in memory as completed
        assert job is not None, "Completed job should be in memory"
        assert job.status.value == "completed", (
            f"Expected 'completed' but got '{job.status.value}' - "
            f"possible TypeError from unexpected kwarg. Error: {job.error}"
        )

    def test_zero_arg_function_fails_when_called_with_params_kwarg(self):
        """Unit test: directly verifying that zero-arg function fails with params= kwarg.

        This is the core of Bug #479: sync_job_wrapper() takes no arguments,
        so calling it with params= raises TypeError.
        """
        wrapper = make_zero_arg_wrapper()

        # Direct call without kwargs - should succeed
        result = wrapper()
        assert result == {"status": "completed", "repo_id": "test-repo-123"}

        # Direct call with params kwarg - should raise TypeError (documenting the bug)
        with pytest.raises(TypeError, match="unexpected keyword argument 'params'"):
            wrapper(params={"repo_id": "test-repo-123"})

    def test_zero_arg_function_succeeds_without_extra_kwargs(self):
        """Regression: zero-arg wrapper must succeed when called with no kwargs."""
        wrapper = make_zero_arg_wrapper()

        # Should succeed without any kwargs
        result = wrapper()
        assert result is not None
        assert result["status"] == "completed"
        assert result["repo_id"] == "test-repo-123"


class TestInlineReposSyncJobSubmitCall:
    """Test that the inline_repos router submit_job call does not pass params=."""

    def test_submit_job_call_has_no_params_kwarg(self):
        """Bug #479 fix: inline_repos.py submit_job call must NOT include params=.

        After the fix, the submit_job call should look like:
            job_id = background_job_manager.submit_job(
                "sync_repository",
                sync_job_wrapper,
                submitter_username=current_user.username,
                repo_alias=cleaned_repo_id,
            )
        Without the params={"repo_id": cleaned_repo_id} line.
        """
        # Read the inline_repos.py source
        source_path = pathlib.Path(
            "src/code_indexer/server/routers/inline_repos.py"
        )
        source = source_path.read_text()
        lines = source.splitlines()

        # Find the submit_job call block that follows sync_job_wrapper definition
        in_submit_block = False
        submit_start_line = None
        for i, line in enumerate(lines):
            if "submit_job(" in line:
                in_submit_block = True
                submit_start_line = i
            if in_submit_block and "sync_job_wrapper" in line:
                # We're in the right submit_job block - check for params=
                block_lines = []
                for j in range(
                    max(0, submit_start_line - 2),
                    min(len(lines), submit_start_line + 10),
                ):
                    block_lines.append(lines[j])
                block_text = "\n".join(block_lines)

                assert 'params={"repo_id"' not in block_text, (
                    f"Bug #479 not fixed: params= kwarg still present in submit_job "
                    f"call near line {submit_start_line + 1}:\n{block_text}"
                )
                in_submit_block = False
                break
