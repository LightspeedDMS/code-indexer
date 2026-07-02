"""Git write handler functions for CIDX MCP server (Story #496).

Covers: git_stage, git_unstage, git_commit, git_push, git_pull,
git_reset, git_clean, git_merge, git_mark_resolved, git_merge_abort,
git_checkout_file, git_branch_create, git_branch_switch, git_branch_delete,
git_stash, git_amend, configure_git_credential, list_git_credentials,
delete_git_credential.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from code_indexer.server.auth.user_manager import User
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.services.git_operations_service import (
    GitCommandError,
    git_operations_service,
)

from ._utils import _mcp_response, app_module

logger = logging.getLogger(__name__)

# Error codes for git write operations (named constants for traceability)
_ERR_STAGE = "MCP-GENERAL-064"
_ERR_UNSTAGE = "MCP-GENERAL-065"
_ERR_COMMIT = "MCP-GENERAL-066"
_ERR_PUSH = "MCP-GENERAL-067"
_ERR_PULL = "MCP-GENERAL-068"
_ERR_RESET = "MCP-GENERAL-070"
_ERR_CLEAN = "MCP-GENERAL-071"
_ERR_MERGE_ABORT = "MCP-GENERAL-072"
_ERR_CHECKOUT_FILE = "MCP-GENERAL-073"
_ERR_BRANCH_CREATE = "MCP-GENERAL-075"
_ERR_BRANCH_SWITCH = "MCP-GENERAL-076"
_ERR_BRANCH_DELETE = "MCP-GENERAL-077"
_ERR_MERGE = "MCP-GENERAL-216"
_ERR_MARK_RESOLVED = "MCP-GENERAL-218"
_ERR_STASH = "MCP-GENERAL-453"
_ERR_AMEND = "MCP-GENERAL-454"


def _handle_git_file_operation(
    args: Dict[str, Any],
    user: User,
    service_fn: Callable[[Path, Any], Dict[str, Any]],
    error_code: str,
    operation_name: str,
) -> Dict[str, Any]:
    """Execute a git file operation (stage/unstage) with standard validation and error handling."""
    import code_indexer.server.mcp.handlers._legacy as _legacy

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    file_paths = args.get("file_paths")
    if not isinstance(file_paths, list) or len(file_paths) == 0:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: file_paths"}
        )

    try:
        repo_path, error_msg = _legacy._resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        if repo_path is None:
            return _mcp_response(
                {"success": False, "error": "Failed to resolve repository path"}
            )
        return _mcp_response(service_fn(Path(repo_path), file_paths))
    except GitCommandError as e:
        logger.error(
            format_error_log(
                error_code,
                f"{operation_name} failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        result: Dict[str, Any] = {
            "success": False,
            "error_type": "GitCommandError",
            "error": str(e),
            "stderr": e.stderr,
        }
        if hasattr(e, "command"):
            result["command"] = e.command
        return _mcp_response(result)
    except FileNotFoundError as e:
        logger.warning(
            f"{operation_name} file not found: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in {operation_name}: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def _get_credential_manager():
    """Create and return a GitCredentialManager backed by the server's database.

    Centralises the config-service lookup and db-path construction used by
    credential handlers and PAT resolution.
    """
    from ...services.config_service import get_config_service
    from ...services.git_credential_manager import GitCredentialManager

    config_service = get_config_service()
    db_path = str(config_service.config_manager.server_dir / "data" / "cidx_server.db")
    return GitCredentialManager(db_path)


def _get_pat_credential_for_remote(
    repo_path: str, remote: str, username: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    """Resolve PAT credential for a git remote's forge host.

    Story #387: Extracts remote URL, identifies forge host, and fetches
    the stored PAT credential for the given username.

    Returns:
        (credential, remote_url, error_msg) tuple.
    """
    from code_indexer.server.services.git_credential_helper import GitCredentialHelper
    from code_indexer.utils.git_runner import run_git_command as _run_git_cmd

    remote_url = ""
    try:
        url_result = _run_git_cmd(
            ["git", "remote", "get-url", remote],
            cwd=Path(repo_path),
            check=True,
        )
        remote_url = url_result.stdout.strip()
    except Exception as e:
        logger.warning(f"Failed to get remote URL for '{remote}': {e}")
        return None, None, f"Failed to get remote URL for '{remote}': {e}"

    forge_host = (
        GitCredentialHelper.extract_host_from_remote_url(remote_url)
        if remote_url
        else None
    )
    if not forge_host:
        return (
            None,
            None,
            (
                f"Unable to determine forge host from remote '{remote}'. "
                "Ensure the repository has a valid remote URL configured."
            ),
        )

    manager = _get_credential_manager()
    credential = manager.get_credential_for_host(username, forge_host)

    if credential is None:
        return (
            None,
            None,
            (
                f"No git credential configured for {forge_host}. "
                "Use configure_git_credential to set up your PAT."
            ),
        )

    return credential, remote_url, None


def _resolve_commit_identity(
    args: Dict[str, Any], user: User, repo_path: str
) -> Tuple[str, str, Optional[str], Optional[str]]:
    """Resolve author/committer identity for git_commit.

    Story #402: Look up PAT credential to set committer identity.
    Credential lookup failure must NEVER block the commit -- errors are
    logged as non-blocking warnings and the fallback identity is used.
    Note: The @cidx.local fallback domain is preserved from _legacy.py line 4499.

    Returns:
        (user_email, user_name, committer_email, committer_name)
    """
    user_email = getattr(user, "email", None) or f"{user.username}@cidx.local"
    user_name = args.get("author_name") or user.username
    committer_email: Optional[str] = None
    committer_name: Optional[str] = None

    try:
        credential, _remote_url, cred_error = _get_pat_credential_for_remote(
            repo_path, "origin", user.username
        )
        if cred_error is not None:
            logger.warning(
                f"PAT credential lookup returned error (non-blocking, using fallback identity): "
                f"{cred_error}",
                extra={"correlation_id": get_correlation_id()},
            )
        elif credential and credential.get("git_user_email"):
            cred_email = credential["git_user_email"]
            cred_name = credential.get("git_user_name") or None
            user_email = cred_email
            user_name = cred_name or user_name
            committer_email = cred_email
            committer_name = cred_name
    except Exception as e:
        logger.warning(
            f"Credential lookup for git_commit committer identity failed (non-blocking): {e}",
            extra={"correlation_id": get_correlation_id()},
        )

    return user_email, user_name, committer_email, committer_name


def git_commit(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_commit tool - create a git commit."""
    import code_indexer.server.mcp.handlers._legacy as _legacy

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    message = args.get("message")
    if not message:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: message"}
        )
    try:
        repo_path, error_msg = _legacy._resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        if repo_path is None:
            return _mcp_response(
                {"success": False, "error": "Failed to resolve repository path"}
            )

        user_email, user_name, committer_email, committer_name = (
            _resolve_commit_identity(args, user, repo_path)
        )
        result = git_operations_service.git_commit(
            Path(repo_path),
            message,
            user_email,
            user_name,
            committer_email=committer_email,
            committer_name=committer_name,
        )
        return _mcp_response(result)
    except GitCommandError as e:
        logger.error(
            format_error_log(
                _ERR_COMMIT,
                f"git_commit failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        result_err: Dict[str, Any] = {
            "success": False,
            "error_type": "GitCommandError",
            "error": str(e),
            "stderr": e.stderr,
        }
        if hasattr(e, "command"):
            result_err["command"] = e.command
        return _mcp_response(result_err)
    except (ValueError, FileNotFoundError) as e:
        logger.warning(
            f"git_commit validation/path error: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_commit: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def _handle_write_error(
    operation: str, error_code: str, exc: Exception
) -> Dict[str, Any]:
    """Shared error handler for git write operations.

    Handles GitCommandError, FileNotFoundError, and generic Exception with
    appropriate logging and MCP response formatting.
    """
    if isinstance(exc, GitCommandError):
        logger.error(
            format_error_log(
                error_code,
                f"{operation} failed: {exc}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        result: Dict[str, Any] = {
            "success": False,
            "error_type": "GitCommandError",
            "error": str(exc),
            "stderr": exc.stderr,
        }
        if hasattr(exc, "command"):
            result["command"] = exc.command
        return _mcp_response(result)
    if isinstance(exc, FileNotFoundError):
        logger.warning(
            f"{operation} file not found: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(exc)})
    logger.exception(
        f"Unexpected error in {operation}: {exc}",
        extra={"correlation_id": get_correlation_id()},
    )
    return _mcp_response({"success": False, "error": str(exc)})


def _confirmation_response(
    result: Dict[str, Any], label: str
) -> Optional[Dict[str, Any]]:
    """If result requires confirmation, return formatted token response; else None."""
    if result.get("requires_confirmation"):
        return _mcp_response(
            {
                "success": False,
                "confirmation_token_required": {
                    "token": result["token"],
                    "message": (
                        f"{label} requires confirmation. "
                        f"Call again with confirmation_token='{result['token']}'"
                    ),
                },
            }
        )
    return None


def _new_confirmation_token(action: str, exc: ValueError) -> Dict[str, Any]:
    """Generate a fresh confirmation token response after validation failure."""
    token = git_operations_service.generate_confirmation_token(action)
    return _mcp_response(
        {
            "success": False,
            "confirmation_token_required": {"token": token, "message": str(exc)},
        }
    )


def _invalidate_wiki_cache(repository_alias: str, operation: str) -> None:
    """Invalidate wiki cache after a git write operation (best-effort)."""
    try:
        from code_indexer.server.wiki.wiki_cache_invalidator import (
            wiki_cache_invalidator,
        )

        wiki_cache_invalidator.invalidate_repo(repository_alias)
    except Exception as e:
        logger.debug(f"Wiki cache invalidation skipped for {operation}: {e}")


_VALID_RESET_MODES = frozenset({"mixed", "soft", "hard"})


def _check_writable(
    repository_alias: str,
    repo_path: str,
    operation_label: str,
) -> Optional[Dict[str, Any]]:
    """Return error response if the repo is not writable, else None."""
    import code_indexer.server.mcp.handlers._legacy as _legacy

    golden_repos_dir = getattr(app_module.app.state, "golden_repos_dir", None)
    if not _legacy._is_writable_repo(repository_alias, repo_path, golden_repos_dir):
        return _mcp_response(
            {
                "success": False,
                "error": (
                    f"Write mode required for {operation_label}. "
                    "Use enter_write_mode first, or activate the repository "
                    "to create a writable workspace."
                ),
            }
        )
    return None


def git_merge(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_merge tool - merge a branch into current branch.

    Story #388: Git Merge with Conflict Detection.
    """
    import code_indexer.server.mcp.handlers._legacy as _legacy

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    source_branch = args.get("source_branch")
    if not source_branch:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: source_branch"}
        )
    try:
        repo_path, error_msg = _legacy._resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        if repo_path is None:
            return _mcp_response(
                {"success": False, "error": "Failed to resolve repository path"}
            )

        write_err = _check_writable(repository_alias, repo_path, "git merge")
        if write_err is not None:
            return write_err

        result = git_operations_service.merge_branch(Path(repo_path), source_branch)
        _invalidate_wiki_cache(repository_alias, "git_merge")
        return _mcp_response(result)
    except GitCommandError as e:
        return _handle_write_error("git_merge", _ERR_MERGE, e)
    except FileNotFoundError as e:
        return _handle_write_error("git_merge", _ERR_MERGE, e)
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_merge: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def list_git_credentials(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for list_git_credentials - list user's configured git forge credentials."""
    try:
        manager = _get_credential_manager()
        credentials = manager.list_credentials(user.username)
        return _mcp_response(
            {"success": True, "credentials": credentials, "count": len(credentials)}
        )
    except Exception as e:
        logger.exception(
            f"list_git_credentials failed: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def delete_git_credential(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for delete_git_credential - remove a git forge credential."""
    credential_id = args.get("credential_id")
    if not credential_id:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: credential_id"}
        )
    try:
        manager = _get_credential_manager()
        manager.delete_credential(user.username, credential_id)
        return _mcp_response(
            {"success": True, "message": f"Credential {credential_id} deleted"}
        )
    except PermissionError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"delete_git_credential failed: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def configure_git_credential(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for configure_git_credential - store a git forge PAT with identity discovery."""
    forge_type = args.get("forge_type")
    forge_host = args.get("forge_host")
    token = args.get("token")
    name = args.get("name")

    if not forge_type or not forge_host or not token:
        return _mcp_response(
            {
                "success": False,
                "error": "Missing required parameters: forge_type, forge_host, token",
            }
        )
    if forge_type not in ("github", "gitlab"):
        return _mcp_response(
            {"success": False, "error": "forge_type must be 'github' or 'gitlab'"}
        )
    try:
        manager = _get_credential_manager()
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                manager.configure_credential(
                    username=user.username,
                    forge_type=forge_type,
                    forge_host=forge_host,
                    token=token,
                    name=name,
                )
            )
        finally:
            loop.close()
        return _mcp_response(result)
    except Exception as e:
        logger.exception(
            f"configure_git_credential failed: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_branch_delete(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_branch_delete tool - delete branch."""
    import code_indexer.server.mcp.handlers._legacy as _legacy

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    branch_name = args.get("branch_name")
    if not branch_name:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: branch_name"}
        )
    confirmation_token = args.get("confirmation_token")
    try:
        repo_path, error_msg = _legacy._resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        if repo_path is None:
            return _mcp_response(
                {"success": False, "error": "Failed to resolve repository path"}
            )

        result = git_operations_service.git_branch_delete(
            Path(repo_path), branch_name, confirmation_token=confirmation_token
        )
        confirm = _confirmation_response(result, "Branch deletion")
        if confirm is not None:
            return confirm
        return _mcp_response(result)
    except ValueError as e:
        return _new_confirmation_token("git_branch_delete", e)
    except GitCommandError as e:
        return _handle_write_error("git_branch_delete", _ERR_BRANCH_DELETE, e)
    except FileNotFoundError as e:
        return _handle_write_error("git_branch_delete", _ERR_BRANCH_DELETE, e)
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_branch_delete: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_branch_switch(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_branch_switch tool - switch to different branch."""
    import code_indexer.server.mcp.handlers._legacy as _legacy

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    # Bug #469: Block git_branch_switch on golden repo base clones.
    if repository_alias.endswith("-global"):
        return _mcp_response(
            {
                "success": False,
                "error": (
                    "Cannot use git_branch_switch on golden repositories. "
                    "Use change_golden_repo_branch instead."
                ),
            }
        )
    branch_name = args.get("branch_name")
    if not branch_name:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: branch_name"}
        )
    try:
        repo_path, error_msg = _legacy._resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        if repo_path is None:
            return _mcp_response(
                {"success": False, "error": "Failed to resolve repository path"}
            )

        result = git_operations_service.git_branch_switch(Path(repo_path), branch_name)
        # Map current_branch to branch_name for consistent API (original _legacy.py L5533)
        if "current_branch" in result and "branch_name" not in result:
            result["branch_name"] = result["current_branch"]
        _invalidate_wiki_cache(repository_alias, "git_branch_switch")
        return _mcp_response(result)
    except GitCommandError as e:
        return _handle_write_error("git_branch_switch", _ERR_BRANCH_SWITCH, e)
    except FileNotFoundError as e:
        return _handle_write_error("git_branch_switch", _ERR_BRANCH_SWITCH, e)
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_branch_switch: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_branch_create(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_branch_create tool - create new branch."""
    import code_indexer.server.mcp.handlers._legacy as _legacy

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    branch_name = args.get("branch_name")
    if not branch_name:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: branch_name"}
        )
    try:
        repo_path, error_msg = _legacy._resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        if repo_path is None:
            return _mcp_response(
                {"success": False, "error": "Failed to resolve repository path"}
            )

        result = git_operations_service.git_branch_create(Path(repo_path), branch_name)
        # Map created_branch to branch_name for consistent API (original _legacy.py L5467)
        if "created_branch" in result and "branch_name" not in result:
            result["branch_name"] = result["created_branch"]
        return _mcp_response(result)
    except GitCommandError as e:
        return _handle_write_error("git_branch_create", _ERR_BRANCH_CREATE, e)
    except FileNotFoundError as e:
        return _handle_write_error("git_branch_create", _ERR_BRANCH_CREATE, e)
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_branch_create: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_checkout_file(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_checkout_file tool - restore file from HEAD."""
    import code_indexer.server.mcp.handlers._legacy as _legacy

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    file_path = args.get("file_path")
    if not file_path:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: file_path"}
        )
    try:
        repo_path, error_msg = _legacy._resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        if repo_path is None:
            return _mcp_response(
                {"success": False, "error": "Failed to resolve repository path"}
            )

        result = git_operations_service.git_checkout_file(Path(repo_path), file_path)
        _invalidate_wiki_cache(repository_alias, "git_checkout_file")
        return _mcp_response(result)
    except GitCommandError as e:
        return _handle_write_error("git_checkout_file", _ERR_CHECKOUT_FILE, e)
    except FileNotFoundError as e:
        return _handle_write_error("git_checkout_file", _ERR_CHECKOUT_FILE, e)
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_checkout_file: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_merge_abort(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_merge_abort tool - abort in-progress merge."""
    import code_indexer.server.mcp.handlers._legacy as _legacy

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    try:
        repo_path, error_msg = _legacy._resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        if repo_path is None:
            return _mcp_response(
                {"success": False, "error": "Failed to resolve repository path"}
            )

        result = git_operations_service.git_merge_abort(Path(repo_path))
        _invalidate_wiki_cache(repository_alias, "git_merge_abort")
        return _mcp_response(result)
    except GitCommandError as e:
        return _handle_write_error("git_merge_abort", _ERR_MERGE_ABORT, e)
    except FileNotFoundError as e:
        return _handle_write_error("git_merge_abort", _ERR_MERGE_ABORT, e)
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_merge_abort: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_mark_resolved(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_mark_resolved tool - mark a conflicted file as resolved."""
    import code_indexer.server.mcp.handlers._legacy as _legacy

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    file_path = args.get("file_path")
    if not file_path:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: file_path"}
        )
    try:
        repo_path, error_msg = _legacy._resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        if repo_path is None:
            return _mcp_response(
                {"success": False, "error": "Failed to resolve repository path"}
            )

        write_err = _check_writable(repository_alias, repo_path, "git mark_resolved")
        if write_err is not None:
            return write_err

        result = git_operations_service.git_mark_resolved(Path(repo_path), file_path)
        _invalidate_wiki_cache(repository_alias, "git_mark_resolved")
        return _mcp_response(result)
    except GitCommandError as e:
        return _handle_write_error("git_mark_resolved", _ERR_MARK_RESOLVED, e)
    except (ValueError, FileNotFoundError) as e:
        logger.warning(
            f"git_mark_resolved validation/path error: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_mark_resolved: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_reset(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_reset tool - reset working tree."""
    import code_indexer.server.mcp.handlers._legacy as _legacy

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    mode = args.get("mode", "mixed")
    if mode not in _VALID_RESET_MODES:
        return _mcp_response(
            {
                "success": False,
                "error": f"Invalid mode '{mode}'. Valid modes: {', '.join(sorted(_VALID_RESET_MODES))}",
            }
        )
    target = args.get("commit_hash")
    confirmation_token = args.get("confirmation_token")

    try:
        repo_path, error_msg = _legacy._resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        if repo_path is None:
            return _mcp_response(
                {"success": False, "error": "Failed to resolve repository path"}
            )

        result = git_operations_service.git_reset(
            Path(repo_path),
            mode=mode,
            commit_hash=target,
            confirmation_token=confirmation_token,
        )
        confirm = _confirmation_response(result, "Hard reset")
        if confirm is not None:
            return confirm
        _invalidate_wiki_cache(repository_alias, "git_reset")
        return _mcp_response(result)
    except ValueError as e:
        return _new_confirmation_token("git_reset_hard", e)
    except Exception as e:
        return _handle_write_error("git_reset", _ERR_RESET, e)


def git_clean(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_clean tool - remove untracked files."""
    import code_indexer.server.mcp.handlers._legacy as _legacy

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    confirmation_token = args.get("confirmation_token")

    try:
        repo_path, error_msg = _legacy._resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        if repo_path is None:
            return _mcp_response(
                {"success": False, "error": "Failed to resolve repository path"}
            )

        result = git_operations_service.git_clean(
            Path(repo_path), confirmation_token=confirmation_token
        )
        confirm = _confirmation_response(result, "Git clean")
        if confirm is not None:
            return confirm
        _invalidate_wiki_cache(repository_alias, "git_clean")
        return _mcp_response(result)
    except ValueError as e:
        return _new_confirmation_token("git_clean", e)
    except Exception as e:
        return _handle_write_error("git_clean", _ERR_CLEAN, e)


def git_push(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_push tool - push commits to remote using PAT authentication.

    Story #387: PAT-Authenticated Git Push with User Attribution.
    """
    import code_indexer.server.mcp.handlers._legacy as _legacy

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    try:
        remote = args.get("remote", "origin")
        branch = args.get("branch")
        set_upstream = args.get("set_upstream", True)

        repo_path, error_msg = _legacy._resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        if repo_path is None:
            return _mcp_response(
                {"success": False, "error": "Failed to resolve repository path"}
            )

        # Trigger migration before push if needed (Bug #639)
        git_operations_service._trigger_migration_if_needed(
            repo_path, user.username, repository_alias
        )

        credential, remote_url, cred_error = _get_pat_credential_for_remote(
            repo_path, remote, user.username
        )
        if cred_error:
            return _mcp_response({"success": False, "error": cred_error})

        result = git_operations_service.git_push_with_pat(
            Path(repo_path),
            remote,
            branch,
            credential,
            remote_url=remote_url,
            set_upstream=set_upstream,
        )
        return _mcp_response(result)
    except Exception as e:
        return _handle_write_error("git_push", _ERR_PUSH, e)


def git_pull(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_pull tool - pull updates from remote."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    try:
        remote = args.get("remote", "origin")
        branch = args.get("branch")
        result = git_operations_service.pull_from_remote(
            repo_alias=repository_alias,
            username=user.username,
            remote=remote,
            branch=branch,
        )
        _invalidate_wiki_cache(repository_alias, "git_pull")
        return _mcp_response(result)
    except Exception as e:
        return _handle_write_error("git_pull", _ERR_PULL, e)


_VALID_STASH_ACTIONS = frozenset({"push", "pop", "apply", "list", "drop"})


def _validate_stash_args(
    args: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Validate and parse git_stash arguments.

    Returns:
        (parsed_args, error_response) -- if error_response is not None, return it.
        parsed_args has keys: repository_alias, action, message, index.
    """
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return None, _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    action = args.get("action")
    if not action:
        return None, _mcp_response(
            {"success": False, "error": "Missing required parameter: action"}
        )
    if action not in _VALID_STASH_ACTIONS:
        return None, _mcp_response(
            {
                "success": False,
                "error": (
                    f"Invalid action '{action}'. "
                    f"Valid actions: {', '.join(sorted(_VALID_STASH_ACTIONS))}"
                ),
            }
        )
    raw_index = args.get("index", 0)
    try:
        index = int(raw_index)
    except (TypeError, ValueError):
        return None, _mcp_response(
            {"success": False, "error": f"Invalid index value: {raw_index!r}"}
        )
    if index < 0:
        return None, _mcp_response(
            {"success": False, "error": f"Index must be >= 0, got {index}"}
        )
    return {
        "repository_alias": repository_alias,
        "action": action,
        "message": args.get("message"),
        "index": index,
    }, None


def git_amend(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_amend tool - amend the most recent git commit.

    Originally Story #454, extracted as part of Story #496 modularisation.
    Uses PAT credential identity for GIT_AUTHOR/COMMITTER env vars.
    """
    import code_indexer.server.mcp.handlers._legacy as _legacy
    import os as _os

    repository_alias = args.get("repository_alias")
    message = args.get("message")  # Optional - if None uses --no-edit

    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    try:
        repo_path, error_msg = _legacy._resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        if repo_path is None:
            return _mcp_response(
                {"success": False, "error": "Failed to resolve repository path"}
            )

        credential, _remote_url, cred_error = _get_pat_credential_for_remote(
            repo_path, "origin", user.username
        )
        if cred_error:
            return _mcp_response({"success": False, "error": cred_error})

        env = _os.environ.copy()
        if credential and credential.get("git_user_email"):
            git_name = credential.get("git_user_name") or user.username
            git_email = credential["git_user_email"]
            env["GIT_AUTHOR_NAME"] = git_name
            env["GIT_AUTHOR_EMAIL"] = git_email
            env["GIT_COMMITTER_NAME"] = git_name
            env["GIT_COMMITTER_EMAIL"] = git_email

        result = git_operations_service.git_amend(
            Path(repo_path), message=message, env=env
        )
        return _mcp_response(result)
    except GitCommandError as e:
        return _handle_write_error("git_amend", _ERR_AMEND, e)
    except (ValueError, FileNotFoundError) as e:
        logger.warning(
            f"git_amend validation/path error: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_amend: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_stash(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_stash tool - stash and restore uncommitted changes.

    Originally Story #453, extracted as part of Story #496 modularisation.
    """
    import code_indexer.server.mcp.handlers._legacy as _legacy

    parsed, err = _validate_stash_args(args)
    if err is not None:
        return err
    assert parsed is not None  # narrowed by err check above

    action = parsed["action"]
    try:
        repo_path, error_msg = _legacy._resolve_git_repo_path(
            parsed["repository_alias"], user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        if repo_path is None:
            return _mcp_response(
                {"success": False, "error": "Failed to resolve repository path"}
            )

        p = Path(repo_path)
        dispatch = {
            "push": lambda: git_operations_service.git_stash_push(
                p, message=parsed["message"]
            ),
            "pop": lambda: git_operations_service.git_stash_pop(
                p, index=parsed["index"]
            ),
            "apply": lambda: git_operations_service.git_stash_apply(
                p, index=parsed["index"]
            ),
            "list": lambda: git_operations_service.git_stash_list(p),
            "drop": lambda: git_operations_service.git_stash_drop(
                p, index=parsed["index"]
            ),
        }
        return _mcp_response(dispatch[action]())
    except GitCommandError as e:
        logger.error(
            format_error_log(
                _ERR_STASH,
                f"git_stash ({action}) failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
            }
        )
    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_stash: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_stage(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_stage tool - stage files for commit."""
    return _handle_git_file_operation(
        args,
        user,
        git_operations_service.git_stage,
        _ERR_STAGE,
        "git_stage",
    )


def git_unstage(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_unstage tool - unstage files."""
    return _handle_git_file_operation(
        args,
        user,
        git_operations_service.git_unstage,
        _ERR_UNSTAGE,
        "git_unstage",
    )


def _register(registry: dict) -> None:
    """Register all git write handlers into the provided HANDLER_REGISTRY."""
    registry["git_stage"] = git_stage
    registry["git_unstage"] = git_unstage
    registry["git_commit"] = git_commit
    registry["git_push"] = git_push
    registry["git_pull"] = git_pull
    registry["git_reset"] = git_reset
    registry["git_clean"] = git_clean
    registry["git_merge"] = git_merge
    registry["git_merge_abort"] = git_merge_abort
    registry["git_mark_resolved"] = git_mark_resolved
    registry["git_checkout_file"] = git_checkout_file
    registry["git_branch_create"] = git_branch_create
    registry["git_branch_switch"] = git_branch_switch
    registry["git_branch_delete"] = git_branch_delete
    registry["configure_git_credential"] = configure_git_credential
    registry["list_git_credentials"] = list_git_credentials
    registry["delete_git_credential"] = delete_git_credential
    registry["git_stash"] = git_stash
    registry["git_amend"] = git_amend
