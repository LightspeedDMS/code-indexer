"""Story S12: the watchdog cleans up on every failure path and its sweep
always makes progress.

* ANY startup failure -- even a non-OSError while rendering the first
  snapshot -- cancels the faulthandler timer, closes both descriptors, removes
  its files and logs one ERROR, without crashing the thread;
* a file error at shutdown never leaves a descriptor open;
* filler entries in the log directory cannot hide a dead worker's dump from
  the bounded startup sweep.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List

import pytest

from code_indexer.server.utils.stall_watchdog import StallWatchdog

_LOGGER = "code_indexer.server.utils.stall_watchdog"
_SRC_DIR = Path(__file__).resolve().parents[4] / "src"
_FAKE_DUMP = "Timeout (0:00:03)!\nThread 0x00007f0000000001 (most recent call first):\n"

# A real audit hook (no mock) rejects sys._current_frames(), so rendering the
# first snapshot raises a non-OSError after the dump timer was armed.
_SNAPSHOT_RAISES_CHILD = """
import logging, os, sys, threading, time
from pathlib import Path
from code_indexer.server.utils.stall_watchdog import StallWatchdog
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")

def deny(event, args):
    if event == "sys._current_frames":
        raise RuntimeError("audit hook denies sys._current_frames")

log_dir = Path(sys.argv[1])
log_dir.mkdir()
before = len(os.listdir("/proc/self/fd"))
sys.addaudithook(deny)
watchdog = StallWatchdog(log_dir, dump_timeout_s=0.5, interval_s=0.1)
watchdog.start()
time.sleep(0.5)
watchdog.stop()
time.sleep(0.8)  # past the dump timeout: a timer left armed would fire now
after = len(os.listdir("/proc/self/fd"))
print(f"fds_before={before} fds_after={after}")
"""


def test_a_snapshot_that_raises_leaves_no_armed_timer_or_open_files(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    child = subprocess.run(
        [sys.executable, "-c", _SNAPSHOT_RAISES_CHILD, str(log_dir)],
        env=dict(os.environ, PYTHONPATH=str(_SRC_DIR)),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr
    assert "Exception in thread" not in child.stderr, child.stderr
    assert "could not start" in child.stderr, child.stderr
    counts = dict(pair.split("=") for pair in child.stdout.split())
    assert counts["fds_after"] == counts["fds_before"], (child.stdout, child.stderr)
    written = [p.name for p in log_dir.iterdir() if p.stat().st_size > 0]
    assert written == [], f"an armed timer wrote a dump: {written}"
    assert [p.name for p in log_dir.iterdir()] == []


def _fds_into(directory: Path) -> List[str]:
    targets = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith(str(directory)):
            targets.append(target)
    return targets


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_shutdown_file_errors_still_close_both_descriptors(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    watchdog = StallWatchdog(log_dir, dump_timeout_s=0.5, interval_s=0.05)
    watchdog.start()
    try:
        deadline = time.monotonic() + 10
        while not list(log_dir.glob("*.evidence")):
            assert time.monotonic() < deadline, "watchdog never wrote its evidence"
            time.sleep(0.02)
        log_dir.chmod(0o500)  # removing the dump file at shutdown now fails
        try:
            watchdog.stop()
        finally:
            log_dir.chmod(0o700)
        assert _fds_into(log_dir) == [], "a shutdown file error leaked a descriptor"
    finally:
        watchdog.stop()


def _dead_pid() -> int:
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(done.stdout.strip())


def test_filler_entries_cannot_hide_a_dead_lifetime_from_the_sweep(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for index in range(10_100):  # unrelated files that never go away
        (log_dir / f"filler-{index:05d}.txt").touch()
    pid = _dead_pid()
    lifetime = [
        log_dir / f"worker-stall-{pid}-1.evidence",
        log_dir / f"worker-stall-{pid}-1.0.dump",
    ]
    lifetime[0].write_text("evidence\n")
    lifetime[1].write_text(_FAKE_DUMP)
    with os.scandir(log_dir) as entries:
        order = [entry.name for entry in entries]
    if min(order.index(path.name) for path in lifetime) < 10_000:
        pytest.skip("this filesystem lists the dead lifetime within the first 10,000")

    reported = False
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        for _ in range(2):  # a bounded number of worker starts
            watchdog = StallWatchdog(log_dir)
            watchdog.start()
            deadline = time.monotonic() + 10
            while not list(log_dir.glob(f"worker-stall-{os.getpid()}-*.evidence")):
                assert time.monotonic() < deadline, "watchdog never armed"
                time.sleep(0.02)
            watchdog.stop()
            reported = any(
                r.levelno == logging.ERROR and f"worker pid {pid} " in r.getMessage()
                for r in caplog.records
            )
            if reported:
                break
    assert reported, "the dead worker's stall was never reported"
    assert list(log_dir.glob(f"worker-stall-{pid}-*.log"))
    assert not any(path.exists() for path in lifetime)
