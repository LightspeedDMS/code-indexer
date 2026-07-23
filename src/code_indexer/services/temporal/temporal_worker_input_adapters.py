"""MCP Dict / REST SemanticQueryRequest -> TemporalWorkerInput adapters.

Story #1400 Phase 3 (FINAL LOCKED DESIGN). Both doors intercept the
temporal branch AFTER their existing alias-promotion logic (MCP's
bare-to-global fallback) and normalize into ONE shared TemporalWorkerInput
via these adapters -- never a vague "filters"/"temporal_params" catch-all.

Alias/rejection rules:
- Missing alias on either door -> reject (temporal requires an explicit
  single repo in v1). error_code TEMPORAL_ALIAS_REQUIRED.
- Any MCP list-typed alias, including a single-element list, is rejected
  with a DISTINCT error_code TEMPORAL_SINGLE_REPO_REQUIRED -- this removes
  list-vs-string ambiguity from the dedup signature entirely, it is not
  merely "v1 is single-repo" prose.

fusion_fetch_limit UNIFICATION is implemented: both doors compute it via the
single shared compute_temporal_fusion_fetch_limit() (temporal_fusion_limit.py,
MCP's access-filter-aware formula adopted as the canonical implementation)
BEFORE calling into these adapters, which still accept fusion_fetch_limit as
an explicit precomputed parameter (adapters do not call the shared function
themselves -- that stays the caller's responsibility, matching each door's
own access-filtering-service/config-service wiring). Because both doors now
compute the identical value for identical logical inputs, Scenario 12's
same-node cross-door dedup join holds for genuinely identical requests.

repo_path is likewise accepted as an explicit parameter (default ""): alias
resolution (activated vs. global) happens in the CALLER via the SAME
helpers _execute_temporal_query already uses -- this module does not
reinvent alias resolution, only field mapping + validation.
"""

from typing import Any, Dict, List, Optional, Tuple, Union

from .temporal_worker_input import TemporalWorkerInput


class TemporalAliasRejectedError(Exception):
    """Raised when a temporal query's repository_alias fails the v1
    single-repo requirement. `error_code` is one of:
      TEMPORAL_ALIAS_REQUIRED       -- alias missing/empty on either door.
      TEMPORAL_SINGLE_REPO_REQUIRED -- MCP alias is list-typed (any length).
    """

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


def _canonicalize_diff_types(
    raw: Optional[Union[str, List[str]]],
) -> Optional[Tuple[str, ...]]:
    """None/empty-string/whitespace-only/[] -> None; comma-string splits;
    plain string -> one-element tuple; list stripped/deduped. Sorted for
    the dedup-signature-shared canonical form (display-order dedup is a
    caller concern if ever needed separately)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        if not raw.strip():
            return None
        if "," in raw:
            items = [p.strip() for p in raw.split(",")]
        else:
            items = [raw.strip()]
    else:
        items = [p.strip() for p in raw]
    deduped = sorted({item for item in items if item})
    return tuple(deduped) if deduped else None


def _resolve_time_range_tuple(time_range_raw: Optional[str]) -> Tuple[str, str]:
    """time_range tuple resolution mirrors _execute_temporal_query: an
    explicit time_range wins; otherwise (time_range_all, at_commit, or
    default) queries the entire git history via ALL_TIME_RANGE."""
    from .temporal_search_service import ALL_TIME_RANGE, parse_date_range

    if time_range_raw:
        return parse_date_range(time_range_raw)
    return ALL_TIME_RANGE


def _validate_alias(alias: Any) -> str:
    if isinstance(alias, list):
        raise TemporalAliasRejectedError(
            "TEMPORAL_SINGLE_REPO_REQUIRED",
            "Temporal queries require a single repository_alias string, "
            f"not a list (got {alias!r}). v1 is single-repo only.",
        )
    if not alias:
        raise TemporalAliasRejectedError(
            "TEMPORAL_ALIAS_REQUIRED",
            "Temporal queries require an explicit repository_alias.",
        )
    return str(alias)


def _validate_requested_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"requested_limit must be an integer, got {limit!r}") from exc
    if value <= 0:
        raise ValueError(f"requested_limit must be > 0, got {value}")
    return value


def _build_from_normalized(
    *,
    repo_path: str,
    alias: str,
    username: str,
    query_text: str,
    requested_limit: int,
    fusion_fetch_limit: int,
    time_range_raw: Optional[str],
    time_range_all: bool,
    file_path_filter: Optional[str],
    at_commit: Optional[str],
    language: Optional[str],
    exclude_language: Optional[str],
    exclude_path: Optional[str],
    diff_type_raw: Optional[Union[str, List[str]]],
    author: Optional[str],
    chunk_type: Optional[str],
    no_embedding_cache_shortcut: bool,
    temporal_embedder: Optional[str],
    rerank_query: Optional[str],
    rerank_instruction: Optional[str],
    min_score: Optional[float],
    file_extensions: Optional[List[str]],
) -> TemporalWorkerInput:
    """Shared TemporalWorkerInput assembly -- both door adapters extract
    their door-specific values, validate, then delegate here so the
    construction itself never drifts between doors."""
    return TemporalWorkerInput(
        repo_path=repo_path,
        repository_alias=alias,
        username=username,
        query_text=query_text,
        requested_limit=requested_limit,
        fusion_fetch_limit=fusion_fetch_limit,
        time_range=_resolve_time_range_tuple(time_range_raw),
        time_range_raw=time_range_raw,
        time_range_all=time_range_all,
        file_path_filter=file_path_filter,
        provider_filter=None,  # neither door exposes this publicly
        at_commit=at_commit,
        language=language,
        exclude_language=exclude_language,
        exclude_path=exclude_path,
        diff_types=_canonicalize_diff_types(diff_type_raw),
        author=author,
        chunk_type=chunk_type,
        no_embedding_cache_shortcut=no_embedding_cache_shortcut,
        temporal_embedder=temporal_embedder,
        rerank_query=rerank_query,
        rerank_instruction=rerank_instruction,
        min_score_ignored_for_temporal=min_score,
        file_extensions_ignored_for_temporal=(
            tuple(file_extensions) if file_extensions else None
        ),
    )


def build_temporal_worker_input_from_mcp_dict(
    params: Dict[str, Any],
    username: str,
    fusion_fetch_limit: int,
    repo_path: str = "",
) -> TemporalWorkerInput:
    """Adapt MCP search_code's params Dict into a TemporalWorkerInput.

    Called AFTER search.py's existing bare-to-global alias promotion.
    """
    alias = _validate_alias(params.get("repository_alias"))
    requested_limit = _validate_requested_limit(params.get("limit", 10))

    return _build_from_normalized(
        repo_path=repo_path,
        alias=alias,
        username=username,
        query_text=params.get("query_text", ""),
        requested_limit=requested_limit,
        fusion_fetch_limit=fusion_fetch_limit,
        time_range_raw=params.get("time_range"),
        time_range_all=bool(params.get("time_range_all", False)),
        file_path_filter=params.get("path_filter"),
        at_commit=params.get("at_commit"),
        language=params.get("language"),
        exclude_language=params.get("exclude_language"),
        exclude_path=params.get("exclude_path"),
        diff_type_raw=params.get("diff_type"),
        author=params.get("author"),
        chunk_type=params.get("chunk_type"),
        no_embedding_cache_shortcut=bool(
            params.get("no_embedding_cache_shortcut", False)
        ),
        temporal_embedder=params.get("temporal_embedder"),
        rerank_query=params.get("rerank_query"),
        rerank_instruction=params.get("rerank_instruction"),
        min_score=params.get("min_score"),
        file_extensions=None,  # MCP has no file_extensions field
    )


def build_temporal_worker_input_from_rest_request(
    request: Any,
    username: str,
    fusion_fetch_limit: int,
    repo_path: str = "",
) -> TemporalWorkerInput:
    """Adapt a REST SemanticQueryRequest (or duck-typed equivalent) into a
    TemporalWorkerInput."""
    alias = _validate_alias(getattr(request, "repository_alias", None))
    requested_limit = _validate_requested_limit(getattr(request, "limit", 10))

    return _build_from_normalized(
        repo_path=repo_path,
        alias=alias,
        username=username,
        query_text=getattr(request, "query_text", ""),
        requested_limit=requested_limit,
        fusion_fetch_limit=fusion_fetch_limit,
        time_range_raw=getattr(request, "time_range", None),
        time_range_all=bool(getattr(request, "time_range_all", False)),
        file_path_filter=getattr(request, "path_filter", None),
        at_commit=getattr(request, "at_commit", None),
        language=getattr(request, "language", None),
        exclude_language=getattr(request, "exclude_language", None),
        exclude_path=getattr(request, "exclude_path", None),
        diff_type_raw=getattr(request, "diff_type", None),
        author=getattr(request, "author", None),
        chunk_type=getattr(request, "chunk_type", None),
        no_embedding_cache_shortcut=bool(
            getattr(request, "no_embedding_cache_shortcut", False)
        ),
        temporal_embedder=getattr(request, "temporal_embedder", None),
        rerank_query=getattr(request, "rerank_query", None),
        rerank_instruction=getattr(request, "rerank_instruction", None),
        min_score=getattr(request, "min_score", None),
        file_extensions=getattr(request, "file_extensions", None),
    )
