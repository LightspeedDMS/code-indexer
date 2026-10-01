"""Global and activated repository search handlers.

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
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.deactivation_query_drain import (
    track_activated_repo_query,
)
from code_indexer.server.telemetry.manager import peek_telemetry_manager
from code_indexer.server.telemetry.metrics_instrumentation import (
    get_application_metrics,
)

from .. import _utils
from .._utils import (
    _coerce_float,
    _coerce_int,
    _error_with_suggestions,
    _get_available_repos,
    _get_golden_repos_dir,
    _get_query_tracker,
    _get_wiki_enabled_repos,
    _list_global_repos,
    _mcp_response,
)
from ._shared import (
    _DEFAULT_EDIT_DISTANCE,
    _DEFAULT_MIN_SCORE,
    _DEFAULT_SEARCH_LIMIT,
    _DEFAULT_SNIPPET_LINES,
    _MEMORY_SEMANTIC_MODES,
    _apply_rerank_and_filter,
    _compute_effective_limit,
    _compute_rerank_limit,
    _enrich_results_with_category,
    _load_category_map,
)
from .memory_retrieval import _compute_shared_query_vector, _run_memory_retrieval

logger = logging.getLogger("code_indexer.server.mcp.handlers.search")


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
    from code_indexer.global_repos.alias_manager import (
        AliasManager,
        resolve_alias_or_index_path,
    )

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

    # Bug #1315: fall back to the registry's own index_path when the alias
    # pointer file is missing/unreadable, mirroring MultiSearchService's
    # omni-path resolution so direct and omni queries behave identically.
    alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
    target_path = resolve_alias_or_index_path(
        alias_manager, alias_name=repository_alias, repo_entry=repo_entry
    )
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
        # Story #1108 (S4): per-request cache bypass; web UI search never sets
        # this — it has no such control, so False is correct for that path.
        no_embedding_cache_shortcut=params.get("no_embedding_cache_shortcut", False),
        # Story #1291 AC7/AC8: explicit temporal embedder override
        temporal_embedder=params.get("temporal_embedder"),
    )


def _record_search_metric(
    params: Dict[str, Any],
    user_repos: list,
    duration_ms: int,
    results: list,
    status: str,
) -> None:
    """Record a cidx.search.* OTEL metric for one _execute_tracked_search call
    (Story #1586 AC1). No-op when telemetry is disabled (ApplicationMetrics
    early-returns internally); never raises into the search call path.
    """
    try:
        telemetry_manager = peek_telemetry_manager()
        if telemetry_manager is None:
            return
        app_metrics = get_application_metrics(telemetry_manager)
        if not app_metrics.is_active:
            return
        search_type = params.get("search_mode", "semantic")
        repository = user_repos[0]["user_alias"] if user_repos else "unknown"
        app_metrics.record_search_request(
            search_type=search_type,
            repository=repository,
            duration_seconds=duration_ms / 1000,
            results_count=len(results),
            status=status,
        )
    except Exception as e:
        logger.debug(f"Failed to record search metrics: {e}")


def _execute_tracked_search(
    params: Dict[str, Any],
    user: User,
    user_repos: list,
    limit: int,
    index_path: Optional[str] = None,
    # Bug #1804: out-param forwarded to _perform_search -- populated BEFORE
    # a total-provider-failure exception is raised, so the caller can build
    # a graceful degraded response instead of propagating the failure.
    _provider_completeness_out: Optional[Dict[str, Any]] = None,
) -> tuple:
    """Execute _perform_search with query-tracker ref counting and timing.

    Args:
        limit: must be > 0; raises ValueError otherwise.

    Returns:
        (results, execution_time_ms, timeout_occurred, effective_strategy)

    Bug #1219: _perform_search returns a 2-tuple (List[QueryResult], str) since
    Bug #1202.  We must unpack it here — mirroring the isinstance guard in
    query_user_repositories — so that callers receive List[QueryResult], not the
    raw tuple.  Backward compat: if a test patches _perform_search to return a
    plain list the plain list is passed through unchanged.
    """
    if limit <= 0:
        raise ValueError(f"limit must be > 0, got {limit}")

    query_tracker = _get_query_tracker()
    kwargs = _build_search_kwargs(params, user, user_repos, limit)
    kwargs["_provider_completeness_out"] = _provider_completeness_out
    start_time = time.time()
    timeout_occurred = False
    ref_incremented = False
    effective_strategy: str = params.get("query_strategy") or "primary_only"
    # Story #1586 AC1: defaults cover the exception-before-assignment path so
    # the metric recorded in `finally` always has a results list to measure.
    results: list = []
    search_status = "error"
    try:
        if query_tracker is not None and index_path:
            query_tracker.increment_ref(index_path)
            ref_incremented = True
        _raw = _utils.app_module.semantic_query_manager._perform_search(**kwargs)
        # Bug #1219 fix: _perform_search returns (results, effective_strategy).
        # Unpack gracefully; fall back if a test patches it to return a plain list.
        if isinstance(_raw, tuple):
            results, effective_strategy = _raw
        else:
            results = _raw
        search_status = "success"
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
        _record_search_metric(
            params, user_repos, execution_time_ms, results, search_status
        )

    return results, execution_time_ms, timeout_occurred, effective_strategy


def _build_provider_unavailable_response(
    params: Dict[str, Any], completeness_info: Dict[str, Any]
) -> Dict[str, Any]:
    """Bug #1804 / epic #485: MCP parallel search degrades gracefully when
    every dispatched embedding provider is unavailable, instead of
    surfacing a hard failure. Returns success:true with an empty result
    set and an explicit `completeness` marker (+ provider_errors) so a
    caller can distinguish "nothing matched" from "the search never ran"
    (anti-silent-failure, Rule 13) -- mirrors the AnalysisCompleteness
    pattern this codebase uses for the identical problem in X-Ray.
    """
    return _mcp_response(
        {
            "success": True,
            "results": {
                "results": [],
                "total_results": 0,
                "query_metadata": {
                    "query_text": params.get("query_text", ""),
                    "execution_time_ms": 0,
                    "repositories_searched": 0,
                    "timeout_occurred": False,
                    "completeness": completeness_info.get(
                        "completeness", "providers_unavailable"
                    ),
                    "provider_errors": completeness_info.get("provider_errors", {}),
                },
            },
        }
    )


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

    # Bug #1804: out-param populated (before raise) only when every
    # dispatched embedding provider is unavailable -- lets this path
    # degrade gracefully instead of propagating the failure (epic #485).
    _completeness: Dict[str, Any] = {}
    try:
        # Bug #1219 fix: _execute_tracked_search now returns a 4-tuple
        # (results, execution_time_ms, timeout_occurred, effective_strategy).
        results, execution_time_ms, timeout_occurred, effective_strategy = (
            _execute_tracked_search(
                params,
                user,
                mock_user_repos,
                effective_limit,
                index_path=target_path,
                _provider_completeness_out=_completeness,
            )
        )
    except Exception:
        if _completeness.get("completeness") == "providers_unavailable":
            return _build_provider_unavailable_response(params, _completeness)
        raise

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
                    "query_text": params.get("query_text", ""),
                    "execution_time_ms": execution_time_ms,
                    "repositories_searched": 1,
                    "timeout_occurred": timeout_occurred,
                    "reranker_used": rerank_meta["reranker_used"],
                    "reranker_provider": rerank_meta["reranker_provider"],
                    "rerank_time_ms": rerank_meta["rerank_time_ms"],
                    # AC7 (Bug #1202 / Bug #1219): echo effective routing decision
                    # on the global-repo path, matching activated-repo parity.
                    "effective_search_mode": params.get("search_mode", "semantic"),
                    "effective_query_strategy": effective_strategy,
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
            # Story #1108 (S4): thread per-request cache bypass into the
            # shared embedding call so both code search and memory retrieval
            # honour the bypass flag without a second Voyage API call.
            # Defect #1148 fix: _compute_shared_query_vector now returns (vector, digest).
            # The activated-repo path is single-repo (no mixed-config fan-out), so the
            # digest is not needed here — only the vector is extracted.
            _shared_vec, _ = _compute_shared_query_vector(
                str(query_text),
                no_embedding_cache_shortcut=params.get(
                    "no_embedding_cache_shortcut", False
                ),
            )
            shared_query_vector = _shared_vec if _shared_vec else None

    kwargs = _build_search_kwargs(params, user, [], effective_limit)
    # query_user_repositories uses repository_alias, not user_repos
    del kwargs["user_repos"]
    kwargs["repository_alias"] = params.get("repository_alias")
    kwargs["precomputed_query_vector"] = shared_query_vector
    # Bug #1804: out-param populated (before raise) only when every
    # dispatched embedding provider is unavailable -- lets this path
    # degrade gracefully instead of propagating the failure (epic #485).
    _completeness: Dict[str, Any] = {}
    kwargs["_provider_completeness_out"] = _completeness

    # Story #1458 AC13 gap (b): wire QueryTracker ref-counting around this
    # read, using the SAME shared track_activated_repo_query() helper the
    # REST/wiki front doors use (Codex HIGH finding, round 2 -- this call
    # site previously duplicated the key-construction + increment/
    # decrement logic instead of reusing the shared helper). Fail-open (no
    # tracker, no repository_alias e.g. omni queries, or a -global alias --
    # golden-repo queries are a separate, already-covered concern) preserves
    # today's behavior exactly.
    try:
        with track_activated_repo_query(
            _get_query_tracker(),
            getattr(_utils.app_module, "activated_repo_manager", None),
            user.username,
            params.get("repository_alias"),
        ):
            result = _utils.app_module.semantic_query_manager.query_user_repositories(
                **kwargs
            )
    except Exception:
        if _completeness.get("completeness") == "providers_unavailable":
            return _build_provider_unavailable_response(params, _completeness)
        raise

    # Touch last_accessed for the activated repo (throttled, non-fatal).
    # Fixes Bug #1098 defect 2: search path never stamped last_accessed,
    # so search-only users were reaped while actively using their repos.
    # Global repos (ending with -global) have no user last_accessed TTL.
    _arm = getattr(_utils.app_module, "activated_repo_manager", None)
    if _arm is not None:
        _repo_alias = params.get("repository_alias")
        if _repo_alias and not str(_repo_alias).endswith("-global"):
            try:
                _arm.touch_last_accessed(user.username, str(_repo_alias))
            except Exception as _exc:
                logger.debug(
                    "touch_last_accessed failed for user=%s repo=%s (non-fatal): %s",
                    user.username,
                    _repo_alias,
                    _exc,
                )

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
        # AC4 (Bug #1202): propagate fts truncation metadata from rerank_meta into qm.
        # _apply_rerank_and_filter merges preview_size_chars / rows_capped from
        # _apply_search_truncation into rerank_meta; they must be forwarded here
        # so the single-repo path matches the omni path's query_metadata coverage.
        for _ac4_key in ("preview_size_chars", "rows_capped"):
            if _ac4_key in rerank_meta:
                qm[_ac4_key] = rerank_meta[_ac4_key]
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
        # AC7 (Bug #1202): effective_search_mode and effective_query_strategy are
        # already in qm — they arrive via QueryMetadata.to_dict() in the
        # per-request result dict.  No singleton read-back needed here.

    return _mcp_response({"success": True, "results": result})
