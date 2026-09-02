"""Cross-process propagation for ChunkStoreThreadCache stale-prefix
signals (GitHub Bug #1775 round 5).

Live staging validation (real evidence): single-worker solo staging
passed cleanly (fd count flat across 10 real refresh+query cycles), but
2-worker CLUSTERED staging FAILED -- fd count grew monotonically
(104->134, chunks.db handles 20->50, 11 leaked snapshot generations), and
120 follow-up queries with no further refresh reclaimed zero handles.

Root cause: ``ChunkStoreThreadCache``'s ``_stale_prefixes``/
``_stale_prefixes_ordered`` registry (rounds 1-4, already correct and
tested) lives in PER-PROCESS memory -- a module-level singleton inside
each uvicorn worker. When worker A calls ``invalidate_prefix()``, the
signal is registered ONLY in worker A's own memory; worker B, a separate
OS process sharing nothing in RAM, never learns about it. This is
CLAUDE.md's "Cluster-Aware State -- ABSOLUTE RULE" violated in its
purest form: module-level RAM holding state that must be visible to
another HTTP request in a cluster.

Fix: this module is an ADDITIVE cross-process propagation layer built on
this project's ALREADY-ESTABLISHED cluster-aware mechanism,
``PayloadCache`` (SQLite solo / PostgreSQL cluster -- CLAUDE.md's
designated system for exactly this "ephemeral cross-node data" class of
problem), not a new parallel mechanism. It does NOT touch, replace, or
duplicate rounds 1-4's per-key correctness check, sweep, pending-recheck,
or local trim logic -- those are unchanged and remain the sole authority
for LOCAL (single-process) correctness. This module's only job is:
publish a stale prefix so every OTHER process can discover it, and feed
discovered prefixes into that SAME existing, proven local pipeline via
the ordinary ``ChunkStoreThreadCache.invalidate_prefix()`` call.

Design: ``PayloadCache`` exposes single-key store/retrieve only (no
"list keys" or "list keys since timestamp" capability -- confirmed by
inspection of ``PayloadCacheBackend``). Rather than extend that protocol
(cross-cutting, higher-risk change touching both SQLite and PostgreSQL
backends), this module uses ONE well-known key holding a bounded,
JSON-encoded, deduped list of recently-registered stale prefixes.
``publish_stale_prefix()`` performs a read-modify-write append (dedup,
floor-safe trim) under normal PayloadCache semantics -- a rare
last-writer-wins race between two near-simultaneous publishers could
drop the LOSING writer's prefix from the SHARED blob (though that
writer's own process still applied it locally, so it is never lost for
the publishing process itself), which is a strictly smaller residual
risk than round 4's already-accepted local trim-gap limitation.

Permanent dedup-by-exact-path is domain-safe: each ``prefix`` published
here is a SPECIFIC versioned-snapshot directory
(``.versioned/{repo}/v_<ts>/...``), timestamped and NEVER reused by a
subsequent refresh (a new refresh always mints a brand-new ``v_<ts>``) --
the identical invariant the LOCAL, already-proven ``ChunkStoreThreadCache
._stale_prefixes`` set (rounds 1-4) already relies on for its own
permanent dedup-forever semantics. The same literal path becoming stale
"twice" with a different meaning cannot happen by construction.

``ChunkStoreCrossProcessPoller`` mirrors ``PayloadCache.start_background_
cleanup()``'s own thread-lifecycle idiom exactly (daemon thread,
``threading.Event`` stop flag, ``.start()``/``.stop()``) -- the
established pattern this codebase already uses for this class of
periodic background work. Polling runs on its OWN fixed-interval
background thread, INDEPENDENT of query traffic (unlike the local
per-thread sweep, which only fires on ``get_or_open()`` calls) -- a
fully idle worker still polls every cycle, and adds ZERO per-query cost
either way, since polling never happens inline on the query hot path.

Round-6 correction: an earlier draft of this note incorrectly claimed
this residual risk "mirrors round 4's accepted idle-thread risk" -- it
does not. There is NO cross-process self-healing mechanism here (no
per-poller cursor recovery against a shared offset, the way round 4's
in-process sweep recovers across a local trim). The actual residual
risk is narrower and different in shape: a burst of publishes trimming
an entry out of the (200-entry-capped) registry between two of a given
worker's poll ticks, or PayloadCache being transiently unreachable
across multiple consecutive polls. Either way, that ONE entry simply
falls back to exactly round 1-4's original per-process behavior --
bounded by the existing 32-entry-per-thread LRU cap -- not an unbounded
regression to the pre-#1775 leak shape.

``register_payload_cache()``/``get_registered_payload_cache()`` is a
bare pointer registration, NOT a second copy of PayloadCache's state --
it lets non-request-scoped background services (``GoldenRepoManager``,
``RefreshScheduler``, etc., reached via ``snapshot_cache_invalidation.
py``'s ``_evict_chunk_store_cache()``) reach the SAME single
``app.state.payload_cache`` instance without threading a new parameter
through 5 deep call sites. This mirrors the existing bare-singleton-
accessor pattern that exact call site already uses for the sibling
caches (``get_global_cache()`` for HNSW, ``get_global_chunk_store_
cache()`` for this cache's own per-process instance).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING, List, Optional, Protocol, Set

if TYPE_CHECKING:
    from code_indexer.server.cache.payload_cache import PayloadCache


class ChunkStoreCacheInvalidator(Protocol):
    """Minimal structural contract ChunkStoreCrossProcessPoller actually
    needs -- deliberately NOT the full ChunkStoreThreadCache API, so a
    lightweight test double can satisfy it without implementing the
    entire class (used by this module's own test suite to prove exact
    call counts, which the real cache's downstream eviction side
    effects cannot discriminate).
    """

    def invalidate_prefix(self, prefix: str) -> None: ...


logger = logging.getLogger(__name__)

#: Well-known single key holding the JSON-encoded, bounded list of
#: recently-registered stale prefixes. One key, not one-per-prefix,
#: because PayloadCache exposes no "list keys" capability.
_REGISTRY_KEY = "chunk_store_cache:stale_prefixes_registry_v1"

#: Bound on the registry list. Deliberately much smaller than the local
#: per-process _MAX_TRACKED_STALE_PREFIXES (1,000,000): this blob is
#: JSON-decoded by EVERY process/node on every poll (cost scales with
#: list size x worker/node count x poll frequency), AND every entry in
#: it costs a sequential retrieve()/page in publish_stale_prefix()'s own
#: read-modify-write, widening its race window (round-6 code review:
#: Claude quantified ~1% of concurrent swaps racing at the OLD 2,000
#: cap). 200 cuts both costs by ~10x while staying comfortably generous
#: -- the registry only ever needs to cover ONE poll interval's worth of
#: publishes, since every live worker drains it within ~30s.
_MAX_REGISTRY_ENTRIES = 200

#: Sanity ceiling on pagination pages read back from PayloadCache.retrieve()
#: -- guards against an unbounded loop if a backend ever misreports
#: has_more=True forever. At _MAX_REGISTRY_ENTRIES entries and a
#: reasonable per-entry path length, the real content fits in far fewer
#: pages than this; hitting the cap is itself a signal something is wrong.
_MAX_REGISTRY_PAGES = 10_000

#: Bound on ChunkStoreCrossProcessPoller's own "already applied" set --
#: generous headroom over the shared registry's own cap so a poller
#: rarely needs to re-apply (harmless, idempotent) an entry it forgot.
_MAX_KNOWN_PREFIXES = 2 * _MAX_REGISTRY_ENTRIES

#: Default poll cadence for ChunkStoreCrossProcessPoller -- see module
#: docstring for why this is comfortably under PayloadCache's default
#: 900s TTL.
_DEFAULT_POLL_INTERVAL_SECONDS = 30.0

#: Default timeout for stop()'s join() on the poll thread. Generous --
#: a single poll_once() call is normally fast (one PayloadCache read
#: plus a handful of invalidate_prefix() calls), so this only matters
#: if the thread is genuinely stuck; round-6 code review (Codex): the
#: caller MUST know whether the thread is confirmed stopped before
#: proceeding to close PayloadCache (a use-after-close race otherwise).
_DEFAULT_STOP_JOIN_TIMEOUT_SECONDS = 10.0


def _trim_floor_safe(entries: List[str], max_entries: int) -> List[str]:
    """Trim ``entries`` to at most ``max_entries``, retaining the NEWEST
    half on overflow, but NEVER trimming the newest entry away (mirrors
    ``ChunkStoreThreadCache.invalidate_prefix()``'s round-4 floor-bug
    fix, applied here for the same reason: a tiny cap must not silently
    disable publishing entirely).
    """
    if len(entries) <= max_entries:
        return entries
    trim_count = len(entries) - (max_entries // 2)
    trim_count = min(trim_count, len(entries) - 1)
    return entries[trim_count:]


def publish_stale_prefix(
    payload_cache: "PayloadCache",
    prefix: str,
    *,
    max_entries: int = _MAX_REGISTRY_ENTRIES,
) -> bool:
    """Publish ``prefix`` to the shared cross-process registry so every
    OTHER process/node's :class:`ChunkStoreCrossProcessPoller` can
    discover and apply it. Best-effort, non-fatal -- a PayloadCache
    failure here must never block the caller's already-completed LOCAL
    invalidation (``ChunkStoreThreadCache.invalidate_prefix()``, called
    separately and unconditionally by the caller).

    Returns:
        True if the prefix is now VERIFIED to be in the registry
        (either just written and read back, or already present from an
        earlier publish). False if the write genuinely failed -- round-6
        code review (Codex): callers MUST check this rather than
        assuming success, since a caller that always logs "Published..."
        regardless of the outcome silently lies about whether
        propagation actually happened. Round-7 correction (Codex):
        ``PayloadCache.store_with_key()`` returns ``None`` on BOTH
        success and failure (the PostgreSQL backend's ``store()``
        catches all exceptions internally and returns ``None`` either
        way) -- "did the call complete without raising" alone cannot
        distinguish success from failure, so this function reads the
        registry back after writing and only reports success if the
        prefix is actually present in that fresh read.

    Raises:
        ValueError: if ``max_entries < 1`` (a caller programming error,
            not a runtime/PayloadCache failure -- fails loud, unlike the
            PayloadCache-failure paths below).
    """
    if max_entries < 1:
        raise ValueError(f"max_entries must be >= 1, got {max_entries}")
    try:
        current = read_stale_prefixes_registry(payload_cache)
        if prefix in current:
            return True
        updated = _trim_floor_safe(current + [prefix], max_entries)
        payload_cache.store_with_key(_REGISTRY_KEY, json.dumps(updated))
        # Round-7 verify-on-write: store_with_key() alone cannot signal
        # a genuine failure (see docstring) -- read back to confirm the
        # write actually persisted.
        verification = read_stale_prefixes_registry(payload_cache)
        return prefix in verification
    except Exception as exc:
        logger.warning(
            "Failed to publish stale prefix %s to cross-process registry: %s",
            prefix,
            exc,
        )
        return False


#: Round-7 code review (both Claude and Codex): tracks whether the MOST
#: RECENT genuine read failure (page-ceiling exceeded, corrupt content,
#: or a raised exception -- NOT a normal CacheNotFoundError) is part of
#: an ONGOING streak, so read_stale_prefixes_registry() can log at
#: WARNING only on a streak's start/recovery (a real, actionable signal
#: for a sustained outage) and DEBUG for the repeats in between (this
#: function runs every ~30s on the background poller, so a WARNING per
#: tick would spam logs and risk this project's log-audit gate).
_registry_read_failing = False
_registry_read_failing_lock = threading.Lock()


def reset_registry_read_failure_state() -> None:
    """Reset the tracked read-failure-streak state (for testing)."""
    global _registry_read_failing
    with _registry_read_failing_lock:
        _registry_read_failing = False


def _record_registry_read_success() -> None:
    global _registry_read_failing
    with _registry_read_failing_lock:
        was_failing = _registry_read_failing
        _registry_read_failing = False
    if was_failing:
        logger.warning(
            "Cross-process stale-prefix registry reads have RECOVERED "
            "after a failure streak."
        )


def _record_registry_read_failure(exc: Exception) -> None:
    global _registry_read_failing
    with _registry_read_failing_lock:
        already_failing = _registry_read_failing
        _registry_read_failing = True
    if already_failing:
        logger.debug("Failed to read cross-process stale-prefix registry: %s", exc)
    else:
        logger.warning(
            "Cross-process stale-prefix registry reads have STARTED FAILING: %s",
            exc,
        )


#: Round-8 code review (Claude): mirrors the read-side tracker above,
#: for the WRITE side -- publish_stale_prefix() can return False (a
#: genuine write-verification failure) with NO exception raised, and
#: the caller previously had no `else` branch at all, producing ZERO
#: log output on a real, sustained write-side outage. Public (unlike
#: the read-side helpers) because the caller lives in a different
#: module (snapshot_cache_invalidation.py).
_registry_publish_failing = False
_registry_publish_failing_lock = threading.Lock()


def reset_registry_publish_failure_state() -> None:
    """Reset the tracked publish-failure-streak state (for testing)."""
    global _registry_publish_failing
    with _registry_publish_failing_lock:
        _registry_publish_failing = False


def record_registry_publish_success() -> None:
    """Call after a successful publish_stale_prefix() -- resets the
    failure streak and logs a recovery WARNING if one was active.
    """
    global _registry_publish_failing
    with _registry_publish_failing_lock:
        was_failing = _registry_publish_failing
        _registry_publish_failing = False
    if was_failing:
        logger.warning(
            "Cross-process stale-prefix registry publishes have "
            "RECOVERED after a failure streak."
        )


def record_registry_publish_failure(prefix: str) -> None:
    """Call after publish_stale_prefix() returns False -- WARNING only
    on the streak's start, DEBUG for repeats.
    """
    global _registry_publish_failing
    with _registry_publish_failing_lock:
        already_failing = _registry_publish_failing
        _registry_publish_failing = True
    if already_failing:
        logger.debug(
            "Failed to publish stale prefix %s to cross-process registry.",
            prefix,
        )
    else:
        logger.warning(
            "Cross-process stale-prefix registry publishes have "
            "STARTED FAILING: write for prefix %s did not verify.",
            prefix,
        )


def read_stale_prefixes_registry(payload_cache: "PayloadCache") -> List[str]:
    """Read the full shared registry (handling PayloadCache's own
    pagination across all pages), returning ``[]`` if the key doesn't
    exist yet (normal -- nothing has ever been published, NOT a
    failure) or a genuine read/parse failure occurred (state-transition
    logged -- see ``_record_registry_read_failure()``).
    """
    from code_indexer.server.cache.payload_cache import CacheNotFoundError

    try:
        content_parts = []
        page = 0
        while True:
            if page >= _MAX_REGISTRY_PAGES:
                _record_registry_read_failure(
                    RuntimeError(
                        f"exceeded the {_MAX_REGISTRY_PAGES}-page sanity "
                        "ceiling while paginating"
                    )
                )
                return []
            result = payload_cache.retrieve(_REGISTRY_KEY, page=page)
            content_parts.append(result.content)
            if not result.has_more:
                break
            page += 1
        decoded = json.loads("".join(content_parts))
        if not isinstance(decoded, list):
            _record_registry_read_failure(
                TypeError(f"registry content is not a JSON list: {type(decoded)}")
            )
            return []
        _record_registry_read_success()
        return [str(item) for item in decoded]
    except CacheNotFoundError:
        _record_registry_read_success()
        return []
    except Exception as exc:
        _record_registry_read_failure(exc)
        return []


class ChunkStoreCrossProcessPoller:
    """Background-thread poller that discovers stale prefixes published
    by OTHER processes/nodes and feeds them into THIS process's own
    ``ChunkStoreThreadCache.invalidate_prefix()`` -- the same,
    already-proven per-process eviction pipeline rounds 1-4 built and
    tested. See module docstring for the thread-lifecycle idiom this
    mirrors and the cadence rationale.
    """

    def __init__(
        self,
        chunk_store_cache: ChunkStoreCacheInvalidator,
        payload_cache: "PayloadCache",
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        stop_join_timeout_seconds: float = _DEFAULT_STOP_JOIN_TIMEOUT_SECONDS,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError(
                f"poll_interval_seconds must be > 0, got {poll_interval_seconds}"
            )
        if stop_join_timeout_seconds <= 0:
            raise ValueError(
                f"stop_join_timeout_seconds must be > 0, got {stop_join_timeout_seconds}"
            )
        self._chunk_store_cache = chunk_store_cache
        self._payload_cache = payload_cache
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_join_timeout_seconds = stop_join_timeout_seconds
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # Bounded (see _MAX_KNOWN_PREFIXES): an ordered list backs the
        # floor-safe trim, a set backs O(1) membership checks.
        self._known_prefixes_ordered: List[str] = []
        self._known_prefixes: Set[str] = set()
        self._known_lock = threading.Lock()

    def poll_once(self) -> int:
        """Apply any newly-discovered prefixes to THIS process's own
        cache. Returns the number newly applied (test observability).
        Non-fatal on read failure -- ``read_stale_prefixes_registry()``
        already degrades to ``[]`` and logs.
        """
        registry = read_stale_prefixes_registry(self._payload_cache)
        with self._known_lock:
            new_prefixes = [p for p in registry if p not in self._known_prefixes]
        for prefix in new_prefixes:
            self._chunk_store_cache.invalidate_prefix(prefix)
            with self._known_lock:
                self._known_prefixes.add(prefix)
                self._known_prefixes_ordered.append(prefix)
                trimmed = _trim_floor_safe(
                    self._known_prefixes_ordered, _MAX_KNOWN_PREFIXES
                )
                if len(trimmed) != len(self._known_prefixes_ordered):
                    dropped = self._known_prefixes_ordered[: -len(trimmed) or None]
                    for stale in dropped:
                        self._known_prefixes.discard(stale)
                    self._known_prefixes_ordered = trimmed
        return len(new_prefixes)

    def start(self) -> None:
        """Start the background poll thread as a daemon (mirrors
        ``PayloadCache.start_background_cleanup()``). Idempotent.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="ChunkStoreCrossProcessPoller",
        )
        self._thread.start()

    def stop(self) -> bool:
        """Stop the background poll thread (mirrors ``PayloadCache.
        stop_background_cleanup()``).

        Returns:
            True if the thread is confirmed stopped (or was never
            started). False if it did NOT exit within
            ``stop_join_timeout_seconds`` -- round-6 code review
            (Codex): the caller MUST check this before proceeding to
            close PayloadCache, since a still-running poller reading
            from an already-closed PayloadCache is a genuine
            use-after-close race, not a cosmetic issue.
        """
        self._stop_event.set()
        if self._thread is None:
            return True
        self._thread.join(timeout=self._stop_join_timeout_seconds)
        if self._thread.is_alive():
            logger.warning(
                "ChunkStoreCrossProcessPoller thread did not stop within "
                "%.1fs -- it may still be running against a closed "
                "PayloadCache.",
                self._stop_join_timeout_seconds,
            )
            return False
        return True

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(self._poll_interval_seconds):
            try:
                self.poll_once()
            except Exception as exc:
                logger.warning("ChunkStoreCrossProcessPoller poll failed: %s", exc)


_registered_payload_cache: Optional["PayloadCache"] = None
_registered_payload_cache_lock = threading.Lock()


def register_payload_cache(payload_cache: "PayloadCache") -> None:
    """Register THIS process's ``PayloadCache`` instance for cross-
    process stale-prefix publishing. Called ONCE from server startup
    (``lifespan.py``) after ``app.state.payload_cache`` is constructed --
    see module docstring for why this is not a state duplication.
    """
    global _registered_payload_cache
    with _registered_payload_cache_lock:
        _registered_payload_cache = payload_cache


def get_registered_payload_cache() -> Optional["PayloadCache"]:
    """Return the registered ``PayloadCache``, or ``None`` if never
    registered (e.g. CLI/solo mode, or server startup hasn't reached
    that point yet). Callers MUST treat ``None`` as "cross-process
    publish unavailable, degrade to local-only" -- never raise.
    """
    with _registered_payload_cache_lock:
        return _registered_payload_cache


def reset_registered_payload_cache() -> None:
    """Reset the registration (for testing)."""
    global _registered_payload_cache
    with _registered_payload_cache_lock:
        _registered_payload_cache = None
