"""
HNSW Index Cache for Server-Side Performance Optimization.

Story #526: Server-Side HNSW Index Caching for 1800x Query Performance

Provides in-memory caching of hnswlib.Index objects with:
- TTL-based eviction (AC2)
- Access-based TTL refresh (AC3)
- Per-repository cache isolation (AC4)
- Thread-safe operations (AC5)
- Configuration externalization (AC6)
- Cache statistics and monitoring (AC7)

Performance improvement: ~277ms → <1ms for repeated queries (1800x faster).
"""

from code_indexer.server.middleware.correlation import get_correlation_id
import ctypes
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, Optional, Set, Tuple
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)

# Bug #897 mitigation 1: glibc heap trim support.
# _LIBC_LOAD_LOCK guards first-load so two concurrent cleanup cycles cannot
# race on _LIBC_HANDLE/_LIBC_LOAD_ATTEMPTED.
_LIBC_LOAD_LOCK: threading.Lock = threading.Lock()
_LIBC_HANDLE: Optional[Any] = None
_LIBC_LOAD_ATTEMPTED: bool = False


def _feature_flag_enabled(flag_name: str) -> bool:
    """Return the boolean value of a bootstrap config flag from ServerConfigManager.

    Reads config.json directly (bootstrap path) so the flag is available on
    the cleanup daemon thread before the DB runtime config is loaded.
    Returns False on any read/parse error so the default is always safe/off.

    Args:
        flag_name: Attribute name on ServerConfig (e.g. "enable_malloc_trim").
    """
    try:
        from code_indexer.server.utils.config_manager import ServerConfigManager

        config = ServerConfigManager().load_config()
        if config is None:
            return False
        return bool(getattr(config, flag_name, False))
    except Exception as exc:
        logger.debug(
            "_feature_flag_enabled(%r) failed, defaulting to False: %s",
            flag_name,
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
        return False


def _maybe_malloc_trim() -> None:
    """Bug #897 mitigation 1: call glibc malloc_trim(0) to return contractible
    brk pages after a bulk HNSW eviction cycle.

    Linux-only. No-ops on non-glibc platforms (macOS dev, musl Alpine).
    The libc handle is lazy-loaded once under _LIBC_LOAD_LOCK and cached for
    the process lifetime so concurrent cleanup cycles do not race.

    malloc_trim(0) return value: 1 means pages were released to the OS,
    0 means the call succeeded but no pages were available to release.
    """
    global _LIBC_HANDLE, _LIBC_LOAD_ATTEMPTED
    if sys.platform != "linux":
        logger.debug(
            "malloc_trim skipped: not Linux (platform=%s)",
            sys.platform,
            extra={"correlation_id": get_correlation_id()},
        )
        return
    with _LIBC_LOAD_LOCK:
        if not _LIBC_LOAD_ATTEMPTED:
            _LIBC_LOAD_ATTEMPTED = True
            try:
                _LIBC_HANDLE = ctypes.CDLL("libc.so.6")
            except OSError as exc:
                # Non-glibc platform (musl Alpine, etc.); expected on some hosts.
                logger.debug(
                    "malloc_trim unavailable: could not load libc.so.6: %s",
                    exc,
                    extra={"correlation_id": get_correlation_id()},
                )
                _LIBC_HANDLE = None
    if _LIBC_HANDLE is None:
        return
    try:
        released = _LIBC_HANDLE.malloc_trim(0)
        if released:
            logger.debug(
                "malloc_trim(0) returned %d: pages released to OS",
                released,
                extra={"correlation_id": get_correlation_id()},
            )
        else:
            logger.debug(
                "malloc_trim(0) returned 0: call succeeded but no pages available to release",
                extra={"correlation_id": get_correlation_id()},
            )
    except AttributeError as exc:
        # musl libc lacks the malloc_trim symbol; expected on Alpine.
        logger.debug(
            "malloc_trim skipped: symbol not found in libc: %s",
            exc,
            extra={"correlation_id": get_correlation_id()},
        )
    except OSError as exc:
        logger.warning(
            "malloc_trim(0) raised OSError unexpectedly: %s",
            exc,
            extra={"correlation_id": get_correlation_id()},
        )


@dataclass
class HNSWIndexCacheConfig:
    """
    Configuration for HNSW index cache (AC6: Configuration Externalization).

    Supports configuration from:
    - Constructor arguments (programmatic)
    - Environment variables (CIDX_INDEX_CACHE_TTL_MINUTES)
    - Config file (~/.cidx-server/config.json)
    """

    ttl_minutes: float = 10.0
    cleanup_interval_seconds: int = 60
    max_cache_size_mb: Optional[int] = None  # No limit by default

    def __post_init__(self):
        """Validate configuration values."""
        if self.ttl_minutes <= 0:
            raise ValueError(f"TTL must be positive, got {self.ttl_minutes}")

        if self.cleanup_interval_seconds <= 0:
            raise ValueError(
                f"Cleanup interval must be positive, got {self.cleanup_interval_seconds}"
            )

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "HNSWIndexCacheConfig":
        """
        Create config from dictionary.

        Args:
            config_dict: Configuration dictionary

        Returns:
            HNSWIndexCacheConfig instance
        """
        return cls(
            ttl_minutes=config_dict.get("ttl_minutes", 10.0),
            cleanup_interval_seconds=config_dict.get("cleanup_interval_seconds", 60),
            max_cache_size_mb=config_dict.get("max_cache_size_mb"),
        )

    @classmethod
    def from_env(cls) -> "HNSWIndexCacheConfig":
        """
        Create config from environment variables.

        Supported environment variables:
        - CIDX_INDEX_CACHE_TTL_MINUTES: TTL in minutes (default: 10)
        - CIDX_INDEX_CACHE_CLEANUP_INTERVAL: Cleanup interval in seconds (default: 60)
        - CIDX_INDEX_CACHE_MAX_SIZE_MB: Maximum cache size in MB (default: None)

        Returns:
            HNSWIndexCacheConfig instance
        """
        ttl_minutes = float(os.environ.get("CIDX_INDEX_CACHE_TTL_MINUTES", "10"))
        cleanup_interval = int(
            os.environ.get("CIDX_INDEX_CACHE_CLEANUP_INTERVAL", "60")
        )
        max_size_mb_str = os.environ.get("CIDX_INDEX_CACHE_MAX_SIZE_MB")
        max_size_mb = int(max_size_mb_str) if max_size_mb_str else None

        return cls(
            ttl_minutes=ttl_minutes,
            cleanup_interval_seconds=cleanup_interval,
            max_cache_size_mb=max_size_mb,
        )

    @classmethod
    def from_file(cls, config_file_path: str) -> "HNSWIndexCacheConfig":
        """
        Create config from JSON configuration file.

        Expected format in config.json:
        {
            "index_cache_ttl_minutes": 15,
            "index_cache_cleanup_interval_seconds": 90,
            "index_cache_max_size_mb": 1024
        }

        Args:
            config_file_path: Path to config.json

        Returns:
            HNSWIndexCacheConfig instance

        Raises:
            FileNotFoundError: If config file doesn't exist
            json.JSONDecodeError: If config file is invalid JSON
        """
        config_path = Path(config_file_path)

        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_file_path}")

        with open(config_path) as f:
            config_data = json.load(f)

        return cls(
            ttl_minutes=config_data.get("index_cache_ttl_minutes", 10.0),
            cleanup_interval_seconds=config_data.get(
                "index_cache_cleanup_interval_seconds", 60
            ),
            max_cache_size_mb=config_data.get("index_cache_max_size_mb"),
        )


#: Bug #1538: the on-disk IDENTITY of an ``hnsw_index.bin`` --
#: ``(st_mtime_ns, st_size, st_ino, st_dev)``.
#:
#: ``st_mtime_ns`` uses nanosecond precision, never the lossy float
#: ``st_mtime``, matching the ``st_mtime_ns`` convention this codebase's other
#: path-keyed caches already use (``CollectionMetaCache``,
#: ``ChunkStoreThreadCache``).
#:
#: Time and size ALONE are not an identity, and relying on them would leave
#: the exact staleness this bug reports reachable through a narrower window: a
#: refresh may legitimately rebuild a shard to the same item count (same file
#: size) with different content, and the timestamps can compare equal too (a
#: coarse server-side mtime granularity, a same-tick rewrite). ``st_ino``
#: (qualified by ``st_dev``, since inode numbers are only unique within a
#: filesystem) closes that: EVERY ``hnsw_index.bin`` publish in this codebase
#: is an atomic rename over the live path
#: (``BackgroundIndexRebuilder.atomic_swap`` and ``HNSWIndexManager``'s two
#: ``os.replace`` sites), which always installs a DIFFERENT inode. Detection
#: is therefore exact rather than probabilistic -- and costs nothing extra,
#: since all four fields come from the SAME single ``os.stat()`` call. A
#: content digest would be the alternative, and was rejected: hashing a
#: multi-megabyte graph on every query-path cache hit is far too expensive for
#: a check the inode already answers exactly.
_IndexFileFingerprint = Tuple[int, int, int, int]

#: Bug #1538: minimum interval between potentially-blocking freshness stats
#: for the SAME cache key.
#:
#: This bounds how often the CHECK runs. Its PURPOSE is not to define an
#: acceptable staleness window, but it does have that as a side effect: after a
#: successful check, a change landing within this interval is served stale
#: until the next check is due. That window is bounded and small, unlike the
#: indefinite staleness Bug #1538 reported. It is not a TTL -- entry lifetime
#: is still governed by ttl_minutes, independently of this.
#: The golden-repos mount is ``hard`` NFSv3,
#: where an outage makes ``os.stat()`` block in uninterruptible kernel retry;
#: without a rate limit, every query for a key would enter that blocking call.
#: Combined with the per-key in-flight guard (``_freshness_checking``), at most
#: ONE thread is inside a blocking stat for a given key at a time.
#:
#: The cost is that a change is noticed up to this long after it lands, on top
#: of the NFS client's own attribute-revalidation window -- negligible next to
#: a refresh cycle, and far smaller than the indefinite staleness Bug #1538
#: reported. A freshly loaded entry has ``freshness_checked_at=None``, which
#: always checks on its FIRST hit, so a refresh landing during the load is
#: still caught immediately rather than waiting out this interval.
_FRESHNESS_RECHECK_MIN_INTERVAL_SECONDS = 2.0


def _stat_index_fingerprint(index_file: Path) -> Optional[_IndexFileFingerprint]:
    """Return ``index_file``'s identity fingerprint, or None if it cannot be
    stat'd (missing file, OSError).

    A None result means freshness is UNVERIFIABLE for this file right now.
    Callers must treat that as "cannot confirm unchanged", never as
    "unchanged": ``_apply_freshness_verdict`` keeps serving the last verified
    graph and WARNs once per degradation, rather than silently accepting the
    entry as current or reloading it on every subsequent hit.
    """
    try:
        stat_result = os.stat(index_file)
    except OSError as e:
        logger.debug(
            f"Could not stat index file {index_file} for freshness tracking: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return None
    return (
        stat_result.st_mtime_ns,
        stat_result.st_size,
        stat_result.st_ino,
        stat_result.st_dev,
    )


@dataclass
class HNSWIndexCacheEntry:
    """
    Cache entry for a single repository's HNSW index (AC4: Per-Repository Isolation).

    Tracks:
    - HNSW index object (hnswlib.Index)
    - ID mapping (label -> vector ID)
    - Access timestamp for TTL refresh (AC3)
    - Access count for statistics (AC7)
    """

    hnsw_index: Any  # hnswlib.Index instance
    id_mapping: Dict[int, str]  # label -> vector ID
    repo_path: str
    ttl_minutes: float

    created_at: datetime = field(default_factory=datetime.now)
    last_accessed: datetime = field(default_factory=datetime.now)
    access_count: int = 0
    index_size_bytes: int = 0
    # EVO-64244 Facet 2 / Bug #1538: identity fingerprint of hnsw_index.bin,
    # captured BEFORE the load that produced this entry (see get_or_load).
    # None when the index_file path was not supplied, or when it could not be
    # stat'd at load time. None does NOT disable checking: once the file
    # becomes stat-able again, _apply_freshness_verdict evicts this entry so
    # the reload can stamp a real fingerprint (an entry that kept a None
    # fingerprint could never be verified for as long as it lived).
    index_file_fingerprint: Optional[_IndexFileFingerprint] = None
    # Bug #1538: monotonic timestamp of the last COMPLETED freshness check.
    # None means "never checked since this entry was loaded", which always
    # makes the next HIT check -- so a refresh that landed during the load is
    # caught immediately instead of waiting out the rate-limit interval.
    freshness_checked_at: Optional[float] = None
    # Bug #1538: True once an UNVERIFIABLE check has been reported for this
    # entry; reset as soon as a check succeeds. Without this, a persistently
    # failing stat would emit one WARNING per query instead of one per
    # degradation.
    freshness_unverified_reported: bool = False

    def record_access(self) -> None:
        """
        Record access to this cache entry (AC3: Access-based TTL refresh).

        Refreshes TTL by updating last_accessed timestamp.
        """
        self.last_accessed = datetime.now()
        self.access_count += 1

    def is_expired(self) -> bool:
        """
        Check if cache entry has exceeded TTL (AC2: TTL-based eviction).

        TTL is calculated from last_accessed time (not created_at),
        implementing access-based TTL refresh (AC3).

        Returns:
            True if expired, False otherwise
        """
        ttl_delta = timedelta(minutes=self.ttl_minutes)
        expiration_time = self.last_accessed + ttl_delta
        return datetime.now() > expiration_time

    def ttl_remaining_seconds(self) -> float:
        """
        Calculate remaining TTL in seconds (AC7: Statistics).

        Returns:
            Remaining TTL in seconds (negative if expired)
        """
        ttl_delta = timedelta(minutes=self.ttl_minutes)
        expiration_time = self.last_accessed + ttl_delta
        remaining = (expiration_time - datetime.now()).total_seconds()
        return remaining


@dataclass
class HNSWIndexCacheStats:
    """
    Cache statistics for monitoring (AC7: Cache Statistics).

    Provides visibility into:
    - Cache size and memory usage
    - Hit/miss ratio
    - Per-repository statistics
    """

    cached_repositories: int
    total_memory_mb: float
    hit_count: int
    miss_count: int
    eviction_count: int
    per_repository_stats: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    oversized_load_count: int = 0

    @property
    def hit_ratio(self) -> float:
        """Calculate cache hit ratio."""
        total = self.hit_count + self.miss_count
        return self.hit_count / total if total > 0 else 0.0


class HNSWIndexCache:
    """
    Thread-safe in-memory cache for HNSW indexes (AC5: Thread-Safe Operations).

    Provides:
    - AC1: Server-side index caching for performance
    - AC2: TTL-based eviction
    - AC3: Access-based TTL refresh
    - AC4: Per-repository cache isolation
    - AC5: Thread-safe operations with proper locking
    - AC6: Configuration externalization
    - AC7: Cache statistics and monitoring

    Performance improvement: ~277ms → <1ms for repeated queries (1800x faster).
    """

    def __init__(self, config: Optional[HNSWIndexCacheConfig] = None):
        """
        Initialize HNSW index cache.

        Args:
            config: Cache configuration (defaults to standard config if None)
        """
        self.config = config or HNSWIndexCacheConfig()

        # Per-repository cache (AC4)
        self._cache: Dict[str, HNSWIndexCacheEntry] = {}

        # Thread-safe locking (AC5)
        # Use Lock (not RLock): no reentrant usage exists; Lock is faster and
        # catches accidental reentrant acquisition (deadlock = fail-fast).
        self._cache_lock = Lock()

        # Per-key load-in-progress sentinels (Story #277: non-blocking cache population)
        # Maps normalized repo_path -> threading.Event signaled when load completes/fails.
        # Allows concurrent loads for DIFFERENT keys while deduplicating SAME-key loads.
        self._loading: Dict[str, threading.Event] = {}

        # Bug #1538: keys with a freshness stat currently in flight. Mirrors
        # the _loading sentinel idea for the CHECK rather than the load: the
        # stat runs with NO lock held and can block indefinitely on a hard NFS
        # mount, so only one thread enters it per key while the others serve
        # the entry they already have.
        self._freshness_checking: Set[str] = set()

        # Statistics tracking (AC7)
        self._hit_count = 0
        self._miss_count = 0
        self._eviction_count = 0
        self._oversized_load_count = 0  # Bug #1377: entries too big to ever cache

        # Background cleanup thread (AC2)
        self._cleanup_thread: Optional[threading.Thread] = None
        self._cleanup_stop_event = threading.Event()

        logger.info(
            f"HNSW Index Cache initialized with TTL={self.config.ttl_minutes} minutes",
            extra={"correlation_id": get_correlation_id()},
        )

    def get_or_load(
        self,
        repo_path: str,
        loader: Callable[[], Tuple[Any, Dict[int, str]]],
        index_file: Optional[Path] = None,
    ) -> Tuple[Any, Dict[int, str]]:
        """
        Get cached HNSW index or load if not cached (AC1: Cache Implementation).

        Implements:
        - AC1: Cache hit returns cached index
        - AC1: Cache miss loads index and caches it
        - AC3: Access refreshes TTL
        - AC5: Thread-safe operations with deduplication

        EVO-64244: a loader that returns (None, id_mapping) — because
        hnsw_index.bin does not exist yet (repo mid-(re)index) — is never
        cached, so a later query re-runs the loader and picks up the built
        index without waiting for TTL/restart. When index_file is provided,
        a cache HIT is also dropped if the on-disk index is no longer the
        file the entry was loaded from, since hnswlib has no in-place reload.

        Args:
            repo_path: Repository path (cache key)
            loader: Function to load index if not cached
                    Returns (hnsw_index, id_mapping)
            index_file: Optional path to hnsw_index.bin. When supplied, its
                    identity fingerprint — (st_mtime_ns, st_size, st_ino,
                    st_dev) — is captured BEFORE the load (Bug #1538) and
                    re-compared on later HITs, so an in-place refresh drops the
                    superseded in-RAM object. The comparison is INEQUALITY of
                    that fingerprint, never "mtime is newer". The stat runs
                    OUTSIDE the cache lock and is rate-limited per key
                    (_FRESHNESS_RECHECK_MIN_INTERVAL_SECONDS), so a stat hung on
                    a hard NFS mount cannot stall other cache consumers; a
                    freshly loaded entry is always checked on its first HIT. A
                    check that cannot be performed serves the last verified
                    graph and WARNs once per degradation rather than reloading
                    on every hit (see _apply_freshness_verdict). Default None
                    disables the freshness check entirely.

        Returns:
            Tuple of (hnsw_index, id_mapping)
        """
        # Normalize repo path for consistent cache keys
        repo_path = str(Path(repo_path).resolve())

        # Per-key Event sentinel pattern (Story #277: non-blocking cache population).
        #
        # The global _cache_lock is held ONLY for dict operations (microseconds).
        # Disk I/O (loader()) runs with NO lock held, so concurrent loads for
        # DIFFERENT keys proceed in parallel.
        #
        # Same-key deduplication: the first thread plants a threading.Event sentinel
        # in _loading[key]. Subsequent threads for the same key wait on that Event
        # (not on the global lock), then loop back to re-check the cache.
        #
        # Failure safety: the finally block always removes the sentinel and signals
        # the Event, ensuring waiters are never permanently blocked even on errors.
        while True:
            # Bug #1538: set when THIS iteration claimed the freshness check for
            # this key, carrying the entry AND its index_file together. The stat
            # then runs after the locked block exits, never inside it -- see
            # _verify_entry_freshness for why that matters.
            pending_check: Optional[Tuple[HNSWIndexCacheEntry, Path]] = None

            with self._cache_lock:
                # Check if cached (fast path: entry exists and not expired)
                if repo_path in self._cache:
                    entry = self._cache[repo_path]

                    if entry.is_expired():
                        # Evict expired entry and fall through to load
                        logger.debug(
                            f"Cache entry expired for {repo_path}, reloading",
                            extra={"correlation_id": get_correlation_id()},
                        )
                        del self._cache[repo_path]
                        self._eviction_count += 1
                        # Fall through (no return here)
                    elif entry.hnsw_index is None:
                        # EVO-64244 Facet 1: never serve a negatively-cached
                        # (None) index. If one somehow exists, treat it as a
                        # miss so the loader re-runs and picks up a now-built
                        # index rather than returning None for the full TTL.
                        logger.debug(
                            f"Cache entry for {repo_path} holds a None index, reloading",
                            extra={"correlation_id": get_correlation_id()},
                        )
                        del self._cache[repo_path]
                        self._eviction_count += 1
                        # Fall through (no return here)
                    elif (
                        index_file is not None
                        and repo_path not in self._freshness_checking
                        and self._freshness_check_due(entry)
                    ):
                        # EVO-64244 Facet 2 / Bug #1538: verify the on-disk
                        # identity, but do the stat OUTSIDE this lock. Claim the
                        # key so concurrent readers of the SAME key serve their
                        # entry instead of piling into the same blocking stat.
                        self._freshness_checking.add(repo_path)
                        # index_file is narrowed to Path by the condition above;
                        # carrying it with the entry keeps the call site typed.
                        pending_check = (entry, index_file)
                    else:
                        # Cache hit - refresh TTL (AC3). Reached when the check
                        # is not yet due, is already in flight on another
                        # thread, or is disabled (index_file is None).
                        entry.record_access()
                        self._hit_count += 1
                        logger.debug(
                            f"Cache HIT for {repo_path} (access_count={entry.access_count})",
                            extra={"correlation_id": get_correlation_id()},
                        )
                        return entry.hnsw_index, entry.id_mapping

                if pending_check is None:
                    # No ready entry. Is another thread already loading this key?
                    if repo_path in self._loading:
                        # Another thread is loading - become a waiter
                        event = self._loading[repo_path]
                        self._miss_count += 1
                        # Release lock BEFORE waiting - lets other threads proceed
                    else:
                        event = threading.Event()
                        self._loading[repo_path] = event
                        self._miss_count += 1
                        # Break out of the with-block to perform I/O without lock
                        break

            if pending_check is not None:
                # === NO LOCK HELD: this stat can block on a hung NFS mount ===
                entry_to_check, index_file_to_check = pending_check
                verified = self._verify_entry_freshness(
                    repo_path, index_file_to_check, entry_to_check
                )
                if verified is not None:
                    return verified
                # Entry was dropped, or replaced mid-stat: re-evaluate.
                continue

            # We are a waiter: block on the per-key Event (NOT on the global lock)
            logger.debug(
                f"Cache WAIT for {repo_path}, another thread is loading",
                extra={"correlation_id": get_correlation_id()},
            )
            event.wait()  # Wakes when the loader thread signals (success or failure)
            # Loop back: re-check cache (may find cached entry or become new loader)
            continue

        # --- NO LOCK HELD during disk I/O ---
        # We are the loader thread (sentinel planted in _loading[repo_path]).
        # Other threads for this key wait on the Event; other keys proceed freely.
        logger.debug(
            f"Cache MISS for {repo_path}, loading index",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            # Bug #1538 (root cause of the indefinite post-refresh staleness):
            # the fingerprint MUST be captured BEFORE the load, never after.
            # An in-place refresh that atomically replaces hnsw_index.bin
            # between the loader's read and the capture would otherwise stamp
            # the entry with the NEW file's identity while it holds the OLD
            # graph -- every later HIT then compares equal and serves the
            # pre-refresh graph forever, with no way to self-heal.
            #
            # Capturing pre-load is conservative in the safe direction: if the
            # file did change during the load, the stored fingerprint is the
            # superseded one, so the very next read detects the difference and
            # reloads (one extra load, never a stale answer). If it did not
            # change, the fingerprint still matches the current file and the
            # entry is served from RAM exactly as before -- no spurious reload.
            #
            # Fail-soft, as before: a missing file / OSError leaves this None,
            # which simply disables the freshness check for this entry.
            index_file_fingerprint: Optional[_IndexFileFingerprint] = (
                _stat_index_fingerprint(index_file) if index_file is not None else None
            )

            hnsw_index, id_mapping = loader()

            # EVO-64244 Facet 1: never negatively-cache a missing index.
            # load_index() returns None when hnsw_index.bin does not exist yet
            # (e.g. a repo mid-(re)index). Caching that None would serve
            # "index not found" for the full TTL even after the graph is built.
            # Return it directly WITHOUT storing an entry; the finally block
            # still releases the sentinel/Event so waiters re-run the loader.
            if hnsw_index is None:
                logger.debug(
                    f"Loader returned no HNSW index for {repo_path}, not caching",
                    extra={"correlation_id": get_correlation_id()},
                )
                return None, id_mapping

            # Capture real index memory footprint.
            # Bug #881 Phase 4: also add sys.getsizeof(id_mapping) so the cache
            # size cap accounts for the Python dict held alongside the native index.
            index_size_bytes = 0
            try:
                index_size_bytes = hnsw_index.index_file_size()
            except Exception as e:
                logger.warning(
                    f"Could not get index file size for {repo_path}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            index_size_bytes += sys.getsizeof(id_mapping)

            # Store result in cache (acquire lock for dict write)
            with self._cache_lock:
                entry = HNSWIndexCacheEntry(
                    hnsw_index=hnsw_index,
                    id_mapping=id_mapping,
                    repo_path=repo_path,
                    ttl_minutes=self.config.ttl_minutes,
                    index_size_bytes=index_size_bytes,
                    index_file_fingerprint=index_file_fingerprint,
                )
                entry.record_access()
                self._cache[repo_path] = entry
                # Enforce size limit while holding lock (as documented)
                self._enforce_size_limit()

            logger.info(
                f"Cached HNSW index for {repo_path}",
                extra={"correlation_id": get_correlation_id()},
            )
            return hnsw_index, id_mapping

        finally:
            # Always clean up sentinel and wake waiters, even on exception.
            # event.set() MUST be called OUTSIDE the lock: waiters wake up and
            # immediately try to re-acquire the lock; holding it here would deadlock.
            with self._cache_lock:
                self._loading.pop(repo_path, None)
            event.set()  # Wake ALL waiters (outside lock)

    def _freshness_check_due(self, entry: HNSWIndexCacheEntry) -> bool:
        """Return True if this entry's on-disk identity may be re-stat'd now.

        Bug #1538: the stat can block indefinitely on a hard NFS mount, so it
        is rate-limited per key instead of run on every hit. A freshly loaded
        entry (``freshness_checked_at is None``) is ALWAYS due, so a refresh
        that landed during the load is caught on the very next read.
        """
        if entry.freshness_checked_at is None:
            return True
        elapsed = time.monotonic() - entry.freshness_checked_at
        return elapsed >= _FRESHNESS_RECHECK_MIN_INTERVAL_SECONDS

    def _verify_entry_freshness(
        self,
        repo_path: str,
        index_file: Path,
        entry: HNSWIndexCacheEntry,
    ) -> Optional[Tuple[Any, Dict[int, str]]]:
        """Stat ``index_file`` WITHOUT the cache lock, then apply the verdict.

        The caller has already claimed this key in ``_freshness_checking`` and
        MUST NOT hold ``_cache_lock``. Bug #1538: on the ``hard`` NFSv3
        golden-repos mount a server outage makes ``os.stat()`` block in
        uninterruptible kernel retry, so running it under the shared lock would
        stall every other cache consumer in this worker behind one hung call.

        Returns the ``(hnsw_index, id_mapping)`` to serve, or None if the caller
        must re-evaluate (entry dropped, or replaced by another thread).
        """
        try:
            current_fingerprint = _stat_index_fingerprint(index_file)
            # The verdict runs INSIDE the try on purpose: it is what records
            # freshness_checked_at, so releasing the claim first would open a
            # window where a second thread sees neither an in-flight claim nor
            # a recorded timestamp, and starts another blocking stat -- the
            # rate limit would not be enforced across the handoff. No deadlock
            # risk: no lock is held here, and the verdict takes and releases
            # _cache_lock on its own.
            return self._apply_freshness_verdict(
                repo_path, index_file, entry, current_fingerprint
            )
        finally:
            # Release the claim on EVERY path, including an unexpected raise,
            # so a failure can never wedge this key's checks permanently.
            with self._cache_lock:
                self._freshness_checking.discard(repo_path)

    def _apply_freshness_verdict(
        self,
        repo_path: str,
        index_file: Path,
        entry: HNSWIndexCacheEntry,
        current_fingerprint: Optional[_IndexFileFingerprint],
    ) -> Optional[Tuple[Any, Dict[int, str]]]:
        """Apply a completed freshness check, under the cache lock.

        Comparison is INEQUALITY of the identity fingerprint, never "mtime is
        strictly newer": a same-tick rewrite, an NFS clock skew or a restored
        shard all change content without advancing the clock.

        A fingerprint we could not obtain NOW keeps serving the last verified
        graph (bounded: warn once, retry after the interval). A fingerprint that
        was never captured AT LOAD drops the entry instead -- the stat works
        now, so one reload re-stamps it permanently, whereas keeping it would
        retain an entry that can never be verified for as long as it lives.
        """
        with self._cache_lock:
            if self._cache.get(repo_path) is not entry:
                return None  # evicted/replaced mid-stat; caller re-evaluates
            entry.freshness_checked_at = time.monotonic()

            if current_fingerprint is None:
                if not entry.freshness_unverified_reported:
                    entry.freshness_unverified_reported = True
                    logger.warning(
                        f"Could not stat {index_file} to verify the cached HNSW "
                        "index is current; serving the last verified graph and "
                        "retrying later (freshness checking is degraded here)",
                        extra={"correlation_id": get_correlation_id()},
                    )
                return self._serve_locked(entry)

            # Succeeded: re-arm reporting so a LATER degradation is not muted.
            entry.freshness_unverified_reported = False

            if entry.index_file_fingerprint is None:
                drop_reason = "carries no freshness fingerprint"
            elif current_fingerprint != entry.index_file_fingerprint:
                drop_reason = "no longer matches the on-disk index"
            else:
                return self._serve_locked(entry)

            logger.debug(
                f"Cached entry for {repo_path} {drop_reason}, reloading",
                extra={"correlation_id": get_correlation_id()},
            )
            del self._cache[repo_path]
            self._eviction_count += 1
            return None

    def _serve_locked(self, entry: HNSWIndexCacheEntry) -> Tuple[Any, Dict[int, str]]:
        """Record a HIT and return the entry's payload.

        The caller MUST already hold ``_cache_lock``.
        """
        entry.record_access()
        self._hit_count += 1
        return entry.hnsw_index, entry.id_mapping

    def invalidate(self, repo_path: str) -> None:
        """
        Invalidate cache entry for specific repository.

        Args:
            repo_path: Repository path to invalidate
        """
        repo_path = str(Path(repo_path).resolve())

        with self._cache_lock:
            if repo_path in self._cache:
                del self._cache[repo_path]
                self._eviction_count += 1
                logger.info(
                    f"Invalidated cache for {repo_path}",
                    extra={"correlation_id": get_correlation_id()},
                )

    def invalidate_prefix(self, path_prefix: str) -> int:
        """Evict all cache entries whose key equals path_prefix or is under path_prefix/.

        Called by RefreshScheduler after swap_alias() to evict stale snapshot entries
        immediately rather than waiting for TTL expiry (Bug #881 Phase 2).

        Uses path separator guard: /a/b evicts /a/b/coll but NOT /a/barbaz.
        Thread-safe via _cache_lock.

        Args:
            path_prefix: Snapshot directory path whose entries to evict.
                         Must be non-empty.

        Returns:
            Number of entries evicted.

        Raises:
            ValueError: If path_prefix is None or empty string.
        """
        if not path_prefix:
            raise ValueError("path_prefix must be a non-empty string")

        path_prefix = str(Path(path_prefix).resolve())
        prefix_with_sep = path_prefix + "/"

        with self._cache_lock:
            stale_keys = [
                key
                for key in self._cache
                if key == path_prefix or key.startswith(prefix_with_sep)
            ]
            for key in stale_keys:
                del self._cache[key]
                self._eviction_count += 1

        evicted_count = len(stale_keys)
        logger.info(
            f"Evicted {evicted_count} stale HNSW cache entries for old snapshot: {path_prefix}",
            extra={"correlation_id": get_correlation_id()},
        )
        return evicted_count

    def clear(self) -> None:
        """Clear all cache entries."""
        with self._cache_lock:
            evicted = len(self._cache)
            self._cache.clear()
            self._eviction_count += evicted
            logger.info(
                f"Cleared cache ({evicted} entries)",
                extra={"correlation_id": get_correlation_id()},
            )

    def _evict_oversized_entries(self, cap_bytes: float) -> None:
        """
        Evict entries whose OWN size exceeds the entire cache cap, in isolation.

        Bug #1377 fix: such an entry can never be made to fit by evicting other
        (smaller, unrelated) entries -- doing so used to flush the whole cache
        for zero benefit. These entries are evicted FIRST and IN ISOLATION;
        no other entry is touched. Must be called while holding _cache_lock.
        """
        oversized_keys = [
            key for key, e in self._cache.items() if e.index_size_bytes > cap_bytes
        ]
        for key in oversized_keys:
            entry = self._cache.pop(key)
            self._eviction_count += 1
            self._oversized_load_count += 1
            logger.warning(
                f"HNSW index for {key} ({entry.index_size_bytes / (1024 * 1024):.1f}MB) "
                f"exceeds the entire cache cap ({self.config.max_cache_size_mb}MB) on its "
                "own and cannot be cached; it will cold-load on every access. Other "
                "cached entries were left untouched. Consider raising "
                "index_cache_max_size_mb or reducing worker count.",
                extra={"correlation_id": get_correlation_id()},
            )

    def _evict_lru_until_under_cap(self, max_cache_size_mb: int) -> None:
        """
        Evict least-recently-accessed entries until the cache is under the cap.

        Must be called while holding _cache_lock (does not acquire lock itself).

        Args:
            max_cache_size_mb: Non-Optional cap in MB, narrowed by the caller's
                None-check (self.config.max_cache_size_mb is Optional[int]).
        """
        current_size_mb = sum(e.index_size_bytes for e in self._cache.values()) / (
            1024 * 1024
        )

        while current_size_mb > max_cache_size_mb and self._cache:
            lru_repo_path = min(
                self._cache.keys(),
                key=lambda path: self._cache[path].last_accessed,
            )

            del self._cache[lru_repo_path]
            self._eviction_count += 1
            logger.debug(
                f"Evicted LRU cache entry to enforce size limit: {lru_repo_path}",
                extra={"correlation_id": get_correlation_id()},
            )

            current_size_mb = sum(e.index_size_bytes for e in self._cache.values()) / (
                1024 * 1024
            )

        if current_size_mb <= max_cache_size_mb and self._cache:
            logger.debug(
                f"Cache size: {current_size_mb}MB / {max_cache_size_mb}MB",
                extra={"correlation_id": get_correlation_id()},
            )

    def _enforce_size_limit(self) -> None:
        """
        Enforce cache size limit by evicting LRU entries (AC3A: Cache size limits).

        IMPORTANT: Must be called while holding _cache_lock (does not acquire lock itself).
        Called after adding new entries to ensure cache stays within max_cache_size_mb.
        Evicts oldest (least recently accessed) entries first.

        Bug #1377 fix: individually-oversized entries are evicted FIRST and IN
        ISOLATION via _evict_oversized_entries() (no other entry is touched),
        then normal LRU eviction runs via _evict_lru_until_under_cap() for the
        remaining entries that CAN fit together.
        """
        # Skip if no size limit configured
        max_cache_size_mb = self.config.max_cache_size_mb
        if max_cache_size_mb is None:
            return

        cap_bytes = max_cache_size_mb * 1024 * 1024
        self._evict_oversized_entries(cap_bytes)
        self._evict_lru_until_under_cap(max_cache_size_mb)

    def _cleanup_expired_entries(self) -> None:
        """
        Clean up expired cache entries (AC2: TTL-based eviction).

        Called by background cleanup thread and manual cleanup.
        """
        with self._cache_lock:
            expired_repos = [
                repo_path
                for repo_path, entry in self._cache.items()
                if entry.is_expired()
            ]

            for repo_path in expired_repos:
                del self._cache[repo_path]
                self._eviction_count += 1
                logger.debug(
                    f"Evicted expired cache entry: {repo_path}",
                    extra={"correlation_id": get_correlation_id()},
                )

            if expired_repos:
                logger.info(
                    f"Evicted {len(expired_repos)} expired cache entries",
                    extra={"correlation_id": get_correlation_id()},
                )

        # Bug #897 mitigation 1: optionally trim glibc heap after eviction.
        # Feature-flagged so operators can measure RSS recovery on staging
        # before committing. Default ON since v9.23.3; set enable_malloc_trim=false in config.json to disable.
        if _feature_flag_enabled("enable_malloc_trim"):
            _maybe_malloc_trim()

    def start_background_cleanup(self) -> None:
        """
        Start background cleanup thread (AC2: Automatic eviction).

        Thread periodically checks for expired entries and evicts them.
        """
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            logger.warning(
                format_error_log(
                    "GIT-GENERAL-012",
                    "Background cleanup thread already running",
                )
            )
            return

        self._cleanup_stop_event.clear()

        def cleanup_loop():
            """Background cleanup loop."""
            while not self._cleanup_stop_event.is_set():
                try:
                    self._cleanup_expired_entries()
                except Exception as e:
                    logger.error(
                        format_error_log(
                            "GIT-GENERAL-013",
                            f"Error in background cleanup: {e}",
                        )
                    )

                # Wait for cleanup interval or stop event
                self._cleanup_stop_event.wait(
                    timeout=self.config.cleanup_interval_seconds
                )

        self._cleanup_thread = threading.Thread(
            target=cleanup_loop, name="HNSWIndexCacheCleanup", daemon=True
        )
        self._cleanup_thread.start()
        logger.info(
            "Started background cache cleanup thread",
            extra={"correlation_id": get_correlation_id()},
        )

    def stop_background_cleanup(self) -> None:
        """Stop background cleanup thread."""
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            self._cleanup_stop_event.set()
            self._cleanup_thread.join(timeout=5)
            logger.info(
                "Stopped background cache cleanup thread",
                extra={"correlation_id": get_correlation_id()},
            )

    def evict_lru_entries(self, n: int) -> int:
        """Evict the n least-recently-used cache entries (YELLOW proactive action).

        Evicts at most min(n, current_size) entries in LRU order (oldest
        `last_accessed` first).  Thread-safe — acquires `_cache_lock`.
        Returns the count actually evicted (may be < n if cache has fewer entries).
        Never raises.

        Args:
            n: Number of LRU entries to evict.  Values <= 0 are treated as 0.

        Returns:
            Count of entries actually evicted.
        """
        if n <= 0:
            return 0
        evicted_count = 0
        with self._cache_lock:
            for _ in range(n):
                if not self._cache:
                    break
                lru_path = min(
                    self._cache.keys(),
                    key=lambda path: self._cache[path].last_accessed,
                )
                del self._cache[lru_path]
                self._eviction_count += 1
                evicted_count += 1
                logger.debug(
                    "GOV evict_lru_entries: evicted LRU entry %s",
                    lru_path,
                    extra={"correlation_id": get_correlation_id()},
                )
        return evicted_count

    def get_stats(self) -> HNSWIndexCacheStats:
        """
        Get cache statistics (AC7: Monitoring).

        Returns:
            HNSWIndexCacheStats with current cache metrics
        """
        with self._cache_lock:
            # Calculate total memory usage using real index sizes
            total_memory_mb = sum(e.index_size_bytes for e in self._cache.values()) / (
                1024 * 1024
            )

            # Per-repository stats
            per_repo_stats = {}
            for repo_path, entry in self._cache.items():
                per_repo_stats[repo_path] = {
                    "access_count": entry.access_count,
                    "last_accessed": entry.last_accessed.isoformat(),
                    "created_at": entry.created_at.isoformat(),
                    "ttl_remaining_seconds": entry.ttl_remaining_seconds(),
                }

            return HNSWIndexCacheStats(
                cached_repositories=len(self._cache),
                total_memory_mb=total_memory_mb,
                hit_count=self._hit_count,
                miss_count=self._miss_count,
                eviction_count=self._eviction_count,
                per_repository_stats=per_repo_stats,
                oversized_load_count=self._oversized_load_count,
            )
