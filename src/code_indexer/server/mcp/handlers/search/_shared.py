"""Shared constants and low-level helpers for the search handlers package.

Domain module for search handlers. Part of the handlers package
modularization (Story #496).

Issue #1935 Part: this module was split out of the former flat
search.py (2,498 lines) into this package, one module per domain seam,
each < 1,000 lines. Pure move -- zero behaviour change.

NOTE: Functions in this module were extracted verbatim from _legacy.py
(via search.py). Pre-existing method lengths and duplication are
preserved intentionally to avoid behavioral changes during extraction.
Refactoring is tracked separately.
"""

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from code_indexer.server.auth.user_manager import User
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.mcp import reranking as _mcp_reranking
from code_indexer.server.services.api_metrics_service import api_metrics_service
from code_indexer.server.services.config_service import get_config_service

from .. import _utils
from .._utils import (
    _apply_fts_payload_truncation,
    _apply_payload_truncation,
    _apply_temporal_payload_truncation,
    _enrich_with_wiki_url,
    _get_access_filtering_service,
    _is_temporal_query,
)

if TYPE_CHECKING:
    from code_indexer.server.services.search_event_log_writer import (
        SearchEventLogWriter,
    )

logger = logging.getLogger("code_indexer.server.mcp.handlers.search")

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
# Issue #1601 remediation round 5 (Priority 1): matches the documented
# inputSchema.max_results.maximum in
# server/mcp/tool_docs/search/regex_search.md. Previously only the LOWER
# bound was clamped (max(1, ...)); an absurdly large caller-supplied
# max_results was passed straight through to RegexSearchService.search()
# unclamped, defeating any per-request memory budget the documented
# ceiling implies (including the aggregate result-content budget added
# in this same remediation round).
_MAX_REGEX_MAX_RESULTS = 1000
# Issue #1601 remediation round 5 (Codex finding, other half of the
# multiplicand): matches the documented inputSchema.context_lines.maximum
# in server/mcp/tool_docs/search/regex_search.md, and mirrors the REST
# route's Field(default=0, ge=0, le=10) in regex_routes.py. Previously
# only the LOWER bound was clamped (max(0, ...)); an absurdly large
# caller-supplied context_lines was passed straight through to
# RegexSearchService.search(), wasting subprocess I/O/CPU on `rg -C
# <huge>` even though the aggregate result-content budget (this same
# remediation round) still bounds the resulting memory.
_MAX_REGEX_CONTEXT_LINES = 10
# Filesystem subdirectory name for the memory HNSW index (Story #883).
_CIDX_META_DIR_NAME = "cidx-meta"

# Bug #881 Phase 1: query_text is truncated to this length in INFO logs to avoid
# logging potentially large or sensitive query strings at INFO level.
_QUERY_LOG_TRUNCATION_LIMIT = 100

# Issue #1159 spec A8: query_text stored in SearchEventRecord is capped at 500 codepoints.
_QUERY_TEXT_MAX_CODEPOINTS = 500

# Bug #881 Phase 1: max number of expanded aliases shown in the omni post-expansion log.
_OMNI_LOG_MAX_ALIASES_SHOWN = 10

# Modes that trigger memory retrieval alongside code search (Story #883).
_MEMORY_SEMANTIC_MODES = frozenset({"semantic", "hybrid"})


def _get_search_event_writer() -> "Optional[SearchEventLogWriter]":
    """Return the SearchEventLogWriter from app state, or None if unavailable.

    Issue #1159: reads app_module.app.state.search_event_log_writer so tests
    can monkeypatch this function directly without touching app state.
    Uses stepwise getattr so attribute-access errors on missing state do not
    propagate and do not silence unrelated failures.
    """
    state = getattr(getattr(_utils.app_module, "app", None), "state", None)
    if state is None:
        return None
    return getattr(state, "search_event_log_writer", None)


def _get_legacy():
    from .. import _legacy

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
) -> Tuple[list, Dict[str, Any]]:
    """Apply payload truncation based on search mode.

    Returns:
        (results, meta) 2-tuple.  For fts/hybrid modes, meta contains:
          - preview_size_chars: int  -- threshold from payload_cache config
          - rows_capped: int         -- rows where at least one field was truncated
        For non-fts/hybrid modes, meta is an empty dict {}.
        When payload_cache is not configured, meta is an empty dict for fts/hybrid too.

    AC4 (Bug #1202): meta is injected into query_metadata by callers so the
    MCP consumer can see how many rows were truncated and at what threshold.
    """
    if search_mode in ["fts", "hybrid"]:
        truncated = _apply_fts_payload_truncation(results)
        # Build meta only when a payload_cache is active.  _apply_fts_payload_truncation
        # returns the original list unchanged when there is no cache, so rows_capped
        # would be 0 anyway -- return empty meta to avoid surfacing a misleading 0.
        payload_cache = getattr(
            getattr(_utils.app_module.app, "state", None), "payload_cache", None
        )
        if payload_cache is None:
            logger.debug(
                "AC4 fts truncation meta: payload_cache not configured, skipping meta"
            )
            return truncated, {}
        rows_capped = sum(
            1
            for r in truncated
            if r.get("snippet_has_more") or r.get("match_text_has_more")
        )
        meta: Dict[str, Any] = {
            "preview_size_chars": payload_cache.config.preview_size_chars,
            "rows_capped": rows_capped,
        }
        return truncated, meta
    elif _is_temporal_query(params):
        return _apply_temporal_payload_truncation(results), {}
    else:
        return _apply_payload_truncation(results), {}


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

    Returns (filtered_results, rerank_meta) where rerank_meta is the dict from
    _apply_reranking_sync, optionally enriched with AC4 fts truncation keys
    (preview_size_chars, rows_capped) when search_mode is fts or hybrid.
    """
    rerank_query = params.get("rerank_query")
    rerank_instruction = params.get("rerank_instruction")
    results, rerank_meta = _mcp_reranking._apply_reranking_sync(
        results=results,
        rerank_query=rerank_query,
        rerank_instruction=rerank_instruction,
        content_extractor=_mcp_reranking.extract_rerank_document,
        requested_limit=requested_limit,
        config_service=get_config_service(),
    )

    search_mode = params.get("search_mode", "semantic")
    results, fts_truncation_meta = _apply_search_truncation(
        results, search_mode, params
    )
    # AC4 (Bug #1202): surface fts truncation metadata alongside rerank meta.
    if fts_truncation_meta:
        rerank_meta.update(fts_truncation_meta)

    access_filtering_service = _get_access_filtering_service()
    if access_filtering_service:
        results = access_filtering_service.filter_query_results(results, user.username)
        if repository_alias and "cidx-meta" in repository_alias:
            results = access_filtering_service.filter_cidx_meta_results(
                results, user.username
            )
        results = results[:requested_limit]

    return results, rerank_meta
