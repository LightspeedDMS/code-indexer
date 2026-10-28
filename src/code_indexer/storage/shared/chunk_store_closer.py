"""Background close coordination for ``ChunkStoreThreadCache`` (#2218, #2223).

One daemon thread closes cached chunk stores handed over by the cache, so a
slow or hung close (e.g. on NFS) never blocks a request thread or the reader
lease renewal thread. A close that fails is retried with exponential backoff,
forever: ``on_closed`` (which releases the store's reader lease) runs only
after a close succeeds, so a snapshot is never deleted under a descriptor
that may still be open.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import queue
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from code_indexer.storage.sqlite_chunk_store import ChunkStore

# ^ invariant queued closes <= this; a full queue => the caller keeps the store and retries later, never blocks
_CLOSE_QUEUE_MAX = 1024

_CLOSER_STOP_WAIT_SECONDS = 5.0

_RETRY_BASE_SECONDS = 1.0

_RETRY_MAX_SECONDS = 600.0

_ERROR_LOG_INTERVAL_SECONDS = 3600.0

# ^ why pending longer than this = stalled, not healthy; a hung NFS close never raises
_STALLED_CLOSE_SECONDS = 30.0

_STOP = object()
_WAKE = object()

logger = logging.getLogger(__name__)


class _Retry:
    def __init__(self, store: ChunkStore) -> None:
        self.store = store
        self.failures = 0
        self.last_error_logged: Optional[float] = None


class ChunkStoreCloser:
    """Closes stores off the calling thread; retries failed closes."""

    def __init__(self, on_closed: Callable[[ChunkStore], None]) -> None:
        self._on_closed = on_closed
        self._queue: "queue.Queue[object]" = queue.Queue(maxsize=_CLOSE_QUEUE_MAX)
        # ^ invariant heap size <= distinct stores claimed for closing <= cache registry size
        self._retries: List[Tuple[float, int, _Retry]] = []
        self._retry_seq = itertools.count()
        self._lock = threading.Lock()
        self._in_flight = 0
        # ^ invariant id(store) keys stay valid: a pending store is strongly held by the queue, heap or caller
        self._pending_since: Dict[int, float] = {}
        self._failed: Set[int] = set()
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    def submit(self, store: ChunkStore) -> bool:
        """Queue ``store`` for closing; False (nothing queued) when full."""
        self._ensure_thread()
        self._mark_pending(store)
        try:
            self._queue.put_nowait(store)
        except queue.Full:
            self._clear_pending(store)
            return False
        return True

    def close_now(self, store: ChunkStore) -> None:
        """Close on the calling thread; a failure is retried by the closer."""
        self._mark_pending(store)
        self._attempt(_Retry(store))

    def backlog(self) -> int:
        with self._lock:
            return self._queue.qsize() + len(self._retries) + self._in_flight

    def troubled_count(self) -> int:
        """Pending closes that failed at least once or are stalled."""
        stalled_before = time.monotonic() - _STALLED_CLOSE_SECONDS
        with self._lock:
            return sum(
                1
                for key, since in self._pending_since.items()
                if key in self._failed or since < stalled_before
            )

    def _mark_pending(self, store: ChunkStore) -> None:
        with self._lock:
            self._pending_since.setdefault(id(store), time.monotonic())

    def _clear_pending(self, store: ChunkStore) -> None:
        with self._lock:
            self._pending_since.pop(id(store), None)
            self._failed.discard(id(store))

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stopping.set()
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            pass  # the loop checks _stopping after its current item
        thread.join(_CLOSER_STOP_WAIT_SECONDS)
        if thread.is_alive():
            logger.warning(
                "Chunk-store closer still busy after %.0fs at shutdown; "
                "abandoning it (stores not yet closed keep their reader leases)",
                _CLOSER_STOP_WAIT_SECONDS,
            )

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping.clear()
            self._thread = threading.Thread(
                target=self._run, name="ChunkStoreIdleCloser", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=self._seconds_to_next_retry())
            except queue.Empty:
                item = None
            if self._stopping.is_set():
                return
            # ^ why ignore _STOP here, not exit; a sentinel left by an earlier stop must not end a restarted loop
            if isinstance(item, ChunkStore):
                self._attempt(_Retry(item))
            for retry in self._pop_due_retries():
                self._attempt(retry)

    def _seconds_to_next_retry(self) -> Optional[float]:
        with self._lock:
            if not self._retries:
                return None
            return max(0.0, self._retries[0][0] - time.monotonic())

    def _pop_due_retries(self) -> List[_Retry]:
        now = time.monotonic()
        due: List[_Retry] = []
        with self._lock:
            while self._retries and self._retries[0][0] <= now:
                due.append(heapq.heappop(self._retries)[2])
        return due

    def _attempt(self, retry: _Retry) -> None:
        with self._lock:
            self._in_flight += 1
        try:
            retry.store.close()
        except Exception as exc:
            with self._lock:
                self._failed.add(id(retry.store))
            self._schedule_retry(retry, exc)
            return
        finally:
            with self._lock:
                self._in_flight -= 1
        self._clear_pending(retry.store)
        try:
            self._on_closed(retry.store)
        except Exception:
            logger.exception("Releasing a closed chunk store's cache entry failed")

    def _schedule_retry(self, retry: _Retry, exc: Exception) -> None:
        retry.failures += 1
        self._log_failure(retry, exc)
        delay = min(
            _RETRY_BASE_SECONDS * 2 ** min(retry.failures - 1, 30), _RETRY_MAX_SECONDS
        )
        with self._lock:
            heapq.heappush(
                self._retries, (time.monotonic() + delay, next(self._retry_seq), retry)
            )
        if threading.current_thread() is not self._thread:
            self._ensure_thread()
            try:
                self._queue.put_nowait(_WAKE)
            except queue.Full:
                pass  # a busy loop re-reads the retry heap after its current item

    def _log_failure(self, retry: _Retry, exc: Exception) -> None:
        if retry.failures == 1:
            logger.warning(
                "Could not close a cached chunk store (%s); it keeps its reader "
                "lease and is retried with backoff",
                type(exc).__name__,
            )
            return
        now = time.monotonic()
        last = retry.last_error_logged
        if last is not None and now - last < _ERROR_LOG_INTERVAL_SECONDS:
            return
        retry.last_error_logged = now
        logger.error(
            "Still cannot close a cached chunk store after %d attempts (%s); it "
            "keeps its reader lease and retries continue",
            retry.failures,
            type(exc).__name__,
        )
