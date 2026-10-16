"""Embedding stats writer registry + NoOp writer -- Story #1418.

``NoOpWriter`` is the DEFAULT active writer -- any code that calls
``EmbeddingStatsWriter.get_active().record(...)`` before ``set_active()`` is
ever invoked safely no-ops. This is critical for standalone CLI usage (no
server orchestration) and as the fail-open kill-switch result (see
``_is_enabled()`` below). The module-level ``_active`` slot mirrors the established
per-process singleton pattern used elsewhere in this codebase (e.g.
``api_metrics_service = ApiMetricsService()``) -- it selects WHICH writer
instance this process uses, it is not itself cross-request/cross-node data
(the ``embedding_call_stats`` table is the actual shared source of truth).
A lock guards lazy-init/replacement so concurrent uvicorn worker threads
cannot race into duplicate default instances or a torn assignment.
"""

from __future__ import annotations

import logging
<<<<<<< HEAD
import threading
=======
import os
import signal
import threading
import time
>>>>>>> upstream/master
from abc import ABC, abstractmethod
from queue import Empty, Full, Queue
from typing import TYPE_CHECKING, Any, Callable, ClassVar, List, Optional

if TYPE_CHECKING:
    # Bug #1441: this is a type-annotation-only need (all annotations in
    # this module are lazy strings via `from __future__ import
    # annotations` above) -- NOT a runtime dependency. NoOpWriter (the
    # last-resort, ZERO-dependency fallback writer) and EmbeddingStatsWriter
    # itself must import cleanly on an interpreter with no postgres
    # packages installed at all; a real top-level import here would pull
    # in embedding_call_stats.py -> connection_pool.py -> `import psycopg`
    # unconditionally, defeating that guarantee.
    from code_indexer.server.services.embedding_call_stats import (
        EmbeddingCallRecord,
    )

logger = logging.getLogger(__name__)

# Bounded drain per flush cycle (MESSI #14 anti-unbounded-loop) -- mirrors
# SearchEmbedEventWriter's _MAX_DRAIN_BATCH.
_MAX_DRAIN_BATCH = 1_000

# Default periodic flush interval, and the static fallback for both writer
# types when no live config value is available (in-process: provider
# exception; cross-process: bootstrap config read failure). MUST match
# EmbeddingStatsConfig.flush_interval_seconds's default so config-driven
# tuning never silently changes this pre-existing behavior.
_DEFAULT_FLUSH_INTERVAL_SECONDS = 30.0

<<<<<<< HEAD
=======
# ONE deadline for the whole SIGTERM-time drain of a `cidx index` child.
# Below the parent's grace period (process_group.SIGTERM_GRACE_SECONDS,
# 2.0 s), leaving time for the main thread to end the process by its own
# SIGTERM before the parent's SIGKILL.
_SIGTERM_DRAIN_BUDGET_SECONDS = 1.2
# Lock wait of each insert in that drain (SQLite busy timeout, PostgreSQL
# lock/statement timeout), so one blocked insert cannot eat the deadline.
_SIGTERM_INSERT_BUSY_TIMEOUT_SECONDS = 0.5
# Longest wait of each step after that deadline (reading the row accounting,
# logging the WARNING), so a thread stalled holding either lock cannot keep
# the process alive until the parent's SIGKILL.
_SIGTERM_REPORT_WAIT_SECONDS = 0.1

>>>>>>> upstream/master

class EmbeddingStatsWriter(ABC):
    """Interface for recording embedding/reranker call stats.

    ``record()`` MUST NOT block the caller -- enqueue only. ``flush()``
    drains the buffer and batch-writes it; called both periodically
    (background task) and at shutdown.
    """

    _active: ClassVar[Optional["EmbeddingStatsWriter"]] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()

    @abstractmethod
    def record(self, call: EmbeddingCallRecord) -> None:
        """Enqueue a call record. MUST NOT block or raise."""

    @abstractmethod
    def flush(self) -> None:
        """Drain the buffer and batch-write it."""

    @classmethod
    def get_active(cls) -> "EmbeddingStatsWriter":
        """Return the process-wide active writer, defaulting to NoOpWriter.

        Story #1418 Phase 3: honors the ``embedding_stats_config.enabled``
        kill-switch -- when disabled, resolves to NoOpWriter regardless of
        what was previously installed via set_active() (mirrors the
        memory_retrieval_enabled kill-switch pattern). The installed writer
        itself is left untouched (_active is not mutated) so re-enabling
        takes effect immediately on the next call, without a fresh
        set_active().
        """
        if not cls._is_enabled():
            return NoOpWriter()
        if cls._active is None:
            with cls._lock:
                if cls._active is None:
                    cls._active = NoOpWriter()
        return cls._active

    @classmethod
    def _is_enabled(cls) -> bool:
        """Kill-switch check: PEEKS at an already-constructed ConfigService
        singleton (never lazily constructs one via get_config_service() --
        that has a side effect of creating a phantom config.json at
        whatever directory this process's default resolves to, which is
        unacceptable to trigger from this hot-path read). Fail-open (True)
        when no singleton exists yet (e.g. inside a `cidx index` child
        subprocess that never calls get_config_service() itself) or when
        any read fails.
        """
        try:
            from code_indexer.server.services import config_service as _cs_module

            svc = _cs_module._config_service
            if svc is None:
                return True
            stats_cfg = svc.get_config().embedding_stats_config
            if stats_cfg is None:
                return True
            return bool(stats_cfg.enabled)
        except Exception as exc:
            logger.debug(
                "EmbeddingStatsWriter._is_enabled: config peek failed, "
                "failing open (enabled=True): %s",
                exc,
            )
            return True

    @classmethod
    def set_active(cls, writer: "EmbeddingStatsWriter") -> None:
        """Install a new process-wide active writer (Phase 2/3 wiring)."""
        with cls._lock:
            cls._active = writer


class NoOpWriter(EmbeddingStatsWriter):
    """Standalone CLI / no-server-orchestration default: records nothing."""

    def record(self, call: EmbeddingCallRecord) -> None:
        pass

    def flush(self) -> None:
        pass


class InProcessAsyncWriter(EmbeddingStatsWriter):
    """Batched background writer living inside the long-lived server process.

    Same batching shape as SearchEmbedEventWriter / ApiMetricsService:
    ``record()`` enqueues (non-blocking); a background thread periodically
    drains the queue and batch-writes via ``backend.insert_batch()`` -- ONE
    transaction per flush cycle, never one insert per record (Bug #1181
    pattern). A flush failure is caught/logged and never crashes the
    background thread or propagates to the caller (fail-open, per this
    project's "ALL cache ops fail-open" convention for observability data).
    """

    def __init__(
        self,
        backend: Any,
        flush_interval_seconds: float = _DEFAULT_FLUSH_INTERVAL_SECONDS,
        maxsize: int = 10_000,
        flush_interval_provider: Optional[Callable[[], float]] = None,
    ) -> None:
        self._backend = backend
        self._flush_interval_seconds = flush_interval_seconds
        self._queue: "Queue[EmbeddingCallRecord]" = Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._overflow_warned = False
        # Story #1418 Phase 3: optional live re-read of the flush interval,
        # used ONLY by the in-process live-server writer (wired via
        # embedding_stats_lifespan_wiring.py). CrossProcessBootstrapWriter
        # never receives one -- the bootstrap installer resolves the value
        # ONCE at child-process startup and passes only the static
        # flush_interval_seconds.
        self._flush_interval_provider = flush_interval_provider
<<<<<<< HEAD
=======
        # Set (before _stop) by a deadline-bounded shutdown (the SIGTERM
        # flush): the writer thread then ends its periodic flush and does its
        # final drain under that deadline (_drain_before).
        self._shutdown_deadline: Optional[float] = None
        # Rows taken off the queue whose insert has not finished, and rows
        # whose insert failed. Updated together with the take under
        # _accounting, so a reader holding it sees every row exactly once
        # (queued, in flight, or failed).
        self._accounting = threading.Lock()
        self._in_flight_rows = 0
        self._unwritten_rows = 0
>>>>>>> upstream/master

    def _resolve_flush_interval(self) -> float:
        """Resolve the current flush interval: live via the provider when
        present, else the static value. Fail-open: a raising provider falls
        back to the static value (never crashes the background loop)."""
        if self._flush_interval_provider is None:
            return self._flush_interval_seconds
        try:
            return self._flush_interval_provider()
        except Exception as exc:
            logger.debug(
                "InProcessAsyncWriter: flush_interval_provider failed, "
                "falling back to static flush_interval_seconds=%s: %s",
                self._flush_interval_seconds,
                exc,
            )
            return self._flush_interval_seconds

    def record(self, call: EmbeddingCallRecord) -> None:
        """Hot path -- NEVER blocks, NEVER raises (enqueue can't fail)."""
        try:
            self._queue.put_nowait(call)
            self._overflow_warned = False
        except Full:
            if not self._overflow_warned:
                logger.warning(
                    "embedding_call_stats queue full -- dropping newest records"
                )
                self._overflow_warned = True

<<<<<<< HEAD
    def _drain(self) -> None:
        """Drain at most _MAX_DRAIN_BATCH records and write them in ONE batch."""
=======
    def _take_batch(self) -> List[EmbeddingCallRecord]:
        """Take at most _MAX_DRAIN_BATCH queued records (never blocks)."""
>>>>>>> upstream/master
        batch: List[EmbeddingCallRecord] = []
        for _ in range(_MAX_DRAIN_BATCH):
            try:
                batch.append(self._queue.get_nowait())
            except Empty:
                break
<<<<<<< HEAD
        if not batch:
            return
        try:
            self._backend.insert_batch(batch)
        except Exception as exc:
            logger.warning("InProcessAsyncWriter: flush failed: %s", exc)

    def flush(self) -> None:
        """Synchronously drain the ENTIRE queue now (periodic + shutdown call)."""
        while not self._queue.empty():
            self._drain()

=======
        return batch

    def _drain(self, busy_timeout_seconds: Optional[float] = None) -> None:
        """Drain at most _MAX_DRAIN_BATCH records and write them in ONE batch;
        ``busy_timeout_seconds`` (shutdown drain only) bounds the insert's
        lock wait."""
        with self._accounting:
            batch = self._take_batch()
            self._in_flight_rows = len(batch)
        if not batch:
            return
        written = False
        try:
            if busy_timeout_seconds is None:
                self._backend.insert_batch(batch)
            else:
                self._backend.insert_batch(
                    batch, busy_timeout_seconds=busy_timeout_seconds
                )
            written = True
        except Exception as exc:
            logger.warning("InProcessAsyncWriter: flush failed: %s", exc)
        finally:
            with self._accounting:
                self._in_flight_rows = 0
                if not written:
                    self._unwritten_rows += len(batch)

    def flush(self) -> None:
        """Synchronously drain the ENTIRE queue now (periodic + shutdown call).
        Stops early once a deadline-bounded shutdown is requested; the loop's
        _drain_before then takes over."""
        while self._shutdown_deadline is None and not self._queue.empty():
            self._drain()

    def _drain_before(self, deadline: float) -> None:
        """Write queued rows batch by batch until the queue is empty or the
        deadline passes (time-bounded); each insert waits for a lock at most
        _SIGTERM_INSERT_BUSY_TIMEOUT_SECONDS."""
        while not self._queue.empty():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._drain(min(_SIGTERM_INSERT_BUSY_TIMEOUT_SECONDS, remaining))

>>>>>>> upstream/master
    def _loop(self) -> None:
        """Background loop: drain every flush_interval_seconds, stop on
        signal. Re-resolves the interval on EACH cycle (via
        _resolve_flush_interval()) so a live flush_interval_provider's
        value change takes effect on the very next wait -- without one, this
        is byte-identical to the pre-Phase-3 static-value behavior.

        Each periodic tick calls flush() (a FULL drain, internally looping
        _drain() until the queue is empty) rather than a single
        _MAX_DRAIN_BATCH(1000)-capped _drain() call. A single-_drain()-per-
        cycle tick artificially ceilings sustained persist throughput at
        _MAX_DRAIN_BATCH / flush_interval_seconds (~33 records/s at the
        30s default) -- above that rate the bounded Queue backs up and
        eventually saturates, silently dropping the newest records during
        the highest-volume, highest-cost period this story exists to
        measure for vendor cost reconciliation. flush() preserves the
        one-transaction-per-batch-of-<=1000 convention (multiple
        insert_batch() calls when the queue exceeds the cap) while removing
        the artificial per-cycle ceiling."""
        while not self._stop.wait(timeout=self._resolve_flush_interval()):
            self.flush()
<<<<<<< HEAD
        # Final drain on shutdown so no queued records are lost.
        self.flush()
=======
        # Final drain on shutdown so no queued records are lost -- under the
        # shutdown deadline when one was set (after finishing the in-flight
        # insert above, if any).
        deadline = self._shutdown_deadline
        if deadline is None:
            self.flush()
        else:
            self._drain_before(deadline)
>>>>>>> upstream/master

    def start(self) -> None:
        """Start the background flush thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="embedding-stats-writer"
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the background thread to stop and wait for it to drain."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "embedding-stats-writer thread did not stop within %.1fs timeout",
                    timeout,
                )


class CrossProcessBootstrapWriter(InProcessAsyncWriter):
    """Writer living inside a `cidx index` child subprocess -- Story #1418.

    Installed via CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR
    (embedding_stats_child_wiring.py's install_embedding_stats_writer_from_bootstrap)
    when a `cidx index` invocation is spawned by the server (golden-repo
    registration/refresh, branch-change reindex, activated-repo indexing,
    provider-index background jobs, meta-repo catch-up reindex). Reuses
    InProcessAsyncWriter's buffer/background-flush machinery UNCHANGED (same
    Queue, same daemon thread, same periodic-drain loop) -- there is no
    behavioral difference in how records are batched or flushed. This class
    exists as a distinct name so the SOURCE (a short-lived child subprocess,
    not the server's own long-lived process) is self-documenting at call
    sites and in logs, and so this subprocess-specific class can diverge
    independently in the future without touching InProcessAsyncWriter. It
    differs from InProcessAsyncWriter only in HOW its backend connection is
    resolved (see install_embedding_stats_writer_from_bootstrap in
    embedding_stats_child_wiring.py) -- an external, construction-time
    concern, not a difference in the flush mechanism itself.

    On process exit (normal completion), the CLI entrypoint calls stop() in
    a finally block, which performs a final best-effort flush() of any
<<<<<<< HEAD
    buffered records. A SIGKILL/OOM before that point loses only the
    unflushed tail -- an accepted fail-open tradeoff consistent with this
    project's other observability data (never blocks or fails real
    indexing/query work over a stats-recording failure).
    """
=======
    buffered records. On SIGTERM (a cancelled job: the parent's
    terminate_process_group) install_sigterm_flush()'s handler flushes
    before the process dies. A SIGKILL/OOM loses only the unflushed tail --
    an accepted fail-open tradeoff consistent with this project's other
    observability data (never blocks or fails real indexing/query work over
    a stats-recording failure).
    """

    def install_sigterm_flush(self) -> bool:
        """Make SIGTERM flush this writer, then end the process BY SIGTERM.

        The default SIGTERM disposition ends the interpreter without atexit,
        so a cancelled child lost every buffered stats row. The handler does
        not raise SystemExit: interpreter exit first runs every work item
        still queued on the indexing ThreadPoolExecutors (more provider
        spend after a cancel) and so outlasts the parent's grace period; and
        the parent routes ``Exit code -15`` as an interruption, so the exit
        status must stay -15. Must be called on the main thread (signal
        handlers can only be installed there); returns False otherwise.

        The handler runs on the main thread between two bytecodes, possibly
        while that thread holds a lock (the queue's mutex inside record(),
        a logging lock...), so it takes NO lock: it only wakes a flusher
        thread through a pipe and returns, letting the main thread release
        whatever it holds. The flusher sets one deadline and stops the
        (started) writer thread, which finishes any in-flight insert and
        drains the rest under that deadline; the flusher waits for it only
        until the deadline, reports the rows still queued, in flight or
        failed without blocking, then ALWAYS signals the main thread
        (pthread_kill, so a blocking call there is interrupted), whose
        handler restores SIG_DFL and re-sends SIGTERM (signal.signal is
        main-thread only). A main thread stuck inside a C call never runs
        that handler; the parent's SIGKILL then ends the process."""
        if threading.current_thread() is not threading.main_thread():
            logger.warning(
                "CrossProcessBootstrapWriter: not on the main thread -- "
                "SIGTERM will not flush embedding stats"
            )
            return False
        self._sigterm_owner_pid = os.getpid()
        self._sigterm_main_ident = threading.get_ident()
        read_fd, self._sigterm_wake_fd = os.pipe()
        os.set_blocking(self._sigterm_wake_fd, False)
        self._sigterm_requested = False
        self._sigterm_drained = False
        threading.Thread(
            target=self._flush_on_sigterm,
            args=(read_fd,),
            daemon=True,
            name="embedding-stats-sigterm-flush",
        ).start()
        signal.signal(signal.SIGTERM, self._on_sigterm)
        return True

    def _on_sigterm(self, signum: int, frame: Any) -> None:
        # Lock-free: attribute reads/writes, one non-blocking 1-byte write to
        # an otherwise empty pipe, and the final signal.signal/os.kill.
        # A forked descendant inherits this handler and the pipe but not the
        # flusher thread: it must die by default, never wake the installer.
        if self._sigterm_drained or os.getpid() != self._sigterm_owner_pid:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTERM)
        elif not self._sigterm_requested:
            # A second SIGTERM while the drain runs changes nothing.
            self._sigterm_requested = True
            os.write(self._sigterm_wake_fd, b"\x0f")

    def _flush_on_sigterm(self, read_fd: int) -> None:
        """Flusher thread: wait for the handler, have the writer thread
        finish its in-flight insert and drain the rest under ONE deadline
        that fits the parent's grace period, report what was not written,
        then ALWAYS have the main thread end the process by SIGTERM. Nothing
        after the deadline blocks: the join is timed and the report bounded."""
        os.read(read_fd, 1)
        try:
            failed_before = self._unwritten_rows
            deadline = time.monotonic() + _SIGTERM_DRAIN_BUDGET_SECONDS
            self._shutdown_deadline = deadline
            self._stop.set()
            writer_thread = self._thread
            if writer_thread is not None:
                writer_thread.join(timeout=max(0.0, deadline - time.monotonic()))
            self._report_unpersisted(failed_before)
        finally:
            self._sigterm_drained = True
            signal.pthread_kill(self._sigterm_main_ident, signal.SIGTERM)

    def _report_unpersisted(self, failed_before: int) -> None:
        """Log how many rows were not persisted: still queued, in flight in
        the writer's insert, or failed since the SIGTERM. Bounded: the
        accounting lock is waited for at most _SIGTERM_REPORT_WAIT_SECONDS
        (the count is "unknown" otherwise), the queue length is read without
        the queue's mutex, and the WARNING is logged on its own thread joined
        for at most _SIGTERM_REPORT_WAIT_SECONDS (a stalled logging lock
        cannot hold the exit)."""
        count = "an unknown number of"
        if self._accounting.acquire(timeout=_SIGTERM_REPORT_WAIT_SECONDS):
            try:
                unwritten = (
                    self._unwritten_rows
                    - failed_before
                    + self._in_flight_rows
                    + len(self._queue.queue)
                )
            finally:
                self._accounting.release()
            if not unwritten:
                return
            count = str(unwritten)
        reporter = threading.Thread(
            target=logger.warning,
            args=(
                "%s embedding-call stats rows not persisted before the "
                "SIGTERM exit (drain deadline %.1fs)",
                count,
                _SIGTERM_DRAIN_BUDGET_SECONDS,
            ),
            daemon=True,
            name="embedding-stats-sigterm-report",
        )
        reporter.start()
        reporter.join(timeout=_SIGTERM_REPORT_WAIT_SECONDS)
>>>>>>> upstream/master
