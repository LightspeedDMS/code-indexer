"""Pull request handler functions for CIDX MCP server.

Covers: create_pull_request, list_pull_requests, get_pull_request,
list_pull_request_comments, comment_on_pull_request, update_pull_request,
merge_pull_request, close_pull_request.

All handlers auto-detect forge type (github/gitlab) from the repository's
remote URL and auto-fetch PAT credentials from stored git credentials.

Stories: #390, #446, #447, #448, #449, #450, #451, #452.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from code_indexer.server.auth.user_manager import User
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.middleware.correlation import get_correlation_id

from ._utils import _mcp_response, _parse_json_string_array

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Forward references to helpers that remain in _legacy.py (shared with other
# domain modules — git_write, cicd, etc.).  We import lazily inside each
# handler to avoid circular imports at module load time.
# ---------------------------------------------------------------------------


def _get_legacy():
    """Return the _legacy module for access to shared private helpers."""
    import code_indexer.server.mcp.handlers._legacy as _leg

    return _leg


# ---------------------------------------------------------------------------
# Public handlers
# ---------------------------------------------------------------------------


def create_pull_request(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for create_pull_request tool - create a GitHub PR or GitLab MR.

    Story #390: Pull/Merge Request Creation via MCP.

    Auto-detects forge type (github/gitlab) from the repository's remote URL.
    Requires write mode to be active for the repository (AC4).
    Returns the PR/MR URL and number on success (AC6).
    """
    repository_alias = args.get("repository_alias")
    title = args.get("title")
    body = args.get("body", "")
    head = args.get("head")
    base = args.get("base")
    token = args.get("token")

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if not title:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: title"}
        )
    if not head:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: head"}
        )
    if not base:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: base"}
        )

    try:
        from code_indexer.utils.git_runner import run_git_command as _run_git_cmd
        from code_indexer.server.clients.forge_client import (
            detect_forge_type,
            extract_owner_repo,
            GitHubForgeClient,
            GitLabForgeClient,
            ForgeAuthenticationError,
        )
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        leg = _get_legacy()
        _resolve_git_repo_path = leg._resolve_git_repo_path
        _get_golden_repos_dir = leg._get_golden_repos_dir
        _is_writable_repo = leg._is_writable_repo
        _get_pat_credential_for_remote = leg._get_pat_credential_for_remote

        # Resolve repository path
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        # AC4: Require write mode or activated workspace (Bug #391)
        golden_repos_dir = _get_golden_repos_dir()
        if not _is_writable_repo(repository_alias, repo_path, golden_repos_dir):
            return _mcp_response(
                {
                    "success": False,
                    "error": (
                        f"Write mode is not active for '{repository_alias}'. "
                        "Use enter_write_mode before creating a pull request, "
                        "or activate the repository to create a writable workspace."
                    ),
                }
            )

        # Bug #392: Auto-fetch PAT from stored credentials when not provided
        if not token:
            credential, _remote_url, cred_error = _get_pat_credential_for_remote(
                repo_path, "origin", user.username
            )
            if cred_error:
                return _mcp_response({"success": False, "error": cred_error})
            assert credential is not None  # narrowed by cred_error check above
            token = credential["token"]

        # Get remote URL to detect forge type (AC3)
        try:
            url_result = _run_git_cmd(
                ["git", "remote", "get-url", "origin"],
                cwd=Path(repo_path),
            )
            remote_url = url_result.stdout.strip()
        except Exception as e:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-219",
                    f"create_pull_request: failed to get remote URL: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return _mcp_response(
                {"success": False, "error": f"Failed to get remote URL: {e}"}
            )

        # AC3: Auto-detect forge type
        forge_type = detect_forge_type(remote_url)
        if forge_type is None:
            return _mcp_response(
                {
                    "success": False,
                    "error": (
                        f"Cannot determine forge type from remote URL '{remote_url}'. "
                        "Only github and gitlab are supported."
                    ),
                }
            )

        # Extract owner and repo from remote URL
        owner, repo = extract_owner_repo(remote_url)
        host = GitCredentialHelper.extract_host_from_remote_url(remote_url) or (
            "github.com" if forge_type == "github" else "gitlab.com"
        )

        # AC1/AC2: Create PR or MR via forge API
        if forge_type == "github":
            client = GitHubForgeClient()
            result = client.create_pull_request(
                token=token,
                host=host,
                owner=owner,
                repo=repo,
                title=title,
                body=body,
                head=head,
                base=base,
            )
        else:
            client = GitLabForgeClient()
            result = client.create_merge_request(
                token=token,
                host=host,
                owner=owner,
                repo=repo,
                title=title,
                body=body,
                source_branch=head,
                target_branch=base,
            )

        logger.info(
            f"create_pull_request: created {forge_type} PR/MR #{result['number']} "
            f"for '{repository_alias}'",
            extra={"correlation_id": get_correlation_id()},
        )

        # AC6: Return PR/MR URL and number on success
        return _mcp_response(
            {
                "success": True,
                "pr_url": result["url"],
                "pr_number": result["number"],
                "forge_type": forge_type,
            }
        )

    except ForgeAuthenticationError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-219",
                f"create_pull_request: authentication error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except ValueError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-219",
                f"create_pull_request: validation error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in create_pull_request: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def list_pull_requests(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for list_pull_requests tool - list GitHub PRs or GitLab MRs.

    Story #446: list_pull_requests - List PRs/MRs for a repository.

    Auto-detects forge type (github/gitlab) from the repository's remote URL.
    Read-only operation - does not require write mode.
    Returns a list of normalized PR/MR dicts on success.
    """
    from code_indexer.server.clients.forge_client import (
        detect_forge_type,
        extract_owner_repo,
        GitHubForgeClient,
        GitLabForgeClient,
        ForgeAuthenticationError,
    )
    from code_indexer.server.services.git_credential_helper import GitCredentialHelper

    repository_alias = args.get("repository_alias")
    state = args.get("state", "open")
    limit = args.get("limit", 10)
    author = args.get("author")

    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        from code_indexer.utils.git_runner import run_git_command as _run_git_cmd

        leg = _get_legacy()
        _resolve_git_repo_path = leg._resolve_git_repo_path
        _get_pat_credential_for_remote = leg._get_pat_credential_for_remote

        # Resolve repository path
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        # Auto-fetch PAT from stored credentials
        credential, _remote_url_from_cred, cred_error = _get_pat_credential_for_remote(
            repo_path, "origin", user.username
        )
        if cred_error:
            return _mcp_response({"success": False, "error": cred_error})
        assert credential is not None  # narrowed by cred_error check above
        token = credential["token"]

        # Get remote URL to detect forge type
        try:
            url_result = _run_git_cmd(
                ["git", "remote", "get-url", "origin"],
                cwd=Path(repo_path),
            )
            remote_url = url_result.stdout.strip()
        except Exception as e:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-220",
                    f"list_pull_requests: failed to get remote URL: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return _mcp_response(
                {"success": False, "error": f"Failed to get remote URL: {e}"}
            )

        # Auto-detect forge type
        forge_type = detect_forge_type(remote_url)
        if forge_type is None:
            return _mcp_response(
                {
                    "success": False,
                    "error": (
                        f"Cannot determine forge type from remote URL '{remote_url}'. "
                        "Only github and gitlab are supported."
                    ),
                }
            )

        # Extract owner and repo from remote URL
        owner, repo = extract_owner_repo(remote_url)
        host = GitCredentialHelper.extract_host_from_remote_url(remote_url) or (
            "github.com" if forge_type == "github" else "gitlab.com"
        )

        # Call appropriate forge client
        if forge_type == "github":
            gh_client = GitHubForgeClient()
            pull_requests = gh_client.list_pull_requests(
                token=token,
                host=host,
                owner=owner,
                repo=repo,
                state=state,
                limit=limit,
                author=author,
            )
        else:
            gl_client = GitLabForgeClient()
            pull_requests = gl_client.list_merge_requests(
                token=token,
                host=host,
                owner=owner,
                repo=repo,
                state=state,
                limit=limit,
                author=author,
            )

        logger.info(
            f"list_pull_requests: listed {len(pull_requests)} {forge_type} PR/MR(s) "
            f"for '{repository_alias}' (state={state})",
            extra={"correlation_id": get_correlation_id()},
        )

        return _mcp_response(
            {
                "success": True,
                "pull_requests": pull_requests,
                "forge_type": forge_type,
                "count": len(pull_requests),
            }
        )

    except ForgeAuthenticationError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-220",
                f"list_pull_requests: authentication error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except ValueError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-220",
                f"list_pull_requests: validation error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in list_pull_requests: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def get_pull_request(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for get_pull_request tool - get full details of a GitHub PR or GitLab MR.

    Story #447: get_pull_request - Get full PR/MR details.

    Auto-detects forge type (github/gitlab) from the repository's remote URL.
    Read-only operation - does not require write mode.
    Returns a single normalized PR/MR dict on success.
    """
    from code_indexer.server.clients.forge_client import (
        detect_forge_type,
        extract_owner_repo,
        GitHubForgeClient,
        GitLabForgeClient,
        ForgeAuthenticationError,
    )
    from code_indexer.server.services.git_credential_helper import GitCredentialHelper

    repository_alias = args.get("repository_alias")
    number = args.get("number")

    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    if number is None:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: number"}
        )

    try:
        from code_indexer.utils.git_runner import run_git_command as _run_git_cmd

        leg = _get_legacy()
        _resolve_git_repo_path = leg._resolve_git_repo_path
        _get_pat_credential_for_remote = leg._get_pat_credential_for_remote

        # Resolve repository path
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        # Auto-fetch PAT from stored credentials
        credential, _remote_url_from_cred, cred_error = _get_pat_credential_for_remote(
            repo_path, "origin", user.username
        )
        if cred_error:
            return _mcp_response({"success": False, "error": cred_error})
        assert credential is not None  # narrowed by cred_error check above
        token = credential["token"]

        # Get remote URL to detect forge type
        try:
            url_result = _run_git_cmd(
                ["git", "remote", "get-url", "origin"],
                cwd=Path(repo_path),
            )
            remote_url = url_result.stdout.strip()
        except Exception as e:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-220",
                    f"get_pull_request: failed to get remote URL: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return _mcp_response(
                {"success": False, "error": f"Failed to get remote URL: {e}"}
            )

        # Auto-detect forge type
        forge_type = detect_forge_type(remote_url)
        if forge_type is None:
            return _mcp_response(
                {
                    "success": False,
                    "error": (
                        f"Cannot determine forge type from remote URL '{remote_url}'. "
                        "Only github and gitlab are supported."
                    ),
                }
            )

        # Extract owner and repo from remote URL
        owner, repo = extract_owner_repo(remote_url)
        host = GitCredentialHelper.extract_host_from_remote_url(remote_url) or (
            "github.com" if forge_type == "github" else "gitlab.com"
        )

        # Call appropriate forge client
        if forge_type == "github":
            gh_client = GitHubForgeClient()
            pull_request = gh_client.get_pull_request(
                token=token,
                host=host,
                owner=owner,
                repo=repo,
                number=int(number),
            )
        else:
            gl_client = GitLabForgeClient()
            pull_request = gl_client.get_merge_request(
                token=token,
                host=host,
                owner=owner,
                repo=repo,
                number=int(number),
            )

        logger.info(
            f"get_pull_request: fetched {forge_type} PR/MR #{number} "
            f"for '{repository_alias}'",
            extra={"correlation_id": get_correlation_id()},
        )

        return _mcp_response(
            {
                "success": True,
                "pull_request": pull_request,
                "forge_type": forge_type,
            }
        )

    except ForgeAuthenticationError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-220",
                f"get_pull_request: authentication error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except ValueError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-220",
                f"get_pull_request: validation error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in get_pull_request: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def list_pull_request_comments(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for list_pull_request_comments tool - list comments on a PR or MR.

    Story #448: list_pull_request_comments - Read review comments and threads.

    Auto-detects forge type (github/gitlab) from the repository's remote URL.
    Read-only operation - does not require write mode.
    Returns a list of unified comment dicts on success.
    """
    from code_indexer.server.clients.forge_client import (
        detect_forge_type,
        extract_owner_repo,
        GitHubForgeClient,
        GitLabForgeClient,
        ForgeAuthenticationError,
    )
    from code_indexer.server.services.git_credential_helper import GitCredentialHelper

    repository_alias = args.get("repository_alias")
    number = args.get("number")
    limit = int(args.get("limit", 50))

    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    if number is None:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: number"}
        )

    try:
        from code_indexer.utils.git_runner import run_git_command as _run_git_cmd

        leg = _get_legacy()
        _resolve_git_repo_path = leg._resolve_git_repo_path
        _get_pat_credential_for_remote = leg._get_pat_credential_for_remote

        # Resolve repository path
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        # Auto-fetch PAT from stored credentials
        credential, _remote_url_from_cred, cred_error = _get_pat_credential_for_remote(
            repo_path, "origin", user.username
        )
        if cred_error:
            return _mcp_response({"success": False, "error": cred_error})
        assert credential is not None  # narrowed by cred_error check above
        token = credential["token"]

        # Get remote URL to detect forge type
        try:
            url_result = _run_git_cmd(
                ["git", "remote", "get-url", "origin"],
                cwd=Path(repo_path),
            )
            remote_url = url_result.stdout.strip()
        except Exception as e:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-220",
                    f"list_pull_request_comments: failed to get remote URL: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return _mcp_response(
                {"success": False, "error": f"Failed to get remote URL: {e}"}
            )

        # Auto-detect forge type
        forge_type = detect_forge_type(remote_url)
        if forge_type is None:
            return _mcp_response(
                {
                    "success": False,
                    "error": (
                        f"Cannot determine forge type from remote URL '{remote_url}'. "
                        "Only github and gitlab are supported."
                    ),
                }
            )

        # Extract owner and repo from remote URL
        owner, repo = extract_owner_repo(remote_url)
        host = GitCredentialHelper.extract_host_from_remote_url(remote_url) or (
            "github.com" if forge_type == "github" else "gitlab.com"
        )

        # Call appropriate forge client
        if forge_type == "github":
            gh_client = GitHubForgeClient()
            comments = gh_client.list_pull_request_comments(
                token=token,
                host=host,
                owner=owner,
                repo=repo,
                number=int(number),
                limit=limit,
            )
        else:
            gl_client = GitLabForgeClient()
            comments = gl_client.list_merge_request_notes(
                token=token,
                host=host,
                owner=owner,
                repo=repo,
                number=int(number),
                limit=limit,
            )

        logger.info(
            f"list_pull_request_comments: fetched {len(comments)} comment(s) "
            f"for {forge_type} PR/MR #{number} in '{repository_alias}'",
            extra={"correlation_id": get_correlation_id()},
        )

        return _mcp_response(
            {
                "success": True,
                "comments": comments,
                "forge_type": forge_type,
                "count": len(comments),
            }
        )

    except ForgeAuthenticationError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-220",
                f"list_pull_request_comments: authentication error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except ValueError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-220",
                f"list_pull_request_comments: validation error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in list_pull_request_comments: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def comment_on_pull_request(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for comment_on_pull_request tool - add a comment to a PR or MR.

    Story #449: comment_on_pull_request - Add comments to PR/MR.

    Auto-detects forge type (github/gitlab) from the repository's remote URL.
    Supports both general comments and inline review comments (file_path+line_number).
    Credentials are auto-fetched from stored git credentials.
    """
    from code_indexer.server.clients.forge_client import (
        detect_forge_type,
        extract_owner_repo,
        GitHubForgeClient,
        GitLabForgeClient,
        ForgeAuthenticationError,
    )
    from code_indexer.server.services.git_credential_helper import GitCredentialHelper

    repository_alias = args.get("repository_alias")
    number = args.get("number")
    body = args.get("body")
    file_path = args.get("file_path")
    line_number = args.get("line_number")

    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if number is None:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: number"}
        )
    if not body:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: body"}
        )
    if file_path is not None and line_number is None:
        return _mcp_response(
            {
                "success": False,
                "error": "line_number is required when file_path is provided",
            }
        )

    try:
        from code_indexer.utils.git_runner import run_git_command as _run_git_cmd

        leg = _get_legacy()
        _resolve_git_repo_path = leg._resolve_git_repo_path
        _get_pat_credential_for_remote = leg._get_pat_credential_for_remote

        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        credential, _remote_url_from_cred, cred_error = _get_pat_credential_for_remote(
            repo_path, "origin", user.username
        )
        if cred_error:
            return _mcp_response({"success": False, "error": cred_error})
        assert credential is not None  # narrowed by cred_error check above
        token = credential["token"]

        try:
            url_result = _run_git_cmd(
                ["git", "remote", "get-url", "origin"],
                cwd=Path(repo_path),
            )
            remote_url = url_result.stdout.strip()
        except Exception as e:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-221",
                    f"comment_on_pull_request: failed to get remote URL: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return _mcp_response(
                {"success": False, "error": f"Failed to get remote URL: {e}"}
            )

        forge_type = detect_forge_type(remote_url)
        if forge_type is None:
            return _mcp_response(
                {
                    "success": False,
                    "error": (
                        f"Cannot determine forge type from remote URL '{remote_url}'. "
                        "Only github and gitlab are supported."
                    ),
                }
            )

        owner, repo = extract_owner_repo(remote_url)
        host = GitCredentialHelper.extract_host_from_remote_url(remote_url) or (
            "github.com" if forge_type == "github" else "gitlab.com"
        )

        if forge_type == "github":
            gh_client = GitHubForgeClient()
            result = gh_client.comment_on_pull_request(
                token=token,
                host=host,
                owner=owner,
                repo=repo,
                number=int(number),
                body=body,
                file_path=file_path,
                line_number=int(line_number) if line_number is not None else None,
            )
        else:
            gl_client = GitLabForgeClient()
            result = gl_client.comment_on_merge_request(
                token=token,
                host=host,
                owner=owner,
                repo=repo,
                number=int(number),
                body=body,
                file_path=file_path,
                line_number=int(line_number) if line_number is not None else None,
            )

        logger.info(
            f"comment_on_pull_request: added comment to {forge_type} PR/MR #{number} "
            f"in '{repository_alias}'",
            extra={"correlation_id": get_correlation_id()},
        )

        return _mcp_response(
            {
                "success": True,
                "comment_id": result["comment_id"],
                "url": result["url"],
                "forge_type": forge_type,
            }
        )

    except ForgeAuthenticationError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-221",
                f"comment_on_pull_request: authentication error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except ValueError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-221",
                f"comment_on_pull_request: validation error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in comment_on_pull_request: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def update_pull_request(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for update_pull_request tool - update PR/MR metadata.

    Story #450: update_pull_request - Update PR/MR metadata.

    Auto-detects forge type (github/gitlab) from the repository's remote URL.
    At least one of title, description, labels, assignees, or reviewers must be provided.
    Credentials are auto-fetched from stored git credentials.
    """
    from code_indexer.server.clients.forge_client import (
        detect_forge_type,
        extract_owner_repo,
        GitHubForgeClient,
        GitLabForgeClient,
        ForgeAuthenticationError,
    )
    from code_indexer.server.services.git_credential_helper import GitCredentialHelper

    repository_alias = args.get("repository_alias")
    number = args.get("number")
    title = args.get("title")
    description = args.get("description")
    labels = _parse_json_string_array(args.get("labels"))
    assignees = _parse_json_string_array(args.get("assignees"))
    reviewers = _parse_json_string_array(args.get("reviewers"))

    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if number is None:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: number"}
        )

    # At least one update field required
    has_any_field = any(
        v is not None for v in [title, description, labels, assignees, reviewers]
    )
    if not has_any_field:
        return _mcp_response(
            {
                "success": False,
                "error": (
                    "At least one field must be provided to update: "
                    "title, description, labels, assignees, or reviewers."
                ),
            }
        )

    try:
        from code_indexer.utils.git_runner import run_git_command as _run_git_cmd

        leg = _get_legacy()
        _resolve_git_repo_path = leg._resolve_git_repo_path
        _get_pat_credential_for_remote = leg._get_pat_credential_for_remote

        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        credential, _remote_url_from_cred, cred_error = _get_pat_credential_for_remote(
            repo_path, "origin", user.username
        )
        if cred_error:
            return _mcp_response({"success": False, "error": cred_error})
        assert credential is not None  # narrowed by cred_error check above
        token = credential["token"]

        try:
            url_result = _run_git_cmd(
                ["git", "remote", "get-url", "origin"],
                cwd=Path(repo_path),
            )
            remote_url = url_result.stdout.strip()
        except Exception as e:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-222",
                    f"update_pull_request: failed to get remote URL: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return _mcp_response(
                {"success": False, "error": f"Failed to get remote URL: {e}"}
            )

        forge_type = detect_forge_type(remote_url)
        if forge_type is None:
            return _mcp_response(
                {
                    "success": False,
                    "error": (
                        f"Cannot determine forge type from remote URL '{remote_url}'. "
                        "Only github and gitlab are supported."
                    ),
                }
            )

        owner, repo = extract_owner_repo(remote_url)
        host = GitCredentialHelper.extract_host_from_remote_url(remote_url) or (
            "github.com" if forge_type == "github" else "gitlab.com"
        )

        if forge_type == "github":
            gh_client = GitHubForgeClient()
            result = gh_client.update_pull_request(
                token=token,
                host=host,
                owner=owner,
                repo=repo,
                number=int(number),
                title=title,
                description=description,
                labels=labels if isinstance(labels, list) else None,
                assignees=assignees if isinstance(assignees, list) else None,
                reviewers=reviewers if isinstance(reviewers, list) else None,
            )
        else:
            gl_client = GitLabForgeClient()
            result = gl_client.update_merge_request(
                token=token,
                host=host,
                owner=owner,
                repo=repo,
                number=int(number),
                title=title,
                description=description,
                labels=labels if isinstance(labels, list) else None,
                assignees=assignees if isinstance(assignees, list) else None,
            )

        logger.info(
            f"update_pull_request: updated {forge_type} PR/MR #{number} "
            f"in '{repository_alias}' fields={result.get('updated_fields', [])}",
            extra={"correlation_id": get_correlation_id()},
        )

        return _mcp_response(
            {
                "success": True,
                "url": result["url"],
                "updated_fields": result["updated_fields"],
                "forge_type": forge_type,
            }
        )

    except ForgeAuthenticationError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-222",
                f"update_pull_request: authentication error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except ValueError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-222",
                f"update_pull_request: validation error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in update_pull_request: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def merge_pull_request(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for merge_pull_request tool - merge a GitHub PR or GitLab MR.

    Story #451: merge_pull_request - Merge a GitHub PR or GitLab MR.

    Auto-detects forge type (github/gitlab) from the repository's remote URL.
    Credentials are auto-fetched from stored git credentials.
    """
    from code_indexer.server.clients.forge_client import (
        detect_forge_type,
        extract_owner_repo,
        GitHubForgeClient,
        GitLabForgeClient,
        ForgeAuthenticationError,
    )
    from code_indexer.server.services.git_credential_helper import GitCredentialHelper

    repository_alias = args.get("repository_alias")
    number = args.get("number")
    merge_method = args.get("merge_method", "merge")
    commit_message = args.get("commit_message")
    delete_branch = bool(args.get("delete_branch", False))

    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if number is None:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: number"}
        )

    try:
        from code_indexer.utils.git_runner import run_git_command as _run_git_cmd

        leg = _get_legacy()
        _resolve_git_repo_path = leg._resolve_git_repo_path
        _get_pat_credential_for_remote = leg._get_pat_credential_for_remote

        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        credential, _remote_url_from_cred, cred_error = _get_pat_credential_for_remote(
            repo_path, "origin", user.username
        )
        if cred_error:
            return _mcp_response({"success": False, "error": cred_error})
        assert credential is not None  # narrowed by cred_error check above
        token = credential["token"]

        try:
            url_result = _run_git_cmd(
                ["git", "remote", "get-url", "origin"],
                cwd=Path(repo_path),
            )
            remote_url = url_result.stdout.strip()
        except Exception as e:
            return _mcp_response(
                {"success": False, "error": f"Failed to get remote URL: {e}"}
            )

        forge_type = detect_forge_type(remote_url)
        if forge_type is None:
            return _mcp_response(
                {
                    "success": False,
                    "error": (
                        f"Cannot determine forge type from remote URL '{remote_url}'. "
                        "Only github and gitlab are supported."
                    ),
                }
            )

        owner, repo = extract_owner_repo(remote_url)
        host = GitCredentialHelper.extract_host_from_remote_url(remote_url) or (
            "github.com" if forge_type == "github" else "gitlab.com"
        )

        if forge_type == "github":
            gh_client = GitHubForgeClient()
            result = gh_client.merge_pull_request(
                token=token,
                host=host,
                owner=owner,
                repo=repo,
                number=int(number),
                merge_method=merge_method,
                commit_message=commit_message,
                delete_branch=delete_branch,
            )
        else:
            gl_client = GitLabForgeClient()
            result = gl_client.merge_merge_request(
                token=token,
                host=host,
                owner=owner,
                repo=repo,
                number=int(number),
                merge_method=merge_method,
                delete_branch=delete_branch,
            )

        logger.info(
            f"merge_pull_request: merged {forge_type} PR/MR #{number} "
            f"in '{repository_alias}'",
            extra={"correlation_id": get_correlation_id()},
        )

        return _mcp_response(
            {
                "success": True,
                "merged": result.get("merged", True),
                "sha": result.get("sha", ""),
                "message": result.get("message", ""),
                "forge_type": forge_type,
            }
        )

    except ForgeAuthenticationError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-451",
                f"merge_pull_request: authentication error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except ValueError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-451",
                f"merge_pull_request: validation error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in merge_pull_request: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def close_pull_request(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for close_pull_request tool - close a GitHub PR or GitLab MR.

    Story #452: close_pull_request - Close a GitHub PR or GitLab MR.

    Auto-detects forge type (github/gitlab) from the repository's remote URL.
    Credentials are auto-fetched from stored git credentials.
    """
    from code_indexer.server.clients.forge_client import (
        detect_forge_type,
        extract_owner_repo,
        GitHubForgeClient,
        GitLabForgeClient,
        ForgeAuthenticationError,
    )
    from code_indexer.server.services.git_credential_helper import GitCredentialHelper

    repository_alias = args.get("repository_alias")
    number = args.get("number")

    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if number is None:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: number"}
        )

    try:
        from code_indexer.utils.git_runner import run_git_command as _run_git_cmd

        leg = _get_legacy()
        _resolve_git_repo_path = leg._resolve_git_repo_path
        _get_pat_credential_for_remote = leg._get_pat_credential_for_remote

        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        credential, _remote_url_from_cred, cred_error = _get_pat_credential_for_remote(
            repo_path, "origin", user.username
        )
        if cred_error:
            return _mcp_response({"success": False, "error": cred_error})
        assert credential is not None  # narrowed by cred_error check above
        token = credential["token"]

        try:
            url_result = _run_git_cmd(
                ["git", "remote", "get-url", "origin"],
                cwd=Path(repo_path),
            )
            remote_url = url_result.stdout.strip()
        except Exception as e:
            return _mcp_response(
                {"success": False, "error": f"Failed to get remote URL: {e}"}
            )

        forge_type = detect_forge_type(remote_url)
        if forge_type is None:
            return _mcp_response(
                {
                    "success": False,
                    "error": (
                        f"Cannot determine forge type from remote URL '{remote_url}'. "
                        "Only github and gitlab are supported."
                    ),
                }
            )

        owner, repo = extract_owner_repo(remote_url)
        host = GitCredentialHelper.extract_host_from_remote_url(remote_url) or (
            "github.com" if forge_type == "github" else "gitlab.com"
        )

        if forge_type == "github":
            gh_client = GitHubForgeClient()
            result = gh_client.close_pull_request(
                token=token,
                host=host,
                owner=owner,
                repo=repo,
                number=int(number),
            )
        else:
            gl_client = GitLabForgeClient()
            result = gl_client.close_merge_request(
                token=token,
                host=host,
                owner=owner,
                repo=repo,
                number=int(number),
            )

        logger.info(
            f"close_pull_request: closed {forge_type} PR/MR #{number} "
            f"in '{repository_alias}'",
            extra={"correlation_id": get_correlation_id()},
        )

        return _mcp_response(
            {
                "success": True,
                "message": result.get("message", f"PR/MR #{number} closed"),
                "forge_type": forge_type,
            }
        )

    except ForgeAuthenticationError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-452",
                f"close_pull_request: authentication error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except ValueError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-452",
                f"close_pull_request: validation error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in close_pull_request: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _register(registry: dict) -> None:
    """Register all pull request handlers into the given HANDLER_REGISTRY."""
    registry["create_pull_request"] = create_pull_request
    registry["list_pull_requests"] = list_pull_requests
    registry["get_pull_request"] = get_pull_request
    registry["list_pull_request_comments"] = list_pull_request_comments
    registry["comment_on_pull_request"] = comment_on_pull_request
    registry["update_pull_request"] = update_pull_request
    registry["merge_pull_request"] = merge_pull_request
    registry["close_pull_request"] = close_pull_request
