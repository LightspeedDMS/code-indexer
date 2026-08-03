"""Per-thread cache of open ChunkStore handles (Story #1492 AC3).

Finding C5 (MEDIUM, report rank 11): ``open_chunk_store_for_path()`` was
called ONCE PER QUERY for mutable collections. ``ChunkStore.__init__``
runs ``_ensure_schema()`` DDL, ``_load_persisted_dim()``, and constructs
fresh zstd compressor/decompressor objects EVERY time it is called --
real, avoidable cost on the query hot path for a REPEAT query against the
SAME collection.

MANDATORY constraint (Story #1456 AC7's established, binding contract):
``sqlite3`` connections are NOT safely shared across threads. This module
respects that ABSOLUTELY: ``ChunkStoreThreadCache`` is built on
``threading.local()`` -- every cached ``ChunkStore`` (and its one open
sqlite3 connection) is visible ONLY to the thread that opened it. Two
different threads calling ``get_or_open()`` for the identical ``db_path``
always receive two DISTINCT ``ChunkStore`` objects with two DISTINCT
connections; nothing here ever hands a connection from one thread to
another. A design that shared one connection across threads would violate
this contract and is explicitly rejected by this implementation.

Invalidation: keyed on ``(str(db_path), mtime_ns)`` -- the CURRENT mtime is
read via a cheap ``os.stat()`` on every ``get_or_open()`` call. A store
whose ``chunks.db`` file was replaced (e.g. ``os.replace`` during a
rebuild/consolidation) gets a fresh mtime, so the stale cached handle is
closed and a new one opened -- it is NEVER used to serve stale or invalid
data. Per-thread cache size is bounded (LRU) so a long-lived worker thread
that touches many distinct collections over its lifetime does not
accumulate an unbounded number of open sqlite3 connections/file
descriptors.

This module does NOT touch the read-only inspection primitive
(``chunk_store_has_real_data``) or the immutable-open branch's semantics --
both remain exactly as Story #1459 hardened them. ``get_or_open()`` simply
wraps ``open_chunk_store_for_path()`` (unchanged), adding a cache layer in
front of it.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional, Tuple, Union

from code_indexer.storage.sqlite_chunk_store import (
    ChunkStore,
    open_chunk_store_for_path,
)

#: Bound on the number of distinct chunks.db handles a single thread keeps
#: open at once. Generous enough that a normal query-serving thread never
#: evicts a handle it will reuse moments later, but still finite.
_MAX_ENTRIES_PER_THREAD = 32

_CacheEntry = Tuple[Optional[int], ChunkStore]


def _safe_close(store: ChunkStore) -> None:
    try:
        store.close()
    except Exception:
        # Best-effort: an already-closed/broken connection must never
        # prevent the cache from proceeding to open a fresh one.
        pass


class ChunkStoreThreadCache:
    """Per-thread, mtime-invalidated cache of open :class:`ChunkStore`
    handles.

    Safe to share ONE instance across as many threads as needed (e.g. a
    server's shared query-executor thread pool): the shared instance only
    coordinates which per-thread store lives under ``threading.local()`` --
    it never itself holds or hands out a connection across threads.
    """

    def __init__(self, max_entries_per_thread: int = _MAX_ENTRIES_PER_THREAD) -> None:
        if max_entries_per_thread < 1:
            raise ValueError(
                f"max_entries_per_thread must be >= 1, got {max_entries_per_thread}"
            )
        self._max_entries = max_entries_per_thread
        self._local = threading.local()

    def _entries(self) -> "OrderedDict[str, _CacheEntry]":
        entries = getattr(self._local, "entries", None)
        if entries is None:
            entries = OrderedDict()
            self._local.entries = entries
        return entries

    def get_or_open(
        self, db_path: Union[str, Path], collection_path: str
    ) -> ChunkStore:
        """Return this THREAD's cached :class:`ChunkStore` for ``db_path``,
        opening (or reopening, on a genuine mtime change) as needed.

        Args:
            db_path: Path to the ``chunks.db``-equivalent SQLite file.
            collection_path: The collection directory path, forwarded to
                ``open_chunk_store_for_path()`` for its mutable-vs-immutable
                decision -- unchanged from the pre-cache call site.
        """
        entries = self._entries()
        key = str(db_path)

        try:
            current_mtime: Optional[int] = os.stat(db_path).st_mtime_ns
        except OSError:
            # Missing right now (e.g. not yet created) -- open_chunk_store_
            # for_path() will create it (mutable path) or raise
            # (immutable path), matching pre-cache behavior exactly. Not
            # cached under a None-mtime key long-term (fresh_mtime below
            # replaces it immediately after a successful open).
            current_mtime = None

        cached = entries.get(key)
        if cached is not None:
            cached_mtime, store = cached
            if cached_mtime == current_mtime:
                entries.move_to_end(key)
                return store
            # Underlying file identity changed (e.g. os.replace during a
            # rebuild) -- the cached handle must NEVER be reused to serve
            # stale/invalid data. Close it and fall through to reopen.
            _safe_close(store)
            del entries[key]

        store = open_chunk_store_for_path(db_path, collection_path)
        try:
            fresh_mtime: Optional[int] = os.stat(db_path).st_mtime_ns
        except OSError:
            fresh_mtime = current_mtime
        entries[key] = (fresh_mtime, store)
        entries.move_to_end(key)

        while len(entries) > self._max_entries:
            _, (_, evicted_store) = entries.popitem(last=False)
            _safe_close(evicted_store)

        return store

    def close_current_thread(self) -> None:
        """Close and drop every cached handle owned by THIS thread.

        Must be called from the SAME thread that opened the entries
        (threading.local semantics) -- never from a different thread.
        """
        entries = self._entries()
        for _key, (_mtime, store) in entries.items():
            _safe_close(store)
        entries.clear()
