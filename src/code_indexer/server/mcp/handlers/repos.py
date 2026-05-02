"""Repository management — listing, activation, golden repos, provider indexes.

Domain module for repository handlers. Part of the handlers package
modularization (Story #496).
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Any, Optional
from pathlib import Path

from code_indexer.server.auth.user_manager import User
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoNotFoundError
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.global_registry import GlobalRegistry

from . import _utils
from ._utils import (
    _mcp_response,
    _get_golden_repos_dir,
    _list_global_repos,
    _get_hnsw_health_service,
    _get_access_filtering_service,
    _get_app_refresh_scheduler,
    _get_available_repos,
    _error_with_suggestions,
)

# _GLOBAL_SUFFIX is used in _resolve_branch_repo_path to strip the alias-manager
# suffix before passing the base alias to golden_repo_manager.get_actual_repo_path().
_GLOBAL_SUFFIX = "-global"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Repository management handlers
# (Mechanically extracted from _legacy.py — Story #496)
# ---------------------------------------------------------------------------


def discover_repositories(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Discover available repositories from configured sources."""
    try:
        repos = _utils.app_module.golden_repo_manager.list_golden_repos()

        access_filtering_service = _get_access_filtering_service()
        if access_filtering_service:
            repo_aliases = [r.get("alias", r.get("name", "")) for r in repos]
            accessible_aliases = access_filtering_service.filter_repo_listing(
                repo_aliases, user.username
            )
            repos = [
                r
                for r in repos
                if r.get("alias", r.get("name", "")) in accessible_aliases
            ]

        return _mcp_response({"success": True, "repositories": repos})
    except Exception as e:
        logger.warning("discover_repositories failed: %s", e, exc_info=True)
        return _mcp_response({"success": False, "error": str(e), "repositories": []})


def deactivate_repository(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Deactivate a repository."""
    try:
        user_alias = params.get("user_alias", "")
        if not user_alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: user_alias",
                    "job_id": None,
                }
            )

        job_id = _utils.app_module.activated_repo_manager.deactivate_repository(
            username=user.username, user_alias=user_alias
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Repository '{user_alias}' deactivation started",
            }
        )
    except Exception as e:
        logger.warning("deactivate_repository failed: %s", e, exc_info=True)
        return _mcp_response({"success": False, "error": str(e), "job_id": None})


def _access_denied_response() -> Dict[str, Any]:
    """Build standard access-denied MCP response for repo handlers."""
    return _mcp_response(
        {
            "success": False,
            "error": (
                "Repository not accessible. Contact your administrator for access."
            ),
            "job_id": None,
        }
    )


def activate_repository(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Activate a repository for querying (supports single or composite)."""
    try:
        golden_repo_alias = params.get("golden_repo_alias")
        golden_repo_aliases = params.get("golden_repo_aliases")

        # Validate inputs
        if not golden_repo_alias and not golden_repo_aliases:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: golden_repo_alias or golden_repo_aliases",
                    "job_id": None,
                }
            )
        if golden_repo_alias is not None and not isinstance(golden_repo_alias, str):
            return _mcp_response(
                {
                    "success": False,
                    "error": "golden_repo_alias must be a string",
                    "job_id": None,
                }
            )
        if golden_repo_aliases is not None:
            if not isinstance(golden_repo_aliases, list) or not all(
                isinstance(a, str) and a for a in golden_repo_aliases
            ):
                return _mcp_response(
                    {
                        "success": False,
                        "error": "golden_repo_aliases must be a list of non-empty strings",
                        "job_id": None,
                    }
                )

        # Story #300: Check group-based access before activating (AC4)
        access_filtering_service = _get_access_filtering_service()
        if access_filtering_service:
            if not access_filtering_service.is_admin_user(user.username):
                accessible_repos = access_filtering_service.get_accessible_repos(
                    user.username
                )
                if golden_repo_alias and golden_repo_alias not in accessible_repos:
                    return _access_denied_response()
                if golden_repo_aliases:
                    for alias in golden_repo_aliases:
                        if alias not in accessible_repos:
                            return _access_denied_response()

        job_id = _utils.app_module.activated_repo_manager.activate_repository(
            username=user.username,
            golden_repo_alias=golden_repo_alias,
            golden_repo_aliases=golden_repo_aliases,
            branch_name=params.get("branch_name"),
            user_alias=params.get("user_alias"),
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": "Repository activation started",
            }
        )
    except Exception as e:
        logger.warning("activate_repository failed: %s", e, exc_info=True)
        return _mcp_response({"success": False, "error": str(e), "job_id": None})


def list_repo_categories(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """List all repository categories (Story #182).

    Story #331 AC10: Accepted risk - repository categories are generic
    organizational labels (e.g., category names/patterns) that do not
    directly reveal specific repository names or existence. Filtering
    categories would provide minimal security benefit.
    """
    try:
        if (
            not hasattr(_utils.app_module, "golden_repo_manager")
            or not _utils.app_module.golden_repo_manager
        ):
            return _mcp_response(
                {
                    "success": False,
                    "error": "Category service not available",
                    "categories": [],
                    "total": 0,
                }
            )

        category_service = getattr(
            _utils.app_module.golden_repo_manager, "_repo_category_service", None
        )
        if not category_service:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Category service not initialized",
                    "categories": [],
                    "total": 0,
                }
            )

        categories = category_service.list_categories()
        return _mcp_response(
            {"success": True, "categories": categories, "total": len(categories)}
        )
    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-035",
                f"Failed to list repository categories: {e}",
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {"success": False, "error": str(e), "categories": [], "total": 0}
        )


def switch_branch(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Switch repository to different branch."""
    try:
        user_alias = params.get("user_alias", "")
        branch_name = params.get("branch_name", "")
        if not user_alias or not branch_name:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameters: user_alias and branch_name",
                }
            )
        create = params.get("create", False)

        result = _utils.app_module.activated_repo_manager.switch_branch(
            username=user.username,
            user_alias=user_alias,
            branch_name=branch_name,
            create=create,
        )
        return _mcp_response({"success": True, "message": result["message"]})
    except Exception as e:
        logger.warning("switch_branch failed: %s", e, exc_info=True)
        return _mcp_response({"success": False, "error": str(e)})


def sync_repository(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Sync repository with upstream."""
    try:
        user_alias = params.get("user_alias", "")
        if not user_alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: user_alias",
                    "job_id": None,
                }
            )

        repos = _utils.app_module.activated_repo_manager.list_activated_repositories(
            user.username
        )
        repo_id = None
        for repo in repos:
            if repo["user_alias"] == user_alias:
                repo_id = repo.get("actual_repo_id", user_alias)
                break

        if not repo_id:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Repository '.*' not found",
                    "job_id": None,
                }
            )

        if _utils.app_module.background_job_manager is None:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Background job manager not initialized",
                    "job_id": None,
                }
            )

        from code_indexer.server.app import _execute_repository_sync

        def sync_job_wrapper():
            return _execute_repository_sync(
                repo_id=repo_id,
                username=user.username,
                options={},
                progress_callback=None,
            )

        job_id = _utils.app_module.background_job_manager.submit_job(
            operation_type="sync_repository",
            func=sync_job_wrapper,
            submitter_username=user.username,
            repo_alias=repo_id,
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Repository '{user_alias}' sync started",
            }
        )
    except Exception as e:
        logger.warning("sync_repository failed: %s", e, exc_info=True)
        return _mcp_response({"success": False, "error": str(e), "job_id": None})


def check_health(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Check system health status."""
    try:
        from code_indexer import __version__
        from code_indexer.server.services.health_service import health_service

        health_response = health_service.get_system_health()
        node_id = getattr(_utils.app_module.app.state, "node_id", None)

        return _mcp_response(
            {
                "success": True,
                "server_version": __version__,
                "node_id": node_id,
                "health": health_response.model_dump(mode="json"),
            }
        )
    except Exception as e:
        logger.warning("check_health failed: %s", e, exc_info=True)
        return _mcp_response({"success": False, "error": str(e), "health": {}})


def _load_category_map() -> dict:
    """Load category map from golden_repo_manager, returning empty dict on failure."""
    try:
        if (
            hasattr(_utils.app_module, "golden_repo_manager")
            and _utils.app_module.golden_repo_manager
        ):
            category_service = getattr(
                _utils.app_module.golden_repo_manager,
                "_repo_category_service",
                None,
            )
            if category_service:
                return category_service.get_repo_category_map()  # type: ignore[no-any-return]  # service returns dict but mypy sees Any from dynamic lookup
    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-034",
                f"Failed to load category map: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
    return {}


def check_hnsw_health(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Check HNSW index health and integrity for a repository."""
    try:
        repository_alias = params.get("repository_alias", "")
        if not repository_alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: repository_alias",
                }
            )
        force_refresh = params.get("force_refresh", False)

        repo = _utils.app_module.golden_repo_manager.get_golden_repo(repository_alias)
        if not repo:
            return _mcp_response(
                {"success": False, "error": f"Repository not found: {repository_alias}"}
            )

        clone_path = Path(repo.clone_path)
        index_path = clone_path / ".code-indexer" / "index" / "default" / "index.bin"

        health_service = _get_hnsw_health_service()
        result = health_service.check_health(
            index_path=str(index_path),
            force_refresh=force_refresh,
        )

        return _mcp_response(
            {"success": True, "health": result.model_dump(mode="json")}
        )
    except Exception as e:
        logger.exception(
            f"Error in check_hnsw_health: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def get_repository_status(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get detailed status of a repository."""
    try:
        user_alias = params.get("repository_alias", "")
        if not user_alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: repository_alias",
                    "status": {},
                }
            )

        category_map = _load_category_map()

        # Global repository path
        if user_alias.endswith("-global"):
            return _get_global_repo_status(user_alias, category_map, user)

        # Activated repository
        status = _utils.app_module.repository_listing_manager.get_repository_details(
            user_alias, user.username
        )
        golden_alias = status.get("golden_repo_alias")
        if golden_alias:
            category_info = category_map.get(golden_alias, {})
            status["repo_category"] = category_info.get("category_name")

        return _mcp_response({"success": True, "status": status})
    except Exception as e:
        logger.warning("get_repository_status failed: %s", e, exc_info=True)
        return _mcp_response({"success": False, "error": str(e), "status": {}})


def _get_global_repo_status(
    user_alias: str, category_map: dict, user: User
) -> Dict[str, Any]:
    """Build status response for a global repository."""
    global_repos = _list_global_repos()
    repo_entry = next((r for r in global_repos if r["alias_name"] == user_alias), None)
    if not repo_entry:
        available_repos = _get_available_repos(user)
        error_envelope = _error_with_suggestions(
            error_msg=f"Global repository '{user_alias}' not found",
            attempted_value=user_alias,
            available_values=available_repos,
        )
        error_envelope["status"] = {}
        return _mcp_response(error_envelope)

    status = {
        "user_alias": repo_entry["alias_name"],
        "golden_repo_alias": repo_entry.get("repo_name"),
        "repo_url": repo_entry.get("repo_url"),
        "is_global": True,
        "path": repo_entry.get("index_path"),
        "last_refresh": repo_entry.get("last_refresh"),
        "created_at": repo_entry.get("created_at"),
        "index_path": repo_entry.get("index_path"),
    }
    golden_alias = repo_entry.get("repo_name")
    category_info = category_map.get(golden_alias, {})
    status["repo_category"] = category_info.get("category_name")
    return _mcp_response({"success": True, "status": status})


def get_repository_statistics(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get repository statistics."""
    try:
        repository_alias = params.get("repository_alias", "")
        if not repository_alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: repository_alias",
                    "statistics": {},
                }
            )

        if repository_alias.endswith("-global"):
            return _get_global_repo_statistics(repository_alias, user)

        from code_indexer.server.services.stats_service import stats_service

        stats_response = stats_service.get_repository_stats(
            repository_alias, username=user.username
        )
        return _mcp_response(
            {"success": True, "statistics": stats_response.model_dump(mode="json")}
        )
    except Exception as e:
        logger.warning("get_repository_statistics failed: %s", e, exc_info=True)
        return _mcp_response({"success": False, "error": str(e), "statistics": {}})


def get_all_repositories_status(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get status summary of all repositories."""
    try:
        repos = _utils.app_module.activated_repo_manager.list_activated_repositories(
            user.username
        )
        status_summary = []
        for repo in repos:
            try:
                details = (
                    _utils.app_module.repository_listing_manager.get_repository_details(
                        repo["user_alias"], user.username
                    )
                )
                status_summary.append(details)
            except Exception as detail_err:
                logger.debug(
                    "Skipping repo %s in status summary: %s",
                    repo.get("user_alias", "?"),
                    detail_err,
                )

        _append_global_repos_to_status(status_summary, user)

        return _mcp_response(
            {
                "success": True,
                "repositories": status_summary,
                "total": len(status_summary),
            }
        )
    except Exception as e:
        logger.warning("get_all_repositories_status failed: %s", e, exc_info=True)
        return _mcp_response(
            {"success": False, "error": str(e), "repositories": [], "total": 0}
        )


def _get_global_repo_statistics(repository_alias: str, user: User) -> Dict[str, Any]:
    """Build statistics response for a global repository."""
    golden_repos_dir = _get_golden_repos_dir()
    global_repos = _list_global_repos()

    repo_entry = next(
        (r for r in global_repos if r["alias_name"] == repository_alias), None
    )
    if not repo_entry:
        available_repos = _get_available_repos(user)
        error_envelope = _error_with_suggestions(
            error_msg=f"Global repository '{repository_alias}' not found",
            attempted_value=repository_alias,
            available_values=available_repos,
        )
        error_envelope["statistics"] = {}
        return _mcp_response(error_envelope)

    alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
    target_path = alias_manager.read_alias(repository_alias)
    if not target_path:
        available_repos = _get_available_repos(user)
        error_envelope = _error_with_suggestions(
            error_msg=f"Alias for '{repository_alias}' not found",
            attempted_value=repository_alias,
            available_values=available_repos,
        )
        error_envelope["statistics"] = {}
        return _mcp_response(error_envelope)

    statistics = {
        "repository_alias": repository_alias,
        "is_global": True,
        "path": target_path,
        "index_path": repo_entry.get("index_path"),
    }
    return _mcp_response({"success": True, "statistics": statistics})


def _append_global_repos_to_status(status_summary: list, user: User) -> None:
    """Append global repos to the status summary list (mutates in place)."""
    try:
        global_repos_data = _list_global_repos()

        access_filtering_service = _get_access_filtering_service()
        if access_filtering_service:
            repo_names = [r.get("repo_name", "") for r in global_repos_data]
            accessible = access_filtering_service.filter_repo_listing(
                repo_names, user.username
            )
            global_repos_data = [
                r for r in global_repos_data if r.get("repo_name", "") in accessible
            ]

        for repo in global_repos_data:
            if "alias_name" not in repo or "repo_name" not in repo:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-035",
                        f"Skipping malformed global repo entry: {repo}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                continue

            global_status = {
                "user_alias": repo["alias_name"],
                "golden_repo_alias": repo["repo_name"],
                "current_branch": None,
                "is_global": True,
                "repo_url": repo.get("repo_url"),
                "last_refresh": repo.get("last_refresh"),
                "index_path": repo.get("index_path"),
                "created_at": repo.get("created_at"),
            }
            status_summary.append(global_status)
    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-036",
                f"Failed to load global repos status: {e}",
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )
        )


# Story #196: Whitelist of MCP-relevant fields for activated repos
_ACTIVATED_REPO_FIELDS = {
    "user_alias",
    "golden_repo_alias",
    "current_branch",
    "is_global",
    "repo_url",
    "last_refresh",
    "repo_category",
    "is_composite",
    "golden_repo_aliases",
}


def _load_global_repos_normalized() -> list:
    """Load and normalize global repos to match activated repo schema."""
    result = []
    try:
        global_repos_data = _list_global_repos()
        for repo in global_repos_data:
            if "alias_name" not in repo or "repo_name" not in repo:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-032",
                        f"Skipping malformed global repo entry: {repo}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                continue
            result.append(
                {
                    "user_alias": repo["alias_name"],
                    "golden_repo_alias": repo["repo_name"],
                    "current_branch": None,
                    "is_global": True,
                    "repo_url": repo.get("repo_url"),
                    "last_refresh": repo.get("last_refresh"),
                }
            )
    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-033",
                f"Failed to load global repos from storage backend: {e}",
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )
        )
    return result


def _enrich_and_filter_repos(
    all_repos: list, params: Dict[str, Any], user: User
) -> list:
    """Enrich repos with category info, apply category filter and access filter."""
    category_map = _load_category_map()

    for repo in all_repos:
        golden_alias = repo.get("golden_repo_alias")
        category_info = category_map.get(golden_alias, {})
        repo["repo_category"] = category_info.get("category_name")

    # Filter by category if requested
    category_filter = params.get("category")
    if category_filter:
        if category_filter == "Unassigned":
            all_repos = [r for r in all_repos if r["repo_category"] is None]
        else:
            all_repos = [r for r in all_repos if r["repo_category"] == category_filter]

    # Sort by priority then alphabetically
    def sort_key(repo):
        golden_alias = repo.get("golden_repo_alias")
        cat_info = category_map.get(golden_alias, {})
        priority = cat_info.get("priority")
        if priority is None:
            return (float("inf"), repo.get("user_alias", ""))
        return (priority, repo.get("user_alias", ""))

    all_repos.sort(key=sort_key)

    # Apply group-based access filtering
    access_filtering_service = _get_access_filtering_service()
    if access_filtering_service:
        repo_aliases = [r.get("golden_repo_alias", "") for r in all_repos]
        accessible_aliases = access_filtering_service.filter_repo_listing(
            repo_aliases, user.username
        )
        all_repos = [
            r for r in all_repos if r.get("golden_repo_alias", "") in accessible_aliases
        ]

    return all_repos


def list_repositories(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """List activated repositories for the current user, plus global repos."""
    try:
        raw_activated = (
            _utils.app_module.activated_repo_manager.list_activated_repositories(
                user.username
            )
        )
        activated_repos = [
            {k: v for k, v in repo.items() if k in _ACTIVATED_REPO_FIELDS}
            for repo in raw_activated
        ]

        global_repos = _load_global_repos_normalized()
        all_repos = activated_repos + global_repos
        all_repos = _enrich_and_filter_repos(all_repos, params, user)

        return _mcp_response({"success": True, "repositories": all_repos})
    except Exception as e:
        logger.warning("list_repositories failed: %s", e, exc_info=True)
        return _mcp_response({"success": False, "error": str(e), "repositories": []})


def _resolve_branch_repo_path(repository_alias: str, user: User) -> tuple:
    """Resolve repo path for branch operations. Returns (path, error_response)."""
    if repository_alias.endswith("-global"):
        global_repos = _list_global_repos()
        repo_entry = next(
            (r for r in global_repos if r["alias_name"] == repository_alias), None
        )
        if not repo_entry:
            available_repos = _get_available_repos(user)
            error_envelope = _error_with_suggestions(
                error_msg=f"Global repository '{repository_alias}' not found",
                attempted_value=repository_alias,
                available_values=available_repos,
            )
            error_envelope["branches"] = []
            return None, _mcp_response(error_envelope)

        # Use golden_repo_manager.get_actual_repo_path to obtain the mutable base clone
        # path rather than AliasManager.read_alias which returns the frozen versioned
        # snapshot path (Issue #966 - remote refs are only visible on the base clone).
        # get_actual_repo_path is keyed on the BASE alias (no -global suffix), so strip
        # the suffix before calling it.
        base_alias = repository_alias
        if base_alias.endswith(_GLOBAL_SUFFIX):
            base_alias = base_alias[: -len(_GLOBAL_SUFFIX)]
        try:
            target_path = _utils.app_module.golden_repo_manager.get_actual_repo_path(
                base_alias
            )
        except GoldenRepoNotFoundError:
            # Fall back to whatever AliasManager recorded for this alias.
            golden_repos_dir = _get_golden_repos_dir()
            alias_manager = AliasManager(golden_repos_dir)
            target_path = alias_manager.read_alias(repository_alias) or ""

        if not target_path:
            available_repos = _get_available_repos(user)
            error_envelope = _error_with_suggestions(
                error_msg=f"Alias for '{repository_alias}' not found",
                attempted_value=repository_alias,
                available_values=available_repos,
            )
            error_envelope["branches"] = []
            return None, _mcp_response(error_envelope)

        return target_path, None

    repo_path = _utils.app_module.activated_repo_manager.get_activated_repo_path(
        username=user.username, user_alias=repository_alias
    )
    return repo_path, None


def get_branches(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get available branches for a repository."""
    from code_indexer.services.git_topology_service import GitTopologyService
    from code_indexer.server.services.branch_service import BranchService

    try:
        repository_alias = params.get("repository_alias", "")
        if not repository_alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: repository_alias",
                    "branches": [],
                }
            )
        include_remote = params.get("include_remote", False)

        repo_path, error_response = _resolve_branch_repo_path(repository_alias, user)
        if error_response is not None:
            return error_response  # type: ignore[no-any-return]  # error_response is dict from _mcp_response but mypy sees Any

        git_topology_service = GitTopologyService(Path(repo_path))
        with BranchService(
            git_topology_service=git_topology_service, index_status_manager=None
        ) as branch_service:
            branches = branch_service.list_branches(include_remote=include_remote)
            branches_data = [
                {
                    "name": b.name,
                    "is_current": b.is_current,
                    "last_commit": {
                        "sha": b.last_commit.sha,
                        "message": b.last_commit.message,
                        "author": b.last_commit.author,
                        "date": b.last_commit.date,
                    },
                    "index_status": (
                        {
                            "status": b.index_status.status,
                            "files_indexed": b.index_status.files_indexed,
                            "total_files": b.index_status.total_files,
                            "last_indexed": b.index_status.last_indexed,
                            "progress_percentage": b.index_status.progress_percentage,
                        }
                        if b.index_status
                        else None
                    ),
                    "remote_tracking": (
                        {
                            "remote": b.remote_tracking.remote,
                            "ahead": b.remote_tracking.ahead,
                            "behind": b.remote_tracking.behind,
                        }
                        if b.remote_tracking
                        else None
                    ),
                }
                for b in branches
            ]
            return _mcp_response({"success": True, "branches": branches_data})
    except Exception as e:
        logger.warning("get_branches failed: %s", e, exc_info=True)
        return _mcp_response({"success": False, "error": str(e), "branches": []})


# ---------------------------------------------------------------------------
# Golden repos handlers
# ---------------------------------------------------------------------------


def add_golden_repo(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Add a golden repository (admin only)."""
    try:
        repo_url = params.get("url", "")
        alias = params.get("alias", "")
        if not repo_url or not alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameters: url and alias",
                }
            )
        default_branch = params.get("branch") or None
        enable_temporal = params.get("enable_temporal", False)
        temporal_options = params.get("temporal_options")

        job_id = _utils.app_module.golden_repo_manager.add_golden_repo(
            repo_url=repo_url,
            alias=alias,
            default_branch=default_branch,
            enable_temporal=enable_temporal,
            temporal_options=temporal_options,
            submitter_username=user.username,
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Golden repository '{alias}' addition started",
            }
        )
    except Exception as e:
        logger.warning("add_golden_repo failed: %s", e, exc_info=True)
        return _mcp_response({"success": False, "error": str(e)})


def remove_golden_repo(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Remove a golden repository (admin only)."""
    try:
        alias = params.get("alias", "")
        if not alias:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: alias"}
            )
        job_id = _utils.app_module.golden_repo_manager.remove_golden_repo(
            alias, submitter_username=user.username
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Golden repository '{alias}' removal started",
            }
        )
    except Exception as e:
        logger.warning("remove_golden_repo failed: %s", e, exc_info=True)
        return _mcp_response({"success": False, "error": str(e)})


def refresh_golden_repo(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Refresh a golden repository (admin only)."""
    try:
        alias = params.get("alias", "")
        if not alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: alias",
                    "job_id": None,
                }
            )
        if alias not in _utils.app_module.golden_repo_manager.golden_repos:
            return _mcp_response(
                {
                    "success": False,
                    "error": f"Golden repository '{alias}' not found",
                    "job_id": None,
                }
            )
        refresh_scheduler = _get_app_refresh_scheduler()
        if refresh_scheduler is None:
            return _mcp_response(
                {
                    "success": False,
                    "error": "RefreshScheduler not available",
                    "job_id": None,
                }
            )
        job_id = refresh_scheduler.trigger_refresh_for_repo(
            alias, submitter_username=user.username
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Golden repository '{alias}' refresh started",
            }
        )
    except Exception as e:
        logger.warning("refresh_golden_repo failed: %s", e, exc_info=True)
        return _mcp_response({"success": False, "error": str(e), "job_id": None})


def change_golden_repo_branch(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Change the active branch of a golden repository async (Story #308)."""
    alias = params.get("alias", "")
    branch = params.get("branch", "")

    if not alias or not branch:
        return _mcp_response(
            {
                "success": False,
                "error": "Missing required parameters: 'alias' and 'branch'",
            }
        )

    try:
        result = _utils.app_module.golden_repo_manager.change_branch_async(
            alias, branch, user.username
        )
        job_id = result.get("job_id")
        if job_id is None:
            return _mcp_response(
                {
                    "success": True,
                    "message": f"Already on branch '{branch}'. No action taken.",
                }
            )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": "Branch change started. Use get_job_details to poll.",
            }
        )
    except Exception as e:
        from code_indexer.server.repositories.background_jobs import DuplicateJobError

        if isinstance(e, DuplicateJobError):
            return _mcp_response(
                {
                    "success": False,
                    "error": str(e),
                    "existing_job_id": e.existing_job_id,
                }
            )
        logger.warning("change_golden_repo_branch failed: %s", e, exc_info=True)
        return _mcp_response({"success": False, "error": str(e)})


def handle_add_golden_repo_index(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for add_golden_repo_index tool (Story #596 AC1, AC3, AC4, AC5)."""
    alias = args.get("alias", "")
    index_type = args.get("index_type", "")

    if not alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: alias"}
        )
    if not index_type:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: index_type"}
        )

    try:
        golden_repo_manager = getattr(_utils.app_module, "golden_repo_manager", None)
        if not golden_repo_manager:
            return _mcp_response(
                {"success": False, "error": "Golden repository manager not available"}
            )

        job_id = golden_repo_manager.add_index_to_golden_repo(
            alias=alias, index_type=index_type, submitter_username=user.username
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Index type '{index_type}' is being added to golden repo '{alias}'. Use get_job_statistics to track progress.",
            }
        )
    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-037",
                f"Error adding index to golden repo: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {"success": False, "error": f"Failed to add index: {str(e)}"}
        )


def handle_get_golden_repo_indexes(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for get_golden_repo_indexes tool (Story #596 AC2, AC4)."""
    alias = args.get("alias", "")
    if not alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: alias"}
        )

    try:
        golden_repo_manager = getattr(_utils.app_module, "golden_repo_manager", None)
        if not golden_repo_manager:
            return _mcp_response(
                {"success": False, "error": "Golden repository manager not available"}
            )

        status = golden_repo_manager.get_golden_repo_indexes(alias)
        return _mcp_response({"success": True, **status})
    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-038",
                f"Error getting golden repo indexes: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {"success": False, "error": f"Failed to get indexes: {str(e)}"}
        )


# ---------------------------------------------------------------------------
# Global/composite handlers
# ---------------------------------------------------------------------------


def _validate_composite_access(
    golden_repo_aliases: list, user: User
) -> Optional[Dict[str, Any]]:
    """Check group access for composite repo component aliases. Returns error response or None."""
    access_service = _get_access_filtering_service()
    if not access_service or not golden_repo_aliases:
        return None
    if access_service.is_admin_user(user.username):
        return None
    accessible = access_service.get_accessible_repos(user.username)
    for component_alias in golden_repo_aliases:
        normalized = component_alias
        if normalized.endswith("-global"):
            normalized = normalized[: -len("-global")]
        if normalized not in accessible:
            return _mcp_response(
                {
                    "success": False,
                    "error": f"Access denied: repository '{component_alias}' is not accessible.",
                    "job_id": None,
                }
            )
    return None


def manage_composite_repository(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Manage composite repository operations."""
    try:
        operation = params.get("operation", "")
        user_alias = params.get("user_alias", "")
        if not operation or not user_alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameters: operation and user_alias",
                    "job_id": None,
                }
            )
        golden_repo_aliases = params.get("golden_repo_aliases", [])

        access_error = _validate_composite_access(golden_repo_aliases, user)
        if access_error is not None:
            return access_error

        if operation == "create":
            job_id = _utils.app_module.activated_repo_manager.activate_repository(
                username=user.username,
                golden_repo_aliases=golden_repo_aliases,
                user_alias=user_alias,
            )
            msg = f"Composite repository '{user_alias}' creation started"
        elif operation == "update":
            try:
                _utils.app_module.activated_repo_manager.deactivate_repository(
                    username=user.username, user_alias=user_alias
                )
            except Exception as deact_err:
                logger.debug(
                    "Deactivate before update for %s: %s", user_alias, deact_err
                )
            job_id = _utils.app_module.activated_repo_manager.activate_repository(
                username=user.username,
                golden_repo_aliases=golden_repo_aliases,
                user_alias=user_alias,
            )
            msg = f"Composite repository '{user_alias}' update started"
        elif operation == "delete":
            job_id = _utils.app_module.activated_repo_manager.deactivate_repository(
                username=user.username, user_alias=user_alias
            )
            msg = f"Composite repository '{user_alias}' deletion started"
        else:
            return _mcp_response(
                {"success": False, "error": f"Unknown operation: {operation}"}
            )

        return _mcp_response({"success": True, "job_id": job_id, "message": msg})
    except Exception as e:
        logger.warning("manage_composite_repository failed: %s", e, exc_info=True)
        return _mcp_response({"success": False, "error": str(e), "job_id": None})


def handle_list_global_repos(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for list_global_repos tool."""
    try:
        repos = _list_global_repos()

        access_filtering_service = _get_access_filtering_service()
        if access_filtering_service:
            repo_aliases = [r.get("repo_name", r.get("alias", "")) for r in repos]
            accessible_aliases = access_filtering_service.filter_repo_listing(
                repo_aliases, user.username
            )
            repos = [
                r
                for r in repos
                if r.get("repo_name", r.get("alias", "")) in accessible_aliases
            ]

        return _mcp_response({"success": True, "repos": repos})
    except Exception as e:
        logger.warning("handle_list_global_repos failed: %s", e, exc_info=True)
        return _mcp_response({"success": False, "error": str(e), "repos": []})


def handle_global_repo_status(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for global_repo_status tool."""
    from code_indexer.global_repos.shared_operations import GlobalRepoOperations

    alias = args.get("alias", "")
    if not alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: alias"}
        )

    try:
        golden_repos_dir = _get_golden_repos_dir()
        ops = GlobalRepoOperations(golden_repos_dir)
        status = ops.get_status(alias)
        return _mcp_response({"success": True, **status})
    except ValueError as ve:
        logger.warning("Global repo '%s' not found: %s", alias, ve)
        return _mcp_response(
            {"success": False, "error": f"Global repo '{alias}' not found"}
        )
    except Exception as e:
        logger.warning("handle_global_repo_status failed: %s", e, exc_info=True)
        return _mcp_response({"success": False, "error": str(e)})


# Maximum characters to include from stdout/stderr tail in provider index job results.
_PROVIDER_JOB_OUTPUT_TAIL_CHARS = 500


def _resolve_golden_repo_path(alias: str) -> Optional[str]:
    """Resolve golden repo alias to the READ-ONLY versioned snapshot path.

    WARNING: The returned path points to an immutable versioned snapshot.
    NEVER write to this path. For WRITE operations, use
    _resolve_golden_repo_base_clone(alias) instead.
    """
    golden_repos_dir = _get_golden_repos_dir()
    alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
    resolved: Optional[str] = alias_manager.read_alias(alias)
    if resolved is None and not alias.endswith("-global"):
        resolved = alias_manager.read_alias(alias + "-global")
    return resolved


def _resolve_golden_repo_base_clone(alias: str) -> Optional[str]:
    """Resolve golden repo alias to the WRITABLE base clone path.

    WARNING: This returns the base clone (golden-repos/{alias_name}/), NOT
    the versioned snapshot. Use for ALL write operations.
    """
    versioned_path = _resolve_golden_repo_path(alias)
    if versioned_path is None:
        return None

    parts = Path(versioned_path).parts
    if ".versioned" in parts:
        versioned_idx = parts.index(".versioned")
        if versioned_idx + 1 >= len(parts):
            logger.warning(
                "_resolve_golden_repo_base_clone: malformed versioned path %s",
                versioned_path,
            )
            return None
        alias_name = parts[versioned_idx + 1]
        golden_repos_dir = str(Path(*parts[:versioned_idx]))
        base_clone = Path(golden_repos_dir) / alias_name
        if base_clone.exists():
            return str(base_clone)
        return None

    # Not a versioned path -- return as-is (legacy flat structure)
    return versioned_path


def _load_repo_config(repo_path: str) -> Optional[tuple]:
    """Validate repo_path and load .code-indexer/config.json.

    Returns (config_data, config_path) on success, None on any failure.
    Rejects versioned snapshot paths (immutable).
    """
    if not repo_path or not isinstance(repo_path, str):
        logger.warning("_load_repo_config: invalid repo_path: %s", repo_path)
        return None

    resolved = Path(repo_path).resolve()
    if ".versioned" in resolved.parts:
        logger.error(
            "_load_repo_config called with versioned snapshot path %s -- "
            "refusing (immutable). Use _resolve_golden_repo_base_clone() instead.",
            repo_path,
        )
        return None

    config_path = resolved / ".code-indexer" / "config.json"
    if not config_path.exists():
        logger.warning("_load_repo_config: config.json not found at %s", config_path)
        return None

    try:
        with open(config_path) as f:
            return json.load(f), config_path
    except Exception as exc:
        logger.warning("_load_repo_config failed for %s: %s", config_path, exc)
        return None


def _append_provider_to_config(repo_path: str, provider_name: str) -> bool:
    """Append provider_name to embedding_providers in .code-indexer/config.json.

    Idempotent: no duplicate added if already present.
    Returns True on success, False on any failure.
    """
    if not provider_name or not isinstance(provider_name, str):
        logger.warning("_append_provider_to_config: invalid provider_name")
        return False

    result = _load_repo_config(repo_path)
    if result is None:
        return False
    config_data, config_path = result

    try:
        existing = config_data.get(
            "embedding_providers",
            [config_data.get("embedding_provider", "voyage-ai")],
        )
        if provider_name not in existing:
            existing.append(provider_name)
        config_data["embedding_providers"] = existing
        with open(config_path, "w") as f:
            json.dump(config_data, f)
        return True
    except Exception as exc:
        logger.warning("_append_provider_to_config failed for %s: %s", config_path, exc)
        return False


def _remove_provider_from_config(repo_path: str, provider_name: str) -> None:
    """Remove provider_name from embedding_providers in .code-indexer/config.json.

    Idempotent. The primary provider (voyage-ai) cannot be removed.
    """
    if not provider_name or not isinstance(provider_name, str):
        logger.warning("_remove_provider_from_config: invalid provider_name")
        return
    if provider_name == "voyage-ai":
        logger.warning("Cannot remove primary provider 'voyage-ai' from config")
        return

    result = _load_repo_config(repo_path)
    if result is None:
        return
    config_data, config_path = result

    try:
        existing = config_data.get("embedding_providers", [])
        if provider_name in existing:
            existing.remove(provider_name)
            config_data["embedding_providers"] = existing
            with open(config_path, "w") as f:
                json.dump(config_data, f)
    except Exception as exc:
        logger.warning(
            "_remove_provider_from_config failed for %s: %s", config_path, exc
        )


def _resolve_provider_api_key(provider_name: str) -> Optional[str]:
    """Return the API key for the given provider from ClaudeIntegrationConfig.

    Reads from the nested claude_integration_config on the server config, not
    from the top-level ServerConfig (Bug #895 fix).
    Returns None for invalid input, missing nested config, or unrecognised
    provider names.
    """
    if not provider_name or not isinstance(provider_name, str):
        return None
    ci_config = getattr(
        get_config_service().get_config(), "claude_integration_config", None
    )
    if ci_config is None:
        return None
    if provider_name == "cohere":
        return getattr(ci_config, "cohere_api_key", None)
    if provider_name == "voyage-ai":
        return getattr(ci_config, "voyageai_api_key", None)
    return None


def _build_provider_api_key_env(provider_name: str) -> dict:
    """Build a subprocess env dict with the correct API key for a provider."""
    import os

    if not provider_name or not isinstance(provider_name, str):
        logger.warning("_build_provider_api_key_env: invalid provider_name")
        return os.environ.copy()

    env = os.environ.copy()
    api_key = _resolve_provider_api_key(provider_name)
    if api_key:
        if provider_name == "cohere":
            env["CO_API_KEY"] = api_key
        elif provider_name == "voyage-ai":
            env["VOYAGE_API_KEY"] = api_key
    return env


def _build_temporal_index_cmd(clear: bool, temporal_options: dict) -> list:
    """Build the cidx index --index-commits command with optional temporal flags."""
    cmd = ["cidx", "index", "--index-commits", "--progress-json"]
    if clear:
        cmd.append("--clear")
    if not temporal_options or not isinstance(temporal_options, dict):
        return cmd
    diff_context = temporal_options.get("diff_context")
    if diff_context is not None:
        cmd.extend(["--diff-context", str(diff_context)])
    if temporal_options.get("all_branches"):
        cmd.append("--all-branches")
    max_commits = temporal_options.get("max_commits")
    if max_commits is not None:
        cmd.extend(["--max-commits", str(max_commits)])
    since_date = temporal_options.get("since_date")
    if since_date:
        cmd.extend(["--since", str(since_date)])
    return cmd


def _resolve_provider_job_repo_path(repo_path: str, repo_alias: str) -> tuple:
    """Resolve the actual indexing path for a provider background job.

    When repo_path is inside a .versioned/ directory (immutable), return the
    base clone path instead. Returns (actual_path, resolved_alias, is_versioned).
    """
    if not repo_path or not isinstance(repo_path, str):
        return repo_path, repo_alias, False

    is_versioned = ".versioned" in Path(repo_path).parts
    if not is_versioned:
        return repo_path, repo_alias, False

    parts = Path(repo_path).parts
    versioned_idx = parts.index(".versioned")
    if versioned_idx + 1 >= len(parts):
        logger.warning("_resolve_provider_job_repo_path: malformed path %s", repo_path)
        return repo_path, repo_alias, True

    alias_name = parts[versioned_idx + 1]
    # Reject traversal or separator segments in alias_name
    if (
        not alias_name
        or alias_name in (".", "..")
        or "/" in alias_name
        or "\\" in alias_name
    ):
        logger.warning(
            "_resolve_provider_job_repo_path: unsafe alias_name '%s'", alias_name
        )
        return repo_path, repo_alias, True

    golden_repos_dir = Path(*parts[:versioned_idx])
    base_clone = golden_repos_dir / alias_name

    # Verify resolved path stays within golden_repos_dir
    try:
        base_clone.resolve().relative_to(golden_repos_dir.resolve())
    except ValueError:
        logger.warning(
            "_resolve_provider_job_repo_path: path escape detected for %s", repo_path
        )
        return repo_path, repo_alias, True

    resolved_alias = repo_alias or f"{alias_name}-global"

    if not base_clone.exists():
        return repo_path, resolved_alias, True

    logger.info(
        "Provider job: using base clone %s instead of versioned snapshot %s",
        base_clone,
        repo_path,
    )
    return str(base_clone), resolved_alias, True


def _post_provider_index_snapshot(
    repo_alias: str, base_clone_path: str, old_snapshot_path: str
) -> None:
    """Create a new versioned snapshot after indexing the base clone.

    Called by _provider_index_job when the original repo_path was a versioned
    snapshot. After indexing the base clone, we create a new snapshot so that
    the alias target is updated and queries reflect the new provider index
    immediately (Bug #604).
    """
    if not repo_alias or not base_clone_path or not old_snapshot_path:
        logger.warning("_post_provider_index_snapshot: missing required arguments")
        return

    scheduler = _get_app_refresh_scheduler()
    if scheduler is None:
        logger.warning(
            "No refresh scheduler available -- skipping snapshot creation after "
            "provider index for %s.",
            repo_alias,
        )
        return

    try:
        new_snapshot = scheduler._create_snapshot(
            alias_name=repo_alias,
            source_path=base_clone_path,
        )
        try:
            scheduler.alias_manager.swap_alias(
                alias_name=repo_alias,
                new_target=new_snapshot,
                old_target=old_snapshot_path,
            )
        except ValueError as swap_exc:
            import shutil as _shutil

            _shutil.rmtree(new_snapshot, ignore_errors=True)
            logger.warning(
                "Alias swap skipped for %s (old_target mismatch): %s. "
                "Orphaned snapshot %s cleanup attempted.",
                repo_alias,
                swap_exc,
                new_snapshot,
            )
            return

        cleanup_manager = getattr(scheduler, "cleanup_manager", None)
        if cleanup_manager is not None:
            cleanup_manager.schedule_cleanup(old_snapshot_path)
        logger.info(
            "Provider index: alias %s now points to new snapshot %s",
            repo_alias,
            new_snapshot,
        )
    except Exception as exc:
        logger.warning(
            "Failed to create new snapshot after provider index for %s: %s. "
            "Index is in base clone and will be visible after next scheduled refresh.",
            repo_alias,
            exc,
        )


def _set_enable_temporal_flag(repo_alias: str) -> None:
    """Set enable_temporal=True in the SQLite backend and in-memory golden_repo_manager.

    Called after _provider_temporal_index_job succeeds to persist the flag.
    Degrades gracefully: logs a warning on any failure rather than raising.
    """
    if not repo_alias:
        return

    grm = getattr(_utils.app_module, "golden_repo_manager", None)
    if grm is None:
        logger.warning(
            "_set_enable_temporal_flag: golden_repo_manager unavailable, "
            "cannot set enable_temporal=True for %s",
            repo_alias,
        )
        return

    try:
        if grm._sqlite_backend.update_enable_temporal(repo_alias, True):
            repo_meta = grm.golden_repos.get(repo_alias)
            if repo_meta is not None:
                repo_meta.enable_temporal = True
            logger.info(
                "Set enable_temporal=True for %s in golden_repos_metadata", repo_alias
            )
        else:
            logger.warning(
                "Failed to set enable_temporal=True for %s in golden_repos_metadata",
                repo_alias,
            )
    except Exception as exc:
        logger.warning("Error setting enable_temporal for %s: %s", repo_alias, exc)

    global_alias = f"{repo_alias}-global"
    try:
        from pathlib import Path as _Path

        data_dir = _Path(grm.data_dir)
        golden_repos_dir = data_dir / "golden-repos"
        sqlite_db_path = str(data_dir / "cidx_server.db")
        registry = GlobalRegistry(
            str(golden_repos_dir),
            use_sqlite=True,
            db_path=sqlite_db_path,
        )
        if (
            registry._sqlite_backend is not None
            and registry._sqlite_backend.update_enable_temporal(global_alias, True)
        ):
            logger.info("Set enable_temporal=True for %s in global_repos", global_alias)
        else:
            logger.warning(
                "Failed to set enable_temporal=True for %s in global_repos",
                global_alias,
            )
    except Exception as exc:
        logger.error("Error updating global_repos table for %s: %s", global_alias, exc)


def _resolve_versioned_to_base_clone(repo_path: str, repo_alias: str) -> tuple:
    """Resolve versioned snapshot path to base clone for background jobs.

    Uses direct path arithmetic (not _resolve_golden_repo_base_clone) because
    background workers have no access to server app state.

    Returns (actual_path, repo_alias, is_versioned, error_msg).
    error_msg is None on success.
    """
    if not repo_path or not isinstance(repo_path, str):
        return repo_path, repo_alias, False, "Invalid repo_path"
    if not Path(repo_path).is_absolute():
        return repo_path, repo_alias, False, f"repo_path must be absolute: {repo_path}"

    resolved = Path(repo_path).resolve()
    is_versioned = ".versioned" in resolved.parts
    if not is_versioned:
        return str(resolved), repo_alias, False, None

    parts = resolved.parts
    versioned_idx = parts.index(".versioned")
    if versioned_idx + 1 >= len(parts):
        return str(resolved), repo_alias, True, f"Malformed versioned path: {repo_path}"

    alias_name = parts[versioned_idx + 1]
    golden_repos_dir = Path(*parts[:versioned_idx])
    base_clone = golden_repos_dir / alias_name

    try:
        base_clone.resolve().relative_to(golden_repos_dir.resolve())
    except ValueError:
        return (
            str(resolved),
            repo_alias,
            True,
            f"Path escape in versioned path: {repo_path}",
        )

    if not repo_alias:
        repo_alias = f"{alias_name}-global"

    if not base_clone.exists():
        return (
            str(resolved),
            repo_alias,
            True,
            (
                f"Base clone not found at {base_clone} for versioned snapshot {repo_path}. "
                "Cannot index versioned snapshot (immutable)."
            ),
        )

    logger.info(
        "Provider index: using base clone %s instead of versioned snapshot %s",
        base_clone,
        repo_path,
    )
    return str(base_clone), repo_alias, True, None


def _run_provider_subprocess(
    cmd: list,
    actual_path: str,
    env: dict,
    phase_name: str,
    index_types: list,
    progress_callback,
) -> tuple:
    """Run cidx index subprocess with progress reporting.

    Returns (success: bool, stdout: str, stderr: str).
    """
    from code_indexer.services.progress_phase_allocator import ProgressPhaseAllocator
    from code_indexer.services.progress_subprocess_runner import (
        IndexingSubprocessError,
        gather_repo_metrics,
        run_with_popen_progress,
    )

    file_count, commit_count = gather_repo_metrics(actual_path)
    allocator = ProgressPhaseAllocator()
    allocator.calculate_weights(
        index_types=index_types,
        file_count=file_count,
        commit_count=commit_count,
    )

    all_stdout: list = []
    all_stderr: list = []

    try:
        from code_indexer.server.services.config_seeding import seed_provider_config

        seed_provider_config(actual_path)
    except Exception as _seed_exc:  # noqa: BLE001
        logger.debug("Bug #678: seed_provider_config failed (non-fatal): %s", _seed_exc)

    try:
        run_with_popen_progress(
            command=cmd,
            phase_name=phase_name,
            allocator=allocator,
            progress_callback=progress_callback,
            all_stdout=all_stdout,
            all_stderr=all_stderr,
            cwd=actual_path,
            env=env,
            timeout=None,
            error_label=f"provider {phase_name}",
        )
        stdout_out = "".join(all_stdout)
        stderr_out = "".join(all_stderr)
        return True, stdout_out, stderr_out
    except IndexingSubprocessError as exc:
        return False, "".join(all_stdout), str(exc)
    finally:
        try:
            from code_indexer.services.provider_health_bridge import (
                drain_and_feed_monitor,
            )

            drain_and_feed_monitor(actual_path)
        except Exception as _drain_exc:  # noqa: BLE001
            logger.debug(
                "Bug #678: drain_and_feed_monitor failed (non-fatal): %s", _drain_exc
            )


def _provider_index_job(
    repo_path: str,
    provider_name: str,
    clear: bool = False,
    progress_callback=None,
    **kwargs,
) -> Dict[str, Any]:
    """Background job worker for provider index add/recreate."""
    import os

    if not repo_path or not provider_name:
        return {
            "success": False,
            "error": "Missing repo_path or provider_name",
            "provider": provider_name,
        }

    repo_alias = kwargs.get("repo_alias", "")
    actual_path, repo_alias, is_versioned, error = _resolve_versioned_to_base_clone(
        repo_path, repo_alias
    )
    if error:
        return {"success": False, "error": error, "provider": provider_name}

    env = os.environ.copy()
    api_key = _resolve_provider_api_key(provider_name)
    if api_key:
        if provider_name == "cohere":
            env["CO_API_KEY"] = api_key
        elif provider_name == "voyage-ai":
            env["VOYAGE_API_KEY"] = api_key

    cmd = ["cidx", "index", "--progress-json"]
    if clear:
        cmd.append("--clear")

    success, stdout_out, stderr_out = _run_provider_subprocess(
        cmd,
        actual_path,
        env,
        "semantic",
        ["semantic"],
        progress_callback,
    )

    if success and is_versioned and actual_path != repo_path:
        _post_provider_index_snapshot(
            repo_alias=repo_alias,
            base_clone_path=actual_path,
            old_snapshot_path=repo_path,
        )

    if not success:
        logger.warning(
            "Provider index failed for provider=%s repo=%s", provider_name, repo_path
        )

    return {
        "success": success,
        "stdout": stdout_out[-_PROVIDER_JOB_OUTPUT_TAIL_CHARS:] if stdout_out else "",
        "stderr": stderr_out[-_PROVIDER_JOB_OUTPUT_TAIL_CHARS:] if stderr_out else "",
    }


def _provider_temporal_index_job(
    repo_path: str,
    provider_name: str,
    clear: bool = False,
    progress_callback=None,
    **kwargs,
) -> Dict[str, Any]:
    """Background job for per-provider temporal index (Story #641).

    Called externally from inline_admin_ops.py via __init__.py re-exports.
    """
    if not repo_path or not provider_name:
        return {
            "success": False,
            "error": "Missing repo_path or provider_name",
            "provider": provider_name,
        }

    repo_alias = kwargs.get("repo_alias", "")
    actual_path, repo_alias, is_versioned = _resolve_provider_job_repo_path(
        repo_path, repo_alias
    )
    if is_versioned and actual_path == repo_path and not Path(actual_path).exists():
        return {
            "success": False,
            "error": f"Base clone not found for {repo_path}",
            "provider": provider_name,
        }

    env = _build_provider_api_key_env(provider_name)
    temporal_options = kwargs.get("temporal_options", {}) or {}
    cmd = _build_temporal_index_cmd(clear, temporal_options)

    success, stdout_out, stderr_out = _run_provider_subprocess(
        cmd,
        actual_path,
        env,
        "temporal",
        ["temporal"],
        progress_callback,
    )

    if success:
        if is_versioned and actual_path != repo_path:
            try:
                _post_provider_index_snapshot(
                    repo_alias=repo_alias,
                    base_clone_path=actual_path,
                    old_snapshot_path=repo_path,
                )
            except Exception as exc:
                logger.warning(
                    "Post-temporal-index snapshot failed for %s: %s", repo_alias, exc
                )
        _set_enable_temporal_flag(repo_alias)
    else:
        logger.warning(
            "Temporal provider index failed for provider=%s repo=%s",
            provider_name,
            repo_path,
        )

    return {
        "success": success,
        "provider": provider_name,
        "stdout": stdout_out[-_PROVIDER_JOB_OUTPUT_TAIL_CHARS:] if stdout_out else "",
        "stderr": stderr_out[-_PROVIDER_JOB_OUTPUT_TAIL_CHARS:] if stderr_out else "",
    }


def _handle_provider_index_action(
    action: str,
    provider_name: str,
    repo_alias: str,
    repo_path: str,
    user: User,
    service,
) -> Dict[str, Any]:
    """Handle add/recreate/remove actions for manage_provider_indexes."""
    if action == "remove":
        base_clone_path = _resolve_golden_repo_base_clone(repo_alias)
        if not base_clone_path:
            return _mcp_response(
                {
                    "error": f"Cannot resolve base clone for '{repo_alias}'. Remove requires a writable path."
                }
            )
        _remove_provider_from_config(base_clone_path, provider_name)
        result = service.remove_provider_index(base_clone_path, provider_name)
        return _mcp_response(
            {
                "success": result["removed"],
                "message": result["message"],
                "collection_name": result["collection_name"],
            }
        )

    # add or recreate
    clear = action == "recreate"
    if _utils.app_module.background_job_manager is None:
        return _mcp_response({"error": "Background job manager not available"})

    base_clone_path = _resolve_golden_repo_base_clone(repo_alias)
    if not base_clone_path:
        return _mcp_response(
            {
                "error": f"Cannot resolve base clone for '{repo_alias}'. Write operations require a writable path."
            }
        )
    if not _append_provider_to_config(base_clone_path, provider_name):
        return _mcp_response(
            {
                "error": f"Failed to write provider '{provider_name}' to config at {base_clone_path}"
            }
        )

    job_id = _utils.app_module.background_job_manager.submit_job(
        operation_type=f"provider_index_{action}",
        func=_provider_index_job,
        submitter_username=user.username,
        repo_alias=repo_alias,
        repo_path=repo_path,
        provider_name=provider_name,
        clear=clear,
    )
    return _mcp_response(
        {
            "success": True,
            "job_id": job_id,
            "action": action,
            "provider": provider_name,
            "repository_alias": repo_alias,
            "message": f"Background job submitted to {action} {provider_name} index for {repo_alias}",
        }
    )


def manage_provider_indexes(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Manage provider-specific semantic indexes (Story #490)."""
    try:
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        action = params.get("action", "")
        if not action:
            return _mcp_response({"error": "Missing required parameter: action"})

        service = ProviderIndexService(config=get_config_service().get_config())

        if action == "list_providers":
            providers = service.list_providers()
            return _mcp_response(
                {"success": True, "providers": providers, "count": len(providers)}
            )

        if action == "status":
            repo_alias = params.get("repository_alias", "")
            if not repo_alias:
                return _mcp_response(
                    {"error": "Missing required parameter: repository_alias"}
                )
            repo_path = _resolve_golden_repo_base_clone(repo_alias)
            if not repo_path:
                repo_path = _resolve_golden_repo_path(repo_alias)
                if not repo_path:
                    return _mcp_response(
                        {"error": f"Repository '{repo_alias}' not found"}
                    )
            status = service.get_provider_index_status(repo_path, repo_alias)
            return _mcp_response(
                {
                    "success": True,
                    "repository_alias": repo_alias,
                    "provider_indexes": status,
                }
            )

        provider_name = params.get("provider", "")
        repo_alias = params.get("repository_alias", "")
        if not provider_name:
            return _mcp_response({"error": "Missing required parameter: provider"})
        if not repo_alias:
            return _mcp_response(
                {"error": "Missing required parameter: repository_alias"}
            )

        error = service.validate_provider(provider_name)
        if error:
            providers = service.list_providers()
            return _mcp_response(
                {"error": error, "available_providers": [p["name"] for p in providers]}
            )

        repo_path = _resolve_golden_repo_path(repo_alias)
        if not repo_path:
            return _mcp_response({"error": f"Repository '{repo_alias}' not found"})

        if action in ("add", "recreate", "remove"):
            return _handle_provider_index_action(
                action, provider_name, repo_alias, repo_path, user, service
            )

        return _mcp_response({"error": f"Unknown action: {action}"})
    except Exception as e:
        logger.error("manage_provider_indexes error: %s", e, exc_info=True)
        return _mcp_response({"error": str(e)})


def bulk_add_provider_index(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Bulk add provider index to all repositories (Story #490)."""
    try:
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        provider_name = params.get("provider", "")
        if not provider_name:
            return _mcp_response({"error": "Missing required parameter: provider"})

        service = ProviderIndexService(config=get_config_service().get_config())
        error = service.validate_provider(provider_name)
        if error:
            providers = service.list_providers()
            return _mcp_response(
                {"error": error, "available_providers": [p["name"] for p in providers]}
            )

        global_repos = _list_global_repos()
        filter_pattern = params.get("filter")
        job_ids = []
        skipped = []

        if _utils.app_module.background_job_manager is None:
            return _mcp_response({"error": "Background job manager not available"})

        for repo in global_repos:
            alias = repo.get("alias_name", "")
            if filter_pattern:
                category = repo.get("category", "")
                if filter_pattern.startswith("category:"):
                    filter_cat = filter_pattern.split(":", 1)[1]
                    if filter_cat.lower() not in category.lower():
                        continue

            repo_path = _resolve_golden_repo_path(alias)
            if not repo_path:
                continue
            status = service.get_provider_index_status(repo_path, alias)
            if status.get(provider_name, {}).get("exists"):
                skipped.append(alias)
                continue

            base_clone_path = _resolve_golden_repo_base_clone(alias)
            if not base_clone_path or not _append_provider_to_config(
                base_clone_path, provider_name
            ):
                logger.warning("bulk_add_provider_index: skipping %s", alias)
                skipped.append(alias)
                continue

            job_id = _utils.app_module.background_job_manager.submit_job(
                operation_type="provider_index_add",
                func=_provider_index_job,
                submitter_username=user.username,
                repo_alias=alias,
                repo_path=repo_path,
                provider_name=provider_name,
                clear=False,
            )
            job_ids.append({"alias": alias, "job_id": job_id})

        return _mcp_response(
            {
                "success": True,
                "provider": provider_name,
                "jobs_created": len(job_ids),
                "jobs": job_ids,
                "skipped": skipped,
                "skipped_count": len(skipped),
                "message": f"Created {len(job_ids)} jobs, skipped {len(skipped)} repos",
            }
        )
    except Exception as e:
        logger.error("bulk_add_provider_index error: %s", e, exc_info=True)
        return _mcp_response({"error": str(e)})


def get_provider_health(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get provider health metrics (Story #491)."""
    try:
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        monitor = ProviderHealthMonitor.get_instance()
        provider = params.get("provider")
        health = monitor.get_health(provider)

        result = {}
        for pname, status in health.items():
            result[pname] = {
                "status": status.status,
                "health_score": status.health_score,
                "p50_latency_ms": status.p50_latency_ms,
                "p95_latency_ms": status.p95_latency_ms,
                "p99_latency_ms": status.p99_latency_ms,
                "error_rate": status.error_rate,
                "availability": status.availability,
                "total_requests": status.total_requests,
                "successful_requests": status.successful_requests,
                "failed_requests": status.failed_requests,
                "window_minutes": status.window_minutes,
            }

        return _mcp_response({"success": True, "provider_health": result})
    except Exception as e:
        logger.error("get_provider_health error: %s", e, exc_info=True)
        return _mcp_response({"error": str(e)})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _register(registry: dict) -> None:
    """Register repo handlers into HANDLER_REGISTRY."""
    registry["discover_repositories"] = discover_repositories
    registry["list_repositories"] = list_repositories
    registry["activate_repository"] = activate_repository
    registry["deactivate_repository"] = deactivate_repository
    registry["get_repository_status"] = get_repository_status
    registry["sync_repository"] = sync_repository
    registry["switch_branch"] = switch_branch
    registry["get_branches"] = get_branches
    registry["check_health"] = check_health
    registry["check_hnsw_health"] = check_hnsw_health
    registry["add_golden_repo"] = add_golden_repo
    registry["remove_golden_repo"] = remove_golden_repo
    registry["refresh_golden_repo"] = refresh_golden_repo
    registry["change_golden_repo_branch"] = change_golden_repo_branch
    registry["get_repository_statistics"] = get_repository_statistics
    registry["get_all_repositories_status"] = get_all_repositories_status
    registry["manage_composite_repository"] = manage_composite_repository
    registry["list_global_repos"] = handle_list_global_repos
    registry["global_repo_status"] = handle_global_repo_status
    registry["add_golden_repo_index"] = handle_add_golden_repo_index
    registry["get_golden_repo_indexes"] = handle_get_golden_repo_indexes
    registry["list_repo_categories"] = list_repo_categories
    registry["manage_provider_indexes"] = manage_provider_indexes
    registry["bulk_add_provider_index"] = bulk_add_provider_index
    registry["get_provider_health"] = get_provider_health
