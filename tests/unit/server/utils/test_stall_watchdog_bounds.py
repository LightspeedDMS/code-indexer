"""Story S12: every piece of the watchdog's work and output is bounded.

* compaction that cannot proceed stops evidence appends visibly (one ERROR)
  instead of letting the file grow, and resumes when it can;
* rendered thread names, code filenames and the NFS mount list are capped;
* the startup sweep reads a bounded number of bytes per file and finalizes a
  bounded batch of dead worker lifetimes per start.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from code_indexer.server.utils.pressure_sampler import PressureSampler
from code_indexer.server.utils.stall_watchdog import StallWatchdog
from code_indexer.server.utils.stall_watchdog_evidence import (
    format_stack,
    thread_table,
)

_LOGGER = "code_indexer.server.utils.stall_watchdog"
_FAKE_DUMP = "Timeout (0:00:03)!\nThread 0x00007f0000000001 (most recent call first):\n"


def _own_evidence(log_dir: Path, deadline_s: float = 10.0) -> Path:
    deadline = time.monotonic() + deadline_s
    pattern = f"worker-stall-{os.getpid()}-*.evidence"
    while True:
        found = [p for p in log_dir.glob(pattern) if p.stat().st_size > 0]
        if found:
            return found[0]
        assert time.monotonic() < deadline, "watchdog never wrote its evidence"
        time.sleep(0.02)


def _run_watchdog_once(log_dir: Path) -> None:
    watchdog = StallWatchdog(log_dir)
    watchdog.start()
    try:
        _own_evidence(log_dir)
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


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_compaction_that_cannot_proceed_stops_appending_with_one_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log_dir = tmp_path / "logs"
    watchdog = StallWatchdog(
        log_dir, dump_timeout_s=0.5, interval_s=0.02, max_file_bytes=2048
    )
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        watchdog.start()
        try:
            evidence = _own_evidence(log_dir)
            log_dir.chmod(0o500)  # compaction's temp file cannot be created
            try:
                time.sleep(0.6)  # the cap (~10 records) is long reached
                frozen = evidence.stat().st_size
                time.sleep(0.5)
                assert evidence.stat().st_size == frozen, "evidence kept growing"
            finally:
                log_dir.chmod(0o700)
            blocked_inode = evidence.stat().st_ino
            deadline = time.monotonic() + 3
            while evidence.stat().st_ino == blocked_inode:
                assert time.monotonic() < deadline, "compaction never resumed"
                time.sleep(0.02)
        finally:
            watchdog.stop()

    messages = [(r.levelno, r.getMessage()) for r in caplog.records]
    stopped = [
        m for lvl, m in messages if lvl == logging.ERROR and "stops appending" in m
    ]
    assert len(stopped) == 1, messages
    assert any(lvl == logging.WARNING and "resume" in m for lvl, m in messages), (
        messages
    )


def test_nfs_field_lists_at_most_16_mounts_with_bounded_names() -> None:
    stanzas = []
    for index in range(20):
        mount = "/mnt/" + "m" * 300 + str(index)
        stanzas.append(
            f"device 192.0.2.{index}:/export mounted on {mount} with fstype nfs "
            "statvers=1.1\n\tper-op statistics\n\t        READ: 1 1 0 0 0 0 0 0\n"
        )
    field = PressureSampler._nfs("".join(stanzas))
    entries = field.split(";")
    assert len(entries) == 17 and entries[-1] == "+4 more", entries[-2:]
    for entry in entries[:-1]:
        mount = entry.split(":retrans=", 1)[0]
        assert len(mount) <= 128, len(mount)


def test_rendered_names_and_code_fields_are_single_bounded_lines() -> None:
    release = threading.Event()
    odd = threading.Thread(
        target=release.wait, name="n" * 1000 + "\nTimeout (0:00:03)!", daemon=True
    )
    odd.start()
    try:
        line = next(
            line
            for line in thread_table(sys._current_frames())
            if f"0x{odd.ident:016x}" in line
        )
        assert "\n" not in line and len(line) <= 140, line[:200]
    finally:
        release.set()
    namespace: dict = {}
    exec(
        compile(
            "import sys\ndef f():\n    return sys._getframe()\n",
            "a\nTimeout (" + "x" * 900,
            "exec",
        ),
        namespace,
    )
    for line in format_stack(namespace["f"]()):
        assert "\n" not in line and len(line) <= 500, line[:200]


def test_dead_lifetime_reads_are_bounded(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    pid = _dead_pid()
    (log_dir / f"worker-stall-{pid}-1.evidence").write_bytes(b"e" * (3 * 1024 * 1024))
    (log_dir / f"worker-stall-{pid}-1.0.dump").write_text(_FAKE_DUMP)

    _run_watchdog_once(log_dir)

    logs = list(log_dir.glob(f"worker-stall-{pid}-*.log"))
    assert len(logs) == 1, logs
    text = logs[0].read_text()
    assert len(text) <= 1024 * 1024 + len(_FAKE_DUMP) + 1024, len(text)
    assert "bytes skipped" in text and text.endswith(_FAKE_DUMP)


def test_sweep_finalizes_at_most_32_dead_lifetimes_per_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    pid = _dead_pid()
    for ticks in range(1, 41):  # 40 dead lifetimes that all stalled
        (log_dir / f"worker-stall-{pid}-{ticks}.evidence").write_text("evidence\n")
        (log_dir / f"worker-stall-{pid}-{ticks}.{0}.dump").write_text(_FAKE_DUMP)

    def stalled_reports() -> int:
        return sum(
            1
            for r in caplog.records
            if r.levelno == logging.ERROR
            and f"worker pid {pid} could not run" in r.getMessage()
        )

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        _run_watchdog_once(log_dir)
        assert stalled_reports() == 32
        assert any(
            "8 more dead worker lifetimes" in r.getMessage() for r in caplog.records
        )
        caplog.clear()
        _run_watchdog_once(log_dir)
        assert stalled_reports() == 8
    assert list(log_dir.glob(f"worker-stall-{pid}-*.dump")) == []
