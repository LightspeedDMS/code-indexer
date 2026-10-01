"""Temporal (async-hybrid) search dispatch handlers.

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

from typing import Any, Dict, Optional, Tuple

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.temporal_live_dispatch import (
    execute_live_temporal_search,
)

from .. import _utils
from .._utils import (
    _coerce_int,
    _get_access_filtering_service,
    _get_query_tracker,
    _mcp_response,
)
from ._shared import _DEFAULT_SEARCH_LIMIT
from .repo_search import _resolve_global_repo_target


def _resolve_temporal_repo_path(
    repository_alias: str, user: User
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Resolve repo_path for a temporal query -- activated vs global,
    reusing the SAME resolution helpers the rest of search_code already
    uses (never reinventing alias resolution, per the locked design).

    Returns (repo_path, None) on success, (None, error_envelope) on failure.
    """
    if repository_alias.endswith("-global"):
        _repo_entry, target_path, err = _resolve_global_repo_target(
            repository_alias, user
        )
        if err is not None:
            return None, err
        return str(target_path), None

    _arm = getattr(_utils.app_module, "activated_repo_manager", None)
    if _arm is None:
        return None, _mcp_response(
            {
                "success": False,
                "error": "Repository service not available",
                "results": [],
            }
        )
    try:
        repo_path = _arm.get_activated_repo_path(user.username, repository_alias)
    except Exception as exc:
        return None, _mcp_response({"success": False, "error": str(exc), "results": []})
    return str(repo_path), None


def _execute_temporal_via_live_dispatch(
    params: Dict[str, Any],
    user: User,
    repository_alias: Any,
    handler_deadline_monotonic: Optional[float],
) -> Dict[str, Any]:
    """Story #1400: the live async-hybrid temporal dispatch entry point.

    Replaces the old fully-synchronous _execute_temporal_query call for the
    temporal branch. Builds TemporalWorkerInput, resolves repo_path,
    computes fusion_fetch_limit via the shared
    compute_temporal_fusion_fetch_limit() (temporal_fusion_limit.py --
    MCP's access-filter-aware formula, now the single implementation both
    doors call), and calls execute_live_temporal_search. Maps the result
    to either the standard unchanged success envelope (Scenario 1: fast
    completion, no job_id/status/partial_results fields) or the
    async-handoff failure envelope (Scenario 2/3: job_id + partial_results
    + continue_polling=True -- a dumb client sees only success=False with
    an error string, indistinguishable in effect from a plain timeout).
    """
    import dataclasses

    from code_indexer.services.temporal.temporal_worker_input_adapters import (
        TemporalAliasRejectedError,
        build_temporal_worker_input_from_mcp_dict,
    )
    from code_indexer.services.temporal.temporal_fusion_limit import (
        compute_temporal_fusion_fetch_limit,
    )
    from code_indexer.server.utils.config_manager import (
        TEMPORAL_RESPONSE_RESERVE_SECONDS,
    )

    requested_limit = _coerce_int(params.get("limit"), _DEFAULT_SEARCH_LIMIT)
    fusion_fetch_limit = compute_temporal_fusion_fetch_limit(
        requested_limit=requested_limit,
        rerank_query=params.get("rerank_query"),
        access_filtering_service=_get_access_filtering_service(),
        username=user.username,
        config_service=get_config_service(),
    )

    try:
        worker_input = build_temporal_worker_input_from_mcp_dict(
            {**params, "repository_alias": repository_alias},
            user.username,
            fusion_fetch_limit,
        )
    except TemporalAliasRejectedError as exc:
        return _mcp_response(
            {
                "success": False,
                "error": str(exc),
                "error_code": exc.error_code,
                "results": [],
            }
        )

    repo_path, err = _resolve_temporal_repo_path(worker_input.repository_alias, user)
    if err is not None:
        return err
    worker_input = dataclasses.replace(worker_input, repo_path=repo_path)

    bjm = getattr(_utils.app_module, "background_job_manager", None)
    _app_state = getattr(_utils.app_module.app, "state", None)
    payload_cache = getattr(_app_state, "payload_cache", None) if _app_state else None
    if bjm is None or payload_cache is None:
        return _mcp_response(
            {
                "success": False,
                "error": "Background job/payload cache service not available",
                "results": [],
            }
        )

    config_service = get_config_service()
    inline_wait_seconds = (
        config_service.get_config().search_timeouts_config.temporal_inline_wait_seconds
    )
    is_admin = hasattr(user, "role") and user.role == UserRole.ADMIN

    dispatch_result = execute_live_temporal_search(
        worker_input=worker_input,
        background_job_manager=bjm,
        payload_cache=payload_cache,
        access_filtering_service=_get_access_filtering_service(),
        is_admin=is_admin,
        inline_wait_seconds=inline_wait_seconds,
        handler_deadline_monotonic=handler_deadline_monotonic,
        response_reserve_seconds=TEMPORAL_RESPONSE_RESERVE_SECONDS,
        config_service=config_service,
        # Bug #1482: thread the real QueryTracker into the live worker so
        # it can construct a resolution-scope-safe TemporalShardResolver
        # and consult the golden-owned sister location Story #1457's
        # relocation trigger may have moved shard data to.
        query_tracker=_get_query_tracker(),
        # Bug #1533: thread the server's DI-wired ActivatedRepoManager so the
        # worker resolves golden lineage from the SHARED (PostgreSQL in
        # cluster mode) metadata store. Omitting this sends the worker back to
        # a node-local read, which on a cluster node cannot see an activation
        # made on another node -- HTTP 200 with zero temporal results.
        # Reached through the already-imported `_utils` module (the same
        # access pattern as `_utils.app_module` above).
        activated_repo_manager=_utils._get_activated_repo_manager(),
    )

    status = dispatch_result.get("status")
    if status == "completed":
        results = dispatch_result.get("results", [])
        return _mcp_response(
            {
                "success": True,
                "results": {
                    "results": results,
                    "total_results": len(results),
                    "query_metadata": {
                        "query_text": params.get("query_text", ""),
                    },
                },
            }
        )

    if status == "waiting":
        return _mcp_response(
            {
                "success": False,
                "error": (
                    f"Temporal query exceeded the {inline_wait_seconds}s "
                    "inline wait window; continuing in the background."
                ),
                "error_code": "TEMPORAL_QUERY_DEFERRED",
                "job_id": dispatch_result["job_id"],
                "results": dispatch_result.get("partial_results", []),
                "partial_results": dispatch_result.get("partial_results", []),
                "continue_polling": True,
                "unranked": True,
                "shards_completed": dispatch_result.get("shards_completed"),
                "shards_total": dispatch_result.get("shards_total"),
            }
        )

    # failed / not_found / capacity_exhausted
    return _mcp_response(
        {
            "success": False,
            "error": dispatch_result.get("error", "Temporal query failed"),
            "error_code": dispatch_result.get("error_code"),
            "job_id": dispatch_result.get("job_id"),
            "results": [],
        }
    )
