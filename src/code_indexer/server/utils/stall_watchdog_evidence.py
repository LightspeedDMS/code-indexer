"""Low-level evidence pieces of the worker-stall watchdog (Story S12).

The rules these follow (lifetime identity, the 100-thread faulthandler cap,
bounded write volume) are documented in ``stall_watchdog.py``.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from types import FrameType
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from code_indexer.server.utils.pressure_sampler import utc_stamp

# CPython's faulthandler cap (MAX_NTHREADS) and per-thread frame cap
# (MAX_FRAME_DEPTH).
FAULTHANDLER_MAX_THREADS = 100
MAX_SNAPSHOT_FRAMES = 100
# A rendered thread name is cut to this many characters.
MAX_THREAD_NAME_CHARS = 64
# A rendered code filename or function name is cut to this many characters.
MAX_FIELD_CHARS = 200


def read_start_ticks(pid: int) -> int:
    """Field 22 of ``/proc/<pid>/stat``: start time in clock ticks since boot.

    Parsed after the LAST ')' because the command name may contain spaces or
    parentheses. Raises FileNotFoundError when no such process exists.
    """
    with open(f"/proc/{pid}/stat", "r", encoding="ascii", errors="replace") as fh:
        text = fh.read()
    return int(text.rsplit(")", 1)[1].split()[19])


def boot_time() -> float:
    """``btime`` from ``/proc/stat``: when this boot started (epoch seconds)."""
    with open("/proc/stat", "r", encoding="ascii", errors="replace") as fh:
        for line in fh:
            if line.startswith("btime "):
                return float(line.split()[1])
    raise OSError("no btime line in /proc/stat")


def lifetime_is_running(
    pid: int, ticks: int, newest_mtime: float, booted_at: float
) -> bool:
    """Is the worker lifetime (pid, start ticks) that wrote these files alive?

    Start ticks restart at every boot, so files last written before this boot
    are dead even if a process now has the same pid and start ticks.
    """
    if newest_mtime < booted_at:
        return False
    try:
        return read_start_ticks(pid) == ticks
    except FileNotFoundError:
        return False


def read_bounded(path: Path, limit: int) -> str:
    """At most ``limit`` bytes of a file: all of it, or its first quarter and
    last three quarters around a '[... N bytes skipped ...]' line."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        if size <= limit:
            data = fh.read(limit)
            return data.decode("utf-8", errors="replace")
        head_len = limit // 4
        head = fh.read(head_len)
        fh.seek(size - (limit - head_len))
        tail = fh.read(limit - head_len)
    skipped = size - limit
    return (
        head.decode("utf-8", errors="replace")
        + f"\n[... {skipped} bytes skipped ...]\n"
        + tail.decode("utf-8", errors="replace")
    )


def one_line(text: str, limit: int) -> str:
    """``text`` as ONE bounded line: control characters escaped, then cut to
    ``limit`` characters (marked with a trailing '...')."""
    escaped = "".join(char if char.isprintable() else repr(char)[1:-1] for char in text)
    return escaped if len(escaped) <= limit else escaped[: limit - 3] + "..."


def format_stack(frame: Optional[FrameType]) -> List[str]:
    """Most recent call first, faulthandler's line format. Reads only code
    objects (no linecache, so no file I/O); bounded by MAX_SNAPSHOT_FRAMES."""
    lines: List[str] = []
    while frame is not None and len(lines) < MAX_SNAPSHOT_FRAMES:
        code = frame.f_code
        filename = one_line(code.co_filename, MAX_FIELD_CHARS)
        name = one_line(code.co_name, MAX_FIELD_CHARS)
        lines.append(f'  File "{filename}", line {frame.f_lineno} in {name}')
        frame = frame.f_back
    return lines or ["  (no Python frames)"]


def thread_table(frames: Dict[int, FrameType]) -> List[str]:
    """The threads a dump can show: the newest FAULTHANDLER_MAX_THREADS
    (``sys._current_frames()`` order == faulthandler order) plus main."""
    main_ident = threading.main_thread().ident
    idents = list(frames)[:FAULTHANDLER_MAX_THREADS]
    if main_ident is not None and main_ident not in idents:
        idents.append(main_ident)
    by_ident = {thread.ident: thread for thread in threading.enumerate()}
    lines = []
    for ident in idents:
        thread = by_ident.get(ident)
        if thread is None:
            lines.append(f"  Thread 0x{ident:016x} name=(not a threading.Thread)")
            continue
        name = one_line(thread.name, MAX_THREAD_NAME_CHARS)
        lines.append(f"  Thread 0x{ident:016x} name={name} daemon={thread.daemon}")
    return lines


def main_thread_stack(frames: Dict[int, FrameType]) -> List[str]:
    main_ident = threading.main_thread().ident
    if main_ident is None:
        return ["  (no main thread)"]
    return format_stack(frames.get(main_ident))


class EvidenceFile:
    """The heartbeat's append-only evidence file. faulthandler never writes
    here (it has its own dump file), so the two can never interleave."""

    def __init__(self, path: Path, replace_existing: bool = False) -> None:
        self.path = path
        # O_EXCL by default: an existing file is another lifetime's evidence;
        # failing the open is better than erasing it. O_TRUNC only for the
        # compaction temp file, which belongs to this lifetime.
        exclusive = os.O_TRUNC if replace_existing else os.O_EXCL
        flags = os.O_WRONLY | os.O_CREAT | exclusive | os.O_APPEND | os.O_CLOEXEC
        self.fd = os.open(str(path), flags, 0o640)
        self.written = 0

    def append(self, text: str) -> None:
        # Count bytes as they land (a write can fail partway: EFBIG, ENOSPC);
        # the compaction cap is measured from this count.
        data = memoryview(text.encode("utf-8", errors="replace"))
        while data:  # bounded: every write consumes >= 1 byte or raises
            count = os.write(self.fd, data)
            assert count > 0, "os.write on a regular file returned 0"
            self.written += count
            data = data[count:]

    def close(self) -> None:
        os.close(self.fd)


class DumpFile:
    """faulthandler's own dump file for one worker lifetime: nothing else
    ever writes it, so a non-empty file IS a stall (no text matching)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_CLOEXEC
        self.fd = os.open(str(path), flags, 0o640)

    def has_dump(self) -> bool:
        return os.fstat(self.fd).st_size > 0

    def close(self) -> None:
        os.close(self.fd)


class Rendered(NamedTuple):
    text: str
    table: List[str]
    main: List[str]


class EvidenceRenderer:
    """Text of an armed file: a snapshot opens a fresh file; a record is one
    heartbeat. Remembers what the CURRENT file last recorded (``commit``), so
    the thread table and the main-thread stack repeat only when they change.
    """

    def __init__(
        self, dump_timeout_s: float, interval_s: float, max_file_bytes: int
    ) -> None:
        self._settings = (
            f"dump_timeout_s={dump_timeout_s} heartbeat_interval_s={interval_s} "
            f"file_cap_bytes={max_file_bytes}"
        )
        self._written: Optional[Tuple[List[str], List[str]]] = None

    def commit(self, rendered: Rendered) -> None:
        """Call after ``rendered.text`` reached the current file in full."""
        self._written = (rendered.table, rendered.main)

    def snapshot(self, pid: int, ticks: int, samples: Sequence[str]) -> Rendered:
        """Full text for a FRESH file: header, the sample ring, both blocks."""
        if not samples:
            raise ValueError("snapshot() needs at least one sample")
        frames = sys._current_frames()  # one call, O(threads)
        table, main = thread_table(frames), main_thread_stack(frames)
        lines = [
            f"cidx worker stall watchdog: pid {pid} start_ticks={ticks}",
            f"written_at={utc_stamp(time.time())} {self._settings}",
            "memory samples (oldest first, one per heartbeat; newer ones are "
            "the records below):",
        ]
        lines.extend(f"  {sample}" for sample in samples[:-1])
        lines.append(f"  {samples[-1]} threads={len(frames)}")
        lines.extend(self._blocks(table, main, always=True))
        lines.append(
            "records (one per heartbeat; the table and the main thread stack "
            "repeat only when they change):"
        )
        return Rendered("\n".join(lines) + "\n", table, main)

    def record(self, samples: Sequence[str]) -> Rendered:
        """One heartbeat: one line, plus the blocks only when they changed."""
        if not samples:
            raise ValueError("record() needs at least one sample")
        frames = sys._current_frames()  # one call, O(threads)
        table, main = thread_table(frames), main_thread_stack(frames)
        lines = [f"  {samples[-1]} threads={len(frames)}"]
        lines.extend(self._blocks(table, main, always=False))
        return Rendered("\n".join(lines) + "\n", table, main)

    def _blocks(self, table: List[str], main: List[str], always: bool) -> List[str]:
        written_table, written_main = self._written or (None, None)
        stamp = utc_stamp(time.time())
        lines: List[str] = []
        if always or table != written_table:
            lines.append(
                f"thread table at {stamp} (the newest {FAULTHANDLER_MAX_THREADS} "
                "threads, which is what faulthandler dumps, plus the main thread):"
            )
            lines.extend(table)
        if always or main != written_main:
            lines.append(
                f"main thread stack at {stamp} (most recent call first, at most "
                f"{MAX_SNAPSHOT_FRAMES} frames; taken before any stall, kept "
                "because the dump may omit the main thread):"
            )
            lines.extend(main)
        return lines
