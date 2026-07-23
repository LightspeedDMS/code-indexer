"""TemporalWorkerInput -- Story #1400 Phase 3 (FINAL LOCKED DESIGN).

A normalized, fully-typed data struct carrying every field the temporal
fusion/reconstruction path needs, so BOTH MCP (`search.py`'s `Dict` payload)
and REST (`query.py`'s `SemanticQueryRequest`) doors can adapt their
protocol-specific payload into ONE shared shape a future async-hybrid
worker consumes -- never a vague "filters"/"temporal_params" catch-all.

Enumerates all 17 real parameters of
`services.temporal.temporal_fusion_dispatch.execute_temporal_query_with_fusion`
(config/index_path/vector_store/query_text/limit are reconstructed
separately via `reconstruct_temporal_backend` + this struct's own
query_text/requested_limit -- they are not struct fields themselves).

`min_score_ignored_for_temporal` / `file_extensions_ignored_for_temporal`
are deliberately NEVER forwarded to fusion -- parity-preserving, since
today's inline `_execute_temporal_query` already silently ignores both for
temporal queries. They exist on the struct purely for observability/dedup
signature completeness, not to be consumed downstream.

This module lives in the CLI-safe `services/temporal/` package (no
server-only imports) so it can eventually be constructed and consumed by
both server-side doors AND, if ever needed, a CLI-side caller -- mirroring
the existing `temporal_fusion_dispatch.py` placement.
"""

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class TemporalWorkerInput:
    """Normalized temporal-query input, shared by MCP and REST adapters."""

    repo_path: str
    repository_alias: str
    username: str
    query_text: str
    requested_limit: int
    fusion_fetch_limit: int
    time_range: Optional[Tuple[str, str]]
    time_range_raw: Optional[str]
    time_range_all: bool
    file_path_filter: Optional[str]
    provider_filter: Optional[str]
    at_commit: Optional[str]
    language: Optional[str]
    exclude_language: Optional[str]
    exclude_path: Optional[str]
    diff_types: Optional[Tuple[str, ...]]
    author: Optional[str]
    chunk_type: Optional[str]
    no_embedding_cache_shortcut: bool
    temporal_embedder: Optional[str]
    rerank_query: Optional[str]
    rerank_instruction: Optional[str]
    # Explicitly-named "not forwarded" fields, documenting the intentional
    # parity gap with today's _execute_temporal_query (both already
    # silently ignore these two for temporal queries):
    min_score_ignored_for_temporal: Optional[float]
    file_extensions_ignored_for_temporal: Optional[Tuple[str, ...]]
