"""Priority 6 (Issue #1601 remediation): SubprocessExecutor._terminate_process
must guarantee real process reaping and escalate gracefully.

Before this fix, ``_terminate_process`` called ``process.kill()``
(SIGKILL) directly, with no attempt at a graceful ``SIGTERM`` first, and
if ``kill()`` raised, or the 5s ``wait()`` timed out, the method logged an
error and returned WITHOUT guaranteeing the child was reaped -- risking a
zombie or a process still writing to an already-deleted temp file.

This test proves two things with a REAL child process (no mocking of
SubprocessExecutor's own logic; ``subprocess.Popen`` is wrapped only to
observationally capture the real child PID, still delegating to the real
implementation):

1. Graceful escalation: the child installs a real SIGTERM handler that
   writes a marker file when it receives the signal, then deliberately
   keeps running (ignoring the "please stop" request) so the executor
   must escalate to SIGKILL. The marker file's existence proves a real
   SIGTERM was sent and handled BEFORE the SIGKILL escalation -- the old
   code, which called ``kill()`` directly, would never create this file.
2. Real reaping: after the call returns, the real child PID no longer
   exists anywhere in the process table (not even as a zombie), verified
   via ``psutil.pid_exists()``.
"""

from __future__ import annotations

import os
import subprocess
from unittest.mock import patch

import psutil
import pytest

from code_indexer.server.services.subprocess_executor import SubprocessExecutor

_TEST_MAX_OUTPUT_BYTES = 8192
_EXECUTION_TIMEOUT_SECONDS = 30

# A real child that traps SIGTERM (writing a marker file to prove it was
# received and handled) but deliberately does NOT exit on it, forcing the
# executor to escalate to SIGKILL. Keeps writing stdout past the byte
# ceiling so the output-cap path triggers termination. Bounded to a
# generous but finite iteration count (~60s worst case) so the script has
# a provable termination path even if something upstream fails to kill
# it -- it is never a truly infinite loop.
_SIGTERM_TRAP_MAX_ITERATIONS = 6000
_SIGTERM_TRAP_SCRIPT = (
    "import signal, sys, time\n"
    "marker_path = sys.argv[1]\n"
    "def _handle_sigterm(signum, frame):\n"
    "    with open(marker_path, 'w') as f:\n"
    "        f.write('sigterm-received')\n"
    "signal.signal(signal.SIGTERM, _handle_sigterm)\n"
    f"for _ in range({_SIGTERM_TRAP_MAX_ITERATIONS}):\n"
    "    sys.stdout.write('x' * 4096)\n"
    "    sys.stdout.flush()\n"
    "    time.sleep(0.01)\n"
)


class TestSubprocessExecutorReaping:
    """Priority 6: graceful terminate-then-kill escalation, real reaping."""

    @pytest.mark.asyncio
    async def test_terminate_process_escalates_and_reaps_real_child(self, tmp_path):
        marker_path = str(tmp_path / "sigterm_marker.txt")
        output_path = str(tmp_path / "sigterm_output.txt")

        captured_pid: dict = {}
        real_popen = subprocess.Popen

        def _capture_popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            captured_pid["pid"] = proc.pid
            return proc

        executor = SubprocessExecutor(max_workers=1)
        try:
            with patch(
                "code_indexer.server.services.subprocess_executor.subprocess.Popen",
                side_effect=_capture_popen,
            ):
                result = await executor.execute_with_limits(
                    command=["python3", "-c", _SIGTERM_TRAP_SCRIPT, marker_path],
                    working_dir=str(tmp_path),
                    timeout_seconds=_EXECUTION_TIMEOUT_SECONDS,
                    output_file_path=output_path,
                    max_output_bytes=_TEST_MAX_OUTPUT_BYTES,
                )
        finally:
            executor.shutdown(wait=True)

        assert result.output_capped is True
        assert "pid" in captured_pid, "the real subprocess was never spawned"

        # Graceful SIGTERM was actually delivered and handled before the
        # executor escalated to SIGKILL -- proves terminate()-then-kill(),
        # not a straight-to-SIGKILL call.
        assert os.path.exists(marker_path), (
            "SIGTERM was never delivered/handled by the child -- "
            "_terminate_process must attempt a graceful terminate() "
            "before escalating to kill()"
        )

        # Real reaping: the child no longer exists anywhere in the
        # process table (not even as a zombie).
        assert not psutil.pid_exists(captured_pid["pid"]), (
            f"child pid {captured_pid['pid']} still exists after "
            f"termination -- not fully reaped"
        )
