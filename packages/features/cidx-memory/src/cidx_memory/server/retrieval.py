"""The ``relevant_memories`` section of code search (Story #883).

Contributed to the core as a search augmenter (``search_augmenters()``): for
a semantic or hybrid single-repository ``search_code`` the core computes the
query embedding once, runs the search, and hands both here; this module
answers the memories the caller may see, ordered and floored, with their
bodies, or the empty-state nudge.

Visibility is the core's memory scope rule, reached through the
``access_projection`` capability (:func:`visible_candidates`): it is decided
before any memory body is read.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from cidx_sdk import SearchAugmenterContribution, SearchAugmentRequest

from cidx_memory._platform import server_platform
from cidx_memory.server import runtime
from cidx_memory.server.memory_candidate_retriever import MemoryCandidate
from cidx_memory.server.memory_retrieval_pipeline import (
    MemoryRetrievalPipeline,
    MemoryRetrievalPipelineConfig,
    _build_empty_nudge_entry,
    _hydrate_memory_bodies,
)

logger = logging.getLogger(__name__)

#: The ``query_metadata`` key the section is answered under.
SECTION_KEY = "relevant_memories"
#: Modes that trigger memory retrieval alongside code search.
MEMORY_SEARCH_MODES = ("semantic", "hybrid")
#: The catalog repository's directory under the golden repositories directory.
CIDX_META_DIR_NAME = "cidx-meta"


def visible_candidates(
    candidates: List[MemoryCandidate], username: str
) -> List[MemoryCandidate]:
    """Keep only the candidates the caller may see, before any body is read.

    Visibility is the ``access_projection`` capability's memory rule (one
    call per request). Without it no candidate is visible.
    """
    if not candidates:
        return []
    projection = runtime.access_projection()
    if projection is None:
        logger.warning(
            "Memory retrieval: access projection unavailable; "
            "no memories returned for this request."
        )
        return []
    visible = set(
        projection.visible_memory_ids(username, [c.memory_id for c in candidates])
    )
    return [c for c in candidates if c.memory_id in visible]


def _golden_repos_dir() -> str:
    app = server_platform().app_attr("app")
    golden_repos_dir = getattr(getattr(app, "state", None), "golden_repos_dir", None)
    if not golden_repos_dir:
        raise RuntimeError("golden_repos_dir not configured in app.state")
    return str(golden_repos_dir)


def is_active() -> bool:
    """The kill-switch (``memory_retrieval_enabled``)."""
    try:
        return bool(runtime.memory_retrieval_config().memory_retrieval_enabled)
    except Exception:
        logger.warning(
            "Memory retrieval: settings unreadable; skipped for this request.",
            exc_info=True,
        )
        return False


def relevant_memories(
    request: SearchAugmentRequest,
) -> Optional[Sequence[Mapping[str, object]]]:
    """The ``relevant_memories`` section for one search, or None to suppress.

    None when the mode is not semantic/hybrid, the kill-switch is off, or no
    query vector is available (the core's embedding call failed).
    """
    if request.search_mode not in MEMORY_SEARCH_MODES:
        return None
    mem_cfg = runtime.memory_retrieval_config()
    if not mem_cfg.memory_retrieval_enabled:
        return None
    query_vector = list(request.query_vector)
    if not query_vector:
        return None

    pipeline_config = MemoryRetrievalPipelineConfig(
        memory_retrieval_enabled=mem_cfg.memory_retrieval_enabled,
        memory_voyage_min_score=mem_cfg.memory_voyage_min_score,
        memory_cohere_min_score=mem_cfg.memory_cohere_min_score,
        memory_retrieval_k_multiplier=mem_cfg.memory_retrieval_k_multiplier,
        memory_retrieval_max_body_chars=mem_cfg.memory_retrieval_max_body_chars,
    )
    store_base_path = str(Path(_golden_repos_dir()) / CIDX_META_DIR_NAME)
    pipeline = MemoryRetrievalPipeline(
        config=pipeline_config, store_base_path=store_base_path
    )

    memory_candidates = pipeline.get_memory_candidates(
        query_vector=query_vector,
        user_id=request.username,
        requested_limit=request.limit,
        search_mode=request.search_mode,
    )
    filtered = pipeline.apply_voyage_floor(memory_candidates)
    visible = visible_candidates(filtered, request.username)

    assembled = pipeline.build_relevant_memories(
        memory_candidates=visible,
        query=request.query_text,
        config_service=None,
        reranker_status=request.reranker_status,
    )
    # GAP 2: order by hnsw_score desc (reranker disabled) or keep reranker
    # order; then apply the Cohere floor (skipped when the reranker is off).
    ordered = pipeline.order_memory_items(assembled, request.reranker_status)
    floor_filtered = pipeline.apply_cohere_floor(ordered, request.reranker_status)

    # GAP 4: hydrate each surviving candidate's body from disk.
    hydrated: List[Dict[str, Any]] = _hydrate_memory_bodies(
        floor_filtered, store_base_path
    )
    # GAP 3: the empty-state nudge when no memory survived every filter.
    if not hydrated:
        return [_build_empty_nudge_entry()]
    return hydrated


MEMORY_SEARCH_AUGMENTER = SearchAugmenterContribution(
    key=SECTION_KEY,
    search_modes=MEMORY_SEARCH_MODES,
    is_active=is_active,
    augment=relevant_memories,
)
