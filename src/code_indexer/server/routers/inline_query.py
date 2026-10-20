"""
Query route handler extracted from inline_routes.py.

Part of the inline_routes.py modularization effort. Contains 1 route handler:
- POST /api/query

Zero behavior change: same path, method, response models, and handler logic.
"""

import logging
import sys
import time
from typing import Any, Optional, Tuple

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
from ..query.search_error_policy import classify_search_error
from ..query.semantic_query_manager import SearchFailedError, SearchRequestError
from ...services.multi_index_query_service import MultiIndexQueryTimeoutError
from ..auth import dependencies
from ..logging_utils import format_error_log, public_error_message
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
from code_indexer.server.services.deactivation_query_drain import (
    track_activated_repo_query,
)
from code_indexer.server.services.repo_access_guard import (
    AccessFilteringServiceUnavailableError,
    narrow_global_repos_to_accessible,
)
from .repo_access_http import access_control_unavailable_error
from code_indexer.server.services.query_admission_gate import (
    check_query_admission,
    raise_memory_pressure_http_error,
)
from code_indexer.server.services.temporal_live_dispatch import (
    execute_live_temporal_search,
)

# Story #1586 Finding 2 remediation: this REST route calls
# query_user_repositories()/TantivyIndexManager.search() directly rather
# than through server/mcp/handlers/search.py's AC1-instrumented
# _execute_tracked_search/_execute_regex_search, so it needs its own
# cidx.search.*/cidx.fts.* recording -- see _record_rest_search_metric/
# _record_rest_fts_metric below. No-op when telemetry is disabled.
from code_indexer.server.telemetry.manager import peek_telemetry_manager
from code_indexer.server.telemetry.metrics_instrumentation import (
    get_application_metrics,
)
from code_indexer.server.services.search_event_recording import SearchOrigin

# Bug #1209: default overfetch multiplier when rerank config is unavailable.
_REST_DEFAULT_OVERFETCH_MULTIPLIER = 5

# Module-level logger
logger = logging.getLogger(__name__)

# Story #2117: the tool and door every POST /api/query search event carries.
REST_QUERY_ORIGIN = SearchOrigin(tool="POST /api/query", door="rest")


def _rest_failure_class(exc: Optional[BaseException]) -> Optional[str]:
    """Story #2117: the error class of the exception leaving the query route.

    None when nothing failed, and for a request rejected before its
    repository resolved (search_event_outcomes.failure_error_class: an
    unknown repository, a refused parameter, an HTTPException the door
    raised itself): nothing is recorded.
    """
    from code_indexer.server.services.search_event_outcomes import (
        failure_error_class,
    )

    return None if exc is None else failure_error_class(exc)


def _record_rest_query_event(
    app: Any,
    context: Any,
    delivered: Optional[list],
    failure_class: Optional[str],
    start_time: float,
) -> None:
    """Record one POST /api/query search (`context` is its
    SearchEventContext): its delivered results (Bug #2111: one row per
    repository it searched), or a failure. Never raises."""
    from code_indexer.server.services.search_event_recording import (
        record_query_failure,
        record_query_results,
        writer_from_app,
    )

    latency_ms = int((time.time() - start_time) * 1000)
    # get_correlation_id: this module's alias of
    # correlation_bridge.get_current_correlation_id (imports above).
    if delivered is not None:
        record_query_results(
            writer_from_app(app),
            origin=REST_QUERY_ORIGIN,
            context=context,
            results=delivered,
            total_latency_ms=latency_ms,
            correlation_id=get_correlation_id(),
        )
    elif failure_class is not None:
        record_query_failure(
            writer_from_app(app),
            origin=REST_QUERY_ORIGIN,
            context=context,
            error_class=failure_class,
            total_latency_ms=latency_ms,
            correlation_id=get_correlation_id(),
        )


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


def _record_rest_search_metric(
    search_type: str,
    repository: str,
    duration_seconds: float,
    results_count: int,
    status: str,
) -> None:
    """Record a cidx.search.* OTEL metric for one REST /api/query semantic/
    hybrid search call (Story #1586 Finding 2). Mirrors
    server/mcp/handlers/search.py's _record_search_metric -- this REST
    route calls query_user_repositories() directly rather than through
    _execute_tracked_search, so AC1's instrumentation never covered it.
    No-op when telemetry is disabled; never raises into the query path.
    """
    try:
        telemetry_manager = peek_telemetry_manager()
        if telemetry_manager is None:
            return
        app_metrics = get_application_metrics(telemetry_manager)
        if not app_metrics.is_active:
            return
        app_metrics.record_search_request(
            search_type=search_type,
            repository=repository,
            duration_seconds=duration_seconds,
            results_count=results_count,
            status=status,
        )
    except Exception as e:
        logger.debug(f"Failed to record REST search metrics: {e}")


def _record_rest_fts_metric(
    repository: str,
    duration_seconds: float,
    matches_count: int,
    status: str,
) -> None:
    """Record a cidx.fts.* OTEL metric for one REST /api/query FTS/hybrid
    search call (Story #1586 Finding 2). Mirrors
    server/mcp/handlers/search.py's _record_fts_metric -- this REST route
    reads the Tantivy index directly rather than through
    _execute_regex_search, so AC1's instrumentation never covered it.
    No-op when telemetry is disabled; never raises into the query path.
    """
    try:
        telemetry_manager = peek_telemetry_manager()
        if telemetry_manager is None:
            return
        app_metrics = get_application_metrics(telemetry_manager)
        if not app_metrics.is_active:
            return
        app_metrics.record_fts_request(
            repository=repository,
            duration_seconds=duration_seconds,
            matches_count=matches_count,
            status=status,
        )
    except Exception as e:
        logger.debug(f"Failed to record REST FTS metrics: {e}")


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

        # Story #2117: refusals before the repository resolved carry a
        # rejection code, so they are not recorded as searches.
        from code_indexer.server.services.search_event_outcomes import (
            REPOSITORY_NOT_FOUND_CODE,
            SERVICE_UNAVAILABLE_CODE,
        )

        golden_repos_dir = getattr(app.state, "golden_repos_dir", None)
        if not golden_repos_dir:
            return None, JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "success": False,
                    "error": "golden_repos_dir not configured",
                    "error_code": SERVICE_UNAVAILABLE_CODE,
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
                    "error_code": REPOSITORY_NOT_FOUND_CODE,
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
    """Story #1400: REST twin of MCP _execute_temporal_via_live_dispatch; both
    doors call the SAME execute_live_temporal_search (shared fusion_fetch_limit),
    so an identical query from either door joins the same in-flight job."""
    from code_indexer.server.services.cidx_meta_access.history_policy import (
        refuse_catalog_temporal,
    )
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
    except ValueError as exc:
        # The builder only normalizes the caller's own parameters.
        raise SearchRequestError(str(exc)) from exc
    except TemporalAliasRejectedError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "success": False,
                "error": str(exc),
                "error_code": exc.error_code,
            },
        )

    refuse_catalog_temporal(
        worker_input.repository_alias,
        current_user.username,
        access_filtering_service,
        activated_repo_manager,
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
        from code_indexer.server.services.search_event_outcomes import (
            SERVICE_UNAVAILABLE_CODE,
        )

        # Story #2117: a refusal before the search started (not recorded).
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "success": False,
                "error": "Background job/payload cache service not available",
                "error_code": SERVICE_UNAVAILABLE_CODE,
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
        # Bug #1482 (REOPENED): thread the real QueryTracker into the live
        # worker so it can construct a resolution-scope-safe
        # TemporalShardResolver and consult the golden-owned sister
        # location Story #1457's relocation trigger may have moved shard
        # data to. Mirrors mcp/handlers/search.py's
        # _execute_temporal_via_live_dispatch (the MCP door, already fixed
        # by commit 7ae9b9bb) -- REST's sibling call site here was the
        # confirmed gap: omitting this kwarg silently forced every REST
        # temporal query back onto the legacy in-repo scan, which is empty
        # once relocation succeeds.
        query_tracker=getattr(app.state, "query_tracker", None),
        # Bug #1533: thread the server's DI-wired ActivatedRepoManager so the
        # worker resolves golden lineage from the SHARED (PostgreSQL in
        # cluster mode) metadata store instead of a node-local read that
        # cannot see an activation made on another node. Wired at BOTH doors
        # deliberately -- the query_tracker kwarg above was the confirmed
        # Bug #1482 gap precisely because only the MCP door had it.
        activated_repo_manager=getattr(app.state, "activated_repo_manager", None),
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
        _admission = check_query_admission()
        if not _admission.allowed:
            raise_memory_pressure_http_error(_admission)

        import time
        from pathlib import Path as PathLib
        from code_indexer.server.services.search_event_context import (
            SearchEventContext,
            _search_event_ctx,
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
            # Full text: the shared recording module caps and marks it.
            query_text=request.query_text or "",
        )
        _ctx_token = _search_event_ctx.set(_event_ctx)
        # Story #2117: what the `finally` below records -- the delivered
        # results of a completed search, a failure known without an
        # exception, or nothing when a job or a deferred handoff records later.
        _delivered: Optional[list] = None
        _failure_class: Optional[str] = None
        _record_on_exit = True

        try:
            # Handle background job submission (semantic mode only)
            if request.async_query:
                if request.search_mode != "semantic":
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Async query only supported for semantic search mode",
                    )

                from code_indexer.server.services.search_event_recording import (
                    PendingSearchEvent,
                    writer_from_app,
                )

                # Bug #2113: the job records this search when it completes,
                # with the real result count; the submission records nothing.
                _record_on_exit = False
                job_id = semantic_query_manager.submit_query_job(
                    username=current_user.username,
                    query_text=request.query_text,
                    repository_alias=request.repository_alias,
                    limit=request.limit,
                    min_score=request.min_score,
                    file_extensions=request.file_extensions,
                    search_event=PendingSearchEvent(
                        writer=writer_from_app(app),
                        context=_event_ctx,
                        correlation_id=get_correlation_id(),
                        origin=REST_QUERY_ORIGIN,
                    ),
                )
                from fastapi.responses import JSONResponse

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
                # Bug #2109: a temporal search is recorded as "temporal",
                # whatever search_mode the request also carries.
                _event_ctx.search_type = "temporal"
                _temporal_response = _execute_temporal_via_live_dispatch_rest(
                    request, current_user, app, activated_repo_manager
                )
                # Bug #1173 / Story #2117: a completed query (exactly 200)
                # records its delivered results -- the finally block below
                # runs on this early return, so _delivered must hold the REAL
                # results. A deferred handoff (202, not yet a result) records
                # nothing now; any other answer is a failed search.
                if _temporal_response.status_code == status.HTTP_200_OK:
                    import json as _json_mod

                    _delivered = []
                    try:
                        _body = _json_mod.loads(bytes(_temporal_response.body))
                        _delivered = list(_body.get("results") or [])
                    except (ValueError, TypeError) as _parse_exc:
                        logger.debug(
                            "inline_query: could not parse temporal response "
                            "body for the search event's results: %s",
                            _parse_exc,
                        )
                elif _temporal_response.status_code == status.HTTP_202_ACCEPTED:
                    # Bug #2109: a deferred handoff leaves a pending record;
                    # GET /api/query/result/{job_id} records the search when
                    # it delivers the completed result.
                    _record_on_exit = False
                    import json as _json_mod

                    from code_indexer.server.services.search_event_recording import (
                        remember_deferred_search,
                    )

                    try:
                        _handoff = _json_mod.loads(bytes(_temporal_response.body))
                    except (ValueError, TypeError) as _parse_exc:
                        logger.warning(
                            "inline_query: deferred temporal search not "
                            "recorded, handoff body unreadable: %s: %s",
                            type(_parse_exc).__name__,
                            _parse_exc,
                        )
                    else:
                        remember_deferred_search(
                            getattr(app.state, "payload_cache", None),
                            _handoff,
                            origin=REST_QUERY_ORIGIN,
                            context=_event_ctx,
                            correlation_id=get_correlation_id(),
                            started_at=start_time,
                        )
                else:
                    # Story #2117: a rejection (4xx, 503) records nothing; a
                    # failure after resolution records its error code's class.
                    import json as _json_mod

                    from code_indexer.server.services.search_event_outcomes import (
                        http_response_failure_class,
                    )

                    try:
                        _error_body = dict(
                            _json_mod.loads(bytes(_temporal_response.body))
                        )
                    except (ValueError, TypeError):
                        _error_body = {}
                    _failure_class = http_response_failure_class(
                        _temporal_response.status_code, _error_body
                    )
                    _record_on_exit = _failure_class is not None
                return _temporal_response

            # Story 5: Handle FTS and Hybrid modes
            if request.search_mode in ["fts", "hybrid"]:
                from ..query.filtered_search import extension_overfetch_deadline

                # #2047: ONE extension over-fetch time budget for this
                # request, shared by the FTS half and the semantic half.
                extension_deadline = extension_overfetch_deadline(
                    request.file_extensions
                )

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

                # Query searches only repositories the caller can access: the
                # FTS index selection below, the alias match and the
                # repositories_searched count only consider accessible global
                # repos (same helper and semantics as
                # SemanticQueryManager.query_user_repositories). Without the
                # access service a query that would include a global repo is
                # refused (500 access_control_unavailable, every caller).
                global_repos_list = narrow_global_repos_to_accessible(
                    getattr(app.state, "access_filtering_service", None),
                    current_user.username,
                    global_repos_list,
                    repository_alias=request.repository_alias,
                )

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
                # Codex HIGH finding (round 5): an alias-less/omni request
                # has request.repository_alias == None, a no-op key for
                # track_activated_repo_query -- capture the ACTUAL repo
                # whose FTS index gets read below, so tracking uses the
                # real per-repository identity (mirrors the fan-out
                # pattern semantic_query_manager.py's query_user_
                # repositories already established, round 4), never the
                # possibly-absent request-level alias.
                fts_repo_alias: Optional[str] = None
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
                        # User repos: activated-repos/username/alias.
                        # Route through the same realpath-containment
                        # helper every other (username, user_alias) join in
                        # ActivatedRepoManager uses, instead of a hand-built
                        # join that bypasses it.
                        repo_path = PathLib(
                            activated_repo_manager.get_activated_repo_path(
                                current_user.username, repo["user_alias"]
                            )
                        )
                    if repo_path is None:
                        continue
                    fts_index_dir = repo_path / ".code-indexer" / "tantivy_index"
                    if fts_index_dir.exists():
                        fts_available = True
                        fts_repo_alias = repo.get("user_alias")
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

                    # Bug #2111 / Story #2117: without an alias, the FTS
                    # search reads fts_repo_alias's index -- noted before it
                    # runs, so a failed FTS search is recorded for it too.
                    if (
                        not request.repository_alias
                        and _event_ctx.searched_repositories is None
                    ):
                        _event_ctx.repo_alias = fts_repo_alias

                    # Codex HIGH finding (round 2): this branch reads the
                    # Tantivy FTS index directly (never calls
                    # query_user_repositories()), so it was completely
                    # invisible to the track_activated_repo_query()
                    # wrapping already applied to the semantic/hybrid
                    # branch below -- wire the SAME tracking here too.
                    #
                    # Codex HIGH finding (round 5): use fts_repo_alias
                    # (the REAL repo whose index is about to be read),
                    # not request.repository_alias -- an alias-less/omni
                    # request has the latter as None, which would make
                    # tracking a silent no-op even though a concrete
                    # activated repo's chunks are being read.
                    with track_activated_repo_query(
                        getattr(app.state, "query_tracker", None),
                        activated_repo_manager,
                        current_user.username,
                        fts_repo_alias,
                    ):
                        # Story #1586 Finding 2: time this real Tantivy call
                        # and record cidx.fts.* in `finally` (success or
                        # error) -- this REST path reads the index
                        # directly, so it needs its own metric recording
                        # instead of inheriting AC1's MCP-side one.
                        _fts_metric_start = time.monotonic()
                        _fts_status = "error"
                        _fts_matches_count = 0
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

                            from ..query.filtered_search import fts_filter_kwargs

                            # Execute FTS query; the filters come from the one
                            # shared builder (Story #503 Phase 1 excludes,
                            # #2047 file_extensions under the request deadline).
                            fts_raw_results = tantivy_manager.search(
                                query_text=request.query_text,
                                case_sensitive=request.case_sensitive,
                                edit_distance=edit_dist,
                                snippet_lines=request.snippet_lines,
                                limit=request.limit,
                                use_regex=request.regex,  # Story #503 Phase 1
                                **fts_filter_kwargs(
                                    language=request.language,
                                    path_filter=request.path_filter,
                                    exclude_language=request.exclude_language,
                                    exclude_path=request.exclude_path,
                                    file_extensions=request.file_extensions,
                                    deadline=extension_deadline,
                                ),
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
                                        # Codex round-6 LOW finding: use
                                        # fts_repo_alias (the repo whose
                                        # index was ACTUALLY read), not
                                        # activated_repos[0] (the first
                                        # repo in the list, which may
                                        # lack an FTS index entirely).
                                        repository_alias=request.repository_alias
                                        or fts_repo_alias
                                        or "",
                                    )
                                )

                            _fts_status = "success"
                            _fts_matches_count = len(fts_raw_results)

                        except Exception as e:
                            logger.error(
                                format_error_log(
                                    "APP-GENERAL-033",
                                    f"FTS search failed: {e}",
                                ),
                                exc_info=True,
                            )
                            if request.search_mode == "fts":
                                _fts_outcome = classify_search_error(e)
                                raise HTTPException(
                                    status_code=(
                                        status.HTTP_400_BAD_REQUEST
                                        if _fts_outcome.client_error
                                        else status.HTTP_504_GATEWAY_TIMEOUT
                                        if _fts_outcome.timed_out
                                        else status.HTTP_500_INTERNAL_SERVER_ERROR
                                    ),
                                    detail=_fts_outcome.message,
                                )
                            # For hybrid mode, continue with semantic only
                            search_mode_actual = "semantic"
                        finally:
                            _record_rest_fts_metric(
                                repository=fts_repo_alias
                                or request.repository_alias
                                or "unknown",
                                duration_seconds=time.monotonic() - _fts_metric_start,
                                matches_count=_fts_matches_count,
                                status=_fts_status,
                            )

                # Execute semantic search for hybrid or degraded mode
                if search_mode_actual in ["semantic", "hybrid"]:
                    # Reviewer Finding 2 (Story #1586 remediation): this
                    # hybrid-mode semantic call is a SEPARATE
                    # query_user_repositories() call site from the default/
                    # pure-semantic path below -- it needs its OWN
                    # cidx.search.* recording, mirroring that path's
                    # _search_metric_start/_record_rest_search_metric
                    # pattern exactly (search_type="hybrid").
                    _hybrid_search_metric_start = time.monotonic()
                    _hybrid_search_status = "error"
                    _hybrid_search_results_count = 0
                    try:
                        # Codex Finding #7: wire the SAME activated-repo
                        # QueryTracker refcount protection MCP search.py
                        # already has, so deactivation's bounded drain has
                        # something real to observe for THIS front door too.
                        with track_activated_repo_query(
                            getattr(app.state, "query_tracker", None),
                            activated_repo_manager,
                            current_user.username,
                            request.repository_alias,
                        ):
                            semantic_results_raw = semantic_query_manager.query_user_repositories(
                                username=current_user.username,
                                query_text=request.query_text,
                                repository_alias=request.repository_alias,
                                limit=request.limit,
                                min_score=request.min_score,
                                file_extensions=request.file_extensions,
                                # #2047: intersect with language, as FTS does.
                                language=request.language,
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
                                # #2047: the budget shared with the FTS half
                                extension_deadline=extension_deadline,
                            )
                        semantic_results_list = [
                            QueryResultItem(**result)
                            for result in semantic_results_raw["results"]
                        ]
                        _hybrid_search_status = "success"
                        _hybrid_search_results_count = len(semantic_results_list)
                    except Exception as e:
                        # One client-error rule: a client error is answered by
                        # the handlers below with its own text.
                        if classify_search_error(e).client_error:
                            raise
                        logger.error(
                            format_error_log(
                                "APP-GENERAL-035",
                                f"Semantic search failed: {e}",
                            ),
                            exc_info=True,
                        )
                        if search_mode_actual == "semantic":
                            raise
                        # Story #2117: the answer carries the FTS hits only;
                        # every repository the semantic half resolved failed
                        # (the FTS repository is recorded as searched below).
                        _resolved = _event_ctx.searched_repositories
                        if _resolved is not None:
                            _event_ctx.note_repositories(
                                searched=[],
                                failed=[
                                    alias
                                    for alias in _resolved
                                    + (_event_ctx.failed_repositories or [])
                                    if alias != fts_repo_alias
                                ],
                            )
                    finally:
                        _record_rest_search_metric(
                            search_type="hybrid",
                            repository=request.repository_alias or "unknown",
                            duration_seconds=time.monotonic()
                            - _hybrid_search_metric_start,
                            results_count=_hybrid_search_results_count,
                            status=_hybrid_search_status,
                        )

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
                    fts_dicts,
                    payload_cache=_payload_cache,
                    owner=current_user.username,
                )
                truncated_semantic = _apply_rest_semantic_truncation(
                    semantic_dicts,
                    payload_cache=_payload_cache,
                    owner=current_user.username,
                )

                # Return as JSONResponse to support truncated fields
                from fastapi.responses import JSONResponse

                # Bug #2110: record the mode that ran (a hybrid request without
                # a usable FTS index runs, and is recorded, as semantic).
                _event_ctx.search_type = search_mode_actual
                # Bug #2111: without an alias, the FTS half searched
                # fts_repo_alias -- an FTS-only search is recorded for it, and
                # in a hybrid search its FTS hits count toward its row.
                if (
                    not request.repository_alias
                    and fts_repo_alias
                    and search_mode_actual in ("fts", "hybrid")
                ):
                    _searched = _event_ctx.searched_repositories
                    _failed = _event_ctx.failed_repositories or []
                    if _searched is None:
                        _event_ctx.repo_alias = fts_repo_alias
                    elif fts_repo_alias not in _searched + _failed:
                        _event_ctx.note_repositories(
                            searched=_searched + [fts_repo_alias], failed=_failed
                        )
                _delivered = list(truncated_fts) + list(truncated_semantic)
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

            # Codex Finding #7: wire the SAME activated-repo QueryTracker
            # refcount protection MCP search.py already has.
            #
            # Story #1586 Finding 2: time this call and record cidx.search.*
            # in `finally` (success or error) -- this REST path calls
            # query_user_repositories() directly, so it needs its own
            # metric recording instead of inheriting AC1's MCP-side one.
            _search_metric_start = time.monotonic()
            _search_status = "error"
            _search_results_count = 0
            try:
                with track_activated_repo_query(
                    getattr(app.state, "query_tracker", None),
                    activated_repo_manager,
                    current_user.username,
                    request.repository_alias,
                ):
                    results = semantic_query_manager.query_user_repositories(
                        username=current_user.username,
                        query_text=request.query_text,
                        repository_alias=request.repository_alias,
                        limit=_fetch_limit,
                        min_score=request.min_score,
                        file_extensions=request.file_extensions,
                        # #2047: intersect with language, as FTS does.
                        language=request.language,
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
                _search_status = "success"
                _search_results_count = len(results.get("results") or [])
            finally:
                _record_rest_search_metric(
                    search_type="semantic",
                    repository=request.repository_alias or "unknown",
                    duration_seconds=time.monotonic() - _search_metric_start,
                    results_count=_search_results_count,
                    status=_search_status,
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
                owner=current_user.username,
            )

            # Return as JSONResponse to support truncated fields
            from fastapi.responses import JSONResponse

            _delivered = list(truncated_results)
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

        except AccessFilteringServiceUnavailableError as e:
            # Query searches only repositories the caller can access; with
            # access control unavailable that cannot be verified -- refuse.
            # An unwired server-side service is an internal failure.
            logger.error(
                format_error_log(
                    "APP-GENERAL-037",
                    f"Access control unavailable in unified search: {e}",
                ),
                exc_info=True,
            )
            raise access_control_unavailable_error(e)

        except MultiIndexQueryTimeoutError as e:
            # #2109: the search ran out of time -- never a short answer. The
            # body is a fixed message; the detail goes to the server log.
            logger.error(
                format_error_log(
                    "APP-GENERAL-037",
                    f"Search timed out in unified search: {e}",
                ),
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=public_error_message("Search timed out"),
            )

        except SearchFailedError as e:
            # #2109: the search itself failed (storage, provider or
            # configuration) -- a server-side failure, not a bad request.
            # The body is a fixed message; the detail goes to the server log.
            logger.error(
                format_error_log(
                    "APP-GENERAL-037",
                    f"Search failed in unified search: {e}",
                ),
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=public_error_message("Search failed"),
            )

        except SearchRequestError as e:
            # The request itself was rejected; its message names only the
            # caller's own input. Every other failure falls through below.
            error_message = str(e)
            raise HTTPException(
                status_code=(
                    status.HTTP_404_NOT_FOUND
                    if "not found" in error_message.lower()
                    else status.HTTP_400_BAD_REQUEST
                ),
                detail=error_message,
            )

        except Exception as e:
            logger.error(
                format_error_log(
                    "APP-GENERAL-037",
                    f"Unexpected error in unified search: {e}",
                ),
                exc_info=True,
            )
            _outcome = classify_search_error(e)
            raise HTTPException(
                status_code=(
                    status.HTTP_400_BAD_REQUEST
                    if _outcome.client_error
                    else status.HTTP_504_GATEWAY_TIMEOUT
                    if _outcome.timed_out
                    else status.HTTP_500_INTERNAL_SERVER_ERROR
                ),
                detail=_outcome.message,
            )
        finally:
            # Issue #1159: reset ctx and record the search event through the
            # shared builder. Story #2117: a completed search records its
            # delivered results (one row per repository it searched, Bug
            # #2111); a failed one records a failure; a job or a deferred
            # handoff records later (_record_on_exit False).
            _search_event_ctx.reset(_ctx_token)
            if _record_on_exit:
                _record_rest_query_event(
                    app,
                    _event_ctx,
                    _delivered,
                    _failure_class or _rest_failure_class(sys.exc_info()[1]),
                    start_time,
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
        if result["status"] == "completed":
            # Bug #2109: the caller's deferred temporal search is recorded
            # when its completed result is delivered (once).
            from code_indexer.server.services.search_event_recording import (
                record_deferred_search,
                writer_from_app,
            )

            record_deferred_search(
                getattr(raw_request.app.state, "payload_cache", None),
                writer_from_app(raw_request.app),
                job_id=job_id,
                username=current_user.username,
                result_count=len(result.get("results") or []),
            )
        return result
