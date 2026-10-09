"""Story S12: the watchdog's always-on write volume stays small and bounded.

Bytes are measured from the kernel's per-thread I/O accounting (``wchar`` in
``/proc/self/task/<tid>/io``) of the watchdog thread itself: everything that
thread writes, whatever the file layout.

Budget: 2 KB per heartbeat in steady state. A steady tick is one sample line
(about 200 bytes on a host with one NFS mount; each further mount adds about
45 bytes), so 2 KB leaves 10x headroom, while any per-tick rewrite of the
id->name table (about 7 KB for 101 entries) or of the 60-sample ring (about
12 KB) blows it.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Dict, List

from code_indexer.server.utils.stall_watchdog import StallWatchdog

_BYTES_PER_TICK_BUDGET = 2048
_INTERVAL_S = 0.05
_SETTLE_S = 0.6
_WINDOW_S = 1.0
_IDLE_THREADS = 600


def _wchar(native_id: int) -> int:
    for line in Path(f"/proc/self/task/{native_id}/io").read_text().splitlines():
        if line.startswith("wchar:"):
            return int(line.split()[1])
    raise AssertionError("no wchar in per-thread io accounting")


def _watchdog_native_id() -> int:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        for thread in threading.enumerate():
            if thread.name == "cidx-stall-watchdog" and thread.native_id:
                return int(thread.native_id)
        time.sleep(0.01)
    raise AssertionError("watchdog thread not found")


def _start_idle_threads(count: int, release: threading.Event) -> List[threading.Thread]:
    threads = [
        threading.Thread(target=release.wait, name=f"idle-{i}", daemon=True)
        for i in range(count)
    ]
    for thread in threads:
        thread.start()
    return threads


def test_steady_state_tick_writes_under_2kb_with_600_idle_threads(
    tmp_path: Path,
) -> None:
    release = threading.Event()
    measured: Dict[str, float] = {}

    def measure() -> None:
        time.sleep(_SETTLE_S)  # first snapshot, first table and stack blocks
        tid = _watchdog_native_id()
        start_bytes, start_time = _wchar(tid), time.monotonic()
        time.sleep(_WINDOW_S)
        measured["bytes"] = _wchar(tid) - start_bytes
        measured["ticks"] = (time.monotonic() - start_time) / _INTERVAL_S

    # Helper threads exist BEFORE the watchdog, so steady state has no churn.
    _start_idle_threads(_IDLE_THREADS, release)
    helper = threading.Thread(target=measure, name="volume-probe", daemon=True)
    helper.start()
    watchdog = StallWatchdog(
        tmp_path / "logs", dump_timeout_s=0.5, interval_s=_INTERVAL_S
    )
    watchdog.start()
    try:
        helper.join(timeout=30)  # the main thread blocks in ONE call: no stack churn
    finally:
        watchdog.stop()
        release.set()

    assert measured, "measurement thread did not finish"
    per_tick = measured["bytes"] / measured["ticks"]
    assert measured["bytes"] > 0, "the heartbeat stopped writing evidence"
    assert per_tick <= _BYTES_PER_TICK_BUDGET, (
        f"{per_tick:.0f} bytes per heartbeat with {_IDLE_THREADS} idle threads "
        f"(budget {_BYTES_PER_TICK_BUDGET})"
    )


def test_thread_set_change_refreshes_the_id_to_name_table(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    release = threading.Event()
    watchdog = StallWatchdog(log_dir, dump_timeout_s=0.5, interval_s=_INTERVAL_S)
    watchdog.start()
    try:
        time.sleep(_SETTLE_S)
        late = threading.Thread(
            target=release.wait, name="late-joiner-probe", daemon=True
        )
        late.start()
        expected = f"Thread 0x{late.ident:016x} name=late-joiner-probe"
        deadline = time.monotonic() + 5
        found = False
        while not found and time.monotonic() < deadline:
            found = any(
                expected in path.read_text(errors="replace")
                for path in log_dir.glob("*.evidence")
            )
            time.sleep(_INTERVAL_S)
        assert found, "a new thread never reached the evidence id->name table"
    finally:
        watchdog.stop()
        release.set()


def test_compaction_bounds_the_file_without_thrashing_or_false_dumps(
    tmp_path: Path,
) -> None:
    """A cap smaller than one snapshot must still compact only every so many
    heartbeats -- never rewrite a full snapshot on every tick. Compaction is an
    atomic replace onto the same name, so it shows as a new inode."""
    log_dir = tmp_path / "logs"
    cap = 4096
    watchdog = StallWatchdog(
        log_dir, dump_timeout_s=0.5, interval_s=0.02, max_file_bytes=cap
    )
    inodes_seen: List[int] = []
    largest = 0
    watchdog.start()
    try:
        deadline = time.monotonic() + 1.0  # about 50 heartbeats
        while time.monotonic() < deadline:
            for path in log_dir.glob("*.evidence"):
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                largest = max(largest, stat.st_size)
                if not inodes_seen or inodes_seen[-1] != stat.st_ino:
                    inodes_seen.append(stat.st_ino)
            time.sleep(0.005)
    finally:
        watchdog.stop()

    compactions = len(inodes_seen) - 1
    assert 1 <= compactions <= 10, f"{compactions} compactions in ~50 heartbeats"
    assert largest < cap + 64 * 1024, largest
    assert list(log_dir.glob("worker-stall-*.log")) == [], "compaction is not a dump"
    leftovers = [
        p.name for p in log_dir.iterdir() if p.suffix in (".evidence", ".tmp", ".dump")
    ]
    assert leftovers == [], leftovers
