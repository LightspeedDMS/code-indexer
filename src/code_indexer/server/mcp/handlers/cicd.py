"""CI/CD handlers -- GitHub Actions and GitLab CI pipeline management.

Domain module for CI/CD handlers. Part of the handlers package
modularization (Story #496).
"""

from __future__ import annotations

import logging
from typing import Dict, Any, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    pass

from code_indexer.server.auth.user_manager import User
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.middleware.correlation import get_correlation_id

from ._utils import (
    _coerce_int,
    _mcp_response,
    _list_global_repos,
    _get_access_filtering_service,
)

logger = logging.getLogger(__name__)


# ============================================================================
# CI/CD credential helpers (Story #404)
# ============================================================================


def _derive_forge_host(base_url: Optional[str], platform: str) -> str:
    """Extract forge host from base_url parameter or use platform default.

    Story #404 AC5: forge_host derivation.
    - Strips protocol (https://, http://)
    - Strips trailing slash
    - Defaults to github.com or gitlab.com when base_url absent

    Args:
        base_url: Optional URL parameter from handler args
        platform: "github" or "gitlab"

    Returns:
        Forge host string (e.g. "github.com", "gitlab.example.com")
    """
    if base_url:
        host = base_url
        # Strip protocol prefix
        for prefix in ("https://", "http://"):
            if host.startswith(prefix):
                host = host[len(prefix) :]
                break
        # Strip trailing slash
        host = host.rstrip("/")
        if host:
            return host

    # Platform defaults
    if platform == "github":
        return "github.com"
    return "gitlab.com"


def _get_personal_credential_for_host(
    username: str, forge_host: str
) -> Optional[Dict[str, Any]]:
    """Fetch per-user PAT credential from GitCredentialManager.

    Story #404: Shared internal helper used by both read and write token resolvers.

    Args:
        username: CIDX username
        forge_host: Forge hostname (e.g. "github.com")

    Returns:
        Credential dict with 'token' key, or None if not found
    """
    from ...services.config_service import get_config_service
    from ...services.git_credential_manager import GitCredentialManager

    try:
        config_service = get_config_service()
        db_path = str(
            config_service.config_manager.server_dir / "data" / "cidx_server.db"
        )
        manager = GitCredentialManager(db_path)
        return manager.get_credential_for_host(username, forge_host)
    except Exception as e:
        logger.warning(
            f"Failed to retrieve personal credential for {username}@{forge_host}: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return None


def _resolve_cicd_project_access(
    project_identifier: str, platform: str, username: str
) -> Optional[str]:
    """Map CI/CD project identifier to golden repo and enforce group access.

    Story #404 AC1: Group access control for CI/CD handlers.

    Algorithm:
    1. Extract owner/project from clone URLs in registry
    2. Match project_identifier against registered golden repos
    3. If no match: allow (ad-hoc query)
    4. If match but no AccessFilteringService: allow
    5. If match and AccessFilteringService: check group membership
       - Allowed -> return None
       - Denied -> return "not found" error (invisible repo pattern)

    Args:
        project_identifier: GitLab "namespace/project" or GitHub "owner/repo"
        platform: "github" or "gitlab" (currently unused, future use)
        username: CIDX username for group membership check

    Returns:
        None if allowed, error message string if denied
    """

    def _extract_project_path(repo_url: str) -> Optional[str]:
        """Extract owner/project from a clone URL."""
        if not repo_url:
            return None
        url = repo_url.strip()
        # Strip .git suffix
        if url.endswith(".git"):
            url = url[:-4]
        # SSH format: git@github.com:owner/repo
        if url.startswith("git@"):
            colon_idx = url.find(":")
            if colon_idx != -1:
                return url[colon_idx + 1 :]
            return None
        # HTTPS format: https://github.com/owner/repo
        for prefix in ("https://", "http://"):
            if url.startswith(prefix):
                url = url[len(prefix) :]
                break
        # Remove host: first path component is host
        slash_idx = url.find("/")
        if slash_idx != -1:
            return url[slash_idx + 1 :]
        return None

    try:
        repos = _list_global_repos()
    except Exception as e:
        logger.warning(
            f"Failed to load golden repos registry for CI/CD access check: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return None  # Fail open on registry errors

    # Match project_identifier against repo clone URLs
    matched_alias: Optional[str] = None
    for repo in repos:
        repo_url = repo.get("repo_url") or ""
        project_path = _extract_project_path(repo_url)
        if project_path and project_path.lower() == project_identifier.lower():
            matched_alias = repo.get("alias_name")
            break

    if matched_alias is None:
        # No registered golden repo matches: allow ad-hoc query
        return None

    # Check group access via AccessFilteringService
    access_svc = _get_access_filtering_service()
    if access_svc is None:
        # No groups configured: allow all
        return None

    accessible = access_svc.get_accessible_repos(username)
    # Strip -global suffix to get base name for comparison
    base_alias = matched_alias
    if base_alias.lower().endswith("-global"):
        base_alias = base_alias[: -len("-global")]

    if base_alias not in accessible:
        return f"Project '{project_identifier}' not found."

    return None  # Allowed


def _resolve_cicd_read_token(
    platform: str, user: Any, forge_host: str
) -> Optional[str]:
    """Resolve token for CI/CD read operations with fallback chain.

    Story #404 AC4: Global PAT -> personal PAT fallback for read handlers.

    Priority:
    1. Global CI token (TokenAuthenticator.resolve_token)
    2. Per-user personal PAT (GitCredentialManager.get_credential_for_host)
    3. None (caller handles the no-token error)

    Args:
        platform: "github" or "gitlab"
        user: Authenticated user (User object with .username)
        forge_host: Forge hostname for personal credential lookup

    Returns:
        Token string or None
    """
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    # Priority 1: Global CI token
    token = TokenAuthenticator.resolve_token(platform)
    if token:
        return str(token)

    # Priority 2: Personal PAT fallback
    logger.info(
        f"Global CI token unavailable for {platform}, using personal credential",
        extra={"correlation_id": get_correlation_id()},
    )
    credential = _get_personal_credential_for_host(user.username, forge_host)
    if credential:
        return credential.get("token")

    return None


def _resolve_cicd_write_token(
    platform: str, user: Any, forge_host: str
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve token for CI/CD write operations (personal PAT ONLY).

    Story #404 AC2: Write operations NEVER use global CI token.

    Args:
        platform: "github" or "gitlab" (for error message context)
        user: Authenticated user
        forge_host: Forge hostname for personal credential lookup

    Returns:
        (token, None) on success
        (None, error_message) when no personal credential configured
    """
    credential = _get_personal_credential_for_host(user.username, forge_host)
    if credential:
        return credential.get("token"), None

    error_msg = (
        f"Configure personal git credential for {forge_host} to perform "
        "write operations. Use configure_git_credential tool."
    )
    return None, error_msg


# Story #633: GitHub Actions Monitoring Handlers
async def handle_gh_actions_list_runs(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gh_actions_list_runs tool.

    Lists workflow runs for a repository with optional filtering by branch and status.
    Implements AC1-AC3 of Story #633.

    Args:
        args: Tool arguments containing:
            - repository (str): Repository in "owner/repo" format
            - branch (str, optional): Filter by branch name
            - status (str, optional): Filter by run status
            - limit (int, optional): Maximum runs to return (default 10)
        user: Authenticated user

    Returns:
        MCP response with workflow runs list
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        repository = args.get("repository")
        if not repository:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repository"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GH_TOKEN environment variable or configure token storage.",
                }
            )

        # Extract optional parameters
        branch = args.get("branch")
        status = args.get("status")
        limit = _coerce_int(args.get("limit"), 10)

        # Create client and list runs
        client = GitHubActionsClient(token)
        runs = await client.list_runs(
            repository=repository, branch=branch, status=status
        )

        return _mcp_response(
            {
                "success": True,
                "repository": repository,
                "runs": runs,
                "count": len(runs),
                "filters": {
                    "branch": branch,
                    "status": status,
                    "limit": limit,
                },
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-081",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-082",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{repository}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gh_actions_list_runs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gh_actions_get_run(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Handler for gh_actions_get_run tool.

    Gets detailed information about a specific workflow run.
    Implements AC4 of Story #633.

    Args:
        args: Tool arguments containing:
            - repository (str): Repository in "owner/repo" format
            - run_id (int): Workflow run ID
        user: Authenticated user

    Returns:
        MCP response with detailed run information
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        repository = args.get("repository")
        run_id = args.get("run_id")
        if not repository:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repository"}
            )
        if not run_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: run_id"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GH_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and get run details
        client = GitHubActionsClient(token)
        run_info = await client.get_run(repository=repository, run_id=run_id)

        return _mcp_response(
            {
                "success": True,
                "repository": repository,
                "run_id": run_id,
                "run": run_info,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-083",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-084",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{repository}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gh_actions_get_run: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gh_actions_search_logs(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gh_actions_search_logs tool.

    Searches workflow run logs using ripgrep pattern matching.
    Implements AC5 of Story #633.

    Args:
        args: Tool arguments containing:
            - repository (str): Repository in "owner/repo" format
            - run_id (int): Workflow run ID
            - pattern (str): Search pattern (regex)
            - context_lines (int, optional): Context lines around matches (default 2)
        user: Authenticated user

    Returns:
        MCP response with search matches
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        repository = args.get("repository")
        run_id = args.get("run_id")
        pattern = args.get("pattern")
        if not repository:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repository"}
            )
        if not run_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: run_id"}
            )
        if not pattern:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: pattern"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GH_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and search logs
        client = GitHubActionsClient(token)
        matches = await client.search_logs(
            repository=repository,
            run_id=run_id,
            pattern=pattern,
        )

        return _mcp_response(
            {
                "success": True,
                "repository": repository,
                "run_id": run_id,
                "pattern": pattern,
                "matches": matches,
                "match_count": len(matches),
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-085",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-086",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{repository}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gh_actions_search_logs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gh_actions_get_job_logs(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gh_actions_get_job_logs tool.

    Gets complete logs for a specific job within a workflow run.
    Implements AC6 of Story #633.

    Args:
        args: Tool arguments containing:
            - repository (str): Repository in "owner/repo" format
            - job_id (int): Job ID
        user: Authenticated user

    Returns:
        MCP response with job logs
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        repository = args.get("repository")
        job_id = args.get("job_id")
        if not repository:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repository"}
            )
        if not job_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: job_id"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GH_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and get job logs
        client = GitHubActionsClient(token)
        logs = await client.get_job_logs(repository=repository, job_id=job_id)

        return _mcp_response(
            {
                "success": True,
                "repository": repository,
                "job_id": job_id,
                "logs": logs,
                "log_length": len(logs),
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-087",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-088",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{repository}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gh_actions_get_job_logs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gh_actions_retry_run(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gh_actions_retry_run tool.

    Retries a failed workflow run.
    Implements AC7 of Story #633.

    Args:
        args: Tool arguments containing:
            - repository (str): Repository in "owner/repo" format
            - run_id (int): Workflow run ID to retry
        user: Authenticated user

    Returns:
        MCP response confirming retry operation
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        repository = args.get("repository")
        run_id = args.get("run_id")
        if not repository:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repository"}
            )
        if not run_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: run_id"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GH_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and retry run
        client = GitHubActionsClient(token)
        result = await client.retry_run(repository=repository, run_id=run_id)

        return _mcp_response(
            {
                "success": True,
                "repository": repository,
                "run_id": run_id,
                "message": "Workflow run retry triggered successfully",
                "result": result,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-089",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-090",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{repository}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gh_actions_retry_run: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gh_actions_cancel_run(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gh_actions_cancel_run tool.

    Cancels a running or queued workflow run.
    Implements AC8 of Story #633.

    Args:
        args: Tool arguments containing:
            - repository (str): Repository in "owner/repo" format
            - run_id (int): Workflow run ID to cancel
        user: Authenticated user

    Returns:
        MCP response confirming cancellation operation
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        repository = args.get("repository")
        run_id = args.get("run_id")
        if not repository:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repository"}
            )
        if not run_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: run_id"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GH_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and cancel run
        client = GitHubActionsClient(token)
        result = await client.cancel_run(repository=repository, run_id=run_id)

        return _mcp_response(
            {
                "success": True,
                "repository": repository,
                "run_id": run_id,
                "message": "Workflow run cancelled successfully",
                "result": result,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-091",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-092",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{repository}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gh_actions_cancel_run: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# gh_actions_* HANDLER_REGISTRY entries removed (Story #222 TODO 5).
# Handler functions preserved below for REST routes in cicd.py.


# ============================================================================
# GitLab CI Handlers (Story #634)
# ============================================================================


async def handle_gitlab_ci_list_pipelines(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gitlab_ci_list_pipelines tool.

    Lists pipelines for a GitLab project with optional filtering by ref and status.
    Implements AC1-AC3 of Story #634.

    Args:
        args: Tool arguments containing:
            - project_id (str): GitLab project ID or path (e.g., "gitlab-org/gitlab")
            - ref (str, optional): Filter by branch/tag name
            - status (str, optional): Filter by pipeline status
            - limit (int, optional): Maximum pipelines to return (default 10)
        user: Authenticated user

    Returns:
        MCP response with pipelines list
    """
    from code_indexer.server.clients.gitlab_ci_client import (
        GitLabCIClient,
        GitLabAuthenticationError,
        GitLabProjectNotFoundError,
    )

    try:
        # Validate required parameters
        project_id = args.get("project_id")
        if not project_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: project_id"}
            )

        # Story #404 AC1: Group access check BEFORE token resolution (fail fast)
        access_error = _resolve_cicd_project_access(project_id, "gitlab", user.username)
        if access_error:
            return _mcp_response({"success": False, "error": access_error})

        # Extract optional parameters
        ref = args.get("ref")
        status = args.get("status")
        limit = _coerce_int(args.get("limit"), 10)
        base_url = args.get("base_url", "https://gitlab.com")

        # Story #404 AC4: Resilient read token (global CI -> personal PAT fallback)
        forge_host = _derive_forge_host(args.get("base_url"), "gitlab")
        token = _resolve_cicd_read_token("gitlab", user, forge_host)
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitLab token not found. Set GITLAB_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and list pipelines (CRITICAL: keyword)
        client = GitLabCIClient(token, base_url=base_url)
        pipelines = await client.list_pipelines(
            project_id=project_id, ref=ref, status=status
        )

        return _mcp_response(
            {
                "success": True,
                "project_id": project_id,
                "pipelines": pipelines,
                "count": len(pipelines),
                "filters": {
                    "ref": ref,
                    "status": status,
                    "limit": limit,
                },
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitLabAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-093",
                f"GitLab authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitLab authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitLabProjectNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-094",
                f"GitLab project not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Project '{project_id}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gitlab_ci_list_pipelines: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gitlab_ci_get_pipeline(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gitlab_ci_get_pipeline tool.

    Gets detailed information about a specific pipeline including jobs.
    Implements AC4 of Story #634.

    Args:
        args: Tool arguments containing:
            - project_id (str): GitLab project ID or path
            - pipeline_id (int): Pipeline ID
        user: Authenticated user

    Returns:
        MCP response with detailed pipeline information
    """
    from code_indexer.server.clients.gitlab_ci_client import (
        GitLabCIClient,
        GitLabAuthenticationError,
        GitLabProjectNotFoundError,
    )

    try:
        # Validate required parameters
        project_id = args.get("project_id")
        pipeline_id = args.get("pipeline_id")
        if not project_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: project_id"}
            )
        if not pipeline_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: pipeline_id"}
            )

        # Story #404 AC1: Group access check BEFORE token resolution (fail fast)
        access_error = _resolve_cicd_project_access(project_id, "gitlab", user.username)
        if access_error:
            return _mcp_response({"success": False, "error": access_error})

        # Extract optional parameters
        base_url = args.get("base_url", "https://gitlab.com")

        # Story #404 AC4: Resilient read token (global CI -> personal PAT fallback)
        forge_host = _derive_forge_host(args.get("base_url"), "gitlab")
        token = _resolve_cicd_read_token("gitlab", user, forge_host)
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitLab token not found. Set GITLAB_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and get pipeline details (CRITICAL: keyword)
        client = GitLabCIClient(token, base_url=base_url)
        pipeline_info = await client.get_pipeline(
            project_id=project_id, pipeline_id=pipeline_id
        )

        return _mcp_response(
            {
                "success": True,
                "project_id": project_id,
                "pipeline_id": pipeline_id,
                "pipeline": pipeline_info,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitLabAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-095",
                f"GitLab authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitLab authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitLabProjectNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-096",
                f"GitLab project not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Project '{project_id}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gitlab_ci_get_pipeline: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gitlab_ci_search_logs(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gitlab_ci_search_logs tool.

    Searches pipeline job logs using ripgrep patterns.
    Implements AC5 of Story #634.

    Args:
        args: Tool arguments containing:
            - project_id (str): GitLab project ID or path
            - pipeline_id (int): Pipeline ID
            - pattern (str): Ripgrep search pattern
            - case_sensitive (bool, optional): Case-sensitive search (default True)
        user: Authenticated user

    Returns:
        MCP response with matching log lines
    """
    from code_indexer.server.clients.gitlab_ci_client import (
        GitLabCIClient,
        GitLabAuthenticationError,
        GitLabProjectNotFoundError,
    )

    try:
        # Validate required parameters
        project_id = args.get("project_id")
        pipeline_id = args.get("pipeline_id")
        pattern = args.get("pattern")
        if not project_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: project_id"}
            )
        if not pipeline_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: pipeline_id"}
            )
        if not pattern:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: pattern"}
            )

        # Story #404 AC1: Group access check BEFORE token resolution (fail fast)
        access_error = _resolve_cicd_project_access(project_id, "gitlab", user.username)
        if access_error:
            return _mcp_response({"success": False, "error": access_error})

        # Extract optional parameters
        case_sensitive = args.get("case_sensitive", True)
        base_url = args.get("base_url", "https://gitlab.com")

        # Story #404 AC4: Resilient read token (global CI -> personal PAT fallback)
        forge_host = _derive_forge_host(args.get("base_url"), "gitlab")
        token = _resolve_cicd_read_token("gitlab", user, forge_host)
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitLab token not found. Set GITLAB_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and search logs (CRITICAL: keyword)
        client = GitLabCIClient(token, base_url=base_url)
        matches = await client.search_logs(
            project_id=project_id,
            pipeline_id=pipeline_id,
            pattern=pattern,
            case_sensitive=case_sensitive,
        )

        return _mcp_response(
            {
                "success": True,
                "project_id": project_id,
                "pipeline_id": pipeline_id,
                "pattern": pattern,
                "matches": matches,
                "count": len(matches),
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitLabAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-097",
                f"GitLab authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitLab authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitLabProjectNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-098",
                f"GitLab project not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Project '{project_id}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gitlab_ci_search_logs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gitlab_ci_get_job_logs(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gitlab_ci_get_job_logs tool.

    Gets complete logs for a specific job.
    Implements AC6 of Story #634.

    Args:
        args: Tool arguments containing:
            - project_id (str): GitLab project ID or path
            - job_id (int): Job ID
        user: Authenticated user

    Returns:
        MCP response with complete job logs
    """
    from code_indexer.server.clients.gitlab_ci_client import (
        GitLabCIClient,
        GitLabAuthenticationError,
        GitLabProjectNotFoundError,
    )

    try:
        # Validate required parameters
        project_id = args.get("project_id")
        job_id = args.get("job_id")
        if not project_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: project_id"}
            )
        if not job_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: job_id"}
            )

        # Story #404 AC1: Group access check BEFORE token resolution (fail fast)
        access_error = _resolve_cicd_project_access(project_id, "gitlab", user.username)
        if access_error:
            return _mcp_response({"success": False, "error": access_error})

        # Extract optional parameters
        base_url = args.get("base_url", "https://gitlab.com")

        # Story #404 AC4: Resilient read token (global CI -> personal PAT fallback)
        forge_host = _derive_forge_host(args.get("base_url"), "gitlab")
        token = _resolve_cicd_read_token("gitlab", user, forge_host)
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitLab token not found. Set GITLAB_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and get job logs (CRITICAL: keyword)
        client = GitLabCIClient(token, base_url=base_url)
        logs = await client.get_job_logs(project_id=project_id, job_id=job_id)

        return _mcp_response(
            {
                "success": True,
                "project_id": project_id,
                "job_id": job_id,
                "logs": logs,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitLabAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-099",
                f"GitLab authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitLab authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitLabProjectNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-100",
                f"GitLab project not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Project '{project_id}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gitlab_ci_get_job_logs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gitlab_ci_retry_pipeline(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gitlab_ci_retry_pipeline tool.

    Retries a failed pipeline.
    Implements AC7 of Story #634.

    Args:
        args: Tool arguments containing:
            - project_id (str): GitLab project ID or path
            - pipeline_id (int): Pipeline ID to retry
        user: Authenticated user

    Returns:
        MCP response confirming retry operation
    """
    from code_indexer.server.clients.gitlab_ci_client import (
        GitLabCIClient,
        GitLabAuthenticationError,
        GitLabProjectNotFoundError,
    )

    try:
        # Validate required parameters
        project_id = args.get("project_id")
        pipeline_id = args.get("pipeline_id")
        if not project_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: project_id"}
            )
        if not pipeline_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: pipeline_id"}
            )

        # Story #404 AC1: Group access check BEFORE token resolution (fail fast)
        access_error = _resolve_cicd_project_access(project_id, "gitlab", user.username)
        if access_error:
            return _mcp_response({"success": False, "error": access_error})

        # Extract optional parameters
        base_url = args.get("base_url", "https://gitlab.com")

        # Story #404 AC2: Per-user write token ONLY (never global CI token)
        forge_host = _derive_forge_host(args.get("base_url"), "gitlab")
        token, token_error = _resolve_cicd_write_token("gitlab", user, forge_host)
        if token_error:
            return _mcp_response({"success": False, "error": token_error})

        # Story #404 AC3: Audit log BEFORE API call
        logger.info(
            f"CI/CD write operation: user={user.username} op=retry_pipeline "
            f"project={project_id} pipeline={pipeline_id}",
            extra={"correlation_id": get_correlation_id()},
        )

        # Create client and retry pipeline (CRITICAL: keyword)
        client = GitLabCIClient(token, base_url=base_url)
        result = await client.retry_pipeline(
            project_id=project_id, pipeline_id=pipeline_id
        )

        return _mcp_response(
            {
                "success": True,
                "project_id": project_id,
                "pipeline_id": pipeline_id,
                "message": "Pipeline retried successfully",
                "result": result,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitLabAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-101",
                f"GitLab authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitLab authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitLabProjectNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-102",
                f"GitLab project not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Project '{project_id}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gitlab_ci_retry_pipeline: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gitlab_ci_cancel_pipeline(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gitlab_ci_cancel_pipeline tool.

    Cancels a running or pending pipeline.
    Implements AC8 of Story #634.

    Args:
        args: Tool arguments containing:
            - project_id (str): GitLab project ID or path
            - pipeline_id (int): Pipeline ID to cancel
        user: Authenticated user

    Returns:
        MCP response confirming cancellation operation
    """
    from code_indexer.server.clients.gitlab_ci_client import (
        GitLabCIClient,
        GitLabAuthenticationError,
        GitLabProjectNotFoundError,
    )

    try:
        # Validate required parameters
        project_id = args.get("project_id")
        pipeline_id = args.get("pipeline_id")
        if not project_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: project_id"}
            )
        if not pipeline_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: pipeline_id"}
            )

        # Story #404 AC1: Group access check BEFORE token resolution (fail fast)
        access_error = _resolve_cicd_project_access(project_id, "gitlab", user.username)
        if access_error:
            return _mcp_response({"success": False, "error": access_error})

        # Extract optional parameters
        base_url = args.get("base_url", "https://gitlab.com")

        # Story #404 AC2: Per-user write token ONLY (never global CI token)
        forge_host = _derive_forge_host(args.get("base_url"), "gitlab")
        token, token_error = _resolve_cicd_write_token("gitlab", user, forge_host)
        if token_error:
            return _mcp_response({"success": False, "error": token_error})

        # Story #404 AC3: Audit log BEFORE API call
        logger.info(
            f"CI/CD write operation: user={user.username} op=cancel_pipeline "
            f"project={project_id} pipeline={pipeline_id}",
            extra={"correlation_id": get_correlation_id()},
        )

        # Create client and cancel pipeline (CRITICAL: keyword)
        client = GitLabCIClient(token, base_url=base_url)
        result = await client.cancel_pipeline(
            project_id=project_id, pipeline_id=pipeline_id
        )

        return _mcp_response(
            {
                "success": True,
                "project_id": project_id,
                "pipeline_id": pipeline_id,
                "message": "Pipeline cancelled successfully",
                "result": result,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitLabAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-103",
                f"GitLab authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitLab authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitLabProjectNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-104",
                f"GitLab project not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Project '{project_id}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gitlab_ci_cancel_pipeline: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# =============================================================================
# GITHUB ACTIONS HANDLERS (Story #633)
# =============================================================================


async def handle_github_actions_list_runs(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for github_actions_list_runs tool.

    Lists workflow runs for a GitHub repository with optional filtering.
    Implements AC1-AC3 of Story #633.

    Args:
        args: Tool arguments containing:
            - owner (str): Repository owner
            - repo (str): Repository name
            - workflow_id (str, optional): Filter by workflow ID or filename
            - status (str, optional): Filter by run status
            - branch (str, optional): Filter by branch name
            - limit (int, optional): Maximum runs to return (default 20)
        user: Authenticated user

    Returns:
        MCP response with workflow runs list
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )

    try:
        # Validate required parameters
        owner = args.get("owner")
        repo = args.get("repo")
        if not owner:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: owner"}
            )
        if not repo:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo"}
            )

        # Story #404 AC1: Group access check BEFORE token resolution (fail fast)
        project_identifier = f"{owner}/{repo}"
        access_error = _resolve_cicd_project_access(
            project_identifier, "github", user.username
        )
        if access_error:
            return _mcp_response({"success": False, "error": access_error})

        # Story #404 AC4: Resilient read token (global CI -> personal PAT fallback)
        forge_host = _derive_forge_host(args.get("base_url"), "github")
        token = _resolve_cicd_read_token("github", user, forge_host)
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GITHUB_TOKEN environment variable or configure token storage.",
                }
            )

        # Extract optional parameters
        workflow_id = args.get("workflow_id")
        status = args.get("status")
        branch = args.get("branch")
        limit = _coerce_int(args.get("limit"), 20)

        # Combine owner and repo into repository format
        repository = f"{owner}/{repo}"

        # Create client and list runs (CRITICAL: keyword)
        client = GitHubActionsClient(token)
        runs = await client.list_runs(
            repository=repository, branch=branch, status=status
        )

        # Apply limit to results
        if limit:
            runs = runs[:limit]

        return _mcp_response(
            {
                "success": True,
                "owner": owner,
                "repo": repo,
                "runs": runs,
                "count": len(runs),
                "filters": {
                    "workflow_id": workflow_id,
                    "status": status,
                    "branch": branch,
                    "limit": limit,
                },
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-105",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-106",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{owner}/{repo}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in github_actions_list_runs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_github_actions_get_run(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for github_actions_get_run tool.

    Gets detailed information for a specific workflow run.
    Implements AC4 of Story #633.

    Args:
        args: Tool arguments containing:
            - owner (str): Repository owner
            - repo (str): Repository name
            - run_id (int): Workflow run ID
        user: Authenticated user

    Returns:
        MCP response with detailed run information
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )

    try:
        # Validate required parameters
        owner = args.get("owner")
        repo = args.get("repo")
        run_id = args.get("run_id")
        if not owner:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: owner"}
            )
        if not repo:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo"}
            )
        if not run_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: run_id"}
            )

        # Story #404 AC1: Group access check BEFORE token resolution (fail fast)
        project_identifier = f"{owner}/{repo}"
        access_error = _resolve_cicd_project_access(
            project_identifier, "github", user.username
        )
        if access_error:
            return _mcp_response({"success": False, "error": access_error})

        # Story #404 AC4: Resilient read token (global CI -> personal PAT fallback)
        forge_host = _derive_forge_host(args.get("base_url"), "github")
        token = _resolve_cicd_read_token("github", user, forge_host)
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GITHUB_TOKEN environment variable or configure token storage.",
                }
            )

        # Combine owner and repo into repository format
        repository = f"{owner}/{repo}"

        # Create client and get run details (CRITICAL: keyword)
        client = GitHubActionsClient(token)
        run_details = await client.get_run(repository=repository, run_id=run_id)

        return _mcp_response(
            {
                "success": True,
                "owner": owner,
                "repo": repo,
                "run": run_details,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-107",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-108",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{owner}/{repo}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in github_actions_get_run: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_github_actions_search_logs(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for github_actions_search_logs tool.

    Searches workflow run logs for a pattern.
    Implements AC5 of Story #633.

    Args:
        args: Tool arguments containing:
            - owner (str): Repository owner
            - repo (str): Repository name
            - run_id (int): Workflow run ID
            - query (str): Search query string
        user: Authenticated user

    Returns:
        MCP response with matching log lines
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )

    try:
        # Validate required parameters
        owner = args.get("owner")
        repo = args.get("repo")
        run_id = args.get("run_id")
        query = args.get("query")
        if not owner:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: owner"}
            )
        if not repo:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo"}
            )
        if not run_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: run_id"}
            )
        if not query:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: query"}
            )

        # Story #404 AC1: Group access check BEFORE token resolution (fail fast)
        project_identifier = f"{owner}/{repo}"
        access_error = _resolve_cicd_project_access(
            project_identifier, "github", user.username
        )
        if access_error:
            return _mcp_response({"success": False, "error": access_error})

        # Story #404 AC4: Resilient read token (global CI -> personal PAT fallback)
        forge_host = _derive_forge_host(args.get("base_url"), "github")
        token = _resolve_cicd_read_token("github", user, forge_host)
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GITHUB_TOKEN environment variable or configure token storage.",
                }
            )

        # Combine owner and repo into repository format
        repository = f"{owner}/{repo}"

        # Create client and search logs (CRITICAL: keyword)
        client = GitHubActionsClient(token)
        matches = await client.search_logs(
            repository=repository, run_id=run_id, pattern=query
        )

        return _mcp_response(
            {
                "success": True,
                "owner": owner,
                "repo": repo,
                "run_id": run_id,
                "query": query,
                "matches": matches,
                "count": len(matches),
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-109",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-110",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{owner}/{repo}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in github_actions_search_logs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_github_actions_get_job_logs(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for github_actions_get_job_logs tool.

    Gets full log output for a specific job.
    Implements AC6 of Story #633.

    Args:
        args: Tool arguments containing:
            - owner (str): Repository owner
            - repo (str): Repository name
            - job_id (int): Job ID
        user: Authenticated user

    Returns:
        MCP response with full job logs
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )

    try:
        # Validate required parameters
        owner = args.get("owner")
        repo = args.get("repo")
        job_id = args.get("job_id")
        if not owner:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: owner"}
            )
        if not repo:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo"}
            )
        if not job_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: job_id"}
            )

        # Story #404 AC1: Group access check BEFORE token resolution (fail fast)
        project_identifier = f"{owner}/{repo}"
        access_error = _resolve_cicd_project_access(
            project_identifier, "github", user.username
        )
        if access_error:
            return _mcp_response({"success": False, "error": access_error})

        # Story #404 AC4: Resilient read token (global CI -> personal PAT fallback)
        forge_host = _derive_forge_host(args.get("base_url"), "github")
        token = _resolve_cicd_read_token("github", user, forge_host)
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GITHUB_TOKEN environment variable or configure token storage.",
                }
            )

        # Combine owner and repo into repository format
        repository = f"{owner}/{repo}"

        # Create client and get job logs (CRITICAL: keyword)
        client = GitHubActionsClient(token)
        logs = await client.get_job_logs(repository=repository, job_id=job_id)

        return _mcp_response(
            {
                "success": True,
                "owner": owner,
                "repo": repo,
                "job_id": job_id,
                "logs": logs,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-111",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-112",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{owner}/{repo}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in github_actions_get_job_logs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_github_actions_retry_run(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for github_actions_retry_run tool.

    Retries a failed workflow run.
    Implements AC7 of Story #633.

    Args:
        args: Tool arguments containing:
            - owner (str): Repository owner
            - repo (str): Repository name
            - run_id (int): Workflow run ID to retry
        user: Authenticated user

    Returns:
        MCP response confirming retry operation
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )

    try:
        # Validate required parameters
        owner = args.get("owner")
        repo = args.get("repo")
        run_id = args.get("run_id")
        if not owner:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: owner"}
            )
        if not repo:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo"}
            )
        if not run_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: run_id"}
            )

        # Story #404 AC1: Group access check BEFORE token resolution (fail fast)
        project_identifier = f"{owner}/{repo}"
        access_error = _resolve_cicd_project_access(
            project_identifier, "github", user.username
        )
        if access_error:
            return _mcp_response({"success": False, "error": access_error})

        # Story #404 AC2: Per-user write token ONLY (never global CI token)
        forge_host = _derive_forge_host(args.get("base_url"), "github")
        token, token_error = _resolve_cicd_write_token("github", user, forge_host)
        if token_error:
            return _mcp_response({"success": False, "error": token_error})

        # Story #404 AC3: Audit log BEFORE API call
        logger.info(
            f"CI/CD write operation: user={user.username} op=retry_run "
            f"project={owner}/{repo} run={run_id}",
            extra={"correlation_id": get_correlation_id()},
        )

        # Combine owner and repo into repository format
        repository = f"{owner}/{repo}"

        # Create client and retry run (CRITICAL: keyword)
        client = GitHubActionsClient(token)
        result = await client.retry_run(repository=repository, run_id=run_id)

        return _mcp_response(
            {
                "success": True,
                "owner": owner,
                "repo": repo,
                "run_id": run_id,
                "message": "Workflow run retry triggered successfully",
                "result": result,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-113",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-114",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{owner}/{repo}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in github_actions_retry_run: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_github_actions_cancel_run(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for github_actions_cancel_run tool.

    Cancels a running workflow.
    Implements AC8 of Story #633.

    Args:
        args: Tool arguments containing:
            - owner (str): Repository owner
            - repo (str): Repository name
            - run_id (int): Workflow run ID to cancel
        user: Authenticated user

    Returns:
        MCP response confirming cancellation operation
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )

    try:
        # Validate required parameters
        owner = args.get("owner")
        repo = args.get("repo")
        run_id = args.get("run_id")
        if not owner:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: owner"}
            )
        if not repo:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo"}
            )
        if not run_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: run_id"}
            )

        # Story #404 AC1: Group access check BEFORE token resolution (fail fast)
        project_identifier = f"{owner}/{repo}"
        access_error = _resolve_cicd_project_access(
            project_identifier, "github", user.username
        )
        if access_error:
            return _mcp_response({"success": False, "error": access_error})

        # Story #404 AC2: Per-user write token ONLY (never global CI token)
        forge_host = _derive_forge_host(args.get("base_url"), "github")
        token, token_error = _resolve_cicd_write_token("github", user, forge_host)
        if token_error:
            return _mcp_response({"success": False, "error": token_error})

        # Story #404 AC3: Audit log BEFORE API call
        logger.info(
            f"CI/CD write operation: user={user.username} op=cancel_run "
            f"project={owner}/{repo} run={run_id}",
            extra={"correlation_id": get_correlation_id()},
        )

        # Combine owner and repo into repository format
        repository = f"{owner}/{repo}"

        # Create client and cancel run (CRITICAL: keyword)
        client = GitHubActionsClient(token)
        result = await client.cancel_run(repository=repository, run_id=run_id)

        return _mcp_response(
            {
                "success": True,
                "owner": owner,
                "repo": repo,
                "run_id": run_id,
                "message": "Workflow run cancelled successfully",
                "result": result,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-115",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-116",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{owner}/{repo}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in github_actions_cancel_run: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def _register(registry: dict) -> None:
    """Register CI/CD handlers into the HANDLER_REGISTRY."""
    # GitLab CI handlers
    registry["gitlab_ci_list_pipelines"] = handle_gitlab_ci_list_pipelines
    registry["gitlab_ci_get_pipeline"] = handle_gitlab_ci_get_pipeline
    registry["gitlab_ci_search_logs"] = handle_gitlab_ci_search_logs
    registry["gitlab_ci_get_job_logs"] = handle_gitlab_ci_get_job_logs
    registry["gitlab_ci_retry_pipeline"] = handle_gitlab_ci_retry_pipeline
    registry["gitlab_ci_cancel_pipeline"] = handle_gitlab_ci_cancel_pipeline
    # GitHub Actions handlers (new style)
    registry["github_actions_list_runs"] = handle_github_actions_list_runs
    registry["github_actions_get_run"] = handle_github_actions_get_run
    registry["github_actions_search_logs"] = handle_github_actions_search_logs
    registry["github_actions_get_job_logs"] = handle_github_actions_get_job_logs
    registry["github_actions_retry_run"] = handle_github_actions_retry_run
    registry["github_actions_cancel_run"] = handle_github_actions_cancel_run
    # NOTE: handle_gh_actions_* (old style) are NOT registered per Story #222 TODO 5.
    # They are preserved for REST routes only.
