"""MCP Tool Handler Functions - Complete implementation for all 22 tools.

All handlers return MCP-compliant responses with content arrays:
{
    "content": [
        {
            "type": "text",
            "text": "<JSON-stringified response data>"
        }
    ]
}
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import asyncio
import json
import logging
import sys
import types
from typing import Dict, Any, Optional, Tuple, TYPE_CHECKING
from pathlib import Path

if TYPE_CHECKING:
    pass
from code_indexer.server.auth.user_manager import User
from . import _utils
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.api_metrics_service import api_metrics_service
from code_indexer.server.services.git_operations_service import (  # noqa: F401
    git_operations_service,
    GitCommandError,
)
from code_indexer.global_repos.git_operations import GitOperationsService  # noqa: F401
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.logging_utils import format_error_log
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.server.mcp import reranking as _mcp_reranking

# Shared utilities extracted to _utils.py (Story #496 refactoring)
from ._utils import (
    _get_scip_audit_repository,  # noqa: F401 — re-exported via handlers namespace
    _get_hnsw_health_service,
    _parse_json_string_array,
    _coerce_int,
    _coerce_float,
    _get_wiki_enabled_repos,
    _enrich_with_wiki_url,
    _mcp_response,
    _get_golden_repos_dir,
    _list_global_repos,
    _get_global_repo,
    _get_query_tracker,
    _get_app_refresh_scheduler,
    _get_access_filtering_service,
    _get_scip_query_service,  # noqa: F401 — re-exported via handlers namespace
    _apply_payload_truncation,
    _apply_fts_payload_truncation,
    _apply_regex_payload_truncation,
    _apply_temporal_payload_truncation,
    _apply_scip_payload_truncation,  # noqa: F401 — re-exported via handlers namespace
    _error_with_suggestions,
    _get_available_repos,
    _format_omni_response,
    _is_temporal_query,
    _get_temporal_status,
    _validate_symbol_format,  # noqa: F401 — re-exported via handlers namespace
    _expand_wildcard_patterns,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Story #653 AC3: Constants used by reranking helpers (also used in git_read.py)
# ---------------------------------------------------------------------------
_DEFAULT_OVERFETCH_MULTIPLIER = 5
_MAX_RERANK_FETCH_LIMIT = 200


def _omni_search_code(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handle omni-search across multiple repositories.

    Called when repository_alias is an array of repository names.
    Aggregates results from all specified repos, sorted by score.

    Story #36: Refactored to use MultiSearchService.search() for parallel execution
    instead of inline asyncio.gather implementation.

    Story #51: Converted from async to sync for FastAPI thread pool execution.
    """
    from collections import defaultdict
    from ...multi.multi_search_config import MultiSearchConfig
    from ...multi.multi_search_service import MultiSearchService
    from ...multi.models import MultiSearchRequest

    repo_aliases = params.get("repository_alias", [])
    repo_aliases = _expand_wildcard_patterns(repo_aliases, user)
    requested_limit = _coerce_int(params.get("limit"), 10)

    # Smart context-aware defaults: multi-repo (2+) uses per_repo, single repo uses global
    if len(repo_aliases) > 1:
        aggregation_mode = params.get("aggregation_mode", "per_repo")
    else:
        aggregation_mode = params.get("aggregation_mode", "global")

    if not repo_aliases:
        return _mcp_response(
            {
                "success": True,
                "results": {
                    "cursor": "",
                    "total_results": 0,
                    "total_repos_searched": 0,
                    "results": [],
                    "errors": {},
                },
            }
        )

    # Story #36: Get config from ConfigService for MultiSearchService
    # Use MultiSearchConfig.from_config() to ensure MCP and REST use unified settings:
    # - multi_search_max_workers (unified)
    # - multi_search_timeout_seconds (unified)
    # NOT the MCP-specific omni_max_workers/omni_per_repo_timeout_seconds.
    config_service = get_config_service()
    config = MultiSearchConfig.from_config(config_service)

    # Story #36: Map MCP search_mode to MultiSearchRequest search_type
    search_mode = params.get("search_mode", "semantic")
    search_type = (
        search_mode if search_mode in ["semantic", "fts", "regex"] else "semantic"
    )
    # Handle temporal queries - map to temporal search_type
    if _is_temporal_query(params):
        search_type = "temporal"

    # Track API metrics for multi-repo searches
    # (Single-repo searches are tracked in semantic_query_manager._perform_search)
    if search_type == "semantic":
        api_metrics_service.increment_semantic_search(username=user.username)
    elif search_type == "regex":
        api_metrics_service.increment_regex_search(username=user.username)
    else:
        # FTS, temporal, hybrid all go to other_index_searches bucket
        api_metrics_service.increment_other_index_search(username=user.username)

    # Story #300 (Finding 1): Over-fetch to compensate for post-search access filtering.
    # Non-admin users may have results removed after HNSW search; fetching more upfront
    # ensures the final result count is closer to what was requested.
    access_svc = _get_access_filtering_service()
    if access_svc and not access_svc.is_admin_user(user.username):
        effective_limit = access_svc.calculate_over_fetch_limit(requested_limit)
    else:
        effective_limit = requested_limit

    # Story #36: Create MultiSearchRequest from MCP params
    request = MultiSearchRequest(
        repositories=repo_aliases,
        query=params.get("query_text", ""),
        search_type=search_type,  # type: ignore[arg-type]
        limit=effective_limit,
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

    # Story #36: Delegate to MultiSearchService for parallel execution
    # Story #51: service.search() is now synchronous
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
        return _mcp_response(
            {
                "success": True,
                "results": {
                    "cursor": "",
                    "total_results": 0,
                    "total_repos_searched": 0,
                    "results": [],
                    "errors": {"service_error": str(e)},
                },
            }
        )

    # Story #182: Load category map for result enrichment
    category_map = {}
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
        # Log but don't fail if category lookup fails
        logger.warning(
            format_error_log(
                "MCP-GENERAL-036",
                f"Failed to load category map in _omni_search_code: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )

    # Story #292: Build wiki-enabled repos set once per request (AC2)
    wiki_enabled_repos = _get_wiki_enabled_repos()

    # Story #36: Convert MultiSearchResponse (grouped by repo) to flat list with source_repo
    all_results = []
    for repo_alias, repo_results in response.results.items():
        for result in repo_results:
            result["source_repo"] = repo_alias
            # Normalize score field name for consistency
            if "score" in result and "similarity_score" not in result:
                result["similarity_score"] = result["score"]

            # Story #182: Enrich with category info
            # Strip -global suffix to get golden repo alias
            golden_alias = repo_alias.replace("-global", "") if repo_alias else None
            if golden_alias:
                category_info = category_map.get(golden_alias, {})
                result["repo_category"] = category_info.get("category_name")

            # Story #292: Enrich with wiki_url for .md files from wiki-enabled repos (AC1, AC4)
            _enrich_with_wiki_url(
                result,
                result.get("file_path", ""),
                repo_alias,
                wiki_enabled_repos,
            )

            all_results.append(result)

    errors = response.errors or {}
    repos_searched = response.metadata.total_repos_searched

    # Story #331 AC7: Filter errors dict to hide unauthorized repo aliases
    access_service_for_errors = _get_access_filtering_service()
    if access_service_for_errors and not access_service_for_errors.is_admin_user(
        user.username
    ):
        accessible = access_service_for_errors.get_accessible_repos(user.username)
        errors = {
            k: v
            for k, v in errors.items()
            if k.replace("-global", "") in accessible or k in accessible
        }

    # Aggregate results based on mode (use requested_limit for slicing, not effective_limit)
    if aggregation_mode == "per_repo":
        # Per-repo mode: take proportional results from each repo
        results_by_repo = defaultdict(list)
        for r in all_results:
            results_by_repo[r.get("source_repo", "unknown")].append(r)

        # Sort each repo's results by score
        for repo in results_by_repo:
            results_by_repo[repo].sort(
                key=lambda x: x.get("similarity_score", x.get("score", 0)), reverse=True
            )

        # Take proportional results from each repo
        num_repos = len(results_by_repo)
        if num_repos > 0:
            per_repo_limit = requested_limit // num_repos
            remainder = requested_limit % num_repos
            final_results = []
            for i, (repo, results) in enumerate(results_by_repo.items()):
                # Give first 'remainder' repos one extra result
                repo_limit = per_repo_limit + (1 if i < remainder else 0)
                final_results.extend(results[:repo_limit])
        else:
            final_results = []
    else:
        # Global mode: sort all by score, take top N
        all_results.sort(
            key=lambda x: x.get("similarity_score", x.get("score", 0)), reverse=True
        )
        final_results = all_results[:requested_limit]

    # Smart context-aware defaults: multi-repo (2+) uses grouped, single repo uses flat
    if len(repo_aliases) > 1:
        response_format = params.get("response_format", "grouped")
    else:
        response_format = params.get("response_format", "flat")

    # Story #683: Apply payload truncation to aggregated multi-repo results
    # This ensures consistency with REST API which calls _apply_multi_truncation()
    # Story #50: Truncation functions are now sync
    if final_results:
        if search_mode in ["fts", "hybrid"]:
            final_results = _apply_fts_payload_truncation(final_results)
        elif _is_temporal_query(params):
            final_results = _apply_temporal_payload_truncation(final_results)
        else:
            final_results = _apply_payload_truncation(final_results)

    # Story #300: Apply group-based access filtering (AC5)
    access_filtering_service = _get_access_filtering_service()
    if access_filtering_service:
        final_results = access_filtering_service.filter_query_results(
            final_results, user.username
        )
        # Story #300 (Finding 1): Truncate back to requested limit after filtering.
        # Over-fetching may have produced extra results; ensure we return no more than requested.
        final_results = final_results[:requested_limit]

    # Use _format_omni_response helper to format results
    formatted = _format_omni_response(
        all_results=final_results,
        response_format=response_format,
        total_repos_searched=repos_searched,
        errors=errors,
        cursor="",
    )

    # Add temporal_status if this is a temporal query (Story #583)
    if _is_temporal_query(params):
        temporal_status = _get_temporal_status(repo_aliases)
        if temporal_status:
            formatted["temporal_status"] = temporal_status

    # Wrap in nested "results" key for backward compatibility with existing API contract
    return _mcp_response(
        {
            "success": True,
            "results": formatted,
        }
    )


def search_code(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Search code using semantic search, FTS, or hybrid mode."""
    try:
        from pathlib import Path

        # Story #4 AC2: Metrics tracking moved to service layer
        # SemanticQueryManager._perform_search() now handles metrics
        # This ensures both MCP and REST API calls are counted

        repository_alias = params.get("repository_alias")

        # Handle JSON string arrays (from MCP clients that serialize arrays as strings)
        repository_alias = _parse_json_string_array(repository_alias)
        params["repository_alias"] = repository_alias  # Update params for downstream

        # Route to omni-search when repository_alias is an array
        # Story #51: _omni_search_code is now synchronous
        if isinstance(repository_alias, list):
            return _omni_search_code(params, user)

        # Check if this is a global repository query (ends with -global suffix)
        if repository_alias and repository_alias.endswith("-global"):
            # Global repository: query directly without activation requirement
            golden_repos_dir = _get_golden_repos_dir()

            # Look up global repo in GlobalRegistry to get actual path
            global_repos = _list_global_repos()

            # Find the matching global repo
            repo_entry = next(
                (r for r in global_repos if r["alias_name"] == repository_alias), None
            )

            if not repo_entry:
                available_repos = _get_available_repos(user)
                error_envelope = _error_with_suggestions(
                    error_msg=f"Global repository '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["results"] = []
                return _mcp_response(error_envelope)

            # Use AliasManager to get current target path (registry path becomes stale after refresh)
            from code_indexer.global_repos.alias_manager import AliasManager

            alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
            target_path = alias_manager.read_alias(repository_alias)

            if not target_path:
                available_repos = _get_available_repos(user)
                error_envelope = _error_with_suggestions(
                    error_msg=f"Alias for '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["results"] = []
                return _mcp_response(error_envelope)

            global_repo_path = Path(target_path)

            # Verify global repo exists
            if not global_repo_path.exists():
                raise FileNotFoundError(
                    f"Global repository '{repository_alias}' not found at {global_repo_path}"
                )

            # Build mock repository list for _perform_search (single global repo)
            mock_user_repos = [
                {
                    "user_alias": repository_alias,
                    "repo_path": str(global_repo_path),
                    "actual_repo_id": repo_entry["repo_name"],
                }
            ]

            # Call _perform_search directly with all query parameters
            # Track query execution with QueryTracker for concurrency safety
            import time

            query_tracker = _get_query_tracker()
            index_path = target_path  # Use resolved path for tracking

            # Story #300 (Finding 1): Over-fetch to compensate for post-search access filtering.
            _requested_limit = _coerce_int(params.get("limit"), 10)
            _access_svc = _get_access_filtering_service()
            if _access_svc and not _access_svc.is_admin_user(user.username):
                _effective_limit = _access_svc.calculate_over_fetch_limit(
                    _requested_limit
                )
            else:
                _effective_limit = _requested_limit

            # Story #653 AC3: Overfetch when reranking is active
            if params.get("rerank_query"):
                _rc = get_config_service().get_config().rerank_config
                _overfetch_mul = (
                    _rc.overfetch_multiplier if _rc else _DEFAULT_OVERFETCH_MULTIPLIER
                )
                _access_filter_extra = _effective_limit - _requested_limit
                _effective_limit = _mcp_reranking.calculate_overfetch_limit(
                    _requested_limit, _overfetch_mul, _access_filter_extra
                )

            start_time = time.time()
            try:
                # Increment ref count before query (if QueryTracker available)
                if query_tracker is not None:
                    query_tracker.increment_ref(index_path)

                # Coerce numeric parameters from MCP string types (MCP protocol sends all values as strings)
                _evolution_limit_raw = params.get("evolution_limit")
                results = _utils.app_module.semantic_query_manager._perform_search(
                    username=user.username,
                    user_repos=mock_user_repos,
                    query_text=params["query_text"],
                    limit=_effective_limit,
                    min_score=_coerce_float(params.get("min_score"), 0.3),
                    file_extensions=params.get("file_extensions"),
                    language=params.get("language"),
                    exclude_language=params.get("exclude_language"),
                    path_filter=params.get("path_filter"),
                    exclude_path=params.get("exclude_path"),
                    accuracy=params.get("accuracy", "balanced"),
                    # Search mode (Story #503 - FTS Bug Fix)
                    search_mode=params.get("search_mode", "semantic"),
                    # Temporal query parameters (Story #446)
                    time_range=params.get("time_range"),
                    time_range_all=params.get("time_range_all", False),
                    at_commit=params.get("at_commit"),
                    include_removed=params.get("include_removed", False),
                    show_evolution=params.get("show_evolution", False),
                    evolution_limit=(
                        _coerce_int(_evolution_limit_raw, 0)
                        if _evolution_limit_raw is not None
                        else None
                    ),
                    # FTS-specific parameters (Story #503 Phase 2)
                    case_sensitive=params.get("case_sensitive", False),
                    fuzzy=params.get("fuzzy", False),
                    edit_distance=_coerce_int(params.get("edit_distance"), 0),
                    snippet_lines=_coerce_int(params.get("snippet_lines"), 5),
                    regex=params.get("regex", False),
                    # Temporal filtering parameters (Story #503 Phase 3)
                    diff_type=params.get("diff_type"),
                    author=params.get("author"),
                    chunk_type=params.get("chunk_type"),
                    # Query strategy parameters (Story #488 Phase 4)
                    query_strategy=params.get("query_strategy"),
                    score_fusion=params.get("score_fusion"),
                    # Multi-provider routing (Story #593)
                    preferred_provider=params.get("preferred_provider"),
                )
                execution_time_ms = int((time.time() - start_time) * 1000)
                timeout_occurred = False
            except TimeoutError as e:
                execution_time_ms = int((time.time() - start_time) * 1000)
                timeout_occurred = True
                raise Exception(f"Query timed out: {str(e)}")
            except Exception as e:
                execution_time_ms = int((time.time() - start_time) * 1000)
                if "timeout" in str(e).lower():
                    raise Exception(f"Query timed out: {str(e)}")
                raise
            finally:
                # Always decrement ref count when query completes (if QueryTracker available)
                if query_tracker is not None:
                    query_tracker.decrement_ref(index_path)

            # Story #182: Load category map for result enrichment
            category_map = {}
            try:
                if (
                    hasattr(_utils.app_module, "golden_repo_manager")
                    and _utils.app_module.golden_repo_manager
                ):
                    category_service = getattr(
                        _utils.app_module.golden_repo_manager,
                        "_repo_category_service",
                        None,
                    )
                    if category_service:
                        category_map = category_service.get_repo_category_map()
            except Exception as e:
                # Log but don't fail if category lookup fails
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-037",
                        f"Failed to load category map in search_code: {e}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )

            # Story #292: Build wiki-enabled repos set once per request (AC2)
            wiki_enabled_repos = _get_wiki_enabled_repos()

            # Build response matching query_user_repositories format
            response_results = []
            for r in results:
                result_dict = r.to_dict()
                result_dict["source_repo"] = (
                    repository_alias  # Fix: Set source_repo for single-repo searches
                )

                # Story #182: Enrich with category info
                # Strip -global suffix to get golden repo alias
                golden_alias = (
                    repository_alias.replace("-global", "")
                    if repository_alias
                    else None
                )
                if golden_alias:
                    category_info = category_map.get(golden_alias, {})
                    result_dict["repo_category"] = category_info.get("category_name")

                # Story #292: Enrich with wiki_url for .md files from wiki-enabled repos (AC1, AC4)
                _enrich_with_wiki_url(
                    result_dict,
                    result_dict.get("file_path", ""),
                    repository_alias,
                    wiki_enabled_repos,
                )

                response_results.append(result_dict)

            # Story #653: Apply cross-encoder reranking after retrieval, before truncation
            _rerank_query = params.get("rerank_query")
            _rerank_instruction = params.get("rerank_instruction")
            response_results, _rerank_meta = _mcp_reranking._apply_reranking_sync(
                results=response_results,
                rerank_query=_rerank_query,
                rerank_instruction=_rerank_instruction,
                content_extractor=lambda r: r.get("content", "")
                or r.get("code_snippet", ""),
                requested_limit=_requested_limit,
                config_service=get_config_service(),
            )

            # Apply payload truncation based on search mode
            # Story #50: Truncation functions are now sync
            search_mode = params.get("search_mode", "semantic")
            if search_mode in ["fts", "hybrid"]:
                # Story #680: FTS truncation for code_snippet and match_text
                response_results = _apply_fts_payload_truncation(response_results)
            # Story #681: Temporal truncation for temporal queries
            if _is_temporal_query(params):
                response_results = _apply_temporal_payload_truncation(response_results)
            else:
                # Story #679: Semantic truncation for content field
                response_results = _apply_payload_truncation(response_results)

            # Story #300: Apply group-based access filtering (global repo path)
            access_filtering_service = _get_access_filtering_service()
            if access_filtering_service:
                response_results = access_filtering_service.filter_query_results(
                    response_results, user.username
                )
                # Story #331 AC6: Filter cidx-meta results that reference inaccessible repos
                if repository_alias and "cidx-meta" in repository_alias:
                    response_results = (
                        access_filtering_service.filter_cidx_meta_results(
                            response_results, user.username
                        )
                    )
                # Story #300 (Finding 1): Truncate back to requested limit after filtering.
                response_results = response_results[:_requested_limit]

            result = {
                "results": response_results,
                "total_results": len(response_results),
                "query_metadata": {
                    "query_text": params["query_text"],
                    "execution_time_ms": execution_time_ms,
                    "repositories_searched": 1,
                    "timeout_occurred": timeout_occurred,
                    # Story #654: reranker telemetry
                    "reranker_used": _rerank_meta["reranker_used"],
                    "reranker_provider": _rerank_meta["reranker_provider"],
                    "rerank_time_ms": _rerank_meta["rerank_time_ms"],
                },
            }

            return _mcp_response({"success": True, "results": result})

        # Activated repository: use semantic_query_manager for activated repositories (matches REST endpoint pattern)
        # Coerce numeric parameters from MCP string types (MCP protocol sends all values as strings)

        # Story #300 (Finding 1): Over-fetch to compensate for post-search access filtering.
        _act_requested_limit = _coerce_int(params.get("limit"), 10)
        _act_access_svc = _get_access_filtering_service()
        if _act_access_svc and not _act_access_svc.is_admin_user(user.username):
            _act_effective_limit = _act_access_svc.calculate_over_fetch_limit(
                _act_requested_limit
            )
        else:
            _act_effective_limit = _act_requested_limit

        # Story #653 AC3: Overfetch when reranking is active
        if params.get("rerank_query"):
            _act_rc = get_config_service().get_config().rerank_config
            _act_overfetch_mul = (
                _act_rc.overfetch_multiplier
                if _act_rc
                else _DEFAULT_OVERFETCH_MULTIPLIER
            )
            _act_access_extra = _act_effective_limit - _act_requested_limit
            _act_effective_limit = _mcp_reranking.calculate_overfetch_limit(
                _act_requested_limit, _act_overfetch_mul, _act_access_extra
            )

        _evolution_limit_activated = params.get("evolution_limit")
        result = _utils.app_module.semantic_query_manager.query_user_repositories(
            username=user.username,
            query_text=params["query_text"],
            repository_alias=params.get("repository_alias"),
            limit=_act_effective_limit,
            min_score=_coerce_float(params.get("min_score"), 0.3),
            file_extensions=params.get("file_extensions"),
            language=params.get("language"),
            exclude_language=params.get("exclude_language"),
            path_filter=params.get("path_filter"),
            exclude_path=params.get("exclude_path"),
            accuracy=params.get("accuracy", "balanced"),
            # Search mode (Story #503 - FTS Bug Fix)
            search_mode=params.get("search_mode", "semantic"),
            # Temporal query parameters (Story #446)
            time_range=params.get("time_range"),
            time_range_all=params.get("time_range_all", False),
            at_commit=params.get("at_commit"),
            include_removed=params.get("include_removed", False),
            show_evolution=params.get("show_evolution", False),
            evolution_limit=(
                _coerce_int(_evolution_limit_activated, 0)
                if _evolution_limit_activated is not None
                else None
            ),
            # FTS-specific parameters (Story #503 Phase 2)
            case_sensitive=params.get("case_sensitive", False),
            fuzzy=params.get("fuzzy", False),
            edit_distance=_coerce_int(params.get("edit_distance"), 0),
            snippet_lines=_coerce_int(params.get("snippet_lines"), 5),
            regex=params.get("regex", False),
            # Temporal filtering parameters (Story #503 Phase 3)
            diff_type=params.get("diff_type"),
            author=params.get("author"),
            chunk_type=params.get("chunk_type"),
            # Query strategy parameters (Story #488 Phase 4)
            query_strategy=params.get("query_strategy"),
            score_fusion=params.get("score_fusion"),
            # Multi-provider routing (Story #593)
            preferred_provider=params.get("preferred_provider"),
        )

        # Story #182: Load category map for result enrichment (activated repos)
        category_map = {}
        try:
            if (
                hasattr(_utils.app_module, "golden_repo_manager")
                and _utils.app_module.golden_repo_manager
            ):
                category_service = getattr(
                    _utils.app_module.golden_repo_manager,
                    "_repo_category_service",
                    None,
                )
                if category_service:
                    category_map = category_service.get_repo_category_map()
        except Exception as e:
            # Log but don't fail if category lookup fails
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-038",
                    f"Failed to load category map in search_code (activated): {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Story #182: Enrich results with category info
        if "results" in result and isinstance(result["results"], list):
            for res in result["results"]:
                # Get the repository alias from the result (may have -global suffix)
                repo_alias = res.get("source_repo") or res.get("repository_alias")
                if repo_alias:
                    # Strip -global suffix to get golden repo alias
                    golden_alias = repo_alias.replace("-global", "")
                    category_info = category_map.get(golden_alias, {})
                    res["repo_category"] = category_info.get("category_name")

        # Story #653/#654: Apply cross-encoder reranking after retrieval, before truncation
        if "results" in result and isinstance(result["results"], list):
            _rerank_query = params.get("rerank_query")
            _rerank_instruction = params.get("rerank_instruction")
            result["results"], _rerank_meta = _mcp_reranking._apply_reranking_sync(
                results=result["results"],
                rerank_query=_rerank_query,
                rerank_instruction=_rerank_instruction,
                content_extractor=lambda r: r.get("content", "")
                or r.get("code_snippet", ""),
                requested_limit=_act_requested_limit,
                config_service=get_config_service(),
            )
            # Story #654: inject telemetry into query_metadata (create if absent)
            _qm: dict = result.setdefault("query_metadata", {})  # type: ignore[assignment]  # result typed as Dict[str,object]; setdefault returns object not dict
            _qm["reranker_used"] = _rerank_meta["reranker_used"]
            _qm["reranker_provider"] = _rerank_meta["reranker_provider"]
            _qm["rerank_time_ms"] = _rerank_meta["rerank_time_ms"]

        # Apply payload truncation based on search mode
        # Story #50: Truncation functions are now sync
        if "results" in result and isinstance(result["results"], list):
            search_mode = params.get("search_mode", "semantic")
            if search_mode in ["fts", "hybrid"]:
                # Story #680: FTS truncation for code_snippet and match_text
                result["results"] = _apply_fts_payload_truncation(result["results"])
            # Story #681: Temporal truncation for temporal queries
            if _is_temporal_query(params):
                result["results"] = _apply_temporal_payload_truncation(
                    result["results"]
                )
            else:
                # Story #679: Semantic truncation for content field
                result["results"] = _apply_payload_truncation(result["results"])

        # Story #300: Apply group-based access filtering (activated repo path)
        access_filtering_service = _get_access_filtering_service()
        if (
            access_filtering_service
            and "results" in result
            and isinstance(result["results"], list)
        ):
            result["results"] = access_filtering_service.filter_query_results(
                result["results"], user.username
            )
            # Story #300 (Finding 1): Truncate back to requested limit after filtering.
            result["results"] = result["results"][:_act_requested_limit]
            result["total_results"] = len(result["results"])

        return _mcp_response({"success": True, "results": result})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "results": []})


def discover_repositories(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Discover available repositories from configured sources."""
    try:
        # List all golden repositories (source_type filter not currently used)
        repos = _utils.app_module.golden_repo_manager.list_golden_repos()

        # Story #300: Apply group-based access filtering (AC3)
        access_filtering_service = _get_access_filtering_service()
        if access_filtering_service:
            repo_aliases = [r.get("alias", r.get("name", "")) for r in repos]
            accessible_aliases = access_filtering_service.filter_repo_listing(
                repo_aliases, user.username
            )
            repos = [
                r
                for r in repos
                if r.get("alias", r.get("name", "")) in accessible_aliases
            ]

        return _mcp_response({"success": True, "repositories": repos})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "repositories": []})


def list_repositories(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """List activated repositories for the current user, plus global repos."""
    try:
        # Story #196: Whitelist of MCP-relevant fields for activated repos
        ACTIVATED_REPO_FIELDS = {
            "user_alias",
            "golden_repo_alias",
            "current_branch",
            "is_global",
            "repo_url",
            "last_refresh",
            "repo_category",
            "is_composite",
            "golden_repo_aliases",  # For composite repos only
        }

        # Get activated repos from database
        raw_activated_repos = (
            _utils.app_module.activated_repo_manager.list_activated_repositories(
                user.username
            )
        )

        # Story #196: Filter activated repos to only include whitelisted fields
        activated_repos = []
        for repo in raw_activated_repos:
            filtered_repo = {
                k: v for k, v in repo.items() if k in ACTIVATED_REPO_FIELDS
            }
            activated_repos.append(filtered_repo)

        # Get global repos from storage backend (SQLite or PostgreSQL via BackendRegistry)
        global_repos = []
        try:
            global_repos_data = _list_global_repos()

            # Normalize global repos schema to match activated repos
            for repo in global_repos_data:
                # Validate required fields exist
                if "alias_name" not in repo or "repo_name" not in repo:
                    logger.warning(
                        format_error_log(
                            "MCP-GENERAL-032",
                            f"Skipping malformed global repo entry: {repo}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    continue

                normalized = {
                    "user_alias": repo["alias_name"],  # Map alias_name → user_alias
                    "golden_repo_alias": repo[
                        "repo_name"
                    ],  # Map repo_name → golden_repo_alias
                    "current_branch": None,  # Global repos are read-only snapshots
                    "is_global": True,
                    "repo_url": repo.get("repo_url"),
                    "last_refresh": repo.get("last_refresh"),
                    # Story #196: Removed index_path and created_at (internal fields, not MCP-relevant)
                }
                global_repos.append(normalized)

        except Exception as e:
            # Log but don't fail - continue with activated repos only
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-033",
                    f"Failed to load global repos from storage backend: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Merge activated and global repos
        all_repos = activated_repos + global_repos

        # Story #182: Enrich repos with category information
        category_map = {}
        try:
            if (
                hasattr(_utils.app_module, "golden_repo_manager")
                and _utils.app_module.golden_repo_manager
            ):
                category_service = getattr(
                    _utils.app_module.golden_repo_manager,
                    "_repo_category_service",
                    None,
                )
                if category_service:
                    category_map = category_service.get_repo_category_map()
        except Exception as e:
            # Log but don't fail if category lookup fails
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-034",
                    f"Failed to load category map: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Enrich each repo with category information
        for repo in all_repos:
            # For activated repos, use golden_repo_alias to look up category
            # For global repos, use golden_repo_alias (same field)
            golden_alias = repo.get("golden_repo_alias")
            category_info = category_map.get(golden_alias, {})
            repo["repo_category"] = category_info.get("category_name")

        # Story #182: Filter by category if requested
        category_filter = params.get("category")
        if category_filter:
            if category_filter == "Unassigned":
                # Filter for repos with NULL category
                all_repos = [r for r in all_repos if r["repo_category"] is None]
            else:
                # Filter for repos matching the specified category name
                all_repos = [
                    r for r in all_repos if r["repo_category"] == category_filter
                ]

        # Story #182: Sort by category priority (ascending), then Unassigned last, then alphabetically
        def sort_key(repo):
            golden_alias = repo.get("golden_repo_alias")
            category_info = category_map.get(golden_alias, {})
            priority = category_info.get("priority")

            # Repos with priority come first (sorted by priority),
            # then Unassigned repos (priority=None) at the end
            if priority is None:
                # Use large number to sort Unassigned last
                return (float("inf"), repo.get("user_alias", ""))
            else:
                return (priority, repo.get("user_alias", ""))

        all_repos.sort(key=sort_key)

        # Story #300: Apply group-based access filtering (AC2)
        # Use golden_repo_alias (not user_alias) because get_accessible_repos() returns
        # golden repo aliases without -global suffix, while user_alias for global repos
        # carries the -global suffix and would not match.
        access_filtering_service = _get_access_filtering_service()
        if access_filtering_service:
            repo_aliases = [r.get("golden_repo_alias", "") for r in all_repos]
            accessible_aliases = access_filtering_service.filter_repo_listing(
                repo_aliases, user.username
            )
            all_repos = [
                r
                for r in all_repos
                if r.get("golden_repo_alias", "") in accessible_aliases
            ]

        return _mcp_response({"success": True, "repositories": all_repos})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "repositories": []})


def activate_repository(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Activate a repository for querying (supports single or composite)."""
    try:
        # Story #300: Check group-based access before activating (AC4)
        golden_repo_alias = params.get("golden_repo_alias")
        golden_repo_aliases = params.get("golden_repo_aliases")
        access_filtering_service = _get_access_filtering_service()
        if access_filtering_service:
            if not access_filtering_service.is_admin_user(user.username):
                accessible_repos = access_filtering_service.get_accessible_repos(
                    user.username
                )
                # Check singular alias
                if golden_repo_alias and golden_repo_alias not in accessible_repos:
                    return _mcp_response(
                        {
                            "success": False,
                            "error": (
                                "Repository not accessible. "
                                "Contact your administrator for access."
                            ),
                            "job_id": None,
                        }
                    )
                # Check composite aliases (plural) to prevent bypass via golden_repo_aliases
                if golden_repo_aliases:
                    for alias in golden_repo_aliases:
                        if alias not in accessible_repos:
                            return _mcp_response(
                                {
                                    "success": False,
                                    "error": (
                                        "Repository not accessible. "
                                        "Contact your administrator for access."
                                    ),
                                    "job_id": None,
                                }
                            )

        job_id = _utils.app_module.activated_repo_manager.activate_repository(
            username=user.username,
            golden_repo_alias=golden_repo_alias,
            golden_repo_aliases=params.get("golden_repo_aliases"),
            branch_name=params.get("branch_name"),
            user_alias=params.get("user_alias"),
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": "Repository activation started",
            }
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "job_id": None})


def deactivate_repository(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Deactivate a repository."""
    try:
        user_alias = params["user_alias"]
        job_id = _utils.app_module.activated_repo_manager.deactivate_repository(
            username=user.username, user_alias=user_alias
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Repository '{user_alias}' deactivation started",
            }
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "job_id": None})


def list_repo_categories(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """List all repository categories (Story #182)."""
    # Story #331 AC10: Accepted risk - repository categories are generic
    # organizational labels (e.g., category names/patterns) that do not
    # directly reveal specific repository names or existence. Filtering
    # categories would provide minimal security benefit.
    try:
        # Get category service from golden_repo_manager
        if (
            not hasattr(_utils.app_module, "golden_repo_manager")
            or not _utils.app_module.golden_repo_manager
        ):
            return _mcp_response(
                {
                    "success": False,
                    "error": "Category service not available",
                    "categories": [],
                    "total": 0,
                }
            )

        category_service = getattr(
            _utils.app_module.golden_repo_manager, "_repo_category_service", None
        )
        if not category_service:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Category service not initialized",
                    "categories": [],
                    "total": 0,
                }
            )

        # Get all categories
        categories = category_service.list_categories()

        return _mcp_response(
            {"success": True, "categories": categories, "total": len(categories)}
        )
    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-035",
                f"Failed to list repository categories: {e}",
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {"success": False, "error": str(e), "categories": [], "total": 0}
        )


def get_repository_status(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get detailed status of a repository."""

    try:
        user_alias = params["repository_alias"]

        # Load category map for enrichment (Story #182 pattern)
        category_map = {}
        try:
            if (
                hasattr(_utils.app_module, "golden_repo_manager")
                and _utils.app_module.golden_repo_manager
            ):
                category_service = getattr(
                    _utils.app_module.golden_repo_manager,
                    "_repo_category_service",
                    None,
                )
                if category_service:
                    category_map = category_service.get_repo_category_map()
        except Exception as e:
            # Log but don't fail if category lookup fails
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-035",
                    f"Failed to load category map in get_repository_status: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Check if this is a global repository (ends with -global suffix)
        if user_alias and user_alias.endswith("-global"):
            global_repos = _list_global_repos()

            repo_entry = next(
                (r for r in global_repos if r["alias_name"] == user_alias), None
            )

            if not repo_entry:
                available_repos = _get_available_repos(user)
                error_envelope = _error_with_suggestions(
                    error_msg=f"Global repository '{user_alias}' not found",
                    attempted_value=user_alias,
                    available_values=available_repos,
                )
                error_envelope["status"] = {}
                return _mcp_response(error_envelope)

            # Build status directly from registry entry (no alias file needed)
            status = {
                "user_alias": repo_entry["alias_name"],
                "golden_repo_alias": repo_entry.get("repo_name"),
                "repo_url": repo_entry.get("repo_url"),
                "is_global": True,
                "path": repo_entry.get("index_path"),
                "last_refresh": repo_entry.get("last_refresh"),
                "created_at": repo_entry.get("created_at"),
                "index_path": repo_entry.get("index_path"),
            }

            # Enrich with category info (Story #182)
            golden_alias = repo_entry.get("repo_name")
            category_info = category_map.get(golden_alias, {})
            status["repo_category"] = category_info.get("category_name")

            return _mcp_response({"success": True, "status": status})

        # Activated repository (original code)
        status = _utils.app_module.repository_listing_manager.get_repository_details(
            user_alias, user.username
        )

        # Enrich with category info (Story #182)
        golden_alias = status.get("golden_repo_alias")
        if golden_alias:
            category_info = category_map.get(golden_alias, {})
            status["repo_category"] = category_info.get("category_name")

        return _mcp_response({"success": True, "status": status})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "status": {}})


def sync_repository(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Sync repository with upstream."""
    try:
        user_alias = params["user_alias"]
        # Resolve alias to repository details
        repos = _utils.app_module.activated_repo_manager.list_activated_repositories(
            user.username
        )
        repo_id = None
        for repo in repos:
            if repo["user_alias"] == user_alias:
                repo_id = repo.get("actual_repo_id", user_alias)
                break

        if not repo_id:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Repository '.*' not found",
                    "job_id": None,
                }
            )

        # Defensive check
        if _utils.app_module.background_job_manager is None:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Background job manager not initialized",
                    "job_id": None,
                }
            )

        # Create sync job wrapper function
        from code_indexer.server.app import _execute_repository_sync

        def sync_job_wrapper():
            return _execute_repository_sync(
                repo_id=repo_id,
                username=user.username,
                options={},
                progress_callback=None,
            )

        # Submit sync job with correct signature
        job_id = _utils.app_module.background_job_manager.submit_job(
            operation_type="sync_repository",
            func=sync_job_wrapper,
            submitter_username=user.username,
            repo_alias=repo_id,  # AC5: Fix unknown repo bug
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Repository '{user_alias}' sync started",
            }
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "job_id": None})


def switch_branch(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Switch repository to different branch."""
    try:
        user_alias = params["user_alias"]
        branch_name = params["branch_name"]
        create = params.get("create", False)

        # Use activated_repo_manager.switch_branch (matches app.py endpoint pattern)
        result = _utils.app_module.activated_repo_manager.switch_branch(
            username=user.username,
            user_alias=user_alias,
            branch_name=branch_name,
            create=create,
        )
        return _mcp_response({"success": True, "message": result["message"]})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e)})


def _omni_list_files(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handle omni-list-files across multiple repositories."""
    import json as json_module

    repo_aliases = params.get("repository_alias", [])
    repo_aliases = _expand_wildcard_patterns(repo_aliases, user)

    if not repo_aliases:
        return _mcp_response(
            {
                "success": True,
                "files": [],
                "total_files": 0,
                "repos_searched": 0,
                "errors": {},
            }
        )

    all_files = []
    errors = {}
    repos_searched = 0

    for repo_alias in repo_aliases:
        try:
            single_params = dict(params)
            single_params["repository_alias"] = repo_alias

            single_result = list_files(single_params, user)

            content = single_result.get("content", [])
            if content and content[0].get("type") == "text":
                result_data = json_module.loads(content[0]["text"])
                if result_data.get("success"):
                    repos_searched += 1
                    files_list = result_data.get("files", [])
                    for f in files_list:
                        f["source_repo"] = repo_alias
                    all_files.extend(files_list)
                else:
                    errors[repo_alias] = result_data.get("error", "Unknown error")
        except Exception as e:
            errors[repo_alias] = str(e)
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-034",
                    f"Omni-list-files failed for {repo_alias}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

    # Story #331 AC7: Filter errors dict to hide unauthorized repo aliases
    _ac7_service = _get_access_filtering_service()
    if _ac7_service and not _ac7_service.is_admin_user(user.username):
        _ac7_accessible = _ac7_service.get_accessible_repos(user.username)
        errors = {
            k: v
            for k, v in errors.items()
            if k.replace("-global", "") in _ac7_accessible or k in _ac7_accessible
        }

    # Get response_format parameter (default to "flat" for backward compatibility)
    response_format = params.get("response_format", "flat")
    formatted = _format_omni_response(
        all_results=all_files,
        response_format=response_format,
        total_repos_searched=repos_searched,
        errors=errors,
    )
    # Add files-specific field for backward compatibility
    if response_format == "flat":
        formatted["files"] = formatted.pop("results")
        formatted["total_files"] = formatted.pop("total_results")
        formatted["repos_searched"] = formatted.pop("total_repos_searched")
    return _mcp_response(formatted)


def list_files(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """List files in a repository."""
    from code_indexer.server.models.api_models import FileListQueryParams
    from pathlib import Path

    try:
        repository_alias = params["repository_alias"]
        repository_alias = _parse_json_string_array(repository_alias)
        params["repository_alias"] = repository_alias  # Update params for downstream

        # Route to omni-search when repository_alias is an array
        if isinstance(repository_alias, list):
            return _omni_list_files(params, user)

        # Extract parameters for path pattern building
        path = params.get("path", "")
        recursive = params.get(
            "recursive", True
        )  # Default to recursive for backward compatibility
        user_path_pattern = params.get("path_pattern")  # Optional advanced filtering

        # Build path pattern combining path and user's pattern
        # This logic mirrors browse_directory (lines 1220-1238)
        final_path_pattern = None
        # Normalize path first (remove trailing slash) - "/" becomes ""
        path = path.rstrip("/") if path else ""
        if path:
            # Base pattern for the specified directory
            base_pattern = f"{path}/**/*" if recursive else f"{path}/*"
            if user_path_pattern:
                # Combine path with user's pattern
                # e.g., path="src", path_pattern="*.py" -> "src/**/*.py"
                if recursive:
                    final_path_pattern = f"{path}/**/{user_path_pattern}"
                else:
                    final_path_pattern = f"{path}/{user_path_pattern}"
            else:
                final_path_pattern = base_pattern
        elif user_path_pattern:
            # Just use the user's pattern directly
            final_path_pattern = user_path_pattern
        # else: final_path_pattern stays None (all files)

        # Check if this is a global repository (ends with -global suffix)
        if repository_alias and repository_alias.endswith("-global"):
            # Look up global repo in GlobalRegistry to get actual path
            golden_repos_dir = _get_golden_repos_dir()

            global_repos = _list_global_repos()

            # Find the matching global repo
            repo_entry = next(
                (r for r in global_repos if r["alias_name"] == repository_alias), None
            )

            if not repo_entry:
                available_repos = _get_available_repos(user)
                error_envelope = _error_with_suggestions(
                    error_msg=f"Global repository '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["files"] = []
                return _mcp_response(error_envelope)

            # Use AliasManager to get current target path (registry path becomes stale after refresh)
            from code_indexer.global_repos.alias_manager import AliasManager

            alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
            target_path = alias_manager.read_alias(repository_alias)

            if not target_path:
                available_repos = _get_available_repos(user)
                error_envelope = _error_with_suggestions(
                    error_msg=f"Alias for '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["files"] = []
                return _mcp_response(error_envelope)

            # Use resolved path instead of alias for file_service
            query_params = FileListQueryParams(
                page=1,
                limit=500,  # Max limit for MCP tool usage
                path_pattern=final_path_pattern,
            )

            result = _utils.app_module.file_service.list_files_by_path(
                repo_path=target_path,
                query_params=query_params,
            )
        else:
            # Create FileListQueryParams object as required by service method signature
            query_params = FileListQueryParams(
                page=1,
                limit=500,  # Max limit for MCP tool usage
                path_pattern=final_path_pattern,
            )

            # Call with correct signature: list_files(repo_id, username, query_params)
            result = _utils.app_module.file_service.list_files(
                repo_id=repository_alias,
                username=user.username,
                query_params=query_params,
            )

        # Extract files from FileListResponse and serialize FileInfo objects
        # Handle both FileListResponse objects and plain dicts
        if hasattr(result, "files"):
            # FileListResponse object with FileInfo objects
            files_data = result.files
        elif isinstance(result, dict):
            # Plain dict (for backward compatibility with tests)
            files_data = result.get("files", [])
        else:
            files_data = []

        # Convert FileInfo Pydantic objects to dicts with proper datetime serialization
        # Use mode='json' to convert datetime objects to ISO format strings
        serialized_files = [
            f.model_dump(mode="json") if hasattr(f, "model_dump") else f
            for f in files_data
        ]

        # Bug #336: Filter cidx-meta files to only show repos the user can access
        if repository_alias and "cidx-meta" in repository_alias:
            access_filtering_service = _get_access_filtering_service()
            if access_filtering_service:
                filenames = [Path(f["path"]).name for f in serialized_files]
                allowed = set(
                    access_filtering_service.filter_cidx_meta_files(
                        filenames, user.username
                    )
                )
                serialized_files = [
                    f for f in serialized_files if Path(f["path"]).name in allowed
                ]

        return _mcp_response({"success": True, "files": serialized_files})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "files": []})


def get_file_content(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get content of a specific file with optional pagination.

    Returns MCP-compliant response with content as array of text blocks.
    Per MCP spec, content must be an array of content blocks, each with 'type' and 'text' fields.

    Pagination parameters:
    - offset: 1-indexed line number to start reading from (optional, default: 1)
    - limit: Maximum number of lines to return (optional, default: None = all lines)
    """
    from pathlib import Path

    try:
        repository_alias = params["repository_alias"]
        file_path = params["file_path"]

        # Bug #336: Check cidx-meta file-level access before returning content
        if repository_alias and "cidx-meta" in repository_alias:
            access_filtering_svc = _get_access_filtering_service()
            if access_filtering_svc:
                basename = Path(file_path).name
                allowed = access_filtering_svc.filter_cidx_meta_files(
                    [basename], user.username
                )
                if not allowed:
                    return _mcp_response(
                        {
                            "success": False,
                            "error": f"Access denied: you are not authorized to access '{basename}'",
                            "content": [],
                            "metadata": {},
                        }
                    )

        # Extract optional pagination parameters
        # Coerce from MCP string types (MCP protocol sends all values as strings)
        # Non-integer floats (e.g. 3.14) are treated as invalid (coerced to 0, fails >= 1 check)
        _offset_raw = params.get("offset")
        _limit_raw = params.get("limit")
        _offset_invalid = (
            isinstance(_offset_raw, float) and not float(_offset_raw).is_integer()
        )
        _limit_invalid = (
            isinstance(_limit_raw, float) and not float(_limit_raw).is_integer()
        )
        offset = (
            0
            if _offset_invalid
            else (_coerce_int(_offset_raw, 0) if _offset_raw is not None else None)
        )
        limit = (
            0
            if _limit_invalid
            else (_coerce_int(_limit_raw, 0) if _limit_raw is not None else None)
        )

        # Validate offset if provided
        if offset is not None:
            if offset < 1:
                return _mcp_response(
                    {
                        "success": False,
                        "error": "offset must be an integer >= 1",
                        "content": [],
                        "metadata": {},
                    }
                )

        # Validate limit if provided
        if limit is not None:
            if limit < 1:
                return _mcp_response(
                    {
                        "success": False,
                        "error": "limit must be an integer >= 1",
                        "content": [],
                        "metadata": {},
                    }
                )

        # Check if this is a global repository (ends with -global suffix)
        if repository_alias and repository_alias.endswith("-global"):
            # Look up global repo in GlobalRegistry to get actual path
            golden_repos_dir = _get_golden_repos_dir()

            global_repos = _list_global_repos()

            # Find the matching global repo
            repo_entry = next(
                (r for r in global_repos if r["alias_name"] == repository_alias), None
            )

            if not repo_entry:
                available_repos = _get_available_repos(user)
                error_envelope = _error_with_suggestions(
                    error_msg=f"Global repository '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["content"] = []
                error_envelope["metadata"] = {}
                return _mcp_response(error_envelope)

            # Use AliasManager to get current target path (registry path becomes stale after refresh)
            from code_indexer.global_repos.alias_manager import AliasManager

            alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
            target_path = alias_manager.read_alias(repository_alias)

            if not target_path:
                available_repos = _get_available_repos(user)
                error_envelope = _error_with_suggestions(
                    error_msg=f"Alias for '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["content"] = []
                error_envelope["metadata"] = {}
                return _mcp_response(error_envelope)

            # Use resolved path for file_service with pagination parameters
            # Story #33 Fix: Use skip_truncation=True so TruncationHelper handles
            # truncation with cache_handle support (avoids double truncation)
            result = _utils.app_module.file_service.get_file_content_by_path(
                repo_path=target_path,
                file_path=file_path,
                offset=offset,
                limit=limit,
                skip_truncation=True,
            )
        else:
            # Call file_service with pagination parameters
            # Story #33 Fix: Use skip_truncation=True so TruncationHelper handles
            # truncation with cache_handle support (avoids double truncation)
            result = _utils.app_module.file_service.get_file_content(
                repository_alias=repository_alias,
                file_path=file_path,
                username=user.username,
                offset=offset,
                limit=limit,
                skip_truncation=True,
            )

        # Story #33: Apply token-based truncation with cache handle support
        file_content = result.get("content", "")
        metadata = result.get("metadata", {})

        # Get payload cache and content limits config
        payload_cache = getattr(_utils.app_module.app.state, "payload_cache", None)
        config_service = get_config_service()
        content_limits = config_service.get_config().content_limits_config

        # Apply truncation if cache is available
        cache_handle = None
        truncated = False
        total_tokens = 0
        preview_tokens = 0
        total_pages = 0
        has_more = False

        if payload_cache is not None and file_content and content_limits is not None:
            from code_indexer.server.cache.truncation_helper import TruncationHelper

            truncation_helper = TruncationHelper(payload_cache, content_limits)
            truncation_result = truncation_helper.truncate_and_cache(
                content=file_content,
                content_type="file",
            )

            file_content = truncation_result.preview
            cache_handle = truncation_result.cache_handle
            truncated = truncation_result.truncated
            total_tokens = truncation_result.original_tokens
            preview_tokens = truncation_result.preview_tokens
            total_pages = truncation_result.total_pages
            has_more = truncation_result.has_more

        # MCP spec: content must be array of content blocks
        content_blocks = (
            [{"type": "text", "text": file_content}] if file_content else []
        )

        # Story #33: Add truncation fields to metadata for backward compatibility.
        # Note: cache_handle, truncated, total_pages, has_more appear in BOTH metadata
        # and top-level response.
        # - Metadata location: For clients that parse nested metadata object
        # - Top-level location: For clients that expect flat response structure
        # This duplication ensures backward compatibility with existing clients (AC4).
        metadata["cache_handle"] = cache_handle
        metadata["truncated"] = truncated
        metadata["total_tokens"] = total_tokens
        metadata["preview_tokens"] = preview_tokens
        metadata["total_pages"] = total_pages
        metadata["has_more"] = has_more

        # Story #292: Enrich metadata with wiki_url for .md files from wiki-enabled repos (AC1, AC4)
        wiki_enabled_repos = _get_wiki_enabled_repos()
        _enrich_with_wiki_url(metadata, file_path, repository_alias, wiki_enabled_repos)

        return _mcp_response(
            {
                "success": True,
                "content": content_blocks,
                "metadata": metadata,
                # Duplicate at top level for flat response structure clients
                "cache_handle": cache_handle,
                "truncated": truncated,
                "total_pages": total_pages,
                "has_more": has_more,
            }
        )
    except Exception as e:
        # Even on error, content must be an array (empty array is valid)
        return _mcp_response(
            {"success": False, "error": str(e), "content": [], "metadata": {}}
        )


def browse_directory(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Browse directory recursively.

    FileListingService doesn't have browse_directory method.
    Use list_files with path patterns instead.
    """
    from code_indexer.server.models.api_models import FileListQueryParams
    from pathlib import Path

    try:
        repository_alias = params["repository_alias"]
        path = params.get("path", "")
        recursive = params.get("recursive", True)
        user_path_pattern = params.get("path_pattern")
        language = params.get("language")
        limit = _coerce_int(params.get("limit"), 500)
        sort_by = params.get("sort_by", "path")

        # Validate limit range
        if limit < 1:
            limit = 1
        elif limit > 500:
            limit = 500

        # Validate sort_by value
        if sort_by not in ("path", "size", "modified_at"):
            sort_by = "path"

        # Check if this is a global repository (ends with -global suffix)
        if repository_alias and repository_alias.endswith("-global"):
            # Look up global repo in GlobalRegistry to get actual path
            golden_repos_dir = _get_golden_repos_dir()

            global_repos = _list_global_repos()

            # Find the matching global repo
            repo_entry = next(
                (r for r in global_repos if r["alias_name"] == repository_alias), None
            )

            if not repo_entry:
                available_repos = _get_available_repos(user)
                error_envelope = _error_with_suggestions(
                    error_msg=f"Global repository '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["structure"] = {}
                return _mcp_response(error_envelope)

            # Use AliasManager to get current target path (registry path becomes stale after refresh)
            from code_indexer.global_repos.alias_manager import AliasManager

            alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
            target_path = alias_manager.read_alias(repository_alias)

            if not target_path:
                available_repos = _get_available_repos(user)
                error_envelope = _error_with_suggestions(
                    error_msg=f"Alias for '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["structure"] = {}
                return _mcp_response(error_envelope)

            # Use resolved path instead of alias for file_service
            repository_alias = target_path
            is_global_repo = True
        else:
            is_global_repo = False

        # Build path pattern combining path and user's pattern
        final_path_pattern = None
        # Normalize path first (remove trailing slash) - "/" becomes ""
        path = path.rstrip("/") if path else ""

        # Determine if user_path_pattern is absolute or relative
        # Absolute patterns contain '/' or '**' (e.g., "code/src/**/*.java", "**/*.py", "src/main/*.py")
        # Relative patterns are simple globs (e.g., "*.py", "*.{py,java}")
        is_absolute_pattern = False
        if user_path_pattern:
            is_absolute_pattern = (
                "/" in user_path_pattern or user_path_pattern.startswith("**")
            )

        if path:
            # Base pattern for the specified directory
            base_pattern = f"{path}/**/*" if recursive else f"{path}/*"
            if user_path_pattern:
                if is_absolute_pattern:
                    # Absolute pattern: use it directly, ignore path parameter
                    # e.g., path="wrong/path", path_pattern="code/src/**/*.java" -> "code/src/**/*.java"
                    final_path_pattern = user_path_pattern
                else:
                    # Relative pattern: combine path with user's pattern
                    # e.g., path="src", path_pattern="*.py" -> "src/**/*.py"
                    if recursive:
                        final_path_pattern = f"{path}/**/{user_path_pattern}"
                    else:
                        final_path_pattern = f"{path}/{user_path_pattern}"
            else:
                final_path_pattern = base_pattern
        elif user_path_pattern:
            # Just use the user's pattern directly
            final_path_pattern = user_path_pattern
        # else: final_path_pattern stays None (all files)

        # Use list_files with user-specified limit
        query_params = FileListQueryParams(
            page=1,
            limit=limit,
            path_pattern=final_path_pattern,
            language=language,
            sort_by=sort_by,
        )

        if is_global_repo:
            result = _utils.app_module.file_service.list_files_by_path(
                repo_path=repository_alias,
                query_params=query_params,
            )
        else:
            result = _utils.app_module.file_service.list_files(
                repo_id=repository_alias,
                username=user.username,
                query_params=query_params,
            )

        # Convert FileInfo objects to dict structure
        files_data = (
            result.files if hasattr(result, "files") else result.get("files", [])
        )
        serialized_files = [
            f.model_dump(mode="json") if hasattr(f, "model_dump") else f
            for f in files_data
        ]

        # Bug #336: Filter cidx-meta files to only show repos the user can access
        if repository_alias and "cidx-meta" in repository_alias:
            access_filtering_service = _get_access_filtering_service()
            if access_filtering_service:
                filenames = [Path(f["path"]).name for f in serialized_files]
                allowed = set(
                    access_filtering_service.filter_cidx_meta_files(
                        filenames, user.username
                    )
                )
                serialized_files = [
                    f for f in serialized_files if Path(f["path"]).name in allowed
                ]

        # Build directory structure from file list
        structure = {
            "path": path or "/",
            "files": serialized_files,
            "total": len(serialized_files),
        }

        return _mcp_response({"success": True, "structure": structure})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "structure": {}})


def get_branches(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get available branches for a repository."""
    from pathlib import Path
    from code_indexer.services.git_topology_service import GitTopologyService
    from code_indexer.server.services.branch_service import BranchService

    try:
        repository_alias = params["repository_alias"]
        include_remote = params.get("include_remote", False)

        # Check if this is a global repository (ends with -global suffix)
        if repository_alias and repository_alias.endswith("-global"):
            # Look up global repo in GlobalRegistry to get actual path
            golden_repos_dir = _get_golden_repos_dir()

            global_repos = _list_global_repos()

            # Find the matching global repo
            repo_entry = next(
                (r for r in global_repos if r["alias_name"] == repository_alias), None
            )

            if not repo_entry:
                available_repos = _get_available_repos(user)
                error_envelope = _error_with_suggestions(
                    error_msg=f"Global repository '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["branches"] = []
                return _mcp_response(error_envelope)

            # Use AliasManager to get current target path (registry path becomes stale after refresh)
            from code_indexer.global_repos.alias_manager import AliasManager

            alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
            target_path = alias_manager.read_alias(repository_alias)

            if not target_path:
                available_repos = _get_available_repos(user)
                error_envelope = _error_with_suggestions(
                    error_msg=f"Alias for '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["branches"] = []
                return _mcp_response(error_envelope)

            # Use resolved path for git operations
            repo_path = target_path
        else:
            # Get repository path (matches app.py endpoint pattern at line 4383-4395)
            repo_path = (
                _utils.app_module.activated_repo_manager.get_activated_repo_path(
                    username=user.username,
                    user_alias=repository_alias,
                )
            )

        # Initialize git topology service
        git_topology_service = GitTopologyService(Path(repo_path))

        # Use BranchService as context manager (matches app.py pattern at line 4404-4408)
        with BranchService(
            git_topology_service=git_topology_service, index_status_manager=None
        ) as branch_service:
            # Get branch information
            branches = branch_service.list_branches(include_remote=include_remote)

            # Convert BranchInfo objects to dicts for JSON serialization
            branches_data = [
                {
                    "name": b.name,
                    "is_current": b.is_current,
                    "last_commit": {
                        "sha": b.last_commit.sha,
                        "message": b.last_commit.message,
                        "author": b.last_commit.author,
                        "date": b.last_commit.date,
                    },
                    "index_status": (
                        {
                            "status": b.index_status.status,
                            "files_indexed": b.index_status.files_indexed,
                            "total_files": b.index_status.total_files,
                            "last_indexed": b.index_status.last_indexed,
                            "progress_percentage": b.index_status.progress_percentage,
                        }
                        if b.index_status
                        else None
                    ),
                    "remote_tracking": (
                        {
                            "remote": b.remote_tracking.remote,
                            "ahead": b.remote_tracking.ahead,
                            "behind": b.remote_tracking.behind,
                        }
                        if b.remote_tracking
                        else None
                    ),
                }
                for b in branches
            ]

            return _mcp_response({"success": True, "branches": branches_data})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "branches": []})


def check_health(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Check system health status."""
    try:
        from code_indexer import __version__
        from code_indexer.server.services.health_service import health_service

        # Call the actual method (not async)
        health_response = health_service.get_system_health()

        # Story #506: Include node_id in response for cluster mode identification.
        # In standalone mode node_id is None (app.state.node_id not set).
        node_id = getattr(_utils.app_module.app.state, "node_id", None)

        # Use mode='json' to serialize datetime objects to ISO format strings
        return _mcp_response(
            {
                "success": True,
                "server_version": __version__,
                "node_id": node_id,
                "health": health_response.model_dump(mode="json"),
            }
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "health": {}})


def check_hnsw_health(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Check HNSW index health and integrity for a repository.

    Performs comprehensive health check on the repository's HNSW index including:
    - File existence and readability
    - HNSW loadability
    - Integrity validation (connections, inbound links)
    - File metadata (size, modification time)

    Results are cached for 5 minutes unless force_refresh=True.
    """
    try:
        from pathlib import Path

        repository_alias = params.get("repository_alias")
        force_refresh = params.get("force_refresh", False)

        if not repository_alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: repository_alias",
                }
            )

        # Resolve repository alias to clone path
        repo = _utils.app_module.golden_repo_manager.get_golden_repo(repository_alias)
        if not repo:
            return _mcp_response(
                {"success": False, "error": f"Repository not found: {repository_alias}"}
            )

        # Construct index path (assumes default collection name)
        clone_path = Path(repo.clone_path)
        index_path = clone_path / ".code-indexer" / "index" / "default" / "index.bin"

        # Get singleton service instance (cache persists across requests)
        health_service = _get_hnsw_health_service()

        # Perform health check
        result = health_service.check_health(
            index_path=str(index_path),
            force_refresh=force_refresh,
        )

        # Use mode='json' to serialize datetime objects to ISO format strings
        return _mcp_response(
            {
                "success": True,
                "health": result.model_dump(mode="json"),
            }
        )

    except Exception as e:
        logger.exception(
            f"Error in check_hnsw_health: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def add_golden_repo(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Add a golden repository (admin only).

    Supports temporal indexing via enable_temporal and temporal_options parameters.
    When enable_temporal=True, the repository will be indexed with --index-commits
    to support time-based searches (git history search).
    """
    try:
        repo_url = params["url"]
        alias = params["alias"]
        default_branch = params.get("branch", "main")

        # Extract temporal indexing parameters (Story #527)
        enable_temporal = params.get("enable_temporal", False)
        temporal_options = params.get("temporal_options")

        job_id = _utils.app_module.golden_repo_manager.add_golden_repo(
            repo_url=repo_url,
            alias=alias,
            default_branch=default_branch,
            enable_temporal=enable_temporal,
            temporal_options=temporal_options,
            submitter_username=user.username,
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Golden repository '{alias}' addition started",
            }
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e)})


def remove_golden_repo(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Remove a golden repository (admin only)."""
    try:
        alias = params["alias"]
        job_id = _utils.app_module.golden_repo_manager.remove_golden_repo(
            alias, submitter_username=user.username
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Golden repository '{alias}' removal started",
            }
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e)})


def refresh_golden_repo(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Refresh a golden repository (admin only)."""
    try:
        alias = params["alias"]
        # Validate repo exists via golden_repo_manager before scheduling
        if alias not in _utils.app_module.golden_repo_manager.golden_repos:
            raise Exception(f"Golden repository '{alias}' not found")
        # Delegate to RefreshScheduler (index-source-first versioned pipeline)
        refresh_scheduler = _get_app_refresh_scheduler()
        if refresh_scheduler is None:
            raise Exception("RefreshScheduler not available")
        # Resolution from bare alias to global format happens inside RefreshScheduler
        job_id = refresh_scheduler.trigger_refresh_for_repo(
            alias, submitter_username=user.username
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Golden repository '{alias}' refresh started",
            }
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "job_id": None})


def change_golden_repo_branch(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Change the active branch of a golden repository async (Story #308)."""
    alias = params.get("alias")
    branch = params.get("branch")

    if not alias or not branch:
        return _mcp_response(
            {
                "success": False,
                "error": "Missing required parameters: 'alias' and 'branch'",
            }
        )

    try:
        result = _utils.app_module.golden_repo_manager.change_branch_async(
            alias, branch, user.username
        )
        job_id = result.get("job_id")
        if job_id is None:
            return _mcp_response(
                {
                    "success": True,
                    "message": f"Already on branch '{branch}'. No action taken.",
                }
            )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": "Branch change started. Use get_job_details to poll.",
            }
        )
    except Exception as e:
        from code_indexer.server.repositories.background_jobs import DuplicateJobError

        if isinstance(e, DuplicateJobError):
            return _mcp_response(
                {
                    "success": False,
                    "error": str(e),
                    "existing_job_id": e.existing_job_id,
                }
            )
        return _mcp_response({"success": False, "error": str(e)})


def get_repository_statistics(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get repository statistics."""
    from pathlib import Path

    try:
        repository_alias = params["repository_alias"]

        # Check if this is a global repository (ends with -global suffix)
        if repository_alias and repository_alias.endswith("-global"):
            golden_repos_dir = _get_golden_repos_dir()
            global_repos = _list_global_repos()

            repo_entry = next(
                (r for r in global_repos if r["alias_name"] == repository_alias), None
            )

            if not repo_entry:
                available_repos = _get_available_repos(user)
                error_envelope = _error_with_suggestions(
                    error_msg=f"Global repository '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["statistics"] = {}
                return _mcp_response(error_envelope)

            from code_indexer.global_repos.alias_manager import AliasManager

            alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
            target_path = alias_manager.read_alias(repository_alias)

            if not target_path:
                available_repos = _get_available_repos(user)
                error_envelope = _error_with_suggestions(
                    error_msg=f"Alias for '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["statistics"] = {}
                return _mcp_response(error_envelope)

            # Build basic statistics for global repo
            statistics = {
                "repository_alias": repository_alias,
                "is_global": True,
                "path": target_path,
                "index_path": repo_entry.get("index_path"),
            }
            return _mcp_response({"success": True, "statistics": statistics})

        # Activated repository (original code)
        from code_indexer.server.services.stats_service import stats_service

        stats_response = stats_service.get_repository_stats(
            repository_alias, username=user.username
        )
        return _mcp_response(
            {"success": True, "statistics": stats_response.model_dump(mode="json")}
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "statistics": {}})


def get_all_repositories_status(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get status summary of all repositories."""
    try:
        # Get activated repos status
        repos = _utils.app_module.activated_repo_manager.list_activated_repositories(
            user.username
        )
        status_summary = []
        for repo in repos:
            try:
                details = (
                    _utils.app_module.repository_listing_manager.get_repository_details(
                        repo["user_alias"], user.username
                    )
                )
                status_summary.append(details)
            except Exception:
                continue

        # Get global repos status (same pattern as list_repositories handler)
        try:
            global_repos_data = _list_global_repos()

            # Story #316: Filter global repos by user's group access
            access_filtering_service = _get_access_filtering_service()
            if access_filtering_service:
                repo_names = [r.get("repo_name", "") for r in global_repos_data]
                accessible = access_filtering_service.filter_repo_listing(
                    repo_names, user.username
                )
                global_repos_data = [
                    r for r in global_repos_data if r.get("repo_name", "") in accessible
                ]

            for repo in global_repos_data:
                if "alias_name" not in repo or "repo_name" not in repo:
                    logger.warning(
                        format_error_log(
                            "MCP-GENERAL-035",
                            f"Skipping malformed global repo entry: {repo}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    continue

                global_status = {
                    "user_alias": repo["alias_name"],
                    "golden_repo_alias": repo["repo_name"],
                    "current_branch": None,
                    "is_global": True,
                    "repo_url": repo.get("repo_url"),
                    "last_refresh": repo.get("last_refresh"),
                    "index_path": repo.get("index_path"),
                    "created_at": repo.get("created_at"),
                }
                status_summary.append(global_status)
        except Exception as e:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-036",
                    f"Failed to load global repos status: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        return _mcp_response(
            {
                "success": True,
                "repositories": status_summary,
                "total": len(status_summary),
            }
        )
    except Exception as e:
        return _mcp_response(
            {"success": False, "error": str(e), "repositories": [], "total": 0}
        )


def manage_composite_repository(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Manage composite repository operations."""
    try:
        operation = params["operation"]
        user_alias = params["user_alias"]
        golden_repo_aliases = params.get("golden_repo_aliases", [])

        # Story #331 AC5: Validate user has access to all component repos
        access_service = _get_access_filtering_service()
        if access_service and golden_repo_aliases:
            if not access_service.is_admin_user(user.username):
                accessible = access_service.get_accessible_repos(user.username)
                for component_alias in golden_repo_aliases:
                    normalized = component_alias
                    if normalized.endswith("-global"):
                        normalized = normalized[: -len("-global")]
                    if normalized not in accessible:
                        return _mcp_response(
                            {
                                "success": False,
                                "error": f"Access denied: repository '{component_alias}' is not accessible.",
                                "job_id": None,
                            }
                        )

        if operation == "create":
            job_id = _utils.app_module.activated_repo_manager.activate_repository(
                username=user.username,
                golden_repo_aliases=golden_repo_aliases,
                user_alias=user_alias,
            )
            return _mcp_response(
                {
                    "success": True,
                    "job_id": job_id,
                    "message": f"Composite repository '{user_alias}' creation started",
                }
            )

        elif operation == "update":
            # For update, deactivate then reactivate
            try:
                _utils.app_module.activated_repo_manager.deactivate_repository(
                    username=user.username, user_alias=user_alias
                )
            except Exception:
                pass  # Ignore if doesn't exist

            job_id = _utils.app_module.activated_repo_manager.activate_repository(
                username=user.username,
                golden_repo_aliases=golden_repo_aliases,
                user_alias=user_alias,
            )
            return _mcp_response(
                {
                    "success": True,
                    "job_id": job_id,
                    "message": f"Composite repository '{user_alias}' update started",
                }
            )

        elif operation == "delete":
            job_id = _utils.app_module.activated_repo_manager.deactivate_repository(
                username=user.username, user_alias=user_alias
            )
            return _mcp_response(
                {
                    "success": True,
                    "job_id": job_id,
                    "message": f"Composite repository '{user_alias}' deletion started",
                }
            )

        else:
            return _mcp_response(
                {"success": False, "error": f"Unknown operation: {operation}"}
            )

    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "job_id": None})


def handle_list_global_repos(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for list_global_repos tool."""
    repos = _list_global_repos()

    # Story #316: Apply group-based access filtering
    access_filtering_service = _get_access_filtering_service()
    if access_filtering_service:
        repo_aliases = [r.get("repo_name", r.get("alias", "")) for r in repos]
        accessible_aliases = access_filtering_service.filter_repo_listing(
            repo_aliases, user.username
        )
        repos = [
            r
            for r in repos
            if r.get("repo_name", r.get("alias", "")) in accessible_aliases
        ]

    return _mcp_response({"success": True, "repos": repos})


def handle_global_repo_status(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for global_repo_status tool."""
    from code_indexer.global_repos.shared_operations import GlobalRepoOperations

    golden_repos_dir = _get_golden_repos_dir()
    ops = GlobalRepoOperations(golden_repos_dir)
    alias = args.get("alias")

    if not alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: alias"}
        )

    try:
        status = ops.get_status(alias)
        return _mcp_response({"success": True, **status})
    except ValueError:
        return _mcp_response(
            {"success": False, "error": f"Global repo '{alias}' not found"}
        )


def handle_add_golden_repo_index(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for add_golden_repo_index tool (Story #596 AC1, AC3, AC4, AC5)."""
    alias = args.get("alias")
    index_type = args.get("index_type")

    # Validate required parameters
    if not alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: alias"}
        )

    if not index_type:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: index_type"}
        )

    try:
        # Get GoldenRepoManager from app state
        golden_repo_manager = getattr(_utils.app_module, "golden_repo_manager", None)
        if not golden_repo_manager:
            return _mcp_response(
                {"success": False, "error": "Golden repository manager not available"}
            )

        # Call backend method to submit background job
        job_id = golden_repo_manager.add_index_to_golden_repo(
            alias=alias, index_type=index_type, submitter_username=user.username
        )

        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Index type '{index_type}' is being added to golden repo '{alias}'. Use get_job_statistics to track progress.",
            }
        )

    except ValueError as e:
        # AC4: Unknown alias, AC3: Invalid type, AC5: Already exists
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-037",
                f"Error adding index to golden repo: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {"success": False, "error": f"Failed to add index: {str(e)}"}
        )


def handle_get_golden_repo_indexes(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for get_golden_repo_indexes tool (Story #596 AC2, AC4)."""
    alias = args.get("alias")

    # Validate required parameter
    if not alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: alias"}
        )

    try:
        # Get GoldenRepoManager from app state
        golden_repo_manager = getattr(_utils.app_module, "golden_repo_manager", None)
        if not golden_repo_manager:
            return _mcp_response(
                {"success": False, "error": "Golden repository manager not available"}
            )

        # Get index status from backend
        status = golden_repo_manager.get_golden_repo_indexes(alias)

        return _mcp_response({"success": True, **status})

    except ValueError as e:
        # AC4: Unknown alias
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-038",
                f"Error getting golden repo indexes: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {"success": False, "error": f"Failed to get indexes: {str(e)}"}
        )


async def _omni_regex_search(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handle omni-regex search across multiple repositories."""
    import json as json_module
    import time

    repo_aliases = args.get("repository_alias", [])
    repo_aliases = _expand_wildcard_patterns(repo_aliases, user)

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
    all_matches = []
    errors = {}
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

    # Story #331 AC7: Filter errors dict to hide unauthorized repo aliases
    access_service = _get_access_filtering_service()
    if access_service and not access_service.is_admin_user(user.username):
        accessible = access_service.get_accessible_repos(user.username)
        errors = {
            k: v
            for k, v in errors.items()
            if k.replace("-global", "") in accessible or k in accessible
        }

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


async def handle_regex_search(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for regex_search tool - pattern matching with timeout protection."""
    from pathlib import Path
    from code_indexer.global_repos.regex_search import RegexSearchService
    from code_indexer.server.services.config_service import get_config_service
    from code_indexer.server.services.search_error_formatter import SearchErrorFormatter

    # Story #4 AC2: Metrics tracking moved to service layer
    # RegexSearchService.search() now handles metrics
    # This ensures both MCP and REST API calls are counted

    repository_alias = args.get("repository_alias")
    repository_alias = _parse_json_string_array(repository_alias)
    args["repository_alias"] = repository_alias  # Update args for downstream

    # Bug #139: Validate include/exclude patterns BEFORE routing to omni-search
    # This ensures validation runs for both single-repo and omni-search modes
    include_patterns = args.get("include_patterns")
    if include_patterns is not None and not isinstance(include_patterns, list):
        return _mcp_response(
            {"success": False, "error": "include_patterns must be a list of strings"}
        )

    exclude_patterns = args.get("exclude_patterns")
    if exclude_patterns is not None and not isinstance(exclude_patterns, list):
        return _mcp_response(
            {"success": False, "error": "exclude_patterns must be a list of strings"}
        )

    # Route to omni-search when repository_alias is an array
    if isinstance(repository_alias, list):
        return await _omni_regex_search(args, user)

    pattern = args.get("pattern")

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if not pattern:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: pattern"}
        )

    try:
        golden_repos_dir = _get_golden_repos_dir()

        # Resolve repository_alias to actual repo path (not index path)
        # Uses _resolve_repo_path which handles all location variants
        resolved = _resolve_repo_path(repository_alias, golden_repos_dir)
        if not resolved:
            return _mcp_response(
                {
                    "success": False,
                    "error": f"Repository '{repository_alias}' not found",
                }
            )
        repo_path = Path(resolved)

        # Get search limits configuration from consolidated config
        config_service = get_config_service()
        config = config_service.get_config()
        search_limits = config.search_limits_config
        # Story #27: Get subprocess_max_workers from background_jobs_config
        subprocess_max_workers = config.background_jobs_config.subprocess_max_workers

        # Create service and execute search with timeout protection
        service = RegexSearchService(
            repo_path, subprocess_max_workers=subprocess_max_workers
        )
        result = await service.search(
            pattern=pattern,
            path=args.get("path"),
            include_patterns=args.get("include_patterns"),
            exclude_patterns=args.get("exclude_patterns"),
            case_sensitive=args.get("case_sensitive", True),
            context_lines=int(args.get("context_lines", 0)),
            max_results=args.get("max_results", 100),
            timeout_seconds=search_limits.timeout_seconds,
            multiline=args.get("multiline", False),
            pcre2=args.get("pcre2", False),
        )

        # Convert dataclass to dict for JSON serialization
        matches = [
            {
                "file_path": m.file_path,
                "line_number": m.line_number,
                "column": m.column,
                "line_content": m.line_content,
                "context_before": m.context_before,
                "context_after": m.context_after,
            }
            for m in result.matches
        ]

        # Story #292: Enrich matches with wiki_url for .md files from wiki-enabled repos (AC1, AC4)
        wiki_enabled_repos = _get_wiki_enabled_repos()
        for match in matches:
            _enrich_with_wiki_url(
                match,
                match.get("file_path", ""),
                repository_alias,
                wiki_enabled_repos,
            )

        # Story #653/#654: Apply cross-encoder reranking after retrieval, before truncation
        _regex_limit = _coerce_int(args.get("max_results"), len(matches))
        _rerank_kwargs = dict(
            results=matches,
            rerank_query=args.get("rerank_query"),
            rerank_instruction=args.get("rerank_instruction"),
            content_extractor=lambda r: r.get("line_content", "") or "",
            requested_limit=_regex_limit,
            config_service=get_config_service(),
        )
        matches, _rerank_meta = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _mcp_reranking._apply_reranking_sync(**_rerank_kwargs)
        )

        # Story #684: Apply payload truncation to regex search results
        # Story #50: Truncation functions are now sync
        matches = _apply_regex_payload_truncation(matches)

        # Bug #337: Filter cidx-meta regex_search results to only show
        # matches from repo files the user is authorized to access.
        if repository_alias and "cidx-meta" in repository_alias:
            access_svc = _get_access_filtering_service()
            if access_svc:
                filenames = [Path(m["file_path"]).name for m in matches]
                allowed = set(
                    access_svc.filter_cidx_meta_files(filenames, user.username)
                )
                matches = [m for m in matches if Path(m["file_path"]).name in allowed]

        return _mcp_response(
            {
                "success": True,
                "matches": matches,
                "total_matches": len(matches),
                "truncated": result.truncated,
                "search_engine": result.search_engine,
                "search_time_ms": result.search_time_ms,
                # Story #654: reranker telemetry
                "query_metadata": {
                    "reranker_used": _rerank_meta["reranker_used"],
                    "reranker_provider": _rerank_meta["reranker_provider"],
                    "rerank_time_ms": _rerank_meta["rerank_time_ms"],
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
        # Format timeout error response
        error_formatter = SearchErrorFormatter()
        config_service = get_config_service()
        search_limits = config_service.get_config().search_limits_config
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


# =============================================================================
# File CRUD Handlers (Story #628)
# =============================================================================


def handle_create_file(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Create new file in activated repository.

    Args:
        params: Dictionary with repository_alias, file_path, content
        user: User performing the operation

    Returns:
        MCP response with file metadata including content_hash for optimistic locking
    """
    from code_indexer.server.services.file_crud_service import (
        file_crud_service,
        CRUDOperationError,
    )
    from code_indexer.server.services.auto_watch_manager import auto_watch_manager
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    try:
        # Validate required parameters
        repository_alias = params.get("repository_alias")
        file_path = params.get("file_path")
        content = params.get("content")

        if not repository_alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: repository_alias",
                }
            )
        if not file_path:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: file_path"}
            )
        if content is None:  # Allow empty string content
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: content"}
            )

        # Start auto-watch before file creation (Story #640, Story #197 AC3)
        # Skip during write mode — write mode manages the full indexing lifecycle
        # via exit_write_mode / _write_mode_run_refresh (Bug #274 Bug 1).
        try:
            # Check if this is a write exception (e.g., cidx-meta-global)
            if file_crud_service.is_write_exception(repository_alias):
                # Use canonical golden repo path for exceptions
                repo_path = str(
                    file_crud_service.get_write_exception_path(repository_alias)
                )
            else:
                # Use activated repo path for normal repos
                activated_repo_manager = ActivatedRepoManager()
                repo_path = activated_repo_manager.get_activated_repo_path(
                    username=user.username, user_alias=repository_alias
                )
            golden_repos_dir = getattr(
                _utils.app_module.app.state, "golden_repos_dir", None
            )
            if not _is_write_mode_active(repository_alias, golden_repos_dir):
                auto_watch_manager.start_watch(repo_path)
            else:
                logger.debug(
                    f"Skipping auto-watch start for {repository_alias}: write mode active",
                    extra={"correlation_id": get_correlation_id()},
                )
        except Exception as e:
            # Log but don't fail - auto-watch is enhancement, not critical
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-041",
                    f"Failed to start auto-watch for {repository_alias}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Call file CRUD service
        result = file_crud_service.create_file(
            repo_alias=repository_alias,
            file_path=file_path,
            content=content,
            username=user.username,
        )

        # Story #304: Invalidate wiki cache for markdown file changes (AC1)
        try:
            from code_indexer.server.wiki.wiki_cache_invalidator import (
                wiki_cache_invalidator,
            )

            wiki_cache_invalidator.invalidate_for_file_change(
                repository_alias, file_path
            )
        except Exception:
            pass  # Wiki cache invalidation is fire-and-forget

        return _mcp_response(result)

    except FileExistsError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-042",
                f"File creation failed - file already exists: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except PermissionError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-043",
                f"File creation failed - permission denied: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except CRUDOperationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-044",
                f"File creation failed - CRUD operation error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except ValueError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-045",
                f"File creation failed - invalid parameters: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in handle_create_file: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_edit_file(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Edit file using exact string replacement with optimistic locking.

    Args:
        params: Dictionary with repository_alias, file_path, old_string, new_string,
                content_hash, and optional replace_all
        user: User performing the operation

    Returns:
        MCP response with new content_hash and change metadata
    """
    from code_indexer.server.services.file_crud_service import (
        file_crud_service,
        HashMismatchError,
        CRUDOperationError,
    )
    from code_indexer.server.services.auto_watch_manager import auto_watch_manager
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    try:
        # Validate required parameters
        repository_alias = params.get("repository_alias")
        file_path = params.get("file_path")
        old_string = params.get("old_string")
        new_string = params.get("new_string")
        content_hash = params.get("content_hash")
        replace_all = params.get("replace_all", False)

        if not repository_alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: repository_alias",
                }
            )
        if not file_path:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: file_path"}
            )
        if old_string is None:  # Allow empty string
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: old_string"}
            )
        if new_string is None:  # Allow empty string
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: new_string"}
            )
        if not content_hash:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: content_hash"}
            )

        # Start auto-watch before file edit (Story #640, Story #197 AC3)
        # Skip during write mode — write mode manages the full indexing lifecycle
        # via exit_write_mode / _write_mode_run_refresh (Bug #274 Bug 1).
        try:
            # Check if this is a write exception (e.g., cidx-meta-global)
            if file_crud_service.is_write_exception(repository_alias):
                # Use canonical golden repo path for exceptions
                repo_path = str(
                    file_crud_service.get_write_exception_path(repository_alias)
                )
            else:
                # Use activated repo path for normal repos
                activated_repo_manager = ActivatedRepoManager()
                repo_path = activated_repo_manager.get_activated_repo_path(
                    username=user.username, user_alias=repository_alias
                )
            golden_repos_dir = getattr(
                _utils.app_module.app.state, "golden_repos_dir", None
            )
            if not _is_write_mode_active(repository_alias, golden_repos_dir):
                auto_watch_manager.start_watch(repo_path)
            else:
                logger.debug(
                    f"Skipping auto-watch start for {repository_alias}: write mode active",
                    extra={"correlation_id": get_correlation_id()},
                )
        except Exception as e:
            # Log but don't fail - auto-watch is enhancement, not critical
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-046",
                    f"Failed to start auto-watch for {repository_alias}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Call file CRUD service
        result = file_crud_service.edit_file(
            repo_alias=repository_alias,
            file_path=file_path,
            old_string=old_string,
            new_string=new_string,
            content_hash=content_hash,
            replace_all=replace_all,
            username=user.username,
        )

        # Story #304: Invalidate wiki cache for markdown file changes (AC1)
        try:
            from code_indexer.server.wiki.wiki_cache_invalidator import (
                wiki_cache_invalidator,
            )

            wiki_cache_invalidator.invalidate_for_file_change(
                repository_alias, file_path
            )
        except Exception as e:
            logger.debug(f"Wiki cache invalidation skipped for edit_file: {e}")

        return _mcp_response(result)

    except HashMismatchError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-047",
                f"File edit failed - hash mismatch (concurrent modification): {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except FileNotFoundError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-048",
                f"File edit failed - file not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except ValueError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-049",
                f"File edit failed - validation error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except PermissionError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-050",
                f"File edit failed - permission denied: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except CRUDOperationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-051",
                f"File edit failed - CRUD operation error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in handle_edit_file: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_delete_file(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Delete file from activated repository.

    Args:
        params: Dictionary with repository_alias, file_path, and optional content_hash
        user: User performing the operation

    Returns:
        MCP response with deletion confirmation
    """
    from code_indexer.server.services.file_crud_service import (
        file_crud_service,
        HashMismatchError,
        CRUDOperationError,
    )
    from code_indexer.server.services.auto_watch_manager import auto_watch_manager
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    try:
        # Validate required parameters
        repository_alias = params.get("repository_alias")
        file_path = params.get("file_path")
        content_hash = params.get("content_hash")  # Optional

        if not repository_alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: repository_alias",
                }
            )
        if not file_path:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: file_path"}
            )

        # Start auto-watch before file deletion (Story #640, Story #197 AC3)
        # Skip during write mode — write mode manages the full indexing lifecycle
        # via exit_write_mode / _write_mode_run_refresh (Bug #274 Bug 1).
        try:
            # Check if this is a write exception (e.g., cidx-meta-global)
            if file_crud_service.is_write_exception(repository_alias):
                # Use canonical golden repo path for exceptions
                repo_path = str(
                    file_crud_service.get_write_exception_path(repository_alias)
                )
            else:
                # Use activated repo path for normal repos
                activated_repo_manager = ActivatedRepoManager()
                repo_path = activated_repo_manager.get_activated_repo_path(
                    username=user.username, user_alias=repository_alias
                )
            golden_repos_dir = getattr(
                _utils.app_module.app.state, "golden_repos_dir", None
            )
            if not _is_write_mode_active(repository_alias, golden_repos_dir):
                auto_watch_manager.start_watch(repo_path)
            else:
                logger.debug(
                    f"Skipping auto-watch start for {repository_alias}: write mode active",
                    extra={"correlation_id": get_correlation_id()},
                )
        except Exception as e:
            # Log but don't fail - auto-watch is enhancement, not critical
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-052",
                    f"Failed to start auto-watch for {repository_alias}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Call file CRUD service
        result = file_crud_service.delete_file(
            repo_alias=repository_alias,
            file_path=file_path,
            content_hash=content_hash,
            username=user.username,
        )

        # Story #304: Invalidate wiki cache for markdown file changes (AC1)
        try:
            from code_indexer.server.wiki.wiki_cache_invalidator import (
                wiki_cache_invalidator,
            )

            wiki_cache_invalidator.invalidate_for_file_change(
                repository_alias, file_path
            )
        except Exception as e:
            logger.debug(f"Wiki cache invalidation skipped for delete_file: {e}")

        return _mcp_response(result)

    except HashMismatchError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-053",
                f"File deletion failed - hash mismatch (safety check): {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except FileNotFoundError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-054",
                f"File deletion failed - file not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except PermissionError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-055",
                f"File deletion failed - permission denied: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except CRUDOperationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-056",
                f"File deletion failed - CRUD operation error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except ValueError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-057",
                f"File deletion failed - invalid parameters: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in handle_delete_file: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Write-mode helpers (Story #231)
# ---------------------------------------------------------------------------


def _write_mode_strip_global(repo_alias: str) -> str:
    """Return alias without trailing '-global' suffix."""
    return (
        repo_alias[: -len("-global")] if repo_alias.endswith("-global") else repo_alias
    )


def _is_write_mode_active(repo_alias: str, golden_repos_dir: Optional[str]) -> bool:
    """Return True if a write-mode marker exists for repo_alias.

    Used by file operation handlers to suppress auto-watch during write mode.
    During write mode, the caller (enter_write_mode / exit_write_mode) manages
    the full indexing lifecycle, so auto-watch must not race with it.

    Args:
        repo_alias: Repository alias (with or without -global suffix)
        golden_repos_dir: Path to the golden repos root directory (may be None)

    Returns:
        True if write mode marker file exists for this repo alias, False otherwise
    """
    if not golden_repos_dir:
        return False
    alias = _write_mode_strip_global(repo_alias)
    marker_file = Path(golden_repos_dir) / ".write_mode" / f"{alias}.json"
    return marker_file.exists()


def _is_writable_repo(
    repo_alias: str, resolved_repo_path: Optional[str], golden_repos_dir: Optional[str]
) -> bool:
    """Return True if write operations are allowed for this repo.

    Bug #391: Activated repos (user workspaces) are always writable without a
    write-mode marker. Write-mode markers are only for special golden repos
    like cidx-meta that require explicit write mode activation.

    A repo is writable if:
    1. It has a write-mode marker (existing check for golden repos in write mode), OR
    2. Its resolved path is an activated repo workspace (contains /activated-repos/)

    Args:
        repo_alias: Repository alias (with or without -global suffix)
        resolved_repo_path: The filesystem path resolved for this repo, or None
        golden_repos_dir: Path to the golden repos root directory (may be None)

    Returns:
        True if write operations are permitted, False otherwise
    """
    # Check write-mode marker first (for golden repos in explicit write mode)
    if _is_write_mode_active(repo_alias, golden_repos_dir):
        return True

    # Bug #391: activated repo workspaces are inherently writable
    # They are identified by their path containing /activated-repos/
    if resolved_repo_path and "/activated-repos/" in resolved_repo_path:
        return True

    return False


def _write_mode_acquire_lock(refresh_scheduler: Any, alias: str) -> Tuple[bool, str]:
    """Acquire write lock; return (acquired, owner_if_held)."""
    acquired = refresh_scheduler.acquire_write_lock(alias, owner_name="mcp_write_mode")
    if acquired:
        return True, ""
    wlm = getattr(refresh_scheduler, "write_lock_manager", None)
    owner = "unknown"
    if wlm is not None:
        info = wlm.get_lock_info(alias)
        if info:
            owner = info.get("owner", "unknown")
    return False, owner


def _write_mode_create_marker(
    golden_repos_dir: Path, alias: str, source_path: str
) -> None:
    """Create the .write_mode/{alias}.json marker file."""
    import json as _json
    from datetime import datetime, timezone

    write_mode_dir = golden_repos_dir / ".write_mode"
    write_mode_dir.mkdir(parents=True, exist_ok=True)
    marker_file = write_mode_dir / f"{alias}.json"
    marker_file.write_text(
        _json.dumps(
            {
                "alias": alias,
                "source_path": source_path,
                "entered_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )


def handle_enter_write_mode(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Enter write mode for a write-exception repo (Story #231 C1).

    No-op for non-write-exception repos. For write-exception repos: acquires
    write lock, creates marker file, returns source path.
    """
    try:
        repo_alias = params.get("repo_alias")
        if not repo_alias:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo_alias"}
            )
        from code_indexer.server.services.file_crud_service import file_crud_service

        if not file_crud_service.is_write_exception(repo_alias):
            return _mcp_response(
                {
                    "success": True,
                    "message": f"no-op: '{repo_alias}' is not a write-exception repo",
                }
            )

        alias = _write_mode_strip_global(repo_alias)
        refresh_scheduler = _get_app_refresh_scheduler()
        if refresh_scheduler is None:
            return _mcp_response(
                {"success": False, "error": "RefreshScheduler not available"}
            )

        acquired, owner = _write_mode_acquire_lock(refresh_scheduler, alias)
        if not acquired:
            return _mcp_response(
                {
                    "success": False,
                    "message": f"Write lock for '{alias}' is already held by '{owner}'",
                }
            )

        try:
            source_path = file_crud_service.get_write_exception_path(repo_alias)
            golden_repos_dir = Path(_get_golden_repos_dir())
            _write_mode_create_marker(golden_repos_dir, alias, str(source_path))
        except Exception:
            refresh_scheduler.release_write_lock(alias, owner_name="mcp_write_mode")
            raise
        logger.info(
            f"enter_write_mode: write mode active for '{repo_alias}', source={source_path}"
        )
        return _mcp_response(
            {"success": True, "alias": repo_alias, "source_path": str(source_path)}
        )
    except Exception as e:
        logger.exception(
            f"Unexpected error in handle_enter_write_mode: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def _write_mode_run_refresh(
    refresh_scheduler: Any, repo_alias: str, golden_repos_dir: Path, alias: str
) -> None:
    """Run synchronous refresh, delete marker, release lock, stop auto-watch.

    The write lock and marker are released BEFORE calling _execute_refresh so
    that _execute_refresh does not see the lock as held and skip the refresh.
    (_execute_refresh checks is_write_locked() and skips for local repos when
    the lock is held — Story #227 guard for external writers.)

    The auto-watch is stopped BEFORE _execute_refresh to prevent the watch
    handler from racing with the refresh (Bug #274 Bug 3).

    The exception from _execute_refresh is re-raised so the caller can return
    an appropriate error response.
    """
    import json as _json

    from code_indexer.server.services.auto_watch_manager import auto_watch_manager

    # Read source_path from marker BEFORE deleting it — needed to stop auto-watch
    marker_file = golden_repos_dir / ".write_mode" / f"{alias}.json"
    source_path: Optional[str] = None
    try:
        marker_data = _json.loads(marker_file.read_text())
        source_path = marker_data.get("source_path")
    except Exception:
        pass  # marker may already be gone or unreadable — proceed without source_path

    # Release marker and lock FIRST so _execute_refresh sees no lock
    try:
        marker_file.unlink()
    except FileNotFoundError:
        pass
    refresh_scheduler.release_write_lock(alias, owner_name="mcp_write_mode")

    # Stop auto-watch for the repo BEFORE running refresh (Bug #274 Bug 3)
    # This prevents the watch handler from processing events that the refresh
    # will handle, avoiding hash mismatches and wasted VoyageAI API calls.
    if source_path:
        try:
            auto_watch_manager.stop_watch(source_path)
            logger.debug(
                f"Stopped auto-watch for {source_path} before write-mode refresh",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as e:
            logger.warning(
                f"Failed to stop auto-watch for {source_path} before refresh: {e}",
                extra={"correlation_id": get_correlation_id()},
            )

    # Now run the refresh — lock is clear, watch is stopped, refresh will proceed
    refresh_scheduler._execute_refresh(repo_alias)


def handle_exit_write_mode(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Exit write mode for a write-exception repo (Story #231 C2).

    No-op for non-write-exception repos. For write-exception repos: triggers
    synchronous refresh, removes marker, releases lock.
    """
    try:
        repo_alias = params.get("repo_alias")
        if not repo_alias:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo_alias"}
            )
        from code_indexer.server.services.file_crud_service import file_crud_service

        if not file_crud_service.is_write_exception(repo_alias):
            return _mcp_response(
                {
                    "success": True,
                    "message": f"no-op: '{repo_alias}' is not a write-exception repo",
                }
            )

        alias = _write_mode_strip_global(repo_alias)
        golden_repos_dir = Path(_get_golden_repos_dir())
        marker_file = golden_repos_dir / ".write_mode" / f"{alias}.json"

        if not marker_file.exists():
            logger.warning(
                f"exit_write_mode: no marker for '{repo_alias}' — not in write mode"
            )
            return _mcp_response(
                {
                    "success": True,
                    "warning": f"Write mode was not active for '{repo_alias}'",
                    "message": "not in write mode — nothing to exit",
                }
            )

        refresh_scheduler = _get_app_refresh_scheduler()
        if refresh_scheduler is None:
            return _mcp_response(
                {"success": False, "error": "RefreshScheduler not available"}
            )

        logger.info(
            f"exit_write_mode: triggering synchronous refresh for '{repo_alias}'"
        )
        _write_mode_run_refresh(refresh_scheduler, repo_alias, golden_repos_dir, alias)
        logger.info(
            f"exit_write_mode: write mode exited for '{repo_alias}', refresh complete"
        )

        # Story #304: Invalidate wiki cache after write mode exit (AC9)
        try:
            from code_indexer.server.wiki.wiki_cache_invalidator import (
                wiki_cache_invalidator,
            )

            wiki_cache_invalidator.invalidate_repo(repo_alias)
        except Exception as e:
            logger.debug(f"Wiki cache invalidation skipped for exit_write_mode: {e}")

        return _mcp_response(
            {
                "success": True,
                "message": f"Refresh complete, write mode exited for '{repo_alias}'",
            }
        )
    except Exception as e:
        logger.exception(
            f"Unexpected error in handle_exit_write_mode: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# Handler registry mapping tool names to handler functions
# Type: Dict[str, Any] because handlers have varying signatures (2-param vs 3-param)
HANDLER_REGISTRY: Dict[str, Any] = {
    "search_code": search_code,
    "discover_repositories": discover_repositories,
    "list_repositories": list_repositories,
    "activate_repository": activate_repository,
    "deactivate_repository": deactivate_repository,
    "get_repository_status": get_repository_status,
    "sync_repository": sync_repository,
    "switch_branch": switch_branch,
    "list_files": list_files,
    "get_file_content": get_file_content,
    "browse_directory": browse_directory,
    "get_branches": get_branches,
    "check_health": check_health,
    "check_hnsw_health": check_hnsw_health,
    "add_golden_repo": add_golden_repo,
    "remove_golden_repo": remove_golden_repo,
    "refresh_golden_repo": refresh_golden_repo,
    "change_golden_repo_branch": change_golden_repo_branch,
    "get_repository_statistics": get_repository_statistics,
    "get_all_repositories_status": get_all_repositories_status,
    "manage_composite_repository": manage_composite_repository,
    "list_global_repos": handle_list_global_repos,
    "global_repo_status": handle_global_repo_status,
    "add_golden_repo_index": handle_add_golden_repo_index,
    "get_golden_repo_indexes": handle_get_golden_repo_indexes,
    "regex_search": handle_regex_search,
    "create_file": handle_create_file,
    "edit_file": handle_edit_file,
    "delete_file": handle_delete_file,
    "enter_write_mode": handle_enter_write_mode,
    "exit_write_mode": handle_exit_write_mode,
}


def _is_git_repo(path: Path) -> bool:
    """Check if path is a valid git repository."""
    return path.exists() and (path / ".git").exists()


def _find_latest_versioned_repo(base_path: Path, repo_name: str) -> Optional[str]:
    """Find most recent versioned git repo in .versioned/{name}/v_*/ structure."""
    versioned_base = base_path / ".versioned" / repo_name
    if not versioned_base.exists():
        return None

    version_dirs = sorted(
        [d for d in versioned_base.iterdir() if d.is_dir() and d.name.startswith("v_")],
        key=lambda d: d.name,
        reverse=True,
    )

    for version_dir in version_dirs:
        if _is_git_repo(version_dir):
            return str(version_dir)

    return None


def _resolve_repo_path(repo_identifier: str, golden_repos_dir: str) -> Optional[str]:
    """Resolve repository identifier to filesystem path.

    Resolution priority:
    0. Alias JSON target_path (authoritative for read operations)
    1. Full path (if not -global and is a git repo)
    2. index_path from registry (if it has .git)
    3. golden-repos/{name} directory
    4. golden-repos/repos/{name} directory
    5. Versioned repos in .versioned/{name}/v_*/
    6. index_path fallback (directory exists)

    Args:
        repo_identifier: Repository alias or path
        golden_repos_dir: Path to golden repos directory

    Returns:
        Filesystem path to repository, or None if not found
    """
    # Step 0: Try alias JSON target_path (authoritative for read operations)
    # AliasManager.read_alias() returns the versioned snapshot path
    aliases_path = Path(golden_repos_dir) / "aliases"
    if aliases_path.is_dir():
        alias_manager = AliasManager(str(aliases_path))
        # Try the identifier directly (e.g. "cidx-meta-global")
        alias_path = alias_manager.read_alias(repo_identifier)
        if alias_path and Path(alias_path).is_dir():
            return str(alias_path)
        # If not -global, try with -global suffix
        if not repo_identifier.endswith("-global"):
            alias_path = alias_manager.read_alias(f"{repo_identifier}-global")
            if alias_path and Path(alias_path).is_dir():
                return str(alias_path)

    # Try as full path first
    if not repo_identifier.endswith("-global"):
        repo_path = Path(repo_identifier)
        if _is_git_repo(repo_path):
            return str(repo_path)

    # Look up in global registry
    repo_entry = _get_global_repo(repo_identifier)

    if not repo_entry:
        return None

    # Get repo name without -global suffix
    repo_name = repo_identifier.replace("-global", "")

    # Try 1: index_path directly (might be a git repo in test environments)
    index_path = repo_entry.get("index_path")
    if index_path:
        index_path_obj = Path(index_path)
        if _is_git_repo(index_path_obj):
            return str(index_path)

    # Get base directory (.cidx-server/)
    base_dir = Path(golden_repos_dir).parent.parent

    # Try 2: Check golden-repos/{name}
    alt_path = base_dir / "golden-repos" / repo_name
    if _is_git_repo(alt_path):
        return str(alt_path)

    # Try 3: Check golden-repos/repos/{name}
    alt_path = base_dir / "golden-repos" / "repos" / repo_name
    if _is_git_repo(alt_path):
        return str(alt_path)

    # Try 4: Check versioned repos in data/golden-repos/.versioned
    versioned_path = _find_latest_versioned_repo(Path(golden_repos_dir), repo_name)
    if versioned_path:
        return versioned_path

    # Try 5: Check versioned repos in alternative location
    versioned_path = _find_latest_versioned_repo(
        base_dir / "data" / "golden-repos", repo_name
    )
    if versioned_path:
        return versioned_path

    # Fallback: Return index_path if it exists as a directory (for non-git operations like regex_search)
    if index_path:
        index_path_obj = Path(index_path)
        if index_path_obj.is_dir():
            return str(index_path)

    return None


def _resolve_git_repo_path(
    repository_alias: str, username: str
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve repository path for git operations.

    For global repos (ending in -global): validates that the resolved path
    has a .git directory. Local repos (e.g. cidx-meta-global backed by
    local://) have no .git and git operations are not meaningful.

    For user-activated repos: returns the activated-repo path if it exists.

    Returns:
        (path, error_message) tuple. If error_message is not None,
        the caller should return the error to the user.
    """
    # Bug #432: Validate repository_alias is a string (clients may pass a list)
    if not isinstance(repository_alias, str):
        return (
            None,
            "repository_alias must be a string, not a list. Use a single repository alias.",
        )
    if repository_alias.endswith("-global"):
        golden_repos_dir = _get_golden_repos_dir()

        # Check repo URL first — local:// repos never support git operations
        repo_entry = _get_global_repo(repository_alias)
        if repo_entry and repo_entry.get("repo_url", "").startswith("local://"):
            return None, (
                f"Repository '{repository_alias}' is a local repository "
                "and does not support git operations."
            )

        resolved = _resolve_repo_path(repository_alias, golden_repos_dir)
        if resolved is None:
            return None, f"Repository '{repository_alias}' not found."
        if not (Path(resolved) / ".git").exists():
            return None, (
                f"Repository '{repository_alias}' is a local repository "
                "and does not support git operations."
            )

        # Story #387: Group access check - invisible repo pattern
        # Strip -global suffix to match base name stored in accessible repos set
        access_filtering_service = _get_access_filtering_service()
        if access_filtering_service is not None:
            base_alias = repository_alias[: -len("-global")]
            accessible = access_filtering_service.get_accessible_repos(username)
            if base_alias not in accessible:
                return None, f"Repository '{repository_alias}' not found."

        return resolved, None

    activated_repo_manager = ActivatedRepoManager()
    repo_path = activated_repo_manager.get_activated_repo_path(
        username=username, user_alias=repository_alias
    )
    if repo_path is None:
        return None, f"User-activated repository '{repository_alias}' not found."
    if not (Path(repo_path) / ".git").exists():
        return None, (
            f"Repository '{repository_alias}' does not have a .git directory "
            "and does not support git operations."
        )
    return repo_path, None


# Story #557: Directory Tree handler
def handle_directory_tree(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for directory_tree tool - generate hierarchical tree view."""
    from pathlib import Path
    from code_indexer.global_repos.directory_explorer import DirectoryExplorerService

    repository_alias = args.get("repository_alias")

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        golden_repos_dir = _get_golden_repos_dir()

        # Resolve repository_alias to actual path
        repo_path = _resolve_repo_path(repository_alias, golden_repos_dir)
        if repo_path is None:
            available_repos = _get_available_repos(user)
            error_envelope = _error_with_suggestions(
                error_msg=f"Repository '{repository_alias}' not found",
                attempted_value=repository_alias,
                available_values=available_repos,
            )
            return _mcp_response(error_envelope)

        # Create service and generate tree
        service = DirectoryExplorerService(Path(repo_path))
        result = service.generate_tree(
            path=args.get("path"),
            max_depth=_coerce_int(args.get("max_depth"), 3),
            max_files_per_dir=_coerce_int(args.get("max_files_per_dir"), 50),
            include_patterns=args.get("include_patterns"),
            exclude_patterns=args.get("exclude_patterns"),
            show_stats=args.get("show_stats", False),
            include_hidden=args.get("include_hidden", False),
        )

        # Bug #336: Filter cidx-meta tree to only show repos the user can access
        if repository_alias and "cidx-meta" in repository_alias:
            _tree_access_svc = _get_access_filtering_service()
            if _tree_access_svc and result.root.children is not None:
                _all_names = [
                    node.name for node in result.root.children if not node.is_directory
                ]
                _allowed = set(
                    _tree_access_svc.filter_cidx_meta_files(_all_names, user.username)
                )
                result.root.children = [
                    node
                    for node in result.root.children
                    if node.is_directory or node.name in _allowed
                ]
                # Rebuild tree_string excluding lines for unauthorized file entries
                _filtered_lines = []
                for _line in result.tree_string.splitlines():
                    _stripped = _line.strip()
                    # Lines for file entries end with a filename (no trailing '/')
                    # Extract the name after the last connector/space sequence
                    _name = _stripped.lstrip("|+- ")
                    if (
                        not _name.endswith("/")
                        and _name in _all_names
                        and _name not in _allowed
                    ):
                        continue
                    _filtered_lines.append(_line)
                result = result.__class__(
                    root=result.root,
                    tree_string="\n".join(_filtered_lines),
                    total_directories=result.total_directories,
                    total_files=len(result.root.children),
                    max_depth_reached=result.max_depth_reached,
                    root_path=result.root_path,
                )

        # Convert TreeNode to dict recursively
        def tree_node_to_dict(node):
            result_dict = {
                "name": node.name,
                "path": node.path,
                "is_directory": node.is_directory,
                "truncated": node.truncated,
                "hidden_count": node.hidden_count,
            }
            if node.children is not None:
                result_dict["children"] = [tree_node_to_dict(c) for c in node.children]
            else:
                result_dict["children"] = None
            return result_dict

        return _mcp_response(
            {
                "success": True,
                "tree_string": result.tree_string,
                "root": tree_node_to_dict(result.root),
                "total_directories": result.total_directories,
                "total_files": result.total_files,
                "max_depth_reached": result.max_depth_reached,
                "root_path": result.root_path,
            }
        )

    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Error in directory_tree: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# Update handler registry with directory tree tool (Story #557)
HANDLER_REGISTRY["directory_tree"] = handle_directory_tree


# SSH Key Management Handlers (Story #572) — extracted to ssh_keys.py
from .ssh_keys import (  # noqa: F401, E402
    get_ssh_key_manager,
    handle_ssh_key_create,
    handle_ssh_key_list,
    handle_ssh_key_delete,
    handle_ssh_key_show_public,
    handle_ssh_key_assign_host,
)
from .ssh_keys import _register as _ssh_keys_register  # noqa: E402
from .guides import _register as _guides_register  # noqa: E402
from .scip import _register as _scip_register  # noqa: E402

_ssh_keys_register(HANDLER_REGISTRY)
_guides_register(HANDLER_REGISTRY)
_scip_register(HANDLER_REGISTRY)

# SCIP handlers extracted to scip.py (Story #496).
# Re-exported here so that tests importing directly from _legacy continue to work.
from .scip import (  # noqa: F401, E402
    scip_definition,
    scip_references,
    scip_dependencies,
    scip_dependents,
    scip_impact,
    scip_callchain,
    scip_context,
    get_scip_audit_log,
    handle_scip_pr_history,
    handle_scip_cleanup_history,
    handle_scip_cleanup_workspaces,
    handle_scip_cleanup_status,
    _filter_audit_entries as _filter_audit_entries,
    _parse_log_details as _parse_log_details,
    _get_pr_logs_from_service as _get_pr_logs_from_service,
    _get_cleanup_logs_from_service as _get_cleanup_logs_from_service,
    _execute_workspace_cleanup,
)


# =============================================================================
# Git write handlers extracted to git_write.py (Story #496)
# =============================================================================

from .git_write import _register as _git_write_register  # noqa: E402
from .git_write import (  # noqa: F401, E402
    git_stage,
    git_unstage,
    git_commit,
    git_push,
    git_pull,
    git_reset,
    git_clean,
    git_merge,
    git_mark_resolved,
    git_merge_abort,
    git_checkout_file,
    git_branch_create,
    git_branch_switch,
    git_branch_delete,
    git_stash,
    git_amend,
    configure_git_credential,
    list_git_credentials,
    delete_git_credential,
    _get_pat_credential_for_remote,
)

_git_write_register(HANDLER_REGISTRY)


# =============================================================================
# Pull request handlers extracted to pull_requests.py (Story #496)
# Stories #390, #446, #447, #448, #449, #450, #451, #452
# =============================================================================

from .pull_requests import _register as _pr_register  # noqa: E402
from .pull_requests import (  # noqa: F401, E402
    create_pull_request,
    list_pull_requests,
    get_pull_request,
    list_pull_request_comments,
    comment_on_pull_request,
    update_pull_request,
    merge_pull_request,
    close_pull_request,
)

_pr_register(HANDLER_REGISTRY)

# =============================================================================
# Git read handlers extracted to git_read.py (Story #496)
# Stories #34, #35, #555, #556, #558, #639, #653, #654, #658, #660, #686
# =============================================================================

from .git_read import _register as _git_read_register  # noqa: E402
from .git_read import (  # noqa: F401, E402
    handle_git_file_history,
    handle_git_log,
    handle_git_show_commit,
    handle_git_file_at_revision,
    handle_git_diff,
    handle_git_blame,
    handle_git_search_commits,
    handle_git_search_diffs,
    git_status,
    git_fetch,
    git_branch_list,
    git_conflict_status,
    git_diff,
    git_log,
    _serialize_file_history_commits,
    _compute_file_history_fetch_limit,
    _omni_git_log,
    _omni_git_search_commits,
)

_git_read_register(HANDLER_REGISTRY)


# git_stash and git_amend extracted to git_write.py (Story #496)


# =============================================================================
# CI/CD handlers extracted to cicd.py (Story #496)
# Stories #633, #634, #404
# =============================================================================

from .cicd import _register as _cicd_register  # noqa: E402
from .cicd import (  # noqa: F401, E402
    _derive_forge_host,
    _get_personal_credential_for_host,
    _resolve_cicd_project_access,
    _resolve_cicd_read_token,
    _resolve_cicd_write_token,
    handle_gh_actions_list_runs,
    handle_gh_actions_get_run,
    handle_gh_actions_search_logs,
    handle_gh_actions_get_job_logs,
    handle_gh_actions_retry_run,
    handle_gh_actions_cancel_run,
    handle_gitlab_ci_list_pipelines,
    handle_gitlab_ci_get_pipeline,
    handle_gitlab_ci_search_logs,
    handle_gitlab_ci_get_job_logs,
    handle_gitlab_ci_retry_pipeline,
    handle_gitlab_ci_cancel_pipeline,
    handle_github_actions_list_runs,
    handle_github_actions_get_run,
    handle_github_actions_search_logs,
    handle_github_actions_get_job_logs,
    handle_github_actions_retry_run,
    handle_github_actions_cancel_run,
)

_cicd_register(HANDLER_REGISTRY)


# ============================================================================
# Story #679: Semantic Search with Payload Control - Cache Retrieval Handler
# ============================================================================


def handle_get_cached_content(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Handler for get_cached_content tool.

    Retrieves cached content by handle with pagination support.
    Implements AC5 of Story #679.

    Args:
        args: Tool arguments containing:
            - handle (str): UUID4 cache handle from search results
            - page (int, optional): Page number (0-indexed, default 0)
        user: Authenticated user

    Returns:
        MCP response with content and pagination info
    """
    # Story #331 AC8: Accepted risk - cache handles are UUID4 (unguessable)
    # and short-lived (TTL-based). Cross-user cache access requires knowing
    # the exact UUID, which is not feasible. Full user-scoping tracking
    # would add complexity without meaningful security benefit.
    from code_indexer.server.cache.payload_cache import CacheNotFoundError

    handle = args.get("handle")
    page = int(args.get("page", 0))  # Bug #464: MCP clients may send page as string

    if not handle:
        return _mcp_response(
            {
                "success": False,
                "error": "Missing required parameter: handle",
            }
        )

    # Get payload_cache from app.state
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


# Register cache retrieval handler
HANDLER_REGISTRY["get_cached_content"] = handle_get_cached_content


# =============================================================================
# Delegation handlers extracted to delegation.py (Story #496)
# =============================================================================

from .delegation import _register as _delegation_register  # noqa: E402
from .delegation import (  # noqa: F401, E402
    handle_list_delegation_functions,
    handle_execute_delegation_function,
    handle_poll_delegation_job,
    handle_execute_open_delegation,
    handle_cs_register_repository,
    handle_cs_list_repositories,
    handle_cs_check_health,
    _get_delegation_function_repo_path,
    _get_user_groups,
    _get_delegation_config,
    _validate_function_parameters,
    _ensure_repos_registered,
    _get_cidx_callback_base_url,
    _load_packages_context,
    _resolve_guardrails,
    _get_repo_ready_timeout,
    _validate_collaborative_params,
    _validate_competitive_params,
    _validate_open_delegation_params,
    _register_open_delegation_callback,
    _submit_open_delegation_job,
    _submit_collaborative_delegation_job,
    _submit_competitive_delegation_job,
    _lookup_golden_repo_for_cs,
)

_delegation_register(HANDLER_REGISTRY)


# handle_query_audit_logs, handle_enter_maintenance_mode, handle_exit_maintenance_mode
# extracted to admin.py (Story #496)


# handle_get_maintenance_status extracted to admin.py (Story #496)
# handle_scip_pr_history, handle_scip_cleanup_history, handle_scip_cleanup_workspaces,
# handle_scip_cleanup_status, _cleanup_job_state, _execute_workspace_cleanup
# extracted to scip.py (Story #496)


HANDLER_REGISTRY["list_repo_categories"] = list_repo_categories

# Admin handlers extracted to admin.py (Story #496)
from .admin import _register as _admin_register  # noqa: E402
from .admin import (  # noqa: F401, E402
    handle_authenticate,
    list_users,
    create_user,
    handle_set_session_impersonation,
    _get_group_manager,
    _validate_group_id,
    handle_list_groups,
    handle_create_group,
    handle_get_group,
    handle_update_group,
    handle_delete_group,
    handle_add_member_to_group,
    handle_remove_member_from_group,
    handle_add_repos_to_group,
    handle_remove_repo_from_group,
    handle_bulk_remove_repos_from_group,
    handle_list_api_keys,
    handle_create_api_key,
    handle_delete_api_key,
    handle_list_mcp_credentials,
    handle_create_mcp_credential,
    handle_delete_mcp_credential,
    handle_admin_list_user_mcp_credentials,
    handle_admin_create_user_mcp_credential,
    handle_admin_delete_user_mcp_credential,
    handle_admin_list_all_mcp_credentials,
    handle_admin_list_system_mcp_credentials,
    handle_query_audit_logs,
    handle_enter_maintenance_mode,
    handle_exit_maintenance_mode,
    handle_get_maintenance_status,
    handle_admin_logs_query,
    admin_logs_export,
    get_job_statistics,
    get_job_details,
    handle_get_global_config,
    handle_set_global_config,
    trigger_reindex,
    get_index_status,
    handle_trigger_dependency_analysis,
)

_admin_register(HANDLER_REGISTRY)


def _resolve_golden_repo_path(alias: str) -> Optional[str]:
    """Resolve golden repo alias to the READ-ONLY versioned snapshot path.

    WARNING: The returned path points to an immutable versioned snapshot
    (.versioned/{alias}/v_{timestamp}/). NEVER write to this path — config
    changes, index builds, and metadata updates will be lost on next refresh.

    For WRITE operations, use _resolve_golden_repo_base_clone(alias) instead,
    which returns the mutable base clone path (golden-repos/{alias_name}/).

    Uses the same pattern as search_code to get the current target path,
    which remains correct after refreshes (registry path becomes stale).

    Returns:
        Resolved filesystem path string, or None if alias not found.
    """
    golden_repos_dir = _get_golden_repos_dir()
    alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
    resolved: Optional[str] = alias_manager.read_alias(alias)
    if resolved is None and not alias.endswith("-global"):
        resolved = alias_manager.read_alias(alias + "-global")
    return resolved


def _resolve_golden_repo_base_clone(alias: str) -> Optional[str]:
    """Resolve golden repo alias to the WRITABLE base clone path.

    WARNING: This returns the base clone (golden-repos/{alias_name}/), NOT
    the versioned snapshot. Use this for ALL write operations (config changes,
    index builds, metadata updates). For read-only operations (queries,
    browsing), use _resolve_golden_repo_path() instead.

    The base clone is the mutable working copy where git pull, cidx init,
    and cidx index operate. Versioned snapshots (.versioned/) are immutable
    CoW copies served to queries.

    Returns:
        Resolved base clone path string, or None if alias not found or base
        clone directory does not exist on disk.
    """
    from pathlib import Path

    versioned_path = _resolve_golden_repo_path(alias)
    if versioned_path is None:
        return None

    parts = Path(versioned_path).parts
    if ".versioned" in parts:
        versioned_idx = parts.index(".versioned")
        alias_name = parts[versioned_idx + 1]
        golden_repos_dir = str(Path(*parts[:versioned_idx]))
        base_clone = Path(golden_repos_dir) / alias_name
        if base_clone.exists():
            return str(base_clone)
        # Base clone might not exist (first clone not yet created)
        return None

    # Not a versioned path — return as-is (legacy flat structure)
    return versioned_path


def manage_provider_indexes(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Manage provider-specific semantic indexes (Story #490)."""
    try:
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        action = params.get("action")
        if not action:
            return _mcp_response({"error": "Missing required parameter: action"})

        service = ProviderIndexService(config=get_config_service().get_config())

        if action == "list_providers":
            providers = service.list_providers()
            return _mcp_response(
                {
                    "success": True,
                    "providers": providers,
                    "count": len(providers),
                }
            )

        if action == "status":
            repo_alias = params.get("repository_alias")
            if not repo_alias:
                return _mcp_response(
                    {"error": "Missing required parameter: repository_alias"}
                )

            # Resolve repo path — prefer base clone (authoritative for index state),
            # fall back to versioned snapshot if base clone is not available.
            repo_path = _resolve_golden_repo_base_clone(repo_alias)
            if not repo_path:
                repo_path = _resolve_golden_repo_path(repo_alias)
                if not repo_path:
                    return _mcp_response(
                        {"error": f"Repository '{repo_alias}' not found"}
                    )

            status = service.get_provider_index_status(repo_path, repo_alias)
            return _mcp_response(
                {
                    "success": True,
                    "repository_alias": repo_alias,
                    "provider_indexes": status,
                }
            )

        # add, recreate, remove require provider
        provider_name = params.get("provider")
        repo_alias = params.get("repository_alias")

        if not provider_name:
            return _mcp_response({"error": "Missing required parameter: provider"})
        if not repo_alias:
            return _mcp_response(
                {"error": "Missing required parameter: repository_alias"}
            )

        # Validate provider
        error = service.validate_provider(provider_name)
        if error:
            providers = service.list_providers()
            return _mcp_response(
                {
                    "error": error,
                    "available_providers": [p["name"] for p in providers],
                }
            )

        # Resolve repo path
        repo_path = _resolve_golden_repo_path(repo_alias)
        if not repo_path:
            return _mcp_response({"error": f"Repository '{repo_alias}' not found"})

        if action == "remove":
            # Remove provider from config AND index on base clone (write path must use base clone)
            base_clone_path = _resolve_golden_repo_base_clone(repo_alias)
            if not base_clone_path:
                return _mcp_response(
                    {
                        "error": f"Cannot resolve base clone for '{repo_alias}'. "
                        "Remove requires a writable base clone path."
                    }
                )
            _remove_provider_from_config(base_clone_path, provider_name)
            result = service.remove_provider_index(base_clone_path, provider_name)
            return _mcp_response(
                {
                    "success": result["removed"],
                    "message": result["message"],
                    "collection_name": result["collection_name"],
                }
            )

        if action in ("add", "recreate"):
            clear = action == "recreate"

            if _utils.app_module.background_job_manager is None:
                return _mcp_response({"error": "Background job manager not available"})

            # Persist provider to config.json on BASE CLONE before submitting job
            # so cidx index picks it up. Write operations must target base clone,
            # not versioned snapshot (Bug #625, Fix 3).
            base_clone_path = _resolve_golden_repo_base_clone(repo_alias)
            if not base_clone_path:
                return _mcp_response(
                    {
                        "error": (
                            f"Cannot resolve base clone for '{repo_alias}'. "
                            "Write operations require a writable base clone path, "
                            "not a versioned snapshot."
                        )
                    }
                )
            if not _append_provider_to_config(base_clone_path, provider_name):
                return _mcp_response(
                    {
                        "error": f"Failed to write provider '{provider_name}' to config at {base_clone_path}"
                    }
                )

            job_id = _utils.app_module.background_job_manager.submit_job(
                operation_type=f"provider_index_{action}",
                func=_provider_index_job,
                submitter_username=user.username,
                repo_alias=repo_alias,
                repo_path=repo_path,
                provider_name=provider_name,
                clear=clear,
            )

            return _mcp_response(
                {
                    "success": True,
                    "job_id": job_id,
                    "action": action,
                    "provider": provider_name,
                    "repository_alias": repo_alias,
                    "message": f"Background job submitted to {action} {provider_name} index for {repo_alias}",
                }
            )

        return _mcp_response({"error": f"Unknown action: {action}"})

    except Exception as e:
        logger.error("manage_provider_indexes error: %s", e, exc_info=True)
        return _mcp_response({"error": str(e)})


def _post_provider_index_snapshot(
    repo_alias: str, base_clone_path: str, old_snapshot_path: str
) -> None:
    """Create a new versioned snapshot after indexing the base clone.

    Called by _provider_index_job when the original repo_path was a versioned
    snapshot. After indexing the base clone, we create a new snapshot so that
    the alias target is updated and queries reflect the new provider index
    immediately (Bug #604).

    Args:
        repo_alias: Global alias name (e.g. "claude-server-global")
        base_clone_path: Path to the base clone that was indexed
        old_snapshot_path: Old versioned snapshot path to replace
    """
    scheduler = _get_app_refresh_scheduler()
    if scheduler is None:
        logger.warning(
            "No refresh scheduler available — skipping snapshot creation after "
            "provider index for %s. Index will be visible after next scheduled refresh.",
            repo_alias,
        )
        return

    try:
        new_snapshot = scheduler._create_snapshot(
            alias_name=repo_alias,
            source_path=base_clone_path,
        )
        try:
            scheduler.alias_manager.swap_alias(
                alias_name=repo_alias,
                new_target=new_snapshot,
                old_target=old_snapshot_path,
            )
        except ValueError as swap_exc:
            # Bug #648/#6: concurrent jobs may arrive here with the same old_target.
            # Only the first swap succeeds; subsequent ones get ValueError.
            # Best-effort cleanup of the orphaned new_snapshot to prevent disk leak.
            import shutil as _shutil

            _shutil.rmtree(new_snapshot, ignore_errors=True)
            logger.warning(
                "Alias swap skipped for %s (old_target mismatch — another job already "
                "swapped ahead): %s. Best-effort cleanup of orphaned snapshot %s attempted.",
                repo_alias,
                swap_exc,
                new_snapshot,
            )
            return
        # Schedule old snapshot for cleanup (it was a versioned dir)
        cleanup_manager = getattr(scheduler, "cleanup_manager", None)
        if cleanup_manager is not None:
            cleanup_manager.schedule_cleanup(old_snapshot_path)
        logger.info(
            "Provider index: alias %s now points to new snapshot %s",
            repo_alias,
            new_snapshot,
        )
    except Exception as exc:
        logger.warning(
            "Failed to create new snapshot after provider index for %s: %s. "
            "Index is in base clone and will be visible after next scheduled refresh.",
            repo_alias,
            exc,
        )


# Maximum characters to include from stdout/stderr tail in provider index job results.
_PROVIDER_JOB_OUTPUT_TAIL_CHARS = 500


def _append_provider_to_config(repo_path: str, provider_name: str) -> bool:
    """Permanently append provider_name to embedding_providers in .code-indexer/config.json.

    Idempotent: if provider_name is already in the list, no duplicate is added.
    If embedding_providers key is absent, it is initialised as ['voyage-ai', provider_name]
    (preserving voyage-ai as the primary provider).

    Args:
        repo_path: Path to the repository root containing .code-indexer/config.json
        provider_name: Provider name to add (e.g. 'cohere')

    Returns:
        True if config was updated (or provider already present), False on any failure.
    """
    from pathlib import Path

    if ".versioned" in Path(repo_path).parts:
        logger.error(
            "_append_provider_to_config called with versioned snapshot path %s — "
            "refusing to write (immutable). Use _resolve_golden_repo_base_clone() instead.",
            repo_path,
        )
        return False

    config_path = Path(repo_path) / ".code-indexer" / "config.json"
    if not config_path.exists():
        logger.warning(
            "_append_provider_to_config: config.json not found at %s", config_path
        )
        return False
    try:
        with open(config_path) as f:
            config_data = json.load(f)
        existing = config_data.get(
            "embedding_providers",
            [config_data.get("embedding_provider", "voyage-ai")],
        )
        if provider_name not in existing:
            existing.append(provider_name)
        config_data["embedding_providers"] = existing
        with open(config_path, "w") as f:
            json.dump(config_data, f)
        return True
    except Exception as exc:
        logger.warning("_append_provider_to_config failed for %s: %s", config_path, exc)
        return False


def _remove_provider_from_config(repo_path: str, provider_name: str) -> None:
    """Remove provider_name from embedding_providers in .code-indexer/config.json.

    Idempotent: if provider_name is not in the list, no change is made.
    The primary provider (voyage-ai) cannot be removed.

    Args:
        repo_path: Path to the repository root containing .code-indexer/config.json
        provider_name: Provider name to remove (e.g. 'cohere')
    """
    from pathlib import Path

    if provider_name == "voyage-ai":
        logger.warning("Cannot remove primary provider 'voyage-ai' from config")
        return

    if ".versioned" in Path(repo_path).parts:
        logger.error(
            "_remove_provider_from_config called with versioned snapshot path %s — "
            "refusing to write (immutable). Use _resolve_golden_repo_base_clone() instead.",
            repo_path,
        )
        return  # Anti-Fallback: refuse, don't degrade

    config_path = Path(repo_path) / ".code-indexer" / "config.json"
    if not config_path.exists():
        return
    try:
        with open(config_path) as f:
            config_data = json.load(f)
        existing = config_data.get("embedding_providers", [])
        if provider_name in existing:
            existing.remove(provider_name)
            config_data["embedding_providers"] = existing
            with open(config_path, "w") as f:
                json.dump(config_data, f)
    except Exception as exc:
        logger.warning(
            "_remove_provider_from_config failed for %s: %s", config_path, exc
        )


def _resolve_provider_job_repo_path(repo_path: str, repo_alias: str) -> tuple:
    """Resolve the actual indexing path for a provider background job.

    When repo_path is inside a .versioned/ directory (immutable), return the
    base clone path instead. Returns (actual_path, resolved_alias, is_versioned).

    Cannot use _resolve_golden_repo_base_clone here because background workers
    have no access to server app state.
    """
    is_versioned = ".versioned" in Path(repo_path).parts
    if not is_versioned:
        return repo_path, repo_alias, False

    parts = Path(repo_path).parts
    versioned_idx = parts.index(".versioned")
    alias_name = parts[versioned_idx + 1]
    golden_repos_dir = str(Path(*parts[:versioned_idx]))
    base_clone = Path(golden_repos_dir) / alias_name
    resolved_alias = repo_alias or f"{alias_name}-global"

    if not base_clone.exists():
        return repo_path, resolved_alias, True  # caller must handle missing base clone

    logger.info(
        "Provider job: using base clone %s instead of versioned snapshot %s",
        base_clone,
        repo_path,
    )
    return str(base_clone), resolved_alias, True


def _build_provider_api_key_env(provider_name: str) -> dict:
    """Build a subprocess env dict with the correct API key for a provider."""
    import os

    env = os.environ.copy()
    server_config = get_config_service().get_config()
    if provider_name == "cohere":
        api_key = getattr(server_config, "cohere_api_key", None)
        if api_key:
            env["CO_API_KEY"] = api_key
    elif provider_name == "voyage-ai":
        api_key = getattr(server_config, "voyageai_api_key", None)
        if api_key:
            env["VOYAGE_API_KEY"] = api_key
    return env


def _build_temporal_index_cmd(clear: bool, temporal_options: dict) -> list:
    """Build the cidx index --index-commits command with optional temporal flags."""
    cmd = ["cidx", "index", "--index-commits", "--progress-json"]
    if clear:
        cmd.append("--clear")
    diff_context = temporal_options.get("diff_context")
    if diff_context is not None:
        cmd.extend(["--diff-context", str(diff_context)])
    if temporal_options.get("all_branches"):
        cmd.append("--all-branches")
    max_commits = temporal_options.get("max_commits")
    if max_commits is not None:
        cmd.extend(["--max-commits", str(max_commits)])
    since_date = temporal_options.get("since_date")
    if since_date:
        cmd.extend(["--since", str(since_date)])
    return cmd


def _provider_index_job(
    repo_path: str,
    provider_name: str,
    clear: bool = False,
    progress_callback=None,
    **kwargs,
) -> Dict[str, Any]:
    """Background job worker for provider index add/recreate (Story #490, Bug #607, Story #620).

    Runs cidx index which reads embedding_providers from ..code-indexer/config.json
    and indexes each configured provider in sequence. No config.json mutation occurs.

    If repo_path points to a versioned snapshot (.versioned/ in the path), the
    index is built on the BASE CLONE instead (Bug #604). This is required because:
    - CLAUDE.md mandates that .versioned/ directories are IMMUTABLE
    - The health check uses get_actual_repo_path() which returns the base clone
    - Indexing the versioned snapshot means the index is lost on next refresh

    After successfully indexing the base clone, a new versioned snapshot is created
    and the alias is swapped so queries reflect the new provider index immediately.

    Story #613: Uses run_with_popen_progress for real progress reporting so the UI
    shows meaningful intermediate values instead of a hardcoded 25% sentinel.
    """
    import os
    from pathlib import Path

    from code_indexer.services.progress_phase_allocator import ProgressPhaseAllocator
    from code_indexer.services.progress_subprocess_runner import (
        IndexingSubprocessError,
        gather_repo_metrics,
        run_with_popen_progress,
    )

    # Determine the actual path to index.
    # If repo_path is inside .versioned/, use the base clone instead.
    # Note: repo_alias is passed via submit_job's explicit repo_alias= parameter,
    # which means it is NOT forwarded to **kwargs. Derive it from the path as fallback.
    # NOTE: We use direct path arithmetic here (not _resolve_golden_repo_base_clone)
    # because _provider_index_job runs as a background worker and does not have access
    # to server app state (required by _resolve_golden_repo_base_clone → _get_golden_repos_dir).
    repo_alias = kwargs.get("repo_alias", "")
    actual_path = repo_path
    is_versioned_snapshot = ".versioned" in Path(repo_path).parts
    if is_versioned_snapshot:
        parts = Path(repo_path).parts
        versioned_idx = parts.index(".versioned")
        alias_name = parts[versioned_idx + 1]  # e.g. "claude-server"
        golden_repos_dir = str(Path(*parts[:versioned_idx]))
        base_clone = Path(golden_repos_dir) / alias_name
        # submit_job consumes repo_alias before forwarding kwargs; derive from path.
        if not repo_alias:
            repo_alias = f"{alias_name}-global"
        if base_clone.exists():
            actual_path = str(base_clone)
            logger.info(
                "Provider index: using base clone %s instead of versioned snapshot %s "
                "(versioned snapshots are immutable per architecture)",
                actual_path,
                repo_path,
            )
        else:
            error_msg = (
                f"Base clone not found at {base_clone} for versioned snapshot {repo_path}. "
                "Cannot index versioned snapshot (immutable)."
            )
            logger.error(error_msg)
            return {"success": False, "error": error_msg, "provider": provider_name}

    # Build subprocess env with the appropriate API key for this provider.
    env = os.environ.copy()
    server_config = get_config_service().get_config()
    if provider_name == "cohere":
        api_key = getattr(server_config, "cohere_api_key", None)
        if api_key:
            env["CO_API_KEY"] = api_key
    elif provider_name == "voyage-ai":
        api_key = getattr(server_config, "voyageai_api_key", None)
        if api_key:
            env["VOYAGE_API_KEY"] = api_key

    cmd = ["cidx", "index", "--progress-json"]
    if clear:
        cmd.append("--clear")

    # Gather repo metrics for progress weight calculation.
    # Returns (0, 0) for non-git repos — graceful degradation.
    file_count, commit_count = gather_repo_metrics(actual_path)

    allocator = ProgressPhaseAllocator()
    allocator.calculate_weights(
        index_types=["semantic"],
        file_count=file_count,
        commit_count=commit_count,
    )

    all_stdout: list = []
    all_stderr: list = []

    # Bug #678: Seed provider config before subprocess
    try:
        from code_indexer.server.services.config_seeding import seed_provider_config

        seed_provider_config(actual_path)
    except Exception as _seed_exc:  # noqa: BLE001
        logger.debug("Bug #678: seed_provider_config failed (non-fatal): %s", _seed_exc)

    try:
        run_with_popen_progress(
            command=cmd,
            phase_name="semantic",
            allocator=allocator,
            progress_callback=progress_callback,
            all_stdout=all_stdout,
            all_stderr=all_stderr,
            cwd=actual_path,
            env=env,
            timeout=None,  # No hard limit — production repos can take several hours
            error_label="provider index",
        )
        if is_versioned_snapshot and actual_path != repo_path:
            # Base clone indexed successfully. Create a new versioned snapshot so
            # the alias target reflects the new provider index immediately.
            _post_provider_index_snapshot(
                repo_alias=repo_alias,
                base_clone_path=actual_path,
                old_snapshot_path=repo_path,
            )
        stdout_out = "".join(all_stdout)
        stderr_out = "".join(all_stderr)
        return {
            "success": True,
            "stdout": (
                stdout_out[-_PROVIDER_JOB_OUTPUT_TAIL_CHARS:] if stdout_out else ""
            ),
            "stderr": (
                stderr_out[-_PROVIDER_JOB_OUTPUT_TAIL_CHARS:] if stderr_out else ""
            ),
        }
    except IndexingSubprocessError as exc:
        logger.warning(
            "Provider index failed for provider=%s repo=%s: %s",
            provider_name,
            repo_path,
            exc,
        )
        return {
            "success": False,
            "stdout": "".join(all_stdout)[-_PROVIDER_JOB_OUTPUT_TAIL_CHARS:],
            "stderr": str(exc),
        }
    finally:
        # Bug #678: Drain health events after subprocess
        try:
            from code_indexer.services.provider_health_bridge import (
                drain_and_feed_monitor,
            )

            drain_and_feed_monitor(actual_path)
        except Exception as _drain_exc:  # noqa: BLE001
            logger.debug(
                "Bug #678: drain_and_feed_monitor failed (non-fatal): %s", _drain_exc
            )


def _set_enable_temporal_flag(repo_alias: str) -> None:
    """Set enable_temporal=True in the SQLite backend and in-memory golden_repo_manager.

    Called after _provider_temporal_index_job succeeds to persist the flag that
    was never written via the provider path (Bug #648/#1).  Mirrors the pattern
    in golden_repo_manager.py:2769-2807 (the only other place the flag is set).

    Degrades gracefully: logs a warning on any failure rather than raising.
    """
    if not repo_alias:
        return

    grm = getattr(_utils.app_module, "golden_repo_manager", None)
    if grm is None:
        logger.warning(
            "_set_enable_temporal_flag: golden_repo_manager unavailable, "
            "cannot set enable_temporal=True for %s",
            repo_alias,
        )
        return

    try:
        if grm._sqlite_backend.update_enable_temporal(repo_alias, True):
            repo_meta = grm.golden_repos.get(repo_alias)
            if repo_meta is not None:
                repo_meta.enable_temporal = True
            logger.info(
                "Set enable_temporal=True for %s in golden_repos_metadata", repo_alias
            )
        else:
            logger.warning(
                "Failed to set enable_temporal=True for %s in golden_repos_metadata",
                repo_alias,
            )
    except Exception as exc:
        logger.warning("Error setting enable_temporal for %s: %s", repo_alias, exc)

    global_alias = f"{repo_alias}-global"
    try:
        from pathlib import Path as _Path

        data_dir = _Path(grm.data_dir)
        golden_repos_dir = data_dir / "golden-repos"
        sqlite_db_path = str(data_dir / "cidx_server.db")
        registry = GlobalRegistry(
            str(golden_repos_dir),
            use_sqlite=True,
            db_path=sqlite_db_path,
        )
        if (
            registry._sqlite_backend is not None
            and registry._sqlite_backend.update_enable_temporal(global_alias, True)
        ):
            logger.info("Set enable_temporal=True for %s in global_repos", global_alias)
        else:
            logger.warning(
                "Failed to set enable_temporal=True for %s in global_repos",
                global_alias,
            )
    except Exception as exc:
        logger.error("Error updating global_repos table for %s: %s", global_alias, exc)


def _provider_temporal_index_job(
    repo_path: str,
    provider_name: str,
    clear: bool = False,
    progress_callback=None,
    **kwargs,
) -> Dict[str, Any]:
    """Background job for per-provider temporal index (Story #641).

    Runs cidx index --index-commits with optional temporal_options from kwargs.
    temporal_options must be passed via kwargs (not from golden_repo_manager
    which is inaccessible from background threads).
    """
    from code_indexer.services.progress_phase_allocator import ProgressPhaseAllocator
    from code_indexer.services.progress_subprocess_runner import (
        IndexingSubprocessError,
        gather_repo_metrics,
        run_with_popen_progress,
    )

    repo_alias = kwargs.get("repo_alias", "")
    actual_path, repo_alias, is_versioned_snapshot = _resolve_provider_job_repo_path(
        repo_path, repo_alias
    )
    if (
        is_versioned_snapshot
        and actual_path == repo_path
        and not Path(actual_path).exists()
    ):
        return {
            "success": False,
            "error": f"Base clone not found for {repo_path}",
            "provider": provider_name,
        }

    env = _build_provider_api_key_env(provider_name)
    temporal_options = kwargs.get("temporal_options", {}) or {}
    cmd = _build_temporal_index_cmd(clear, temporal_options)

    file_count, commit_count = gather_repo_metrics(actual_path)
    allocator = ProgressPhaseAllocator()
    allocator.calculate_weights(
        index_types=["temporal"], file_count=file_count, commit_count=commit_count
    )

    all_stdout: list = []
    all_stderr: list = []

    # Bug #678: Seed provider config before subprocess
    try:
        from code_indexer.server.services.config_seeding import seed_provider_config

        seed_provider_config(actual_path)
    except Exception as _seed_exc:  # noqa: BLE001
        logger.debug("Bug #678: seed_provider_config failed (non-fatal): %s", _seed_exc)

    try:
        run_with_popen_progress(
            command=cmd,
            phase_name="temporal",
            allocator=allocator,
            progress_callback=progress_callback,
            all_stdout=all_stdout,
            all_stderr=all_stderr,
            cwd=actual_path,
            env=env,
            timeout=None,
            error_label="provider temporal index",
        )
        if is_versioned_snapshot and actual_path != repo_path:
            try:
                _post_provider_index_snapshot(
                    repo_alias=repo_alias,
                    base_clone_path=actual_path,
                    old_snapshot_path=repo_path,
                )
            except Exception as exc:
                logger.warning(
                    "Post-temporal-index snapshot failed for %s: %s", repo_alias, exc
                )

        # Bug #648/#1: Set enable_temporal=True in DB and in-memory after successful CLI run.
        # The route handler removed "temporal" from remaining_index_types before calling
        # add_indexes_to_golden_repo, which is the only other place the flag gets set.
        # Mirror the pattern from golden_repo_manager.py:2769-2807.
        _set_enable_temporal_flag(repo_alias)

        stdout_out = "".join(all_stdout)
        stderr_out = "".join(all_stderr)
        return {
            "success": True,
            "provider": provider_name,
            "stdout": (
                stdout_out[-_PROVIDER_JOB_OUTPUT_TAIL_CHARS:] if stdout_out else ""
            ),
            "stderr": (
                stderr_out[-_PROVIDER_JOB_OUTPUT_TAIL_CHARS:] if stderr_out else ""
            ),
        }
    except IndexingSubprocessError as exc:
        logger.warning(
            "Temporal provider index failed for provider=%s repo=%s: %s",
            provider_name,
            repo_path,
            exc,
        )
        return {
            "success": False,
            "provider": provider_name,
            "stdout": "".join(all_stdout)[-_PROVIDER_JOB_OUTPUT_TAIL_CHARS:],
            "stderr": str(exc),
        }
    finally:
        # Bug #678: Drain health events after subprocess
        try:
            from code_indexer.services.provider_health_bridge import (
                drain_and_feed_monitor,
            )

            drain_and_feed_monitor(actual_path)
        except Exception as _drain_exc:  # noqa: BLE001
            logger.debug(
                "Bug #678: drain_and_feed_monitor failed (non-fatal): %s", _drain_exc
            )


def bulk_add_provider_index(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Bulk add provider index to all repositories (Story #490)."""
    try:
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        provider_name = params.get("provider")
        if not provider_name:
            return _mcp_response({"error": "Missing required parameter: provider"})

        service = ProviderIndexService(config=get_config_service().get_config())

        # Validate provider
        error = service.validate_provider(provider_name)
        if error:
            providers = service.list_providers()
            return _mcp_response(
                {
                    "error": error,
                    "available_providers": [p["name"] for p in providers],
                }
            )

        # Get all golden repos
        global_repos = _list_global_repos()
        filter_pattern = params.get("filter")

        job_ids = []
        skipped = []

        if _utils.app_module.background_job_manager is None:
            return _mcp_response({"error": "Background job manager not available"})

        for repo in global_repos:
            alias = repo.get("alias_name", "")

            # Apply filter if specified
            if filter_pattern:
                category = repo.get("category", "")
                if filter_pattern.startswith("category:"):
                    filter_cat = filter_pattern.split(":", 1)[1]
                    if filter_cat.lower() not in category.lower():
                        continue

            # Check if provider index already exists
            repo_path = _resolve_golden_repo_path(alias)
            if not repo_path:
                continue

            status = service.get_provider_index_status(repo_path, alias)
            provider_status = status.get(provider_name, {})

            if provider_status.get("exists"):
                skipped.append(alias)
                continue

            # Write operations must target the base clone, never the versioned
            # snapshot (Bug #625, Fix 3/W1). If the base clone cannot be resolved,
            # skip this alias — writing to the versioned path would be incorrect.
            base_clone_path = _resolve_golden_repo_base_clone(alias)
            if not base_clone_path:
                logger.warning(
                    "bulk_add_provider_index: cannot resolve base clone for alias %s"
                    " — skipping config write and job submission",
                    alias,
                )
                skipped.append(alias)
                continue

            if not _append_provider_to_config(base_clone_path, provider_name):
                logger.warning(
                    "bulk_add_provider_index: config write failed for %s — skipping",
                    alias,
                )
                skipped.append(alias)
                continue

            # Submit job
            job_id = _utils.app_module.background_job_manager.submit_job(
                operation_type="provider_index_add",
                func=_provider_index_job,
                submitter_username=user.username,
                repo_alias=alias,
                repo_path=repo_path,
                provider_name=provider_name,
                clear=False,
            )
            job_ids.append({"alias": alias, "job_id": job_id})

        return _mcp_response(
            {
                "success": True,
                "provider": provider_name,
                "jobs_created": len(job_ids),
                "jobs": job_ids,
                "skipped": skipped,
                "skipped_count": len(skipped),
                "message": f"Created {len(job_ids)} jobs, skipped {len(skipped)} repos (already have {provider_name} index)",
            }
        )

    except Exception as e:
        logger.error("bulk_add_provider_index error: %s", e, exc_info=True)
        return _mcp_response({"error": str(e)})


HANDLER_REGISTRY["manage_provider_indexes"] = manage_provider_indexes
HANDLER_REGISTRY["bulk_add_provider_index"] = bulk_add_provider_index


def get_provider_health(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get provider health metrics (Story #491)."""
    try:
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        monitor = ProviderHealthMonitor.get_instance()
        provider = params.get("provider")
        health = monitor.get_health(provider)

        result = {}
        for pname, status in health.items():
            result[pname] = {
                "status": status.status,
                "health_score": status.health_score,
                "p50_latency_ms": status.p50_latency_ms,
                "p95_latency_ms": status.p95_latency_ms,
                "p99_latency_ms": status.p99_latency_ms,
                "error_rate": status.error_rate,
                "availability": status.availability,
                "total_requests": status.total_requests,
                "successful_requests": status.successful_requests,
                "failed_requests": status.failed_requests,
                "window_minutes": status.window_minutes,
            }

        return _mcp_response(
            {
                "success": True,
                "provider_health": result,
            }
        )

    except Exception as e:
        logger.error("get_provider_health error: %s", e, exc_info=True)
        return _mcp_response({"error": str(e)})


HANDLER_REGISTRY["get_provider_health"] = get_provider_health


# ---------------------------------------------------------------------------
# Module-level forwarding for mock-patch compatibility
# ---------------------------------------------------------------------------
# When domain handlers are extracted from this module into separate files
# (e.g. scip.py, guides.py), those modules import utilities like
# `_get_scip_query_service` directly from `_utils`.  Tests that patch
# `code_indexer.server.mcp.handlers._legacy._get_scip_query_service` would
# normally only update the binding in THIS module's global dict, leaving the
# domain module's binding untouched.
#
# The _ForwardingModule below intercepts every `setattr` on _legacy and
# mirrors the write into each extracted domain module (when the name exists
# there), preserving all existing test patches without requiring test changes.


class _LegacyForwardingModule(types.ModuleType):
    """Forward attribute writes on _legacy to extracted domain submodules."""

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if not name.startswith("__"):
            for _submod_name in (
                "code_indexer.server.mcp.handlers.scip",
                "code_indexer.server.mcp.handlers.guides",
                "code_indexer.server.mcp.handlers.ssh_keys",
                "code_indexer.server.mcp.handlers.delegation",
                "code_indexer.server.mcp.handlers.pull_requests",
                "code_indexer.server.mcp.handlers.git_read",
                "code_indexer.server.mcp.handlers.git_write",
                "code_indexer.server.mcp.handlers.admin",
            ):
                _submod = sys.modules.get(_submod_name)
                if _submod is not None and name in _submod.__dict__:
                    _submod.__dict__[name] = value


sys.modules[__name__].__class__ = _LegacyForwardingModule
