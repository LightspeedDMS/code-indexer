"""search_code MCP tool entry point -- routes to omni/global/activated/temporal.

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
import socket
import time
from typing import Any, Dict, Optional

from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.query_admission_gate import (
    check_query_admission,
    memory_pressure_mcp_payload,
)
from code_indexer.server.services.search_event_context import (
    SearchEventContext,
    _search_event_ctx,
)
from code_indexer.server.services.search_event_log_writer import SearchEventRecord
from code_indexer.server.telemetry.correlation_bridge import (
    get_current_correlation_id as get_correlation_id,
)

from .. import _utils
from .._utils import (
    _has_wildcard,
    _is_temporal_query,
    _mcp_response,
    _parse_json_string_array,
)
from ._shared import (
    _QUERY_LOG_TRUNCATION_LIMIT,
    _QUERY_TEXT_MAX_CODEPOINTS,
    _get_search_event_writer,
)
from .omni import _omni_search_code
from .repo_search import _search_activated_repo, _search_global_repo
from .temporal_search import _execute_temporal_via_live_dispatch

logger = logging.getLogger("code_indexer.server.mcp.handlers.search")


def search_code(
    params: Dict[str, Any],
    user: User,
    handler_deadline_monotonic: Optional[float] = None,
) -> Dict[str, Any]:
    """Search code using semantic search, FTS, or hybrid mode.

    Routes to the appropriate handler based on repository_alias type:
    - list: _omni_search_code (multi-repo)
    - str ending with -global: _search_global_repo
    - str (other): _search_activated_repo

    Issue #1159: installs SearchEventContext ContextVar before routing so that
    embedding call sites can write cache metadata into it.  Enqueues a
    SearchEventRecord on success only (spec H11).  Resets ContextVar in finally.
    """
    _admission = check_query_admission()
    if not _admission.allowed:
        return _mcp_response(memory_pressure_mcp_payload(_admission))

    import json as _json_mod

    _search_start = time.monotonic()

    # Issue #1159: capture context fields before any param mutation below.
    _query_text_raw = str(params.get("query_text", "") or "")
    _repo_alias_raw = params.get("repository_alias")
    _repo_alias_str = str(_repo_alias_raw) if isinstance(_repo_alias_raw, str) else None
    _search_mode = str(params.get("search_mode", "semantic") or "semantic")
    _event_ctx = SearchEventContext(
        username=user.username,
        repo_alias=_repo_alias_str,
        query_text=_query_text_raw[:_QUERY_TEXT_MAX_CODEPOINTS],
        search_type=_search_mode,
    )
    _ctx_token = _search_event_ctx.set(_event_ctx)

    try:
        repository_alias = params.get("repository_alias")
        repository_alias = _parse_json_string_array(repository_alias)
        params["repository_alias"] = repository_alias

        # Bug #881 Phase 1 entry log: operators can audit every search_code call.
        # py-spy logging-lock fix (follow-up to Bug #1078): demoted INFO -> DEBUG
        # so this per-query audit line does not acquire the logging handler lock
        # on the hot path at default levels. Re-enable via DEBUG to audit calls.
        _query_log = str(params.get("query_text", ""))[:_QUERY_LOG_TRUNCATION_LIMIT]
        logger.debug(
            f"search_code entry: user={user.username!r} "
            f"correlation_id={get_correlation_id()!r} "
            f"repository_alias={repository_alias!r} "
            f"limit={params.get('limit')!r} "
            f"accuracy={params.get('accuracy')!r} "
            f"query_text={_query_log!r}",
            extra={"correlation_id": get_correlation_id()},
        )

        # Bug #1029: validate query_text before routing to sub-handlers.
        # Hard dict access in _build_search_kwargs causes KeyError when missing.
        _query_text = params.get("query_text")
        if not isinstance(_query_text, str) or not _query_text.strip():
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: query_text",
                    "results": [],
                }
            )

        # Story #1039: bare-to-global alias fallback for read-only handlers.
        # Applied BEFORE routing so the if/elif/else below sees the promoted alias.
        if (
            isinstance(repository_alias, str)
            and repository_alias
            and not repository_alias.endswith("-global")
        ):
            _arm = getattr(_utils.app_module, "activated_repo_manager", None)
            _grm = getattr(_utils.app_module, "golden_repo_manager", None)
            if _arm is not None and _grm is not None:
                if not _arm.user_has_activated_repo(user.username, repository_alias):
                    from .._global_fallback import try_global_fallback

                    _promoted = try_global_fallback(repository_alias, _grm)
                    if _promoted is not None:
                        logger.info(
                            "bare-alias fallback: %r -> %r for user %r",
                            repository_alias,
                            _promoted,
                            user.username,
                        )
                        params["repository_alias"] = _promoted
                        repository_alias = _promoted

        # Story #1400: async-hybrid temporal query execution. Intercept
        # AFTER the Story #1039 alias promotion above (so a bare alias that
        # was promoted to its -global form is what temporal sees) but
        # BEFORE the wildcard/list routing below -- temporal queries bypass
        # the old fully-synchronous _execute_temporal_query path entirely
        # via the async-hybrid worker/dedup/poll machinery. Alias
        # validation (missing/list-typed) is handled inside the dispatch
        # function itself via the shared adapter.
        if _is_temporal_query(params):
            return _execute_temporal_via_live_dispatch(
                params, user, repository_alias, handler_deadline_monotonic
            )

        # Bug #1119: a wildcard string like "*" or "fastapi-?" must be routed to the
        # omni/expansion path, not to _search_activated_repo. Wrap the single wildcard
        # string as a one-element list so the routing below sends it to _omni_search_code
        # where _expand_wildcard_patterns can expand it properly.
        if (
            isinstance(repository_alias, str)
            and repository_alias
            and _has_wildcard(repository_alias)
        ):
            repository_alias = [repository_alias]
            params["repository_alias"] = repository_alias

        if isinstance(repository_alias, list):
            _result = _omni_search_code(params, user)
        elif repository_alias and repository_alias.endswith("-global"):
            _result = _search_global_repo(params, user, repository_alias)
        else:
            _result = _search_activated_repo(params, user)

        # Bug #881 Phase 1 exit log: elapsed_ms and result_count for every call.
        # py-spy logging-lock fix (follow-up to Bug #1078): demoted INFO -> DEBUG
        # so this per-query audit line does not acquire the logging handler lock
        # on the hot path at default levels.
        _elapsed_ms = int((time.monotonic() - _search_start) * 1000)
        logger.debug(
            f"search_code complete: correlation_id={get_correlation_id()!r} "
            f"result_count=0 elapsed_ms={_elapsed_ms}ms",
            extra={"correlation_id": get_correlation_id()},
        )

        # Issue #1159: enqueue SearchEventRecord on success only (spec H11).
        _writer = _get_search_event_writer()
        if _writer is not None:
            # Extract result count from the MCP-wrapped response (content[0].text JSON).
            # Uses narrow type checks and logs on parse failure — no broad exception swallow.
            _result_count = 0
            _content_list = (
                _result.get("content") if isinstance(_result, dict) else None
            )
            if isinstance(_content_list, list) and _content_list:
                _first = _content_list[0]
                _text = _first.get("text") if isinstance(_first, dict) else None
                if isinstance(_text, str):
                    try:
                        _inner = _json_mod.loads(_text)
                    except _json_mod.JSONDecodeError as _jde:
                        logger.debug(
                            "search_code: could not parse MCP response text for telemetry: %s",
                            _jde,
                        )
                        _inner = {}
                    _results_payload = (
                        _inner.get("results") if isinstance(_inner, dict) else None
                    )
                    # _results_payload may be a dict (activated-repo wraps results in outer
                    # dict) or a list (omni / global paths).
                    if isinstance(_results_payload, dict):
                        _results_payload = _results_payload.get("results")
                    if isinstance(_results_payload, list):
                        _result_count = len(_results_payload)

            # Stepwise node_id lookup — never raises (getattr with defaults).
            _cfg_svc = get_config_service()
            _cfg_get = getattr(_cfg_svc, "get_config", None)
            _cfg_obj = _cfg_get() if callable(_cfg_get) else None
            _node_id = str(getattr(_cfg_obj, "node_id", "") or "")
            if not _node_id:
                try:
                    _node_id = socket.gethostname()
                except OSError as _hn_exc:
                    logger.debug(
                        "search_code: socket.gethostname() failed, using 'unknown': %s",
                        _hn_exc,
                    )
                    _node_id = "unknown"

            _record = SearchEventRecord(
                timestamp=time.time(),
                username=user.username,
                repo_alias=_event_ctx.repo_alias,
                search_type=_event_ctx.search_type,
                query_text=_event_ctx.query_text,
                voyage_cache_hit=_event_ctx.voyage_cache_hit,
                voyage_cache_mode=_event_ctx.voyage_cache_mode,
                voyage_latency_ms=_event_ctx.voyage_latency_ms,
                cohere_cache_hit=_event_ctx.cohere_cache_hit,
                cohere_cache_mode=_event_ctx.cohere_cache_mode,
                cohere_latency_ms=_event_ctx.cohere_latency_ms,
                total_latency_ms=_elapsed_ms,
                result_count=_result_count,
                node_id=_node_id,
                correlation_id=get_correlation_id(),
            )
            _writer.enqueue(_record)

        return _result
    except Exception as e:
        logger.exception(
            f"Error in search_code: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e), "results": []})
    finally:
        # Issue #1159: always reset ContextVar so it never leaks into the next request.
        _search_event_ctx.reset(_ctx_token)
