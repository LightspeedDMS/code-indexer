"""
Unit tests for cooperative cancellation wiring in ActivatedRepoIndexManager
(Bug #1342).

Cancelling a running activation job used to be a no-op while the worker was
blocked inside the `cidx index` branch-delta reindex subprocess. These tests
prove:

1. `_run_subprocess_with_telemetry` accepts a `cancel_check` callable and,
   when it fires, kills the REAL child process group promptly (no process
   mocks — a real `bash` subprocess is spawned and observed).
2. Bug #1218 is preserved: with no cancellation requested, a subprocess that
   legitimately runs across several poll intervals is NOT killed by any
   implicit wall-clock ceiling.
3. `cancel_check` is correctly threaded through the call chain
   `run_branch_delta_index` -> `_execute_semantic_indexing` ->
   `_run_subprocess_with_telemetry` (wiring/forwarding tests — legitimate to
   assert via method-boundary mocks, since this is testing parameter
   plumbing between methods on the same class, not subprocess behavior).
"""

import tempfile
import time
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)
from code_indexer.server.utils.cancellable_subprocess import (
    SubprocessCancelledError,
)


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def index_manager(temp_data_dir):
    mock_bg = Mock()
    mock_arm = Mock()
    return ActivatedRepoIndexManager(
        data_dir=temp_data_dir,
        background_job_manager=mock_bg,
        activated_repo_manager=mock_arm,
    )


class TestRunSubprocessWithTelemetryRealCancellation:
    """Real-subprocess cancellation coverage for the reindex subprocess helper."""

    def test_cancel_check_kills_real_subprocess_promptly(self, index_manager):
        start = time.monotonic()
        with pytest.raises(SubprocessCancelledError):
            index_manager._run_subprocess_with_telemetry(
                ["bash", "-c", "sleep 30"],
                "/tmp",
                cancel_check=lambda: True,
                poll_interval=0.05,
            )
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, f"cancellation took {elapsed:.2f}s, expected < 5s"

    def test_no_cancel_requested_process_completes_normally(self, index_manager):
        result = index_manager._run_subprocess_with_telemetry(
            ["bash", "-c", "echo real-output-1342"],
            "/tmp",
            cancel_check=lambda: False,
        )
        assert result.returncode == 0
        assert "real-output-1342" in result.stdout

    def test_bug_1218_no_wall_clock_ceiling_when_never_cancelled(self, index_manager):
        """A subprocess that runs across many poll intervals must complete
        naturally — no hardcoded per-job/subprocess timeout is ever applied."""
        result = index_manager._run_subprocess_with_telemetry(
            ["bash", "-c", "sleep 0.3; echo survived-1218"],
            "/tmp",
            cancel_check=lambda: False,
            poll_interval=0.02,
        )
        assert result.returncode == 0
        assert "survived-1218" in result.stdout


class TestCancelCheckForwardingWiring:
    """Parameter-plumbing tests: cancel_check must reach the subprocess call
    from the public run_branch_delta_index entrypoint."""

    def test_execute_semantic_indexing_forwards_cancel_check(self, index_manager):
        sentinel_cancel_check = Mock(return_value=False)
        with patch.object(
            index_manager,
            "_run_subprocess_with_telemetry",
            return_value=Mock(returncode=0, stdout="", stderr=""),
        ) as mock_run:
            index_manager._execute_semantic_indexing(
                "/tmp/repo", False, cancel_check=sentinel_cancel_check
            )
        _, kwargs = mock_run.call_args
        assert kwargs.get("cancel_check") is sentinel_cancel_check

    def test_run_branch_delta_index_forwards_cancel_check(self, index_manager):
        sentinel_cancel_check = Mock(return_value=False)
        with patch.object(
            index_manager,
            "_execute_semantic_indexing",
            return_value={"success": True, "message": "ok"},
        ) as mock_exec:
            index_manager.run_branch_delta_index(
                "/tmp/repo", cancel_check=sentinel_cancel_check
            )
        args, kwargs = mock_exec.call_args
        assert kwargs.get("cancel_check") is sentinel_cancel_check

    def test_run_branch_delta_index_cancellation_propagates_as_runtime_error(
        self, index_manager
    ):
        """When the reindex subprocess is cancelled, run_branch_delta_index
        must still surface a RuntimeError (its documented contract) so the
        caller (_run_branch_delta_index in ActivatedRepoManager) can wrap it
        into ActivatedRepoError and trigger existing cleanup."""
        with patch.object(
            index_manager,
            "_run_subprocess_with_telemetry",
            side_effect=SubprocessCancelledError("cancelled"),
        ):
            with pytest.raises(RuntimeError):
                index_manager.run_branch_delta_index(
                    "/tmp/repo", cancel_check=lambda: True
                )


class TestCancellationIdentityPreservedBug1346:
    """Bug #1346: ActivatedRepoManager._run_branch_delta_index distinguishes a
    user cancel from a genuine failure via `isinstance(exc,
    SubprocessCancelledError)`. That only works if the SubprocessCancelledError
    raised deep inside `_run_subprocess_with_telemetry` survives
    `_execute_semantic_indexing`'s broad `except Exception` (which used to
    swallow it into a plain result dict, losing the type) and
    `run_branch_delta_index`'s re-raise unscathed."""

    def test_run_branch_delta_index_cancellation_preserves_subprocess_cancelled_identity(
        self, index_manager
    ):
        with patch.object(
            index_manager,
            "_run_subprocess_with_telemetry",
            side_effect=SubprocessCancelledError("cidx index cancelled"),
        ):
            with pytest.raises(SubprocessCancelledError):
                index_manager.run_branch_delta_index(
                    "/tmp/repo", cancel_check=lambda: True
                )
