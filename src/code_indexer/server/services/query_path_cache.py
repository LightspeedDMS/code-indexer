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
import os
import re
import threading
from collections import OrderedDict
from time import monotonic
from typing import Callable, Dict, Generic, Hashable, Optional, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """Thread-safe, single-flight, bounded-LRU cache with TTL and counters.

    Concurrency model:
    - A single structure lock (``_lock``) guards the store, the counters, and
      the per-key lock registry. It is held only for O(1) bookkeeping, never
      across a (potentially slow) loader call.
    - Each key has its own loader lock. On a miss/expiry, the caller acquires
      the per-key lock, double-checks freshness, and runs the loader exactly
      once. Concurrent callers for the SAME key block on that per-key lock
      (single-flight: no thundering herd), then observe the freshly stored
      value on the double-check. Callers for DIFFERENT keys never contend.

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
        self._key_locks: Dict[K, threading.Lock] = {}

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

    def _key_lock(self, key: K) -> threading.Lock:
        with self._lock:
            lk = self._key_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._key_locks[key] = lk
            return lk

    def _try_fresh_hit(self, key: K, now: float) -> tuple[bool, Optional[V]]:
        """Return (hit, value). Bumps hit counter and refreshes LRU on hit."""
        with self._lock:
            entry = self._store.get(key)
            if entry is not None and self._fresh(entry[1], now):
                self._store.move_to_end(key)  # LRU: mark most-recently used
                self._counters["hit"] += 1
                return True, entry[0]
        return False, None

    def _store_value(self, key: K, value: V, now: float) -> None:
        """Store a freshly loaded value, enforcing the LRU bound."""
        with self._lock:
            expires_at = None if self._ttl is None else now + self._ttl
            self._store[key] = (value, expires_at)
            self._store.move_to_end(key)
            self._counters["reload"] += 1
            # Enforce bound (LRU eviction of oldest entries).
            while len(self._store) > self._max_entries:
                evicted_key, _ = self._store.popitem(last=False)
                self._counters["evict"] += 1
                self._key_locks.pop(evicted_key, None)

    # ---- public API ----

    def get(self, key: K) -> V:
        now = self._time_fn()
        hit, value = self._try_fresh_hit(key, now)
        if hit:
            return value  # type: ignore[return-value]

        # Miss or expired: single-flight on the per-key lock.
        with self._key_lock(key):
            now = self._time_fn()
            # Double-check: another thread may have loaded it while we waited.
            hit, value = self._try_fresh_hit(key, now)
            if hit:
                return value  # type: ignore[return-value]

            with self._lock:
                self._counters["miss"] += 1

            loaded = self._loader(key)
            self._store_value(key, loaded, self._time_fn())
            return loaded

    def invalidate(self, key: K) -> None:
        """Remove a single key. No-op (and not counted) if absent."""
        with self._lock:
            if key in self._store:
                del self._store[key]
                self._key_locks.pop(key, None)
                self._counters["invalidate"] += 1

    def clear(self) -> None:
        """Remove all entries. Counters are preserved for telemetry."""
        with self._lock:
            self._store.clear()
            self._key_locks.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._store)

    def counters(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counters)


# ---------------------------------------------------------------------------
# Immutable-path predicate (the ONLY gate for NO-TTL caching)
# ---------------------------------------------------------------------------

# A versioned snapshot path looks like: <golden_repos_dir>/.versioned/<alias>/v_<timestamp>
# The alias segment must not be empty and the version segment must be v_<digits>.
_VERSION_SEGMENT_RE = re.compile(r"^v_\d+$")


def is_immutable_versioned_snapshot(path: str) -> bool:
    """Return True ONLY for a validated immutable ``.versioned/{alias}/v_*`` path.

    The immutable golden-repo snapshot layout (see project CLAUDE.md "Golden Repo
    Versioned Path") is ``.../.versioned/{alias}/v_{timestamp}/...``. A query path
    that matches this shape is bound to an unchanging source: a golden-repo
    refresh produces a NEW ``v_*`` directory (a new key / cache miss), never an
    in-place mutation, so its config can be cached with NO TTL.

    Everything else -- the MUTABLE base clone returned by
    ``GoldenRepoManager.get_actual_repo_path()`` Priority-1, activated CoW
    clones, and arbitrary paths -- is rejected here and MUST use the default
    SHORT-TTL cache.

    The check is purely structural (no filesystem access, NFS-safe): it requires
    a ``.versioned`` segment immediately followed by a non-empty alias segment
    and then a ``v_<digits>`` segment. Path traversal tokens (``..``) are
    rejected.
    """
    if not path:
        return False

    normalized = os.path.normpath(path)
    parts = normalized.split(os.sep)

    if ".." in parts:
        return False

    try:
        idx = parts.index(".versioned")
    except ValueError:
        return False

    # Need at least: .versioned / <alias> / v_<ts>
    if idx + 2 >= len(parts):
        return False

    alias_segment = parts[idx + 1]
    version_segment = parts[idx + 2]

    if not alias_segment or alias_segment in (".", ".."):
        return False
    if not _VERSION_SEGMENT_RE.match(version_segment):
        return False

    return True


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
