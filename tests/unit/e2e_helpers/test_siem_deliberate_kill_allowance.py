"""Only the PID a Phase 7 test deliberately SIGKILLed may be excused.

The stall watchdog reports, on the next start, any worker that died without a
clean shutdown.  test_04 SIGKILLs its own server on purpose, so the restart
logs that WARNING for the killed PID.  That row -- and only that row -- may be
excused from the restartable-server audit; the same WARNING for any other PID,
or in any other phase (global allowlist), must still fail the audit.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Dict

from tests.e2e.log_audit_gate import LOG_AUDIT_ALLOWLIST, is_allowlisted
from tests.e2e.siem_delivery.log_allowlist import (
    PHASE7_LOG_ALLOWLIST,
    deliberately_killed_worker_allowlist,
)
from tests.e2e.siem_delivery.restartable_server import RestartableServer

KILLED_PID = 4242


def _unclean_exit_warning(pid: int) -> Dict[str, Any]:
    """The exact text stall_watchdog_sweep._keep_evidence_of_unclean_exit logs."""
    return {
        "level": "WARNING",
        "source": "code_indexer.server.utils.stall_watchdog",
        "message": (
            f"Worker stall watchdog: worker pid {pid} ended without a clean "
            "shutdown and without a stall dump (not a stalled worker: e.g. an "
            "OOM kill or a SIGKILL); its last memory samples are in "
            f"/example/logs/worker-stall-{pid}-20260101T000000000Z.log"
        ),
    }


def test_killed_pid_warning_is_excused() -> None:
    extra = PHASE7_LOG_ALLOWLIST + deliberately_killed_worker_allowlist([KILLED_PID])
    assert is_allowlisted(_unclean_exit_warning(KILLED_PID), extra)


def test_other_pid_warning_is_not_excused() -> None:
    extra = PHASE7_LOG_ALLOWLIST + deliberately_killed_worker_allowlist([KILLED_PID])
    for other in (KILLED_PID + 1, 424, 42420, 14242):
        assert not is_allowlisted(_unclean_exit_warning(other), extra), other


def test_no_kill_recorded_excuses_nothing() -> None:
    assert deliberately_killed_worker_allowlist([]) == ()
    extra = PHASE7_LOG_ALLOWLIST + deliberately_killed_worker_allowlist([])
    assert not is_allowlisted(_unclean_exit_warning(KILLED_PID), extra)


def test_other_phases_do_not_excuse_the_warning() -> None:
    """Phases 3/4/5 audit with the global list only: the WARNING still fails."""
    assert not is_allowlisted(_unclean_exit_warning(KILLED_PID))
    assert not any("ended without a clean" in p.lower() for p in LOG_AUDIT_ALLOWLIST)
    # The shared Phase 7 server's audit uses the static list only.
    assert not is_allowlisted(_unclean_exit_warning(KILLED_PID), PHASE7_LOG_ALLOWLIST)


def test_kill_records_the_killed_pid() -> None:
    server = RestartableServer.__new__(RestartableServer)
    server.killed_pids = []
    server._log = None
    server.process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    pid = server.process.pid
    server.kill()
    assert server.killed_pids == [pid]
    server.kill()  # already dead: nothing new was killed
    assert server.killed_pids == [pid]
