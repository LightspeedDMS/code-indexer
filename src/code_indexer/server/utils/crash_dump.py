# ^ see docs/server/auto-update.md; #2299 per-process dump file faulthandler-<pid>-<start ticks>.log in the server logs dir
# ^ hazard fatal-signal dumps come from faulthandler (C) => at most 100 threads, newest first; the whole core is the full record
# ^ why Python SIGUSR1 handler, not faulthandler.register; register shares the 100-thread cap and #2228 had ~190 threads

from __future__ import annotations

import faulthandler
import logging
import os
import re
import signal
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import IO, List, Optional

from code_indexer.server.utils.stall_watchdog_evidence import (
    boot_time,
    lifetime_is_running,
    read_start_ticks,
)

logger = logging.getLogger(__name__)

DUMP_FILE_PREFIX = "faulthandler-"
_DUMP_NAME = re.compile(r"^faulthandler-(\d+)-(\d+)\.log$")
MAX_SWEEP_ENTRIES = 10_000
MAX_FRAMES_PER_THREAD = 100
DUMP_SIGNAL = signal.SIGUSR1
SECTION_HEADER = "=== SIGUSR1 dump"
_FAULTHANDLER_ERRORS = (OSError, RuntimeError, ValueError)


@dataclass
class DumpFile:
    pid: int
    path: Path
    # ^ hazard closing this file while faulthandler is armed => a crash dump lands in whatever reuses the fd
    file: IO[str]


_active: Optional[DumpFile] = None
_lock = threading.Lock()
_dumping = False


# ^ invariant returns an open dump file, or None after one logged WARNING; never raises
# ^ perf reads /proc, mkdir and open => async callers run it via anyio.to_thread
def prepare_dump_file(logs_dir: Path) -> Optional[DumpFile]:
    pid = os.getpid()
    try:
        path = logs_dir / f"{DUMP_FILE_PREFIX}{pid}-{read_start_ticks(pid)}.log"
        active = _active
        # ^ why reuse the armed handle, not open a second one; arm_crash_dumps then has nothing to close
        if active is not None and active.pid == pid and active.path == path:
            return active
        logs_dir.mkdir(parents=True, exist_ok=True)
        # ^ why append, not "w"; a reused pid+ticks after a reboot must not truncate an older crash
        dump_file = open(path, "a", encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "faulthandler crash dumps disabled: cannot open a dump file in %s (%s: %s)",
            logs_dir,
            type(exc).__name__,
            exc,
        )
        return None
    return DumpFile(pid, path, dump_file)


# ^ invariant returns the dump path, or None after one logged WARNING; never raises
# ^ why the calling thread, not anyio.to_thread; faulthandler.enable sets its alternate signal stack per calling thread
def arm_crash_dumps(prepared: DumpFile) -> Optional[Path]:
    with _lock:
        return _arm_locked(prepared)


def _arm_locked(prepared: DumpFile) -> Optional[Path]:
    global _active
    current = _active
    previous = current if current is not None and current.pid == prepared.pid else None
    # ^ why reuse only for the same file, not any dir; a later app in this process (e2e create_app) dumps into its own logs dir
    if previous is not None and previous.path == prepared.path:
        # ^ invariant a racing second prepare => its duplicate handle never reached faulthandler
        if prepared.file is not previous.file:
            prepared.file.close()
        return previous.path
    try:
        faulthandler.enable(file=prepared.file, all_threads=True)
    except _FAULTHANDLER_ERRORS as exc:
        prepared.file.close()
        logger.warning(
            "faulthandler crash dumps disabled: %s: %s", type(exc).__name__, exc
        )
        return None
    _active = prepared
    # ^ why keep the previous file open, not close it; a SIGUSR1 handler holding it mid-render must finish its write
    # ^ why skip signal.signal on a dir move, not re-install; _dump_all_threads reads _active and the first enable reported SIGUSR1
    if previous is not None:
        return prepared.path
    try:
        signal.signal(DUMP_SIGNAL, _dump_all_threads)
    except (OSError, ValueError) as exc:
        logger.warning(
            "SIGUSR1 on-demand dumps unavailable (fatal-signal dumps still go to "
            "%s): %s: %s",
            prepared.path,
            type(exc).__name__,
            exc,
        )
    return prepared.path


def _render_all_threads() -> str:
    names = {t.ident: t.name for t in threading.enumerate()}
    frames = sys._current_frames()
    stamp = datetime.now(timezone.utc).isoformat()
    lines: List[str] = [
        f"{SECTION_HEADER} pid={os.getpid()} utc={stamp} threads={len(frames)} ==="
    ]
    for ident, frame in frames.items():
        name = names.get(ident, "<unknown>")
        lines.append(f"Thread {ident:#x} {name!r} (most recent call first):")
        stack = traceback.StackSummary.extract(
            traceback.walk_stack(frame),
            limit=MAX_FRAMES_PER_THREAD,
            lookup_lines=False,
        )
        for entry in stack:
            lines.append(
                f'  File "{entry.filename}", line {entry.lineno} in {entry.name}'
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def _dump_all_threads(signum: int, frame: Optional[FrameType]) -> None:
    global _dumping
    active = _active
    if active is None or _dumping:
        return
    _dumping = True
    try:
        # ^ why the dump file, not logger, for failures; the handler may interrupt code holding a logging queue Lock
        try:
            text = _render_all_threads()
        except Exception as exc:
            text = f"{SECTION_HEADER} failed: {type(exc).__name__}: {exc} ===\n"
        try:
            active.file.write(text)
            active.file.flush()
        # ^ why pass, not log; the dump file write failed and logger may deadlock in this handler
        except (OSError, ValueError):
            pass
    finally:
        _dumping = False


# ^ perf sync scandir + stat of up to MAX_SWEEP_ENTRIES => async callers run it via anyio.to_thread
def sweep_dead_empty_dumps(logs_dir: Path, keep: Path) -> None:
    try:
        booted_at = boot_time()
        with os.scandir(logs_dir) as entries:
            for seen, entry in enumerate(entries):
                if seen >= MAX_SWEEP_ENTRIES:
                    break
                match = _DUMP_NAME.match(entry.name)
                if match is None or entry.path == str(keep):
                    continue
                # ^ order workers starting together sweep the same dir; a peer may unlink after listing, before stat or unlink
                try:
                    stat = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.st_size != 0:
                    continue
                pid, ticks = int(match.group(1)), int(match.group(2))
                if lifetime_is_running(pid, ticks, stat.st_mtime, booted_at):
                    continue
                try:
                    os.unlink(entry.path)
                except FileNotFoundError:
                    continue
    except OSError as exc:
        logger.warning(
            "faulthandler dump sweep of %s stopped: %s: %s",
            logs_dir,
            type(exc).__name__,
            exc,
        )
