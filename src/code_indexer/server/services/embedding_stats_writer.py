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
import threading
from abc import ABC, abstractmethod
from queue import Empty, Full, Queue
from typing import Any, Callable, ClassVar, List, Optional

from code_indexer.server.services.embedding_call_stats import EmbeddingCallRecord

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

    def _drain(self) -> None:
        """Drain at most _MAX_DRAIN_BATCH records and write them in ONE batch."""
        batch: List[EmbeddingCallRecord] = []
        for _ in range(_MAX_DRAIN_BATCH):
            try:
                batch.append(self._queue.get_nowait())
            except Empty:
                break
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
        # Final drain on shutdown so no queued records are lost.
        self.flush()

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
    buffered records. A SIGKILL/OOM before that point loses only the
    unflushed tail -- an accepted fail-open tradeoff consistent with this
    project's other observability data (never blocks or fails real
    indexing/query work over a stats-recording failure).
    """
