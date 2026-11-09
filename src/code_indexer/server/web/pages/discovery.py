"""
Admin repository auto-discovery pages and APIs.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from typing import Any, Dict, List, Optional, Tuple
from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from ...auth import dependencies
from ...utils.bounded_submission_gate import (
    BoundedSubmissionGate,
    SubmissionGateOverloadedError,
)
from code_indexer.server.logging_utils import format_error_log
from code_indexer.utils.credential_redaction import mask_url_credentials

from ._shared import (
    _create_login_redirect,
    _get_background_job_manager,
    _get_golden_repo_manager,
    _get_token_manager,
    _require_admin_session,
    generate_csrf_token,
    get_csrf_token_from_cookie,
    logger,
    set_csrf_cookie,
    templates,
    validate_login_csrf_token,
)

web_router = APIRouter()

# Story #1491 AC3: upper bound on how many synchronous git ls-remote branch
# fetches fetch_discovery_branches may offload to the executor at once. The
# story requires the introduced concurrency to be BOUNDED -- a large
# auto-discovery repo list must never fan out into an unbounded burst of git
# subprocesses.
_DISCOVERY_BRANCH_FETCH_MAX_CONCURRENCY = 8

# Story #1491 review item 11: that bound must be PROCESS-WIDE, not per-request.
# It was originally an asyncio.Semaphore constructed inside
# fetch_discovery_branches, which bounded a single request's fan-out and nothing
# else: K concurrent admin discovery requests permitted 8 x K simultaneous git
# subprocesses -- exactly the burst AC3 exists to prevent (measured: two
# concurrent requests reached 16).
#
# A dedicated pool is the limiter rather than a shared counter, for two specific
# reasons:
#   * An asyncio.Semaphore binds to the event loop that first awaits it, so a
#     module-level one is a cross-loop hazard: a second loop in the same process
#     (tests, or any loop-per-thread work such as the sync-dispatched handlers
#     this same story introduced) either raises or shares nothing.
#   * A threading.Semaphore acquired INSIDE the default executor would bound the
#     git processes but hold default-executor threads hostage while they waited,
#     starving every other run_in_executor caller in the process.
# The pool size IS the bound: excess work queues inside this pool (never dropped,
# never widened), the shared default executor is untouched, and no asyncio
# primitive is involved. Created lazily so importing this module starts no
# threads, and disposed via shutdown_discovery_branch_fetch_executor() above.
#
# Bug #1800: this was originally "never shut down, because it lives exactly as
# long as the process". Wrong, and it wedged the server test gate. The pool's
# NON-DAEMON workers are registered in concurrent.futures.thread._threads_queues
# and joined by _python_exit inside threading._shutdown(), so the pool decides
# when the process may die, waiting out whatever it is running.
_DISCOVERY_BRANCH_FETCH_EXECUTOR: Optional[ThreadPoolExecutor] = None
_DISCOVERY_BRANCH_FETCH_EXECUTOR_LOCK = threading.Lock()

# Story #1491 dual-review round 4: the pool above bounds how many fetches RUN,
# but a ThreadPoolExecutor's internal work queue is an unbounded SimpleQueue --
# so a large repo list (or several concurrent requests) could still hand it an
# unlimited number of PENDING git tasks, growing memory with no ceiling and no
# signal. This is the admission budget that closes that: at most this many
# fetches may be outstanding (submitted and unfinished) process-wide, which caps
# the pool's queue at OUTSTANDING - MAX_CONCURRENCY entries. Excess callers wait
# in async space, holding no thread and no queue slot.
#
# 2x the worker count: enough queued work that a finishing worker always has its
# next fetch ready (no throughput loss versus today), while keeping the queue a
# fixed, small number of pending tasks rather than an unbounded backlog.
_DISCOVERY_BRANCH_FETCH_MAX_OUTSTANDING = 2 * _DISCOVERY_BRANCH_FETCH_MAX_CONCURRENCY

# ONE shared gate is the entire bound, held module-level for exactly the reason
# the pool is (review item 11): a gate constructed per request would bound a
# single request and nothing else.
_DISCOVERY_BRANCH_FETCH_GATE: Optional[BoundedSubmissionGate] = None
_DISCOVERY_BRANCH_FETCH_GATE_LOCK = threading.Lock()


def _get_discovery_branch_fetch_executor() -> ThreadPoolExecutor:
    """Return the process-wide bounded pool for synchronous ls-remote fetches."""
    global _DISCOVERY_BRANCH_FETCH_EXECUTOR
    with _DISCOVERY_BRANCH_FETCH_EXECUTOR_LOCK:
        if _DISCOVERY_BRANCH_FETCH_EXECUTOR is None:
            _DISCOVERY_BRANCH_FETCH_EXECUTOR = ThreadPoolExecutor(
                max_workers=_DISCOVERY_BRANCH_FETCH_MAX_CONCURRENCY,
                thread_name_prefix="discovery-branch-fetch",
            )
        return _DISCOVERY_BRANCH_FETCH_EXECUTOR


def shutdown_discovery_branch_fetch_executor() -> None:
    """Dispose the process-wide branch-fetch pool (Bug #1800).

    The pool's workers are non-daemon threads that interpreter exit must join,
    so an owner that never disposes it makes process exit wait on whatever the
    pool is running. Whoever owns the process lifecycle calls this: the server
    calls it from the lifespan shutdown; a test session calls it at session end.

    Idempotent, and a no-op when the pool was never created. The global is
    cleared so a later caller builds a fresh pool instead of submitting to a
    dead one. ``shutdown`` runs outside the lock so disposal never blocks a
    concurrent creator, and ``cancel_futures`` drops work that has not started
    rather than making exit wait for a queue nobody is going to read.
    """
    global _DISCOVERY_BRANCH_FETCH_EXECUTOR
    with _DISCOVERY_BRANCH_FETCH_EXECUTOR_LOCK:
        executor = _DISCOVERY_BRANCH_FETCH_EXECUTOR
        _DISCOVERY_BRANCH_FETCH_EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def _get_discovery_branch_fetch_gate() -> BoundedSubmissionGate:
    """Return the process-wide submission budget for ls-remote fetches."""
    global _DISCOVERY_BRANCH_FETCH_GATE
    with _DISCOVERY_BRANCH_FETCH_GATE_LOCK:
        if _DISCOVERY_BRANCH_FETCH_GATE is None:
            _DISCOVERY_BRANCH_FETCH_GATE = BoundedSubmissionGate(
                _DISCOVERY_BRANCH_FETCH_MAX_OUTSTANDING
            )
        return _DISCOVERY_BRANCH_FETCH_GATE


# Discovery enrichment limit (Story #754): max clone_urls per enrich request
MAX_ENRICH_CLONE_URLS = 50


# =============================================================================
# Auto-Discovery Routes
# =============================================================================


def _get_gitlab_provider():
    """Create GitLab provider with required dependencies."""
    from ...services.repository_providers.gitlab_provider import GitLabProvider

    token_manager = _get_token_manager()
    golden_repo_manager = _get_golden_repo_manager()

    return GitLabProvider(
        token_manager=token_manager, golden_repo_manager=golden_repo_manager
    )


def _get_github_provider():
    """Create GitHub provider with required dependencies."""
    from ...services.repository_providers.github_provider import GitHubProvider

    token_manager = _get_token_manager()
    golden_repo_manager = _get_golden_repo_manager()

    return GitHubProvider(
        token_manager=token_manager, golden_repo_manager=golden_repo_manager
    )


def _get_hidden_discovery_backend():
    """Get or create HiddenDiscoveryReposSqliteBackend instance."""
    from ...storage.sqlite_backends import HiddenDiscoveryReposSqliteBackend
    from ...services.config_service import get_config_service

    config_service = get_config_service()
    db_path = str(config_service.config_manager.server_dir / "data" / "cidx_server.db")
    return HiddenDiscoveryReposSqliteBackend(db_path)


def _build_gitlab_repos_response(
    request: Request,
    repositories: Optional[list] = None,
    page_size: int = 50,
    next_cursor: Optional[str] = None,
    has_next_page: bool = False,
    partial_due_to_cap: bool = False,
    source_total: Optional[int] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    search_term: Optional[str] = None,
    show_hidden: bool = False,
    hidden_identifiers: Optional[set] = None,
):
    """Build GitLab repos partial template response."""
    # Get existing CSRF token from cookie or generate new one
    csrf_token = get_csrf_token_from_cookie(request) or generate_csrf_token()

    response = templates.TemplateResponse(
        request,
        "partials/gitlab_repos.html",
        {
            "request": request,
            "csrf_token": csrf_token,
            "repositories": repositories or [],
            "page_size": page_size,
            "next_cursor": next_cursor,
            "has_next_page": has_next_page,
            "partial_due_to_cap": partial_due_to_cap,
            "source_total": source_total,
            "error_type": error_type,
            "error_message": error_message,
            "search_term": search_term or "",
            "show_hidden": show_hidden,
            "hidden_identifiers": hidden_identifiers or set(),
        },
    )

    # Set CSRF cookie to ensure token is available for form submission
    set_csrf_cookie(response, csrf_token)
    return response


def _build_github_repos_response(
    request: Request,
    repositories: Optional[list] = None,
    page_size: int = 50,
    next_cursor: Optional[str] = None,
    has_next_page: bool = False,
    partial_due_to_cap: bool = False,
    source_total: Optional[int] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    search_term: Optional[str] = None,
    show_hidden: bool = False,
    hidden_identifiers: Optional[set] = None,
):
    """Build GitHub repos partial template response."""
    # Get existing CSRF token from cookie or generate new one
    csrf_token = get_csrf_token_from_cookie(request) or generate_csrf_token()

    response = templates.TemplateResponse(
        request,
        "partials/github_repos.html",
        {
            "request": request,
            "csrf_token": csrf_token,
            "repositories": repositories or [],
            "page_size": page_size,
            "next_cursor": next_cursor,
            "has_next_page": has_next_page,
            "partial_due_to_cap": partial_due_to_cap,
            "source_total": source_total,
            "error_type": error_type,
            "error_message": error_message,
            "search_term": search_term or "",
            "show_hidden": show_hidden,
            "hidden_identifiers": hidden_identifiers or set(),
        },
    )

    # Set CSRF cookie to ensure token is available for form submission
    set_csrf_cookie(response, csrf_token)
    return response


@web_router.get("/auto-discovery", response_class=HTMLResponse)
def auto_discovery_page(request: Request):
    """Auto-discovery page - discover repositories from GitLab/GitHub."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    csrf_token = get_csrf_token_from_cookie(request) or generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "auto_discovery.html",
        {
            "request": request,
            "current_page": "auto-discovery",
            "show_nav": True,
            "csrf_token": csrf_token,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


def _load_hidden_ids(show_hidden: bool):
    """Load hidden discovery repo identifiers (Story #719)."""
    try:
        all_hidden = _get_hidden_discovery_backend().get_hidden_set()
    except Exception as e:
        logger.warning("Failed to load hidden repos: %s", e)
        all_hidden = set()
    return (set() if show_hidden else all_hidden), all_hidden


def _resolve_provider(request: Request, platform: str):
    """Authenticate the admin session and resolve the platform provider.

    Handles ONLY auth and platform lookup — does NOT call any provider methods.
    Returns (provider, None) on success, or (None, JSONResponse) on failure.
    """
    session = _require_admin_session(request)
    if not session:
        return None, JSONResponse(content={"error": "Unauthorized"}, status_code=401)

    if platform == "gitlab":
        return _get_gitlab_provider(), None
    if platform == "github":
        return _get_github_provider(), None

    return None, JSONResponse(
        content={"error": f"Unknown platform: {platform}"}, status_code=400
    )


@web_router.get("/api/discovery/{platform}/all")
def discovery_all(
    request: Request,
    platform: str,
    show_hidden: bool = Query(default=False),
):
    """Return all unregistered repositories for the given platform (Story #754).

    Performs an exhaustive fetch from the upstream API and returns every
    repository not yet registered as a golden repo, annotated with is_hidden.
    Client-side JS handles pagination, search, and sort.
    """
    provider, error = _resolve_provider(request, platform)
    if error is not None:
        return error

    try:
        if not provider.is_configured():
            return JSONResponse(
                content={"error": f"{platform.capitalize()} token not configured"},
                status_code=422,
            )
        indexed_urls = provider._get_indexed_canonical_urls()
        _, all_hidden = _load_hidden_ids(show_hidden)
        hidden_ids = set() if show_hidden else all_hidden
        result = provider.discover_all_repositories(
            indexed_urls=indexed_urls,
            hidden_identifiers=hidden_ids,
        )
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-044",
                f"Unexpected error in {platform} discovery/all: {e}",
            )
        )
        return JSONResponse(content={"error": str(e)}, status_code=500)


@web_router.post(
    "/api/discovery/{platform}/start",
    dependencies=[Depends(dependencies.require_elevation())],
)
async def discovery_start(
    request: Request,
    platform: str,
):
    """Start a background discovery job for the given platform (Story #1157).

    Returns job_id immediately. Client polls GET /api/jobs/{job_id} for progress.
    Deduplicates: if a PENDING or RUNNING job of the same type exists, returns it.
    """
    session = await asyncio.to_thread(_require_admin_session, request)
    if session is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    provider, err = await asyncio.to_thread(_resolve_provider, request, platform)
    if err is not None:
        return err
    if not provider.is_configured():
        return JSONResponse(
            {"error": f"{platform.capitalize()} token not configured"}, status_code=422
        )

    bgm = _get_background_job_manager()
    if bgm is None:
        return JSONResponse(
            {"error": "Background job manager unavailable"}, status_code=503
        )

    from code_indexer.server.repositories.background_jobs import JobStatus

    op_type = f"{platform}_discovery"

    # Manual dedup: repo_alias=None bypasses BGM atomic DB gate
    with bgm._lock:
        for job in bgm.jobs.values():
            if job.operation_type == op_type and job.status in (
                JobStatus.PENDING,
                JobStatus.RUNNING,
            ):
                return JSONResponse({"job_id": job.job_id, "existing": True})

    indexed_urls = provider._get_indexed_canonical_urls()
    _, all_hidden = _load_hidden_ids(show_hidden=False)

    payload_cache = getattr(request.app.state, "payload_cache", None)
    job_id_holder: dict = {}

    def _worker(progress_callback=None):  # type: ignore[misc]
        result = provider.discover_all_repositories(
            indexed_urls=indexed_urls,
            hidden_identifiers=all_hidden,
            progress_callback=progress_callback,
        )
        jid = job_id_holder.get("job_id")
        if jid and payload_cache is not None:
            payload_cache.store_with_key(f"discovery:{jid}", json.dumps(result))
        return {
            "total_source": result["total_source"],
            "total_unregistered": result["total_unregistered"],
            "result_ready": True,
        }

    job_id = bgm.submit_job(
        op_type,
        _worker,
        submitter_username=session.username,
        repo_alias=None,
    )
    job_id_holder["job_id"] = job_id
    return JSONResponse({"job_id": job_id, "existing": False})


@web_router.get("/api/discovery/{platform}/result/{job_id}")
async def discovery_result(
    request: Request,
    platform: str,
    job_id: str,
):
    """Return the full discovery result for a completed job (Story #1157).

    Result lives in PayloadCache (TTL-based, not read-once). Re-reads within TTL return 200.
    Auth required.
    """
    session = await asyncio.to_thread(_require_admin_session, request)
    if session is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    payload_cache = getattr(request.app.state, "payload_cache", None)
    if payload_cache is None:
        return JSONResponse({"error": "Payload cache unavailable"}, status_code=503)

    cache_key = f"discovery:{job_id}"
    if not payload_cache.has_key(cache_key):
        return JSONResponse({"error": "Result not found or expired"}, status_code=404)

    # Accumulate all pages — PayloadCache.retrieve() returns one page at a time
    chunks: list = []
    page = 0
    try:
        while True:
            cache_result = payload_cache.retrieve(cache_key, page=page)
            chunks.append(cache_result.content)
            if not cache_result.has_more:
                break
            page += 1
    except Exception:
        logger.error(
            "Failed to retrieve discovery result for %s", cache_key, exc_info=True
        )
        return JSONResponse(
            {"error": "Failed to retrieve discovery result"}, status_code=500
        )

    data = json.loads("".join(chunks))
    return JSONResponse(data)


@web_router.post(
    "/api/discovery/{platform}/enrich",
    dependencies=[Depends(dependencies.require_elevation())],
)
async def discovery_enrich(
    request: Request,
    platform: str,
):
    """Enrich a list of repositories with latest commit metadata (Story #754).

    Accepts JSON body: {"clone_urls": ["https://..."]}
    Returns a dict mapping each clone_url to its commit info.
    """
    provider, error = await asyncio.to_thread(_resolve_provider, request, platform)
    if error is not None:
        return error

    try:
        body = await request.json()
    except Exception as exc:
        # Client sent malformed JSON — intentional client-error path, log at debug
        logger.debug("discovery_enrich: failed to parse JSON body: %s", exc)
        return JSONResponse(content={"error": "Invalid JSON body"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse(
            content={"error": "Request body must be a JSON object"}, status_code=400
        )

    clone_urls = body.get("clone_urls")
    if not isinstance(clone_urls, list):
        return JSONResponse(
            content={"error": "clone_urls must be a list"}, status_code=400
        )

    invalid = [u for u in clone_urls if not isinstance(u, str) or not u.strip()]
    if invalid:
        return JSONResponse(
            content={"error": "All clone_urls entries must be non-empty strings"},
            status_code=400,
        )

    if len(clone_urls) > MAX_ENRICH_CLONE_URLS:
        return JSONResponse(
            content={
                "error": f"clone_urls must contain at most {MAX_ENRICH_CLONE_URLS} entries"
            },
            status_code=400,
        )

    try:
        if not provider.is_configured():
            return JSONResponse(
                content={"error": f"{platform.capitalize()} token not configured"},
                status_code=422,
            )
        result = provider.enrich_repositories(clone_urls=clone_urls)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-045",
                f"Unexpected error in {platform} discovery/enrich: {e}",
            )
        )
        return JSONResponse(content={"error": str(e)}, status_code=500)


# =============================================================================
# Discovery Partial Routes (legacy HTMX partials — kept for backwards compat)
# =============================================================================


@web_router.get("/partials/auto-discovery/gitlab", response_class=HTMLResponse)
def gitlab_repos_partial(
    request: Request,
    cursor: Optional[str] = None,
    page_size: int = Query(default=50, ge=1, le=100),
    search: Optional[str] = None,
    show_hidden: bool = Query(default=False),
):
    """HTMX partial for GitLab repository discovery."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    from ...services.repository_providers.gitlab_provider import GitLabProviderError

    search_term = search.strip() if search else None

    try:
        provider = _get_gitlab_provider()
        if not provider.is_configured():
            return _build_gitlab_repos_response(
                request,
                error_type="not_configured",
                error_message="GitLab token not configured",
                search_term=search_term,
            )

        hidden_ids, all_hidden = _load_hidden_ids(show_hidden)
        result = provider.discover_repositories(
            cursor=cursor,
            page_size=page_size,
            search=search_term,
            hidden_identifiers=hidden_ids,
        )
        return _build_gitlab_repos_response(
            request,
            result.repositories,
            result.page_size,
            result.next_cursor,
            result.has_next_page,
            result.partial_due_to_cap,
            result.source_total,
            search_term=search_term,
            show_hidden=show_hidden,
            hidden_identifiers=all_hidden if show_hidden else set(),
        )
    except GitLabProviderError as e:
        logger.warning("GitLab provider error: %s", e)
        return _build_gitlab_repos_response(
            request,
            page_size=page_size,
            error_type="api_error",
            error_message=str(e),
            search_term=search_term,
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-042",
                f"Unexpected error in GitLab discovery: {e}",
            )
        )
        return _build_gitlab_repos_response(
            request,
            page_size=page_size,
            error_type="api_error",
            error_message=f"Unexpected error: {e}",
            search_term=search_term,
        )


@web_router.get("/partials/auto-discovery/github", response_class=HTMLResponse)
def github_repos_partial(
    request: Request,
    cursor: Optional[str] = None,
    page_size: int = Query(default=50, ge=1, le=100),
    search: Optional[str] = None,
    show_hidden: bool = Query(default=False),
):
    """HTMX partial for GitHub repository discovery."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    from ...services.repository_providers.github_provider import GitHubProviderError

    search_term = search.strip() if search else None

    try:
        provider = _get_github_provider()
        if not provider.is_configured():
            return _build_github_repos_response(
                request,
                error_type="not_configured",
                error_message="GitHub token not configured",
                search_term=search_term,
            )

        hidden_ids, all_hidden = _load_hidden_ids(show_hidden)
        result = provider.discover_repositories(
            cursor=cursor,
            page_size=page_size,
            search=search_term,
            hidden_identifiers=hidden_ids,
        )
        return _build_github_repos_response(
            request,
            result.repositories,
            result.page_size,
            result.next_cursor,
            result.has_next_page,
            result.partial_due_to_cap,
            result.source_total,
            search_term=search_term,
            show_hidden=show_hidden,
            hidden_identifiers=all_hidden if show_hidden else set(),
        )
    except GitHubProviderError as e:
        logger.warning("GitHub provider error: %s", e)
        error_msg = str(e)
        error_type = "rate_limit" if "rate limit" in error_msg.lower() else "api_error"
        return _build_github_repos_response(
            request,
            page_size=page_size,
            error_type=error_type,
            error_message=error_msg,
            search_term=search_term,
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-043",
                f"Unexpected error in GitHub discovery: {e}",
            )
        )
        return _build_github_repos_response(
            request,
            page_size=page_size,
            error_type="api_error",
            error_message=f"Unexpected error: {e}",
            search_term=search_term,
        )


# =============================================================================
# Discovery Hide/Unhide Endpoints (Story #719)
# =============================================================================


async def _validate_discovery_hide_request(
    request: Request,
) -> Tuple[Optional[HTMLResponse], str]:
    """Validate session, CSRF, and repo_identifier for hide/unhide endpoints.

    Returns (error_response, repo_identifier). If error_response is not None,
    the caller should return it immediately.
    """
    session = await asyncio.to_thread(_require_admin_session, request)
    if not session:
        return HTMLResponse(content="", status_code=status.HTTP_401_UNAUTHORIZED), ""

    form_data = await request.form()
    repo_identifier = str(form_data.get("repo_identifier", ""))
    csrf_token = str(form_data.get("csrf_token", ""))

    if not validate_login_csrf_token(request, csrf_token):
        return (
            HTMLResponse(
                content="Invalid CSRF token", status_code=status.HTTP_403_FORBIDDEN
            ),
            "",
        )

    if not repo_identifier:
        return (
            HTMLResponse(
                content="Missing repo_identifier",
                status_code=status.HTTP_400_BAD_REQUEST,
            ),
            "",
        )

    return None, repo_identifier


@web_router.post(
    "/api/discovery/hide",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
async def hide_discovery_repo(request: Request):
    """Hide a repository from the auto-discovery view (Story #719)."""
    error, repo_identifier = await _validate_discovery_hide_request(request)
    if error is not None:
        return error

    backend = _get_hidden_discovery_backend()
    backend.add_hidden_repo(repo_identifier)
    return HTMLResponse(content="", status_code=status.HTTP_200_OK)


@web_router.post(
    "/api/discovery/unhide",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
async def unhide_discovery_repo(request: Request):
    """Unhide a repository from the auto-discovery view (Story #719)."""
    error, repo_identifier = await _validate_discovery_hide_request(request)
    if error is not None:
        return error

    backend = _get_hidden_discovery_backend()
    backend.remove_hidden_repo(repo_identifier)
    return HTMLResponse(content="", status_code=status.HTTP_200_OK)


# =============================================================================
# Discovery Branches API Endpoint (Story #21)
# =============================================================================


@web_router.post(
    "/api/discovery/branches",
    dependencies=[Depends(dependencies.require_elevation())],
)
async def fetch_discovery_branches(request: Request):
    """
    Fetch branches for remote repositories during auto-discovery.

    This endpoint fetches available branches from remote git repositories
    and filters out issue-tracker pattern branches (e.g., SCM-1234, PROJ-567).

    Request body:
        {
            "repos": [
                {"clone_url": "https://github.com/org/repo.git", "platform": "github"},
                {"clone_url": "https://gitlab.com/org/repo.git", "platform": "gitlab"}
            ]
        }

    Response:
        {
            "https://github.com/org/repo.git": {
                "branches": ["main", "develop", "feature/login"],
                "default_branch": "main",
                "error": null
            },
            "https://gitlab.com/org/repo.git": {
                "branches": [],
                "default_branch": null,
                "error": "Repository not found"
            }
        }
    """
    # Require admin authentication
    session = await asyncio.to_thread(_require_admin_session, request)
    if not session:
        return JSONResponse(
            status_code=401,
            content={"error": "Authentication required"},
        )

    try:
        # Parse request body
        body = await request.json()
        repos = body.get("repos", [])

        if not isinstance(repos, list):
            return JSONResponse(
                status_code=422,
                content={"error": "repos must be a list"},
            )

        # Import and use RemoteBranchService
        from ...services.remote_branch_service import RemoteBranchService

        service = RemoteBranchService()

        # Get token manager to retrieve stored credentials
        token_manager = _get_token_manager()

        # Story #1491 AC3 (report Finding B3): fetch_remote_branches is
        # SYNCHRONOUS -- subprocess.run(["git","ls-remote",...], timeout=30).
        # Calling it in a plain sequential loop from this async route froze the
        # whole event loop for N x 30 s when N remotes were unreachable. Each
        # call is now offloaded to the default executor, with concurrency
        # BOUNDED by _DISCOVERY_BRANCH_FETCH_MAX_CONCURRENCY so a large repo
        # list can never fan out into an unbounded burst of git subprocesses.
        #
        # Semantics are deliberately unchanged: credential lookups still run
        # once per repo in the original order, the per-repo response dicts are
        # built from the same fields, the missing-clone_url entry still keys on
        # str(repo), results are inserted in the ORIGINAL request order (so
        # duplicate clone_urls collapse exactly as before), and an unexpected
        # exception still propagates to the outer handler as a 500. The 30 s
        # per-remote timeout lives inside fetch_remote_branches and is
        # untouched.
        loop = asyncio.get_running_loop()
        # Review item 11: the bound is PROCESS-WIDE, enforced by the size of the
        # shared pool below -- not by a per-request limiter, which bounded one
        # request while K concurrent requests still burst 8 x K git subprocesses.
        executor = _get_discovery_branch_fetch_executor()

        # Round 4: submission itself is budgeted by the process-wide gate added
        # alongside the pool. Without it, every planned fetch was handed to the
        # pool immediately and the surplus piled up in its unbounded internal
        # queue; now at most _DISCOVERY_BRANCH_FETCH_MAX_OUTSTANDING are ever in
        # the pool at once and the rest wait here -- suspended coroutines only,
        # no thread parked and no queue slot held.
        gate = _get_discovery_branch_fetch_gate()

        async def _fetch_one(
            clone_url: str, platform: str, credentials: Optional[str]
        ) -> Dict[str, Any]:
            try:
                await gate.acquire()
            except SubmissionGateOverloadedError as overloaded:
                # Story #1491 (review round 7): degrade THIS repository, never
                # the whole request. Letting the overload propagate discarded
                # the entire result set -- including every repository that had
                # already succeeded -- and turned an ordinary large-org
                # discovery into an HTTP 500. The shape here matches the
                # existing "Missing clone_url" degradation so the client
                # handles it through the same per-repo error path.
                logger.warning(
                    format_error_log(
                        "STORE-GENERAL-045",
                        "Branch discovery shed load for "
                        f"{mask_url_credentials(clone_url)}: {overloaded}",
                    )
                )
                return {
                    "branches": [],
                    "default_branch": None,
                    "error": "Server busy - branch discovery deferred, please retry",
                }
            try:
                result = await loop.run_in_executor(
                    executor,
                    functools.partial(
                        service.fetch_remote_branches,
                        clone_url=clone_url,
                        platform=platform,
                        credentials=credentials,
                    ),
                )
            finally:
                gate.release()
            return {
                "branches": result.branches,
                "default_branch": result.default_branch,
                "error": result.error,
            }

        # Pass 1: resolve response keys + credentials sequentially on the loop
        # (cheap local token reads). NO task is created in this pass -- that is
        # deliberate. token_manager.get_token() is a real credential-store read
        # that can raise; if tasks were created as we went, a failure on repo N
        # would leave the tasks for repos 0..N-1 running and never awaited
        # (unretrieved exceptions, git subprocesses outliving the response).
        # Planning fully first means an exception here escapes with zero tasks
        # in flight.
        # Story #1491 (review item 2): CITokenManager.get_token() performs real
        # SYNCHRONOUS backend access, so calling it from this async route put
        # blocking I/O straight back onto the event loop -- the exact defect
        # class this story exists to remove, and it would have negated the
        # subprocess offload below on any slow credential store. It is now
        # offloaded to a worker thread. Each distinct platform is resolved
        # exactly ONCE per request rather than once per repository, so a
        # 50-repo GitHub discovery does one token read instead of fifty.
        requested_platforms = {
            repo.get("platform", "github") for repo in repos if repo.get("clone_url")
        }
        credentials_by_platform: Dict[str, Optional[str]] = {}
        for platform_name in sorted(requested_platforms):
            if platform_name not in ("github", "gitlab"):
                # Unknown platform: same as before -- no credentials attached.
                credentials_by_platform[platform_name] = None
                continue
            token_data = await asyncio.to_thread(token_manager.get_token, platform_name)
            credentials_by_platform[platform_name] = (
                token_data.token if token_data else None
            )

        plan: List[Tuple[str, Optional[Tuple[str, str, Optional[str]]]]] = []
        for repo in repos:
            clone_url = repo.get("clone_url")
            platform = repo.get("platform", "github")

            if not clone_url:
                plan.append((str(repo), None))
                continue

            credentials = credentials_by_platform.get(platform)
            plan.append((clone_url, (clone_url, platform, credentials)))

        # The plan is complete, so task creation below can no longer be
        # interleaved with anything that raises (a credential-store failure
        # escapes with zero tasks in flight).
        planned = plan

        # Pass 2: run the fetches in sequential WINDOWS, each at most
        # _DISCOVERY_BRANCH_FETCH_MAX_CONCURRENCY wide.
        #
        # Story #1491 (review round 7): bounding the fan-out HERE, at the
        # source, is what makes a large discovery safe. Creating one task per
        # repository up front and letting them all pile up on the submission
        # gate meant a 100-repo request -- an ordinary large-org discovery --
        # queued far more waiters than the gate would hold and was rejected
        # outright, discarding every repository that had already succeeded.
        # Windowing means this request never has more outstanding acquires
        # than the gate's own capacity, so it bounds its own memory AND never
        # provokes rejection on its own behalf. The per-repo degradation in
        # _fetch_one remains only as a cross-request safety net.
        #
        # Within a window, return_exceptions=True guarantees EVERY task is
        # awaited, so no sibling is orphaned with an unretrieved exception when
        # one fetch fails. A genuine failure is then re-raised in ORIGINAL
        # request order, surfacing to the outer handler exactly as before.
        results: Dict[str, Dict[str, Any]] = {}
        window: List[Tuple[str, "asyncio.Future"]] = []

        async def _drain_window() -> None:
            if not window:
                return
            await asyncio.gather(*(task for _, task in window), return_exceptions=True)
            for window_key, task in window:
                task_error = task.exception()
                if task_error is not None:
                    raise task_error
                results[window_key] = task.result()
            window.clear()

        for key, fetch_args in planned:
            if fetch_args is None:
                results[key] = {
                    "branches": [],
                    "default_branch": None,
                    "error": "Missing clone_url",
                }
                continue
            window.append((key, asyncio.ensure_future(_fetch_one(*fetch_args))))
            if len(window) >= _DISCOVERY_BRANCH_FETCH_MAX_CONCURRENCY:
                await _drain_window()
        await _drain_window()

        return JSONResponse(content=results)

    except json.JSONDecodeError:
        return JSONResponse(
            status_code=422,
            content={"error": "Invalid JSON in request body"},
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-044",
                f"Error fetching discovery branches: {e}",
            ),
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"error": f"Internal server error: {str(e)}"},
        )
