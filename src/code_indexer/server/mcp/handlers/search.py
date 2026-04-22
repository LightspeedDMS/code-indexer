"""Search handlers -- semantic search, regex search, cached content.

Domain module for search handlers. Part of the handlers package
modularization (Story #496).

NOTE: Functions in this module were extracted verbatim from _legacy.py.
Pre-existing method lengths and duplication are preserved intentionally
to avoid behavioral changes during extraction.  Refactoring is tracked
separately.
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import asyncio
import logging
import time
from typing import Dict, Any, List, Optional, cast
from pathlib import Path

from code_indexer.server.auth.user_manager import User
from . import _utils
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.api_metrics_service import api_metrics_service
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.mcp import reranking as _mcp_reranking
from code_indexer.server.mcp.memory_retrieval_pipeline import (
    MemoryRetrievalPipeline,
    MemoryRetrievalPipelineConfig,
    _build_empty_nudge_entry,
    _hydrate_memory_bodies,
)
from ._utils import (
    CapBreach,
    cap_breach_response,
    _mcp_response,
    _coerce_int,
    _coerce_float,
    _parse_json_string_array,
    _apply_payload_truncation,
    _apply_fts_payload_truncation,
    _apply_regex_payload_truncation,
    _apply_temporal_payload_truncation,
    _error_with_suggestions,
    _get_available_repos,
    _format_omni_response,
    _is_temporal_query,
    _get_temporal_status,
    _expand_wildcard_patterns,
    _get_query_tracker,
    _get_access_filtering_service,
    _list_global_repos,
    _get_golden_repos_dir,
    _get_wiki_enabled_repos,
    _enrich_with_wiki_url,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Story #653 AC3: Constants used by reranking helpers (also used in git_read.py)
# ---------------------------------------------------------------------------
_DEFAULT_OVERFETCH_MULTIPLIER = 5
_DEFAULT_SEARCH_LIMIT = 10
_DEFAULT_MIN_SCORE = 0.3
_DEFAULT_EDIT_DISTANCE = 0
_DEFAULT_SNIPPET_LINES = 5
_DEFAULT_REGEX_MAX_RESULTS = 100
_DEFAULT_REGEX_CONTEXT_LINES = 0
# Filesystem subdirectory name for the memory HNSW index (Story #883).
_CIDX_META_DIR_NAME = "cidx-meta"

# Bug #881 Phase 1: query_text is truncated to this length in INFO logs to avoid
# logging potentially large or sensitive query strings at INFO level.
_QUERY_LOG_TRUNCATION_LIMIT = 100

# Bug #881 Phase 1: max number of expanded aliases shown in the omni post-expansion log.
_OMNI_LOG_MAX_ALIASES_SHOWN = 10


def _get_legacy():
    from . import _legacy

    return _legacy


def _load_category_map(caller_label: str) -> dict:
    """Load category map from golden_repo_manager, returning empty dict on failure."""
    category_map: dict = {}
    try:
        if (
            hasattr(_utils.app_module, "golden_repo_manager")
            and _utils.app_module.golden_repo_manager
        ):
            category_service = getattr(
                _utils.app_module.golden_repo_manager, "_repo_category_service", None
            )
            if category_service:
                category_map = category_service.get_repo_category_map()
    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-036",
                f"Failed to load category map in {caller_label}: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
    return category_map


def _filter_errors_for_user(errors: dict, user: User) -> dict:
    """Filter errors dict to hide unauthorized repo aliases (Story #331 AC7)."""
    access_service = _get_access_filtering_service()
    if access_service and not access_service.is_admin_user(user.username):
        accessible = access_service.get_accessible_repos(user.username)
        return {
            k: v
            for k, v in errors.items()
            if k.removesuffix("-global") in accessible or k in accessible
        }
    return errors


def _apply_search_truncation(
    results: list, search_mode: str, params: Dict[str, Any]
) -> list:
    """Apply payload truncation based on search mode."""
    if search_mode in ["fts", "hybrid"]:
        return _apply_fts_payload_truncation(results)
    elif _is_temporal_query(params):
        return _apply_temporal_payload_truncation(results)
    else:
        return _apply_payload_truncation(results)


def _resolve_search_type(params: Dict[str, Any], user: User) -> str:
    """Determine search type from params and track API metrics.

    Invalid search_mode values are coerced to "semantic" (preserved legacy behavior).
    """
    search_mode = params.get("search_mode", "semantic")
    search_type = (
        search_mode if search_mode in ["semantic", "fts", "regex"] else "semantic"
    )
    if _is_temporal_query(params):
        search_type = "temporal"

    if search_type == "semantic":
        api_metrics_service.increment_semantic_search(username=user.username)
    elif search_type == "regex":
        api_metrics_service.increment_regex_search(username=user.username)
    else:
        api_metrics_service.increment_other_index_search(username=user.username)
    return search_type


def _compute_effective_limit(requested_limit: int, user: User) -> int:
    """Calculate over-fetch limit for access filtering (Story #300).

    Args:
        requested_limit: pre-validated non-negative int from _coerce_int(default=10)
    """
    access_svc = _get_access_filtering_service()
    if access_svc and not access_svc.is_admin_user(user.username):
        return access_svc.calculate_over_fetch_limit(requested_limit)  # type: ignore[no-any-return]  # service returns int but mypy sees Any
    return requested_limit


def _empty_omni_response(
    errors: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Build the empty multi-repo search response payload."""
    return _mcp_response(
        {
            "success": True,
            "results": {
                "cursor": "",
                "total_results": 0,
                "total_repos_searched": 0,
                "results": [],
                "errors": errors or {},
            },
        }
    )


def _build_multi_search_request(
    repo_aliases: list,
    params: Dict[str, Any],
    search_type: str,
    limit: int,
) -> Any:  # Returns MultiSearchRequest — local import, not in module type contract
    """Build a MultiSearchRequest from MCP params.

    Args:
        limit: pre-validated via _coerce_int(default=10) and _compute_effective_limit
    """
    from ...multi.models import MultiSearchRequest

    return MultiSearchRequest(  # type: ignore[arg-type]  # search_type validated to Literal values by _resolve_search_type
        repositories=repo_aliases,
        query=params.get("query_text", ""),
        search_type=search_type,  # type: ignore[arg-type]
        limit=limit,
        min_score=(
            _coerce_float(params.get("min_score"), 0.0)
            if params.get("min_score") is not None
            else None
        ),
        language=params.get("language"),
        path_filter=params.get("path_filter"),
        exclude_language=params.get("exclude_language"),
        exclude_path=params.get("exclude_path"),
        accuracy=params.get("accuracy", "balanced"),
    )


def _flatten_multi_results(
    response: Any,  # MultiSearchResponse — local import in caller
    category_map: dict,
    wiki_enabled_repos: set,
) -> list:
    """Flatten MultiSearchResponse into a flat list with source_repo."""
    all_results = []
    for repo_alias, repo_results in response.results.items():
        for result in repo_results:
            result["source_repo"] = repo_alias
            if "score" in result and "similarity_score" not in result:
                result["similarity_score"] = result["score"]

            golden_alias = repo_alias.removesuffix("-global") if repo_alias else None
            if golden_alias:
                category_info = category_map.get(golden_alias, {})
                result["repo_category"] = category_info.get("category_name")

            _enrich_with_wiki_url(
                result,
                result.get("file_path", ""),
                repo_alias,
                wiki_enabled_repos,
            )
            all_results.append(result)
    return all_results


def _aggregate_results(
    all_results: list, aggregation_mode: str, requested_limit: int
) -> list:
    """Aggregate results based on per_repo or global mode.

    Args:
        requested_limit: pre-validated non-negative int from _coerce_int(default=10)
    """
    from collections import defaultdict

    if aggregation_mode == "per_repo":
        results_by_repo = defaultdict(list)
        for r in all_results:
            results_by_repo[r.get("source_repo", "unknown")].append(r)

        for repo in results_by_repo:
            results_by_repo[repo].sort(
                key=lambda x: x.get("similarity_score", x.get("score", 0)),
                reverse=True,
            )

        num_repos = len(results_by_repo)
        if num_repos > 0:
            per_repo_limit = requested_limit // num_repos
            remainder = requested_limit % num_repos
            final_results = []
            for i, (_repo, results) in enumerate(results_by_repo.items()):
                repo_limit = per_repo_limit + (1 if i < remainder else 0)
                final_results.extend(results[:repo_limit])
        else:
            final_results = []
    else:
        all_results.sort(
            key=lambda x: x.get("similarity_score", x.get("score", 0)),
            reverse=True,
        )
        final_results = all_results[:requested_limit]
    return final_results


def _omni_search_code(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handle omni-search across multiple repositories.

    Called when repository_alias is an array of repository names.
    Story #36: Uses MultiSearchService for parallel execution.
    Story #51: Synchronous for FastAPI thread pool execution.
    """
    from ...multi.multi_search_config import MultiSearchConfig
    from ...multi.multi_search_service import MultiSearchService

    repo_aliases = _expand_wildcard_patterns(params.get("repository_alias", []), user)
    if isinstance(repo_aliases, CapBreach):
        return cap_breach_response(repo_aliases)
    requested_limit = _coerce_int(params.get("limit"), _DEFAULT_SEARCH_LIMIT)
    aggregation_mode = params.get(
        "aggregation_mode", "per_repo" if len(repo_aliases) > 1 else "global"
    )

    if not repo_aliases:
        return _empty_omni_response()

    # Bug #881 Phase 1 post-expansion log: operators can audit fan-out factor
    _aliases_preview = repo_aliases[:_OMNI_LOG_MAX_ALIASES_SHOWN]
    _elided = len(repo_aliases) - len(_aliases_preview)
    _aliases_display = (
        repr(_aliases_preview) + f" ... and {_elided} more"
        if _elided > 0
        else repr(_aliases_preview)
    )
    logger.info(
        f"_omni_search_code post-expansion: user={user.username!r} "
        f"correlation_id={get_correlation_id()!r} "
        f"expanded_count={len(repo_aliases)} "
        f"expanded_aliases={_aliases_display}",
        extra={"correlation_id": get_correlation_id()},
    )

    search_type = _resolve_search_type(params, user)
    effective_limit = _compute_effective_limit(requested_limit, user)
    request = _build_multi_search_request(
        repo_aliases, params, search_type, effective_limit
    )

    config = MultiSearchConfig.from_config(get_config_service())
    service = MultiSearchService(config)
    try:
        response = service.search(request)
    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-031",
                f"MultiSearchService failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _empty_omni_response(errors={"service_error": str(e)})

    category_map = _load_category_map("_omni_search_code")
    all_results = _flatten_multi_results(
        response, category_map, _get_wiki_enabled_repos()
    )
    errors = _filter_errors_for_user(response.errors or {}, user)
    final_results = _aggregate_results(all_results, aggregation_mode, requested_limit)

    response_format = params.get(
        "response_format", "grouped" if len(repo_aliases) > 1 else "flat"
    )
    search_mode = params.get("search_mode", "semantic")
    if final_results:
        final_results = _apply_search_truncation(final_results, search_mode, params)

    access_filtering_service = _get_access_filtering_service()
    if access_filtering_service:
        final_results = access_filtering_service.filter_query_results(
            final_results, user.username
        )
        final_results = final_results[:requested_limit]

    formatted = _format_omni_response(
        all_results=final_results,
        response_format=response_format,
        total_repos_searched=response.metadata.total_repos_searched,
        errors=errors,
        cursor="",
    )

    if _is_temporal_query(params):
        temporal_status = _get_temporal_status(repo_aliases)
        if temporal_status:
            formatted["temporal_status"] = temporal_status

    return _mcp_response({"success": True, "results": formatted})


def _compute_rerank_limit(
    params: Dict[str, Any], requested_limit: int, effective_limit: int
) -> int:
    """Calculate overfetch limit when reranking is active (Story #653 AC3).

    Args:
        requested_limit: pre-validated via _coerce_int
        effective_limit: pre-computed via _compute_effective_limit
    """
    if not params.get("rerank_query"):
        return effective_limit
    rc = get_config_service().get_config().rerank_config
    overfetch_mul = rc.overfetch_multiplier if rc else _DEFAULT_OVERFETCH_MULTIPLIER
    access_filter_extra = effective_limit - requested_limit
    return _mcp_reranking.calculate_overfetch_limit(  # type: ignore[no-any-return]  # returns int
        requested_limit, overfetch_mul, access_filter_extra
    )


def _enrich_results_with_category(
    results: list,
    category_map: dict,
    wiki_enabled_repos: set,
    repository_alias: str,
) -> None:
    """Enrich results with category info and wiki URLs (Story #182, #292).

    Modifies results in place.
    """
    golden_alias = (
        repository_alias.removesuffix("-global") if repository_alias else None
    )
    for res in results:
        if golden_alias:
            category_info = category_map.get(golden_alias, {})
            res["repo_category"] = category_info.get("category_name")
        _enrich_with_wiki_url(
            res,
            res.get("file_path", ""),
            repository_alias,
            wiki_enabled_repos,
        )


def _apply_rerank_and_filter(
    results: list,
    params: Dict[str, Any],
    requested_limit: int,
    repository_alias: Optional[str],
    user: User,
) -> tuple:
    """Apply reranking, truncation, and access filtering to search results.

    Returns (filtered_results, rerank_meta).
    """
    rerank_query = params.get("rerank_query")
    rerank_instruction = params.get("rerank_instruction")
    results, rerank_meta = _mcp_reranking._apply_reranking_sync(
        results=results,
        rerank_query=rerank_query,
        rerank_instruction=rerank_instruction,
        content_extractor=lambda r: r.get("content", "") or r.get("code_snippet", ""),
        requested_limit=requested_limit,
        config_service=get_config_service(),
    )

    search_mode = params.get("search_mode", "semantic")
    results = _apply_search_truncation(results, search_mode, params)

    access_filtering_service = _get_access_filtering_service()
    if access_filtering_service:
        results = access_filtering_service.filter_query_results(results, user.username)
        if repository_alias and "cidx-meta" in repository_alias:
            results = access_filtering_service.filter_cidx_meta_results(
                results, user.username
            )
        results = results[:requested_limit]

    return results, rerank_meta


# Modes that trigger memory retrieval alongside code search (Story #883).
_MEMORY_SEMANTIC_MODES = frozenset({"semantic", "hybrid"})


def _compute_memory_query_vector(query_text: str) -> List[float]:
    """Compute a Voyage embedding for query_text using VoyageAIClient.

    Uses VoyageAIClient(VoyageAIConfig()) which picks up VOYAGE_API_KEY from
    the environment — the same provider and key used by code search internally.
    This is called once per request and the vector is shared with memory
    retrieval (GAP 1: zero duplicate Voyage API calls).

    Returns:
        A non-empty list of floats on success.
        An empty list on any error (logged at WARNING); the caller must check
        for emptiness and skip memory retrieval when [] is returned.
    """
    try:
        from code_indexer.config import VoyageAIConfig
        from code_indexer.services.voyage_ai import VoyageAIClient

        provider = VoyageAIClient(VoyageAIConfig())
        # cast: EmbeddingProvider.get_embedding is typed as returning Any upstream
        # (broad protocol), but for VoyageAIClient it always yields a List[float].
        return cast(
            List[float], provider.get_embedding(query_text, embedding_purpose="query")
        )
    except Exception as exc:
        logger.warning(
            "Memory retrieval: could not compute query vector — %s. "
            "Memory retrieval skipped for this request.",
            exc,
        )
        return []


# Story #883 Phase C: shared alias so callers are explicit about the intent.
# _compute_shared_query_vector is the single Voyage call per semantic request;
# the resulting vector is reused by both code search and memory retrieval.
_compute_shared_query_vector = _compute_memory_query_vector


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
        query_vector = _compute_memory_query_vector(query_text)
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
        List[Dict[str, Any]], _hydrate_memory_bodies(floor_filtered, store_base_path)
    )

    # GAP 3: inject empty-state nudge when no memories survived all filters.
    if not hydrated:
        return [_build_empty_nudge_entry()]

    return hydrated


def _repo_lookup_error(
    user: User, error_msg: str, attempted_value: str
) -> Dict[str, Any]:
    """Build MCP error response for repo-not-found with suggestions."""
    available_repos = _get_available_repos(user)
    error_envelope = _error_with_suggestions(
        error_msg=error_msg,
        attempted_value=attempted_value,
        available_values=available_repos,
    )
    error_envelope["results"] = []
    return _mcp_response(error_envelope)


def _resolve_global_repo_target(repository_alias: str, user: User) -> tuple:
    """Resolve a global repo alias to (repo_entry, target_path) or return error.

    Returns:
        (repo_entry, target_path, None) on success.
        (None, None, mcp_error_response) on failure.
    """
    from code_indexer.global_repos.alias_manager import AliasManager

    golden_repos_dir = _get_golden_repos_dir()
    global_repos = _list_global_repos()

    repo_entry = next(
        (r for r in global_repos if r["alias_name"] == repository_alias), None
    )
    if not repo_entry:
        err = _repo_lookup_error(
            user,
            f"Global repository '{repository_alias}' not found",
            repository_alias,
        )
        return None, None, err

    alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
    target_path = alias_manager.read_alias(repository_alias)
    if not target_path:
        err = _repo_lookup_error(
            user,
            f"Alias for '{repository_alias}' not found",
            repository_alias,
        )
        return None, None, err

    if not Path(target_path).exists():
        raise FileNotFoundError(
            f"Global repository '{repository_alias}' not found at {target_path}"
        )

    return repo_entry, target_path, None


def _build_search_kwargs(
    params: Dict[str, Any], user: User, user_repos: list, limit: int
) -> dict:
    """Build kwargs dict for SemanticQueryManager._perform_search from MCP params.

    The ~30 parameters are required by the _perform_search API and cannot
    be reduced without changing the SemanticQueryManager interface.
    """
    evolution_limit_raw = params.get("evolution_limit")
    return dict(
        username=user.username,
        user_repos=user_repos,
        query_text=params["query_text"],
        limit=limit,
        min_score=_coerce_float(params.get("min_score"), _DEFAULT_MIN_SCORE),
        file_extensions=params.get("file_extensions"),
        language=params.get("language"),
        exclude_language=params.get("exclude_language"),
        path_filter=params.get("path_filter"),
        exclude_path=params.get("exclude_path"),
        accuracy=params.get("accuracy", "balanced"),
        search_mode=params.get("search_mode", "semantic"),
        time_range=params.get("time_range"),
        time_range_all=params.get("time_range_all", False),
        at_commit=params.get("at_commit"),
        include_removed=params.get("include_removed", False),
        show_evolution=params.get("show_evolution", False),
        evolution_limit=(
            _coerce_int(evolution_limit_raw, _DEFAULT_EDIT_DISTANCE)
            if evolution_limit_raw is not None
            else None
        ),
        case_sensitive=params.get("case_sensitive", False),
        fuzzy=params.get("fuzzy", False),
        edit_distance=_coerce_int(params.get("edit_distance"), _DEFAULT_EDIT_DISTANCE),
        snippet_lines=_coerce_int(params.get("snippet_lines"), _DEFAULT_SNIPPET_LINES),
        regex=params.get("regex", False),
        diff_type=params.get("diff_type"),
        author=params.get("author"),
        chunk_type=params.get("chunk_type"),
        query_strategy=params.get("query_strategy"),
        score_fusion=params.get("score_fusion"),
        preferred_provider=params.get("preferred_provider"),
    )


def _execute_tracked_search(
    params: Dict[str, Any],
    user: User,
    user_repos: list,
    limit: int,
    index_path: Optional[str] = None,
) -> tuple:
    """Execute _perform_search with query-tracker ref counting and timing.

    Args:
        limit: must be > 0; raises ValueError otherwise.

    Returns:
        (results, execution_time_ms, timeout_occurred)
    """
    if limit <= 0:
        raise ValueError(f"limit must be > 0, got {limit}")

    query_tracker = _get_query_tracker()
    kwargs = _build_search_kwargs(params, user, user_repos, limit)
    start_time = time.time()
    timeout_occurred = False
    ref_incremented = False
    try:
        if query_tracker is not None and index_path:
            query_tracker.increment_ref(index_path)
            ref_incremented = True
        results = _utils.app_module.semantic_query_manager._perform_search(**kwargs)
    except TimeoutError as e:
        timeout_occurred = True
        raise Exception(f"Query timed out: {e}") from e
    except Exception as e:
        if "timeout" in str(e).lower():
            raise Exception(f"Query timed out: {e}") from e
        raise
    finally:
        execution_time_ms = int((time.time() - start_time) * 1000)
        if ref_incremented and query_tracker is not None and index_path:
            query_tracker.decrement_ref(index_path)

    return results, execution_time_ms, timeout_occurred


def _search_global_repo(
    params: Dict[str, Any], user: User, repository_alias: str
) -> Dict[str, Any]:
    """Handle search against a global repository (ends with -global).

    Extracted from search_code global-repo branch (_legacy.py lines 373-634).
    """
    repo_entry, target_path, err = _resolve_global_repo_target(repository_alias, user)
    if err is not None:
        return err  # type: ignore[no-any-return]  # err is dict from _mcp_response but mypy sees Any

    mock_user_repos = [
        {
            "user_alias": repository_alias,
            "repo_path": str(Path(target_path)),
            "actual_repo_id": repo_entry["repo_name"],
        }
    ]

    requested_limit = _coerce_int(params.get("limit"), _DEFAULT_SEARCH_LIMIT)
    effective_limit = _compute_effective_limit(requested_limit, user)
    effective_limit = _compute_rerank_limit(params, requested_limit, effective_limit)

    results, execution_time_ms, timeout_occurred = _execute_tracked_search(
        params, user, mock_user_repos, effective_limit, index_path=target_path
    )

    category_map = _load_category_map("search_code")
    wiki_enabled_repos = _get_wiki_enabled_repos()
    response_results = [r.to_dict() for r in results]
    for rd in response_results:
        rd["source_repo"] = repository_alias
    _enrich_results_with_category(
        response_results, category_map, wiki_enabled_repos, repository_alias
    )
    response_results, rerank_meta = _apply_rerank_and_filter(
        response_results, params, requested_limit, repository_alias, user
    )

    return _mcp_response(
        {
            "success": True,
            "results": {
                "results": response_results,
                "total_results": len(response_results),
                "query_metadata": {
                    "query_text": params["query_text"],
                    "execution_time_ms": execution_time_ms,
                    "repositories_searched": 1,
                    "timeout_occurred": timeout_occurred,
                    "reranker_used": rerank_meta["reranker_used"],
                    "reranker_provider": rerank_meta["reranker_provider"],
                    "rerank_time_ms": rerank_meta["rerank_time_ms"],
                },
            },
        }
    )


def _enrich_activated_results(result: dict, params: Dict[str, Any]) -> None:
    """Enrich activated-repo search results with category info (Story #182).

    Modifies result dict in place.
    """
    category_map = _load_category_map("search_code (activated)")
    if "results" not in result or not isinstance(result["results"], list):
        return
    for res in result["results"]:
        repo_alias = res.get("source_repo") or res.get("repository_alias")
        if repo_alias:
            golden_alias = repo_alias.removesuffix("-global")
            category_info = category_map.get(golden_alias, {})
            res["repo_category"] = category_info.get("category_name")


def _search_activated_repo(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handle search against an activated (non-global) repository.

    Extracted from search_code activated-repo branch (_legacy.py lines 636-791).

    Story #883 Phase C: when search_mode is semantic/hybrid and memory retrieval is
    enabled, the Voyage embedding vector is computed ONCE here via
    _compute_shared_query_vector and reused by both code search (via
    precomputed_query_vector kwarg to query_user_repositories) and memory retrieval
    (via query_vector kwarg to _run_memory_retrieval).  This guarantees exactly one
    Voyage API call per semantic request regardless of whether memories are retrieved.
    """
    requested_limit = _coerce_int(params.get("limit"), _DEFAULT_SEARCH_LIMIT)
    effective_limit = _compute_effective_limit(requested_limit, user)
    effective_limit = _compute_rerank_limit(params, requested_limit, effective_limit)

    # Story #883 Phase C: compute shared Voyage vector once for semantic/hybrid modes
    # when memory retrieval is enabled.  The same vector is threaded through to both
    # code search and memory retrieval so only one Voyage API call is made per request.
    search_mode = params.get("search_mode", "semantic")
    config_service = get_config_service()
    shared_query_vector: Optional[List[float]] = None
    if search_mode in _MEMORY_SEMANTIC_MODES:
        mem_cfg = config_service.get_config().memory_retrieval_config
        if mem_cfg.memory_retrieval_enabled:
            query_text = params.get("query_text", "") or ""
            shared_query_vector = _compute_shared_query_vector(str(query_text))

    kwargs = _build_search_kwargs(params, user, [], effective_limit)
    # query_user_repositories uses repository_alias, not user_repos
    del kwargs["user_repos"]
    kwargs["repository_alias"] = params.get("repository_alias")
    kwargs["precomputed_query_vector"] = shared_query_vector
    result = _utils.app_module.semantic_query_manager.query_user_repositories(**kwargs)

    _enrich_activated_results(result, params)

    if "results" in result and isinstance(result["results"], list):
        result["results"], rerank_meta = _apply_rerank_and_filter(
            result["results"],
            params,
            requested_limit,
            params.get("repository_alias"),
            user,
        )
        result["total_results"] = len(result["results"])
        qm: dict = result.setdefault("query_metadata", {})  # type: ignore[assignment]  # setdefault returns Any
        qm["reranker_used"] = rerank_meta["reranker_used"]
        qm["reranker_provider"] = rerank_meta["reranker_provider"]
        qm["rerank_time_ms"] = rerank_meta["rerank_time_ms"]
        # Story #883: parallel memory retrieval — reranker_status extracted here so
        # _run_memory_retrieval receives a plain string, not a nested dict.
        reranker_status: str = rerank_meta["reranker_status"]["status"]
        # Story #883 Phase C: pass shared vector to avoid second Voyage API call
        relevant_memories = _run_memory_retrieval(
            params,
            user,
            config_service,
            reranker_status,
            query_vector=shared_query_vector,
        )
        if relevant_memories is not None:
            qm["relevant_memories"] = relevant_memories

    return _mcp_response({"success": True, "results": result})


def search_code(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Search code using semantic search, FTS, or hybrid mode.

    Routes to the appropriate handler based on repository_alias type:
    - list: _omni_search_code (multi-repo)
    - str ending with -global: _search_global_repo
    - str (other): _search_activated_repo
    """
    import time

    _search_start = time.monotonic()
    try:
        repository_alias = params.get("repository_alias")
        repository_alias = _parse_json_string_array(repository_alias)
        params["repository_alias"] = repository_alias

        # Bug #881 Phase 1 entry log: operators can audit every search_code call
        _query_log = str(params.get("query_text", ""))[:_QUERY_LOG_TRUNCATION_LIMIT]
        logger.info(
            f"search_code entry: user={user.username!r} "
            f"correlation_id={get_correlation_id()!r} "
            f"repository_alias={repository_alias!r} "
            f"limit={params.get('limit')!r} "
            f"accuracy={params.get('accuracy')!r} "
            f"query_text={_query_log!r}",
            extra={"correlation_id": get_correlation_id()},
        )

        if isinstance(repository_alias, list):
            _result = _omni_search_code(params, user)
        elif repository_alias and repository_alias.endswith("-global"):
            _result = _search_global_repo(params, user, repository_alias)
        else:
            _result = _search_activated_repo(params, user)

        # Bug #881 Phase 1 exit log: elapsed_ms and result_count for every call
        _elapsed_ms = int((time.monotonic() - _search_start) * 1000)
        logger.info(
            f"search_code complete: correlation_id={get_correlation_id()!r} "
            f"result_count=0 elapsed_ms={_elapsed_ms}ms",
            extra={"correlation_id": get_correlation_id()},
        )
        return _result
    except Exception as e:
        logger.exception(
            f"Error in search_code: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e), "results": []})


async def _omni_regex_search(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handle omni-regex search across multiple repositories.

    Extracted from _legacy.py lines 1957-2041.
    """
    import json as json_module

    repo_aliases = _expand_wildcard_patterns(args.get("repository_alias", []), user)
    if isinstance(repo_aliases, CapBreach):
        return cap_breach_response(repo_aliases)

    if not repo_aliases:
        return _mcp_response(
            {
                "success": True,
                "matches": [],
                "total_matches": 0,
                "truncated": False,
                "search_engine": "ripgrep",
                "search_time_ms": 0,
                "repos_searched": 0,
                "errors": {},
            }
        )

    start_time = time.time()
    all_matches: list = []
    errors: dict = {}
    repos_searched = 0
    truncated = False

    for repo_alias in repo_aliases:
        try:
            single_args = dict(args)
            single_args["repository_alias"] = repo_alias
            single_result = await handle_regex_search(single_args, user)

            content = single_result.get("content", [])
            if content and content[0].get("type") == "text":
                result_data = json_module.loads(content[0]["text"])
                if result_data.get("success"):
                    repos_searched += 1
                    matches = result_data.get("matches", [])
                    for m in matches:
                        m["source_repo"] = repo_alias
                    all_matches.extend(matches)
                    if result_data.get("truncated"):
                        truncated = True
                else:
                    errors[repo_alias] = result_data.get("error", "Unknown error")
        except Exception as e:
            errors[repo_alias] = str(e)
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-039",
                    f"Omni-regex failed for {repo_alias}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

    elapsed_ms = int((time.time() - start_time) * 1000)
    errors = _filter_errors_for_user(errors, user)

    response_format = args.get("response_format", "flat")
    formatted = _format_omni_response(
        all_results=all_matches,
        response_format=response_format,
        total_repos_searched=repos_searched,
        errors=errors,
    )
    formatted["truncated"] = truncated
    formatted["search_engine"] = "ripgrep"
    formatted["search_time_ms"] = elapsed_ms
    if response_format == "flat":
        formatted["matches"] = formatted.pop("results")
        formatted["total_matches"] = formatted.pop("total_results")
        formatted["repos_searched"] = formatted.pop("total_repos_searched")
    return _mcp_response(formatted)


def _validate_regex_args(args: Dict[str, Any]) -> tuple:
    """Validate and normalize regex search arguments.

    Returns:
        (repository_alias, None) on success.
        (None, mcp_error_response) on validation failure.
    """
    include_patterns = args.get("include_patterns")
    if include_patterns is not None and not isinstance(include_patterns, list):
        return None, _mcp_response(
            {"success": False, "error": "include_patterns must be a list of strings"}
        )

    exclude_patterns = args.get("exclude_patterns")
    if exclude_patterns is not None and not isinstance(exclude_patterns, list):
        return None, _mcp_response(
            {"success": False, "error": "exclude_patterns must be a list of strings"}
        )

    repository_alias = _parse_json_string_array(args.get("repository_alias"))
    args["repository_alias"] = repository_alias

    if not args.get("pattern"):
        return None, _mcp_response(
            {"success": False, "error": "Missing required parameter: pattern"}
        )

    if isinstance(repository_alias, list):
        if not repository_alias:
            return None, _mcp_response(
                {"success": False, "error": "repository_alias list must not be empty"}
            )
    elif not repository_alias:
        return None, _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    return repository_alias, None


async def _execute_regex_search(
    args: Dict[str, Any],
    repo_path: Path,
    repository_alias: str,
    user: User,
) -> tuple:
    """Execute regex search, enrich, rerank, truncate, and filter results.

    Returns:
        (matches, rerank_meta, search_result) where search_result is the
        raw RegexSearchResult dataclass.
    """
    from code_indexer.global_repos.regex_search import RegexSearchService

    config = get_config_service().get_config()
    search_limits = config.search_limits_config
    subprocess_max_workers = config.background_jobs_config.subprocess_max_workers
    max_results = max(
        1, _coerce_int(args.get("max_results"), _DEFAULT_REGEX_MAX_RESULTS)
    )
    context_lines = max(
        0, _coerce_int(args.get("context_lines"), _DEFAULT_REGEX_CONTEXT_LINES)
    )

    service = RegexSearchService(
        repo_path, subprocess_max_workers=subprocess_max_workers
    )
    search_result = await service.search(
        pattern=args["pattern"],
        path=args.get("path"),
        include_patterns=args.get("include_patterns"),
        exclude_patterns=args.get("exclude_patterns"),
        case_sensitive=args.get("case_sensitive", True),
        context_lines=context_lines,
        max_results=max_results,
        timeout_seconds=search_limits.timeout_seconds,
        multiline=args.get("multiline", False),
        pcre2=args.get("pcre2", False),
    )

    matches = [
        {
            "file_path": m.file_path,
            "line_number": m.line_number,
            "column": m.column,
            "line_content": m.line_content,
            "context_before": m.context_before,
            "context_after": m.context_after,
        }
        for m in search_result.matches
    ]

    wiki_enabled_repos = _get_wiki_enabled_repos()
    for match in matches:
        _enrich_with_wiki_url(
            match,
            match.get("file_path", ""),
            repository_alias,
            wiki_enabled_repos,
        )

    regex_limit = _coerce_int(args.get("max_results"), len(matches))
    rerank_kwargs = dict(
        results=matches,
        rerank_query=args.get("rerank_query"),
        rerank_instruction=args.get("rerank_instruction"),
        content_extractor=lambda r: r.get("line_content", "") or "",
        requested_limit=regex_limit,
        config_service=get_config_service(),
    )
    matches, rerank_meta = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _mcp_reranking._apply_reranking_sync(**rerank_kwargs)
    )

    matches = _apply_regex_payload_truncation(matches)

    if repository_alias and "cidx-meta" in repository_alias:
        access_svc = _get_access_filtering_service()
        if access_svc:
            filenames = [Path(m["file_path"]).name for m in matches]
            allowed = set(access_svc.filter_cidx_meta_files(filenames, user.username))
            matches = [m for m in matches if Path(m["file_path"]).name in allowed]

    return matches, rerank_meta, search_result


async def handle_regex_search(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for regex_search tool - pattern matching with timeout protection.

    Extracted from _legacy.py lines 2044-2220.
    """
    from code_indexer.server.services.search_error_formatter import SearchErrorFormatter

    repository_alias, err = _validate_regex_args(args)
    if err is not None:
        return err  # type: ignore[no-any-return]  # err is dict from _mcp_response

    if isinstance(repository_alias, list):
        return await _omni_regex_search(args, user)

    # Bug #721: regex_search is in _SELF_TRACKING_TOOLS, so handler must track itself.
    # Placed after the omni branch: _omni_regex_search recurses into handle_regex_search
    # once per repo with a single alias, so each recursive call increments here —
    # giving exactly N increments for N repos, with no double-count at the omni entry.
    api_metrics_service.increment_regex_search(username=user.username)

    try:
        golden_repos_dir = _get_golden_repos_dir()
        resolved = _get_legacy()._resolve_repo_path(repository_alias, golden_repos_dir)
        if not resolved:
            return _mcp_response(
                {
                    "success": False,
                    "error": f"Repository '{repository_alias}' not found",
                }
            )

        matches, rerank_meta, sr = await _execute_regex_search(
            args, Path(resolved), repository_alias, user
        )
        return _mcp_response(
            {
                "success": True,
                "matches": matches,
                "total_matches": len(matches),
                "truncated": sr.truncated,
                "search_engine": sr.search_engine,
                "search_time_ms": sr.search_time_ms,
                "query_metadata": {
                    "reranker_used": rerank_meta["reranker_used"],
                    "reranker_provider": rerank_meta["reranker_provider"],
                    "rerank_time_ms": rerank_meta["rerank_time_ms"],
                },
            }
        )
    except TimeoutError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-040",
                f"Search timeout in regex_search: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        error_formatter = SearchErrorFormatter()
        search_limits = get_config_service().get_config().search_limits_config
        error_data = error_formatter.format_timeout_error(
            timeout_seconds=search_limits.timeout_seconds,
            partial_results=None,
        )
        return _mcp_response({"success": False, **error_data})
    except Exception as e:
        logger.exception(
            f"Error in regex_search: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_get_cached_content(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for get_cached_content tool.

    Retrieves cached content by handle with pagination support.
    Implements AC5 of Story #679.
    """
    # Story #331 AC8: Accepted risk - cache handles are UUID4 (unguessable)
    # and short-lived (TTL-based). Cross-user cache access requires knowing
    # the exact UUID, which is not feasible. Full user-scoping tracking
    # would add complexity without meaningful security benefit.
    from code_indexer.server.cache.payload_cache import CacheNotFoundError

    handle = args.get("handle")
    page = max(0, _coerce_int(args.get("page"), 0))

    if not handle:
        return _mcp_response(
            {
                "success": False,
                "error": "Missing required parameter: handle",
            }
        )

    payload_cache = getattr(_utils.app_module.app.state, "payload_cache", None)
    if payload_cache is None:
        return _mcp_response(
            {
                "success": False,
                "error": "Cache service not available",
            }
        )

    try:
        result = payload_cache.retrieve(handle, page=page)
        return _mcp_response(
            {
                "success": True,
                "content": result.content,
                "page": result.page,
                "total_pages": result.total_pages,
                "has_more": result.has_more,
            }
        )
    except CacheNotFoundError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-117",
                f"Cache handle not found or expired: {handle}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "cache_expired",
                "message": str(e),
                "handle": handle,
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in get_cached_content: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def _register(registry: dict) -> None:
    """Register search handlers in the HANDLER_REGISTRY."""
    registry["search_code"] = search_code
    registry["regex_search"] = handle_regex_search
    registry["get_cached_content"] = handle_get_cached_content
