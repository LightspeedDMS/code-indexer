"""
Query route handler extracted from inline_routes.py.

Part of the inline_routes.py modularization effort. Contains 1 route handler:
- POST /api/query

Zero behavior change: same path, method, response models, and handler logic.
"""

import logging
import time
from typing import Optional, Tuple

from fastapi import (
    FastAPI,
    HTTPException,
    status,
    Depends,
    Request,
)
from fastapi.responses import JSONResponse

from ..models.query import (
    SemanticQueryRequest,
    FTSResultItem,
)
from ..models.api_models import QueryResultItem
from ..query.semantic_query_manager import SemanticQueryError
from ..auth import dependencies
from ..logging_utils import format_error_log
from code_indexer.server.telemetry.correlation_bridge import (
    get_current_correlation_id as get_correlation_id,
)
from ..app_helpers import (
    _apply_rest_semantic_truncation,
    _apply_rest_fts_truncation,
)
from code_indexer.server.mcp.reranking import (
    _apply_reranking_sync as _rest_apply_reranking_sync,
    calculate_overfetch_limit as _rest_calculate_overfetch_limit,
    extract_rerank_document as _rest_extract_rerank_document,
)
from code_indexer.server.services.temporal_live_dispatch import (
    execute_live_temporal_search,
)

# Bug #1209: default overfetch multiplier when rerank config is unavailable.
_REST_DEFAULT_OVERFETCH_MULTIPLIER = 5

# Module-level logger
logger = logging.getLogger(__name__)

# Story #1400: temporal-query detector for the REST SemanticQueryRequest
# shape -- mirrors mcp/handlers/_utils.py's _is_temporal_query exactly
# (same field set, same "any truthy" semantics), kept as a separate
# function since the two doors' request shapes (Dict vs Pydantic model)
# are not interchangeable.
_TEMPORAL_REQUEST_FIELDS = (
    "time_range",
    "time_range_all",
    "at_commit",
    "chunk_type",
    "diff_type",
    "author",
)


def _is_temporal_query_request(request: "SemanticQueryRequest") -> bool:
    return any(getattr(request, f, None) for f in _TEMPORAL_REQUEST_FIELDS)


def _resolve_temporal_repo_path_rest(
    repository_alias: str,
    current_user,
    app: FastAPI,
    activated_repo_manager,
) -> "Tuple[Optional[str], Optional[JSONResponse]]":
    """Resolve repo_path for a temporal REST query -- activated vs global,
    reusing the SAME resolution patterns this file already uses for the
    FTS/hybrid branch above (never reinventing alias resolution).

    Returns (repo_path, None) on success, (None, error_response) on
    failure -- error_response is a ready-to-return JSONResponse/dict.
    """
    from pathlib import Path as PathLib

    if repository_alias.endswith("-global"):
        from code_indexer.global_repos.alias_manager import AliasManager

        golden_repos_dir = getattr(app.state, "golden_repos_dir", None)
        if not golden_repos_dir:
            return None, JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "success": False,
                    "error": "golden_repos_dir not configured",
                },
            )
        alias_manager = AliasManager(str(PathLib(golden_repos_dir) / "aliases"))
        resolved_path = alias_manager.read_alias(repository_alias)
        if not resolved_path:
            return None, JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={
                    "success": False,
                    "error": f"Alias for '{repository_alias}' not found",
                },
            )
        return str(resolved_path), None

    repo_path = (
        PathLib(activated_repo_manager.activated_repos_dir)
        / current_user.username
        / repository_alias
    )
    return str(repo_path), None


def _execute_temporal_via_live_dispatch_rest(
    request: "SemanticQueryRequest",
    current_user,
    app: FastAPI,
    activated_repo_manager,
) -> JSONResponse:
    """Story #1400: the REST live async-hybrid temporal dispatch entry
    point -- mirrors mcp/handlers/search.py's
    _execute_temporal_via_live_dispatch exactly, adapted for REST's
    SemanticQueryRequest shape and JSONResponse wire format.

    Scenario 12: both doors call the SAME execute_live_temporal_search
    function, so an identical logical query landing on the same node joins
    the same in-flight job. fusion_fetch_limit is computed via the shared
    compute_temporal_fusion_fetch_limit() (temporal_fusion_limit.py) below,
    so an identical logical query from either door now genuinely produces
    the identical dedup signature.
    """
    from code_indexer.services.temporal.temporal_worker_input_adapters import (
        TemporalAliasRejectedError,
        build_temporal_worker_input_from_rest_request,
    )
    from code_indexer.server.services.config_service import get_config_service
    from code_indexer.server.utils.config_manager import (
        TEMPORAL_RESPONSE_RESERVE_SECONDS,
    )
    from code_indexer.services.temporal.temporal_fusion_limit import (
        compute_temporal_fusion_fetch_limit,
    )

    config_service = get_config_service()
    access_filtering_service = getattr(app.state, "access_filtering_service", None)
    fusion_fetch_limit = compute_temporal_fusion_fetch_limit(
        requested_limit=request.limit,
        rerank_query=request.rerank_query,
        access_filtering_service=access_filtering_service,
        username=current_user.username,
        config_service=config_service,
    )

    try:
        worker_input = build_temporal_worker_input_from_rest_request(
            request, current_user.username, fusion_fetch_limit
        )
    except TemporalAliasRejectedError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "success": False,
                "error": str(exc),
                "error_code": exc.error_code,
            },
        )

    repo_path, err = _resolve_temporal_repo_path_rest(
        worker_input.repository_alias, current_user, app, activated_repo_manager
    )
    if err is not None:
        return err
    import dataclasses

    worker_input = dataclasses.replace(worker_input, repo_path=repo_path)

    bjm = getattr(app.state, "background_job_manager", None)
    payload_cache = getattr(app.state, "payload_cache", None)
    if bjm is None or payload_cache is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "success": False,
                "error": "Background job/payload cache service not available",
            },
        )

    inline_wait_seconds = (
        config_service.get_config().search_timeouts_config.temporal_inline_wait_seconds
    )
    is_admin = (
        access_filtering_service.is_admin_user(current_user.username)
        if access_filtering_service
        else False
    )

    # Issue #1435: REST's own outer handler-deadline cap, mirroring MCP's
    # protocol.py _invoke_handler (time.monotonic() + timeout_seconds)
    # exactly. Threading a real value through here (instead of the
    # previous hardcoded None) makes execute_live_temporal_search's
    # waiter_deadline = min(inline_wait, handler_deadline - reserve)
    # computation correctly bound REST's inline wait too -- no route
    # cancellation involved, purely a shorter "waiting" handoff when the
    # operator-configured temporal_inline_wait_seconds is too large.
    handler_deadline_monotonic = (
        time.monotonic()
        + config_service.get_config().search_timeouts_config.rest_query_handler_timeout_seconds
    )

    dispatch_result = execute_live_temporal_search(
        worker_input=worker_input,
        background_job_manager=bjm,
        payload_cache=payload_cache,
        access_filtering_service=access_filtering_service,
        is_admin=is_admin,
        inline_wait_seconds=inline_wait_seconds,
        handler_deadline_monotonic=handler_deadline_monotonic,
        response_reserve_seconds=TEMPORAL_RESPONSE_RESERVE_SECONDS,
        config_service=config_service,
    )

    status_field = dispatch_result.get("status")
    if status_field == "completed":
        results = dispatch_result.get("results", [])
        return JSONResponse(
            content={
                "results": results,
                "total_results": len(results),
                "query_metadata": {"query_text": request.query_text},
            }
        )

    if status_field == "waiting":
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "success": False,
                "error": (
                    f"Temporal query exceeded the {inline_wait_seconds}s "
                    "inline wait window; continuing in the background."
                ),
                "error_code": "TEMPORAL_QUERY_DEFERRED",
                "job_id": dispatch_result["job_id"],
                "partial_results": dispatch_result.get("partial_results", []),
                "continue_polling": True,
                "unranked": True,
                "shards_completed": dispatch_result.get("shards_completed"),
                "shards_total": dispatch_result.get("shards_total"),
            },
        )

    # failed / not_found / capacity_exhausted
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={
            "success": False,
            "error": dispatch_result.get("error", "Temporal query failed"),
            "error_code": dispatch_result.get("error_code"),
            "job_id": dispatch_result.get("job_id"),
        },
    )


def register_query_routes(
    app: FastAPI,
    *,
    semantic_query_manager,
    activated_repo_manager,
) -> None:
    """
    Register the POST /api/query route handler onto the FastAPI app.

    The handler is defined as a closure over the function parameters,
    exactly as it was a closure over create_app() locals before extraction.
    No handler logic is changed.

    Args:
        app: The FastAPI application instance
        semantic_query_manager: SemanticQueryManager instance
        activated_repo_manager: ActivatedRepoManager instance
    """

    @app.post("/api/query")
    def semantic_query(
        request: SemanticQueryRequest,
        raw_request: Request,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Unified search endpoint supporting semantic, FTS, and hybrid modes (Story 5).

        Args:
            request: Search request with mode selection and parameters
            current_user: Current authenticated user

        Returns:
            UnifiedSearchResponse for FTS/hybrid modes, or
            SemanticQueryResponse for backward compatibility with semantic mode

        Raises:
            HTTPException: If query fails, index missing, or invalid parameters
        """
        import socket
        import time
        from pathlib import Path as PathLib
        from code_indexer.server.services.search_event_context import (
            SearchEventContext,
            _search_event_ctx,
        )
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventRecord,
        )
        from code_indexer.server.services.config_service import get_config_service
        from ..services.shard_router import FORWARD_HEADER

        # Cluster sharding: if this is a concrete single-repo query for a repo this
        # node does not own, forward it to an owner pod (which has it cached) and
        # return that response. Loop-guarded via FORWARD_HEADER; wildcards (omni)
        # and async job submissions stay local. Fails open: any routing problem
        # (or no shard_router at all -> solo/symmetric) falls through to local.
        shard_router = getattr(raw_request.app.state, "shard_router", None)
        if (
            shard_router is not None
            and raw_request.headers.get(FORWARD_HEADER.lower()) != "1"
        ):
            alias = request.repository_alias
            if (
                isinstance(alias, str)
                and alias
                and "*" not in alias
                and not getattr(request, "async_query", False)
            ):
                try:
                    target = shard_router.target_for(alias)
                    if target:
                        from fastapi.responses import JSONResponse

                        forwarded = shard_router.forward(
                            target,
                            request.model_dump(),
                            raw_request.headers.get("authorization"),
                        )
                        return JSONResponse(content=forwarded)
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "shard forward failed for %r (%s); serving locally",
                        alias,
                        exc,
                        extra={"correlation_id": get_correlation_id()},
                    )

        start_time = time.time()

        # Issue #1159: install per-request search event context.
        _search_type = request.search_mode or "semantic"
        _event_ctx = SearchEventContext(
            username=current_user.username,
            repo_alias=request.repository_alias,
            search_type=_search_type,
            query_text=(request.query_text or "")[:500],
        )
        _ctx_token = _search_event_ctx.set(_event_ctx)
        _result_count = 0
        _search_succeeded = False

        try:
            # Handle background job submission (semantic mode only)
            if request.async_query:
                if request.search_mode != "semantic":
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Async query only supported for semantic search mode",
                    )

                job_id = semantic_query_manager.submit_query_job(
                    username=current_user.username,
                    query_text=request.query_text,
                    repository_alias=request.repository_alias,
                    limit=request.limit,
                    min_score=request.min_score,
                    file_extensions=request.file_extensions,
                )
                from fastapi.responses import JSONResponse

                _search_succeeded = True
                return JSONResponse(
                    status_code=status.HTTP_202_ACCEPTED,
                    content={
                        "job_id": job_id,
                        "message": "Semantic query submitted as background job",
                    },
                )

            # Story #1400: async-hybrid temporal query execution. Intercept
            # BEFORE the FTS/hybrid branch below -- temporal queries bypass
            # the old fully-synchronous semantic_query_manager path
            # entirely via the async-hybrid worker/dedup/poll machinery
            # shared with the MCP door (execute_live_temporal_search).
            if _is_temporal_query_request(request):
                _temporal_response = _execute_temporal_via_live_dispatch_rest(
                    request, current_user, app, activated_repo_manager
                )
                # Bug #1173: only enqueue telemetry on real success -- a
                # deferred handoff (202, not yet a real result) must NOT be
                # logged as success; a genuinely completed query (200) must.
                # Exact equality (not "< 400"): 202 is < 400 but is NOT a
                # completed result and must never be logged as success.
                # The finally block below genuinely runs on this early
                # return (Python finally semantics), so _result_count must
                # reflect the REAL result count, not the stale default --
                # a hardcoded 0 would silently corrupt the search-event log
                # for every temporal query, including genuine successes.
                _search_succeeded = _temporal_response.status_code == 200
                if _search_succeeded:
                    import json as _json_mod

                    try:
                        _body = _json_mod.loads(bytes(_temporal_response.body))
                        _result_count = len(_body.get("results") or [])
                    except (ValueError, TypeError) as _parse_exc:
                        logger.debug(
                            "inline_query: could not parse temporal response "
                            "body for telemetry result_count: %s",
                            _parse_exc,
                        )
                return _temporal_response

            # Story 5: Handle FTS and Hybrid modes
            if request.search_mode in ["fts", "hybrid"]:
                # Get user's activated repositories
                activated_repos = activated_repo_manager.list_activated_repositories(
                    current_user.username
                )

                # ALSO get global repos from BackendRegistry (works in standalone and cluster mode)
                global_repos_list = []
                try:
                    backend_registry = getattr(app.state, "backend_registry", None)
                    if backend_registry:
                        repos_dict = backend_registry.global_repos.list_repos()
                        for alias_name, repo_data in repos_dict.items():
                            global_repos_list.append(
                                {
                                    "user_alias": repo_data["alias_name"],
                                    "username": "global",
                                    "is_global": True,
                                    "repo_url": repo_data.get("repo_url", ""),
                                }
                            )
                except Exception as e:
                    logger.warning(f"Failed to load global repos for FTS/hybrid: {e}")

                # Merge user repos and global repos
                activated_repos = activated_repos + global_repos_list

                if not activated_repos:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="No activated repositories found for user",
                    )

                # Filter to specific repository if requested
                if request.repository_alias:
                    activated_repos = [
                        repo
                        for repo in activated_repos
                        if repo["user_alias"] == request.repository_alias
                    ]
                    if not activated_repos:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Repository '{request.repository_alias}' not found",
                        )

                # Check FTS index availability for each repository
                fts_available = False
                repo_path = None
                for repo in activated_repos:
                    # Construct path - different for global vs user repos
                    if repo.get("is_global"):
                        # Global repos: use AliasManager to resolve versioned snapshot path
                        from code_indexer.global_repos.alias_manager import AliasManager

                        golden_repos_dir = getattr(app.state, "golden_repos_dir", None)
                        if not golden_repos_dir:
                            logger.warning(
                                f"golden_repos_dir not configured, skipping global repo {repo['user_alias']}"
                            )
                            continue
                        alias_manager = AliasManager(
                            str(PathLib(golden_repos_dir) / "aliases")
                        )
                        resolved_path = alias_manager.read_alias(repo["user_alias"])
                        if not resolved_path:
                            logger.warning(
                                f"Failed to resolve alias path for {repo['user_alias']}, skipping"
                            )
                            continue
                        repo_path = PathLib(resolved_path)
                    else:
                        # User repos: activated-repos/username/alias
                        repo_path = (
                            PathLib(activated_repo_manager.activated_repos_dir)
                            / current_user.username
                            / repo["user_alias"]
                        )
                    if repo_path is None:
                        continue
                    fts_index_dir = repo_path / ".code-indexer" / "tantivy_index"
                    if fts_index_dir.exists():
                        fts_available = True
                        break

                # Validate search mode based on index availability
                search_mode_actual = request.search_mode
                if request.search_mode == "fts" and not fts_available:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "error": "FTS index not available",
                            "suggestion": "Build FTS index with 'cidx index --fts' in the repository",
                            "available_modes": ["semantic"],
                        },
                    )

                if request.search_mode == "hybrid" and not fts_available:
                    # Graceful degradation for hybrid mode
                    logger.warning(
                        format_error_log(
                            "APP-GENERAL-032",
                            f"FTS index not available for user {current_user.username}, degrading hybrid to semantic-only",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    search_mode_actual = "semantic"

                # Execute FTS or hybrid search
                fts_results = []
                semantic_results_list = []

                if search_mode_actual in ["fts", "hybrid"] and fts_available:
                    # Execute FTS search
                    from code_indexer.services.tantivy_index_manager import (
                        TantivyIndexManager,
                    )
                    from ..services.api_metrics_service import api_metrics_service

                    # Track FTS search at service layer (Story #4 AC2)
                    api_metrics_service.increment_other_index_search(
                        username=current_user.username
                    )

                    try:
                        # repo_path is guaranteed to be set if fts_available is True
                        if repo_path is None:
                            raise RuntimeError(
                                "repo_path is None despite FTS being available"
                            )

                        # Initialize Tantivy manager for first available repository
                        tantivy_manager = TantivyIndexManager(
                            repo_path / ".code-indexer" / "tantivy_index"
                        )
                        tantivy_manager.open_for_search()

                        # Handle fuzzy flag
                        edit_dist = request.edit_distance
                        if request.fuzzy and edit_dist == 0:
                            edit_dist = 1

                        # Execute FTS query
                        fts_raw_results = tantivy_manager.search(
                            query_text=request.query_text,
                            case_sensitive=request.case_sensitive,
                            edit_distance=edit_dist,
                            snippet_lines=request.snippet_lines,
                            limit=request.limit,
                            language_filter=request.language,
                            path_filter=request.path_filter,
                            exclude_languages=(
                                [request.exclude_language]
                                if request.exclude_language
                                else None
                            ),  # Story #503 Phase 1
                            exclude_paths=(
                                [request.exclude_path] if request.exclude_path else None
                            ),  # Story #503 Phase 1
                            use_regex=request.regex,  # Story #503 Phase 1
                        )

                        # Convert to API response format
                        for result in fts_raw_results:
                            fts_results.append(
                                FTSResultItem(
                                    path=result.get("path", ""),
                                    line_start=result.get("line_start", 0),
                                    line_end=result.get("line_end", 0),
                                    snippet=result.get("snippet", ""),
                                    language=result.get("language", "unknown"),
                                    repository_alias=request.repository_alias
                                    or activated_repos[0]["user_alias"],
                                )
                            )

                    except Exception as e:
                        logger.error(
                            format_error_log(
                                "APP-GENERAL-033",
                                f"FTS search failed: {e}",
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )
                        if request.search_mode == "fts":
                            raise HTTPException(
                                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail=f"FTS search failed: {str(e)}",
                            )
                        # For hybrid mode, continue with semantic only
                        search_mode_actual = "semantic"

                # Execute semantic search for hybrid or degraded mode
                if search_mode_actual in ["semantic", "hybrid"]:
                    try:
                        semantic_results_raw = semantic_query_manager.query_user_repositories(
                            username=current_user.username,
                            query_text=request.query_text,
                            repository_alias=request.repository_alias,
                            limit=request.limit,
                            min_score=request.min_score,
                            file_extensions=request.file_extensions,
                            # Phase 1 parameters (Story #503)
                            exclude_language=request.exclude_language,
                            exclude_path=request.exclude_path,
                            accuracy=request.accuracy,
                            # Temporal parameters (Story #446)
                            time_range=request.time_range,
                            time_range_all=request.time_range_all,
                            at_commit=request.at_commit,
                            # Phase 3 temporal filtering parameters (Story #503)
                            diff_type=request.diff_type,
                            author=request.author,
                            chunk_type=request.chunk_type,
                            # Story #1108 (S4): per-request cache bypass
                            no_embedding_cache_shortcut=request.no_embedding_cache_shortcut,
                            # Story #1291 AC7/AC8: explicit embedder override
                            temporal_embedder=request.temporal_embedder,
                        )
                        semantic_results_list = [
                            QueryResultItem(**result)
                            for result in semantic_results_raw["results"]
                        ]
                    except ValueError as e:
                        # Surface validation errors as HTTP 400
                        logger.warning(
                            format_error_log(
                                "APP-GENERAL-034",
                                f"Validation error in query: {e}",
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail={
                                "error": "Invalid query parameters",
                                "message": str(e),
                            },
                        )
                    except Exception as e:
                        logger.error(
                            format_error_log(
                                "APP-GENERAL-035",
                                f"Semantic search failed: {e}",
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )
                        if search_mode_actual == "semantic":
                            raise

                # Calculate execution time
                execution_time_ms = int((time.time() - start_time) * 1000)

                # Apply access filtering based on user's group membership (Story #707)
                if (
                    hasattr(app.state, "access_filtering_service")
                    and app.state.access_filtering_service
                ):
                    fts_dicts = [r.model_dump() for r in fts_results]
                    fts_dicts = app.state.access_filtering_service.filter_query_results(
                        fts_dicts, current_user.username
                    )
                    semantic_dicts = [r.model_dump() for r in semantic_results_list]
                    semantic_dicts = (
                        app.state.access_filtering_service.filter_query_results(
                            semantic_dicts, current_user.username
                        )
                    )
                else:
                    fts_dicts = [r.model_dump() for r in fts_results]
                    semantic_dicts = [r.model_dump() for r in semantic_results_list]

                # Apply payload truncation for consistency with MCP handlers
                _payload_cache = getattr(app.state, "payload_cache", None)
                truncated_fts = _apply_rest_fts_truncation(
                    fts_dicts, payload_cache=_payload_cache
                )
                truncated_semantic = _apply_rest_semantic_truncation(
                    semantic_dicts, payload_cache=_payload_cache
                )

                # Return as JSONResponse to support truncated fields
                from fastapi.responses import JSONResponse

                _result_count = len(truncated_fts) + len(truncated_semantic)
                _search_succeeded = True
                return JSONResponse(
                    content={
                        "search_mode": search_mode_actual,
                        "query": request.query_text,
                        "fts_results": truncated_fts,
                        "semantic_results": truncated_semantic,
                        "metadata": {
                            "query_text": request.query_text,
                            "search_mode_requested": request.search_mode,
                            "search_mode_actual": search_mode_actual,
                            "execution_time_ms": execution_time_ms,
                            "fts_available": fts_available,
                            "semantic_available": True,
                            "repositories_searched": len(activated_repos),
                        },
                    }
                )

            # Default semantic mode (backward compatibility)
            # Bug #1209: when reranking is requested, overfetch so the reranker
            # receives a larger candidate pool before trimming to requested_limit.
            _requested_limit = request.limit
            _fetch_limit = _requested_limit
            if request.rerank_query:
                _cfg_svc = get_config_service()
                _rc = _cfg_svc.get_config().rerank_config
                _overfetch_mul = (
                    _rc.overfetch_multiplier
                    if _rc
                    else _REST_DEFAULT_OVERFETCH_MULTIPLIER
                )
                _fetch_limit = _rest_calculate_overfetch_limit(
                    _requested_limit, _overfetch_mul
                )

            results = semantic_query_manager.query_user_repositories(
                username=current_user.username,
                query_text=request.query_text,
                repository_alias=request.repository_alias,
                limit=_fetch_limit,
                min_score=request.min_score,
                file_extensions=request.file_extensions,
                # Phase 1 parameters (Story #503)
                exclude_language=request.exclude_language,
                exclude_path=request.exclude_path,
                accuracy=request.accuracy,
                # Temporal parameters (Story #446)
                time_range=request.time_range,
                time_range_all=request.time_range_all,
                at_commit=request.at_commit,
                # Phase 3 temporal filtering parameters (Story #503)
                diff_type=request.diff_type,
                author=request.author,
                chunk_type=request.chunk_type,
                # Story #1108 (S4): per-request cache bypass
                no_embedding_cache_shortcut=request.no_embedding_cache_shortcut,
                # Story #1291 AC7/AC8: explicit embedder override
                temporal_embedder=request.temporal_embedder,
            )

            # Apply access filtering based on user's group membership (Story #707)
            if (
                hasattr(app.state, "access_filtering_service")
                and app.state.access_filtering_service
            ):
                results["results"] = (
                    app.state.access_filtering_service.filter_query_results(
                        results["results"], current_user.username
                    )
                )
                results["total_results"] = len(results["results"])

            # Bug #1209: apply reranking AFTER fusion and BEFORE truncation,
            # mirroring the MCP _apply_rerank_and_filter pipeline order exactly.
            # The reranker receives the full fused candidate set; it trims to
            # _requested_limit internally.
            if request.rerank_query:
                results["results"], _rerank_meta = _rest_apply_reranking_sync(
                    results=results["results"],
                    rerank_query=request.rerank_query,
                    rerank_instruction=request.rerank_instruction,
                    content_extractor=_rest_extract_rerank_document,
                    requested_limit=_requested_limit,
                    config_service=get_config_service(),
                )
                results["total_results"] = len(results["results"])

            # Apply payload truncation for consistency with MCP handlers
            truncated_results = _apply_rest_semantic_truncation(
                results["results"],
                payload_cache=getattr(app.state, "payload_cache", None),
            )

            # Return as JSONResponse to support truncated fields
            from fastapi.responses import JSONResponse

            _result_count = len(truncated_results)
            _search_succeeded = True
            return JSONResponse(
                content={
                    "results": truncated_results,
                    "total_results": results["total_results"],
                    "query_metadata": results["query_metadata"],
                    "warning": results.get("warning"),
                }
            )

        except HTTPException:
            # Re-raise HTTP exceptions as-is
            raise

        except ValueError as e:
            # Surface validation errors from backend as HTTP 400
            logger.warning(
                format_error_log(
                    "APP-GENERAL-036",
                    f"Validation error in query: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "Invalid query parameters", "message": str(e)},
            )

        except SemanticQueryError as e:
            error_message = str(e)

            # Determine appropriate HTTP status code based on error type
            if "not found" in error_message.lower():
                status_code = status.HTTP_404_NOT_FOUND
            elif "timed out" in error_message.lower():
                status_code = status.HTTP_408_REQUEST_TIMEOUT
            elif "no activated repositories" in error_message.lower():
                status_code = status.HTTP_400_BAD_REQUEST
            else:
                status_code = status.HTTP_400_BAD_REQUEST

            raise HTTPException(
                status_code=status_code,
                detail=error_message,
            )

        except Exception as e:
            logger.error(
                format_error_log(
                    "APP-GENERAL-037",
                    f"Unexpected error in unified search: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Internal search error: {str(e)}",
            )
        finally:
            # Issue #1159: reset ctx and enqueue search event record.
            # Bug #1173: only enqueue on success (H11 — failed searches must NOT log).
            _search_event_ctx.reset(_ctx_token)
            _writer = getattr(
                getattr(app, "state", None), "search_event_log_writer", None
            )
            if _writer is not None and _search_succeeded:
                try:
                    _total_ms = int((time.time() - start_time) * 1000)
                    _cfg_svc = get_config_service()
                    _cfg_get = getattr(_cfg_svc, "get_config", None)
                    _cfg_obj = _cfg_get() if callable(_cfg_get) else None
                    _node_id = str(getattr(_cfg_obj, "node_id", "") or "")
                    if not _node_id:
                        try:
                            _node_id = socket.gethostname()
                        except OSError as _hn_exc:
                            logger.debug(
                                "inline_query: socket.gethostname() failed, using 'unknown': %s",
                                _hn_exc,
                            )
                            _node_id = "unknown"
                    _record = SearchEventRecord(
                        timestamp=time.time(),
                        username=current_user.username,
                        repo_alias=_event_ctx.repo_alias,
                        search_type=_event_ctx.search_type,
                        query_text=_event_ctx.query_text,
                        voyage_cache_hit=_event_ctx.voyage_cache_hit,
                        voyage_cache_mode=_event_ctx.voyage_cache_mode,
                        voyage_latency_ms=_event_ctx.voyage_latency_ms,
                        cohere_cache_hit=_event_ctx.cohere_cache_hit,
                        cohere_cache_mode=_event_ctx.cohere_cache_mode,
                        cohere_latency_ms=_event_ctx.cohere_latency_ms,
                        total_latency_ms=_total_ms,
                        result_count=_result_count,
                        node_id=_node_id,
                        correlation_id=get_correlation_id(),
                    )
                    _writer.enqueue(_record)
                except Exception as _enq_exc:  # noqa: BLE001
                    logger.debug(
                        "inline_query: failed to enqueue search event record: %s",
                        _enq_exc,
                    )

    @app.get("/api/query/result/{job_id}")
    def get_query_result(
        job_id: str,
        raw_request: Request,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """Story #1400 Phase 8: poll an async-hybrid temporal query job.

        Ownership-checked via background_job_manager.get_job_status (same
        not-found/unauthorized-indistinguishable contract as the MCP
        poll_search_job tool). Thin reader around poll_temporal_job_status.
        """
        from code_indexer.server.mcp.handlers._utils import (
            _get_access_filtering_service,
        )
        from code_indexer.server.services.temporal_poll_job_status import (
            poll_temporal_job_status,
        )
        from code_indexer.server.services.temporal_snapshot_store import (
            read_temporal_snapshot,
        )
        from code_indexer.server.auth.user_manager import UserRole
        from code_indexer.server.services.config_service import get_config_service

        bjm = getattr(raw_request.app.state, "background_job_manager", None)
        if bjm is None:
            raise HTTPException(
                status_code=503, detail="Background job service not available"
            )

        is_admin = hasattr(current_user, "role") and current_user.role == UserRole.ADMIN
        job_status = bjm.get_job_status(
            job_id, current_user.username, is_admin=is_admin
        )

        def _read_snapshot():
            return read_temporal_snapshot(
                getattr(raw_request.app.state, "payload_cache", None), job_id
            )

        result = poll_temporal_job_status(
            job_status=job_status,
            read_snapshot_fn=_read_snapshot,
            access_filtering_service=_get_access_filtering_service(),
            username=current_user.username,
            is_admin=is_admin,
            config_service=get_config_service(),
        )
        if result["status"] == "not_found":
            return JSONResponse(result, status_code=404)
        return result
