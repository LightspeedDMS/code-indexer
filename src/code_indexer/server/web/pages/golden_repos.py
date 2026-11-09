"""
Admin golden repository pages.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

from code_indexer.server.middleware.correlation import get_correlation_id
import asyncio
import functools
import json
import os
import re as _re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Union, cast

if TYPE_CHECKING:
    from datetime import datetime
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from ...auth import dependencies
from ..auth import SessionData
from ...services.config_service import get_config_service
from ...services.golden_repo_audited_ops import request_golden_repo_refresh
from code_indexer.server.logging_utils import format_error_log, get_log_extra
from code_indexer.utils.credential_redaction import (
    mask_url_credentials,
    with_masked_repo_url,
)

from ._shared import (
    _create_login_redirect,
    _get_activated_repo_manager,
    _get_golden_repo_manager,
    _get_repo_category_service,
    _get_users_list,
    _require_admin_session,
    generate_csrf_token,
    get_csrf_token_from_cookie,
    logger,
    set_csrf_cookie,
    templates,
    validate_login_csrf_token,
)

web_router = APIRouter()

# ==============================================================================
# Groups Management Routes (Story #710: Admin User and Group Management Interface)
# ==============================================================================


def _safe_ts_slice(val: Optional[Union[str, "datetime"]], end: int) -> Optional[str]:
    """Safely slice a timestamp that may be str or datetime (PG vs SQLite compat).

    PG returns native datetime objects for timestamp columns; SQLite returns strings.

    Args:
        val: Timestamp value — str, datetime, or None.
        end: Number of characters to keep (must be >= 0).

    Returns:
        Sliced string, or None when val is None.
    """
    if end < 0:
        raise ValueError(f"_safe_ts_slice: end must be >= 0, got {end!r}")
    if val is None:
        return None
    from datetime import datetime

    if isinstance(val, datetime):
        return val.isoformat()[:end]
    return str(val)[:end]


def _golden_repo_not_found_error():
    """Lazy import of GoldenRepoNotFoundError to avoid circular imports."""
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoNotFoundError,
    )

    return GoldenRepoNotFoundError


# Module-level lazy sentinel for exception matching
_GoldenRepoNotFoundError = type("_LazyError", (Exception,), {})
try:
    from code_indexer.server.repositories.golden_repo_manager import (  # type: ignore[no-redef]
        GoldenRepoNotFoundError as _GoldenRepoNotFoundError,
    )
except ImportError:
    pass


def _get_golden_repo_branch_service():
    """Create golden repo branch service on-demand (may return None)."""
    manager = _get_golden_repo_manager()
    if not manager:
        return None
    try:
        from code_indexer.server.services.golden_repo_branch_service import (
            GoldenRepoBranchService,
        )

        return GoldenRepoBranchService(manager)
    except ImportError:
        return None


def generate_unique_alias(
    repo_name: str,
    golden_repo_manager,
    *,
    existing_aliases: Optional[Set[str]] = None,
) -> str:
    """
    Generate unique alias from repository name.

    Examples:
        "org/my-project" -> "my-project"
        "group/subgroup/project" -> "project"

    If alias exists, add suffix: "project-2", "project-3", etc.

    Args:
        repo_name: Repository name (may include path components like org/project)
        golden_repo_manager: GoldenRepoManager instance to check for conflicts
        existing_aliases: Optional pre-built set of existing aliases. When
            provided, list_golden_repos() is NOT called — callers that process
            many repos in a loop should hoist that call once and pass the result
            here (Story #1092: batch-create fast path).

    Returns:
        Unique alias string (lowercase, special chars replaced with dashes)
    """
    import re

    # Extract project name (last path component)
    base_alias = repo_name.split("/")[-1]

    # Clean up: lowercase, replace special chars with dashes
    base_alias = re.sub(r"[^a-z0-9-]", "-", base_alias.lower())

    # Collapse multiple dashes into one
    base_alias = re.sub(r"-+", "-", base_alias)

    # Remove leading/trailing dashes
    base_alias = base_alias.strip("-")

    # Handle empty result
    if not base_alias:
        base_alias = "repo"

    # Use pre-built set when provided (Story #1092: avoid N × list_golden_repos
    # calls in batch operations), otherwise fall back to the live query.
    if existing_aliases is None:
        existing_repos = golden_repo_manager.list_golden_repos()
        alias_set: Set[str] = {r["alias"] for r in existing_repos}
    else:
        alias_set = existing_aliases

    if base_alias not in alias_set:
        return base_alias

    # Add numeric suffix
    suffix = 2
    while f"{base_alias}-{suffix}" in alias_set:
        suffix += 1

    return f"{base_alias}-{suffix}"


MAX_BATCH_CREATE_REPOS = 50
MAX_BATCH_CREATE_ERROR_LENGTH = 200
_ERROR_TRUNCATION_SUFFIX = "..."


def _sanitize_batch_create_error(error: Exception) -> str:
    """Trim error message to <=MAX_BATCH_CREATE_ERROR_LENGTH chars, replace newlines."""
    message = str(error).replace("\n", " ").strip()
    if len(message) > MAX_BATCH_CREATE_ERROR_LENGTH:
        cutoff = MAX_BATCH_CREATE_ERROR_LENGTH - len(_ERROR_TRUNCATION_SUFFIX)
        message = message[:cutoff] + _ERROR_TRUNCATION_SUFFIX
    return message


def _batch_create_repos(
    repos: List[Dict[str, str]],
    submitter_username: str,
    golden_repo_manager,
) -> Dict[str, Any]:
    """
    Create multiple golden repositories from discovered repos.

    Args:
        repos: List of repo objects with clone_url, alias, branch, platform
        submitter_username: Username of the admin submitting the batch
        golden_repo_manager: GoldenRepoManager instance

    Returns:
        Dict with success, results array, and summary string
    """
    results = []

    # Story #1092: Hoist the alias-conflict check to a single call before the
    # loop so that a batch of N repos pays O(1) DB reads instead of O(N).
    existing_repos = golden_repo_manager.list_golden_repos()
    existing_aliases_set: Set[str] = {r["alias"] for r in existing_repos}

    for repo_data in repos:
        try:
            # Generate unique alias from repo name using the pre-built set so
            # we don't call list_golden_repos() once per repo (Story #1092).
            alias = generate_unique_alias(
                repo_data["alias"],
                golden_repo_manager,
                existing_aliases=existing_aliases_set,
            )
            # Register the newly chosen alias so later iterations in this batch
            # don't collide with it.
            existing_aliases_set.add(alias)

            # Create golden repo — skip per-repo git network validation so
            # the endpoint returns immediately with all job IDs (Story #1092).
            job_id = golden_repo_manager.add_golden_repo(
                repo_url=repo_data["clone_url"],
                alias=alias,
                default_branch=repo_data.get("branch") or None,
                submitter_username=submitter_username,
                skip_pre_flight_git_validation=True,
            )

            results.append(
                {
                    "alias": alias,
                    "status": "success",
                    "job_id": job_id,
                    "clone_url": repo_data.get("clone_url", ""),
                }
            )
        except Exception as e:
            logger.warning(
                format_error_log(
                    "WEB-GENERAL-067",
                    "Batch golden repo create failed",
                    repo_alias=repo_data.get("alias", "unknown"),
                    repo_url=mask_url_credentials(
                        repo_data.get("clone_url", "unknown")
                    ),
                    submitter=submitter_username,
                ),
                extra=get_log_extra("WEB-GENERAL-067"),
                exc_info=True,
            )
            results.append(
                {
                    "alias": repo_data.get("alias", "unknown"),
                    "status": "failed",
                    "error": _sanitize_batch_create_error(e),
                    "clone_url": repo_data.get("clone_url", ""),
                }
            )

    success_count = len([r for r in results if r["status"] == "success"])
    failed_count = len([r for r in results if r["status"] == "failed"])

    return {
        "success": failed_count == 0,
        "results": results,
        "summary": f"{success_count} succeeded, {failed_count} failed",
    }


_SAFE_ALIAS_MAX_LEN = 127
_SAFE_ALIAS_RE = _re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0," + str(_SAFE_ALIAS_MAX_LEN - 1) + r"}$"
)


def _is_safe_alias(alias: Optional[str]) -> bool:
    """Return True iff alias is a safe filesystem path component (no traversal risk)."""
    if not isinstance(alias, str) or not alias:
        return False
    return bool(_SAFE_ALIAS_RE.match(alias))


def _resolve_alias_metadata(
    alias: str, global_repos: dict, aliases_dir: "Path"
) -> tuple:
    """
    Return (global_alias, globally_queryable, version, last_refresh, index_path).

    version and last_refresh are initialized to None before branching — never duplicated.
    """
    from code_indexer.global_repos.alias_manager import AliasManager

    version = None
    last_refresh = None
    index_path = None

    if alias in global_repos:
        global_alias = global_repos[alias]["alias_name"]
        globally_queryable = True
        if _is_safe_alias(alias):
            try:
                alias_manager = AliasManager.existing(aliases_dir)
                alias_data = (
                    alias_manager.read_record(f"{alias}-global")
                    if alias_manager is not None
                    else None
                )
                if alias_data is not None:
                    index_path = alias_data.get("target_path")
                    if index_path and ".versioned" in index_path:
                        version = Path(index_path).name
                    raw_refresh = alias_data.get("last_refresh", "")
                    last_refresh = (
                        _safe_ts_slice(raw_refresh, 19) if raw_refresh else None
                    )
            except Exception as e:
                logger.warning(
                    format_error_log(
                        "SCIP-GENERAL-045",
                        f"Could not read alias pointer for {alias}: {e}",
                    ),
                    extra={"correlation_id": get_correlation_id()},
                )
    else:
        global_alias = None
        globally_queryable = False
        index_path = None  # clone_path added by caller for non-global repos

    return global_alias, globally_queryable, version, last_refresh, index_path


def _golden_repos_dir_from_env() -> Path:
    """Resolve the golden-repos root from CIDX_SERVER_DATA_DIR.

    Mirrors the SAME `os.environ.get("CIDX_SERVER_DATA_DIR",
    os.path.expanduser("~/.cidx-server"))` bootstrap-config convention
    already used at multiple other call sites in this module (e.g.
    `_get_golden_repos_list`, `_get_single_repo_enriched`) -- extracted
    here as a single named helper rather than a fourth inline copy, for
    GitHub Issue #1459 AC4's resolver-aware temporal detection.
    """
    server_data_dir = os.environ.get(
        "CIDX_SERVER_DATA_DIR", os.path.expanduser("~/.cidx-server")
    )
    return Path(server_data_dir) / "data" / "golden-repos"


def _detect_index_flags(
    index_path: Optional[str], repo_alias: Optional[str] = None
) -> dict:
    """
    Return has_semantic/has_fts/has_temporal/has_scip flags via filesystem inspection.

    All flags default False when index_path is None or the .code-indexer dir is absent.

    Args:
        index_path: Golden repo clone (or resolved global index) path.
        repo_alias: The golden repo's BARE alias. When provided AND the
            local-clone temporal scan finds nothing, temporal detection
            additionally routes through the shared TemporalShardResolver-
            based get_temporal_repo_status() helper (GitHub Issue #1459
            AC4) so temporal data relocated to Story #1457's sister
            location is still detected, never a false "not indexed".
    """
    flags = {
        "has_semantic": False,
        "has_fts": False,
        "has_temporal": False,
        "has_scip": False,
    }
    if not index_path:
        return flags
    index_base = Path(index_path) / ".code-indexer"
    if not index_base.exists():
        return flags
    index_dir = index_base / "index"
    if index_dir.exists():
        for model_dir in index_dir.iterdir():
            if model_dir.is_dir() and (model_dir / "hnsw_index.bin").exists():
                flags["has_semantic"] = True
                break
    tantivy_dir = index_base / "tantivy_index"
    if tantivy_dir.exists() and any(tantivy_dir.iterdir()):
        flags["has_fts"] = True
    from code_indexer.services.temporal.temporal_collection_naming import (
        is_temporal_collection as _is_temporal,
    )

    temporal_dir = next(
        (
            d
            for d in sorted(index_dir.iterdir() if index_dir.is_dir() else [])
            if d.is_dir() and _is_temporal(d.name) and (d / "hnsw_index.bin").exists()
        ),
        None,
    )
    if temporal_dir is not None:
        flags["has_temporal"] = True
    elif repo_alias:
        # Resolver-aware detection (GitHub Issue #1459 AC4): routes through
        # the SAME TemporalShardResolver/catalog mechanism the query path
        # uses, never a parallel sister-root scan.
        from code_indexer.services.temporal.temporal_status import (
            get_temporal_repo_status,
        )

        temporal_status = get_temporal_repo_status(
            golden_repos_dir=_golden_repos_dir_from_env(),
            repo_alias=repo_alias,
            legacy_index_path=index_dir,
        )
        flags["has_temporal"] = temporal_status.is_queryable
    scip_dir = (
        index_base / "scip"
    )  # CRITICAL: only .scip.db persists after cidx scip generate
    if scip_dir.exists() and list(scip_dir.glob("**/*.scip.db")):
        flags["has_scip"] = True
    return flags


def _load_temporal_status(global_alias: Optional[str], repo_alias: str) -> dict:
    """
    Fetch temporal index status for a globally-activated repo.

    repo_alias is used only in warning logs to preserve original log context.
    Returns {"format": "none"} when global_alias is absent.
    """
    if not global_alias:
        return {"format": "none"}
    try:
        from code_indexer.server.services.dashboard_service import DashboardService

        # cast: DashboardService return type is too broad for mypy; narrowing to route contract
        return cast(
            Dict[str, Any],
            DashboardService().get_temporal_index_status(
                username="_global", repo_alias=global_alias
            ),
        )
    except Exception as e:
        logger.warning(
            format_error_log(
                "SCIP-GENERAL-046",
                f"Failed to get temporal status for {repo_alias}: {e}",
            ),
            extra={"correlation_id": get_correlation_id()},
        )
        return {"format": "error", "message": str(e)}


def _build_category_lookup() -> dict:
    """
    Build alias -> {category_id, category_name, category_priority} from RepoCategoryService.

    Returns empty dict on failure; category data is non-critical display information.
    """
    try:
        repo_map = _get_repo_category_service().get_repo_category_map()
        return {
            alias: {
                "category_id": info["category_id"],
                "category_name": info["category_name"],
                "category_priority": info["priority"],
            }
            for alias, info in repo_map.items()
            if info.get("category_name")
        }
    except Exception as e:
        logger.warning(
            format_error_log(
                "SCIP-GENERAL-048", f"Could not load category information: {e}"
            ),
            extra={"correlation_id": get_correlation_id()},
        )
        return {}


def _get_golden_repos_list(backend_registry=None):
    """Get list of all golden repositories with global alias, version, and index info."""
    try:
        manager = _get_golden_repo_manager()
        # Repository URLs are rendered with their userinfo redacted.
        repos = [with_masked_repo_url(r) for r in manager.list_golden_repos()]
        server_data_dir = os.environ.get(
            "CIDX_SERVER_DATA_DIR", os.path.expanduser("~/.cidx-server")
        )
        aliases_dir = Path(server_data_dir) / "data" / "golden-repos" / "aliases"
        if backend_registry is not None:
            global_repos = {
                v["repo_name"]: v
                for v in backend_registry.global_repos.list_repos().values()
            }
        else:
            global_repos = {}
        category_lookup = _build_category_lookup()
        for repo in repos:
            if "status" not in repo:
                repo["status"] = "ready"
            repo["last_indexed"] = _safe_ts_slice(repo.get("created_at"), 10)
            alias = repo.get("alias", "")
            g_alias, g_queryable, version, last_refresh, index_path = (
                _resolve_alias_metadata(alias, global_repos, aliases_dir)
            )
            repo.update(
                {
                    "global_alias": g_alias,
                    "globally_queryable": g_queryable,
                    "version": version,
                    "last_refresh": last_refresh,
                }
            )
            if not g_queryable:
                index_path = repo.get("clone_path")
            # temporal_status deferred to details partial (_get_single_repo_enriched)
            # to avoid per-alias overhead on initial page load (Finding 3 / AC1).
            repo.update(_detect_index_flags(index_path, repo_alias=alias))
            cat = category_lookup.get(alias, {})
            repo.update(
                {
                    "category_id": cat.get("category_id"),
                    "category_name": cat.get("category_name"),
                    "category_priority": cat.get("category_priority"),
                }
            )
        return sorted(repos, key=lambda r: r.get("alias", "").lower())
    except Exception as e:
        logger.error(
            format_error_log(
                "SCIP-GENERAL-047", f"Failed to get golden repos list: {e}"
            ),
            exc_info=True,
            extra={"correlation_id": get_correlation_id()},
        )
        return []


def _create_golden_repos_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HTMLResponse:
    """Create golden repos page response with all necessary context."""
    csrf_token = generate_csrf_token()
    backend_registry = getattr(request.app.state, "backend_registry", None)
    repos = _get_golden_repos_list(backend_registry)
    users = _get_users_list()

    # Get categories for dropdown (Story #183)
    try:
        category_service = _get_repo_category_service()
        categories = category_service.list_categories()
    except Exception as e:
        logger.warning(
            format_error_log(
                "SCIP-GENERAL-049", f"Could not load categories for template: {e}"
            ),
            extra={"correlation_id": get_correlation_id()},
        )
        categories = []

    response = templates.TemplateResponse(
        request,
        "golden_repos.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "golden-repos",
            "show_nav": True,
            "csrf_token": csrf_token,
            "repos": repos,
            "users": [{"username": u.username} for u in users],
            "categories": categories,
            "success_message": success_message,
            "error_message": error_message,
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


@web_router.get("/golden-repos", response_class=HTMLResponse)
def golden_repos_page(request: Request):
    """Golden repositories management page - list all golden repos with CRUD operations."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_golden_repos_page_response(request, session)


@web_router.post(
    "/golden-repos/add",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def add_golden_repo(
    request: Request,
    alias: str = Form(...),
    repo_url: str = Form(...),
    default_branch: str = Form(""),
    csrf_token: Optional[str] = Form(None),
):
    """Add a new golden repository."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_golden_repos_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Validate inputs
    if not alias or not alias.strip():
        return _create_golden_repos_page_response(
            request, session, error_message="Repository name is required"
        )

    if not repo_url or not repo_url.strip():
        return _create_golden_repos_page_response(
            request, session, error_message="Repository path/URL is required"
        )

    # Try to add the repository
    try:
        manager = _get_golden_repo_manager()
        job_id = manager.add_golden_repo(
            repo_url=repo_url.strip(),
            alias=alias.strip(),
            default_branch=default_branch.strip() or None,
            submitter_username=session.username,
        )
        return _create_golden_repos_page_response(
            request,
            session,
            success_message=f"Repository '{alias}' add job submitted (Job ID: {job_id})",
        )
    except Exception as e:
        error_msg = str(e)
        # Handle common error cases
        if "already exists" in error_msg.lower():
            error_msg = f"Repository alias '{alias}' already exists"
        elif "invalid" in error_msg.lower() or "inaccessible" in error_msg.lower():
            error_msg = (
                f"Invalid or inaccessible repository: {mask_url_credentials(repo_url)}"
            )
        return _create_golden_repos_page_response(
            request, session, error_message=error_msg
        )


@web_router.post(
    "/golden-repos/batch-create",
    dependencies=[Depends(dependencies.require_elevation())],
)
def batch_create_golden_repos(
    request: Request,
    repos: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """
    Create multiple golden repositories from discovered repos.

    Body params:
        repos: JSON array of objects with:
            - clone_url: Repository URL
            - alias: Generated alias (project name)
            - branch: Default branch
            - platform: gitlab or github
        csrf_token: CSRF token for validation
    """
    session = _require_admin_session(request)
    if not session:
        return JSONResponse(
            {"success": False, "error": "Authentication required"},
            status_code=401,
        )

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return JSONResponse(
            {"success": False, "error": "Invalid CSRF token"},
            status_code=403,
        )

    # Parse JSON repos array
    try:
        repo_list = json.loads(repos)
    except json.JSONDecodeError as e:
        return JSONResponse(
            {"success": False, "error": f"Invalid JSON: {e}"},
            status_code=400,
        )

    if not isinstance(repo_list, list):
        return JSONResponse(
            {"success": False, "error": "repos must be a JSON array"},
            status_code=400,
        )

    if len(repo_list) > MAX_BATCH_CREATE_REPOS:
        return JSONResponse(
            {
                "success": False,
                "error": f"Maximum batch size is {MAX_BATCH_CREATE_REPOS} repositories",
            },
            status_code=400,
        )

    # Process batch creation
    manager = _get_golden_repo_manager()
    results = _batch_create_repos(repo_list, session.username, manager)

    return JSONResponse(results)


@web_router.post(
    "/golden-repos/{alias}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def delete_golden_repo(
    request: Request,
    alias: str,
    csrf_token: Optional[str] = Form(None),
):
    """Delete a golden repository."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_golden_repos_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Try to delete the repository
    try:
        manager = _get_golden_repo_manager()
        job_id = manager.remove_golden_repo(
            alias=alias,
            submitter_username=session.username,
        )
        return _create_golden_repos_page_response(
            request,
            session,
            success_message=f"Repository '{alias}' deletion job submitted (Job ID: {job_id})",
        )
    except Exception as e:
        error_msg = str(e)
        if "not found" in error_msg.lower():
            error_msg = f"Repository '{alias}' not found"
        return _create_golden_repos_page_response(
            request, session, error_message=error_msg
        )


@web_router.post(
    "/golden-repos/{alias}/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def refresh_golden_repo(
    request: Request,
    alias: str,
    csrf_token: Optional[str] = Form(None),
):
    """Refresh (re-index) a golden repository."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_golden_repos_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Try to refresh the repository
    try:
        manager = _get_golden_repo_manager()
        # Validate repo exists before scheduling.
        # Bug #1481: use get_golden_repo() (authoritative shared-backend
        # read), NOT the raw per-worker `golden_repos` cache dict -- in a
        # cluster, a repo registered/refreshed on another node/worker is
        # invisible to this worker's cache, causing false "not found" errors.
        if manager.get_golden_repo(alias) is None:
            raise Exception(f"Repository '{alias}' not found")
        # Delegate to RefreshScheduler (index-source-first versioned pipeline)
        from code_indexer.server import app as app_module

        lifecycle_manager = getattr(
            cast(Any, app_module.app).state, "global_lifecycle_manager", None
        )
        if not lifecycle_manager or not lifecycle_manager.refresh_scheduler:
            raise Exception("RefreshScheduler not available")
        # Resolution from bare alias to global format happens inside RefreshScheduler
        job_id = request_golden_repo_refresh(
            lifecycle_manager.refresh_scheduler, alias, actor=session.username
        )
        return _create_golden_repos_page_response(
            request,
            session,
            success_message=f"Repository '{alias}' refresh job submitted (Job ID: {job_id})",
        )
    except Exception as e:
        error_msg = str(e)
        if "not found" in error_msg.lower():
            error_msg = f"Repository '{alias}' not found"
        return _create_golden_repos_page_response(
            request, session, error_message=error_msg
        )


@web_router.post(
    "/golden-repos/{alias}/force-resync",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def force_resync_golden_repo(
    request: Request,
    alias: str,
    csrf_token: Optional[str] = Form(None),
):
    """Force re-sync (reset and re-index) a golden repository, discarding local divergence."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_golden_repos_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Try to force re-sync the repository
    try:
        manager = _get_golden_repo_manager()
        # Validate repo exists before scheduling.
        # Bug #1481: use get_golden_repo() (authoritative shared-backend
        # read), NOT the raw per-worker `golden_repos` cache dict -- in a
        # cluster, a repo registered/refreshed on another node/worker is
        # invisible to this worker's cache, causing false "not found" errors.
        if manager.get_golden_repo(alias) is None:
            raise Exception(f"Repository '{alias}' not found")
        # Delegate to RefreshScheduler with force_reset=True
        from code_indexer.server import app as app_module

        lifecycle_manager = getattr(
            cast(Any, app_module.app).state, "global_lifecycle_manager", None
        )
        if not lifecycle_manager or not lifecycle_manager.refresh_scheduler:
            raise Exception("RefreshScheduler not available")
        # force_reset=True discards divergent local state before re-indexing
        job_id = request_golden_repo_refresh(
            lifecycle_manager.refresh_scheduler,
            alias,
            actor=session.username,
            force_reset=True,
        )
        return _create_golden_repos_page_response(
            request,
            session,
            success_message=f"Repository '{alias}' force re-sync job submitted (Job ID: {job_id})",
        )
    except Exception as e:
        error_msg = str(e)
        if "not found" in error_msg.lower():
            error_msg = f"Repository '{alias}' not found"
        return _create_golden_repos_page_response(
            request, session, error_message=error_msg
        )


@web_router.post(
    "/golden-repos/{alias}/temporal-options",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def save_temporal_options(
    request: Request,
    alias: str,
    max_commits: Optional[str] = Form(None),
    diff_context: Optional[str] = Form(None),
    since_date: Optional[str] = Form(None),
    all_branches: Optional[str] = Form(None),
    csrf_token: Optional[str] = Form(None),
):
    """Save temporal indexing options for a golden repo (Story #478)."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)
    if not validate_login_csrf_token(request, csrf_token):
        return templates.TemplateResponse(
            request,
            "partials/error_message.html",
            {"request": request, "error": "Invalid CSRF token"},
            status_code=400,
        )

    options: Dict[str, Any] = {}

    if max_commits and max_commits.strip():
        try:
            mc = int(max_commits.strip())
            if mc < 1:
                raise ValueError("max_commits must be a positive integer")
            options["max_commits"] = mc
        except ValueError:
            return templates.TemplateResponse(
                request,
                "partials/error_message.html",
                {"request": request, "error": "max_commits must be a positive integer"},
                status_code=400,
            )

    if diff_context and diff_context.strip():
        try:
            dc = int(diff_context.strip())
            if dc < 0 or dc > 50:
                raise ValueError("out of range")
            options["diff_context"] = dc
        except ValueError:
            return templates.TemplateResponse(
                request,
                "partials/error_message.html",
                {"request": request, "error": "diff_context must be an integer 0-50"},
                status_code=400,
            )

    if since_date and since_date.strip():
        since_date_val = since_date.strip()
        try:
            from datetime import datetime

            datetime.strptime(since_date_val, "%Y-%m-%d")
        except ValueError:
            return templates.TemplateResponse(
                request,
                "partials/error_message.html",
                {
                    "request": request,
                    "error": "since_date must be in YYYY-MM-DD format",
                },
                status_code=400,
            )
        options["since_date"] = since_date_val

    options["all_branches"] = all_branches == "1"

    # Story #1412: golden/server temporal all-branches indexing is gated
    # behind a server-wide runtime flag, shipped OFF by default. Reject
    # loudly here rather than silently downgrading/dropping the request.
    if options["all_branches"]:
        _gate_config = get_config_service().get_config()
        _gate_enabled = bool(
            getattr(
                _gate_config.indexing_config, "temporal_all_branches_enabled", False
            )
        )
        if not _gate_enabled:
            return templates.TemplateResponse(
                request,
                "partials/error_message.html",
                {
                    "request": request,
                    "error": (
                        "All-branches temporal indexing is disabled on this "
                        "server (temporal_all_branches_enabled=false). "
                        "Contact your administrator to enable it."
                    ),
                },
                status_code=400,
            )

    manager = _get_golden_repo_manager()
    manager.save_temporal_options(alias, options)

    return _create_golden_repos_page_response(
        request, session, success_message="Temporal options saved successfully"
    )


@web_router.post(
    "/golden-repos/{alias}/change-branch",
    dependencies=[Depends(dependencies.require_elevation())],
)
async def change_golden_repo_branch(
    request: Request,
    alias: str,
):
    """Change the active branch of a golden repository async (Story #308)."""
    session = await asyncio.to_thread(_require_admin_session, request)
    if not session:
        return JSONResponse(
            {"success": False, "error": "Authentication required"},
            status_code=401,
        )

    try:
        body = await request.json()
        branch = body.get("branch")
        if not branch:
            return JSONResponse(
                status_code=400,
                content={"error": "Missing required field: branch"},
            )

        manager = _get_golden_repo_manager()
        # The whole audited submission (DB reads, job submit, audit row)
        # runs off the event loop.
        result = await asyncio.to_thread(
            functools.partial(
                manager.change_branch_async,
                alias,
                branch,
                submitter_username=session.username,
            )
        )
        job_id = result.get("job_id")
        if job_id is None:
            # Already on the target branch - no background job needed
            return JSONResponse(
                status_code=200,
                content={"success": True, "message": f"Already on branch '{branch}'"},
            )
        return JSONResponse(
            status_code=202,
            content={"success": True, "job_id": job_id},
        )

    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except (FileNotFoundError, _GoldenRepoNotFoundError) as e:  # type: ignore[misc]
        return JSONResponse(status_code=404, content={"error": str(e)})
    except Exception as e:
        from code_indexer.server.repositories.background_jobs import DuplicateJobError

        if isinstance(e, DuplicateJobError):
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "error": str(e),
                    "existing_job_id": e.existing_job_id,
                },
            )
        logger.error(f"Branch change failed for {alias}: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Branch change failed: {e}"},
        )


@web_router.get("/golden-repos/{alias}/branches")
def get_golden_repo_branches(
    request: Request,
    alias: str,
):
    """Get list of branches for a golden repository (Story #303, AC5)."""
    session = _require_admin_session(request)
    if not session:
        return JSONResponse(
            {"success": False, "error": "Authentication required"},
            status_code=401,
        )

    try:
        branch_service = _get_golden_repo_branch_service()
        if not branch_service:
            return JSONResponse(
                status_code=500,
                content={"error": "Branch service not available"},
            )
        branches = branch_service.get_golden_repo_branches(alias)
        return JSONResponse(
            content={
                "branches": [
                    {
                        "name": b.name,
                        "last_commit_hash": b.last_commit_hash,
                        "last_commit_author": b.last_commit_author,
                        "branch_type": b.branch_type,
                        "is_default": b.is_default,
                    }
                    for b in branches
                ]
            }
        )
    except (FileNotFoundError, _GoldenRepoNotFoundError) as e:  # type: ignore[misc]
        return JSONResponse(status_code=404, content={"error": str(e)})
    except Exception as e:
        logger.error(f"Failed to get branches for {alias}: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@web_router.post(
    "/golden-repos/activate",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def activate_golden_repo(
    request: Request,
    golden_alias: str = Form(...),
    username: str = Form(...),
    user_alias: str = Form(""),
    csrf_token: Optional[str] = Form(None),
):
    """Activate a golden repository for a user."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_golden_repos_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Validate inputs
    if not golden_alias or not golden_alias.strip():
        return _create_golden_repos_page_response(
            request, session, error_message="Golden repository alias is required"
        )

    if not username or not username.strip():
        return _create_golden_repos_page_response(
            request, session, error_message="Username is required"
        )

    # Use golden_alias as user_alias if not provided
    effective_user_alias = (
        user_alias.strip() if user_alias.strip() else golden_alias.strip()
    )

    # Try to activate the repository
    from code_indexer.server.services.activated_repo_audited_ops import (
        activate_repository_for_user,
    )

    try:
        job_id = activate_repository_for_user(
            _get_activated_repo_manager(),
            username.strip(),
            golden_alias.strip(),
            user_alias=effective_user_alias,
            actor=session.username,
        )
        return _create_golden_repos_page_response(
            request,
            session,
            success_message=f"Repository '{golden_alias}' activation for user '{username}' submitted (Job ID: {job_id})",
        )
    except Exception as e:
        error_msg = str(e)
        if "not found" in error_msg.lower():
            error_msg = f"Golden repository '{golden_alias}' not found"
        elif "already" in error_msg.lower():
            error_msg = f"User '{username}' already has this repository activated"
        return _create_golden_repos_page_response(
            request, session, error_message=error_msg
        )


@web_router.get("/golden-repos/{alias}/details", response_class=HTMLResponse)
def golden_repo_details(
    request: Request,
    alias: str,
):
    """Get details for a specific golden repository."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    try:
        manager = _get_golden_repo_manager()
        repo = manager.get_golden_repo(alias)
        if not repo:
            raise HTTPException(
                status_code=404, detail=f"Repository '{alias}' not found"
            )

        # Return repository details as JSON-like HTML response
        # Get existing CSRF token from cookie or generate new one
        csrf_token = get_csrf_token_from_cookie(request) or generate_csrf_token()

        response = templates.TemplateResponse(
            request,
            "partials/golden_repos_list.html",
            {
                "request": request,
                "csrf_token": csrf_token,
                "repos": [repo.to_dict()],
            },
        )

        # Set CSRF cookie to ensure token is available for form submission
        set_csrf_cookie(response, csrf_token)
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-026",
                f"Failed to get golden repo details for '{alias}': {e}",
            ),
            exc_info=True,
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(status_code=404, detail=f"Repository '{alias}' not found")


@web_router.get("/partials/golden-repos-list", response_class=HTMLResponse)
def golden_repos_list_partial(request: Request):
    """
    Partial refresh endpoint for golden repos list section.

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Reuse existing CSRF token from cookie instead of generating new one
    csrf_token = get_csrf_token_from_cookie(request)
    if not csrf_token:
        # Fallback: generate new token if cookie missing/invalid
        csrf_token = generate_csrf_token()
    backend_registry = getattr(request.app.state, "backend_registry", None)
    repos = _get_golden_repos_list(backend_registry)

    # Get categories for dropdown (Story #183)
    try:
        category_service = _get_repo_category_service()
        categories = category_service.list_categories()
    except Exception as e:
        logger.warning(
            format_error_log(
                "SCIP-GENERAL-050",
                f"Could not load categories for partial template: {e}",
            ),
            extra={"correlation_id": get_correlation_id()},
        )
        categories = []

    response = templates.TemplateResponse(
        request,
        "partials/golden_repos_list.html",
        {
            "request": request,
            "csrf_token": csrf_token,
            "repos": repos,
            "categories": categories,
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


def _render_details_error(
    request: Request,
    alias: str,
    message: str,
    error_kind: str,
    status_code: int,
    show_retry: bool,
) -> HTMLResponse:
    """
    Render the details_error.html fragment with the given parameters.

    Used by golden_repo_details_partial for 401/404/500 error paths so the
    client receives an inline HTML fragment rather than a redirect or JSON
    error, allowing htmx to swap the message into the details cell.
    """
    return templates.TemplateResponse(
        request,
        "partials/details_error.html",
        {
            "request": request,
            "alias": alias,
            "message": message,
            "error_kind": error_kind,
            "show_retry": show_retry,
        },
        status_code=status_code,
    )


def _get_single_repo_enriched(alias: str, backend_registry=None) -> Optional[dict]:
    """
    Return a fully-enriched repo dict for the given alias, or None if not found.

    Applies the same enrichment as _get_golden_repos_list: global alias, version,
    temporal status, index flags, and category data.
    """
    manager = _get_golden_repo_manager()
    repos = manager.list_golden_repos()
    repo = next((r for r in repos if r.get("alias") == alias), None)
    if repo is None:
        return None
    # The repository URL is rendered with its userinfo redacted.
    repo = with_masked_repo_url(repo)
    server_data_dir = os.environ.get(
        "CIDX_SERVER_DATA_DIR", os.path.expanduser("~/.cidx-server")
    )
    aliases_dir = Path(server_data_dir) / "data" / "golden-repos" / "aliases"
    if backend_registry is not None:
        global_repos = {
            v["repo_name"]: v
            for v in backend_registry.global_repos.list_repos().values()
        }
    else:
        global_repos = {}
    if "status" not in repo:
        repo["status"] = "ready"
    repo["last_indexed"] = _safe_ts_slice(repo.get("created_at"), 10)
    g_alias, g_queryable, version, last_refresh, index_path = _resolve_alias_metadata(
        alias, global_repos, aliases_dir
    )
    repo.update(
        {
            "global_alias": g_alias,
            "globally_queryable": g_queryable,
            "version": version,
            "last_refresh": last_refresh,
        }
    )
    if not g_queryable:
        index_path = repo.get("clone_path")
    repo["temporal_status"] = _load_temporal_status(g_alias, alias)
    repo.update(_detect_index_flags(index_path, repo_alias=alias))
    cat = _build_category_lookup().get(alias, {})
    repo.update(
        {
            "category_id": cat.get("category_id"),
            "category_name": cat.get("category_name"),
            "category_priority": cat.get("category_priority"),
        }
    )
    # cast: list_golden_repos() returns List[Any]; narrowing to route contract
    return cast(Optional[Dict[str, Any]], repo)


@web_router.get("/partials/golden-repos/{alias}/details", response_class=HTMLResponse)
def golden_repo_details_partial(request: Request, alias: str):
    """
    Lazy-load details partial for a single golden repo (Story #863).

    Returns the enriched details card HTML fragment for htmx.ajax swap.
    Does NOT rotate the CSRF token (Finding 2 / AC6/AC7/AC8): error paths
    return inline HTML fragments via _render_details_error so htmx can swap
    the message into the details cell without a page redirect.
    """
    session = _require_admin_session(request)
    if not session:
        return _render_details_error(
            request,
            alias,
            "Session expired — please reload the page",
            "session_expired",
            401,
            False,
        )

    try:
        backend_registry = getattr(request.app.state, "backend_registry", None)
        repo = _get_single_repo_enriched(alias, backend_registry)
        if repo is None:
            return _render_details_error(
                request,
                alias,
                "Repository not found",
                "not_found",
                404,
                False,
            )
        csrf_token = get_csrf_token_from_cookie(request)
        categories = _get_repo_category_service().list_categories()
        # Story #1412: pass the gate value so the all-branches checkbox can
        # be disabled/hidden with an explanatory note when the server-wide
        # temporal_all_branches_enabled flag is off.
        _gate_config = get_config_service().get_config()
        _temporal_all_branches_enabled = bool(
            getattr(
                _gate_config.indexing_config, "temporal_all_branches_enabled", False
            )
        )
        return templates.TemplateResponse(
            request,
            "partials/golden_repo_details.html",
            {
                "request": request,
                "repo": repo,
                "csrf_token": csrf_token,
                "categories": categories,
                "temporal_all_branches_enabled": _temporal_all_branches_enabled,
            },
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "SCIP-GENERAL-052",
                f"Unexpected error loading details for '{alias}': {e}",
            ),
            exc_info=True,
            extra={"correlation_id": get_correlation_id()},
        )
        return _render_details_error(
            request,
            alias,
            "Failed to load repository details",
            "internal_error",
            500,
            True,
        )
