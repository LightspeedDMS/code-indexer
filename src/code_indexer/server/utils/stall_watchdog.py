"""Worker-stall watchdog (Story S12, crash-safe reconcile design #2087, section 11).

Why
---
uvicorn's multiprocess supervisor pings every worker over a pipe and SIGKILLs a
worker whose pong does not arrive within ``timeout_worker_healthcheck`` (5 s,
``uvicorn/supervisors/multiprocess.py``). The pong is sent by a *Python* thread
in the worker, so a worker in which no Python thread can run for 5 s (a C call
holding the GIL, a process starved by swapping) dies without a traceback. This
watchdog leaves the evidence behind before that kill: every thread's stack plus
the memory-pressure samples leading up to the stall.

How
---
Each worker lifetime has two files in ``<server_dir>/logs`` (names and the
finalized log layout: ``stall_watchdog_sweep``): a DUMP file that only
faulthandler writes, and an EVIDENCE file that only the heartbeat writes.
One daemon thread ticks every ``HEARTBEAT_INTERVAL_SECONDS``: it re-arms
``faulthandler.dump_traceback_later`` (timeout ``STALL_DUMP_TIMEOUT_SECONDS``)
on the dump file, then appends one record to the evidence file. If no Python
thread can run, the re-arm stops and faulthandler writes every thread's stack
into the dump file about 3 s into the stall: before the 5 s kill. A stall is
"the dump file is non-empty" -- never a text match. The dump file is armed
BEFORE any evidence is written, including the first snapshot, so protection
never depends on a write.

After a stall the worker survived, a fresh dump file (the next generation) is
armed first -- ``dump_traceback_later`` cancels the old timer and waits for a
dump in progress -- and the old one is finalized into a log together with the
evidence. A worker that died is finalized by the next worker's sweep.

RULE -- one writer per file
---------------------------
faulthandler writes a dump with many small native writes and the heartbeat
writes with Python writes; on one shared descriptor a record could land inside
a dump. Never let the heartbeat write the dump file, or faulthandler the
evidence file.

RULE -- bounded write volume
----------------------------
A steady-state tick appends ONE record line: the memory sample plus
``threads=<n>`` (about 200-300 bytes; about 25 MB a day per worker). Two
blocks are appended only when they CHANGE since the evidence file last
recorded them (``EvidenceRenderer``):

* the id->name table, capped at the ``FAULTHANDLER_MAX_THREADS`` newest
  threads plus the main thread. That is exactly the set a stall dump can show:
  ``sys._current_frames()`` lists threads newest first in the same order
  faulthandler walks them (verified on 3.9.25), so no dumped id is unmapped
  except a thread started after the last heartbeat. At most 101 lines.
* the main thread's stack, at most ``MAX_SNAPSHOT_FRAMES`` frames.

Once the records appended after the evidence file's snapshot reach
``max_file_bytes`` (default 256 KB), a fresh snapshot (header, the sample
ring, table, main stack) is written to a temp file and atomically renamed
over the evidence file. The file is therefore at most one snapshot (about
35 KB worst case) plus ``max_file_bytes`` plus one record. The cap counts
records only, so compaction never repeats on consecutive heartbeats.

Other bounds: a rendered thread name is cut to ``MAX_THREAD_NAME_CHARS`` and a
code filename or function name to ``MAX_FIELD_CHARS`` (control characters
escaped); the NFS field lists at most ``MAX_NFS_MOUNTS`` mounts
(``pressure_sampler``); the sweep's per-file reads, per-pass batch and scan
are bounded (``stall_watchdog_sweep``).

Worker lifetime identity
------------------------
File names carry the worker LIFETIME -- pid plus the process start time in
clock ticks since boot (``/proc/<pid>/stat`` field 22) -- and are created with
``O_EXCL``. A worker that inherits a dead worker's pid never truncates that
worker's files; the startup sweep treats files as a live worker's only if
they were written after this boot (``btime``) and their pid still has those
start ticks.

RULE -- why the dump fires even though the re-arm needs the GIL
---------------------------------------------------------------
``dump_traceback_later`` starts a NATIVE watchdog thread. That thread waits
on a lock with a timeout (``PyThread_acquire_lock_timed``) without holding the
GIL, and on expiry writes every thread's Python frames straight to the file
descriptor with ``write(2)`` (``_Py_DumpTracebackThreads``, the same
async-signal-safe path faulthandler uses for fatal signals), again without
taking the GIL. So the heartbeat thread being unable to re-arm (it needs the
GIL) is exactly what lets the timer expire, and the dump is written while
another thread still holds the GIL. Do not replace this with a Python-level
timer or ``sys._current_frames()``: both need the GIL and would stall with
the worker.

RULE -- faulthandler dumps at most 100 threads, newest first
------------------------------------------------------------
CPython 3.9's ``_Py_DumpTracebackThreads`` walks the thread-state list from its
head, newest thread first, and stops after ``FAULTHANDLER_MAX_THREADS`` (100)
with ``...`` (verified on 3.9.25: 121 threads -> 100 dumped, main thread
missing). The main thread -- the event loop -- is the OLDEST, so a worker with
more than 100 threads can stall with the event loop absent from the dump.
Mitigation: the main thread's stack is always in the evidence -- in the
snapshot, and again whenever it changes. Limits: it is the stack at the LAST
HEARTBEAT (just before, not during, the stall); other old threads are not
recorded; ``threads=<n>`` shows when the dump was truncated. A stall-time stack
would need a kernel timer signal (``faulthandler.register``), which takes over
a process-wide signal, so it is not used.

Always on, no setting (a diagnostic for an unproven kill cause must not be
gated). Never touches uvicorn's health-check pipe and never exits the worker
(``exit=False``).
"""

from __future__ import annotations

import asyncio
import faulthandler
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Optional

import anyio

from code_indexer.server.utils.pressure_sampler import PressureSampler, utc_stamp
from code_indexer.server.utils.stall_watchdog_evidence import (
    FAULTHANDLER_MAX_THREADS,
    MAX_FIELD_CHARS,
    MAX_SNAPSHOT_FRAMES,
    MAX_THREAD_NAME_CHARS,
    DumpFile,
    EvidenceFile,
    EvidenceRenderer,
    read_start_ticks,
)
from code_indexer.server.utils.stall_watchdog_sweep import (
    MAX_KEPT_DUMPS,
    dump_file_path,
    evidence_path,
    evidence_tmp_path,
    finalize_stall_dump,
    logger,
    prune_dumps,
    sweep_dead_workers,
)

__all__ = [
    "FAULTHANDLER_MAX_THREADS",
    "MAX_FIELD_CHARS",
    "MAX_KEPT_DUMPS",
    "MAX_SNAPSHOT_FRAMES",
    "MAX_THREAD_NAME_CHARS",
    "StallWatchdog",
    "start_stall_watchdog",
    "stop_stall_watchdog",
]

# Below uvicorn's 5 s health-check timeout, with >= 1 s margin even when the
# stall starts just before a tick (dump at 2-3 s into the stall).
STALL_DUMP_TIMEOUT_SECONDS = 3.0
HEARTBEAT_INTERVAL_SECONDS = 1.0
# One sample per heartbeat: the minute before a stall.
SAMPLE_RING_SIZE = 60
STOP_JOIN_TIMEOUT_SECONDS = 10.0
# Records appended after a snapshot before compaction ("bounded write volume").
MAX_ARMED_FILE_BYTES = 256 * 1024


class StallWatchdog:
    """Per-worker heartbeat that leaves a stack dump behind a stalled worker."""

    def __init__(
        self,
        log_dir: Path,
        dump_timeout_s: float = STALL_DUMP_TIMEOUT_SECONDS,
        interval_s: float = HEARTBEAT_INTERVAL_SECONDS,
        max_file_bytes: int = MAX_ARMED_FILE_BYTES,
    ) -> None:
        if not 0 < interval_s < dump_timeout_s:
            raise ValueError("need 0 < interval_s < dump_timeout_s")
        if max_file_bytes <= 0:
            raise ValueError("need max_file_bytes > 0")
        self._log_dir = log_dir
        self._dump_timeout_s = dump_timeout_s
        self._interval_s = interval_s
        self._max_file_bytes = max_file_bytes
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._dump: Optional[DumpFile] = None
        self._dump_generation = 0
        self._evidence: Optional[EvidenceFile] = None
        # Evidence size right after its snapshot: the cap counts records only.
        self._snapshot_bytes = 0
        # True while compaction keeps failing: evidence appends are stopped.
        self._evidence_frozen = False
        self._ticks = 0  # this worker lifetime's start ticks, set in _run
        self._sampler = PressureSampler()
        self._samples: Deque[str] = deque(maxlen=SAMPLE_RING_SIZE)
        self._renderer = EvidenceRenderer(dump_timeout_s, interval_s, max_file_bytes)

    def start(self) -> None:
        """Spawn the heartbeat thread. No I/O here: safe inside ``async def``."""
        if self._thread is not None:
            raise RuntimeError("StallWatchdog already started")
        self._thread = threading.Thread(
            target=self._run, name="cidx-stall-watchdog", daemon=True
        )
        self._thread.start()

    def request_stop(self) -> None:
        """Ask the thread to stop; it then cancels the timer and finalizes or
        removes its files on its own. Never blocks: safe on any exit path."""
        self._stop_event.set()

    def stop(self) -> None:
        """Stop the thread and wait (bounded) for it to finish."""
        if self._thread is None:
            return
        self.request_stop()
        self._thread.join(STOP_JOIN_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            logger.error(
                "Worker stall watchdog thread did not stop within %.0fs",
                STOP_JOIN_TIMEOUT_SECONDS,
            )

    def _run(self) -> None:
        pid = os.getpid()
        try:
            self._ticks = read_start_ticks(pid)  # this worker lifetime's identity
            self._log_dir.mkdir(parents=True, exist_ok=True)
            sweep_dead_workers(self._log_dir, self._dump_timeout_s)
            self._samples.append(self._take_sample())
            self._dump = DumpFile(dump_file_path(self._log_dir, pid, self._ticks, 0))
            self._arm()  # BEFORE any evidence is written
            self._evidence = self._write_snapshot(pid, compacting=False)
        except Exception:  # ANY startup failure: clean up, say so, never crash
            self._abandon_start()
            logger.error(
                "Worker stall watchdog could not start in %s; stalls of worker "
                "pid %d will not be captured",
                self._log_dir,
                pid,
                exc_info=True,
            )
            return
        failures = 0
        try:
            while not self._stop_event.is_set():
                try:
                    self._tick(pid)
                    failures = 0
                except Exception:
                    failures += 1
                    if failures == 1:  # once per failure streak, not per second
                        logger.warning(
                            "Worker stall watchdog tick failed in pid %d",
                            pid,
                            exc_info=True,
                        )
                self._stop_event.wait(self._interval_s)
        finally:
            self._shutdown(pid)

    def _abandon_start(self) -> None:
        """Undo a partial start: no timer, no open descriptor, no dump file.
        Never raises (it runs inside the startup failure handler)."""
        faulthandler.cancel_dump_traceback_later()
        dump, self._dump = self._dump, None
        if dump is None:
            return
        try:
            dump.close()
        except OSError:
            logger.warning("Worker stall watchdog could not close %s", dump.path)
        try:
            dump.path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Worker stall watchdog could not remove %s", dump.path)

    def _take_sample(self) -> str:
        """Never raises: a failed sample must not skip the re-arm (that would
        let the timer expire and write a false stall dump)."""
        try:
            return self._sampler.sample()
        except Exception as exc:
            return f"{utc_stamp(time.time())} sample_failed={type(exc).__name__}"

    def _arm(self) -> None:
        # See the module RULE: this re-arm needs the GIL; the timer does not.
        # An int fd is accepted since Python 3.5 and keeps the descriptor's
        # lifetime ours (a file object wrapper would close it when collected).
        assert self._dump is not None, "arm before the dump file was opened"
        faulthandler.dump_traceback_later(
            self._dump_timeout_s, repeat=False, file=self._dump.fd, exit=False
        )

    def _tick(self, pid: int) -> None:
        self._samples.append(self._take_sample())
        assert self._dump is not None, "tick before the dump file was opened"
        if self._dump.has_dump():  # a stall the worker survived
            self._rotate_dump(pid)  # arms a fresh dump file first
        else:
            self._arm()  # BEFORE writing evidence
        self._write_evidence(pid)

    def _rotate_dump(self, pid: int) -> None:
        old = self._dump
        assert old is not None
        next_path = dump_file_path(
            self._log_dir, pid, self._ticks, self._dump_generation + 1
        )
        try:
            fresh = DumpFile(next_path)
        except OSError:
            self._arm()  # keep watching on the old dump file
            raise
        self._dump, self._dump_generation = fresh, self._dump_generation + 1
        self._arm()  # waits for a dump in progress on the old file
        old.close()
        self._report_dump(pid, old.path)

    def _report_dump(self, pid: int, dump: Path) -> None:
        evidence = self._evidence.path if self._evidence is not None else None
        try:
            target = finalize_stall_dump(self._log_dir, pid, dump, evidence)
        except OSError:
            logger.error(
                "Worker stall watchdog could not finalize the stall dump %s "
                "(the dump stays in that file or its .log)",
                dump,
                exc_info=True,
            )
            return
        if target is None:
            return
        logger.error(
            "Worker stall watchdog: worker pid %d could not run Python code for "
            "over %.1fs (a thread held the GIL or the process was starved); every "
            "thread's stack and the memory samples before the stall are in %s",
            pid,
            self._dump_timeout_s,
            target,
        )
        prune_dumps(self._log_dir)

    def _write_evidence(self, pid: int) -> None:
        evidence = self._evidence
        assert evidence is not None, "evidence before the snapshot was written"
        if evidence.written - self._snapshot_bytes >= self._max_file_bytes:
            try:
                fresh = self._write_snapshot(pid, compacting=True)
            except OSError:
                # Bounded writing cannot continue: append nothing (the file
                # stays bounded), say so once, retry next heartbeat. The dump
                # timer was armed before this call, so stalls are still caught.
                if not self._evidence_frozen:
                    self._evidence_frozen = True
                    logger.error(
                        "Worker stall watchdog could not compact its evidence "
                        "file %s; it stops appending heartbeat records until "
                        "compaction succeeds (stall dumps are unaffected)",
                        evidence.path,
                        exc_info=True,
                    )
                return
            if self._evidence_frozen:
                self._evidence_frozen = False
                logger.warning(
                    "Worker stall watchdog compacted %s again; heartbeat "
                    "records resume",
                    evidence.path,
                )
            evidence.close()  # its inode was replaced; this was the last ref
            self._evidence = fresh
            return
        rendered = self._renderer.record(list(self._samples))
        evidence.append(rendered.text)
        self._renderer.commit(rendered)

    def _write_snapshot(self, pid: int, compacting: bool) -> EvidenceFile:
        """A full snapshot: the first evidence file, or (compacting) a temp
        file atomically renamed over it. A failure leaves no partial file."""
        final = evidence_path(self._log_dir, pid, self._ticks)
        path = (
            evidence_tmp_path(self._log_dir, pid, self._ticks) if compacting else final
        )
        fresh = EvidenceFile(path, replace_existing=compacting)
        try:  # ANY failure, rendering included, releases the fresh file
            rendered = self._renderer.snapshot(pid, self._ticks, list(self._samples))
            fresh.append(rendered.text)
            if compacting:
                os.replace(path, final)
        except BaseException:
            fresh.close()
            path.unlink(missing_ok=True)
            raise
        fresh.path = final  # the temp file now lives under the final name
        self._renderer.commit(rendered)
        self._snapshot_bytes = fresh.written
        return fresh

    def _shutdown(self, pid: int) -> None:
        """Each file is handled on its own: an error with one never skips
        closing the other's descriptor."""
        faulthandler.cancel_dump_traceback_later()
        dump, evidence = self._dump, self._evidence
        if dump is not None:
            try:
                if os.fstat(dump.fd).st_size > 0:  # a stall just before the stop
                    self._report_dump(pid, dump.path)
                else:
                    dump.path.unlink()
            except OSError:
                logger.warning(
                    "Worker stall watchdog could not clean up %s",
                    dump.path,
                    exc_info=True,
                )
            finally:
                self._close_quietly(dump)
        if evidence is not None:
            try:
                evidence.path.unlink()
            except OSError:
                logger.warning(
                    "Worker stall watchdog could not clean up %s",
                    evidence.path,
                    exc_info=True,
                )
            finally:
                self._close_quietly(evidence)

    @staticmethod
    def _close_quietly(armed: Any) -> None:
        try:
            armed.close()
        except OSError:
            logger.warning("Worker stall watchdog could not close %s", armed.path)


def start_stall_watchdog(app: Any, server_data_dir: str) -> StallWatchdog:
    """Start this worker's watchdog (once per worker, from the app lifespan)."""
    watchdog = StallWatchdog(Path(server_data_dir) / "logs")
    watchdog.start()
    app.state.stall_watchdog = watchdog
    return watchdog


async def stop_stall_watchdog(app: Any) -> None:
    """Stop this worker's watchdog from the lifespan shutdown, off the loop.

    The stop is REQUESTED before the await, so even if the await is cancelled
    the heartbeat thread still stops and cleans up on its own.
    """
    watchdog: Optional[StallWatchdog] = getattr(app.state, "stall_watchdog", None)
    if watchdog is None:
        return
    app.state.stall_watchdog = None
    watchdog.request_stop()
    await anyio.to_thread.run_sync(watchdog.stop)


async def stop_stall_watchdog_on_exit(app: Any) -> Optional[asyncio.CancelledError]:
    """Lifespan-exit cleanup that never raises.

    A cancellation arriving during the await is RETURNED (the stop was already
    requested), so the caller decides whether it may replace an error that is
    already ending the lifespan. Any other failure is logged.
    """
    try:
        await stop_stall_watchdog(app)
    except asyncio.CancelledError as cancelled:
        return cancelled
    except Exception as exc:
        logger.warning(
            "Story S12: failed to stop the worker stall watchdog on lifespan exit: %s",
            exc,
        )
    return None
