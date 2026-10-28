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
class _ActiveDump:
    pid: int
    path: Path
    # ^ hazard closing this file while faulthandler is armed => a crash dump lands in whatever reuses the fd
    file: IO[str]


_active: Optional[_ActiveDump] = None
_lock = threading.Lock()
_dumping = False


# ^ invariant returns the dump path, or None after one logged WARNING; never raises
def enable_crash_dumps(logs_dir: Path) -> Optional[Path]:
    with _lock:
        path = _enable_locked(logs_dir)
    if path is not None:
        _sweep_dead_empty_dumps(logs_dir, keep=path)
    return path


def _enable_locked(logs_dir: Path) -> Optional[Path]:
    global _active
    pid = os.getpid()
    if _active is not None and _active.pid == pid:
        return _active.path
    try:
        path = logs_dir / f"{DUMP_FILE_PREFIX}{pid}-{read_start_ticks(pid)}.log"
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
    try:
        faulthandler.enable(file=dump_file, all_threads=True)
    except _FAULTHANDLER_ERRORS as exc:
        dump_file.close()
        logger.warning(
            "faulthandler crash dumps disabled: %s: %s", type(exc).__name__, exc
        )
        return None
    _active = _ActiveDump(pid, path, dump_file)
    try:
        signal.signal(DUMP_SIGNAL, _dump_all_threads)
    except (OSError, ValueError) as exc:
        logger.warning(
            "SIGUSR1 on-demand dumps unavailable (fatal-signal dumps still go to "
            "%s): %s: %s",
            path,
            type(exc).__name__,
            exc,
        )
    return path


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


def _sweep_dead_empty_dumps(logs_dir: Path, keep: Path) -> None:
    try:
        booted_at = boot_time()
        with os.scandir(logs_dir) as entries:
            for seen, entry in enumerate(entries):
                if seen >= MAX_SWEEP_ENTRIES:
                    break
                match = _DUMP_NAME.match(entry.name)
                if match is None or entry.path == str(keep):
                    continue
                stat = entry.stat(follow_symlinks=False)
                if stat.st_size != 0:
                    continue
                pid, ticks = int(match.group(1)), int(match.group(2))
                if lifetime_is_running(pid, ticks, stat.st_mtime, booted_at):
                    continue
                try:
                    os.unlink(entry.path)
                # ^ order workers starting together sweep the same dir; a peer may unlink before this one
                except FileNotFoundError:
                    continue
    except OSError as exc:
        logger.warning(
            "faulthandler dump sweep of %s stopped: %s: %s",
            logs_dir,
            type(exc).__name__,
            exc,
        )
