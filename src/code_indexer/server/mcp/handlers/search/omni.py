"""Omni (multi-repository) search handlers.

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
from typing import Any, Dict, Optional

from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.telemetry.correlation_bridge import (
    get_current_correlation_id as get_correlation_id,
)

from .._utils import (
    CapBreach,
    cap_breach_response,
    _coerce_float,
    _coerce_int,
    _enforce_repo_count_cap,
    _enrich_with_wiki_url,
    _expand_wildcard_patterns,
    _format_omni_response,
    _get_access_filtering_service,
    _get_temporal_status,
    _get_wiki_enabled_repos,
    _is_temporal_query,
    _mcp_response,
)
from ._shared import (
    _DEFAULT_SEARCH_LIMIT,
    _OMNI_LOG_MAX_ALIASES_SHOWN,
    _apply_search_truncation,
    _compute_effective_limit,
    _filter_errors_for_user,
    _load_category_map,
    _resolve_search_type,
)
from .memory_retrieval import _compute_shared_query_vector

logger = logging.getLogger("code_indexer.server.mcp.handlers.search")


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
    from ....multi.models import MultiSearchRequest

    return MultiSearchRequest(  # type: ignore[arg-type, call-arg]  # search_type validated to Literal values by _resolve_search_type; precomputed_query_vector has default None but exclude=True confuses mypy
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
        no_embedding_cache_shortcut=params.get("no_embedding_cache_shortcut", False),
        temporal_embedder=params.get("temporal_embedder"),
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
    from ....multi.multi_search_config import MultiSearchConfig
    from ....multi.multi_search_service import MultiSearchService

    # Bug #1287 Defect B: search_mode is needed BEFORE wildcard expansion so
    # the internal cidx-meta* bookkeeping repo(s) can be excluded from
    # fts/hybrid wildcard fan-out (they have no FTS index by design).
    search_mode = params.get("search_mode", "semantic")
    repo_aliases = _expand_wildcard_patterns(
        params.get("repository_alias", []), user, search_mode=search_mode
    )
    if isinstance(repo_aliases, CapBreach):
        return cap_breach_response(repo_aliases)
    # Bug #894: enforce total fan-out cap after wildcard expansion + literal union
    _repo_count_breach = _enforce_repo_count_cap(repo_aliases)
    if _repo_count_breach is not None:
        return cap_breach_response(_repo_count_breach)
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
    # py-spy logging-lock fix (follow-up to Bug #1078): demoted from INFO to
    # DEBUG so this per-query audit line no longer acquires the logging handler
    # lock on the hot path at default log levels (operators can re-enable via
    # DEBUG when diagnosing fan-out expansion).
    logger.debug(
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

    # Story #1148 PART 1: For semantic omni searches, compute the query embedding
    # ONCE here (via the same VoyageAI chokepoint used by _search_activated_repo)
    # and thread it into every per-repo call as precomputed_query_vector.
    # Defect #1148 fix: _compute_shared_query_vector now returns (vector, digest).
    # The digest is set on the request so _search_semantic_sync can verify that
    # each repo's own embedding-service config matches before reusing the vector.
    # Repos on a different provider config (e.g. Cohere embed-v4.0 vs Voyage 1024)
    # receive precomputed_query_vector=None and embed via their own chokepoint.
    # On failure: WARNING is logged by _compute_shared_query_vector (Messi #13);
    # no precomputed vector is set so each repo embeds independently (explicit fallback).
    if search_type == "semantic" and repo_aliases:
        query_text = params.get("query_text", "") or ""
        if query_text:
            _omni_vec, _omni_digest = _compute_shared_query_vector(
                str(query_text),
                no_embedding_cache_shortcut=params.get(
                    "no_embedding_cache_shortcut", False
                ),
            )
            if _omni_vec:
                request.precomputed_query_vector = _omni_vec
                request.precomputed_query_vector_digest = _omni_digest

    config = MultiSearchConfig.from_config(get_config_service())
    from ....app import _server_hnsw_cache as _hnsw_cache

    service = MultiSearchService.get_instance(config, hnsw_index_cache=_hnsw_cache)
    try:
        response = service.search(request)
    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-031",
                f"MultiSearchService failed: {e}",
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
    fts_truncation_meta: Dict[str, Any] = {}
    if final_results:
        final_results, fts_truncation_meta = _apply_search_truncation(
            final_results, search_mode, params
        )

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

    # AC4 (Bug #1202): surface fts truncation metadata in omni response.
    if fts_truncation_meta:
        omni_qm = formatted.setdefault("query_metadata", {})
        omni_qm.update(fts_truncation_meta)

    if _is_temporal_query(params):
        temporal_status = _get_temporal_status(repo_aliases)
        if temporal_status:
            formatted["temporal_status"] = temporal_status

    return _mcp_response({"success": True, "results": formatted})
