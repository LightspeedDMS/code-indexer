"""Shared governed embedding call helper (Bug #1078).

Consolidates the duplicated
    execute_with_backoff(lambda: governor.execute(budget, lambda: provider.get_embedding(...)))
pattern used by all 4 query-embedding serving sites:
  - search_service.py  (non-FilesystemVectorStore path)
  - handlers/search.py (_compute_memory_query_vector)
  - services/temporal/temporal_search_service.py
  - storage/filesystem_vector_store.py (generate_embedding inner fn)

Reranking (reranking.py _attempt_provider_rerank) is intentionally NOT here —
it calls client.rerank(), a different operation on a different client type.

The test at tests/integration/test_provider_governor_real_concurrency_1078.py
uses this function directly to exercise the real Voyage HTTP path under a 20-
thread simultaneous burst and assert the governor caps in-flight to K=8.

Story #1105: coalesced_query_embedding now wraps a QueryEmbeddingCache (when
wired by lifespan) to short-circuit provider calls on cache HITs (on-mode) or
collect shadow measurements without altering the live path (shadow-mode).
"""

import logging
import random
import struct
from typing import Any, Callable, Dict, List, Optional, cast

from code_indexer.server.services.coalescer_registry import get_coalescer_registry
from code_indexer.server.services.config_service import get_config_service

logger = logging.getLogger(__name__)

# Seconds to wait for a governor slot — shared across all 4 embedding sites.
# 30 s is well within the 60 s caller timeout and absorbs momentary bursts.
_GOVERNOR_ACQUIRE_TIMEOUT_SECS: float = 30.0

# ---------------------------------------------------------------------------
# Story #1105: process-level QueryEmbeddingCache accessor
# ---------------------------------------------------------------------------
# The cache is wired by lifespan startup (server mode only).  CLI / daemon
# paths never set it so the accessor returns None there — the same "absent =
# first-class documented branch" pattern used by get_coalescer_registry().

_query_embedding_cache: Any = None


def get_query_embedding_cache() -> Any:
    """Return the process-level QueryEmbeddingCache, or None (CLI / pre-lifespan).

    None is the CLI/solo path — coalesced_query_embedding treats it as "no
    cache, delegate to _compute_live" (Messi #2 anti-fallback: explicit branch,
    not a silent default).
    """
    return _query_embedding_cache


def set_query_embedding_cache(cache: Any) -> None:
    """Install the process-level cache (called once in lifespan startup)."""
    global _query_embedding_cache
    _query_embedding_cache = cache


def clear_query_embedding_cache() -> None:
    """Clear the process-level cache (lifespan shutdown / test isolation)."""
    global _query_embedding_cache
    _query_embedding_cache = None


# ---------------------------------------------------------------------------
# Story #1109 (S5): process-level QueryEmbeddingCacheMetrics accessor
# ---------------------------------------------------------------------------
# The metrics object is wired by lifespan startup (server mode + telemetry
# enabled only).  CLI / daemon paths never set it so the accessor returns None
# there — same "absent = explicit documented branch" pattern as the cache
# accessor above.

_query_embedding_cache_metrics: Any = None


def get_query_embedding_cache_metrics() -> Any:
    """Return the process-level QueryEmbeddingCacheMetrics, or None.

    None on CLI / pre-lifespan / telemetry-disabled — coalesced_query_embedding
    passes None to _serve_with_cache which is a documented no-op branch.
    """
    return _query_embedding_cache_metrics


def set_query_embedding_cache_metrics(metrics: Any) -> None:
    """Install the process-level cache metrics (called once in lifespan startup)."""
    global _query_embedding_cache_metrics
    _query_embedding_cache_metrics = metrics


def clear_query_embedding_cache_metrics() -> None:
    """Clear the process-level cache metrics (lifespan shutdown / test isolation)."""
    global _query_embedding_cache_metrics
    _query_embedding_cache_metrics = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_embedding_budget(provider: Any) -> str:
    """Map an embedding provider instance to its governor EMBED-lane key.

    Story #1079 Phase B+C: the governor is now 4-lane. Embedding calls route to
    the ``:embed`` lane of the provider:
      - "cohere:embed" for CohereEmbeddingProvider instances,
      - "voyage:embed" for everything else (VoyageAIClient and any future
        Voyage-backed providers).
    Rerank calls use the ``:rerank`` lanes (see mcp/reranking.py).
    """
    from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

    return (
        "cohere:embed"
        if isinstance(provider, CohereEmbeddingProvider)
        else "voyage:embed"
    )


def governed_query_embedding(
    provider: Any,
    text: str,
    *,
    embedding_purpose: Optional[str] = "query",
    acquire_timeout: float = _GOVERNOR_ACQUIRE_TIMEOUT_SECS,
) -> List[float]:
    """Gate one query-embedding HTTP call through the concurrency governor.

    Wraps the canonical serving-path call:
        execute_with_backoff(
            lambda: governor.execute(
                budget,
                lambda: provider.get_embedding(text, embedding_purpose=...),
                acquire_timeout=...,
            )
        )

    The governor semaphore caps concurrent in-flight calls at K per budget.
    The execute_with_backoff wrapper handles HTTP 429 retries OUTSIDE the
    held slot so other callers can use the freed slot during backoff sleep.

    Args:
        provider: Any EmbeddingProvider (VoyageAIClient, CohereEmbeddingProvider, …).
        text: Query text to embed.
        embedding_purpose: Passed through to provider.get_embedding().
            Default "query" for all serving-path callers.
        acquire_timeout: Seconds to wait for a governor slot.

    Returns:
        List[float] embedding vector.

    Raises:
        GovernorBusyError: acquire_timeout elapsed with no slot available.
        ProviderSinbinnedError: provider budget is sinbinned.
        ProviderRateLimitedError: all retry attempts exhausted (429).
        Any other exception raised by provider.get_embedding().
    """
    from code_indexer.server.services.provider_concurrency_governor import (
        ProviderConcurrencyGovernor,
    )
    from code_indexer.services.provider_backoff import execute_with_backoff

    budget = _get_embedding_budget(provider)
    governor = ProviderConcurrencyGovernor.get_instance()

    return execute_with_backoff(  # type: ignore[no-any-return]
        lambda: governor.execute(
            budget,
            lambda: provider.get_embedding(text, embedding_purpose=embedding_purpose),
            acquire_timeout=acquire_timeout,
        )
    )


def _compute_live(
    provider: Any,
    text: str,
    embedding_purpose: Optional[str] = "query",
    acquire_timeout: float = _GOVERNOR_ACQUIRE_TIMEOUT_SECS,
) -> List[float]:
    """Execute the live embedding path (coalescer or direct governed call).

    This is the verbatim body that ``coalesced_query_embedding`` previously
    contained: registry-absent / kill-switch / lane-absent / coalesced branches
    are all here.  Extracted so the cache wrap in ``coalesced_query_embedding``
    can call it explicitly and tests can spy / stub it.

    Args:
        provider: Any EmbeddingProvider.
        text: Query text to embed.
        embedding_purpose: Passed through to provider / coalescer.
        acquire_timeout: Governor slot wait timeout.

    Returns:
        List[float] embedding vector from the live provider path.
    """

    def _direct() -> List[float]:
        return governed_query_embedding(
            provider,
            text,
            embedding_purpose=embedding_purpose,
            acquire_timeout=acquire_timeout,
        )

    registry = get_coalescer_registry()
    if registry is None:
        logger.debug("coalesced_query_embedding: no registry -> direct governed call")
        return _direct()

    try:
        coalesce_enabled = bool(get_config_service().get_config().coalesce_enabled)
    except Exception as exc:  # noqa: BLE001 — config read is best-effort here
        logger.warning(
            "coalesced_query_embedding: could not read coalesce_enabled (%s); "
            "delegating to direct governed call",
            exc,
        )
        coalesce_enabled = False

    if not coalesce_enabled:
        logger.debug("coalesced_query_embedding: coalesce disabled -> direct call")
        return _direct()

    lane = _get_embedding_budget(provider)
    coalescer = registry.get(lane)
    if coalescer is None:
        logger.debug(
            "coalesced_query_embedding: no coalescer for lane=%s -> direct call",
            lane,
        )
        return _direct()

    logger.debug("coalesced_query_embedding: coalescing on lane=%s", lane)
    return cast(
        List[float],
        coalescer.submit(text, embedding_purpose=embedding_purpose or "query"),
    )


def _bytes_to_floats(blob: bytes) -> List[float]:
    """Decode float32 LE bytes back to a Python float list.

    Uses the same encoding that ``QueryEmbeddingCache.record_miss_or_shadow``
    writes (numpy ``<f4`` / struct ``<{n}f``).
    """
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _audit_sample_rate_for(provider_name: str) -> float:
    """Return the per-provider audit sample rate, clamped to [0.0, 1.0].

    Story #1110 (S6): live read of the QueryEmbeddingCacheConfig fields:
      - "cohere" -> query_embedding_cache_cohere_audit_sample_rate
      - everything else -> query_embedding_cache_voyage_audit_sample_rate

    Fail-open: any config/attr error returns 0.0 (audit disabled).

    Args:
        provider_name: e.g. "voyage-ai" or "cohere".

    Returns:
        float in [0.0, 1.0].
    """
    try:
        qec_cfg = get_config_service().get_config().query_embedding_cache_config
        if qec_cfg is None:
            return 0.0
        if provider_name == "cohere":
            rate = float(qec_cfg.query_embedding_cache_cohere_audit_sample_rate)
        else:
            rate = float(qec_cfg.query_embedding_cache_voyage_audit_sample_rate)
        return max(0.0, min(1.0, rate))
    except Exception as exc:  # noqa: BLE001 — telemetry must never break query path
        logger.debug(
            "_audit_sample_rate_for(%s): config read failed (%s) -> 0.0",
            provider_name,
            exc,
        )
        return 0.0


def _serve_with_cache(
    cache: Any,
    provider_name: str,
    cache_key: str,
    qualifier: Any,
    live_fn: Callable[[], List[float]],
    *,
    metrics: Optional[Any] = None,
    audit_ctx: Optional[Dict[str, Any]] = None,
) -> List[float]:
    """Apply the Story #1105 cache policy for one embedding request.

    Called only when the cache is enabled for *provider_name* and mode != "off".

    Modes:
      on     — HIT skips live_fn entirely; MISS calls live_fn then upserts.
      shadow — ALWAYS calls live_fn; HIT touches last_used; MISS upserts.
               Returns the LIVE vector in both shadow sub-cases.

    All cache operations are fail-open (the service swallows DB errors).

    Story #1109 (S5): accepts optional `metrics` (QueryEmbeddingCacheMetrics).
      - hit/miss counters are recorded with {"mode": <mode>, "provider": <provider>}.
      - shadow_cosine is recorded ONLY in shadow mode when a prior cached blob exists.
      - provider error in shadow: miss metric recorded, no cosine, error re-raised.
      - metrics=None (default) preserves all existing caller behaviour unchanged.

    Story #1110 (S6): accepts optional `audit_ctx` (Dict[str, Any]).
      - On a cache HIT (on-mode or shadow-mode), if audit_ctx is not None and the
        per-provider audit_sample_rate > 0.0 and random.random() < rate, the dict
        is populated with audit sampling data (fail-open: errors never propagate).
      - on-mode HIT:   {"sampled": True, "mode": "on", "provider": ...,
                         "cached_blob": <bytes>}  (NO live_vec; Chunk B re-embeds)
      - shadow HIT:    {"sampled": True, "mode": "shadow", "provider": ...,
                         "cached_blob": <bytes>, "live_vec": <list>}
      - Misses and non-sampled HITs leave audit_ctx untouched (empty dict).
      - audit_ctx=None (default) is a documented no-op; all existing callers unaffected.

    Args:
        cache: QueryEmbeddingCache instance.
        provider_name: e.g. "voyage-ai".
        cache_key: SHA-256 hex from build_key(text).
        qualifier: CacheQualifier named-tuple.
        live_fn: Zero-arg callable that produces the live embedding vector.
        metrics: Optional QueryEmbeddingCacheMetrics; no-op when None.
        audit_ctx: Optional mutable dict; populated on sampled HITs. Default None.

    Returns:
        List[float] — cached vector (on-mode HIT) or live vector (all other paths).
    """
    mode: str = cache.mode_for(provider_name)

    if mode == "on":
        cached_blob: Optional[bytes] = cache.lookup(cache_key, qualifier)
        if cached_blob is not None:
            logger.debug(
                "coalesced_query_embedding: cache HIT (mode=on, provider=%s)",
                provider_name,
            )
            cache.record_hit(cache_key, qualifier)
            if metrics is not None:
                metrics.record_hit(mode=mode, provider=provider_name)
            # Story #1110 (S6): populate audit_ctx on sampled on-mode HITs.
            if audit_ctx is not None:
                try:
                    rate = _audit_sample_rate_for(provider_name)
                    if rate > 0.0 and random.random() < rate:
                        audit_ctx["sampled"] = True
                        audit_ctx["mode"] = mode
                        audit_ctx["provider"] = provider_name
                        audit_ctx["cached_blob"] = cached_blob
                        # No live_vec: Chunk B re-embeds from the cache
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "_serve_with_cache: audit_ctx population failed (on-mode): %s",
                        exc,
                    )
            return _bytes_to_floats(cached_blob)
        # MISS
        logger.debug(
            "coalesced_query_embedding: cache MISS (mode=on, provider=%s)",
            provider_name,
        )
        live_vec: List[float] = live_fn()
        cache.record_miss_or_shadow(cache_key, qualifier, live_vec)
        if metrics is not None:
            metrics.record_miss(mode=mode, provider=provider_name)
        return live_vec

    # shadow (or any unrecognised mode treated as shadow per cache.mode_for default)
    # AC5: wrap live_fn() so provider errors record a miss metric then re-raise.
    try:
        live_vec = live_fn()
    except Exception:
        if metrics is not None:
            metrics.record_miss(mode=mode, provider=provider_name)
        raise

    shadow_blob: Optional[bytes] = cache.lookup(cache_key, qualifier)
    if shadow_blob is not None:
        logger.debug(
            "coalesced_query_embedding: shadow HIT (provider=%s) -> touch_last_used",
            provider_name,
        )
        cache.record_hit(cache_key, qualifier)
        if metrics is not None:
            metrics.record_hit(mode=mode, provider=provider_name)
            # AC2: record cosine ONLY in shadow + prior-cached
            metrics.record_shadow_cosine(cached_blob=shadow_blob, live_vec=live_vec)
        # Story #1110 (S6): populate audit_ctx on sampled shadow HITs.
        if audit_ctx is not None:
            try:
                rate = _audit_sample_rate_for(provider_name)
                if rate > 0.0 and random.random() < rate:
                    audit_ctx["sampled"] = True
                    audit_ctx["mode"] = mode
                    audit_ctx["provider"] = provider_name
                    audit_ctx["cached_blob"] = shadow_blob
                    audit_ctx["live_vec"] = live_vec
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "_serve_with_cache: audit_ctx population failed (shadow-mode): %s",
                    exc,
                )
    else:
        logger.debug(
            "coalesced_query_embedding: shadow MISS (provider=%s) -> record_miss",
            provider_name,
        )
        cache.record_miss_or_shadow(cache_key, qualifier, live_vec)
        if metrics is not None:
            metrics.record_miss(mode=mode, provider=provider_name)
    return live_vec


def coalesced_query_embedding(
    provider: Any,
    text: str,
    *,
    embedding_purpose: Optional[str] = "query",
    acquire_timeout: float = _GOVERNOR_ACQUIRE_TIMEOUT_SECS,
    no_embedding_cache_shortcut: bool = False,
    audit_ctx: Optional[Dict[str, Any]] = None,
) -> List[float]:
    """Server-gated entry point for a single query embedding (Story #1079 Phase E).

    Story #1105 adds a QueryEmbeddingCache layer as the OUTERMOST layer.  The
    cache intercepts before the coalescer/governor so cache HITs avoid any
    concurrency-governor overhead, and the cache works regardless of whether
    the coalescer kill-switch is on or off:

    - Cache None / provider not enabled / mode=="off" -> _compute_live() as before.
    - Mode "on"  + HIT  -> decode cached bytes; skip _compute_live entirely.
    - Mode "on"  + MISS -> _compute_live() (handles coalescer/direct); record_miss;
                           return live vec.
    - Mode "shadow" + HIT  -> _compute_live() (always); touch_last_used; return LIVE.
    - Mode "shadow" + MISS -> _compute_live(); record_miss; return live vec.

    Story #1108 (S4): no_embedding_cache_shortcut bypasses the cache READ when True.
    The write (record_miss_or_shadow) still fires so future requests can benefit.
    The mode==off / not-enabled gates fire FIRST (no_embedding_cache_shortcut cannot
    re-enable a disabled cache).

    Story #1110 (S6): audit_ctx (optional mutable dict) is threaded into
    _serve_with_cache.  On a sampled cache HIT (per-provider audit_sample_rate),
    the dict is populated with {"sampled": True, "mode": ..., "provider": ...,
    "cached_blob": ...} (on-mode) or additionally "live_vec" (shadow-mode).
    Default None is a documented no-op; all existing callers unaffected.

    The 4 query sites call this (swapping only the function name). ALL gating
    lives here so call sites are identical on CLI and server.
    """

    def live() -> List[float]:
        return _compute_live(provider, text, embedding_purpose, acquire_timeout)

    cache = get_query_embedding_cache()
    if cache is None:
        return live()

    provider_name: str = provider.get_provider_name()

    if not cache.enabled_for(provider_name):
        logger.debug(
            "coalesced_query_embedding: cache disabled for %s -> live",
            provider_name,
        )
        return live()

    if cache.mode_for(provider_name) == "off":
        logger.debug(
            "coalesced_query_embedding: cache mode=off for %s -> live",
            provider_name,
        )
        return live()

    cache_key: str = cache.build_key_for_provider(text, provider_name)
    qualifier: Any = cache.qualifier(provider)

    # Story #1108 (S4): bypass cache READ when requested; still write on miss.
    if no_embedding_cache_shortcut:
        logger.debug(
            "coalesced_query_embedding: bypass=True for %s -> skip read, compute live",
            provider_name,
        )
        live_vec: List[float] = live()
        cache.record_miss_or_shadow(cache_key, qualifier, live_vec)
        return live_vec

    return _serve_with_cache(
        cache,
        provider_name,
        cache_key,
        qualifier,
        live,
        metrics=get_query_embedding_cache_metrics(),
        audit_ctx=audit_ctx,
    )
