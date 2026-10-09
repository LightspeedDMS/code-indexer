"""Story S12: faulthandler's dump never mixes with the heartbeat's evidence.

faulthandler writes a dump with many small native writes; the heartbeat
appends records with Python writes. Sharing one descriptor lets a record land
inside a dump. Each worker lifetime therefore has a DUMP file that only
faulthandler writes and an EVIDENCE file that only the heartbeat writes; a
stall is "the dump file is non-empty", never a text match.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from code_indexer.server.utils.stall_watchdog import StallWatchdog

_LOGGER = "code_indexer.server.utils.stall_watchdog"
_SRC_DIR = Path(__file__).resolve().parents[4] / "src"

# The line that opens the dump section of a finalized stall log.
_DUMP_SECTION_LINE = re.compile(r"^=== faulthandler dump.*===$", re.MULTILINE)
# Every line faulthandler (CPython 3.9) writes in a dump_traceback_later dump.
_FAULTHANDLER_LINE = re.compile(
    r"^(Timeout \(\d+:\d\d:\d\d(\.\d+)?\)!"
    r"|(Current thread|Thread) 0x[0-9a-f]+ \(most recent call first\):"
    r'|  File ".*", line \d+ in .*'
    r"| *<no Python frame>"
    r"|  \.\.\."
    r"|\.\.\."
    r"|)$"
)
_RECORD_START = re.compile(r"^  \d{8}T\d{9}Z ")
_WHOLE_RECORD = re.compile(
    r"^  \d{8}T\d{9}Z rss_kb=\S+ swap_kb=\S+ mem_available_kb=\S+ "
    r"swap_free_kb=\S+ swap_in_pages_per_s=\S+ swap_out_pages_per_s=\S+ "
    r"psi_memory=\S+ psi_io=\S+ nfs=\S+ threads=\d+$"
)

# Stalls just past the dump timeout, while the heartbeat appends constantly
# (thread churn makes most heartbeats append a table block too), with the GIL
# handed over as often as possible and dumps made slow by deep stacks.
_STALLS_DURING_APPENDS_CHILD = """
import ctypes, sys, threading, time
from pathlib import Path
sys.setswitchinterval(1e-6)
from code_indexer.server.utils.stall_watchdog import StallWatchdog
release = threading.Event()

def deep(n):
    if n:
        return deep(n - 1)
    release.wait()

def churn():
    while not release.is_set():
        t = threading.Thread(target=time.sleep, args=(0.003,), daemon=True)
        t.start()
        t.join()

for _ in range(30):
    threading.Thread(target=deep, args=(60,), daemon=True).start()
threading.Thread(target=churn, daemon=True).start()
watchdog = StallWatchdog(Path(sys.argv[1]), dump_timeout_s=0.05, interval_s=0.005)
watchdog.start()
time.sleep(0.2)
for _ in range(6):
    ctypes.PyDLL(None).usleep(55_000)
    time.sleep(0.03)
release.set()
watchdog.stop()
"""


# The child deliberately holds its GIL six times amid heavy thread churn;
# under load it outlasts the 15 s gate ceiling. 90 s sits above the child's
# own 60 s subprocess timeout.
@pytest.mark.timeout(90)
def test_stall_during_heartbeat_appends_leaves_an_intact_dump(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    child = subprocess.run(
        [sys.executable, "-c", _STALLS_DURING_APPENDS_CHILD, str(log_dir)],
        env=dict(os.environ, PYTHONPATH=str(_SRC_DIR)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert child.returncode == 0, child.stderr
    logs = sorted(log_dir.glob("worker-stall-*-*.log"))
    assert logs, child.stderr
    for log in logs:
        text = log.read_text(errors="replace")
        section = _DUMP_SECTION_LINE.search(text)
        assert section, f"{log.name}: the dump is not in its own section"
        evidence = text[: section.start()]
        dump_lines = text[section.end() :].lstrip("\n").splitlines()
        assert dump_lines and dump_lines[0].startswith("Timeout ("), dump_lines[:3]
        foreign = [line for line in dump_lines if not _FAULTHANDLER_LINE.match(line)]
        assert foreign == [], f"{log.name}: not faulthandler output: {foreign[:3]}"
        broken = [
            line
            for line in evidence.splitlines()
            if _RECORD_START.match(line) and not _WHOLE_RECORD.match(line)
        ]
        assert broken == [], f"{log.name}: partial records: {broken[:3]}"


def _dead_pid() -> int:
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(done.stdout.strip())


def test_a_timeout_line_in_evidence_is_not_a_dump(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An embedded newline in a recorded code filename can put a line that
    starts with 'Timeout (' into the evidence; that is not a stall."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    pid = _dead_pid()
    (log_dir / f"worker-stall-{pid}-1.evidence").write_text(
        "cidx worker stall watchdog: dead worker\n"
        '  File "weird\nTimeout (0:00:03)!\n.py", line 1 in f\n'
    )
    (log_dir / f"worker-stall-{pid}-1.0.dump").write_text("")  # never fired

    watchdog = StallWatchdog(log_dir)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        watchdog.start()
        watchdog.stop()

    messages = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert any(
        level == logging.WARNING
        and f"worker pid {pid} ended without a clean shutdown and without a stall"
        in message
        for level, message in messages
    ), messages
    assert not any(
        level == logging.ERROR and f"worker pid {pid}" in message
        for level, message in messages
    ), messages
