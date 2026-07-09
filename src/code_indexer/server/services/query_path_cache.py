"""Drift-safe query-path caching primitives (Story #1082).

This module provides the single, KISS-conformant caching foundation that the
server query hot path uses to stop redundantly recomputing per-query
orchestration work (static model-spec re-parse, repo-config reload, provider
model-spec/config reconstruction) WITHOUT introducing staleness beyond the
configured policy.

Three primitives live here, deliberately in one cohesive module (anti-file-chaos):

1. ``TTLCache`` -- a generic, thread-safe, single-flight, bounded-LRU cache with
   explicit hit/miss/reload/invalidate/evict counters. Supports a NO-TTL mode
   (``ttl_seconds=None``) used ONLY for provably-immutable keys and static
   assets; even the NO-TTL mode is bounded by ``max_entries`` (LRU eviction).

2. ``is_immutable_versioned_snapshot(path)`` -- the ONLY gate that may route a
   key to a NO-TTL cache. Returns True strictly for a validated
   ``.versioned/{alias}/v_*`` snapshot path; everything else (mutable base
   clone, activated CoW clone, arbitrary paths) is rejected and MUST use the
   default SHORT-TTL cache.

3. ``provider_config_digest(...)`` -- a normalized, stable digest over ALL
   behavior-affecting provider-config fields, using the API key FINGERPRINT
   (never the raw secret) so two repos that differ only in
   endpoint/timeouts/retries never share cached provider state.

Staleness policy (see Story #1082):
- ZERO staleness only for static package assets and predicate-proven immutable
  keys (NO TTL, but still bounded).
- BOUNDED staleness <= configured short TTL ``T`` for mutable / not-provably
  immutable paths, provider-config state, and DB metadata.
- Auth-bearing rows are NEVER cached here.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from pathlib import PurePosixPath
from dataclasses import dataclass, field
from time import monotonic
from typing import Callable, Dict, Generic, Hashable, Optional, TypeVar

from code_indexer.server.storage.shared.snapshot_paths import (
    is_versioned_snapshot as _canonical_is_versioned_snapshot,
)

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


@dataclass
class _KeyLock:
    """Refcounted per-key loader lock.

    ``lock`` serializes loaders for one key (single-flight). ``refcount`` tracks
    how many callers currently hold or are waiting on this key-lock; an entry is
    reclaimable from the registry ONLY when ``refcount == 0``. ``epoch`` is the
    per-key invalidate generation: every ``invalidate(key)`` bumps it so a value
    loaded across an invalidate is never stored as fresh.

    All fields are mutated ONLY under the owning cache's structure ``_lock``.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    refcount: int = 0
    epoch: int = 0


class TTLCache(Generic[K, V]):
    """Thread-safe, single-flight, bounded-LRU cache with TTL and counters.

    Concurrency model:
    - A single structure lock (``_lock``) guards the store, the counters, and
      the refcounted per-key lock registry. It is held only for O(1)
      bookkeeping, never across a (potentially slow) loader call or across the
      ``with key_lock`` critical section -> no deadlock.
    - Each key has its own REFCOUNTED loader lock (``_KeyLock``). On a
      miss/expiry, the caller acquires the key-lock (refcount += 1), double-
      checks freshness, and runs the loader exactly once. Concurrent callers
      for the SAME key block on that key-lock (single-flight: no thundering
      herd), then observe the freshly stored value on the double-check. Callers
      for DIFFERENT keys never contend.
    - STRICT single-flight: a key-lock with ``refcount > 0`` is NEVER reclaimed
      by ``invalidate`` or LRU eviction, so a second concurrent caller for an
      in-flight key reuses the SAME key-lock (and blocks) instead of minting a
      new one and starting a second loader.
    - Invalidate-during-load correctness via a per-key epoch: the loader caller
      snapshots the key's epoch before loading; ``invalidate(key)`` bumps it; if
      the epoch advanced during the load, the freshly loaded value is stored as
      ALREADY-EXPIRED so it is never served as fresh and the next get reloads.

    NO-TTL mode: ``ttl_seconds=None`` disables expiry. Entries still obey the
    ``max_entries`` LRU bound and respond to ``invalidate``/``clear``.
    """

    def __init__(
        self,
        ttl_seconds: Optional[float],
        max_entries: int,
        loader: Callable[[K], V],
        time_fn: Callable[[], float] = monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive or None, got {ttl_seconds}")

        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._loader = loader
        self._time_fn = time_fn

        # key -> (value, expires_at_or_None). OrderedDict gives O(1) LRU.
        self._store: "OrderedDict[K, tuple[V, Optional[float]]]" = OrderedDict()
        self._lock = threading.Lock()
        # Refcounted per-key loader locks. A holder with refcount > 0 is pinned
        # (never reclaimed by invalidate/eviction) -> strict single-flight.
        self._key_locks: Dict[K, _KeyLock] = {}

        self._counters: Dict[str, int] = {
            "hit": 0,
            "miss": 0,
            "reload": 0,
            "invalidate": 0,
            "evict": 0,
        }

    # ---- internal helpers (callers hold no lock unless noted) ----

    def _fresh(self, expires_at: Optional[float], now: float) -> bool:
        return expires_at is None or now < expires_at

    def _acquire_key_lock(self, key: K) -> _KeyLock:
        """Pin (get-or-create) the key's refcounted holder. Caller MUST release.

        Under ``_lock``: get-or-create the ``_KeyLock`` for ``key`` and bump its
        refcount. While refcount > 0 the holder cannot be reclaimed by
        ``invalidate`` or LRU eviction, so a concurrent caller for the same key
        observes the SAME holder and blocks on its lock (single-flight) rather
        than minting a new one and starting a second loader.
        """
        with self._lock:
            holder = self._key_locks.get(key)
            if holder is None:
                holder = _KeyLock()
                self._key_locks[key] = holder
            holder.refcount += 1
            return holder

    def _release_key_lock(self, key: K) -> None:
        """Drop one refcount; reclaim the holder only when it reaches zero.

        Under ``_lock``: decrement the holder's refcount. The registry entry is
        reclaimable strictly at ``refcount == 0`` (no in-flight or waiting
        caller), keeping the registry bounded by ``max_entries`` worth of live
        holders without ever orphaning an in-flight loader's lock.
        """
        with self._lock:
            holder = self._key_locks.get(key)
            if holder is None:
                return
            holder.refcount -= 1
            if holder.refcount <= 0:
                self._key_locks.pop(key, None)

    def _try_fresh_hit(self, key: K, now: float) -> tuple[bool, Optional[V]]:
        """Return (hit, value). Bumps hit counter and refreshes LRU on hit."""
        with self._lock:
            entry = self._store.get(key)
            if entry is not None and self._fresh(entry[1], now):
                self._store.move_to_end(key)  # LRU: mark most-recently used
                self._counters["hit"] += 1
                return True, entry[0]
        return False, None

    def _store_value(
        self, key: K, value: V, now: float, store_expired: bool = False
    ) -> None:
        """Store a freshly loaded value, enforcing the LRU bound.

        When ``store_expired`` is True (the key was invalidated DURING this
        load, i.e. its epoch advanced), the value is stored with an
        already-past expiry so it is never served as fresh -- the next get
        observes the stale entry and reloads. This holds even in NO-TTL mode.

        Key-locks are NOT reclaimed here on eviction: the refcounted registry
        (``_acquire_key_lock``/``_release_key_lock``) governs key-lock lifetime,
        so an in-flight key-lock (refcount > 0) survives eviction of its store
        entry -> strict single-flight.
        """
        with self._lock:
            if store_expired:
                # Strictly in the past relative to ``now`` so _fresh() == False
                # on the very next read, regardless of TTL/NO-TTL mode.
                expires_at: Optional[float] = now - 1.0
            else:
                expires_at = None if self._ttl is None else now + self._ttl
            self._store[key] = (value, expires_at)
            self._store.move_to_end(key)
            self._counters["reload"] += 1
            # Enforce bound (LRU eviction of oldest entries). Key-lock holders
            # are reclaimed by refcount, NOT here -- evicting an in-flight key's
            # store entry must NOT orphan its (still-pinned) loader lock.
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)
                self._counters["evict"] += 1

    # ---- public API ----

    def get(self, key: K) -> V:
        now = self._time_fn()
        hit, value = self._try_fresh_hit(key, now)
        if hit:
            return value  # type: ignore[return-value]

        # Miss or expired: single-flight on the REFCOUNTED per-key lock. The
        # holder is pinned (refcount > 0) across the whole critical section, so
        # a concurrent invalidate/eviction cannot reclaim it and let a sibling
        # caller start a second loader for the same key.
        holder = self._acquire_key_lock(key)
        try:
            with holder.lock:
                now = self._time_fn()
                # Double-check: another thread may have loaded it while we waited.
                hit, value = self._try_fresh_hit(key, now)
                if hit:
                    return value  # type: ignore[return-value]

                with self._lock:
                    self._counters["miss"] += 1
                    # Snapshot the invalidate generation BEFORE loading. If
                    # invalidate(key) bumps it during the load, the result must
                    # not be cached as fresh.
                    epoch_before = holder.epoch

                loaded = self._loader(key)

                with self._lock:
                    invalidated_mid_load = holder.epoch != epoch_before
                self._store_value(
                    key,
                    loaded,
                    self._time_fn(),
                    store_expired=invalidated_mid_load,
                )
                return loaded
        finally:
            self._release_key_lock(key)

    def invalidate(self, key: K) -> None:
        """Remove a single key and bump its invalidate epoch.

        Drops any cached entry AND advances the key's ``_KeyLock.epoch`` so a
        loader that is mid-flight for this key (it holds a pinned holder)
        detects the invalidate and stores its result as already-expired -- the
        next get reloads, never serving a value invalidated during its own load.

        A pinned holder (refcount > 0) is NEVER popped here (strict single-
        flight); an idle holder (refcount == 0) created solely to carry the
        epoch bump is reclaimed immediately. Counted when the key was cached OR
        a loader is in-flight (so an invalidate landing mid-load is observable).
        """
        with self._lock:
            was_cached = key in self._store
            if was_cached:
                del self._store[key]

            holder = self._key_locks.get(key)
            in_flight = holder is not None and holder.refcount > 0
            if holder is not None:
                holder.epoch += 1
                if holder.refcount <= 0:
                    # Idle holder -> nothing to pin; drop it to stay bounded.
                    self._key_locks.pop(key, None)

            if was_cached or in_flight:
                self._counters["invalidate"] += 1

    def clear(self) -> None:
        """Remove all entries. Counters are preserved for telemetry.

        Idle key-lock holders (refcount == 0) are dropped. Pinned holders
        (refcount > 0, an in-flight loader) are PRESERVED so the owning caller
        can still release them and single-flight is not broken; their epoch is
        bumped so any value loaded across this clear is stored as expired rather
        than fresh.
        """
        with self._lock:
            self._store.clear()
            for k in list(self._key_locks.keys()):
                holder = self._key_locks[k]
                if holder.refcount <= 0:
                    del self._key_locks[k]
                else:
                    holder.epoch += 1

    def size(self) -> int:
        with self._lock:
            return len(self._store)

    def key_lock_count(self) -> int:
        """Number of live key-lock holders in the registry.

        Bounded by ``max_entries`` worth of live holders at rest (every
        completed get releases its refcount, making the holder reclaimable), so
        this never grows with total distinct keys seen -- only with concurrent
        in-flight/cached keys. Exposed for the bounded-registry regression test.
        """
        with self._lock:
            return len(self._key_locks)

    def counters(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counters)


# ---------------------------------------------------------------------------
# Immutable-path predicate (the ONLY gate for NO-TTL caching)
# ---------------------------------------------------------------------------


def is_immutable_versioned_snapshot(path: str) -> bool:
    """Return True ONLY for a path at or inside an immutable canonical snapshot.

    Bug #1084 B4: the ``.versioned`` decision is delegated to the SINGLE canonical
    predicate (``snapshot_paths.is_versioned_snapshot``), CANONICAL clause only
    (``mount_point`` omitted). The immutable golden-repo snapshot layout (project
    CLAUDE.md "Golden Repo Versioned Path") is ``.../.versioned/{alias}/v_{ts}/``;
    a refresh produces a NEW ``v_*`` directory (a new key / cache miss), never an
    in-place mutation, so its config can be cached with NO TTL. Canonical
    cow-daemon snapshots (``{mount}/.versioned/{ns}/v_*``) now legitimately gain
    NO-TTL caching.

    LEGACY cow shapes (``{mount}/{ns}/v_*``) and flat ONTAP (``{mount}/v_*``) are
    deliberately NOT recognized here (the mount point is intentionally withheld):
    those predate retention and could be deleted, so the NO-TTL immutability
    promise must not extend to them -- they stay on the SHORT-TTL cache, the safe
    (self-healing) direction. The MUTABLE base clone and activated CoW clones are
    likewise rejected and MUST use the default SHORT-TTL cache.

    The canonical predicate matches the snapshot LEAF (``v_<ts>``); a query path
    is frequently DEEPER (e.g. ``.../v_42/.code-indexer/config.json``) and is
    equally immutable. So we also test the path's ancestors: True iff the path
    itself OR any ancestor is a canonical snapshot. Purely structural (no
    filesystem access, NFS-safe); traversal tokens are rejected by the canonical
    predicate's normalization.
    """
    if not path:
        return False

    if "/../" in path or path.endswith("/..") or path.startswith("../") or path == "..":
        # Reject traversal tokens up front (Story #1082 contract). The canonical
        # predicate would also reject the resulting shape, but be explicit.
        return False

    if _canonical_is_versioned_snapshot(path):
        return True

    # Walk ancestors: a subpath inside a canonical snapshot is still immutable.
    pure = PurePosixPath(path)
    for ancestor in pure.parents:
        ancestor_str = str(ancestor)
        if ancestor_str in ("", "/", "."):
            break
        if _canonical_is_versioned_snapshot(ancestor_str):
            return True

    return False


# ---------------------------------------------------------------------------
# Provider-config digest (normalized; key FINGERPRINT, never the raw secret)
# ---------------------------------------------------------------------------


def api_key_fingerprint(api_key: Optional[str]) -> str:
    """Return a short, non-reversible fingerprint of an API key.

    NEVER returns or embeds the raw secret. ``None``/empty keys map to a stable
    sentinel so an unconfigured provider still produces a deterministic digest.
    """
    if not api_key:
        return "nokey"
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def provider_config_digest(
    *,
    provider: str,
    model: str,
    api_key: Optional[str],
    api_endpoint: str,
    connect_timeout: float,
    timeout: float,
    max_retries: Optional[int] = None,
    retry_delay: Optional[float] = None,
    exponential_backoff: Optional[bool] = None,
) -> str:
    """Compute a stable digest over ALL behavior-affecting provider-config fields.

    The digest is the cache key for parsed provider/model-spec state. It MUST
    cover every field that changes provider behavior so that two repos sharing a
    provider/model/key but differing in endpoint, timeouts, or retry/backoff
    settings produce DISTINCT digests (and therefore never share cached state).

    The API key is included ONLY via its fingerprint (``api_key_fingerprint``);
    the raw secret never enters the digest input.

    Cohere-only fields (``max_retries``/``retry_delay``/``exponential_backoff``)
    are included when provided; for providers that do not use them they are
    ``None`` and contribute a stable, distinct token.
    """
    # Canonical, order-stable representation. Field names are embedded so a
    # value moving between fields cannot collide.
    components = [
        f"provider={provider}",
        f"model={model}",
        f"keyfp={api_key_fingerprint(api_key)}",
        f"endpoint={api_endpoint}",
        f"connect_timeout={connect_timeout}",
        f"timeout={timeout}",
        f"max_retries={max_retries}",
        f"retry_delay={retry_delay}",
        f"exponential_backoff={exponential_backoff}",
    ]
    canonical = "|".join(components)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# RepoConfigCache: default-to-TTL repo-config cache (Scenarios 2/6/7/10/12)
# ---------------------------------------------------------------------------


class RepoConfigCache(Generic[V]):
    """Cache parsed repo Config keyed on the repo path, with drift safety.

    Routing (default-to-TTL):
    - A path PROVEN immutable by ``is_immutable_versioned_snapshot`` goes to a
      NO-TTL bounded-LRU cache: a golden-repo refresh produces a NEW versioned
      path (new key = cache miss), never an in-place mutation, so zero staleness.
    - EVERY other path -- including the MUTABLE base clone returned by
      ``GoldenRepoManager.get_actual_repo_path()`` Priority-1, and activated CoW
      clones -- goes to a SHORT-TTL bounded cache so missed invalidations
      self-correct within ``config_ttl_seconds``.

    Both sub-caches are bounded (``config_max_entries`` each) and single-flight.
    ``invalidate(path)`` clears the entry from both sub-caches so a refresh/sync
    event takes effect immediately regardless of how the path was classified.
    """

    def __init__(
        self,
        config_ttl_seconds: float,
        config_max_entries: int,
        loader: Callable[[str], V],
        time_fn: Callable[[], float] = monotonic,
    ) -> None:
        self._immutable: TTLCache[str, V] = TTLCache(
            ttl_seconds=None,
            max_entries=config_max_entries,
            loader=loader,
            time_fn=time_fn,
        )
        self._mutable: TTLCache[str, V] = TTLCache(
            ttl_seconds=config_ttl_seconds,
            max_entries=config_max_entries,
            loader=loader,
            time_fn=time_fn,
        )

    def get_config(self, repo_path: str) -> V:
        if is_immutable_versioned_snapshot(repo_path):
            return self._immutable.get(repo_path)
        return self._mutable.get(repo_path)

    def invalidate(self, repo_path: str) -> None:
        """Drop the path from BOTH sub-caches (refresh/sync/repoint event)."""
        self._immutable.invalidate(repo_path)
        self._mutable.invalidate(repo_path)

    def clear(self) -> None:
        self._immutable.clear()
        self._mutable.clear()

    def immutable_size(self) -> int:
        return self._immutable.size()

    def mutable_size(self) -> int:
        return self._mutable.size()

    def counters(self) -> Dict[str, Dict[str, int]]:
        return {
            "immutable": self._immutable.counters(),
            "mutable": self._mutable.counters(),
        }
