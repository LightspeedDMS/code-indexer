"""MemoryRetrievalPipeline — Story #883 Components 3-9 (pipeline stages).

Encapsulates the per-query memory retrieval filter and ordering stages:
  - Gate check (kill-switch, search-mode bypass)
  - Voyage HNSW floor filter
  - Cohere post-rerank floor filter (with reranker-disabled bypass)
  - HNSW-score ordering when reranker is unconfigured
  - Body truncation with marker
  - Candidate list assembly for the MCP response

NOTE: Body hydration (disk I/O via read_memory_file), empty-state nudge
injection, and response serialization are performed by the search handler
in search.py, not by this module.  This module owns only the pure
filter/order/truncate logic so it can be tested without disk dependencies.

Design notes:
  - MemoryRetrievalPipeline holds a MemoryCandidateRetriever instance wired
    to the store_base_path supplied at construction time.
  - All public methods are synchronous (MCP handlers run in a WSGI thread).
"""

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from code_indexer.server.services.memory_candidate_retriever import (
    MemoryCandidate,
    MemoryCandidateRetriever,
)
from code_indexer.server.services.memory_io import read_memory_file

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level named constants (avoid magic numbers throughout)
# ---------------------------------------------------------------------------

DEFAULT_VOYAGE_MIN_SCORE: float = 0.5
DEFAULT_COHERE_MIN_SCORE: float = 0.4
DEFAULT_K_MULTIPLIER: int = 5
DEFAULT_MAX_BODY_CHARS: int = 2000

# Minimum candidate pool size regardless of limit * multiplier.
MIN_CANDIDATE_POOL_SIZE: int = 20

# Modes that trigger memory retrieval alongside code search.
_SEMANTIC_MODES = frozenset({"semantic", "hybrid"})

# Marker appended to bodies that exceed max_body_chars.
_TRUNCATION_MARKER = "\n\n[...truncated]"

# Path to the nudge .md file, relative to this module (Messi Rule #11).
_NUDGE_MD_PATH = Path(__file__).parent / "prompts" / "memory_empty_nudge.md"

# Safe memory ID pattern: alphanumeric, hyphens, underscores only.
_MEMORY_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@lru_cache(maxsize=1)
def _load_empty_nudge_text() -> str:
    """Return nudge text from prompts/memory_empty_nudge.md (cached, Messi Rule #11)."""
    return _NUDGE_MD_PATH.read_text(encoding="utf-8")


def _build_empty_nudge_entry() -> Dict[str, Any]:
    """Return the __empty_nudge__ sentinel dict for injection into relevant_memories."""
    return {
        "memory_id": "__empty_nudge__",
        "title": "No memories found",
        "body": _load_empty_nudge_text(),
        "hnsw_score": 0.0,
        "is_nudge": True,
    }


def _validate_memory_id(memory_id: Any) -> bool:
    """Return True iff memory_id is a non-empty string matching the safe pattern."""
    if not isinstance(memory_id, str) or not memory_id:
        return False
    return bool(_MEMORY_ID_SAFE_RE.match(memory_id))


def _resolve_confined_memory_path(memory_id: str, memories_dir: Path) -> Optional[Path]:
    """Resolve memory file path and confirm it stays inside memories_dir.

    Uses Path.relative_to() for path-aware confinement (immune to prefix attacks).
    Returns None when the resolved path escapes the directory.
    """
    resolved = (memories_dir / f"{memory_id}.md").resolve()
    try:
        resolved.relative_to(memories_dir)
        return resolved
    except ValueError:
        return None


def _hydrate_memory_bodies(
    candidates: List[Dict[str, Any]],
    store_base_path: str,
) -> List[Dict[str, Any]]:
    """Hydrate 'body' in each candidate dict by reading its memory file from disk.

    Skips __empty_nudge__ entries (body already set).  Drops any item whose
    memory_id fails validation, whose path escapes memories_dir, or whose file
    cannot be read — all skips logged at WARNING (AC15 skip-on-corrupt).
    """
    if not isinstance(store_base_path, str) or not store_base_path:
        raise ValueError("store_base_path must be a non-empty string")
    if not isinstance(candidates, list):
        raise TypeError(f"candidates must be a list, got {type(candidates).__name__}")

    memories_dir = Path(store_base_path).resolve() / "memories"
    hydrated: List[Dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            logger.warning("Skipping non-dict candidate: %r", item)
            continue
        memory_id = item.get("memory_id")
        if memory_id == "__empty_nudge__":
            hydrated.append(item)
            continue
        if not _validate_memory_id(memory_id):
            logger.warning("Skipping memory with invalid memory_id %r", memory_id)
            continue
        # cast: _validate_memory_id confirms memory_id is a non-empty str here,
        # but mypy cannot infer the type narrowing from a boolean validator function.
        memory_path = _resolve_confined_memory_path(cast(str, memory_id), memories_dir)
        if memory_path is None:
            logger.warning(
                "Skipping memory '%s': path escapes memories directory", memory_id
            )
            continue
        try:
            _fm, body, _hash = read_memory_file(memory_path)
            hydrated.append({**item, "body": body})
        except Exception as exc:
            logger.warning("Skipping memory '%s': file error — %s", memory_id, exc)
    return hydrated


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------


@dataclass
class MemoryRetrievalPipelineConfig:
    """Runtime configuration for the memory retrieval pipeline.

    All fields correspond to the 5 Story #883 runtime config keys stored in
    the CIDX server database (not in config.json).

    Attributes:
        memory_retrieval_enabled: Master kill-switch (default True).
        memory_voyage_min_score: HNSW cosine floor; candidates below this are
            dropped before reranking.
        memory_cohere_min_score: Post-rerank floor applied only when the
            reranker is active.
        memory_retrieval_k_multiplier: Candidate pool size = max(MIN, limit*N).
            Must be a positive integer (>= 1).
        memory_retrieval_max_body_chars: Maximum characters per memory body;
            bodies longer than this are truncated with _TRUNCATION_MARKER.
            Must be a positive integer (>= 1).
    """

    memory_retrieval_enabled: bool = True
    memory_voyage_min_score: float = DEFAULT_VOYAGE_MIN_SCORE
    memory_cohere_min_score: float = DEFAULT_COHERE_MIN_SCORE
    memory_retrieval_k_multiplier: int = DEFAULT_K_MULTIPLIER
    memory_retrieval_max_body_chars: int = DEFAULT_MAX_BODY_CHARS

    def __post_init__(self) -> None:
        if (
            isinstance(self.memory_retrieval_k_multiplier, bool)
            or not isinstance(self.memory_retrieval_k_multiplier, int)
            or self.memory_retrieval_k_multiplier < 1
        ):
            raise ValueError(
                "memory_retrieval_k_multiplier must be a positive integer (>= 1), "
                f"got {self.memory_retrieval_k_multiplier!r}"
            )
        if (
            isinstance(self.memory_retrieval_max_body_chars, bool)
            or not isinstance(self.memory_retrieval_max_body_chars, int)
            or self.memory_retrieval_max_body_chars < 1
        ):
            raise ValueError(
                "memory_retrieval_max_body_chars must be a positive integer (>= 1), "
                f"got {self.memory_retrieval_max_body_chars!r}"
            )


# ---------------------------------------------------------------------------
# Pipeline class
# ---------------------------------------------------------------------------


class MemoryRetrievalPipeline:
    """Encapsulates memory retrieval filter/order/truncate stages (Story #883).

    Constructed once per request (or cached by the caller) with a
    MemoryRetrievalPipelineConfig and a store_base_path pointing to the
    cidx-meta golden-repo directory that holds the memories HNSW index.
    """

    def __init__(
        self,
        config: MemoryRetrievalPipelineConfig,
        store_base_path: str,
    ) -> None:
        self._config = config
        self._retriever = MemoryCandidateRetriever(store_base_path=store_base_path)

    def get_memory_candidates(
        self,
        query_vector: List[float],
        user_id: str,
        requested_limit: int,
        search_mode: str,
    ) -> List[MemoryCandidate]:
        """Return raw HNSW memory candidates, or [] when gating rules apply.

        Gating rules (no retriever call made):
          - kill-switch off (memory_retrieval_enabled=False)
          - search_mode not in {semantic, hybrid}

        Args:
            query_vector: Pre-computed Voyage embedding shared with code search.
            user_id: Caller identity for future per-user partitioning.
            requested_limit: The code search limit; must be a positive integer (>= 1).
            search_mode: One of "semantic", "hybrid", "fts", "regex", etc.

        Returns:
            List of MemoryCandidate ordered by descending HNSW score, or [].

        Raises:
            ValueError: If requested_limit is not a positive integer (>= 1).
        """
        if (
            isinstance(requested_limit, bool)
            or not isinstance(requested_limit, int)
            or requested_limit < 1
        ):
            raise ValueError(
                f"requested_limit must be a positive integer (>= 1), got {requested_limit!r}"
            )

        if not self._config.memory_retrieval_enabled:
            return []

        if search_mode not in _SEMANTIC_MODES:
            return []

        k = max(
            MIN_CANDIDATE_POOL_SIZE,
            requested_limit * self._config.memory_retrieval_k_multiplier,
        )
        # cast: MemoryCandidateRetriever.retrieve() is typed List[MemoryCandidate]
        # but mypy infers Any through the imported class boundary here.
        return cast(
            List[MemoryCandidate],
            self._retriever.retrieve(query_vector=query_vector, user_id=user_id, k=k),
        )

    def apply_voyage_floor(
        self, candidates: List[MemoryCandidate]
    ) -> List[MemoryCandidate]:
        """Drop candidates whose HNSW score is below memory_voyage_min_score.

        The floor is inclusive: a candidate at exactly the threshold is kept.
        """
        floor = self._config.memory_voyage_min_score
        return [c for c in candidates if c.hnsw_score >= floor]

    def apply_cohere_floor(
        self,
        memory_pool_items: List[Dict[str, Any]],
        reranker_status: str,
    ) -> List[Dict[str, Any]]:
        """Drop memory pool items below memory_cohere_min_score after rerank.

        Skipped entirely when reranker_status == "disabled" (Scenario 16):
        when no reranker is configured, only the Voyage floor applies.
        """
        if reranker_status == "disabled":
            return list(memory_pool_items)
        floor = self._config.memory_cohere_min_score
        return [
            item for item in memory_pool_items if item.get("rerank_score", 0.0) >= floor
        ]

    def order_memory_items(
        self,
        memory_pool_items: List[Dict[str, Any]],
        reranker_status: str,
    ) -> List[Dict[str, Any]]:
        """Order memory items by hnsw_score descending when reranker is disabled.

        When reranker_status != "disabled" the caller relies on the reranker's
        ordering (rerank_score desc) instead.
        """
        if reranker_status == "disabled":
            return sorted(
                memory_pool_items,
                key=lambda item: item.get("hnsw_score", 0.0),
                reverse=True,
            )
        return list(memory_pool_items)

    def truncate_body(self, text: str) -> str:
        """Truncate text to memory_retrieval_max_body_chars and append marker.

        Short texts (len <= max_body_chars) are returned unchanged.
        Config already validated by __post_init__ so limit is always >= 1.
        """
        limit = self._config.memory_retrieval_max_body_chars
        if len(text) <= limit:
            return text
        return text[:limit] + _TRUNCATION_MARKER

    def build_relevant_memories(
        self,
        memory_candidates: List[MemoryCandidate],
        query: str,  # noqa: ARG002  reserved for future relevance annotation
        config_service: Optional[Any],  # noqa: ARG002  reserved for hot-reload
        reranker_status: str,  # noqa: ARG002  reserved for ordering annotation
    ) -> List[Dict[str, Any]]:
        """Assemble the relevant_memories candidate list.

        Returns a list of dicts for each candidate that survived all floor
        filters and access control.  Returns [] when no candidates supplied.

        NOTE: Body hydration (read_memory_file), body truncation application,
        and the empty-state nudge injection are performed by search.py after
        calling this method.
        """
        if not memory_candidates:
            return []

        return [
            {
                "memory_id": candidate.memory_id,
                "title": candidate.title,
                "hnsw_score": candidate.hnsw_score,
            }
            for candidate in memory_candidates
        ]
