"""Story #1787 AC9/AC14: Python-side proxy for the X-Ray graph cache.

`GraphCache` (AC9, `rust/xray-core/src/graph/graph_cache.rs`) is an
in-process Rust LRU that only outlives a single `xray-cli` subprocess
invocation -- there is no persistent graph-serving daemon (ADR-003
Decision 4). What the SERVER process can actually retain and let the
governor evict is retention of the AC7 mmap-backed wire file a build
produced: keeping it mapped avoids paying re-extraction+re-bind on a
repeat query (AC9's "served from cache" framing), and dropping it from
this LRU (closing the mmap) is what AC14's YELLOW eviction does.

`XrayGraphCacheProxy` implements the SAME `get_stats()`/
`evict_lru_entries(n)` contract `MemoryGovernor.evict_lru_to_floor()`
already expects (mirroring `HNSWIndexCache`, including its
lock-protected registry), so it can be attached via the EXISTING
`governor.attach_cache()` -- see `cache_governor_bridge.py` for how it
coexists with the HNSW cache already attached there.
"""

from __future__ import annotations

import logging
import mmap
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class XrayGraphCacheStats:
    """Matches `HNSWIndexCacheStats`'s shape (`cached_repositories`) so
    `evict_lru_to_floor()`'s `stats.cached_repositories` read works
    unchanged against this cache too.
    """

    cached_repositories: int


class GraphWireFileHandle:
    """One mmap'd AC7 wire file. `wire_file_path` MUST be absolute --
    the only real callers are server-internal code that already resolved
    a concrete path, and requiring absolute rejects a malformed/relative
    input early with a clear error rather than silently resolving
    against an unpredictable cwd (mirrors `xray-cli`'s own
    `--files-from` convention). `close()` unmaps and closes the
    underlying file descriptor; never raises (best-effort teardown,
    matching the project's cgroup-cleanup convention).
    """

    def __init__(self, wire_file_path: str) -> None:
        if (
            not wire_file_path
            or not wire_file_path.startswith("/")
            or ".." in wire_file_path.split("/")
        ):
            raise ValueError(
                f"wire_file_path must be an absolute path with no '..' components, got: {wire_file_path!r}"
            )
        self.wire_file_path = wire_file_path
        self._file = open(wire_file_path, "rb")
        try:
            self._mmap: Optional[mmap.mmap] = mmap.mmap(
                self._file.fileno(), 0, access=mmap.ACCESS_READ
            )
        except ValueError:
            # mmap.mmap refuses a zero-length file -- an empty/placeholder
            # wire file is a legitimate (if degenerate) input, not a bug.
            self._mmap = None
        except Exception:
            # Any OTHER mmap failure must not leak the already-opened fd.
            self._file.close()
            raise

    def close(self) -> None:
        try:
            if self._mmap is not None:
                self._mmap.close()
                self._mmap = None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "GraphWireFileHandle.close: mmap close failed (best-effort): %s", exc
            )
        finally:
            try:
                self._file.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "GraphWireFileHandle.close: file close failed (best-effort): %s",
                    exc,
                )


def _require_repo_snapshot_identity(repo_snapshot_identity: str) -> None:
    if not isinstance(repo_snapshot_identity, str) or not repo_snapshot_identity:
        raise ValueError(
            f"repo_snapshot_identity must be a non-empty string, got: {repo_snapshot_identity!r}"
        )


class XrayGraphCacheProxy:
    """LRU registry of `GraphWireFileHandle`s, keyed by
    `repo_snapshot_identity` (AC9's cache key). All registry access is
    guarded by `_lock`, mirroring `HNSWIndexCache`'s own `_cache_lock`.
    Attach an instance via `governor.attach_cache()` (AC14) to make it
    subject to YELLOW proactive eviction alongside any other attached
    cache (see `cache_governor_bridge.CompositeLRUCache`). `get_stats`/
    `evict_lru_entries` -- the two methods `evict_lru_to_floor()`
    actually calls -- are added in the next change.
    """

    def __init__(self) -> None:
        self._entries: "OrderedDict[str, GraphWireFileHandle]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, repo_snapshot_identity: str) -> Optional[GraphWireFileHandle]:
        """Returns the cached handle and marks it most-recently-used, or
        `None` on a cache miss.
        """
        _require_repo_snapshot_identity(repo_snapshot_identity)
        with self._lock:
            handle = self._entries.get(repo_snapshot_identity)
            if handle is not None:
                self._entries.move_to_end(repo_snapshot_identity)
            return handle

    def put(self, repo_snapshot_identity: str, handle: GraphWireFileHandle) -> None:
        """Inserts/replaces the entry for `repo_snapshot_identity`,
        marking it most-recently-used. Replacing an existing entry closes
        the handle it displaces.
        """
        _require_repo_snapshot_identity(repo_snapshot_identity)
        if handle is None:
            raise ValueError("handle must not be None")
        with self._lock:
            existing = self._entries.get(repo_snapshot_identity)
            if existing is not None and existing is not handle:
                existing.close()
            self._entries[repo_snapshot_identity] = handle
            self._entries.move_to_end(repo_snapshot_identity)

    def get_stats(self) -> XrayGraphCacheStats:
        with self._lock:
            return XrayGraphCacheStats(cached_repositories=len(self._entries))

    def evict_lru_entries(self, n: int) -> int:
        """Evicts the `n` least-recently-used entries (YELLOW proactive
        action), closing each handle's mmap. Returns the count actually
        evicted (may be < n if the cache has fewer entries). Values
        <= 0 evict nothing. Never raises.
        """
        if n <= 0:
            return 0
        evicted = 0
        with self._lock:
            for _ in range(n):
                if not self._entries:
                    break
                _repo_snapshot_identity, handle = self._entries.popitem(last=False)
                handle.close()
                evicted += 1
        return evicted


# ---------------------------------------------------------------------------
# Process-level singleton — None until server startup installs it.
#
# H4/H6 remediation: mirrors memory_governor.py's own
# get/set/clear_memory_governor() pattern exactly, so service_init.py can
# install the ONE XrayGraphCacheProxy instance composed into the governor
# (see cache_governor_bridge.CompositeLRUCache) and a future graph-build
# call site retrieves that SAME instance rather than constructing a second,
# disconnected proxy the governor never sees.
# ---------------------------------------------------------------------------

_xray_graph_cache: Optional[XrayGraphCacheProxy] = None
_xray_graph_cache_lock = threading.Lock()


def get_xray_graph_cache() -> Optional[XrayGraphCacheProxy]:
    """Return the process-level X-Ray graph cache, or None (CLI/pre-init)."""
    with _xray_graph_cache_lock:
        return _xray_graph_cache


def set_xray_graph_cache(cache: XrayGraphCacheProxy) -> None:
    """Install the process-level X-Ray graph cache (called once in server
    service_init)."""
    global _xray_graph_cache
    with _xray_graph_cache_lock:
        _xray_graph_cache = cache


def clear_xray_graph_cache() -> None:
    """Clear the process-level X-Ray graph cache (lifespan shutdown / test
    isolation)."""
    global _xray_graph_cache
    with _xray_graph_cache_lock:
        _xray_graph_cache = None
