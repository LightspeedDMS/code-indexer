"""The shared query-vector helper for search handlers.

The query embedding is computed once per request and shared by the code
search fan-out (omni) and every contributed search augmenter
(``features/search_augmenters.py``; the memory feature's
``relevant_memories``). The memory retrieval itself moved into the
cidx-memory feature (modular phase 4f).

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
from typing import List, cast

from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.search_embed_event_emit import emit_embed_event
from code_indexer.server.services.search_event_context import _search_event_ctx


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


def _compute_shared_query_vector(
    query_text: str,
    no_embedding_cache_shortcut: bool = False,
) -> tuple:
    """Compute the shared pre-fan-out query vector for omni semantic searches.

    Defect #1148 fix: this function returns a 2-tuple
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
                cache_hit=_embed_meta.search_event_cache_hit,
                cache_mode=_embed_meta.search_event_cache_mode,
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
