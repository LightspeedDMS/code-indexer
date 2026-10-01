"""Memory-retrieval query-vector helpers for search handlers.

Domain module for search handlers. Part of the handlers package
modularization (Story #496).

Issue #1935 Part: split out of the former flat search.py (2,498 lines)
into this package, one module per domain seam, each < 1,000 lines.
Pure move -- zero behaviour change.

NOTE: Functions in this module were extracted verbatim from _legacy.py
(via search.py). Pre-existing method lengths and duplication are
preserved intentionally to avoid behavioral changes during extraction.
Refactoring is tracked separately.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from code_indexer.server.auth.user_manager import User
from code_indexer.server.mcp.memory_retrieval_pipeline import (
    MemoryRetrievalPipeline,
    MemoryRetrievalPipelineConfig,
    _build_empty_nudge_entry,
    _hydrate_memory_bodies,
)
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.search_embed_event_emit import emit_embed_event
from code_indexer.server.services.search_event_context import _search_event_ctx

from .._utils import _coerce_int, _get_golden_repos_dir
from ._shared import _CIDX_META_DIR_NAME, _DEFAULT_SEARCH_LIMIT, _MEMORY_SEMANTIC_MODES

# Issue #1935: literal package name (not __name__) so every submodule's
# logger carries the identical name HEAD's single logger had -- the
# log-audit gate and admin_logs_query front door key off this exact name.
logger = logging.getLogger("code_indexer.server.mcp.handlers.search")


def _configured_embedding_timeout_seconds() -> int:
    """Return the Web-UI-configured embedding provider HTTP timeout
    (Issue #1398), replacing the previously hardcoded VoyageAIConfig
    default of 30s at the server-side query-embedding construction sites.

    Fails open to 30 (the pre-#1398 hardcoded default) if
    search_timeouts_config is unset or absent entirely -- ServerConfig
    .__post_init__ guarantees it is always set on a real ServerConfig, but
    getattr() is used defensively (matching reranking.py's
    _configured_reranker_timeout_seconds) since many existing unit tests
    stub get_config_service() with a minimal fake object that has no
    search_timeouts_config attribute at all.
    """
    cfg = get_config_service().get_config()
    search_timeouts = getattr(cfg, "search_timeouts_config", None)
    if search_timeouts is None:
        return 30
    return search_timeouts.embedding_provider_timeout_seconds  # type: ignore[no-any-return]


def _compute_memory_query_vector(
    query_text: str,
    no_embedding_cache_shortcut: bool = False,
) -> List[float]:
    """Compute a Voyage embedding for query_text using VoyageAIClient.

    Uses VoyageAIClient(VoyageAIConfig()) which picks up VOYAGE_API_KEY from
    the environment — the same provider and key used by code search internally.
    This is called once per request and the vector is shared with memory
    retrieval (GAP 1: zero duplicate Voyage API calls).

    Bug #1078: the HTTP call is gated through ProviderConcurrencyGovernor so
    memory-retrieval embeddings do not bypass the per-budget concurrency cap.

    Bug #899 (fault-injection factory wired here): previously VoyageAIClient was
    constructed without an http_client_factory, bypassing fault injection in Phase 5
    E2E tests. Now passes _get_http_client_factory() from search_service.

    Story #1108 (S4): no_embedding_cache_shortcut is forwarded to
    coalesced_query_embedding so the caller can bypass the cache read for this
    request without disabling future cache benefit (write still fires).

    Returns:
        A non-empty list of floats on success.
        An empty list on any error (logged at WARNING); the caller must check
        for emptiness and skip memory retrieval when [] is returned.
    """
    try:
        from code_indexer.config import VoyageAIConfig
        from code_indexer.services.voyage_ai import VoyageAIClient
        from code_indexer.server.services.search_service import _get_http_client_factory
        from code_indexer.server.services.governed_call import (
            coalesced_query_embedding,
        )

        # Bug #899 fix: pass factory so fault injection intercepts this client.
        # AttributeError guard: app.state not set in unit-test environments without
        # full lifespan; None is equivalent to the pre-#899 default (no fault injection).
        try:
            _factory = _get_http_client_factory()
        except AttributeError:
            logger.warning(
                "http_client_factory not available on app.state; "
                "VoyageAIClient using default transport (fault injection inactive). "
                "This is expected only in unit-test environments, not in production."
            )
            _factory = None

        provider = VoyageAIClient(
            VoyageAIConfig(timeout=_configured_embedding_timeout_seconds()),
            http_client_factory=_factory,
        )
        vec, _embed_meta = coalesced_query_embedding(
            provider,
            query_text,
            no_embedding_cache_shortcut=no_embedding_cache_shortcut,
        )
        # Issue #1159: propagate Voyage embedding cache metadata to SearchEventContext.
        # Bug #1813 (DEFECT 2): write atomically via record_provider_cache_fields()
        # -- the single, lock-protected write path (consistency with the omni
        # fan-out call sites, which DO race on this same shared context).
        _event_ctx = _search_event_ctx.get(None)
        if _event_ctx is not None:
            _event_ctx.record_provider_cache_fields(
                "voyage-ai",
                cache_hit=_embed_meta.key_found,
                cache_mode=_embed_meta.cache_mode,
                latency_ms=_embed_meta.provider_latency_ms,
            )
        # Story #1293: emit the durable search_embed_event row for this inline
        # MCP call. No-op when meta isn't yet classified (Path A coalescer
        # path — Story #1293 S1b) or when no writer is installed.
        emit_embed_event(_embed_meta)
        return cast(List[float], vec)
    except Exception as exc:
        logger.warning(
            "Memory retrieval: could not compute query vector — %s. "
            "Memory retrieval skipped for this request.",
            exc,
        )
        return []


def _compute_shared_query_vector(
    query_text: str,
    no_embedding_cache_shortcut: bool = False,
) -> tuple:
    """Compute the shared pre-fan-out query vector for omni semantic searches.

    Defect #1148 fix: unlike _compute_memory_query_vector (which returns [] on
    any error for memory-retrieval callers), this function returns a 2-tuple
    (vector: List[float], digest: str) so that the omni handler can propagate
    the provider-config digest alongside the vector.  Per-repo searches in
    _search_semantic_sync compare the digest against their own embedding-service
    digest and only reuse the precomputed vector when they match — repos on a
    different provider config embed via their own chokepoint (config-correct).

    On failure: logs WARNING (Messi #13 anti-silent-failure) and returns
    ([], "") so the caller can detect failure and fall back EXPLICITLY to
    per-repo embedding (the fallback is observable in logs, not silent).

    Returns:
        (vector, digest) where vector is a non-empty List[float] and digest is
        a non-empty str on success; ([], "") on any error.
    """
    try:
        from code_indexer.config import VoyageAIConfig
        from code_indexer.services.voyage_ai import VoyageAIClient
        from code_indexer.server.services.search_service import _get_http_client_factory
        from code_indexer.server.services.governed_call import (
            coalesced_query_embedding,
        )
        from code_indexer.server.services.coalescer_registry import (
            _digest_for_provider,
        )

        try:
            _factory = _get_http_client_factory()
        except AttributeError:
            logger.warning(
                "http_client_factory not available on app.state; "
                "VoyageAIClient using default transport (fault injection inactive). "
                "This is expected only in unit-test environments, not in production."
            )
            _factory = None

        provider = VoyageAIClient(
            VoyageAIConfig(timeout=_configured_embedding_timeout_seconds()),
            http_client_factory=_factory,
        )
        digest = _digest_for_provider(provider)
        vec, _embed_meta = coalesced_query_embedding(
            provider,
            query_text,
            no_embedding_cache_shortcut=no_embedding_cache_shortcut,
        )
        # Issue #1159: propagate Voyage embedding cache metadata to SearchEventContext.
        # Bug #1813 (DEFECT 2): write atomically via record_provider_cache_fields()
        # -- the single, lock-protected write path (consistency with the omni
        # fan-out call sites, which DO race on this same shared context).
        _event_ctx = _search_event_ctx.get(None)
        if _event_ctx is not None:
            _event_ctx.record_provider_cache_fields(
                "voyage-ai",
                cache_hit=_embed_meta.key_found,
                cache_mode=_embed_meta.cache_mode,
                latency_ms=_embed_meta.provider_latency_ms,
            )
        # Story #1293: emit the durable search_embed_event row for this inline
        # MCP call. No-op when meta isn't yet classified (Path A coalescer
        # path — Story #1293 S1b) or when no writer is installed.
        emit_embed_event(_embed_meta)
        return (cast(List[float], vec), digest)
    except Exception as exc:
        logger.warning(
            "Omni search: could not compute shared query vector — %s. "
            "Falling back to per-repo embedding (each repo will embed via its "
            "own provider chokepoint). This is an explicit fallback, not a "
            "silent failure.",
            exc,
        )
        return ([], "")


def _run_memory_retrieval(
    params: Dict[str, Any],
    user: User,
    config_service: Any,
    reranker_status: str,
    query_vector: Optional[List[float]] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Run memory retrieval and return candidate list, or None to suppress.

    Returns None when:
      - search_mode not in {semantic, hybrid}
      - memory_retrieval_enabled is False (kill-switch)

    Preconditions (enforced by caller):
      - params["query_text"] is a non-empty string
      - user.username is a non-empty string
      - config_service is a live ConfigService with a memory_retrieval_config attribute
      - reranker_status is the "status" string extracted from rerank_meta by the caller

    Args:
        params: Raw MCP params dict (query_text, search_mode, limit, ...).
        user: Authenticated caller; user.username for candidate partitioning.
        config_service: Live config service forwarded to build_relevant_memories.
        reranker_status: "status" value from rerank_meta["reranker_status"]["status"],
            extracted by the caller after _apply_rerank_and_filter returns.
        query_vector: Optional pre-computed Voyage embedding vector (Story #883 Phase C).
            When not None, this vector is reused directly and _compute_memory_query_vector
            is NOT called — eliminating the duplicate Voyage API call.
            When None (legacy callers), the vector is computed internally.
    """
    search_mode = params.get("search_mode", "semantic")
    if search_mode not in _MEMORY_SEMANTIC_MODES:
        return None

    raw_mem_cfg = config_service.get_config().memory_retrieval_config
    if not raw_mem_cfg.memory_retrieval_enabled:
        return None

    # Normalise query_text to a plain string; guard against None/non-string values.
    raw_query = params.get("query_text")
    query_text = str(raw_query) if raw_query is not None else ""

    # Story #883 Phase C: reuse caller-supplied vector when not None to avoid a
    # second Voyage API round-trip.  Use `is None` (not falsy) so an explicitly
    # supplied non-empty vector is always used; only compute internally when the
    # caller did not supply a vector at all.
    if query_vector is None:
        # Story #1108 (S4): thread per-request cache bypass so the fallback
        # embedding call also honours the flag.
        query_vector = _compute_memory_query_vector(
            query_text,
            no_embedding_cache_shortcut=params.get(
                "no_embedding_cache_shortcut", False
            ),
        )
    if not query_vector:
        # Empty list: either compute returned [] (WARNING logged there) or caller
        # passed an empty list (treated as "no vector available").
        return None

    pipeline_config = MemoryRetrievalPipelineConfig(
        memory_retrieval_enabled=raw_mem_cfg.memory_retrieval_enabled,
        memory_voyage_min_score=raw_mem_cfg.memory_voyage_min_score,
        memory_cohere_min_score=raw_mem_cfg.memory_cohere_min_score,
        memory_retrieval_k_multiplier=raw_mem_cfg.memory_retrieval_k_multiplier,
        memory_retrieval_max_body_chars=raw_mem_cfg.memory_retrieval_max_body_chars,
    )
    store_base_path = str(Path(_get_golden_repos_dir()) / _CIDX_META_DIR_NAME)
    pipeline = MemoryRetrievalPipeline(
        config=pipeline_config,
        store_base_path=store_base_path,
    )

    requested_limit = _coerce_int(params.get("limit"), _DEFAULT_SEARCH_LIMIT)
    # GAP 1: pass real vector so retriever does not raise ValueError on empty [].
    memory_candidates = pipeline.get_memory_candidates(
        query_vector=query_vector,
        user_id=user.username,
        requested_limit=requested_limit,
        search_mode=search_mode,
    )
    filtered_candidates = pipeline.apply_voyage_floor(memory_candidates)

    assembled = pipeline.build_relevant_memories(
        memory_candidates=filtered_candidates,
        query=query_text,
        config_service=config_service,
        reranker_status=reranker_status,
    )

    # GAP 2: order by hnsw_score desc (reranker disabled) or keep reranker order;
    # then apply Cohere floor (skipped when reranker_status == "disabled").
    ordered = pipeline.order_memory_items(assembled, reranker_status)
    floor_filtered = pipeline.apply_cohere_floor(ordered, reranker_status)

    # GAP 4: hydrate body from disk for each real candidate.
    # cast: _hydrate_memory_bodies is typed correctly in memory_retrieval_pipeline.py
    # but mypy loses the return-type annotation across the module import boundary.
    hydrated: List[Dict[str, Any]] = cast(
        List[Dict[str, Any]],
        _hydrate_memory_bodies(floor_filtered, store_base_path),
    )

    # GAP 3: inject empty-state nudge when no memories survived all filters.
    if not hydrated:
        return [_build_empty_nudge_entry()]

    return hydrated
