"""The ``memory`` feature's config section: the keys, their kind and defaults.

Consumed by the core (``config_section()``): every key is a runtime key,
stored in the server's ``feature_settings["memory"]`` (database, Web UI).
Before this feature owned them they were ``ServerConfig.memory_retrieval_config``
(Story #883); the core moves a stored ``memory_retrieval_config`` into the
section when it loads a configuration, so an upgrade loses nothing. The memory
store's limits (summary length, rate limit, lock TTLs) were never settings and
are not now (``server/memory_store_service_factory.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Dict, Mapping

from cidx_sdk import ConfigKey, ConfigSection

#: Section and feature name.
SECTION_NAME = "memory"

MEMORY_RETRIEVAL_ENABLED = "memory_retrieval_enabled"


@dataclass
class MemoryRetrievalConfig:
    """Server-side memory retrieval configuration (Story #883).

    Controls automatic parallel memory lookup triggered by semantic/hybrid
    queries. All keys are runtime (DB-backed) -- NOT bootstrap config.json keys.
    """

    # Master kill-switch: set False to suppress memory retrieval and nudge entirely
    memory_retrieval_enabled: bool = True
    # HNSW cosine floor from Voyage embedding search
    memory_voyage_min_score: float = 0.5
    # Post-rerank floor from Cohere reranker (skipped when reranker is disabled)
    memory_cohere_min_score: float = 0.4
    # k = max(20, request.limit * k_multiplier) candidates fetched before reranking
    memory_retrieval_k_multiplier: int = 5
    # Maximum body characters included in each memory entry; excess is truncated
    memory_retrieval_max_body_chars: int = 2000


_DESCRIPTIONS = {
    MEMORY_RETRIEVAL_ENABLED: "Offer relevant memories next to semantic and "
    "hybrid code search results (relevant_memories).",
    "memory_voyage_min_score": "Minimum embedding similarity of a memory candidate.",
    "memory_cohere_min_score": "Minimum reranker score of a memory (skipped "
    "when the reranker is disabled).",
    "memory_retrieval_k_multiplier": "Candidates fetched: max(20, limit * this).",
    "memory_retrieval_max_body_chars": "Maximum characters of each memory body "
    "returned; longer bodies are truncated.",
}

_MINIMUMS: Dict[str, float] = {
    "memory_voyage_min_score": 0.0,
    "memory_cohere_min_score": 0.0,
    "memory_retrieval_k_multiplier": 1,
    "memory_retrieval_max_body_chars": 1,
}
_MAXIMUMS: Dict[str, float] = {
    "memory_voyage_min_score": 1.0,
    "memory_cohere_min_score": 1.0,
}

_LABELS = {
    MEMORY_RETRIEVAL_ENABLED: "Memory Retrieval Enabled",
    "memory_voyage_min_score": "Voyage Min Score",
    "memory_cohere_min_score": "Cohere Min Score",
    "memory_retrieval_k_multiplier": "Candidate Multiplier (k)",
    "memory_retrieval_max_body_chars": "Max Body Characters",
}

MEMORY_CONFIG_SECTION = ConfigSection(
    name=SECTION_NAME,
    title="Memory Retrieval",
    description=(
        "Automatic memory lookup next to semantic and hybrid code search "
        "results (Story #883)."
    ),
    keys=tuple(
        ConfigKey(
            name=f.name,
            default=f.default,
            label=_LABELS[f.name],
            description=_DESCRIPTIONS[f.name],
            minimum=_MINIMUMS.get(f.name),
            maximum=_MAXIMUMS.get(f.name),
        )
        for f in fields(MemoryRetrievalConfig)
    ),
)


def default_values() -> Dict[str, object]:
    """Every key of the section at its default."""
    return {key.name: key.default for key in MEMORY_CONFIG_SECTION.keys}


def memory_retrieval_config_from(values: Mapping[str, Any]) -> MemoryRetrievalConfig:
    """A :class:`MemoryRetrievalConfig` from section values (unknown keys
    ignored, missing keys at their defaults)."""
    known = {f.name for f in fields(MemoryRetrievalConfig)}
    return MemoryRetrievalConfig(**{k: v for k, v in values.items() if k in known})
