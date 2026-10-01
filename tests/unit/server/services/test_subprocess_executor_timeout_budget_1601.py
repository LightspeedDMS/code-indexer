"""Issue #1601 remediation round 5, Priority 2 (REQUIRED -- Codex High
finding, concerns code #1601 is actively modifying).

``execute_with_limits`` wraps the synchronous ``_run_subprocess`` call in
``asyncio.wait_for(..., timeout=timeout_seconds + 1)``. That flat "+1s"
slack does not cover the FULL worst-case synchronous termination/cleanup
budget ``_wait_with_output_cap`` can spend after its own internal
deadline is reached:

- The natural-EOF reap wait (``_PROCESS_EXIT_GRACE_SECONDS`` = 5s) can
  itself time out.
- That escalates through ``_terminate_process``'s full SIGTERM
  (``_TERMINATE_GRACE_SECONDS`` = 2s) then SIGKILL
  (``_KILL_GRACE_SECONDS`` = 5s) grace periods.

Worst case: 5 + 2 + 5 = 12 seconds of synchronous cleanup AFTER
``_wait_with_output_cap``'s own deadline is reached -- twelve times the
"+1s" the outer ``asyncio.wait_for`` currently budgets. If the outer
deadline is shorter than this, ``asyncio.wait_for`` can time out and
return control to the caller while the synchronous termination sequence
is STILL RUNNING in the thread pool underneath -- exactly the class of
bug this project's "never call sync work directly on the event loop
without a properly-sized deadline" invariant exists to prevent.

This test proves, behaviorally (not by reading source), that the actual
``timeout`` value passed to ``asyncio.wait_for`` for a given
``timeout_seconds`` covers the full worst-case cleanup budget derived
from the THREE real grace-period constants -- not a flat "+1s".
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from code_indexer.server.services.subprocess_executor import (
    SubprocessExecutor,
    _PROCESS_EXIT_GRACE_SECONDS,
)

_TEST_TIMEOUT_SECONDS = 100


class TestOuterTimeoutCoversTerminationBudget:
    """Priority 2: the outer async deadline must budget for the full
    worst-case synchronous termination/cleanup sequence, not a flat
    margin."""

    @pytest.mark.asyncio
    async def test_outer_wait_for_timeout_covers_worst_case_cleanup_budget(
        self, tmp_path
    ):
        captured: dict = {}
        real_wait_for = asyncio.wait_for

        async def _capture_wait_for(aw, timeout):
            captured["timeout"] = timeout
            return await real_wait_for(aw, timeout=timeout)

        executor = SubprocessExecutor(max_workers=1)
        try:
            with patch(
                "code_indexer.server.services.subprocess_executor.asyncio.wait_for",
                side_effect=_capture_wait_for,
            ):
                await executor.execute_with_limits(
                    command=["python3", "-c", "print('hi')"],
                    working_dir=str(tmp_path),
                    timeout_seconds=_TEST_TIMEOUT_SECONDS,
                    output_file_path=str(tmp_path / "out.txt"),
                )
        finally:
            executor.shutdown(wait=True)

        assert "timeout" in captured, "asyncio.wait_for was never called"

        worst_case_cleanup_seconds = (
            _PROCESS_EXIT_GRACE_SECONDS
            + SubprocessExecutor._TERMINATE_GRACE_SECONDS
            + SubprocessExecutor._KILL_GRACE_SECONDS
        )
        minimum_required_timeout = _TEST_TIMEOUT_SECONDS + worst_case_cleanup_seconds
        assert captured["timeout"] >= minimum_required_timeout, (
            f"outer asyncio.wait_for timeout ({captured['timeout']}s) does "
            f"not cover timeout_seconds ({_TEST_TIMEOUT_SECONDS}s) plus the "
            f"full worst-case termination/cleanup budget "
            f"({worst_case_cleanup_seconds}s = "
            f"_PROCESS_EXIT_GRACE_SECONDS + _TERMINATE_GRACE_SECONDS + "
            f"_KILL_GRACE_SECONDS) -- asyncio.wait_for could time out and "
            f"return while synchronous cleanup is still running in the "
            f"thread pool"
        )
