"""Search handlers -- semantic search, regex search, cached content.

Domain module for search handlers. Part of the handlers package
modularization (Story #496).

Issue #1935: this module was a single 2,498-line flat file. It is now a
package with one module per domain seam (_shared, omni, memory_retrieval,
repo_search, temporal_search, code_search, regex_search, cached_content).
This ``__init__.py`` is a pure compatibility facade: every submodule
imports its real dependencies directly and calls them by bare local name;
this file only re-exports, so every existing
``from code_indexer.server.mcp.handlers.search import X`` (and
``from code_indexer.server.mcp.handlers import search; search.X``) call
site keeps working unchanged. Pure move -- zero behaviour change.

NOTE: Functions in this module were extracted verbatim from _legacy.py
(via the former flat search.py). Pre-existing method lengths and
duplication are preserved intentionally to avoid behavioral changes
during extraction. Refactoring is tracked separately.
"""

# Story #1293: WIRED correlation_id reader. The previous import
# (code_indexer.server.middleware.correlation.get_correlation_id) reads a
# ContextVar whose CorrelationContextMiddleware is NEVER registered in
# startup/app_wiring.py -- only telemetry.correlation_bridge's
# CorrelationBridgeMiddleware is -- so that reader always returned None in
# production and every search_event_log / search_embed_event row emitted
# from this module silently carried correlation_id=None.
from code_indexer.server.telemetry.correlation_bridge import (
    get_current_correlation_id as get_correlation_id,  # noqa: F401
)

# Story #1586 AC1: cidx.search.*/cidx.fts.* OTEL metrics -- no-op when
# telemetry is disabled (ApplicationMetrics.is_active gates every record_*
# call internally).
from code_indexer.server.telemetry.manager import (
    peek_telemetry_manager as peek_telemetry_manager,
)
from code_indexer.server.telemetry.metrics_instrumentation import (
    get_application_metrics as get_application_metrics,
)

import asyncio as asyncio
import functools as functools
import logging
import socket as socket
import time as time

import anyio.to_thread  # noqa: F401 -- binds `anyio` for dir(search) parity with HEAD; anyio.to_thread itself is used only by regex_search.py's own direct import

from code_indexer.server.services.deactivation_query_drain import (
    track_activated_repo_query as track_activated_repo_query,
)
from typing import (
    TYPE_CHECKING,
    Dict as Dict,
    Any as Any,
    List as List,
    Optional as Optional,
    Tuple as Tuple,
    cast as cast,
)

if TYPE_CHECKING:
    from code_indexer.server.services.search_event_log_writer import (
        SearchEventLogWriter as SearchEventLogWriter,
    )
from pathlib import Path as Path

from code_indexer.server.auth.user_manager import User as User, UserRole as UserRole
from .. import _utils as _utils
from code_indexer.server.services.temporal_snapshot_store import (
    read_temporal_snapshot as read_temporal_snapshot,
)
from code_indexer.server.services.temporal_poll_job_status import (
    poll_temporal_job_status as poll_temporal_job_status,
)
from code_indexer.server.services.temporal_live_dispatch import (
    execute_live_temporal_search as execute_live_temporal_search,
)
from code_indexer.server.services.config_service import (
    get_config_service as get_config_service,
)
from code_indexer.server.services.query_admission_gate import (
    check_query_admission as check_query_admission,
    memory_pressure_mcp_payload as memory_pressure_mcp_payload,
)
from code_indexer.server.services.api_metrics_service import (
    api_metrics_service as api_metrics_service,
)
from code_indexer.server.logging_utils import format_error_log as format_error_log
from code_indexer.server.services.search_event_context import (
    SearchEventContext as SearchEventContext,
    _search_event_ctx as _search_event_ctx,
)
from code_indexer.server.services.search_event_log_writer import (
    SearchEventRecord as SearchEventRecord,
)
from code_indexer.server.services.search_embed_event_emit import (
    emit_embed_event as emit_embed_event,
)
from code_indexer.server.mcp import reranking as _mcp_reranking  # noqa: F401
from code_indexer.server.mcp.memory_retrieval_pipeline import (
    MemoryRetrievalPipeline as MemoryRetrievalPipeline,
    MemoryRetrievalPipelineConfig as MemoryRetrievalPipelineConfig,
    _build_empty_nudge_entry as _build_empty_nudge_entry,
    _hydrate_memory_bodies as _hydrate_memory_bodies,
)
from .._utils import (
    CapBreach as CapBreach,
    cap_breach_response as cap_breach_response,
    _mcp_response as _mcp_response,
    _coerce_int as _coerce_int,
    _coerce_float as _coerce_float,
    _parse_json_string_array as _parse_json_string_array,
    _apply_payload_truncation as _apply_payload_truncation,
    _apply_fts_payload_truncation as _apply_fts_payload_truncation,
    _apply_regex_payload_truncation as _apply_regex_payload_truncation,
    _apply_temporal_payload_truncation as _apply_temporal_payload_truncation,
    _error_with_suggestions as _error_with_suggestions,
    _get_available_repos as _get_available_repos,
    _format_omni_response as _format_omni_response,
    _is_temporal_query as _is_temporal_query,
    _get_temporal_status as _get_temporal_status,
    _expand_wildcard_patterns as _expand_wildcard_patterns,
    _has_wildcard as _has_wildcard,
    _enforce_repo_count_cap as _enforce_repo_count_cap,
    _get_query_tracker as _get_query_tracker,
    _get_access_filtering_service as _get_access_filtering_service,
    _list_global_repos as _list_global_repos,
    _get_golden_repos_dir as _get_golden_repos_dir,
    _get_wiki_enabled_repos as _get_wiki_enabled_repos,
    _enrich_with_wiki_url as _enrich_with_wiki_url,
)

from ._shared import (
    _DEFAULT_OVERFETCH_MULTIPLIER as _DEFAULT_OVERFETCH_MULTIPLIER,
    _DEFAULT_SEARCH_LIMIT as _DEFAULT_SEARCH_LIMIT,
    _DEFAULT_MIN_SCORE as _DEFAULT_MIN_SCORE,
    _DEFAULT_EDIT_DISTANCE as _DEFAULT_EDIT_DISTANCE,
    _DEFAULT_SNIPPET_LINES as _DEFAULT_SNIPPET_LINES,
    _DEFAULT_REGEX_MAX_RESULTS as _DEFAULT_REGEX_MAX_RESULTS,
    _DEFAULT_REGEX_CONTEXT_LINES as _DEFAULT_REGEX_CONTEXT_LINES,
    _MAX_REGEX_MAX_RESULTS as _MAX_REGEX_MAX_RESULTS,
    _MAX_REGEX_CONTEXT_LINES as _MAX_REGEX_CONTEXT_LINES,
    _CIDX_META_DIR_NAME as _CIDX_META_DIR_NAME,
    _QUERY_LOG_TRUNCATION_LIMIT as _QUERY_LOG_TRUNCATION_LIMIT,
    _QUERY_TEXT_MAX_CODEPOINTS as _QUERY_TEXT_MAX_CODEPOINTS,
    _OMNI_LOG_MAX_ALIASES_SHOWN as _OMNI_LOG_MAX_ALIASES_SHOWN,
    _MEMORY_SEMANTIC_MODES as _MEMORY_SEMANTIC_MODES,
    _get_search_event_writer as _get_search_event_writer,
    _get_legacy as _get_legacy,
    _load_category_map as _load_category_map,
    _filter_errors_for_user as _filter_errors_for_user,
    _apply_search_truncation as _apply_search_truncation,
    _resolve_search_type as _resolve_search_type,
    _compute_effective_limit as _compute_effective_limit,
    _compute_rerank_limit as _compute_rerank_limit,
    _enrich_results_with_category as _enrich_results_with_category,
    _apply_rerank_and_filter as _apply_rerank_and_filter,
)
from .omni import (
    _empty_omni_response as _empty_omni_response,
    _build_multi_search_request as _build_multi_search_request,
    _flatten_multi_results as _flatten_multi_results,
    _aggregate_results as _aggregate_results,
    _omni_search_code as _omni_search_code,
)
from .memory_retrieval import (
    _configured_embedding_timeout_seconds as _configured_embedding_timeout_seconds,
    _compute_memory_query_vector as _compute_memory_query_vector,
    _compute_shared_query_vector as _compute_shared_query_vector,
    _run_memory_retrieval as _run_memory_retrieval,
)
from .repo_search import (
    _repo_lookup_error as _repo_lookup_error,
    _resolve_global_repo_target as _resolve_global_repo_target,
    _build_search_kwargs as _build_search_kwargs,
    _record_search_metric as _record_search_metric,
    _execute_tracked_search as _execute_tracked_search,
    _build_provider_unavailable_response as _build_provider_unavailable_response,
    _search_global_repo as _search_global_repo,
    _enrich_activated_results as _enrich_activated_results,
    _search_activated_repo as _search_activated_repo,
)
from .temporal_search import (
    _resolve_temporal_repo_path as _resolve_temporal_repo_path,
    _execute_temporal_via_live_dispatch as _execute_temporal_via_live_dispatch,
)
from .code_search import search_code as search_code
from .regex_search import (
    _omni_regex_search as _omni_regex_search,
    _validate_regex_args as _validate_regex_args,
    _build_and_enrich_matches as _build_and_enrich_matches,
    _rerank_truncate_and_filter_regex_matches as _rerank_truncate_and_filter_regex_matches,
    _execute_regex_search_impl as _execute_regex_search_impl,
    _record_fts_metric as _record_fts_metric,
    _execute_regex_search as _execute_regex_search,
    handle_regex_search as handle_regex_search,
    handle_regex_search_sync as handle_regex_search_sync,
)
from .cached_content import (
    handle_get_cached_content as handle_get_cached_content,
    handle_poll_search_job as handle_poll_search_job,
)

logger = logging.getLogger(__name__)


def _register(registry: dict) -> None:
    """Register search handlers in the HANDLER_REGISTRY."""
    registry["search_code"] = search_code
    # Story #1491 AC2: sync-dispatched (handle_regex_search_sync, defined above)
    # so protocol.py offloads the handler's synchronous work -- trigram
    # prefilter, Path.resolve fan-out, ripgrep output read + json.loads -- to
    # the executor instead of running it on the event loop.
    registry["regex_search"] = handle_regex_search_sync
    registry["poll_search_job"] = handle_poll_search_job
    registry["get_cached_content"] = handle_get_cached_content
