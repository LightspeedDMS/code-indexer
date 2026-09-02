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

Bug #1775 fix -- stale-prefix eviction: golden-repo refresh NEVER replaces
a chunk-store file in place -- every refresh creates a brand-new
``.versioned/<repo>/v_<ts>/.../chunks.db`` path and atomically swaps the
alias pointer (``GoldenRepoManager._cb_swap_alias`` and the other real
alias-swap/publish sites -- see ``server/cache/snapshot_cache_
invalidation.py``). Because the OLD snapshot's own db_path never changes,
the mtime-based invalidation above NEVER fires for it, so the stale handle
(and its open sqlite3 connection / file descriptor) was held forever --
bounded only by the passive per-thread LRU cap, multiplied by thread count
(production incident: ~1260 leaked fds).

Round-3 FINAL design (both an independent Claude review and an
independent Codex review, converged on this after round-2's per-key-only
redesign was found to have deleted the only mechanism that actually
closed leaked handles -- see below): TWO pieces together, not one or the
other.

1. ``_stale_prefixes`` (a plain ``set[str]``) + ``_is_stale()``: the
   round-2 piece, kept as-is because both reviews confirmed it works. A
   cheap (O(path-depth): ``resolved in stale_set`` plus a walk of
   ``PurePosixPath(resolved).parents``), definitive, ALWAYS-current
   correctness check for the SPECIFIC key being requested -- run
   unconditionally on every ``get_or_open()`` call (hit and miss alike).

2. ``_stale_prefixes_ordered`` (an append-only, deduped list) + a
   per-thread cursor (``_local.stale_cursor``) + ``_sweep_stale_same_
   thread()``: restores round-2's ORIGINAL sweep idea, bounded
   differently than round-2's actual defect. Round-2's cursor design
   copied+scanned the FULL historical registry on every cache MISS and
   every new thread's first touch (still effectively O(N) at either
   point, just moved off the hot HIT path). This sweep instead scans
   ONLY the calling thread's own capped (<= 32-entry) dict against the
   DELTA of newly-registered prefixes since THAT THREAD's own last sweep
   -- in steady state that delta is 0 or 1, so this is cheap; it is
   never a full-registry scan.

   WHY THIS IS NECESSARY (not optional): round-2's per-key-only check is
   evaluated ONLY for the key a caller actually requests. After a real
   alias swap, callers resolve to the NEW snapshot target and simply
   never request the OLD path again by name -- so ``_is_stale()`` is
   NEVER evaluated for that old, now-orphaned cache entry. It just sits
   until the 32-entry LRU eventually rotates it out -- reproducing the
   EXACT ``32 x thread-count`` mechanism that produced production's
   ~1260 leaked fds in the first place. Claude's reviewer reproduced
   this directly: after ``invalidate_prefix()`` plus 20 subsequent real
   queries against a NEW target, the OLD snapshot's fd was STILL open.
   The restored sweep is what actually closes it, proactively, without
   requiring the caller to ever touch the old key again.

Round-3 simplification (now that the sweep exists again): a cache MISS on
a key ``_is_stale()`` flags is cached NORMALLY (round-2's "return
uncached" branch, and its dependence on CPython refcounting to avoid a
leak on that path, is removed) -- the sweep proactively re-evicts it
later if this thread stops using it, and the LRU cap remains the final
backstop.

Bounded growth (round-3 MEDIUM remediation, round-4 HONESTY correction):
``_stale_prefixes`` / ``_stale_prefixes_ordered`` growth is capped
(``max_tracked_stale_prefixes``, default 1,000,000 -- both an independent
Claude review and an independent Codex review measured unbounded growth
at fleet scale). Once exceeded, the OLDEST half of BOTH structures is
trimmed together, tracked via ``_stale_prefixes_trim_offset`` so a thread
whose cursor fell behind a trim self-heals: it re-sweeps everything
CURRENTLY tracked once (a safe, idempotent, larger-than-usual sweep).
This never crosses a thread boundary -- the trim only mutates the SHARED
registry (already lock-protected); each thread's own cursor recovery
happens locally, inside its own next ``get_or_open()`` call. The trim
always retains AT LEAST the newest entry (round-4 floor-bug fix): the
old ``trim_count = len(ordered) - (cap // 2)`` math discarded EVERYTHING,
including the entry just added, when ``cap // 2 == 0`` (e.g.
``max_tracked_stale_prefixes=1``), silently disabling invalidation
entirely from that point on.

ROUND-4 HONEST LIMITATION (both an independent Claude review and an
independent Codex review, independently reproduced -- Codex with a
crippled cap): self-heal above only re-syncs against prefixes that are
STILL RETAINED at the time of the sweep. If a prefix is trimmed away
BEFORE the owning thread's cursor ever advances past it (e.g. it is
registered, then enough OTHER prefixes are registered by OTHER threads
to push it out of the retained half, all before this thread's next
``get_or_open()`` call for ANY key), the loss is TOTAL -- discarded from
both the ordered list (breaks the sweep) AND the set (breaks even a
DIRECT per-key ``_is_stale()`` re-check of the exact same path). That one
entry then degrades to LRU-only eviction, identical to the pre-#1775
baseline. This is a narrow, accepted residual risk, not a claim that
growth is "not load-bearing": full watermark-based trim tracking (never
discard a prefix a still-alive thread's cursor hasn't reached) was
considered and judged disproportionate complexity for the size of this
gap -- it also introduces its own hazard (an idle-but-still-alive thread
blocking all trimming indefinitely) that would need its own safety
valve. The chosen mitigation is instead the substantially raised default
cap above (50,000 -> 1,000,000, ~130 bytes/entry, ~130MB worst case --
trivial at this project's fleet scale), which pushes the reachable
window from ~days to ~weeks/months of ZERO cache access from one
specific thread combined with continuous fleet-wide invalidation churn
-- see ``TestPrefixTrimmedBeforeAnySweepIsPermanentlyLost`` for a
deterministic repro at a deliberately crippled cap.

Normalization uses ``os.path.normpath()`` (pure string manipulation, ZERO
filesystem syscalls) rather than ``Path(...).resolve()`` -- alias
pointers are JSON path records, not symlinks, so symlink resolution was
never actually needed, and a real syscall here is a genuine risk on this
project's ``hard`` NFSv3 shared-storage mount (CLAUDE.md's standing
invariant: a ``hard`` mount can block a filesystem call FOREVER when the
backing host is unresponsive).

The master-base-clone guard (a repo's FIRST-EVER refresh has
``old_target`` equal to the master clone, not a ``.versioned/`` snapshot)
lives ONE LAYER UP, in ``server/cache/snapshot_cache_invalidation.py``.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from typing import List, Optional, Set, Tuple, Union

from code_indexer.storage.sqlite_chunk_store import (
    ChunkStore,
    open_chunk_store_for_path,
)

#: Bound on the number of distinct chunks.db handles a single thread keeps
#: open at once. Generous enough that a normal query-serving thread never
#: evicts a handle it will reuse moments later, but still finite.
_MAX_ENTRIES_PER_THREAD = 32

#: Bound on the number of distinct stale prefixes tracked at once (both
#: the set AND the ordered/cursor-sweepable list). Once exceeded, the
#: OLDEST half is trimmed -- see module docstring's "Bounded growth"
#: section for why this is safe, how a lagging thread self-heals, and the
#: round-4 honest residual-limitation note (a prefix trimmed away BEFORE
#: the owning thread's next sweep IS genuinely lost, not merely delayed --
#: this cap is deliberately generous, ~130MB worst case, to push that
#: window out to weeks/months rather than days).
_MAX_TRACKED_STALE_PREFIXES = 1_000_000

_CacheEntry = Tuple[Optional[int], ChunkStore]


def _safe_close(store: ChunkStore) -> None:
    try:
        store.close()
    except Exception:
        # Best-effort: an already-closed/broken connection must never
        # prevent the cache from proceeding to open a fresh one.
        pass


def _matches_stale_prefix(resolved_path: str, prefixes: Set[str]) -> bool:
    """True iff ``resolved_path`` equals a prefix in ``prefixes`` or lives
    under one as a genuine filesystem ANCESTOR directory.

    ``prefixes`` is a ``Set[str]`` (O(1) membership).

    Uses ``PurePosixPath(...).parents`` -- a REAL ancestor-directory
    walk, not a string-prefix-with-separator check -- so a sibling
    snapshot sharing a textual prefix (e.g. ``v_1`` vs ``v_10``) can
    never be mismatched: ``PurePosixPath("/a/b/v_10/x").parents`` never
    yields ``/a/b/v_1``, because that is not an actual ancestor of that
    path.
    """
    if resolved_path in prefixes:
        return True
    for parent in PurePosixPath(resolved_path).parents:
        if str(parent) in prefixes:
            return True
    return False


class ChunkStoreThreadCache:
    """Per-thread, mtime-invalidated cache of open :class:`ChunkStore`
    handles.

    Safe to share ONE instance across as many threads as needed (e.g. a
    server's shared query-executor thread pool): the shared instance only
    coordinates which per-thread store lives under ``threading.local()`` --
    it never itself holds or hands out a connection across threads.
    """

    def __init__(
        self,
        max_entries_per_thread: int = _MAX_ENTRIES_PER_THREAD,
        max_tracked_stale_prefixes: int = _MAX_TRACKED_STALE_PREFIXES,
    ) -> None:
        if max_entries_per_thread < 1:
            raise ValueError(
                f"max_entries_per_thread must be >= 1, got {max_entries_per_thread}"
            )
        if max_tracked_stale_prefixes < 1:
            raise ValueError(
                "max_tracked_stale_prefixes must be >= 1, got "
                f"{max_tracked_stale_prefixes}"
            )
        self._max_entries = max_entries_per_thread
        self._max_tracked_stale_prefixes = max_tracked_stale_prefixes
        self._local = threading.local()
        # Bug #1775: shared, lock-protected. `_stale_prefixes` (a plain
        # set) backs the definitive per-key `_is_stale()` check.
        # `_stale_prefixes_ordered` (append-only, deduped) + `_stale_
        # prefixes_trim_offset` back the restored per-thread cursor
        # sweep. Both are trimmed together once `max_tracked_stale_
        # prefixes` is exceeded -- see module docstring. ONLY ever
        # read/written under `_stale_prefixes_lock`; NEVER used to reach
        # into another thread's `threading.local()` entries.
        self._stale_prefixes: Set[str] = set()
        self._stale_prefixes_ordered: List[str] = []
        self._stale_prefixes_trim_offset = 0
        self._stale_prefixes_lock = threading.Lock()

    def _entries(self) -> "OrderedDict[Tuple[str, bool], _CacheEntry]":
        entries = getattr(self._local, "entries", None)
        if entries is None:
            entries = OrderedDict()
            self._local.entries = entries
        return entries

    def invalidate_prefix(self, path_prefix: str) -> None:
        """Mark ``path_prefix`` (and everything under it) stale.

        Bug #1775: called by ``snapshot_cache_invalidation.
        invalidate_snapshot_caches()`` after a real alias swap/publish,
        with the OLD (now-superseded) snapshot directory. This is
        intentionally the ONLY thing this method does -- it never touches
        any thread's cached entries directly, so it is always safe to
        call from any thread (the caller's own, a background scheduler
        thread, etc.). Actual eviction happens lazily, same-thread, inside
        ``get_or_open()`` (see :meth:`_is_stale` and
        :meth:`_sweep_stale_same_thread`).

        Adds to BOTH the shared set (definitive correctness) and the
        ordered list (cursor-sweepable) -- repeated registrations of the
        same path dedupe naturally against the set, so the ordered list
        never gets a duplicate append either. Normalizes via
        ``os.path.normpath()`` (pure string, zero filesystem syscalls --
        see module docstring) so a non-canonical form (trailing slash,
        double separator, embedded ``./``) registered here still matches
        on lookup.

        Trims the OLDEST half of both structures once
        ``max_tracked_stale_prefixes`` is exceeded -- see module
        docstring's "Bounded growth" section.
        """
        if not path_prefix:
            raise ValueError("path_prefix must be a non-empty string")
        normalized = os.path.normpath(path_prefix)
        with self._stale_prefixes_lock:
            if normalized in self._stale_prefixes:
                return
            self._stale_prefixes.add(normalized)
            self._stale_prefixes_ordered.append(normalized)

            if len(self._stale_prefixes_ordered) > self._max_tracked_stale_prefixes:
                trim_count = len(self._stale_prefixes_ordered) - (
                    self._max_tracked_stale_prefixes // 2
                )
                # Round-4 floor-bug fix: never trim the newest
                # just-registered entry, even at a tiny cap where
                # `cap // 2 == 0` would otherwise discard EVERYTHING
                # (silently disabling invalidation entirely).
                trim_count = min(trim_count, len(self._stale_prefixes_ordered) - 1)
                trimmed = self._stale_prefixes_ordered[:trim_count]
                del self._stale_prefixes_ordered[:trim_count]
                self._stale_prefixes_trim_offset += trim_count
                for stale_path in trimmed:
                    self._stale_prefixes.discard(stale_path)

    def _is_stale(self, db_path_str: str) -> bool:
        """True iff ``db_path_str`` equals a registered stale prefix, or
        lives under one as a genuine filesystem ANCESTOR directory.

        O(path depth) -- a handful of hash lookups against the shared
        set, held under the lock only for their brief duration (never a
        full copy/scan of the set). Independent of how many prefixes have
        ever been registered, so this is cheap enough to call
        unconditionally on every ``get_or_open()`` call (hit AND miss).
        """
        resolved = os.path.normpath(db_path_str)
        with self._stale_prefixes_lock:
            return _matches_stale_prefix(resolved, self._stale_prefixes)

    def _stale_prefixes_delta_since(self, cursor: int) -> Tuple[Set[str], int]:
        """Return ``(delta, new_cursor)``: the prefixes registered SINCE
        ``cursor``, as a ``Set[str]`` (round-4 fix -- O(1) membership;
        previously a ``list``, O(n), measured ~50ms vs ~0.66ms on a
        25,000-entry delta in code review), and the cursor value to
        persist for the next call.

        Self-healing across a trim: if ``cursor`` predates a trim
        (``cursor < trim_offset``), the slice would otherwise start at a
        negative/invalid index -- clamped to 0 instead, which
        conservatively returns EVERYTHING currently tracked rather than
        silently skipping prefixes this thread never got to see.
        """
        with self._stale_prefixes_lock:
            trim_offset = self._stale_prefixes_trim_offset
            list_start = max(0, cursor - trim_offset)
            delta = set(self._stale_prefixes_ordered[list_start:])
            new_cursor = trim_offset + len(self._stale_prefixes_ordered)
        return delta, new_cursor

    def _sweep_stale_same_thread(
        self, entries: "OrderedDict[Tuple[str, bool], _CacheEntry]"
    ) -> None:
        """Close and drop every entry in THIS thread's own ``entries``
        whose db_path is under a prefix registered SINCE this thread's
        own last sweep. Same-thread only -- always safe per this module's
        sqlite3 thread-affinity contract.

        Bug #1775 round-3 restoration: consumes the shared stale-prefix
        registry via :meth:`_stale_prefixes_delta_since` and a PER-THREAD
        cursor (``_local.stale_cursor``) rather than re-examining the
        full historical registry on every call. In steady state the
        delta since a thread's last sweep is 0 or 1 new prefixes, so this
        scan (bounded by ``_max_entries`` <= 32 cached entries times that
        tiny delta) is cheap -- this is what actually converges leaked
        fds to zero when callers stop referencing an old snapshot
        entirely (the realistic production pattern), not merely when
        they happen to re-request it by name.

        Round-4 addition: FIRST, unconditionally drains this thread's
        ``pending_recheck`` set (populated by :meth:`get_or_open` when a
        stale key is evicted+recached under a prefix that ALREADY
        predates this thread's cursor within that SAME call) -- the
        ordinary cursor-delta scan below can never catch these, because
        the causing prefix is old news to the cursor by the time the
        entry exists to be swept.
        """
        pending = getattr(self._local, "pending_recheck", None)
        if pending:
            for pending_key in pending:
                cached = entries.pop(pending_key, None)
                if cached is not None:
                    _mtime, store = cached
                    _safe_close(store)
            pending.clear()

        cursor = getattr(self._local, "stale_cursor", 0)
        new_prefixes, new_cursor = self._stale_prefixes_delta_since(cursor)
        if not new_prefixes:
            return

        stale_keys = [
            key
            for key in entries
            if _matches_stale_prefix(os.path.normpath(key[0]), new_prefixes)
        ]
        for key in stale_keys:
            _mtime, store = entries.pop(key)
            _safe_close(store)
        self._local.stale_cursor = new_cursor

    def _mark_pending_recheck(self, key: Tuple[str, bool]) -> None:
        """Record ``key`` for a FORCED, unconditional eviction on this
        thread's next sweep opportunity -- see :meth:`_sweep_stale_same_
        thread`'s round-4 addition docstring.
        """
        pending = getattr(self._local, "pending_recheck", None)
        if pending is None:
            pending = set()
            self._local.pending_recheck = pending
        pending.add(key)

    def get_or_open(
        self,
        db_path: Union[str, Path],
        collection_path: str,
        *,
        read_only: bool = False,
    ) -> ChunkStore:
        """Return this THREAD's cached :class:`ChunkStore` for ``db_path``,
        opening (or reopening, on a genuine mtime change) as needed.

        Args:
            db_path: Path to the ``chunks.db``-equivalent SQLite file.
            collection_path: The collection directory path, forwarded to
                ``open_chunk_store_for_path()`` for its mutable-vs-immutable
                decision -- unchanged from the pre-cache call site.
            read_only: Bug #1760 -- forwarded to ``open_chunk_store_for_path
                (..., read_only=read_only)``; also part of the cache key so
                a mutable and a read-only handle for the same db_path are
                never confused. Read-only callers MUST pass True.
        """
        entries = self._entries()

        # Bug #1775 round-3: sweep THIS thread's own (small, <=
        # _max_entries) cached entries for anything that's gone stale
        # since this thread's cursor last advanced -- bounded by the LRU
        # cap times the delta of newly-registered prefixes since this
        # thread's last call, NOT by the full historical registry size.
        # This is what actually closes leaked fds for entries this
        # thread never re-requests by name (the common case after a real
        # alias swap).
        self._sweep_stale_same_thread(entries)

        key = (str(db_path), read_only)

        # Definitive per-key check (always against the FULL current set,
        # cheap: O(path depth) hash lookups) -- catches the requested key
        # even if its prefix predates this thread's cursor.
        is_stale_hit = self._is_stale(key[0])
        if is_stale_hit:
            cached = entries.pop(key, None)
            if cached is not None:
                _mtime, stale_store = cached
                _safe_close(stale_store)
            # Fall through to the normal open+cache path below -- a
            # stale key is now cached normally; the sweep above will
            # proactively re-evict it later if this thread stops using
            # it, and the LRU cap remains the final backstop. Round-4:
            # this thread's cursor already consumed the causing prefix
            # during THIS call (the sweep runs before this check), so
            # _mark_pending_recheck() below forces a one-shot eviction on
            # this thread's NEXT sweep opportunity -- the ordinary
            # cursor-delta scan alone could never catch this entry.

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

        store = open_chunk_store_for_path(db_path, collection_path, read_only=read_only)
        try:
            fresh_mtime: Optional[int] = os.stat(db_path).st_mtime_ns
        except OSError:
            fresh_mtime = current_mtime
        entries[key] = (fresh_mtime, store)
        if is_stale_hit:
            self._mark_pending_recheck(key)
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


# ---------------------------------------------------------------------------
# Singleton accessor (mirrors get_global_id_index_cache / reset_global_id_
# index_cache in server/cache/id_index_cache.py, and the identical
# collection_meta_cache.py accessor added alongside this one).
#
# Post-manual-E2E-test production fix (Story #1492 follow-up): a real
# running server was strace-verified to show ZERO cross-request benefit
# from this cache, because FilesystemVectorStore.__init__ only constructs
# a ChunkStoreThreadCache() when the caller passes None -- and every query
# constructs a brand-new FilesystemVectorStore, so every instance got its
# own private, single-use cache that died with it. This module's own
# docstring already documents that ONE shared instance is safe across as
# many threads as needed (it only coordinates which per-thread store lives
# under threading.local() -- it never itself holds or hands out a
# connection across threads), so FilesystemBackend.get_vector_store_
# client() must inject THIS singleton in server mode.
# ---------------------------------------------------------------------------

_global_chunk_store_cache_instance: Optional[ChunkStoreThreadCache] = None
_global_chunk_store_cache_lock = threading.Lock()


def get_global_chunk_store_cache() -> ChunkStoreThreadCache:
    """Get or create the process-wide ChunkStoreThreadCache singleton."""
    global _global_chunk_store_cache_instance
    if _global_chunk_store_cache_instance is None:
        with _global_chunk_store_cache_lock:
            if _global_chunk_store_cache_instance is None:
                _global_chunk_store_cache_instance = ChunkStoreThreadCache()
    return _global_chunk_store_cache_instance


def reset_global_chunk_store_cache() -> None:
    """Reset the singleton (for testing)."""
    global _global_chunk_store_cache_instance
    with _global_chunk_store_cache_lock:
        _global_chunk_store_cache_instance = None
