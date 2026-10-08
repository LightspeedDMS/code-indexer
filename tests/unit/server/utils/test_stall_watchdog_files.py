"""Story S12: worker-stall watchdog file handling, in-process with real files.

Each test runs a real ``StallWatchdog`` thread (real faulthandler timer, real
files under a temp ``logs/`` directory) and stops it again. Per worker
lifetime the watchdog keeps ``worker-stall-<pid>-<ticks>.evidence`` (heartbeat
only) and ``worker-stall-<pid>-<ticks>.<gen>.dump`` (faulthandler only).
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from code_indexer.server.utils.stall_watchdog import StallWatchdog

_LOGGER = "code_indexer.server.utils.stall_watchdog"
_ARMED_WAIT_S = 10.0
_SRC_DIR = Path(__file__).resolve().parents[4] / "src"
# Dumps left behind by EARLIER workers: rotation bounds the dumps of every
# worker that ever ran, not only the current process's.
_PAST_WORKER_PID = 4242
_LIFETIME_FILE = re.compile(
    r"^worker-stall-\d+-\d+\.(evidence|evidence\.tmp|\d+\.dump)$"
)
_DUMP_SECTION_LINE = re.compile(r"^=== faulthandler dump.*===$", re.MULTILINE)
_FAKE_DUMP = "Timeout (0:00:03)!\nThread 0x00007f0000000001 (most recent call first):\n"


def _run_watchdog_once(log_dir: Path) -> None:
    """Start a watchdog, wait for its first heartbeat evidence, then stop it."""
    watchdog = StallWatchdog(log_dir)
    watchdog.start()
    try:
        deadline = time.monotonic() + _ARMED_WAIT_S
        own = f"worker-stall-{os.getpid()}-"
        while not any(
            p.name.startswith(own) and p.stat().st_size > 0
            for p in log_dir.glob("*.evidence")
        ):
            assert time.monotonic() < deadline, "watchdog never armed"
            time.sleep(0.05)
    finally:
        watchdog.stop()


def _names(log_dir: Path, pattern: str) -> List[str]:
    return sorted(p.name for p in log_dir.glob(pattern))


def _lifetime_files(log_dir: Path) -> List[str]:
    return sorted(p.name for p in log_dir.iterdir() if _LIFETIME_FILE.match(p.name))


def _plant_lifetime(
    log_dir: Path,
    pid: int,
    ticks: int,
    label: str,
    dumped: bool,
    mtime: Optional[float] = None,
) -> Tuple[Path, Path]:
    """A worker lifetime's files as the watchdog leaves them: the evidence and
    dump file 0, which holds faulthandler text when the worker stalled."""
    evidence = log_dir / f"worker-stall-{pid}-{ticks}.evidence"
    dump = log_dir / f"worker-stall-{pid}-{ticks}.0.dump"
    evidence.write_text(
        f"cidx worker stall watchdog: {label}\n  20261006T120000000Z rss_kb=1\n"
    )
    dump.write_text(_FAKE_DUMP if dumped else "")
    if mtime is not None:
        for path in (evidence, dump):
            os.utime(path, (mtime, mtime))
    return evidence, dump


def _split_at_dump(text: str) -> Tuple[str, str]:
    """(the evidence written before the stall, faulthandler's dump)."""
    section = _DUMP_SECTION_LINE.search(text)
    assert section, f"no faulthandler dump section in:\n{text[-2000:]}"
    return text[: section.start()], text[section.end() :].lstrip("\n")


def test_startup_keeps_only_the_newest_20_stall_dumps(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    now = time.time()
    dumps = []
    for index in range(25):
        dump = log_dir / (
            f"worker-stall-{_PAST_WORKER_PID}-20261006T1200{index:02d}000Z.log"
        )
        dump.write_text("stack\n")
        os.utime(dump, (now - 1000 + index, now - 1000 + index))
        dumps.append(dump.name)
    unrelated = ["server.stdout.log", "server.stderr.log", "worker-stall-notes.txt"]
    for name in unrelated:
        (log_dir / name).write_text("keep me\n")
        os.utime(log_dir / name, (now - 5000, now - 5000))

    _run_watchdog_once(log_dir)

    assert _names(log_dir, "worker-stall-*.log") == sorted(dumps[5:])
    for name in unrelated:
        assert (log_dir / name).read_text() == "keep me\n"
    assert _lifetime_files(log_dir) == [], "a clean stop removes its files"


def _dead_pid() -> int:
    """The pid of a real process that has exited and been reaped."""
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(done.stdout.strip())


def _start_ticks(pid: int) -> int:
    """Field 22 of /proc/<pid>/stat: start time in clock ticks since boot."""
    text = Path(f"/proc/{pid}/stat").read_text()
    return int(text.rsplit(")", 1)[1].split()[19])


def _boot_time() -> float:
    for line in Path("/proc/stat").read_text().splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    raise AssertionError("no btime line in /proc/stat")


def _assert_reported_as_dump(
    log_dir: Path, pid: int, label: str, caplog: pytest.LogCaptureFixture
) -> None:
    reported = [
        p for p in log_dir.glob(f"worker-stall-{pid}-*.log") if label in p.read_text()
    ]
    assert len(reported) == 1, sorted(p.name for p in log_dir.iterdir())
    evidence, dump = _split_at_dump(reported[0].read_text())
    assert label in evidence and dump.startswith("Timeout ("), reported[0].read_text()
    assert any(
        r.levelno == logging.ERROR
        and f"worker pid {pid}" in r.getMessage()
        and str(reported[0]) in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_dead_earlier_lifetime_of_this_very_pid_is_reported_not_clobbered(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """PID reuse: the new worker got the pid of a worker that died stalled.
    Its dump must be reported, never truncated by the new worker's files."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    pid = os.getpid()
    evidence, dump = _plant_lifetime(
        log_dir, pid, _start_ticks(pid) - 1, "earlier lifetime of this pid", True
    )

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        _run_watchdog_once(log_dir)

    assert not evidence.exists() and not dump.exists()
    _assert_reported_as_dump(log_dir, pid, "earlier lifetime of this pid", caplog)


def test_dead_lifetime_whose_pid_a_live_process_now_owns_is_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """PID reuse: an unrelated live process now holds the dead worker's pid.
    The dead lifetime is reported; the live lifetime's files are left alone."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ticks = _start_ticks(owner.pid)
        stale = _plant_lifetime(
            log_dir, owner.pid, ticks - 1, "dead lifetime of a reused pid", True
        )
        live = _plant_lifetime(log_dir, owner.pid, ticks, "live lifetime", True)
        live_texts = [path.read_text() for path in live]

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _run_watchdog_once(log_dir)

        assert not any(path.exists() for path in stale)
        _assert_reported_as_dump(
            log_dir, owner.pid, "dead lifetime of a reused pid", caplog
        )
        assert [path.read_text() for path in live] == live_texts
    finally:
        owner.kill()
        owner.wait(timeout=10)


def test_previous_boot_file_matching_a_live_lifetime_is_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Start ticks restart at every boot: files written before this boot are
    dead even if their pid and start ticks match a process alive now."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    pid = os.getpid()
    planted = _plant_lifetime(
        log_dir,
        pid,
        _start_ticks(pid),
        "written before this boot",
        True,
        mtime=_boot_time() - 3600,
    )

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        _run_watchdog_once(log_dir)

    assert not any(path.exists() for path in planted)
    _assert_reported_as_dump(log_dir, pid, "written before this boot", caplog)


def test_sweep_keeps_last_samples_of_a_worker_that_died_without_a_stall(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    pid = _dead_pid()
    _plant_lifetime(log_dir, pid, 1, "last samples", dumped=False)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        _run_watchdog_once(log_dir)

    kept = list(log_dir.glob(f"worker-stall-{pid}-*.log"))
    assert len(kept) == 1 and "last samples" in kept[0].read_text()
    assert [n for n in _lifetime_files(log_dir) if f"-{pid}-" in n] == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        f"worker pid {pid} ended without a clean shutdown" in r.getMessage()
        and str(kept[0]) in r.getMessage()
        for r in warnings
    ), [r.getMessage() for r in caplog.records]


def test_sweep_leaves_files_of_live_workers_alone(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        planted = _plant_lifetime(
            log_dir, sibling.pid, _start_ticks(sibling.pid), "live sibling", True
        )
        texts = [path.read_text() for path in planted]

        _run_watchdog_once(log_dir)

        assert [path.read_text() for path in planted] == texts
        assert list(log_dir.glob(f"worker-stall-{sibling.pid}-*.log")) == []
    finally:
        sibling.kill()
        sibling.wait(timeout=10)


def test_one_unreadable_dead_worker_does_not_stop_the_sweep_of_others(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    broken_pid, stalled_pid = sorted([_dead_pid(), _dead_pid()])  # broken first
    (log_dir / f"worker-stall-{broken_pid}-1.evidence").mkdir()  # read -> OSError
    (log_dir / f"worker-stall-{broken_pid}-1.0.dump").write_text(_FAKE_DUMP)
    _plant_lifetime(log_dir, stalled_pid, 1, "stalled worker", dumped=True)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        _run_watchdog_once(log_dir)

    _assert_reported_as_dump(log_dir, stalled_pid, "stalled worker", caplog)


# A child process (never the pytest process) that stalls past the dump
# timeout and then recovers: no supervisor kills it here.
_SURVIVED_STALL_CHILD = """
import ctypes, logging, sys, time
from pathlib import Path
from code_indexer.server.utils.stall_watchdog import StallWatchdog
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")

def hold_gil_in_c_call(microseconds):
    ctypes.PyDLL(None).usleep(microseconds)

watchdog = StallWatchdog(Path(sys.argv[1]), dump_timeout_s=0.6, interval_s=0.2)
watchdog.start()
time.sleep(0.6)
hold_gil_in_c_call(1_200_000)
time.sleep(0.8)
watchdog.stop()
"""


def _run_child(
    script: str, log_dir: Path, timeout: int = 45
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [sys.executable, "-c", script, str(log_dir)],
        env=dict(os.environ, PYTHONPATH=str(_SRC_DIR)),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_worker_that_survives_a_stall_reports_its_own_dump(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    child = _run_child(_SURVIVED_STALL_CHILD, log_dir)
    assert child.returncode == 0, child.stderr

    dumps = list(log_dir.glob("worker-stall-*-*.log"))
    assert len(dumps) == 1, (dumps, child.stderr)
    samples, stack = _split_at_dump(dumps[0].read_text())
    assert "rss_kb=" in samples
    assert stack.startswith("Timeout (") and "hold_gil_in_c_call" in stack
    assert "ERROR Worker stall watchdog: worker pid " in child.stderr
    assert str(dumps[0]) in child.stderr, child.stderr
    assert _lifetime_files(log_dir) == []


def test_watchdog_that_cannot_start_logs_an_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    not_a_dir = tmp_path / "logs"
    not_a_dir.write_text("a file where the log directory should be\n")
    watchdog = StallWatchdog(not_a_dir)
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        watchdog.start()
        watchdog.stop()
    assert any(
        "could not start" in r.getMessage() and str(not_a_dir) in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_watchdog_rejects_interval_not_below_timeout_and_double_start(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        StallWatchdog(tmp_path, dump_timeout_s=1.0, interval_s=1.0)
    watchdog = StallWatchdog(tmp_path / "logs")
    watchdog.start()
    try:
        with pytest.raises(RuntimeError):
            watchdog.start()
    finally:
        watchdog.stop()


# A child whose evidence appends fail for real (file-size limit -> EFBIG, like
# a full disk) for longer than the dump timeout, with no stall. The dump file
# is empty and far below the limit, so a false dump WOULD be written there.
_WRITE_FAILURE_CHILD = """
import logging, resource, signal, sys, time
from pathlib import Path
from code_indexer.server.utils.stall_watchdog import StallWatchdog
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
watchdog = StallWatchdog(Path(sys.argv[1]), dump_timeout_s=0.6, interval_s=0.2)
watchdog.start()
time.sleep(0.8)
largest = max(p.stat().st_size for p in Path(sys.argv[1]).glob("*.evidence"))
resource.setrlimit(resource.RLIMIT_FSIZE, (largest + 150, resource.RLIM_INFINITY))
time.sleep(2.0)
watchdog.stop()
"""


def test_failed_evidence_writes_keep_watching_without_a_false_dump(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    child = _run_child(_WRITE_FAILURE_CHILD, log_dir)
    assert child.returncode == 0, child.stderr
    assert "File too large" in child.stderr, child.stderr  # the writes did fail
    assert child.stderr.count("Worker stall watchdog tick failed") == 1, child.stderr
    assert list(log_dir.glob("worker-stall-*.log")) == [], "a false stall dump"
    assert _lifetime_files(log_dir) == []


def test_stop_is_a_noop_when_the_watchdog_never_started(tmp_path: Path) -> None:
    import asyncio
    from types import SimpleNamespace

    from code_indexer.server.utils.stall_watchdog import stop_stall_watchdog

    StallWatchdog(tmp_path / "logs").stop()
    assert not (tmp_path / "logs").exists()
    # A lifespan whose start failed has no app.state.stall_watchdog.
    app = SimpleNamespace(state=SimpleNamespace())
    asyncio.run(stop_stall_watchdog(app))
    assert not hasattr(app.state, "stall_watchdog")


# A child with exactly ONE free descriptor number when the watchdog starts:
# the dump file opens, the evidence file open fails with a real EMFILE.
_ONE_FREE_FD_CHILD = """
import logging, os, resource, sys
from pathlib import Path
from code_indexer.server.utils.stall_watchdog import StallWatchdog
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
log_dir = Path(sys.argv[1])
log_dir.mkdir()
before = len(os.listdir("/proc/self/fd"))
lowest_free = os.dup(0)
os.close(lowest_free)
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
watchdog = StallWatchdog(log_dir)
resource.setrlimit(resource.RLIMIT_NOFILE, (lowest_free + 1, hard))
watchdog.start()
watchdog.stop()
resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
after = len(os.listdir("/proc/self/fd"))
print(f"fds_before={before} fds_after={after}")
"""


def test_failed_evidence_open_after_dump_open_leaks_nothing(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    child = _run_child(_ONE_FREE_FD_CHILD, log_dir, timeout=30)
    assert child.returncode == 0, child.stderr
    assert "could not start" in child.stderr, child.stderr
    assert "Too many open files" in child.stderr, child.stderr  # real EMFILE
    counts = dict(pair.split("=") for pair in child.stdout.split())
    assert counts["fds_after"] == counts["fds_before"], (child.stdout, child.stderr)
    assert _lifetime_files(log_dir) == []


def hold_gil_in_c_call_in_process(microseconds: int) -> None:
    """ctypes.PyDLL calls the C function WITHOUT releasing the GIL."""
    ctypes.PyDLL(None).usleep(microseconds)


_STALL_FRAME = "hold_gil_in_c_call_in_process"
# A loaded machine can starve this process past the 0.5 s timeout on its own:
# that real stall is dumped too, and if it fires the one-shot timer just
# before the deliberate stall begins, nothing can re-arm it during the stall.
# Each attempt is a full deliberate stall; the bound keeps the test finite.
_STALL_ATTEMPTS = 5


def _evidence_bytes(log_dir: Path) -> int:
    return sum(p.stat().st_size for p in log_dir.glob("*.evidence"))


def _await_heartbeat(log_dir: Path) -> None:
    """Return just after a tick: its re-arm restarted the dump timer."""
    before = _evidence_bytes(log_dir)
    deadline = time.monotonic() + _ARMED_WAIT_S
    while _evidence_bytes(log_dir) == before:
        assert time.monotonic() < deadline, "the watchdog stopped ticking"
        time.sleep(0.005)


def _await_stall_log(log_dir: Path, wait_s: float) -> Optional[Path]:
    """The finalized log whose DUMP shows the deliberate stall's frame, among
    every log written so far (load may have added logs of real stalls)."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        for log in log_dir.glob("worker-stall-*-*.log"):
            text = log.read_text()
            if (
                _DUMP_SECTION_LINE.search(text)
                and _STALL_FRAME in _split_at_dump(text)[1]
            ):
                return log
        time.sleep(0.02)
    return None


def test_core_mechanism_in_process_gil_stall_leaves_stack_and_samples(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fast-lane twin of the slow real-uvicorn test: the same faulthandler
    mechanism, with short timings, in this process."""
    log_dir = tmp_path / "logs"
    watchdog = StallWatchdog(log_dir, dump_timeout_s=0.5, interval_s=0.1)
    stall_log: Optional[Path] = None
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        watchdog.start()
        try:
            deadline = time.monotonic() + _ARMED_WAIT_S
            while not any(p.stat().st_size > 0 for p in log_dir.glob("*.evidence")):
                assert time.monotonic() < deadline, "watchdog never armed"
                time.sleep(0.02)
            time.sleep(0.3)  # a few heartbeats: samples in the ring
            for _ in range(_STALL_ATTEMPTS):
                _await_heartbeat(log_dir)
                hold_gil_in_c_call_in_process(1_500_000)  # 1.5 s = 3x the timeout
                stall_log = _await_stall_log(log_dir, wait_s=5.0)
                if stall_log is not None:
                    break
        finally:
            watchdog.stop()

    assert stall_log is not None, sorted(p.name for p in log_dir.iterdir())
    samples, stack = _split_at_dump(stall_log.read_text())
    assert "rss_kb=" in samples
    assert stack.startswith("Timeout (") and _STALL_FRAME in stack
    assert any(
        r.levelno == logging.ERROR and str(stall_log) in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


# 120 idle threads (like a busy worker's thread pools), then the MAIN thread
# -- the event loop in a uvicorn worker, and the OLDEST thread -- stalls.
_MANY_THREADS_CHILD = """
import ctypes, logging, sys, threading, time
from pathlib import Path
from code_indexer.server.utils.stall_watchdog import StallWatchdog
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
release = threading.Event()

def idle_pool_thread():
    release.wait()

def main_thread_waits_before_stall():
    time.sleep(0.8)

def main_thread_holds_gil():
    ctypes.PyDLL(None).usleep(1_200_000)

watchdog = StallWatchdog(Path(sys.argv[1]), dump_timeout_s=0.6, interval_s=0.2)
watchdog.start()
for _ in range(120):
    threading.Thread(target=idle_pool_thread, daemon=True).start()
main_thread_waits_before_stall()
main_thread_holds_gil()
time.sleep(0.8)
watchdog.stop()
release.set()
"""


def test_main_thread_stack_is_kept_when_faulthandler_truncates_at_100_threads(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    child = _run_child(_MANY_THREADS_CHILD, log_dir, timeout=30)
    assert child.returncode == 0, child.stderr
    dumps = list(log_dir.glob("worker-stall-*-*.log"))
    assert len(dumps) == 1, (dumps, child.stderr)
    preamble, stack = _split_at_dump(dumps[0].read_text())
    # Precondition: faulthandler really truncated (newest 100 threads, then "...").
    headers = [
        line
        for line in stack.splitlines()
        if line.startswith(("Thread 0x", "Current thread 0x"))
    ]
    assert len(headers) == 100 and "\n...\n" in stack + "\n", stack[-300:]
    # The bounded id->name table maps every thread the dump shows.
    dumped_ids = {re.search(r"0x[0-9a-f]+", line).group(0) for line in headers}  # type: ignore[union-attr]
    mapped_ids = set(re.findall(r"^  Thread (0x[0-9a-f]+) name=", preamble, re.M))
    assert dumped_ids <= mapped_ids, sorted(dumped_ids - mapped_ids)[:5]
    # The main thread's stack is always in the evidence (the LAST block before
    # the stall; blocks repeat whenever the stack changes).
    header = "main thread stack at"
    assert header in preamble, preamble[-2000:]
    snapshot = preamble.rsplit(header, 1)[1]
    assert (
        "main_thread_waits_before_stall" in snapshot
        or "main_thread_holds_gil" in snapshot
    ), snapshot
    counts = [int(n) for n in re.findall(r"threads=(\d+)", preamble)]
    assert counts and max(counts) >= 120, preamble[:500]
