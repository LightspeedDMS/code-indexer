"""CrossProcessBootstrapWriter.install_sigterm_flush: a SIGTERMed indexing
child flushes its buffered stats rows within the parent's grace period, then
still ends BY SIGTERM (-15).

Each signal test runs a real child interpreter (the exit path ends its own
process) writing to a real SQLite EmbeddingCallStatsSqliteBackend. The
PostgreSQL mirror is
tests/unit/server/storage/postgres/test_embedding_stats_sigterm_flush_postgres.py.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import List, Tuple

import pytest

import code_indexer
from code_indexer.server.services.embedding_stats_writer import (
    CrossProcessBootstrapWriter,
)
from code_indexer.utils.process_group import (
    SIGTERM_GRACE_SECONDS,
    terminate_process_group,
)

SRC_DIR = Path(code_indexer.__file__).resolve().parents[1]

pytestmark = pytest.mark.timeout(120)

#: Child body after `backend` is built. Run as
#: `python -c <code> <backend arg> <record_count> <mode> <marker>`: under -c,
#: sys.argv is ['-c', backend arg, record_count, mode, marker]. Modes:
#: plain -- record, then idle; slow -- every insert first writes the marker
#: and takes 0.5 s; signal_in_put -- the LAST record raises SIGTERM inside
#: Queue._put, i.e. while the main thread holds the queue's mutex;
#: inflight_slow / inflight_stuck -- the periodic writer (0.05 s interval)
#: writes the marker inside its FIRST insert, which then takes 0.6 s / far
#: longer than the grace period; hold_queue_mutex / hold_accounting /
#: hold_log_lock -- after the last record a thread takes that lock for good,
#: then writes the marker. `Path` and `sys` come from the head script. The
#: idle loop is bounded (120 s); every test ends the child well before that.
CHILD_TAIL = textwrap.dedent(
    """
    import logging, os, signal, threading, time
    from queue import Queue
    from code_indexer.server.services.embedding_call_stats import (
        EmbeddingCallRecord,
    )
    from code_indexer.server.services.embedding_stats_writer import (
        CrossProcessBootstrapWriter,
    )

    logging.basicConfig(level=logging.WARNING)
    record_count, mode, marker = int(sys.argv[2]), sys.argv[3], sys.argv[4]

    if mode == "slow":
        _insert = backend.insert_batch

        def _slow_insert(records, **kwargs):
            Path(marker).write_text("flushing")
            time.sleep(0.5)
            _insert(records, **kwargs)

        backend.insert_batch = _slow_insert

    if mode.startswith("inflight"):
        _periodic_insert = backend.insert_batch
        _first = [True]

        def _blocking_first_insert(records, **kwargs):
            if _first[0]:
                _first[0] = False
                Path(marker).write_text("in flight")
                time.sleep(0.6 if mode == "inflight_slow" else 30)
            _periodic_insert(records, **kwargs)

        backend.insert_batch = _blocking_first_insert


    class SignallingQueue(Queue):
        armed = False

        def _put(self, item):
            super()._put(item)
            if self.armed:
                os.kill(os.getpid(), signal.SIGTERM)
                # The handler runs at the next bytecode boundary, still
                # inside Queue.put's `with self.not_full` block.
                for _ in range(1000):
                    pass


    writer = CrossProcessBootstrapWriter(
        backend,
        flush_interval_seconds=0.05 if mode.startswith("inflight") else 3600,
    )
    writer._queue = SignallingQueue(maxsize=100_000)
    writer.start()
    assert writer.install_sigterm_flush()


    def _record():
        writer.record(EmbeddingCallRecord(
            provider="voyageai", call_type="embed", model="voyage-code-3",
            item_count=1, token_count=10, batch_size=1, purpose="refresh",
            success=True, latency_ms=5, occurred_at=time.time(),
        ))


    for _ in range(record_count - 1):
        _record()
    print("READY", flush=True)
    if mode == "signal_in_put":
        writer._queue.armed = True
    _record()

    _HELD = {
        "hold_queue_mutex": lambda: writer._queue.mutex,
        "hold_accounting": lambda: writer._accounting,
        "hold_log_lock": lambda: logging.getLogger().handlers[0].lock,
    }


    def _hold_for_good(lock):
        lock.acquire()
        Path(marker).write_text("held")
        threading.Event().wait(120)


    if mode in _HELD:
        threading.Thread(
            target=_hold_for_good, args=(_HELD[mode](),), daemon=True
        ).start()
    for _ in range(120):
        time.sleep(1)
    """
)

_SQLITE_HEAD = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from code_indexer.server.services.embedding_call_stats import (
        EmbeddingCallStatsSqliteBackend,
    )

    backend = EmbeddingCallStatsSqliteBackend(sys.argv[1])
    """
)


def spawn_child(
    head: str, backend_arg: str, record_count: int, mode: str, marker: str = ""
) -> subprocess.Popen:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            head + CHILD_TAIL,
            backend_arg,
            str(record_count),
            mode,
            marker,
        ],
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert proc.stdout is not None and proc.stderr is not None
    line = proc.stdout.readline()
    assert line.strip() == "READY", f"child failed: {proc.stderr.read()[-3000:]}"
    return proc


def wait_dead(proc: subprocess.Popen, timeout: float) -> Tuple[float, str]:
    """(seconds until the child died, its stderr); SIGKILLs it after
    `timeout` so a hang fails the test instead of the run."""
    started = time.monotonic()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    elapsed = time.monotonic() - started
    assert proc.stderr is not None
    return elapsed, proc.stderr.read()


def _spawn(db_path: Path, record_count: int, mode: str, marker: str = ""):
    return spawn_child(_SQLITE_HEAD, str(db_path), record_count, mode, marker)


def _rows(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(
            conn.execute("SELECT COUNT(*) FROM embedding_call_stats").fetchone()[0]
        )
    finally:
        conn.close()


def test_sigterm_flush_cost_fits_parent_grace_period(tmp_path: Path) -> None:
    """One full drain batch (1000 rows) is flushed inside the parent's grace
    period: the child ends by its own SIGTERM, never the parent's SIGKILL."""
    db_path = tmp_path / "cidx_server.db"
    proc = _spawn(db_path, 1000, "plain")
    started = time.monotonic()
    terminate_process_group(proc)
    elapsed = time.monotonic() - started

    assert proc.returncode == -signal.SIGTERM, f"exit {proc.returncode}"
    assert _rows(db_path) == 1000
    assert elapsed < SIGTERM_GRACE_SECONDS, f"flush took {elapsed:.3f}s"


def test_multi_batch_drain_fits_grace_period(tmp_path: Path) -> None:
    """2500 rows (three drain batches) are all persisted inside the grace
    period."""
    db_path = tmp_path / "cidx_server.db"
    proc = _spawn(db_path, 2500, "plain")
    started = time.monotonic()
    terminate_process_group(proc)
    elapsed = time.monotonic() - started

    assert proc.returncode == -signal.SIGTERM, f"exit {proc.returncode}"
    assert _rows(db_path) == 2500
    assert elapsed < SIGTERM_GRACE_SECONDS, f"drain took {elapsed:.3f}s"


def test_sigterm_while_main_thread_holds_queue_lock_persists_rows(
    tmp_path: Path,
) -> None:
    """SIGTERM handled while the main thread is inside Queue.put (holding the
    queue's non-reentrant mutex): the flush must not need that lock from the
    handler, so every row, the interrupted one included, is persisted."""
    db_path = tmp_path / "cidx_server.db"
    proc = _spawn(db_path, 50, "signal_in_put")
    elapsed, stderr = wait_dead(proc, SIGTERM_GRACE_SECONDS + 3)

    assert proc.returncode == -signal.SIGTERM, f"exit {proc.returncode}: {stderr}"
    assert _rows(db_path) == 50, stderr[-3000:]
    assert elapsed < SIGTERM_GRACE_SECONDS, f"took {elapsed:.3f}s"


def test_locked_stats_db_still_ends_within_grace_and_warns(tmp_path: Path) -> None:
    """Another connection holds the stats DB's write lock: the drain gives up
    at its deadline, the child still ends by SIGTERM inside the grace period
    and logs how many rows it could not persist."""
    db_path = tmp_path / "cidx_server.db"
    proc = _spawn(db_path, 2500, "plain")
    blocker = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        terminate_process_group(proc)
        elapsed = time.monotonic() - started
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert proc.stderr is not None
    stderr = proc.stderr.read()

    assert proc.returncode == -signal.SIGTERM, f"exit {proc.returncode}: {stderr}"
    assert elapsed < SIGTERM_GRACE_SECONDS, f"took {elapsed:.3f}s"
    assert "2500 embedding-call stats rows not persisted" in stderr, stderr[-3000:]
    assert _rows(db_path) == 0


def test_second_sigterm_during_flush_neither_hangs_nor_skips_flush(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "cidx_server.db"
    marker = tmp_path / "flushing.marker"
    proc = _spawn(db_path, 5, "slow", str(marker))
    os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while not marker.exists() and proc.poll() is None:
        assert time.monotonic() < deadline, "the flush never started"
        time.sleep(0.01)
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)  # lands during the slow flush
    _elapsed, stderr = wait_dead(proc, SIGTERM_GRACE_SECONDS + 3)

    assert proc.returncode == -signal.SIGTERM, f"exit {proc.returncode}: {stderr}"
    assert _rows(db_path) == 5


def _wait_marker(proc: subprocess.Popen, marker: Path) -> None:
    deadline = time.monotonic() + 10
    while not marker.exists():
        assert proc.poll() is None, "the child died before the window opened"
        assert time.monotonic() < deadline, "the window never opened"
        time.sleep(0.01)


def _sigterm_in_window(
    tmp_path: Path, record_count: int, mode: str
) -> Tuple[Path, str]:
    """Spawn a child in `mode` and SIGTERM it once its window is open; assert
    it ended by SIGTERM inside the grace period. Returns (db path, stderr)."""
    db_path = tmp_path / "cidx_server.db"
    marker = tmp_path / "window.marker"
    proc = _spawn(db_path, record_count, mode, str(marker))
    _wait_marker(proc, marker)
    os.killpg(proc.pid, signal.SIGTERM)
    elapsed, stderr = wait_dead(proc, SIGTERM_GRACE_SECONDS + 3)
    assert proc.returncode == -signal.SIGTERM, f"exit {proc.returncode}: {stderr}"
    assert elapsed < SIGTERM_GRACE_SECONDS, f"took {elapsed:.3f}s: {stderr}"
    return db_path, stderr


def test_sigterm_during_periodic_insert_persists_in_flight_rows(
    tmp_path: Path,
) -> None:
    """SIGTERM lands while the periodic writer is inside its insert (the batch
    is already off the queue): the shutdown waits for that insert within the
    deadline, so no row is lost."""
    db_path, stderr = _sigterm_in_window(tmp_path, 50, "inflight_slow")

    assert _rows(db_path) == 50, stderr[-3000:]


def test_sigterm_during_stuck_periodic_insert_counts_in_flight_rows(
    tmp_path: Path,
) -> None:
    """The periodic writer's insert outlasts the deadline: the child still
    ends by SIGTERM inside the grace period, and the WARNING counts the
    in-flight rows with the queued ones."""
    db_path, stderr = _sigterm_in_window(tmp_path, 50, "inflight_stuck")

    assert "50 embedding-call stats rows not persisted" in stderr, stderr[-3000:]
    assert _rows(db_path) == 0


def test_stalled_queue_mutex_past_deadline_still_ends_by_sigterm_and_warns(
    tmp_path: Path,
) -> None:
    """A thread holds the queue's mutex for good, so the drain stalls: after
    the deadline nothing may block the final SIGTERM, and the WARNING is
    still logged."""
    _db_path, stderr = _sigterm_in_window(tmp_path, 50, "hold_queue_mutex")

    assert "not persisted before the SIGTERM exit" in stderr, stderr[-3000:]


def test_stalled_accounting_lock_reports_unknown_count(tmp_path: Path) -> None:
    """A thread holds the row accounting lock for good: the report cannot
    read the count, so it says the number is unknown, and the child still
    ends by SIGTERM inside the grace period."""
    db_path, stderr = _sigterm_in_window(tmp_path, 50, "hold_accounting")

    assert "an unknown number of embedding-call stats rows not persisted" in stderr, (
        stderr[-3000:]
    )
    assert _rows(db_path) == 0


def test_stalled_logging_lock_cannot_hold_the_exit(tmp_path: Path) -> None:
    """The stats DB is write-locked (so the drain fails and rows must be
    reported) and a thread holds the logging handler's lock for good: the
    WARNING cannot be written, yet the child still ends by SIGTERM inside
    the grace period."""
    db_path = tmp_path / "cidx_server.db"
    marker = tmp_path / "window.marker"
    proc = _spawn(db_path, 50, "hold_log_lock", str(marker))
    _wait_marker(proc, marker)
    blocker = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        os.killpg(proc.pid, signal.SIGTERM)
        elapsed, stderr = wait_dead(proc, SIGTERM_GRACE_SECONDS + 3)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert proc.returncode == -signal.SIGTERM, f"exit {proc.returncode}: {stderr}"
    assert elapsed < SIGTERM_GRACE_SECONDS, f"took {elapsed:.3f}s"
    assert _rows(db_path) == 0


_FORKING_CHILD = textwrap.dedent(
    """
    import multiprocessing, os, signal, sys, time
    from code_indexer.server.services.embedding_call_stats import (
        EmbeddingCallStatsSqliteBackend,
    )
    from code_indexer.server.services.embedding_stats_writer import (
        CrossProcessBootstrapWriter,
    )

    writer = CrossProcessBootstrapWriter(
        EmbeddingCallStatsSqliteBackend(sys.argv[1]), flush_interval_seconds=3600
    )
    writer.start()
    assert writer.install_sigterm_flush()
    forked = multiprocessing.get_context("fork").Process(target=time.sleep, args=(30,))
    forked.start()
    os.kill(forked.pid, signal.SIGTERM)
    forked.join(5)
    print(forked.exitcode, flush=True)
    if forked.exitcode is None:
        forked.kill()
    time.sleep(1.0)  # a wrongly woken flusher would end THIS process now
    print("ALIVE", flush=True)
    """
)


def test_forked_child_dies_by_sigterm_without_waking_the_parent(
    tmp_path: Path,
) -> None:
    """A forked descendant inherits the handler and the wake pipe but not the
    flusher: its SIGTERM must end it by default, never wake (and so end) the
    process that installed the flush."""
    proc = subprocess.run(
        [sys.executable, "-c", _FORKING_CHILD, str(tmp_path / "cidx_server.db")],
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.stdout.split() == [str(-signal.SIGTERM), "ALIVE"], (
        f"exit {proc.returncode}; stdout {proc.stdout!r}; {proc.stderr[-2000:]}"
    )
    assert proc.returncode == 0


class _UnusedBackend:
    def insert_batch(self, records: List[object]) -> None:
        raise AssertionError("never flushed in this test")


def test_install_off_main_thread_is_refused() -> None:
    """Signal handlers exist only on the main thread: elsewhere the install
    reports False and leaves the SIGTERM disposition untouched."""
    writer = CrossProcessBootstrapWriter(_UnusedBackend())
    before = signal.getsignal(signal.SIGTERM)
    results: List[bool] = []
    worker = threading.Thread(
        target=lambda: results.append(writer.install_sigterm_flush())
    )
    worker.start()
    worker.join(timeout=10)

    assert results == [False]
    assert signal.getsignal(signal.SIGTERM) is before
