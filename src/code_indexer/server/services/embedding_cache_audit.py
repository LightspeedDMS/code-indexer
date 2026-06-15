"""Story #1110 (S6 Chunk B): Deep-fidelity embedding cache audit.

Provides `_run_deep_fidelity_audit()` — called from the FSV search() chokepoint
after the primary HNSW search when `audit_ctx["sampled"]` is True.

Architecture:
- shadow mode: primary search used the LIVE vector; second search uses the
  CACHED vector (decoded from audit_ctx["cached_blob"]).
- on mode: primary search used the CACHED vector; second search re-embeds via
  `governed_query_embedding()` (the one sampled-fraction-only live call).

After both searches, compute:
  top10_overlap = |primary_topK ∩ second_topK| / max(len(primary_topK), len(second_topK))
  top1_match    = primary_top1 == second_top1

Record via `QueryEmbeddingCacheMetrics.record_audit()`.

Fail-open: any exception inside this function is caught and logged at WARNING.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

import numpy as np

from code_indexer.server.services.governed_call import (
    governed_query_embedding,
    get_query_embedding_cache_metrics,
)

logger = logging.getLogger(__name__)

# Named constant for the audit top-K size used in overlap computation.
AUDIT_TOP_K = 10


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _decode_cached_vector(cached_blob: bytes) -> List[float]:
    """Decode a float32 LE-encoded blob into a list of floats."""
    num_floats = len(cached_blob) // 4
    return list(struct.unpack(f"<{num_floats}f", cached_blob))


def _get_second_search_vector(
    mode: str,
    audit_ctx: Dict[str, Any],
    embedding_provider: Any,
    query: str,
) -> Optional[List[float]]:
    """Return the vector for the second (audit) HNSW search.

    shadow: second search uses CACHED blob (primary used LIVE).
    on:     second search re-embeds live via governed_query_embedding.
    other:  unknown mode — log and return None (skip audit).
    """
    if mode == "shadow":
        return _decode_cached_vector(audit_ctx["cached_blob"])
    elif mode == "on":
        return cast(
            Optional[List[float]],
            governed_query_embedding(
                embedding_provider, query, embedding_purpose="query"
            ),
        )
    else:
        logger.warning(
            "_run_deep_fidelity_audit: unknown mode %r — skipping audit", mode
        )
        return None


def _record_audit_metrics(
    *,
    primary_candidate_ids: List[str],
    second_ids: List[str],
    provider_name: str,
    mode: str,
) -> None:
    """Compute top-K overlap + top-1 match and call metrics.record_audit()."""
    primary_top = primary_candidate_ids[:AUDIT_TOP_K]
    second_top = second_ids[:AUDIT_TOP_K]
    denom = max(len(primary_top), len(second_top))
    top10_overlap = (len(set(primary_top) & set(second_top)) / denom) if denom else 0.0
    top1_match = bool(
        primary_candidate_ids
        and second_ids
        and primary_candidate_ids[0] == second_ids[0]
    )

    metrics = get_query_embedding_cache_metrics()
    if metrics is not None:
        metrics.record_audit(
            top10_overlap=top10_overlap,
            top1_match=top1_match,
            provider=provider_name,
            mode=mode,
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _run_deep_fidelity_audit(
    *,
    audit_ctx: Dict[str, Any],
    hnsw_index: Any,
    hnsw_manager: Any,
    collection_path: Path,
    ef: int,
    primary_candidate_ids: List[str],
    embedding_provider: Any,
    query: str,
) -> None:
    """Run a deep-fidelity audit comparing cached vs live HNSW result sets.

    Called from FilesystemVectorStore.search() after the primary HNSW search
    when audit_ctx["sampled"] is True.

    Args:
        audit_ctx: Mutable dict populated by coalesced_query_embedding().
        hnsw_index: The loaded hnswlib.Index from the primary search.
        hnsw_manager: HNSWIndexManager instance.
        collection_path: Path to the collection directory.
        ef: HNSW ef parameter (same as primary search; must be > 0).
        primary_candidate_ids: Candidate IDs from the primary HNSW search.
        embedding_provider: Embedding provider (for on-mode re-embed).
        query: Original query text (for on-mode re-embed).

    Fail-open: any exception is swallowed with WARNING logging.
    """
    if not audit_ctx.get("sampled"):
        return

    try:
        mode: str = audit_ctx["mode"]
        provider_name: str = audit_ctx["provider"]

        # Validate ef parameter before use
        if not isinstance(ef, int) or ef <= 0:
            logger.warning(
                "_run_deep_fidelity_audit: invalid ef=%r — skipping audit", ef
            )
            return

        # Guard: empty primary result
        if not primary_candidate_ids:
            logger.debug(
                "_run_deep_fidelity_audit: primary result set is empty — skipping"
            )
            return

        # Obtain the second-search vector
        second_vec_list = _get_second_search_vector(
            mode, audit_ctx, embedding_provider, query
        )
        if second_vec_list is None:
            return

        second_vec = np.array(second_vec_list, dtype=np.float32)

        # Guard: zero-norm second vector
        if np.linalg.norm(second_vec) == 0:
            logger.debug(
                "_run_deep_fidelity_audit: second vector has zero norm — skipping"
            )
            return

        # Second HNSW search
        second_ids, _ = hnsw_manager.query(
            index=hnsw_index,
            query_vector=second_vec,
            collection_path=collection_path,
            k=AUDIT_TOP_K,
            ef=ef,
        )

        if not second_ids:
            logger.debug(
                "_run_deep_fidelity_audit: second HNSW returned empty result — skipping"
            )
            return

        _record_audit_metrics(
            primary_candidate_ids=primary_candidate_ids,
            second_ids=second_ids,
            provider_name=provider_name,
            mode=mode,
        )

    except Exception as exc:  # noqa: BLE001
        logger.warning("_run_deep_fidelity_audit: audit failed (swallowed): %s", exc)


# Public alias so callers (e.g. FilesystemVectorStore) can import a stable name
# while the private function remains patchable by tests via the module namespace.
run_deep_fidelity_audit = _run_deep_fidelity_audit
