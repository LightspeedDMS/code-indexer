"""
ID Index Cache for Server-Side Performance Optimization.

Bug #1078: Cross-query id_index caching for FilesystemVectorStore.

py-spy profiling proved that per-query id_index deserialization
(rebuilding thousands of pathlib.Path objects in id_index_manager.load_index)
accounts for ~33% of all GIL-holding time, and it happens every query
because a fresh FilesystemVectorStore is created per query so its per-instance
self._id_index cache never persists.

The HNSW index IS cached cross-query via HNSWIndexCache; this module closes
that asymmetry for the id_index.

Mirrors HNSWIndexCache exactly:
- TTL-based eviction
- Access-based TTL refresh
- Per-collection cache isolation
- Thread-safe operations with per-key load deduplication
- Configuration from env / config file
- Cache statistics
"""

from code_indexer.server.middleware.correlation import get_correlation_id
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional
from code_indexer.global_repos.snapshot_reader_lease import SnapshotReaderLease

logger = logging.getLogger(__name__)


@dataclass
class IdIndexCacheConfig:
    """Configuration for id_index cache.

    Mirrors HNSWIndexCacheConfig. Reads from the same config.json keys
    used by the HNSW cache so operators have a single place to tune TTL.
    """

    ttl_minutes: float = 10.0
    cleanup_interval_seconds: int = 60
    max_entries: Optional[int] = 200  # id_index ~0.5 MB each; 200 entries = ~100 MB cap

    def __post_init__(self) -> None:
        if self.ttl_minutes <= 0:
            raise ValueError(f"TTL must be positive, got {self.ttl_minutes}")
        if self.cleanup_interval_seconds <= 0:
            raise ValueError(
                f"Cleanup interval must be positive, got {self.cleanup_interval_seconds}"
            )

    @classmethod
    def from_env(cls) -> "IdIndexCacheConfig":
        """Create config from environment variables.

        Supported env vars (reuse HNSW vars so config is not duplicated):
        - CIDX_INDEX_CACHE_TTL_MINUTES  (default 10)
        - CIDX_INDEX_CACHE_CLEANUP_INTERVAL  (default 60)
        - CIDX_ID_INDEX_CACHE_MAX_ENTRIES  (default 200)
        """
        ttl_minutes = float(os.environ.get("CIDX_INDEX_CACHE_TTL_MINUTES", "10"))
        cleanup_interval = int(
            os.environ.get("CIDX_INDEX_CACHE_CLEANUP_INTERVAL", "60")
        )
        max_entries_str = os.environ.get("CIDX_ID_INDEX_CACHE_MAX_ENTRIES")
        max_entries = int(max_entries_str) if max_entries_str else 200
        return cls(
            ttl_minutes=ttl_minutes,
            cleanup_interval_seconds=cleanup_interval,
            max_entries=max_entries,
        )

    @classmethod
    def from_file(cls, config_file_path: str) -> "IdIndexCacheConfig":
        """Create config from JSON configuration file.

        Reads the same keys as HNSWIndexCacheConfig so operators only need
        one TTL setting in config.json.

        Expected format:
        {
            "index_cache_ttl_minutes": 15,
            "index_cache_cleanup_interval_seconds": 90,
            "id_index_cache_max_entries": 200
        }
        """
        config_path = Path(config_file_path)
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_file_path}")
        with open(config_path) as f:
            data = json.load(f)
        return cls(
            ttl_minutes=data.get("index_cache_ttl_minutes", 10.0),
            cleanup_interval_seconds=data.get(
                "index_cache_cleanup_interval_seconds", 60
            ),
            max_entries=data.get("id_index_cache_max_entries", 200),
        )


@dataclass
class _IdIndexCacheEntry:
    """Single cached id_index entry."""

    id_index: Any  # Dict[str, Path] (the loaded id_index object)
    collection_path: str
    ttl_minutes: float

    created_at: datetime = field(default_factory=datetime.now)
    last_accessed: datetime = field(default_factory=datetime.now)
    access_count: int = 0

    def record_access(self) -> None:
        """Refresh TTL and increment counter."""
        self.last_accessed = datetime.now()
        self.access_count += 1

    def is_expired(self) -> bool:
        """Return True if the entry has exceeded its TTL."""
        expiration = self.last_accessed + timedelta(minutes=self.ttl_minutes)
        return datetime.now() > expiration


class IdIndexCache:
    """Thread-safe cross-query cache for id_index objects.

    Mirrors HNSWIndexCache in structure and locking discipline.

    get_or_load() uses the same per-key Event sentinel pattern as HNSWIndexCache:
    - The global _cache_lock is held ONLY for dict operations (microseconds).
    - Disk I/O (loader()) runs with NO lock held.
    - Same-key concurrent callers wait on a per-key Event; different-key
      callers proceed in parallel.
    """

    def __init__(
        self,
        config: Optional[IdIndexCacheConfig] = None,
        *,
        lease_root: Optional[Path] = None,
        is_versioned_snapshot: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.config = config or IdIndexCacheConfig()
        self._lease_root = lease_root
        self._is_versioned_snapshot = is_versioned_snapshot

        self._cache: Dict[str, _IdIndexCacheEntry] = {}
        self._cache_lock = Lock()

        # Per-key load-in-progress sentinels (mirrors HNSW pattern)
        self._loading: Dict[str, threading.Event] = {}

        self._hit_count = 0
        self._miss_count = 0
        self._eviction_count = 0
        self._reader_leases: Dict[str, SnapshotReaderLease] = {}

        # Bug #1849: leases queued for release/renewal while holding
        # _cache_lock. Purely in-memory -- appending here does NO
        # filesystem I/O and spawns NO thread. Drained serially, off
        # lock, by the already-running background cleanup thread (see
        # _cleanup_expired_entries / _process_pending_lease_work), and
        # synchronously on stop_background_cleanup so no lease outlives
        # the process.
        self._pending_lease_releases: List[SnapshotReaderLease] = []
        self._pending_lease_renewals: List[SnapshotReaderLease] = []

        self._cleanup_thread: Optional[threading.Thread] = None
        self._cleanup_stop_event = threading.Event()

        logger.info(
            "IdIndexCache initialized with TTL=%s minutes",
            self.config.ttl_minutes,
            extra={"correlation_id": get_correlation_id()},
        )

    def get_or_load(
        self,
        collection_path: str,
        loader: Callable[[], Any],
    ) -> Any:
        """Return the cached id_index for collection_path, loading if absent.

        Thread-safe. Concurrent calls for the same key deduplicate: only one
        thread calls loader(); others wait and receive the same object.
        Concurrent calls for different keys run in parallel.

        Args:
            collection_path: Resolved collection path (cache key).
            loader: Zero-arg callable that returns the id_index dict.

        Returns:
            Loaded (or cached) id_index dict.
        """
        collection_path = str(Path(collection_path).resolve())

        while True:
            with self._cache_lock:
                if collection_path in self._cache:
                    entry = self._cache[collection_path]
                    if entry.is_expired():
                        del self._cache[collection_path]
                        self._release_reader_lease_locked(collection_path)
                        self._eviction_count += 1
                        logger.debug(
                            "IdIndexCache entry expired for %s, reloading",
                            collection_path,
                            extra={"correlation_id": get_correlation_id()},
                        )
                        # fall through to load
                    else:
                        entry.record_access()
                        self._hit_count += 1
                        logger.debug(
                            "IdIndexCache HIT for %s (access_count=%d)",
                            collection_path,
                            entry.access_count,
                            extra={"correlation_id": get_correlation_id()},
                        )
                        return entry.id_index

                # No ready entry — check if another thread is loading this key.
                if collection_path in self._loading:
                    event = self._loading[collection_path]
                    self._miss_count += 1
                    # Release lock BEFORE waiting
                else:
                    event = threading.Event()
                    self._loading[collection_path] = event
                    self._miss_count += 1
                    break  # We are the loader thread

            # Waiter path: block until the loader signals completion/failure.
            logger.debug(
                "IdIndexCache WAIT for %s, another thread is loading",
                collection_path,
                extra={"correlation_id": get_correlation_id()},
            )
            event.wait()
            continue  # Loop back to re-check cache

        # --- NO LOCK HELD during I/O ---
        logger.debug(
            "IdIndexCache MISS for %s, loading id_index",
            collection_path,
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            id_index = loader()
            reader_lease = None
            if self._is_versioned_snapshot is not None and self._is_versioned_snapshot(
                collection_path
            ):
                if self._lease_root is None:
                    logger.error(
                        "Snapshot %s is versioned but no lease_root is wired "
                        "into this IdIndexCache instance; skipping reader-lease "
                        "publication",
                        collection_path,
                    )
                else:
                    try:
                        reader_lease = SnapshotReaderLease(
                            collection_path,
                            self.config.ttl_minutes * 60.0,
                            lease_root=self._lease_root,
                        )
                        reader_lease.acquire()
                    except OSError as exc:
                        logger.warning(
                            "Could not publish snapshot reader lease for %s: %s",
                            collection_path,
                            exc,
                        )

            with self._cache_lock:
                entry = _IdIndexCacheEntry(
                    id_index=id_index,
                    collection_path=collection_path,
                    ttl_minutes=self.config.ttl_minutes,
                )
                entry.record_access()
                self._cache[collection_path] = entry
                if reader_lease is not None:
                    self._reader_leases[collection_path] = reader_lease
                self._enforce_entry_limit()

            logger.info(
                "IdIndexCache: cached id_index for %s",
                collection_path,
                extra={"correlation_id": get_correlation_id()},
            )
            return id_index

        finally:
            # Always clean up sentinel and wake waiters, even on exception.
            # event.set() MUST be called OUTSIDE the lock.
            with self._cache_lock:
                self._loading.pop(collection_path, None)
            event.set()

    def invalidate(self, collection_path: str) -> None:
        """Remove a single entry by exact key.

        Args:
            collection_path: Collection path (will be resolved).
        """
        collection_path = str(Path(collection_path).resolve())
        with self._cache_lock:
            if collection_path in self._cache:
                del self._cache[collection_path]
                self._release_reader_lease_locked(collection_path)
                self._eviction_count += 1
                logger.info(
                    "IdIndexCache: invalidated %s",
                    collection_path,
                    extra={"correlation_id": get_correlation_id()},
                )

    def invalidate_prefix(self, path_prefix: str) -> int:
        """Evict all entries whose key equals path_prefix or is under path_prefix/.

        Mirrors HNSWIndexCache.invalidate_prefix — used by RefreshScheduler
        after swap_alias() to evict stale snapshot entries immediately.

        Args:
            path_prefix: Snapshot directory path. Must be non-empty.

        Returns:
            Number of entries evicted.

        Raises:
            ValueError: If path_prefix is None or empty.
        """
        if not path_prefix:
            raise ValueError("path_prefix must be a non-empty string")

        path_prefix = str(Path(path_prefix).resolve())
        prefix_with_sep = path_prefix + "/"

        with self._cache_lock:
            stale = [
                key
                for key in self._cache
                if key == path_prefix or key.startswith(prefix_with_sep)
            ]
            for key in stale:
                del self._cache[key]
                self._release_reader_lease_locked(key)
                self._eviction_count += 1

        evicted = len(stale)
        logger.info(
            "IdIndexCache: evicted %d stale entries for prefix %s",
            evicted,
            path_prefix,
            extra={"correlation_id": get_correlation_id()},
        )
        return evicted

    def clear(self) -> None:
        """Remove all entries."""
        with self._cache_lock:
            evicted = len(self._cache)
            for collection_path in tuple(self._cache):
                self._release_reader_lease_locked(collection_path)
            self._cache.clear()
            self._eviction_count += evicted
            logger.info(
                "IdIndexCache: cleared %d entries",
                evicted,
                extra={"correlation_id": get_correlation_id()},
            )

    def _enforce_entry_limit(self) -> None:
        """Evict LRU entries until under max_entries cap.

        Must be called while holding _cache_lock.
        """
        if self.config.max_entries is None:
            return
        while len(self._cache) > self.config.max_entries:
            lru_key = min(
                self._cache.keys(),
                key=lambda k: self._cache[k].last_accessed,
            )
            del self._cache[lru_key]
            self._release_reader_lease_locked(lru_key)
            self._eviction_count += 1
            logger.debug(
                "IdIndexCache: evicted LRU entry to enforce entry limit: %s",
                lru_key,
                extra={"correlation_id": get_correlation_id()},
            )

    def _cleanup_expired_entries(self) -> None:
        """Evict all entries that have exceeded their TTL.

        Bug #1849: lease release/renewal is queued (in-memory only) while
        holding _cache_lock, then the lock is dropped and every queued
        lease is released/renewed serially on this thread -- the
        already-running background cleanup thread -- instead of
        spawning a new thread per lease. This does not make one queued
        lease independent of another: a genuinely hung lease.release()/
        renew() (e.g. a stuck hard-NFS os.replace) still blocks the
        remaining queued leases behind it on THIS pass, same as the
        canonical chunk_store_cache.py pattern this mirrors. What the
        per-lease try/except (see _release_reader_lease /
        _renew_reader_lease) actually guarantees is that a lease whose
        call *raises* (rather than hangs) does not stop the rest of the
        queue from being processed.
        """
        with self._cache_lock:
            expired = [k for k, v in self._cache.items() if v.is_expired()]
            for k in expired:
                del self._cache[k]
                self._release_reader_lease_locked(k)
                self._eviction_count += 1

            for collection_path, lease in self._reader_leases.items():
                if collection_path in self._cache:
                    self._renew_reader_lease_locked(lease)

            # Snapshot + clear the queued work while still holding the
            # lock, then release the lock before any filesystem I/O.
            pending_releases = self._pending_lease_releases
            self._pending_lease_releases = []
            pending_renewals = self._pending_lease_renewals
            self._pending_lease_renewals = []

        if expired:
            logger.info(
                "IdIndexCache: evicted %d expired entries",
                len(expired),
                extra={"correlation_id": get_correlation_id()},
            )

        # --- NO LOCK HELD: serial I/O on the calling (cleanup) thread ---
        self._process_pending_lease_work(pending_releases, pending_renewals)

    def _release_reader_lease_locked(self, collection_path: str) -> None:
        """Queue a reader lease for release. Must be called while holding
        _cache_lock. Bug #1849: NO filesystem I/O and NO thread spawn
        here -- the lease is released serially, off-lock, the next time
        _process_pending_lease_work runs (on the background cleanup
        thread, or synchronously on stop_background_cleanup).
        """
        lease = self._reader_leases.pop(collection_path, None)
        if lease is not None:
            self._pending_lease_releases.append(lease)

    @staticmethod
    def _release_reader_lease(lease: SnapshotReaderLease) -> None:
        try:
            lease.release()
        except OSError:
            logger.debug("Could not release snapshot reader lease", exc_info=True)

    def _renew_reader_lease_locked(self, lease: SnapshotReaderLease) -> None:
        """Queue a reader lease for renewal. Must be called while holding
        _cache_lock. Bug #1849: NO filesystem I/O and NO thread spawn --
        see _release_reader_lease_locked.
        """
        self._pending_lease_renewals.append(lease)

    def _process_pending_lease_work(
        self,
        pending_releases: List[SnapshotReaderLease],
        pending_renewals: List[SnapshotReaderLease],
    ) -> None:
        """Release and renew queued leases serially, with NO cache lock
        held. Bug #1849: replaces the old one-OS-thread-per-lease
        mechanism. Always runs on the caller's own thread (the
        background cleanup thread during normal operation, or the
        calling thread synchronously during stop_background_cleanup).
        """
        for lease in pending_releases:
            self._release_reader_lease(lease)
        for lease in pending_renewals:
            self._renew_reader_lease(lease)

    @staticmethod
    def _renew_reader_lease(lease: SnapshotReaderLease) -> None:
        try:
            lease.renew()
        except OSError:
            logger.debug("Could not renew snapshot reader lease", exc_info=True)

    def start_background_cleanup(self) -> None:
        """Start daemon thread that periodically evicts expired entries."""
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            return
        self._cleanup_stop_event.clear()

        def loop() -> None:
            while not self._cleanup_stop_event.is_set():
                try:
                    self._cleanup_expired_entries()
                except Exception as exc:
                    logger.error(
                        "IdIndexCache: error in background cleanup: %s",
                        exc,
                        extra={"correlation_id": get_correlation_id()},
                    )
                self._cleanup_stop_event.wait(
                    timeout=self.config.cleanup_interval_seconds
                )

        self._cleanup_thread = threading.Thread(
            target=loop, name="IdIndexCacheCleanup", daemon=True
        )
        self._cleanup_thread.start()
        logger.info(
            "IdIndexCache: started background cleanup thread",
            extra={"correlation_id": get_correlation_id()},
        )

    def stop_background_cleanup(self) -> None:
        """Stop the background cleanup thread.

        Bug #1849: also drains any lease queued for release/renewal
        synchronously, since the cleanup thread that would normally
        drain it on its next tick is being stopped here -- a lease must
        never outlive the process. The drain only runs once the cleanup
        thread has actually stopped: if join() times out (the thread is
        genuinely stuck, e.g. blocked on a hard-NFS lease I/O call), that
        thread could still be mid-drain itself, so draining here too
        would race it. In that case the pending queues are left for the
        (still-running) thread to drain on its own -- this call simply
        does not block waiting for it, matching the original bounded
        join() semantics.
        """
        cleanup_thread_stopped = True
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            self._cleanup_stop_event.set()
            self._cleanup_thread.join(timeout=5)
            cleanup_thread_stopped = not self._cleanup_thread.is_alive()
            if cleanup_thread_stopped:
                logger.info(
                    "IdIndexCache: stopped background cleanup thread",
                    extra={"correlation_id": get_correlation_id()},
                )
            else:
                logger.warning(
                    "IdIndexCache: background cleanup thread did not stop "
                    "within timeout; skipping synchronous lease drain to "
                    "avoid racing with it",
                    extra={"correlation_id": get_correlation_id()},
                )

        if cleanup_thread_stopped:
            with self._cache_lock:
                pending_releases = self._pending_lease_releases
                self._pending_lease_releases = []
                pending_renewals = self._pending_lease_renewals
                self._pending_lease_renewals = []
            self._process_pending_lease_work(pending_releases, pending_renewals)


# ---------------------------------------------------------------------------
# Singleton accessor (mirrors get_global_cache / reset_global_cache)
# ---------------------------------------------------------------------------

_global_id_index_cache_instance: Optional[IdIndexCache] = None


def get_global_id_index_cache() -> IdIndexCache:
    """Get or create the server-wide IdIndexCache singleton.

    Configuration loaded from (in order):
    1. ~/.cidx-server/config.json
    2. Environment variables
    3. Defaults (10 minute TTL, 200 entry cap)
    """
    global _global_id_index_cache_instance

    if _global_id_index_cache_instance is None:
        config_file = Path.home() / ".cidx-server" / "config.json"
        if config_file.exists():
            try:
                config = IdIndexCacheConfig.from_file(str(config_file))
                logger.info(
                    "Loaded IdIndexCache config from %s: TTL=%s min",
                    config_file,
                    config.ttl_minutes,
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as exc:
                logger.warning(
                    "Failed to load IdIndexCache config from %s: %s. Using defaults.",
                    config_file,
                    exc,
                    extra={"correlation_id": get_correlation_id()},
                )
                config = IdIndexCacheConfig.from_env()
        else:
            config = IdIndexCacheConfig.from_env()
            logger.info(
                "Initialized IdIndexCache with env/default config: TTL=%s min",
                config.ttl_minutes,
                extra={"correlation_id": get_correlation_id()},
            )

        from . import _resolve_id_index_lease_kwargs

        _global_id_index_cache_instance = IdIndexCache(
            config=config, **_resolve_id_index_lease_kwargs()
        )
        _global_id_index_cache_instance.start_background_cleanup()

    return _global_id_index_cache_instance


def reset_global_id_index_cache() -> None:
    """Reset the singleton (for testing)."""
    global _global_id_index_cache_instance
    if _global_id_index_cache_instance is not None:
        _global_id_index_cache_instance.stop_background_cleanup()
        _global_id_index_cache_instance = None
