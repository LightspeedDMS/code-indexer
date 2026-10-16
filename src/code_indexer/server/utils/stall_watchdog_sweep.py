"""Stall-log assembly, dead-worker sweep and rotation (Story S12).

Per worker LIFETIME (pid, start ticks) the watchdog keeps, in the log dir:

* ``worker-stall-<pid>-<ticks>.evidence`` -- heartbeat evidence; only the
  heartbeat writes it (``.evidence.tmp`` is its compaction temp file);
* ``worker-stall-<pid>-<ticks>.<gen>.dump`` -- faulthandler's dump file; only
  faulthandler writes it. A stall is a NON-EMPTY dump file, never a text match.

A stall's finalized log is ``worker-stall-<pid>-<UTC>.log``: the evidence, a
``=== faulthandler dump (...) ===`` line, then the dump. A worker SIGKILLed
mid-stall (uvicorn's health check) or by the OOM killer cannot log; the next
worker to start sweeps its files.

Bounds: a file read here is cut to MAX_EVIDENCE_READ_BYTES (evidence) or
MAX_DUMP_READ_BYTES (dump), head and tail kept; one startup pass collects no
more than MAX_SWEEP_MATCHED_FILES lifetime or log files (unrelated entries
cost one name match and never count, so they cannot hide a dump) and
finalizes no more than MAX_SWEEP_LIFETIMES dead lifetimes (the next worker
start takes the rest -- a finalized lifetime leaves the matched set, so
successive passes always advance); MAX_KEPT_DUMPS logs are kept.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from code_indexer.server.utils.pressure_sampler import utc_stamp
from code_indexer.server.utils.stall_watchdog_evidence import (
    boot_time,
    lifetime_is_running,
    read_bounded,
)

# One logger for the whole feature, so operators see one name.
logger = logging.getLogger("code_indexer.server.utils.stall_watchdog")

STALL_FILE_PREFIX = "worker-stall-"
MAX_KEPT_DUMPS = 20
MAX_SWEEP_LIFETIMES = 32
# Most lifetime or log files one pass collects. Unrelated entries cost one
# name match each (no stat) and never count, so they cannot hide a dump.
MAX_SWEEP_MATCHED_FILES = 10_000
MAX_EVIDENCE_READ_BYTES = 1024 * 1024
MAX_DUMP_READ_BYTES = 4 * 1024 * 1024
DUMP_SECTION_PREFIX = "=== faulthandler dump"

LIFETIME_FILE_RE = re.compile(
    rf"^{re.escape(STALL_FILE_PREFIX)}(\d+)-(\d+)\.(evidence|evidence\.tmp|\d+\.dump)$"
)
DUMP_NAME_RE = re.compile(rf"^{re.escape(STALL_FILE_PREFIX)}\d+-\d{{8}}T\d{{9}}Z\.log$")


def evidence_path(log_dir: Path, pid: int, ticks: int) -> Path:
    return log_dir / f"{STALL_FILE_PREFIX}{pid}-{ticks}.evidence"


def evidence_tmp_path(log_dir: Path, pid: int, ticks: int) -> Path:
    return log_dir / f"{STALL_FILE_PREFIX}{pid}-{ticks}.evidence.tmp"


def dump_file_path(log_dir: Path, pid: int, ticks: int, generation: int) -> Path:
    return log_dir / f"{STALL_FILE_PREFIX}{pid}-{ticks}.{generation}.dump"


def stall_log_path(log_dir: Path, pid: int, mtime: float) -> Path:
    """``worker-stall-<pid>-<UTC of the last write>.log``."""
    return log_dir / f"{STALL_FILE_PREFIX}{pid}-{utc_stamp(mtime)}.log"


def finalize_stall_dump(
    log_dir: Path, pid: int, dump: Path, evidence: Optional[Path]
) -> Optional[Path]:
    """Turn a non-empty dump file into the stall's log; None if another
    starting worker claimed it first.

    The dump is CLAIMED by renaming it to its final name (atomic: one winner),
    then the bounded evidence is put in front of it. If that second step
    fails, the claimed log still holds the raw dump.
    """
    try:
        target = stall_log_path(log_dir, pid, dump.stat().st_mtime)
        os.rename(dump, target)
    except FileNotFoundError:
        return None
    dump_text = read_bounded(target, MAX_DUMP_READ_BYTES)
    try:
        if evidence is None:
            raise FileNotFoundError("no evidence file")
        evidence_text = read_bounded(evidence, MAX_EVIDENCE_READ_BYTES)
    except FileNotFoundError:
        evidence_text = "(no heartbeat evidence file)\n"
    combined = (
        evidence_text.rstrip("\n")
        + f"\n{DUMP_SECTION_PREFIX} ({dump.name}) ===\n"
        + dump_text
    )
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(combined, encoding="utf-8")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def prune_dumps(log_dir: Path) -> None:
    """Keep the newest ``MAX_KEPT_DUMPS`` logs (of all workers)."""
    try:
        dumps = []
        for path in _matching_paths(log_dir, DUMP_NAME_RE):
            try:
                dumps.append((path.stat().st_mtime, path))
            except FileNotFoundError:
                continue  # a sibling worker pruned it first
        dumps.sort(reverse=True)
        for _, path in dumps[MAX_KEPT_DUMPS:]:
            path.unlink(missing_ok=True)
    except OSError:
        logger.warning(
            "Worker stall watchdog could not prune old dumps in %s",
            log_dir,
            exc_info=True,
        )


def sweep_dead_workers(log_dir: Path, dump_timeout_s: float) -> None:
    """Report the files of worker lifetimes that ended without a clean stop."""
    try:
        booted_at = boot_time()
        by_lifetime: Dict[Tuple[int, int], List[Path]] = {}
        for path in _matching_paths(log_dir, LIFETIME_FILE_RE):
            match = LIFETIME_FILE_RE.match(path.name)
            if match:
                key = (int(match.group(1)), int(match.group(2)))
                by_lifetime.setdefault(key, []).append(path)
        dead = [
            (key, paths)
            for key, paths in sorted(by_lifetime.items())
            if not _is_running(key, paths, booted_at)
        ]
    except OSError:
        logger.warning(
            "Worker stall watchdog could not list %s for dead workers",
            log_dir,
            exc_info=True,
        )
        dead = []
    for (pid, ticks), paths in dead[:MAX_SWEEP_LIFETIMES]:
        try:  # one unreadable lifetime must not hide the others' dumps
            _finalize_dead_lifetime(log_dir, pid, ticks, paths, dump_timeout_s)
        except OSError:
            logger.warning(
                "Worker stall watchdog could not report the files of dead "
                "worker pid %d in %s",
                pid,
                log_dir,
                exc_info=True,
            )
    if len(dead) > MAX_SWEEP_LIFETIMES:
        logger.warning(
            "Worker stall watchdog: %d more dead worker lifetimes in %s are left "
            "for the next worker start (at most %d per start)",
            len(dead) - MAX_SWEEP_LIFETIMES,
            log_dir,
            MAX_SWEEP_LIFETIMES,
        )
    prune_dumps(log_dir)


def _matching_paths(log_dir: Path, pattern: "re.Pattern[str]") -> List[Path]:
    """Entries whose NAME matches ``pattern``, at most MAX_SWEEP_MATCHED_FILES.
    Other entries cost one name match (no stat) and never count, so unrelated
    files cannot hide a dump; a finalized lifetime leaves the matched set, so
    each pass advances."""
    paths: List[Path] = []
    with os.scandir(log_dir) as entries:
        for entry in entries:
            if not pattern.match(entry.name):
                continue
            if len(paths) >= MAX_SWEEP_MATCHED_FILES:
                logger.warning(
                    "Worker stall watchdog collected only the first %d matching "
                    "files of %s this pass",
                    MAX_SWEEP_MATCHED_FILES,
                    log_dir,
                )
                break
            paths.append(Path(entry.path))
    return paths


def _is_running(key: Tuple[int, int], paths: List[Path], booted_at: float) -> bool:
    mtimes = []
    for path in paths:
        try:
            mtimes.append(path.stat().st_mtime)
        except FileNotFoundError:
            continue  # a sibling worker already took it
    if not mtimes:
        return True  # nothing left to report
    return lifetime_is_running(key[0], key[1], max(mtimes), booted_at)


def _finalize_dead_lifetime(
    log_dir: Path, pid: int, ticks: int, paths: List[Path], dump_timeout_s: float
) -> None:
    evidence = evidence_path(log_dir, pid, ticks)
    stalled = False
    for dump in sorted(p for p in paths if p.name.endswith(".dump")):
        try:
            if dump.stat().st_size == 0:  # armed, never fired
                dump.unlink(missing_ok=True)
                continue
        except FileNotFoundError:
            continue  # a sibling worker already took it
        stalled = True
        target = finalize_stall_dump(log_dir, pid, dump, evidence)
        if target is not None:
            logger.error(
                "Worker stall watchdog: worker pid %d could not run "
                "Python code for over %.1fs and then died (uvicorn kills "
                "a worker that misses its health check); every thread's "
                "stack and the memory samples before the stall are in %s",
                pid,
                dump_timeout_s,
                target,
            )
    if not stalled:
        _keep_evidence_of_unclean_exit(log_dir, pid, evidence)
    evidence.unlink(missing_ok=True)
    evidence_tmp_path(log_dir, pid, ticks).unlink(missing_ok=True)


def _keep_evidence_of_unclean_exit(log_dir: Path, pid: int, evidence: Path) -> None:
    try:
        target = stall_log_path(log_dir, pid, evidence.stat().st_mtime)
        os.rename(evidence, target)
    except FileNotFoundError:
        return  # no evidence, or a sibling worker took it
    logger.warning(
        "Worker stall watchdog: worker pid %d ended without a clean "
        "shutdown and without a stall dump (not a stalled worker: "
        "e.g. an OOM kill or a SIGKILL); its last memory samples "
        "are in %s",
        pid,
        target,
    )
