"""
API Metrics Service for Story #4 AC2 - Rolling Window Implementation.

Tracks API call timestamps using rolling window approach:
- Semantic Searches (search_code with semantic mode)
- Other Index Searches (FTS, temporal, hybrid searches)
- Regex Searches (regex_search calls)
- All Other API Calls (remaining API endpoints)

SQLite database storage allows multiple uvicorn workers to share metrics.
Timestamps older than 24 hours are automatically cleaned up.

Story #1083 — batched background writer:
    The hot path enqueues metric events; the background ``_writer_loop`` drains the
    currently-queued backlog (bounded by ``min(qsize(), _MAX_DRAIN_BATCH)``) and
    writes ALL of them via a SINGLE ``backend.upsert_buckets_batch()`` transaction
    per drain — collapsing the previous one-``BEGIN EXCLUSIVE``-per-event churn
    (~4N transactions) into ~1.  Counts are coalesced per bucket key and preserved
    exactly.  ``stop_writer()`` signals + joins the thread + final-drains so no
    queued counts are lost on shutdown (wired into lifespan).
"""

import logging
import queue
import socket
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, cast
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)

# Background writer queue capacity
_QUEUE_MAXSIZE = 10_000

# Bucket write cleanup interval (cleanup every N writes)
_BUCKET_CLEANUP_INTERVAL = 100

# Background writer queue poll timeout (seconds)
_QUEUE_POLL_TIMEOUT_S = 1.0

# Story #1083: max metric events coalesced into a single batched write/transaction.
# Caps per-drain loop iterations + in-memory event list + transaction size so the
# batched writer stays bounded under sustained producer pressure (MESSI #14).
_MAX_DRAIN_BATCH = 1_000


def _truncate_min1(dt: datetime) -> str:
    """Truncate datetime to 1-minute bucket boundary (zero seconds + microseconds)."""
    return dt.replace(second=0, microsecond=0).isoformat()


def _truncate_min5(dt: datetime) -> str:
    """Truncate datetime to 5-minute bucket boundary."""
    return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0).isoformat()


def _truncate_hour1(dt: datetime) -> str:
    """Truncate datetime to 1-hour bucket boundary (zero minutes, seconds, microseconds)."""
    return dt.replace(minute=0, second=0, microsecond=0).isoformat()


def _truncate_day1(dt: datetime) -> str:
    """Truncate datetime to 1-day bucket boundary (zero time component)."""
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


class ApiMetricsService:
    """Service for tracking API call metrics using rolling windows.

    SQLite database storage allows multiple uvicorn workers to share metrics.
    Timestamps are stored per API call category and filtered by window on read.

    Usage:
        service = ApiMetricsService()
        service.initialize("/path/to/metrics.db")
        service.increment_semantic_search()
    """

    def __init__(self):
        """Initialize the API metrics service (database not yet connected)."""
        self._backend: Optional[Any] = None
        self._node_id: Optional[str] = None
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._writer_thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()

    def initialize(
        self,
        db_path: str,
        storage_backend: Optional[Any] = None,
        node_id: Optional[str] = None,
    ) -> None:
        """Initialize the service with a storage backend.

        Args:
            db_path: Ignored (kept for call-site compatibility). The SQLite-direct
                api_metrics table path was removed in Story #1083 dead-code cleanup.
            storage_backend: Required storage backend (ApiMetricsSqliteBackend or
                ApiMetricsPostgresBackend). Raises ValueError when None.
            node_id: Optional cluster node identifier. Defaults to socket.gethostname()
                when not provided.

        Raises:
            ValueError: If storage_backend is None.

        Note:
            Can be called multiple times safely (idempotent).
        """
        self._node_id = node_id or socket.gethostname()
        self._backend = storage_backend

        if storage_backend is not None:
            logger.debug(
                f"ApiMetricsService using injected storage backend "
                f"(node_id={self._node_id!r})"
            )
            # Clear any stop flag left over from a previous stop_writer() so a
            # SECOND lifespan startup in the same process (e.g. in-process
            # FastAPI TestClient E2E) starts a writer that actually runs. Without
            # this, the re-init thread sees the stale stop flag and exits
            # immediately, silently dropping every enqueued metric (MESSI #13).
            self._stop_event.clear()
            # Only (re)start when no writer is already running, so an idempotent
            # re-init while the existing writer is still alive does not spawn a
            # duplicate thread.
            if self._writer_thread is None or not self._writer_thread.is_alive():
                self._writer_thread = threading.Thread(
                    target=self._writer_loop, daemon=True, name="api-metrics-writer"
                )
                self._writer_thread.start()
            return

        # storage_backend is required: the SQLite-direct fallback (api_metrics table)
        # was removed in Story #1083 dead-code cleanup.
        raise ValueError(
            "storage_backend must be provided; direct SQLite api_metrics path is removed"
        )

    @staticmethod
    def _bucket_map_for(timestamp: datetime) -> Dict[str, str]:
        """Precompute the 4-tier (granularity -> bucket_start) map for one event."""
        return {
            "min1": _truncate_min1(timestamp),
            "min5": _truncate_min5(timestamp),
            "hour1": _truncate_hour1(timestamp),
            "day1": _truncate_day1(timestamp),
        }

    def _drain_and_write(self, first_item: Tuple[str, str, datetime]) -> int:
        """Drain the currently-queued backlog (starting with first_item) and write
        it to the backend in ONE batched transaction (Story #1083).

        Returns the number of events written so the caller can drive periodic
        cleanup.

        PROVABLE BOUND (MESSI #14): the additional drain count is fixed up-front to
        ``min(qsize() snapshot, _MAX_DRAIN_BATCH)``.  Items producers enqueue AFTER
        the snapshot are NOT chased — they roll to the next drain cycle.  This caps
        both the loop iterations and the in-memory ``events`` list / transaction
        size regardless of sustained producer pressure.
        """
        events: List[Dict[str, Any]] = []
        metric_type, username, timestamp = first_item
        events.append(
            {
                "username": username,
                "metric_type": metric_type,
                "buckets": self._bucket_map_for(timestamp),
            }
        )
        # Snapshot the backlog ONCE; drain at most that many (capped) more items.
        backlog = min(self._queue.qsize(), _MAX_DRAIN_BATCH)
        for _ in range(backlog):
            try:
                metric_type, username, timestamp = self._queue.get_nowait()
            except queue.Empty:
                break
            events.append(
                {
                    "username": username,
                    "metric_type": metric_type,
                    "buckets": self._bucket_map_for(timestamp),
                }
            )

        if self._backend is None:
            return 0

        try:
            self._backend.upsert_buckets_batch(events, node_id=self._node_id)
        except Exception as e:
            logger.warning(
                format_error_log(
                    "APP-GENERAL-050",
                    f"Failed to batch-upsert {len(events)} metric event(s): {e}",
                )
            )
        return len(events)

    def _writer_loop(self) -> None:
        """Background thread: drain the queue and write buckets in BATCHED writes.

        Story #1083: instead of one BEGIN EXCLUSIVE transaction per metric event,
        the loop blocks for the next event then drains the currently-queued backlog
        (bounded) and writes it in ONE upsert_buckets_batch transaction.  This
        collapses ~4N per-event transactions into ~1 per drain under load.

        Runs until _stop_event is set; on shutdown the trailing queue is drained by
        stop_writer().  Polls with a bounded timeout so the loop terminates cleanly.
        """
        write_count = 0
        while not self._stop_event.is_set():
            try:
                first_item = self._queue.get(timeout=_QUEUE_POLL_TIMEOUT_S)
            except queue.Empty:
                continue

            write_count += self._drain_and_write(first_item)
            if write_count >= _BUCKET_CLEANUP_INTERVAL and self._backend is not None:
                try:
                    self._backend.cleanup_expired_buckets()
                except Exception as e:
                    logger.warning(
                        format_error_log(
                            "APP-GENERAL-051",
                            f"Failed to cleanup expired buckets: {e}",
                        )
                    )
                write_count = 0

    def stop_writer(self, join_timeout: float = 5.0) -> None:
        """Stop the background writer and drain any remaining queued events.

        Story #1083: signals the stop event, joins the writer thread, then performs
        a FINAL bounded drain so no queued metric counts are lost on shutdown.
        Idempotent and safe to call when no writer thread was ever started.

        Args:
            join_timeout: Max seconds to wait for the writer thread to exit.
        """
        self._stop_event.set()
        thread = self._writer_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)

        if self._backend is None:
            return

        # Final drain: flush whatever remained queued at shutdown. Bounded by the
        # queue's finite maxsize — after the writer thread has stopped there are no
        # more producers in the shutdown path, so each pass strictly shrinks the
        # queue and the loop terminates (MESSI #14).
        while True:
            try:
                first_item = self._queue.get_nowait()
            except queue.Empty:
                return
            self._drain_and_write(first_item)

    def _insert_metric(self, metric_type: str, username: str = "_anonymous") -> None:
        """Enqueue a metric event to the background writer (non-blocking hot path).

        Args:
            metric_type: Type of metric ('semantic', 'other_index', 'regex', 'other_api')
            username: Username for bucket attribution. Defaults to '_anonymous'.

        If the queue is full, the metric is dropped with a warning (never crashes).
        """
        now = datetime.now(timezone.utc)
        try:
            self._queue.put_nowait((metric_type, username, now))
        except queue.Full:
            logger.warning(
                format_error_log(
                    "APP-GENERAL-048",
                    f"API metrics queue full, dropping metric {metric_type} for {username}",
                )
            )

    def increment_semantic_search(self, username: str = "_anonymous") -> None:
        """Record a semantic search call timestamp."""
        self._insert_metric("semantic", username=username)

    def increment_other_index_search(self, username: str = "_anonymous") -> None:
        """Record an other index search call timestamp (FTS, temporal, hybrid)."""
        self._insert_metric("other_index", username=username)

    def increment_regex_search(self, username: str = "_anonymous") -> None:
        """Record a regex search call timestamp."""
        self._insert_metric("regex", username=username)

    def increment_other_api_call(self, username: str = "_anonymous") -> None:
        """Record an other API call timestamp."""
        self._insert_metric("other_api", username=username)

    def set_node_id(self, node_id: str) -> None:
        """Update the node_id used for metric tagging.

        Called after cluster config resolves the configured node identifier,
        which may differ from the default socket.gethostname().
        """
        self._node_id = node_id

    def get_metrics_bucketed(
        self,
        period_seconds: int,
        username: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """Return metric totals from api_metrics_buckets for the given period.

        Delegates to the backend when available; returns zero counts otherwise.
        cast() is used because _backend is Optional[Any] — importing the
        ApiMetricsBackend protocol here would create a circular dependency since
        storage-layer tests consume this service.

        Args:
            period_seconds: Duration in seconds. Must be in PERIOD_TO_TIER.
            username: When provided, filter to this user's rows only.
                      When None, aggregate across all users.
            node_id: When provided, filter to this cluster node's rows only.
                     When None, aggregate across all nodes.

        Returns:
            Dict with keys: semantic_searches, other_index_searches,
            regex_searches, other_api_calls.
        """
        if self._backend is not None:
            return cast(
                Dict[str, int],
                self._backend.get_metrics_bucketed(
                    period_seconds, username, node_id=node_id
                ),
            )
        return {
            "semantic_searches": 0,
            "other_index_searches": 0,
            "regex_searches": 0,
            "other_api_calls": 0,
        }

    def get_metrics_by_user(self, period_seconds: int) -> Dict[str, Dict[str, int]]:
        """Return per-user metric totals from api_metrics_buckets for the given period.

        Delegates to the backend when available; returns empty dict otherwise.
        cast() is used because _backend is Optional[Any] — see get_metrics_bucketed.

        Args:
            period_seconds: Duration in seconds. Must be in PERIOD_TO_TIER.

        Returns:
            Dict mapping username to {metric_type: count}.
        """
        if self._backend is not None:
            return cast(
                Dict[str, Dict[str, int]],
                self._backend.get_metrics_by_user(period_seconds),
            )
        return {}

    def get_metrics_timeseries(self, period_seconds: int) -> List[Tuple[str, str, int]]:
        """Return timeseries data from api_metrics_buckets for the given period.

        Delegates to the backend when available; returns empty list otherwise.
        cast() is used because _backend is Optional[Any] — see get_metrics_bucketed.

        Args:
            period_seconds: Duration in seconds. Must be in PERIOD_TO_TIER.

        Returns:
            List of (bucket_start, metric_type, count) tuples ordered by bucket_start ASC.
        """
        if self._backend is not None:
            return cast(
                List[Tuple[str, str, int]],
                self._backend.get_metrics_timeseries(period_seconds),
            )
        return []

    def reset(self) -> None:
        """Clear all bucket data from the database (used for testing / manual resets)."""
        if self._backend is not None:
            self._backend.reset()


# Global service instance
api_metrics_service = ApiMetricsService()
