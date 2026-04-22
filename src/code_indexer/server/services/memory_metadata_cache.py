"""
MemoryMetadataCache — bounded TTL cache for memory file frontmatter.

Story #877 Phase 3-A.

Caches parsed YAML frontmatter dicts from memory files to avoid repeated
filesystem reads in the hot path of filter_cidx_meta_files(). Uses an
OrderedDict as an LRU backing store: on every cache hit the entry is moved
to the end (most-recently-used); eviction removes from the front (oldest/LRU).

Thread safety: all mutations are guarded by a threading.RLock. The cache
stores a private shallow copy of each frontmatter dict, and callers also
receive a fresh shallow copy, so neither the cache nor any caller share the
same mutable dict instance.

Fail-closed contract: if a memory file cannot be read or parsed for any
reason, get() returns None and logs the condition. The caller
(AccessFilteringService) then excludes the file from results.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from code_indexer.server.services.memory_io import (
    MemoryFileCorruptError,
    MemoryFileNotFoundError,
    read_memory_file,
)

logger = logging.getLogger(__name__)

# memory_id must be exactly 32 lowercase hex chars (uuid4().hex format)
_MEMORY_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# (frontmatter_dict, cache_timestamp)
_CacheEntry = Tuple[Dict[str, Any], float]


def _validate_memory_id(memory_id: str) -> None:
    """Raise ValueError if memory_id does not match the expected hex pattern."""
    if not isinstance(memory_id, str) or not _MEMORY_ID_RE.match(memory_id):
        raise ValueError(
            f"memory_id must be exactly 32 lowercase hex characters, got {memory_id!r}"
        )


class MemoryMetadataCache:
    """Bounded LRU cache for memory file frontmatter dicts with TTL expiry.

    Args:
        memories_dir: Directory containing ``{memory_id}.md`` files.
        ttl_seconds:  How long a cached entry is considered fresh (default 60 s).
                      Must be > 0.
        max_entries:  Maximum number of entries before LRU eviction (default 2048).
                      Must be > 0.
        _clock:       Injectable time source (default ``time.monotonic``).
                      Accepts any callable returning a float (seconds).

    Raises:
        ValueError: if ttl_seconds <= 0 or max_entries <= 0.
    """

    def __init__(
        self,
        memories_dir: Path,
        *,
        ttl_seconds: int = 60,
        max_entries: int = 2048,
        _clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be > 0, got {ttl_seconds!r}")
        if max_entries <= 0:
            raise ValueError(f"max_entries must be > 0, got {max_entries!r}")
        self._memories_dir = memories_dir
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock: Callable[[], float] = _clock or time.monotonic
        # OrderedDict used as LRU: end = most-recently-used, front = LRU
        self._store: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Return parsed frontmatter dict for memory_id, or None on any failure.

        Cache hit fresher than TTL returns a shallow copy of the cached dict.
        Stale or missing entries are read from disk, cached (as a private copy),
        and returned (as another copy). Neither the cache nor the caller share
        the same mutable dict instance.

        If the file is missing or unparseable, returns None (fail-closed).

        Args:
            memory_id: Must be exactly 32 lowercase hex characters.

        Returns:
            Shallow copy of the frontmatter dict, or None.
        """
        try:
            _validate_memory_id(memory_id)
        except ValueError:
            logger.debug(
                "MemoryMetadataCache.get: invalid memory_id %r — returning None",
                memory_id,
            )
            return None

        with self._lock:
            now = self._clock()
            entry = self._store.get(memory_id)
            if entry is not None:
                cached_fm, ts = entry
                if (now - ts) < self._ttl_seconds:
                    # Move to end (most-recently-used) and return a copy to caller
                    self._store.move_to_end(memory_id)
                    return dict(cached_fm)
                # Stale — remove and re-read below
                del self._store[memory_id]

        # Cache miss or stale: read from disk (outside lock to avoid I/O under lock)
        raw_fm = self._load_from_disk(memory_id)
        if raw_fm is None:
            return None

        # Store a private copy in the cache; return a separate copy to the caller
        cached_copy = dict(raw_fm)
        with self._lock:
            self._store[memory_id] = (cached_copy, self._clock())
            self._store.move_to_end(memory_id)
            self._evict_if_needed()
        return dict(cached_copy)

    def invalidate(self, memory_id: str) -> None:
        """Remove a single entry from the cache.  Idempotent."""
        with self._lock:
            self._store.pop(memory_id, None)

    def invalidate_all(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._store.clear()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_from_disk(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Read memory file from disk. Returns None on any error (fail-closed).

        Every error path logs the condition before returning None.
        """
        path = self._memories_dir / f"{memory_id}.md"
        try:
            fm, _body, _hash = read_memory_file(path)
            return fm
        except MemoryFileNotFoundError:
            logger.debug(
                "MemoryMetadataCache: memory file not found for %r — excluding",
                memory_id,
            )
            return None
        except MemoryFileCorruptError as exc:
            logger.debug(
                "MemoryMetadataCache: corrupt memory file %r — excluding: %s",
                memory_id,
                exc,
            )
            return None
        except Exception as exc:
            logger.warning(
                "MemoryMetadataCache: unexpected error reading %r — excluding: %s",
                memory_id,
                exc,
            )
            return None

    def _evict_if_needed(self) -> None:
        """Evict oldest (LRU) entries until len <= max_entries. Caller holds lock."""
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)  # remove LRU (front)
