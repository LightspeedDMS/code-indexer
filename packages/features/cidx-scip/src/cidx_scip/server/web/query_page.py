"""
The SCIP search mode of the Web UI query page (``web_query_modes()``).

The core's query page (``web/pages/query.py``) resolves the repository the
user picked and hands a ``search_mode=scip`` request to :func:`execute`,
which runs the query and records its search event (Story #2115). Moved out
of the core in modular phase 4d; it had moved out of ``web/routes.py`` in
phase 6.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from cidx_sdk import WebQueryOutcome, WebQueryRequest

from cidx_scip._platform import server_platform
from cidx_scip.server._core import format_error_log, get_correlation_id

logger = logging.getLogger(__name__)

#: The ``search_mode`` value of this mode.
MODE = "scip"


# The SCIP query types the Web query page executes (both SCIP paths).
_WEB_SCIP_QUERY_TYPES = (
    "definition",
    "references",
    "dependencies",
    "dependents",
    "impact",
    "callchain",
    "context",
)


def _record_web_scip_search(
    request: Any,
    username: str,
    repository: str,
    query_text: str,
    search_mode: str,
    scip_query_type: str,
    error_message: Optional[str],
    result_count: int,
    started: float,
    failure: Optional[BaseException],
) -> None:
    """Story #2115: record a SCIP-mode Web query as "scip".

    Nothing is recorded for other modes (recorded by _execute_text_query),
    for an unrecognized query type (no query ran), or when the request was
    refused before a search ran (an error message and no `failure`). A
    query that ran and raised (`failure`, Story #2117) is a failure row,
    classified like MCP and REST SCIP. `repository` is the form value
    "alias (owner)".
    """
    if search_mode != MODE or scip_query_type not in _WEB_SCIP_QUERY_TYPES:
        return
    search_event_recording = server_platform().search_events_module()
    if error_message is not None and failure is None:
        return
    recorded_text = query_text.strip()
    symbols = recorded_text.split(maxsplit=1)
    if scip_query_type == "callchain" and len(symbols) == 2:
        # Same text as MCP and REST.
        recorded_text = search_event_recording.scip_callchain_query_text(*symbols)
    writer = search_event_recording.writer_from_app(request.app)
    origin = search_event_recording.SearchOrigin(tool="web:query", door="web")
    repo_alias = repository.split(" (")[0]
    if failure is not None:
        search_event_recording.record_scip_search(
            writer,
            {"success": False},
            origin=origin,
            username=username,
            repo_alias=repo_alias,
            query_text=recorded_text,
            started_monotonic=started,
            correlation_id=get_correlation_id(),
            error=failure,
        )
        return
    search_event_recording.record_completed_search(
        writer,
        origin=origin,
        search_type="scip",
        username=username,
        repo_alias=repo_alias,
        query_text=recorded_text,
        result_count=result_count,
        started_monotonic=started,
        correlation_id=get_correlation_id(),
    )


def _execute_scip_query(
    repo_path: str,
    user_alias: str,
    query_text: str,
    scip_query_type: str,
    scip_exact: bool,
    limit: int,
    min_score: str,
) -> tuple[list, Optional[str], Optional[BaseException]]:
    """
    Execute SCIP query for repository. Supports all 7 SCIP query types.

    Returns tuple of (results_list, error_message, failure): `failure` is the
    exception a query that ran failed with (Story #2117), None when it
    completed or was refused before running.
    """
    import glob

    from cidx_scip.database.queries import QueryTimeoutError
    from cidx_scip.query.primitives import QueryResult, SCIPQueryEngine

    results: List[Dict[str, Any]] = []
    scip_pattern = str(Path(repo_path) / ".code-indexer" / "scip" / "**" / "*.scip")
    scip_files = glob.glob(scip_pattern, recursive=True)
    if not scip_files:
        return (
            results,
            f"No SCIP index found for repository '{user_alias}'. Run 'cidx scip index' first.",
            None,
        )

    try:
        query_results = []
        scip_dir = Path(repo_path) / ".code-indexer" / "scip"

        if scip_query_type == "impact":
            from cidx_scip.query.composites import analyze_impact

            res = analyze_impact(
                query_text.strip(), scip_dir, depth=2, project=str(repo_path)
            )
            query_results = [
                QueryResult(
                    symbol=a.symbol,
                    project=str(repo_path),
                    file_path=str(a.file_path),
                    line=a.line,
                    column=a.column,
                    kind="impact",
                    relationship=a.relationship,
                    context=None,
                )
                for a in res.affected_symbols
            ]
        elif scip_query_type == "callchain":
            parts = query_text.strip().split(maxsplit=1)
            if len(parts) != 2:
                # A refused request, not a failed search.
                raise server_platform().search_request_error()(
                    "Call chain requires two symbols: 'from_symbol to_symbol'"
                )
            engine = SCIPQueryEngine(Path(scip_files[0]))
            from cidx_scip.database.queries import MAX_DEPTH_CAP

            timeout_errors: List[str] = []
            chains = engine.trace_call_chain(
                parts[0],
                parts[1],
                max_depth=MAX_DEPTH_CAP,
                timeout_errors=timeout_errors,
            )
            if timeout_errors:
                # Bug #1603 code review round 4 Priority 1: a timeout must
                # never be reported as the indistinguishable empty-results
                # success rendered below.
                raise QueryTimeoutError(
                    f"Callchain query timed out: {timeout_errors[0]}"
                )
            query_results = [
                QueryResult(
                    symbol=" -> ".join(c.path),
                    project=str(repo_path),
                    file_path="(call chain)",
                    line=0,
                    column=0,
                    kind="callchain",
                    relationship=f"length={c.length}",
                    context=None,
                )
                for c in chains
            ]
        elif scip_query_type == "context":
            from cidx_scip.query.composites import get_smart_context

            context_res = get_smart_context(
                query_text.strip(),
                scip_dir,
                limit=limit,
                min_score=float(min_score) if min_score else 0.0,
                project=str(repo_path),
            )
            query_results = [
                QueryResult(
                    symbol=query_text.strip(),
                    project=str(repo_path),
                    file_path=str(f.path),
                    line=0,
                    column=0,
                    kind="context",
                    relationship=f"score={f.relevance_score:.2f}, symbols={len(f.symbols)}",
                    context=None,
                )
                for f in context_res.files
            ]
        else:
            engine = SCIPQueryEngine(Path(scip_files[0]))
            if scip_query_type == "definition":
                query_results = engine.find_definition(
                    query_text.strip(), exact=scip_exact
                )
            elif scip_query_type == "references":
                query_results = engine.find_references(
                    query_text.strip(), limit=limit, exact=scip_exact
                )
            elif scip_query_type == "dependencies":
                query_results = engine.get_dependencies(
                    query_text.strip(), exact=scip_exact
                )
            elif scip_query_type == "dependents":
                query_results = engine.get_dependents(
                    query_text.strip(), exact=scip_exact
                )

        # Format results for template
        for result in query_results:
            results.append(
                {
                    "file_path": result.file_path,
                    "line_numbers": str(result.line),
                    "content": f"{result.kind}: {result.symbol}",
                    "score": 1.0,  # SCIP results don't have similarity scores
                    "repository_alias": user_alias,
                    "scip_symbol": result.symbol,
                    "scip_kind": result.kind,
                }
            )
    except QueryTimeoutError as e:
        logger.warning(
            format_error_log(
                "STORE-GENERAL-052", f"SCIP callchain query timed out: {e}"
            ),
            extra={"correlation_id": get_correlation_id()},
        )
        return (
            results,
            f"Callchain query timed out for repository '{user_alias}': {e}. Try a smaller max-depth or narrower symbols.",
            e,
        )
    except FileNotFoundError as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-038", f"SCIP query failed - file not found: {e}"
            ),
            exc_info=True,
            extra={"correlation_id": get_correlation_id()},
        )
        return (
            results,
            f"SCIP index not found or corrupted for repository '{user_alias}'. Generate an index with: `cidx scip generate`",
            e,
        )
    except Exception as e:
        logger.error(
            format_error_log("STORE-GENERAL-039", f"SCIP query execution failed: {e}"),
            exc_info=True,
            extra={"correlation_id": get_correlation_id()},
        )
        return (
            results,
            f"SCIP query failed for repository '{user_alias}': {str(e)}. Try regenerating the index with: `cidx scip generate`",
            e,
        )

    return results, None, None


def execute(request: WebQueryRequest) -> WebQueryOutcome:
    """Run one SCIP-mode query of the Web UI query page and record it
    (``WebQueryModeContribution.execute``). Never raises: a refused request
    and a failed query are an outcome with the message the page shows."""
    import time

    started = time.monotonic()
    options = request.options
    scip_query_type = str(options.get("scip_query_type") or "definition")
    scip_exact = bool(options.get("scip_exact"))
    failure: Optional[BaseException] = None
    try:
        rows, error, failure = _execute_scip_query(
            request.repository_path,
            request.repository_alias,
            request.query_text,
            scip_query_type,
            scip_exact,
            request.limit,
            request.min_score,
        )
    except Exception as exc:  # the page shows it; a refusal records nothing
        logger.warning(
            format_error_log("STORE-GENERAL-035", f"SCIP web query refused: {exc}"),
            extra={"correlation_id": get_correlation_id()},
        )
        rows, error = [], f"Query failed: {exc}"
    _record_web_scip_search(
        request.http_request,
        request.username,
        request.repository_alias,
        request.query_text,
        MODE,
        scip_query_type,
        error,
        len(rows),
        started,
        failure,
    )
    return WebQueryOutcome(rows=rows, error=error)
