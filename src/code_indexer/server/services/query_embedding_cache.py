"""Story #1105 / #1106 / #1149: QueryEmbeddingCache service — anchor-token embedding cache.

Provides:
- build_key(text, *, config_digest, anchor_tokens=2) -> Optional[str]
  (Story #1149) Returns a config-namespaced readable string of the form
  ``s:<config-digest>:<normalized-query>`` where normalized-query is the
  anchor-token-normalised form of *text* (case-preserved, anchor-token
  normalised, Story #1106).  Returns None when the normalized-query part
  exceeds the 256-char cap — the form is NEVER truncated.  The ``s:`` prefix
  makes new keys provably disjoint from legacy 64-hex SHA-256 keys so both
  keyspaces can coexist and legacy rows age out via LRU (passive reset).
  The ``config_digest`` is the SAME digest computed by the #1112 coalescer
  registry (provider + endpoint + model) so cache identity == coalescer
  identity: two endpoints produce two digests = two keyspaces, closing the
  endpoint cross-serve gap.
- CacheQualifier: named-tuple PK fields (provider, model, dimension)
- QueryEmbeddingCache: service wrapping a QueryEmbeddingCacheBackend with
  per-provider mode gating (off / shadow / on) and fail-open error handling.
  ``enabled_for()``, ``mode_for()``, and ``anchor_tokens_for()`` read LIVE from
  the config service on every call (mirror of ``coalesce_enabled`` in
  governed_call.py) so the master kill switch, per-provider mode, and
  anchor-depth take effect WITHOUT a restart.

Namespace-change observability (Story #1106):
  When the effective ``anchor_tokens`` for a provider changes at runtime, exactly
  ONE structured WARNING is emitted so operators understand the resulting
  hit-rate dip.  The process-local memo is per-provider so a change on one
  provider never suppresses a log for another.  Changing ``anchor_tokens``
  INTENTIONALLY fragments the keyspace: old rows keyed under the old normalisation
  no longer match new keys and age out via LRU.  Correctness is preserved —
  each row's key still correctly matches its own normalisation.  NOTE: a live
  model change is NOT tracked by this log because ``anchor_tokens_for()``
  receives only the provider-name string, not the provider object, so the model
  is genuinely unavailable at that call site.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, NamedTuple, Optional, Tuple, cast

from code_indexer.server.storage.protocols import QueryEmbeddingCacheBackend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Async touch-flush constants (Bug #1181 Perf Fix #2)
# ---------------------------------------------------------------------------

# Background flush interval: drain the coalescing buffer every 5 seconds.
_TOUCH_FLUSH_INTERVAL_SECONDS = 5.0

# Soft cap on the coalescing buffer (distinct keys).  Coalescing already limits
# growth to one entry per distinct (cache_key, provider, model, dimension) tuple
# per flush interval.  This cap guards against an extreme number of unique queries
# in a single interval — when reached, an early synchronous flush is triggered.
_TOUCH_BUFFER_MAX_SIZE = 2048

# ---------------------------------------------------------------------------
# Mode constants
# ---------------------------------------------------------------------------

_MODE_OFF = "off"
_MODE_SHADOW = "shadow"
_MODE_ON = "on"

_VALID_MODES = {_MODE_OFF, _MODE_SHADOW, _MODE_ON}

# ---------------------------------------------------------------------------
# Default anchor depth
# ---------------------------------------------------------------------------

_DEFAULT_ANCHOR_TOKENS = 2

# ---------------------------------------------------------------------------
# Key cap: the normalized-query part MUST NOT exceed this many characters.
# Measured BEFORE the 's:' prefix and config-digest are prepended.
# Over-cap -> build_key returns None (NEVER truncated).
# ---------------------------------------------------------------------------

_NORMALIZED_QUERY_CAP = 256

# ---------------------------------------------------------------------------
# Key building (Story #1149)
# ---------------------------------------------------------------------------


def build_key(
    text: str,
    anchor_tokens: int = _DEFAULT_ANCHOR_TOKENS,
    *,
    config_digest: str,
) -> Optional[str]:
    """Return a config-namespaced readable cache key, or None when over the cap.

    Key format (Story #1149): ``s:<config-digest>:<normalized-query>``

    The ``s:`` prefix is provably disjoint from legacy 64-hex SHA-256 keys
    (which never start with 's:'), enabling a passive LRU reset: old keys age
    out via prune_to_max without any active clear() or destructive DDL.

    Normalisation algorithm (same as Story #1106):
    1. Tokenise: ``text.split()`` — collapses whitespace runs, strips leading/
       trailing whitespace, preserves punctuation (attached to token).
    2. Take the first ``anchor_tokens`` tokens in ORIGINAL order (anchor prefix).
    3. Sort REMAINING tokens ALPHABETICALLY (lexicographic on raw Unicode code
       points — NEVER lowercased). Duplicates kept as sorted multiset.
    4. Normalised string = anchor prefix + sorted tail, joined by a single space.
    5. If len(normalised) > 256 -> return None (NEVER truncate).
    6. Else -> return ``f"s:{config_digest}:{normalised}"``.

    Cap behaviour:
    - The cap (256 chars) is measured on the *normalised-query part ONLY*,
      before the prefix and digest are prepended.
    - An over-cap result MUST return None — the form is NEVER truncated.
    - Callers MUST treat None as a MISS and skip lookup and write.

    Case: NEVER lowercased at any step.

    Args:
        text: The raw query string (any length, including empty).
        anchor_tokens: Number of leading tokens to keep in original order.
            Remaining tokens are sorted alphabetically.  Negative values are
            treated as 0 (sort-all).  Default: 2.
        config_digest: The coalescer-registry digest for the provider config
            (provider + endpoint + model). MUST be the value from
            ``coalescer_registry._digest_for_provider(provider)`` — reused, not
            recomputed here. Keyword-only to prevent positional accidents.

    Returns:
        ``f"s:{config_digest}:{normalised}"`` when normalised <= 256 chars,
        or ``None`` when the normalised-query part exceeds the cap.
    """
    # Clamp to >= 0 defensively (negative is not meaningful)
    n = max(0, anchor_tokens)

    tokens = text.split()  # collapses whitespace runs; case preserved
    if n >= len(tokens):
        # All tokens in original order — exact-match semantics
        normalised = " ".join(tokens)
    else:
        anchor = tokens[:n]
        tail = sorted(tokens[n:])  # lexicographic (case-aware); duplicates kept
        normalised = " ".join(anchor + tail)

    # Cap check: measured on the normalized-query part ONLY.
    if len(normalised) > _NORMALIZED_QUERY_CAP:
        return None

    return f"s:{config_digest}:{normalised}"


# ---------------------------------------------------------------------------
# Qualifier (composite PK fields)
# ---------------------------------------------------------------------------


class CacheQualifier(NamedTuple):
    """Identifies the (provider, model, dimension) axis of a cached embedding.

    Used as a typed carrier between ``QueryEmbeddingCache.qualifier()`` and
    the ``lookup`` / ``record_miss_or_shadow`` / ``record_hit`` methods so
    callers never pass three separate positional strings.
    """

    provider: str
    model: str
    dimension: int


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class QueryEmbeddingCache:
    """Fail-open cache service for query embeddings.

    Constructor is called with explicit arguments.  Tests instantiate directly
    with known values.  Chunk C will wire live ServerConfig reads.

    Args:
        backend: A QueryEmbeddingCacheBackend implementation.
        enabled: Master kill switch.  When False, all operations are no-ops.
        voyage_mode: "off" | "shadow" | "on" for voyage-ai provider.
        cohere_mode: "off" | "shadow" | "on" for cohere provider.
        max_entries: Soft cap stored for Chunk C pruning integration.
    """

    def __init__(
        self,
        backend: QueryEmbeddingCacheBackend,
        *,
        enabled: bool = True,
        voyage_mode: str = _MODE_SHADOW,
        cohere_mode: str = _MODE_SHADOW,
        max_entries: int = 10000,
    ) -> None:
        self._backend = backend
        self._enabled = enabled
        self._modes = {
            "voyage-ai": voyage_mode if voyage_mode in _VALID_MODES else _MODE_SHADOW,
            "cohere": cohere_mode if cohere_mode in _VALID_MODES else _MODE_SHADOW,
        }
        self._max_entries = max_entries
        # Process-local memo of last-seen anchor_tokens per provider (Story #1106).
        # Used to detect changes and emit exactly ONE structured WARNING per change.
        self._last_anchor_tokens: Dict[str, int] = {}
        # Story #1109 (S5): cheap in-process memoized entry count for the OTEL
        # ObservableGauge callback.  Incremented on every successful upsert so
        # the gauge callback can call cached_total_entries() without a DB round-trip.
        # Clamped to the resolved cap via min(..., _resolve_max_entries()) at each
        # record_miss_or_shadow write — pins at the cap rather than growing without
        # bound, matching post-prune reality cheaply with no DB call on the exporter
        # thread.
        self._cached_total: int = 0
        # Bug #1181 Perf Fix #2: async/coalescing last_used touch buffer.
        # Keys: (cache_key, provider, model, dimension); values: latest timestamp.
        # Guarded by _touch_buffer_lock for thread safety.
        self._touch_buffer: Dict[Tuple[str, str, str, int], float] = {}
        self._touch_buffer_lock = threading.Lock()
        self._touch_buffer_max_size: int = _TOUCH_BUFFER_MAX_SIZE
        # Background flush thread lifecycle (None until start() is called).
        self._stop_event = threading.Event()
        self._flush_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Key / qualifier helpers
    # ------------------------------------------------------------------

    @staticmethod
    def build_key(
        text: str,
        anchor_tokens: int = _DEFAULT_ANCHOR_TOKENS,
        *,
        config_digest: str,
    ) -> Optional[str]:
        """Delegate to module-level :func:`build_key`.

        Story #1149: returns the config-namespaced key
        ``f"s:{config_digest}:{normalised}"`` or None when the normalised
        query exceeds the 256-char cap.

        Args:
            text: The raw query string.
            anchor_tokens: Number of leading tokens to keep in original order.
                Remaining tokens are sorted alphabetically.  Default: 2.
            config_digest: Coalescer-registry digest for the provider config.
                Keyword-only to prevent positional accidents.

        Returns:
            ``f"s:{config_digest}:{normalised}"`` or None when over cap.
        """
        return build_key(text, anchor_tokens, config_digest=config_digest)

    def qualifier(self, provider: object) -> CacheQualifier:
        """Extract a :class:`CacheQualifier` from a provider object.

        The provider must expose:
        - ``get_provider_name() -> str``
        - ``get_current_model() -> str``
        - ``get_model_info() -> dict`` with key ``"dimensions"``

        Args:
            provider: An embedding-provider instance (duck-typed).

        Returns:
            CacheQualifier named tuple.
        """
        pname: str = provider.get_provider_name()  # type: ignore[attr-defined]
        model: str = provider.get_current_model()  # type: ignore[attr-defined]
        info: dict = provider.get_model_info()  # type: ignore[attr-defined]
        dimension: int = int(info["dimensions"])
        return CacheQualifier(provider=pname, model=model, dimension=dimension)

    # ------------------------------------------------------------------
    # Live config read helper
    # ------------------------------------------------------------------

    def _live_qec_cfg(self) -> Optional[object]:
        """Return QueryEmbeddingCacheConfig from live config service, or None.

        Fail-open: any exception returns None so callers fall back to the
        construction-time defaults stored in ``self._enabled`` / ``self._modes``.
        """
        try:
            from code_indexer.server.services.config_service import get_config_service

            cfg = get_config_service().get_config()
            return getattr(cfg, "query_embedding_cache_config", None)
        except Exception:  # noqa: BLE001 — config read is best-effort
            logger.debug(
                "query_embedding_cache: could not read live config; using construction defaults",
            )
            return None

    # ------------------------------------------------------------------
    # Mode gating
    # ------------------------------------------------------------------

    def enabled_for(self, provider_name: str) -> bool:
        """Return True iff master switch is ON and provider mode is not 'off'.

        Reads LIVE from ``get_config_service().get_config().query_embedding_cache_config``
        on every call (mirror of ``coalesce_enabled`` in governed_call.py).
        Falls back to the construction-time ``enabled`` arg when config unavailable.

        Args:
            provider_name: e.g. "voyage-ai" or "cohere".

        Returns:
            bool — False means caller should skip all cache interaction.
        """
        qec_cfg = self._live_qec_cfg()
        if qec_cfg is not None:
            enabled = bool(
                getattr(qec_cfg, "query_embedding_cache_enabled", self._enabled)
            )
        else:
            enabled = self._enabled
        if not enabled:
            return False
        return self._live_mode_for(provider_name, qec_cfg) != _MODE_OFF

    def mode_for(self, provider_name: str) -> str:
        """Return the effective mode string for *provider_name*.

        Reads LIVE from the config service on every call.

        Returns:
            One of "off", "shadow", "on".  Unknown providers default to "shadow".
        """
        return self._live_mode_for(provider_name, self._live_qec_cfg())

    def _live_mode_for(self, provider_name: str, qec_cfg: Optional[object]) -> str:
        """Extract mode for *provider_name* from *qec_cfg* (live) or fallback."""
        if qec_cfg is None:
            return self._modes.get(provider_name, _MODE_SHADOW)
        if provider_name == "voyage-ai":
            raw = getattr(
                qec_cfg,
                "query_embedding_cache_voyage_mode",
                self._modes.get("voyage-ai", _MODE_SHADOW),
            )
        elif provider_name == "cohere":
            raw = getattr(
                qec_cfg,
                "query_embedding_cache_cohere_mode",
                self._modes.get("cohere", _MODE_SHADOW),
            )
        else:
            raw = self._modes.get(provider_name, _MODE_SHADOW)
        mode = str(raw) if raw in _VALID_MODES else _MODE_SHADOW
        return mode

    # ------------------------------------------------------------------
    # Anchor-token dial (Story #1106)
    # ------------------------------------------------------------------

    def anchor_tokens_for(self, provider_name: str) -> int:
        """Return the effective ``anchor_tokens`` for *provider_name*.

        Reads LIVE from the config service on every call — a value change takes
        effect immediately without a restart.

        Per-provider config keys (tried in order):
        - ``query_embedding_cache_voyage_anchor_tokens`` (when provider == "voyage-ai")
        - ``query_embedding_cache_cohere_anchor_tokens`` (when provider == "cohere")
        - :data:`_DEFAULT_ANCHOR_TOKENS` (module-level constant, value 2)

        Namespace-change observability: when the resolved ``anchor_tokens`` value
        differs from the last-seen value for this provider, ONE structured WARNING
        is emitted so operators understand the resulting hit-rate dip.  Subsequent
        calls with the same new value do NOT re-log.  NOTE: a live model change is
        NOT tracked here because this method receives only the provider-name string,
        not the provider object — the model is genuinely unavailable at this call
        site.

        The keyspace fragmentation is INTENTIONAL: old rows keyed under the old
        normalisation no longer match new keys and age out via LRU.  Correctness
        is preserved — each row's key still correctly matches its own
        normalisation.

        Args:
            provider_name: e.g. "voyage-ai" or "cohere".

        Returns:
            int >= 0.  Negative config values are clamped to 0.
        """
        qec_cfg = self._live_qec_cfg()
        raw: int = _DEFAULT_ANCHOR_TOKENS
        if qec_cfg is not None:
            # Try per-provider key first
            if provider_name == "voyage-ai":
                per_provider = getattr(
                    qec_cfg,
                    "query_embedding_cache_voyage_anchor_tokens",
                    None,
                )
            elif provider_name == "cohere":
                per_provider = getattr(
                    qec_cfg,
                    "query_embedding_cache_cohere_anchor_tokens",
                    None,
                )
            else:
                per_provider = None

            if per_provider is not None:
                raw = int(per_provider)
            else:
                raw = _DEFAULT_ANCHOR_TOKENS

        effective = max(0, raw)  # clamp negative to sort-all

        # Namespace-change observability: emit ONE WARNING when value changes.
        last = self._last_anchor_tokens.get(provider_name)
        if last is not None and last != effective:
            logger.warning(
                "query_embedding_cache: anchor_tokens changed for provider '%s' "
                "(%d -> %d) — cache namespace fragmented; old keys age out via LRU; "
                "correctness preserved (each row matches its own normalisation).",
                provider_name,
                last,
                effective,
            )
        self._last_anchor_tokens[provider_name] = effective

        return effective

    def build_key_for_provider(
        self,
        text: str,
        provider_name: str,
        *,
        config_digest: str,
    ) -> Optional[str]:
        """Build a cache key using the LIVE ``anchor_tokens`` for *provider_name*.

        Story #1149: returns the config-namespaced key
        ``f"s:{config_digest}:{normalised}"`` or None when the normalised query
        exceeds the 256-char cap.

        Convenience method that combines :meth:`anchor_tokens_for` and
        :func:`build_key` in one call.  Used by the cache-wrap layer so the
        active anchor depth is always up-to-date.

        Args:
            text: The raw query string.
            provider_name: e.g. "voyage-ai" or "cohere".
            config_digest: Coalescer-registry digest for the provider config.
                Keyword-only to prevent positional accidents.

        Returns:
            ``f"s:{config_digest}:{normalised}"`` or None when over cap.
        """
        return build_key(
            text, self.anchor_tokens_for(provider_name), config_digest=config_digest
        )

    # ------------------------------------------------------------------
    # Cache operations — all fail-open
    # ------------------------------------------------------------------

    def lookup(
        self,
        cache_key: str,
        qualifier: CacheQualifier,
    ) -> Optional[bytes]:
        """Look up a cached embedding.  Fail-open on any backend error.

        Args:
            cache_key: String key of the form ``s:<config-digest>:<normalized-query>``
                as returned by :meth:`build_key` (Story #1149).
            qualifier: Provider / model / dimension tuple.

        Returns:
            Raw float32 LE bytes or None (miss OR backend error).
        """
        try:
            return cast(
                Optional[bytes],
                self._backend.lookup(
                    cache_key,
                    qualifier.provider,
                    qualifier.model,
                    qualifier.dimension,
                ),
            )
        except Exception:
            logger.warning(
                "query_embedding_cache: lookup failed (fail-open)",
                exc_info=True,
            )
            return None

    def _resolve_max_entries(self) -> int:
        """Return the effective LRU cap, applying the >=100 safe floor.

        Reads the LIVE ``query_embedding_cache_max_entries`` from the config
        service on every call (mirror of how ``mode_for``/``anchor_tokens_for``
        read live config via ``_live_qec_cfg()``).  Applies the safe floor:
        ``return max(configured, 100)`` so values < 100 are raised to 100.

        The >=100 floor is the SOLE location of this floor logic — it must NOT
        be duplicated inside the backend primitives (which are pure).

        Returns:
            int >= 100.
        """
        qec_cfg = self._live_qec_cfg()
        if qec_cfg is not None:
            raw = int(
                getattr(
                    qec_cfg,
                    "query_embedding_cache_max_entries",
                    self._max_entries,
                )
            )
        else:
            raw = self._max_entries
        return max(raw, 100)

    def record_miss_or_shadow(
        self,
        cache_key: str,
        qualifier: CacheQualifier,
        embedding: List[float],
    ) -> None:
        """UPSERT the embedding bytes into the backend, then enforce the LRU cap.

        Converts the float list to float32 little-endian bytes before writing.
        After a successful upsert, calls ``prune_to_max`` with the LIVE resolved
        cap so the cap is NOT orphan code.  Prune failure is fail-open: logs a
        WARNING but does NOT roll back the write and does NOT raise.

        Args:
            cache_key: String key of the form ``s:<config-digest>:<normalized-query>``
                as returned by :meth:`build_key` (Story #1149).
            qualifier: Provider / model / dimension tuple.
            embedding: List of floats (the live embedding result).
        """
        try:
            import numpy as np

            blob: bytes = np.asarray(embedding, dtype="<f4").tobytes()
            now = time.time()
            self._backend.upsert(
                cache_key,
                qualifier.provider,
                qualifier.model,
                qualifier.dimension,
                blob,
                now,
                now,
            )
            # Story #1109 (S5): update cheap memo after the write, CLAMPED to the
            # resolved cap so it matches post-prune reality (prune evicts down to the
            # cap; the real count never exceeds it, so the memo must not drift past it).
            self._cached_total = min(
                self._cached_total + 1, self._resolve_max_entries()
            )
        except Exception:
            logger.warning(
                "query_embedding_cache: upsert failed (fail-open)",
                exc_info=True,
            )
            return

        # Enforce the LRU cap on every miss-write.  Workload is ~500 searches/day
        # so COUNT+delete-excess is trivial.  Prune failure is fail-open.
        try:
            self._backend.prune_to_max(self._resolve_max_entries())
        except Exception:
            logger.warning(
                "query_embedding_cache: prune_to_max failed (fail-open)",
                exc_info=True,
            )

    def cached_total_entries(self) -> int:
        """Return the cheap memoized entry count — NO backend call.

        Story #1109 (S5): used as the OTEL ObservableGauge callback so the
        exporter thread never performs a blocking DB COUNT query.  The value is
        updated on every successful ``record_miss_or_shadow`` upsert and CLAMPED to
        the resolved cap, so it pins at the cap (matching post-prune reality) instead
        of drifting upward — a best-effort cheap count for observability.

        Returns:
            int — current memoized entry count (>= 0).
        """
        return self._cached_total

    def record_hit(
        self,
        cache_key: str,
        qualifier: CacheQualifier,
    ) -> None:
        """Buffer a last_used touch for an existing cache row.  Non-blocking, fail-open.

        Bug #1181 Perf Fix #2: the hot cache-hit path performs ZERO synchronous DB
        writes.  The touch is coalesced into an in-process dict keyed by
        ``(cache_key, provider, model, dimension)`` with the latest timestamp.
        A background thread (started by ``start()``) drains the buffer every
        ``_TOUCH_FLUSH_INTERVAL_SECONDS`` via ``touch_last_used_batch``.

        If the buffer reaches ``_touch_buffer_max_size`` distinct keys, an early
        synchronous flush is triggered inline to bound memory growth (Messi #14).
        The early flush is also fail-open: any backend error is logged at WARNING.

        Args:
            cache_key: String key of the form ``s:<config-digest>:<normalized-query>``
                as returned by :meth:`build_key` (Story #1149).
            qualifier: Provider / model / dimension tuple.
        """
        buf_key: Tuple[str, str, str, int] = (
            cache_key,
            qualifier.provider,
            qualifier.model,
            qualifier.dimension,
        )
        ts = time.time()
        with self._touch_buffer_lock:
            self._touch_buffer[buf_key] = ts
            # Early flush when the buffer hits the cap (Messi #14: bounded growth).
            if len(self._touch_buffer) >= self._touch_buffer_max_size:
                self._flush_touches_locked()

    # ------------------------------------------------------------------
    # Async touch-flush internals (Bug #1181 Perf Fix #2)
    # ------------------------------------------------------------------

    def _drain_buffer_locked(self) -> List[Tuple[str, str, str, int, float]]:
        """Snapshot and clear the touch buffer.  MUST be called while holding _touch_buffer_lock.

        Returns the snapshotted items (may be empty).  Does NOT write to the
        backend — callers on the periodic/final flush path must perform the
        backend write OUTSIDE the lock so concurrent record_hit calls are never
        blocked by DB I/O.
        """
        if not self._touch_buffer:
            return []
        items: List[Tuple[str, str, str, int, float]] = [
            (cache_key, provider, model, dimension, ts)
            for (
                cache_key,
                provider,
                model,
                dimension,
            ), ts in self._touch_buffer.items()
        ]
        self._touch_buffer.clear()
        return items

    def _flush_touches(self) -> None:
        """Drain the touch buffer and persist via touch_last_used_batch.  Fail-open.

        Called by the background thread every _TOUCH_FLUSH_INTERVAL_SECONDS, and
        synchronously by stop() for the final drain so no touches are lost.

        Lock discipline: snapshot+clear under the lock, then write outside it.
        This ensures concurrent record_hit calls on the hot path are never
        blocked by DB I/O (the exact fix for Bug #1181 code-review defect #9).
        """
        with self._touch_buffer_lock:
            items = self._drain_buffer_locked()
        # Lock is released here — backend write happens outside the lock.
        if not items:
            return
        try:
            self._backend.touch_last_used_batch(items)
        except Exception:
            logger.warning(
                "query_embedding_cache: touch_last_used_batch failed (fail-open, %d items)",
                len(items),
                exc_info=True,
            )

    def _flush_touches_locked(self) -> None:
        """Drain+write while already holding ``_touch_buffer_lock``.  Used by record_hit only.

        This is the inline early-flush safety-valve: called from record_hit when
        the buffer reaches its cap.  It intentionally holds the lock across the
        backend write because the overflow case is rare and bounded (Messi #14),
        and restructuring record_hit to release-then-reacquire would add complexity
        for a path that fires infrequently.  The periodic flush path (_flush_touches)
        uses _drain_buffer_locked + out-of-lock write instead.
        """
        items = self._drain_buffer_locked()
        if not items:
            return
        try:
            self._backend.touch_last_used_batch(items)
        except Exception:
            logger.warning(
                "query_embedding_cache: touch_last_used_batch failed (fail-open, %d items)",
                len(items),
                exc_info=True,
            )

    def _flush_loop(self) -> None:
        """Background thread body: flush every _TOUCH_FLUSH_INTERVAL_SECONDS."""
        while not self._stop_event.wait(timeout=_TOUCH_FLUSH_INTERVAL_SECONDS):
            try:
                self._flush_touches()
            except Exception:
                logger.warning(
                    "query_embedding_cache: flush loop error (fail-open)",
                    exc_info=True,
                )
        # Final drain on shutdown so no buffered touches are lost.
        try:
            self._flush_touches()
        except Exception:
            logger.warning(
                "query_embedding_cache: final flush error on stop (fail-open)",
                exc_info=True,
            )

    def start(self) -> None:
        """Start the background touch-flush thread.  Idempotent.

        Called by server lifespan startup after the cache is wired.
        Has no effect if the thread is already running.
        """
        if self._flush_thread is not None and self._flush_thread.is_alive():
            return
        self._stop_event.clear()
        self._flush_thread = threading.Thread(
            target=self._flush_loop,
            daemon=True,
            name="qec-touch-flusher",
        )
        self._flush_thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the flush thread to stop, perform a final drain, and join.

        Called by server lifespan shutdown.  Blocks until the thread exits or
        ``timeout`` seconds elapse.  The final drain inside ``_flush_loop``
        ensures no buffered touches are lost on graceful shutdown.

        Args:
            timeout: Maximum seconds to wait for the thread to join.
        """
        self._stop_event.set()
        if self._flush_thread is not None:
            self._flush_thread.join(timeout=timeout)
            if self._flush_thread.is_alive():
                logger.warning(
                    "query_embedding_cache: touch-flush thread did not stop within %.1fs",
                    timeout,
                )

    def total_entries(self) -> int:
        """Return total row count from the backend.

        Returns:
            int row count, or 0 on backend error.
        """
        try:
            return cast(int, self._backend.total_entries())
        except Exception:
            logger.warning(
                "query_embedding_cache: total_entries failed (fail-open)",
                exc_info=True,
            )
            return 0

    def clear_all(self) -> None:
        """Delete all rows from the persisted cache table and reset the in-process count memo.

        Story #1156 (AC3): truncates the query_embedding_cache table via the active
        backend (SQLite or PostgreSQL) and immediately resets _cached_total to 0 so
        the ObservableGauge and the Web UI count readout are accurate without a
        DB round-trip.  Fail-open: backend errors are logged as WARNING but not raised.

        Distinct from the in-memory coalescer/registry clear
        (governed_call.clear_query_embedding_cache) -- that clears the RAM-side
        coalescer state; this clears the persisted embedding table.
        """
        try:
            self._backend.clear_all()
        except Exception:
            logger.warning(
                "query_embedding_cache: clear_all failed (fail-open)",
                exc_info=True,
            )
        # Reset memo regardless of backend outcome so UI reflects the intent.
        self._cached_total = 0
