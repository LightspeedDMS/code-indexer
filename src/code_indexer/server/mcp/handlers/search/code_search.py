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

import json
import logging
import time
from typing import Any, Dict, Optional

from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.search_event_recording import (
    elapsed_ms,
    record_search_payload,
    remember_deferred_search,
)
from code_indexer.server.services.query_admission_gate import (
    check_query_admission,
    memory_pressure_mcp_payload,
)
from code_indexer.server.services.search_event_context import (
    SearchEventContext,
    _search_event_ctx,
)
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
    _get_search_event_writer,
)
from .omni import _omni_search_code
from .repo_search import _search_activated_repo, _search_global_repo
from .temporal_search import _execute_temporal_via_live_dispatch

logger = logging.getLogger("code_indexer.server.mcp.handlers.search")


def _mcp_payload(result: Any) -> Dict[str, Any]:
    """The JSON body of an MCP-wrapped response (content[0].text), or {}."""
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list) or not content:
        return {}
    first = content[0]
    text = first.get("text") if isinstance(first, dict) else None
    if not isinstance(text, str):
        return {}
    try:
        inner = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.debug("search_code: could not parse MCP response text: %s", exc)
        return {}
    return inner if isinstance(inner, dict) else {}


def _invalid_search_mode_response(search_mode: str) -> Optional[Dict[str, Any]]:
    """Bug #2110: the rejection of a search_mode REST would answer 422 for.

    None when `search_mode` is valid. An unknown mode is never searched or
    recorded under a value no Search Type filter can select.
    """
    from code_indexer.server.models.query import SEARCH_MODES

    if search_mode in SEARCH_MODES:
        return None
    allowed = ", ".join(SEARCH_MODES)
    message = f"Invalid search_mode {search_mode!r}: must be one of {allowed}"
    return _mcp_response({"success": False, "error": message, "results": []})


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
    embedding call sites can write cache metadata into it. Resets ContextVar
    in finally. Bug #2113 recording rule: one search event per successful
    search (response payload success true; a temporal search is recorded as
    "temporal"); a failure, handled error, rejected request or deferred
    temporal handoff records nothing.
    """
    _admission = check_query_admission()
    if not _admission.allowed:
        return _mcp_response(memory_pressure_mcp_payload(_admission))

    _search_start = time.monotonic()
    # Bug #2109: wall-clock start, for a deferred search recorded on any node.
    _search_started_at = time.time()

    # Issue #1159: capture context fields before any param mutation below.
    _query_text_raw = str(params.get("query_text", "") or "")
    _repo_alias_raw = params.get("repository_alias")
    _repo_alias_str = str(_repo_alias_raw) if isinstance(_repo_alias_raw, str) else None
    _search_mode = str(params.get("search_mode", "semantic") or "semantic")
    _event_ctx = SearchEventContext(
        username=user.username,
        repo_alias=_repo_alias_str,
        # Full text: the shared recording module caps and marks it (#2114).
        query_text=_query_text_raw,
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

        # Bug #2110: reject an unknown search_mode before any search runs.
        _mode_rejection = _invalid_search_mode_response(_search_mode)
        if _mode_rejection is not None:
            return _mode_rejection

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
            _temporal_result = _execute_temporal_via_live_dispatch(
                params, user, repository_alias, handler_deadline_monotonic
            )
            # Bug #2109: a completed temporal search is recorded as
            # "temporal". A deferred handoff (job still running) records
            # nothing now; it leaves a pending record that poll_search_job
            # records when it delivers the completed result.
            _temporal_payload = _mcp_payload(_temporal_result)
            record_search_payload(
                _get_search_event_writer(),
                _temporal_payload,
                search_type="temporal",
                context=_event_ctx,
                total_latency_ms=elapsed_ms(_search_start),
                correlation_id=get_correlation_id(),
            )
            remember_deferred_search(
                getattr(
                    getattr(getattr(_utils.app_module, "app", None), "state", None),
                    "payload_cache",
                    None,
                ),
                _temporal_payload,
                context=_event_ctx,
                correlation_id=get_correlation_id(),
                started_at=_search_started_at,
            )
            return _temporal_result

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

        # Bug #2113: only a search whose result is known is recorded
        # (search_payload_is_known): a handled error, a search no provider
        # could run and an omni search where every repository failed record
        # nothing (exceptions, a missing query_text, an unknown search_mode
        # and admission refusals return before this point).
        record_search_payload(
            _get_search_event_writer(),
            _mcp_payload(_result),
            search_type=_event_ctx.search_type,
            context=_event_ctx,
            total_latency_ms=_elapsed_ms,
            correlation_id=get_correlation_id(),
        )

        return _result
    except Exception as e:
        from code_indexer.server.query.search_error_policy import (
            classify_search_error,
        )

        outcome = classify_search_error(e)
        # A request the caller can fix is not a server fault: WARNING, no
        # traceback (as REST /api/query). Everything else keeps ERROR.
        logger.log(
            logging.ERROR if outcome.log_as_internal else logging.WARNING,
            f"Error in search_code: {e}",
            exc_info=outcome.log_as_internal,
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response(
            {"success": False, "error": outcome.message, "results": []}
        )
    finally:
        # Issue #1159: always reset ContextVar so it never leaks into the next request.
        _search_event_ctx.reset(_ctx_token)
