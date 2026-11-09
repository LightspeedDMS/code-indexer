"""
Web query (search) page and its result partials.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

from code_indexer.server.services.served_index_cache import reads_served_indexes
from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.services import search_event_recording
from typing import Any, Dict, List, Optional, Tuple, cast
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from ..auth import SessionData
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.features.query_modes import (
    CORE_QUERY_MODES,
    feature_query_mode,
    feature_query_modes,
)
from cidx_sdk import WebQueryModeContribution, WebQueryRequest

from ._shared import (
    _create_login_redirect,
    _detect_language_from_path,
    _require_admin_session,
    generate_csrf_token,
    logger,
    set_csrf_cookie,
    templates,
    validate_login_csrf_token,
)
from .activated_repos import (
    _get_all_activated_repos,
)

web_router = APIRouter()

# Session-based query history storage (in-memory, per session)
# Key: session_id, Value: list of query dicts
# TODO: per-node state -- under round-robin load balancing a user's history is
# only visible on the node that recorded it (CLAUDE.md "Cluster-Aware State").
# Kept as is by the phase 6 move; belongs in a shared store.
_query_history: dict = {}
MAX_QUERY_HISTORY = 10


def _get_session_query_history(session_username: str) -> list:
    """Get query history for a session."""
    return cast(list, _query_history.get(session_username, []))


def _add_to_query_history(
    session_username: str, query_text: str, repository: str, search_mode: str
) -> None:
    """Add a query to session history."""
    if session_username not in _query_history:
        _query_history[session_username] = []

    history = _query_history[session_username]

    # Add new query at the beginning
    history.insert(
        0,
        {
            "query_text": query_text,
            "repository": repository,
            "search_mode": search_mode,
        },
    )

    # Keep only MAX_QUERY_HISTORY items
    if len(history) > MAX_QUERY_HISTORY:
        _query_history[session_username] = history[:MAX_QUERY_HISTORY]


def _get_all_activated_repos_for_query(backend_registry=None) -> list:
    """
    Get all activated repositories for query dropdown.

    Returns list of repos with user_alias, username, and is_global flag.
    Includes both user-activated repos and globally activated repos.
    """
    repos = []

    # Add globally activated repos first
    if backend_registry is not None:
        try:
            repos_dict = backend_registry.global_repos.list_repos()
            for global_repo in repos_dict.values():
                repos.append(
                    {
                        "user_alias": global_repo["alias_name"],
                        "username": "global",
                        "is_global": True,
                        "repo_name": global_repo.get("repo_name", ""),
                        "path": global_repo.get("index_path"),
                    }
                )
        except Exception as e:
            logger.warning(
                format_error_log(
                    "STORE-GENERAL-030", f"Could not load global repos for query: {e}"
                ),
                extra={"correlation_id": get_correlation_id()},
            )

    # Add user-activated repos
    user_repos = _get_all_activated_repos()
    for repo in user_repos:
        repo["is_global"] = False
        repos.append(repo)

    return sorted(repos, key=lambda r: r.get("user_alias", "").lower())


def _create_query_page_response(
    request: Request,
    session: SessionData,
    query_text: str = "",
    selected_repository: str = "",
    search_mode: str = "semantic",
    limit: int = 10,
    language: str = "",
    path_pattern: str = "",
    min_score: str = "",
    results: Optional[list] = None,
    query_executed: bool = False,
    error_message: Optional[str] = None,
    success_message: Optional[str] = None,
    time_range_all: bool = False,
    time_range: str = "",
    at_commit: str = "",
    case_sensitive: bool = False,
    fuzzy: bool = False,
    regex: bool = False,
    scip_query_type: str = "definition",
    scip_exact: bool = False,
    warning_message: Optional[str] = None,
) -> HTMLResponse:
    """Create query page response with all necessary context."""
    csrf_token = generate_csrf_token()
    backend_registry = getattr(request.app.state, "backend_registry", None)
    repositories = _get_all_activated_repos_for_query(backend_registry)
    query_history = _get_session_query_history(session.username)

    response = templates.TemplateResponse(
        request,
        "query.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "query",
            "show_nav": True,
            "csrf_token": csrf_token,
            "repositories": repositories,
            "query_history": query_history,
            "query_text": query_text,
            "selected_repository": selected_repository,
            "search_mode": search_mode,
            "limit": limit,
            "language": language,
            "path_pattern": path_pattern,
            "min_score": min_score,
            "results": results,
            "query_executed": query_executed,
            "error_message": error_message,
            "warning_message": warning_message,
            "success_message": success_message,
            "time_range_all": time_range_all,
            "time_range": time_range,
            "at_commit": at_commit,
            "case_sensitive": case_sensitive,
            "fuzzy": fuzzy,
            "regex": regex,
            "scip_query_type": scip_query_type,
            "scip_exact": scip_exact,
            # Search modes enabled features contribute (the page offers them).
            "feature_query_modes": feature_query_modes(),
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


def _query_repo_path(
    target_repo: Dict[str, Any], user_alias: str, backend_registry: Any
) -> Optional[str]:
    """The picked repository's root on this pod: an activated repository's
    path, or a global repository's index path from the registry."""
    repo_path = target_repo.get("path")
    if not repo_path and target_repo.get("is_global") and backend_registry is not None:
        try:
            global_repo_meta = backend_registry.global_repos.get_repo(user_alias)
            if global_repo_meta:
                repo_path = global_repo_meta.get("index_path")
        except Exception as e:
            logger.warning(
                format_error_log(
                    "STORE-GENERAL-031",
                    f"Failed to resolve global repo path for '{user_alias}': {e}",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
    return str(repo_path) if repo_path else None


def _run_feature_query_mode(
    mode: WebQueryModeContribution,
    request: Request,
    target_repo: Dict[str, Any],
    user_alias: str,
    username: str,
    query_text: str,
    limit: int,
    min_score: str,
    options: Dict[str, Any],
    backend_registry: Any,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Run a query in a feature's search mode (``web_query_modes()``): the
    feature runs the search and records its event. Returns the result rows
    and the message shown instead, if any."""
    repo_path = _query_repo_path(target_repo, user_alias, backend_registry)
    if not repo_path:
        return [], f"Repository '{user_alias}' path not found"
    outcome = mode.execute(
        WebQueryRequest(
            http_request=request,
            username=username,
            repository_alias=user_alias,
            repository_path=repo_path,
            query_text=query_text,
            limit=limit,
            min_score=min_score,
            options=options,
        )
    )
    rows = [dict(row) for row in outcome.rows]
    for row in rows:
        row.setdefault("language", _detect_language_from_path(str(row["file_path"])))
    return rows, outcome.error


def _mode_options(scip_query_type: str, scip_exact: bool) -> Dict[str, Any]:
    """The page's mode-specific form fields a contributed mode receives (the
    SCIP mode's, the one the page has fields for)."""
    return {"scip_query_type": scip_query_type, "scip_exact": scip_exact}


def _unavailable_mode_message(search_mode: str) -> Optional[str]:
    """Why *search_mode* cannot run here: not a core mode and no enabled
    feature contributes it. None when it can."""
    if search_mode in CORE_QUERY_MODES or feature_query_mode(search_mode):
        return None
    return f"Search mode '{search_mode}' is not available on this server"


@web_router.get("/query", response_class=HTMLResponse)
def query_page(request: Request):
    """Query testing interface page."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_query_page_response(request, session)


@web_router.post("/query", response_class=HTMLResponse)
@reads_served_indexes
def query_submit(
    request: Request,
    query_text: str = Form(""),
    repository: str = Form(""),
    search_mode: str = Form("semantic"),
    limit: int = Form(10),
    language: str = Form(""),
    path_pattern: str = Form(""),
    min_score: str = Form(""),
    csrf_token: Optional[str] = Form(None),
    time_range_all: bool = Form(False),
    time_range: str = Form(""),
    at_commit: str = Form(""),
    case_sensitive: bool = Form(False),
    fuzzy: bool = Form(False),
    regex: bool = Form(False),
    scip_query_type: str = Form("definition"),
    scip_exact: bool = Form(False),
):
    """Process query form submission."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_query_page_response(
            request,
            session,
            query_text=query_text,
            selected_repository=repository,
            search_mode=search_mode,
            limit=limit,
            language=language,
            path_pattern=path_pattern,
            min_score=min_score,
            error_message="Invalid CSRF token",
            time_range_all=time_range_all,
            time_range=time_range,
            at_commit=at_commit,
            case_sensitive=case_sensitive,
            fuzzy=fuzzy,
            regex=regex,
            scip_query_type=scip_query_type,
            scip_exact=scip_exact,
        )

    # Validate required fields
    if not query_text or not query_text.strip():
        return _create_query_page_response(
            request,
            session,
            query_text=query_text,
            selected_repository=repository,
            search_mode=search_mode,
            limit=limit,
            language=language,
            path_pattern=path_pattern,
            min_score=min_score,
            error_message="Query text is required",
            time_range_all=time_range_all,
            time_range=time_range,
            at_commit=at_commit,
            case_sensitive=case_sensitive,
            fuzzy=fuzzy,
            regex=regex,
            scip_query_type=scip_query_type,
            scip_exact=scip_exact,
        )

    if not repository:
        return _create_query_page_response(
            request,
            session,
            query_text=query_text,
            selected_repository=repository,
            search_mode=search_mode,
            limit=limit,
            language=language,
            path_pattern=path_pattern,
            min_score=min_score,
            error_message="Please select a repository",
            time_range_all=time_range_all,
            time_range=time_range,
            at_commit=at_commit,
            case_sensitive=case_sensitive,
            fuzzy=fuzzy,
            regex=regex,
            scip_query_type=scip_query_type,
            scip_exact=scip_exact,
        )

    # Add to query history
    _add_to_query_history(session.username, query_text.strip(), repository, search_mode)

    # Handle temporal search mode - default to time_range_all if no specific temporal params
    if search_mode == "temporal":
        if not time_range and not at_commit and not time_range_all:
            time_range_all = True

    # Parse min_score
    parsed_min_score = None
    if min_score and min_score.strip():
        try:
            parsed_min_score = float(min_score)
        except ValueError:
            pass

    # Execute actual query
    results = []
    query_executed = True
    error_message = None
    warning_message: Optional[str] = None
    backend_registry = getattr(request.app.state, "backend_registry", None)
    contributed_mode = feature_query_mode(search_mode)

    try:
        unavailable = _unavailable_mode_message(search_mode)
        if unavailable is not None:
            error_message = unavailable
        elif contributed_mode is not None:
            # A feature's search mode (web_query_modes()): the feature runs
            # the search and records its event.
            repo_parts = repository.split(" (")
            user_alias = repo_parts[0] if repo_parts else repository
            all_repos = _get_all_activated_repos_for_query(backend_registry)
            target_repo = next(
                (r for r in all_repos if r.get("user_alias") == user_alias), None
            )
            if not target_repo:
                error_message = f"Repository '{user_alias}' not found"
            else:
                mode_rows, error_message = _run_feature_query_mode(
                    contributed_mode,
                    request,
                    target_repo,
                    user_alias,
                    session.username,
                    query_text,
                    limit,
                    min_score,
                    _mode_options(scip_query_type, scip_exact),
                    backend_registry,
                )
                results.extend(mode_rows)
        else:
            # Handle semantic/FTS/hybrid/temporal queries
            query_manager = _get_semantic_query_manager()
            # Repository format is "user_alias (username)"
            repo_parts = repository.split(" (")
            user_alias = repo_parts[0] if repo_parts else repository

            # Get the repository from all available repos (including global)
            all_repos = _get_all_activated_repos_for_query(backend_registry)
            target_repo = None
            for repo in all_repos:
                if repo.get("user_alias") == user_alias:
                    target_repo = repo
                    break

            if not query_manager:
                error_message = "Query service not available"
            elif not target_repo:
                error_message = f"Repository '{user_alias}' not found"
            else:
                # Activated and global repositories: the same mode-aware path.
                try:
                    text_rows, warning_message = _execute_text_query(
                        query_manager,
                        target_repo,
                        user_alias,
                        session.username,
                        query_text,
                        limit=limit,
                        min_score=parsed_min_score,
                        language=language,
                        path_filter=path_pattern,
                        search_mode=search_mode,
                        time_range=time_range,
                        time_range_all=time_range_all,
                        at_commit=at_commit,
                        case_sensitive=case_sensitive,
                        fuzzy=fuzzy,
                        regex=regex,
                        event_writer=search_event_recording.writer_from_app(
                            request.app
                        ),
                    )
                except WebQueryNotCompleted as e:
                    # The query was not completed (access refused, invalid
                    # parameters, provider outage, timeout, missing index):
                    # _execute_text_query logged it and classified the text
                    # the user is shown (classify_search_error).
                    error_message = f"Query failed: {e}"
                else:
                    results.extend(text_rows)

    except Exception as e:
        message = _classify_web_query_failure(
            e, error_code="STORE-GENERAL-035", detail_logged_upstream=False
        )
        error_message = f"Query failed: {message}"

    return _create_query_page_response(
        request,
        session,
        query_text=query_text,
        selected_repository=repository,
        search_mode=search_mode,
        limit=limit,
        language=language,
        path_pattern=path_pattern,
        min_score=min_score,
        results=results if not error_message else None,
        query_executed=query_executed,
        error_message=error_message,
        time_range_all=time_range_all,
        time_range=time_range,
        at_commit=at_commit,
        case_sensitive=case_sensitive,
        fuzzy=fuzzy,
        regex=regex,
        scip_query_type=scip_query_type,
        scip_exact=scip_exact,
        warning_message=warning_message,
    )


@web_router.get("/partials/query-results", response_class=HTMLResponse)
def query_results_partial(request: Request):
    """
    Partial refresh endpoint for query results.

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    csrf_token = generate_csrf_token()

    response = templates.TemplateResponse(
        request,
        "partials/query_results.html",
        {
            "request": request,
            "results": None,
            "query_executed": False,
            "query_text": "",
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


def _get_semantic_query_manager():
    """Get the semantic query manager instance."""
    try:
        from ...app import semantic_query_manager

        return semantic_query_manager
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-036", f"Failed to get semantic query manager: {e}"
            ),
            exc_info=True,
            extra={"correlation_id": get_correlation_id()},
        )
        return None


class WebQueryNotCompleted(Exception):
    """A Web text query the query layer did not complete. ``str()`` is what
    the user is shown: the ``classify_search_error`` message (a client
    error's own text, else the fixed public failure message)."""


def _classify_web_query_failure(
    error: BaseException, *, error_code: str, detail_logged_upstream: bool
) -> str:
    """Log a failed Web query once and return the text the user is shown.

    One rule for every search front door (``classify_search_error``): only a
    client error's text (it describes the caller's own request) is logged,
    at WARNING, and shown. Any other failure can carry a provider's or the
    server's internal text, so the user sees only the fixed public message;
    its detail is logged at ERROR with the traceback, or, when the query
    layer already logged it (``detail_logged_upstream``), only its class is
    logged at WARNING.
    """
    from code_indexer.server.query.search_error_policy import classify_search_error

    outcome = classify_search_error(error)
    error_class = type(error).__name__
    if not outcome.log_as_internal or detail_logged_upstream:
        detail = (
            outcome.message
            if outcome.client_error
            else "not a client error, detail not logged"
        )
        logger.warning(
            format_error_log(
                error_code, f"Query not completed: {error_class}: {detail}"
            ),
            extra={"correlation_id": get_correlation_id()},
        )
    else:
        logger.error(
            format_error_log(
                error_code, f"Query execution failed: {error_class}: {error}"
            ),
            exc_info=error,
            extra={"correlation_id": get_correlation_id()},
        )
    return outcome.message


def _execute_text_query(
    query_manager: Any,
    target_repo: Dict[str, Any],
    user_alias: str,
    session_username: str,
    query_text: str,
    *,
    limit: int,
    min_score: Optional[float],
    language: str,
    path_filter: str,
    search_mode: str,
    time_range: str,
    time_range_all: bool,
    at_commit: str,
    case_sensitive: bool,
    fuzzy: bool,
    regex: bool,
    # Any: the app's SearchEventLogWriter, or None when it is not running
    # (then nothing is recorded); typed loosely like every recording door.
    event_writer: Any,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Run a semantic/FTS/hybrid/temporal Web query.

    Returns ``(template rows, warning)``; the warning is the query layer's
    own (e.g. no temporal index), the same text REST and MCP return.

    Shared by both Web query handlers. Activated and global repositories go
    through the same mode-aware ``query_user_repositories`` path REST and
    MCP use, with every form parameter. A global repository is queried as
    the signed-in user, so the normal per-user repository access narrowing
    applies (a repository the user cannot access is refused like an unknown
    one); an activated repository is queried as its owner. Failures raise;
    a query the query layer did not complete (SemanticQueryError, ValueError)
    is classified with ``classify_search_error``, logged here once at
    WARNING, and raised as ``WebQueryNotCompleted`` carrying the message the
    user is shown.

    Story #2115: a completed query is recorded in the search event log
    (``run_recorded_search``) as the signed-in user, with the mode that ran
    ("temporal" whenever a time range or commit scopes it); a query that
    raises records a failure row (Story #2117). Tool "web:query", door web.
    """
    from code_indexer.server.query.semantic_query_manager import SemanticQueryError
    from code_indexer.server.services.search_event_recording import (
        run_recorded_search,
    )

    if target_repo.get("is_global"):
        username = session_username
    else:
        username = target_repo.get("username", session_username)

    try:
        query_response = run_recorded_search(
            event_writer,
            lambda: query_manager.query_user_repositories(
                username=username,
                query_text=query_text.strip(),
                repository_alias=user_alias,
                limit=limit,
                min_score=min_score,
                language=language if language else None,
                path_filter=path_filter if path_filter else None,
                search_mode=search_mode,
                time_range=time_range if time_range else None,
                time_range_all=time_range_all,
                at_commit=at_commit if at_commit else None,
                case_sensitive=case_sensitive,
                fuzzy=fuzzy,
                regex=regex,
            ),
            # search_event_recording: the module imported at the top of
            # this file.
            origin=search_event_recording.SearchOrigin(tool="web:query", door="web"),
            username=session_username,
            repo_alias=user_alias,
            search_type=(
                "temporal"
                if (time_range or time_range_all or at_commit)
                else search_mode
            ),
            query_text=query_text.strip(),
            correlation_id=get_correlation_id(),
        )
    except (SemanticQueryError, ValueError) as e:
        # The query layer logged the detail of a failure it raised itself.
        message = _classify_web_query_failure(
            e, error_code="STORE-GENERAL-053", detail_logged_upstream=True
        )
        raise WebQueryNotCompleted(message) from e

    rows = [
        {
            "file_path": result.get("file_path", ""),
            "line_numbers": f"{result.get('line_number', 1)}",
            "content": result.get("code_snippet", ""),
            "score": result.get("similarity_score", 0.0),
            "language": _detect_language_from_path(result.get("file_path", "")),
            "repository_alias": result.get("repository_alias", ""),
            "source_repo": result.get("source_repo"),
            "metadata": result.get("metadata"),
            "temporal_context": result.get("temporal_context"),
        }
        for result in query_response.get("results", [])
    ]
    return rows, query_response.get("warning")


@web_router.post("/partials/query-results", response_class=HTMLResponse)
@reads_served_indexes
def query_results_partial_post(
    request: Request,
    query_text: str = Form(""),
    repository: str = Form(""),
    search_mode: str = Form("semantic"),
    limit: int = Form(10),
    language: str = Form(""),
    path_pattern: str = Form(""),
    min_score: str = Form(""),
    csrf_token: Optional[str] = Form(None),
    time_range_all: bool = Form(False),
    time_range: str = Form(""),
    at_commit: str = Form(""),
    case_sensitive: bool = Form(False),
    fuzzy: bool = Form(False),
    regex: bool = Form(False),
    scip_query_type: str = Form("definition"),
    scip_exact: bool = Form(False),
):
    """
    Execute query and return results partial via htmx.

    Returns HTML fragment for htmx partial updates.

    Note: CSRF validation is intentionally not performed for this endpoint because:
    1. Session authentication already protects against unauthorized access
    2. HTMX requests are same-origin (browser enforces this)
    3. HTMX adds specific headers (HX-Request) that indicate the request origin
    4. The main form submission route (/admin/query) retains CSRF protection
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Note: csrf_token parameter is accepted but not validated for HTMX partials
    # See docstring above for security rationale

    # Validate required fields
    if not query_text or not query_text.strip():
        return templates.TemplateResponse(
            request,
            "partials/query_results.html",
            {
                "request": request,
                "results": None,
                "query_executed": False,
                "query_text": query_text,
                "error_message": "Query text is required",
            },
        )

    if not repository:
        return templates.TemplateResponse(
            request,
            "partials/query_results.html",
            {
                "request": request,
                "results": None,
                "query_executed": False,
                "query_text": query_text,
                "error_message": "Please select a repository",
            },
        )

    # Add to query history
    _add_to_query_history(session.username, query_text.strip(), repository, search_mode)

    # Handle temporal search mode - default to time_range_all if no specific temporal params
    if search_mode == "temporal":
        if not time_range and not at_commit and not time_range_all:
            time_range_all = True

    # Parse min_score
    parsed_min_score = None
    if min_score and min_score.strip():
        try:
            parsed_min_score = float(min_score)
        except ValueError:
            pass

    # Execute actual query
    results = []
    query_executed = True
    error_message = None
    warning_message: Optional[str] = None
    backend_registry = getattr(request.app.state, "backend_registry", None)
    contributed_mode = feature_query_mode(search_mode)

    try:
        query_manager = _get_semantic_query_manager()
        unavailable = _unavailable_mode_message(search_mode)
        if unavailable is not None:
            error_message = unavailable
        elif not query_manager:
            error_message = "Query service not available"
        else:
            # Find the username for this repository
            # Repository format is "user_alias (username)"
            repo_parts = repository.split(" (")
            user_alias = repo_parts[0] if repo_parts else repository

            # Get the repository owner from activated repos
            all_repos = _get_all_activated_repos_for_query(backend_registry)
            target_repo = None
            for repo in all_repos:
                if repo.get("user_alias") == user_alias:
                    target_repo = repo
                    break

            if not target_repo:
                error_message = f"Repository '{user_alias}' not found"
            elif contributed_mode is not None:
                # A feature's search mode (web_query_modes()).
                mode_rows, mode_error = _run_feature_query_mode(
                    contributed_mode,
                    request,
                    target_repo,
                    user_alias,
                    session.username,
                    query_text,
                    limit,
                    min_score,
                    _mode_options(scip_query_type, scip_exact),
                    backend_registry,
                )
                results.extend(mode_rows)
                if mode_error:
                    error_message = mode_error
            else:
                # Activated and global repositories: the same mode-aware path.
                try:
                    text_rows, warning_message = _execute_text_query(
                        query_manager,
                        target_repo,
                        user_alias,
                        session.username,
                        query_text,
                        limit=limit,
                        min_score=parsed_min_score,
                        language=language,
                        path_filter=path_pattern,
                        search_mode=search_mode,
                        time_range=time_range,
                        time_range_all=time_range_all,
                        at_commit=at_commit,
                        case_sensitive=case_sensitive,
                        fuzzy=fuzzy,
                        regex=regex,
                        event_writer=search_event_recording.writer_from_app(
                            request.app
                        ),
                    )
                except WebQueryNotCompleted as e:
                    # The query was not completed (access refused, invalid
                    # parameters, provider outage, timeout, missing index):
                    # _execute_text_query logged it and classified the text
                    # the user is shown (classify_search_error).
                    error_message = f"Query failed: {e}"
                else:
                    results.extend(text_rows)

    except Exception as e:
        message = _classify_web_query_failure(
            e, error_code="STORE-GENERAL-041", detail_logged_upstream=False
        )
        error_message = f"Query failed: {message}"

    csrf_token_new = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "partials/query_results.html",
        {
            "request": request,
            "results": results if not error_message else None,
            "query_executed": query_executed,
            "query_text": query_text,
            "error_message": error_message,
            "warning_message": warning_message,
            "search_mode": search_mode,
        },
    )

    set_csrf_cookie(response, csrf_token_new)
    return response
