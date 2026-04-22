"""MCP Tool Handler Functions - Complete implementation for all 22 tools.

All handlers return MCP-compliant responses with content arrays:
{
    "content": [
        {
            "type": "text",
            "text": "<JSON-stringified response data>"
        }
    ]
}
"""

import logging
import sys
import types
from typing import Dict, Any, Optional, Tuple, TYPE_CHECKING
from pathlib import Path

if TYPE_CHECKING:
    pass
from code_indexer.server.services.git_operations_service import (  # noqa: F401
    git_operations_service,
    GitCommandError,
)
from code_indexer.global_repos.git_operations import GitOperationsService  # noqa: F401
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.services.config_service import get_config_service  # noqa: F401
from code_indexer.global_repos.alias_manager import AliasManager

# Shared utilities extracted to _utils.py (Story #496 refactoring)
from . import _utils  # noqa: F401 — tests patch _legacy._utils
from ._utils import (
    _get_scip_audit_repository,  # noqa: F401 — re-exported via handlers namespace
    _get_golden_repos_dir,
    _get_global_repo,
    _get_access_filtering_service,
    _get_scip_query_service,  # noqa: F401 — re-exported via handlers namespace
    _apply_scip_payload_truncation,  # noqa: F401 — re-exported via handlers namespace
    _validate_symbol_format,  # noqa: F401 — re-exported via handlers namespace
    # Re-imported for test patch compatibility (tests patch _legacy.X)
    _expand_wildcard_patterns,  # noqa: F401
    _get_query_tracker,  # noqa: F401
    _get_wiki_enabled_repos,  # noqa: F401
    _list_global_repos,  # noqa: F401
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Story #653 AC3: Constants used by reranking helpers (also used in git_read.py)
# ---------------------------------------------------------------------------
# Search handlers extracted to search.py (Story #496)
from .search import _register as _search_register  # noqa: E402
from .search import (  # noqa: F401, E402
    _omni_search_code,
    _omni_regex_search,
    search_code,
    handle_regex_search,
    handle_get_cached_content,
    _DEFAULT_OVERFETCH_MULTIPLIER,
)

_MAX_RERANK_FETCH_LIMIT = 200

# Extracted to files.py (Story #496)
from .files import _omni_list_files  # noqa: F401, E402
from .files import list_files  # noqa: F401, E402


# Extracted to files.py (Story #496)
from .files import get_file_content  # noqa: F401, E402
from .files import browse_directory  # noqa: F401, E402


# =============================================================================
# File CRUD Handlers (Story #628)
# =============================================================================


# Extracted to files.py (Story #496)
from .files import handle_create_file  # noqa: F401, E402
from .files import handle_edit_file  # noqa: F401, E402
from .files import handle_delete_file  # noqa: F401, E402
from .files import _write_mode_strip_global  # noqa: F401, E402
from .files import _is_write_mode_active  # noqa: F401, E402
from .files import _is_writable_repo  # noqa: F401, E402
from .files import _write_mode_acquire_lock  # noqa: F401, E402
from .files import _write_mode_create_marker  # noqa: F401, E402
from .files import handle_enter_write_mode  # noqa: F401, E402
from .files import _write_mode_run_refresh  # noqa: F401, E402
from .files import handle_exit_write_mode  # noqa: F401, E402


# Handler registry mapping tool names to handler functions
# Type: Dict[str, Any] because handlers have varying signatures (2-param vs 3-param)
HANDLER_REGISTRY: Dict[str, Any] = {}

# Repo handlers extracted to repos.py (Story #496)
from .repos import _register as _repos_register  # noqa: F401, E402

_repos_register(HANDLER_REGISTRY)

from .repos import (  # noqa: F401, E402
    discover_repositories,
    list_repositories,
    activate_repository,
    deactivate_repository,
    list_repo_categories,
    get_repository_status,
    sync_repository,
    switch_branch,
    get_branches,
    check_health,
    check_hnsw_health,
    add_golden_repo,
    remove_golden_repo,
    refresh_golden_repo,
    change_golden_repo_branch,
    get_repository_statistics,
    get_all_repositories_status,
    manage_composite_repository,
    handle_list_global_repos,
    handle_global_repo_status,
    handle_add_golden_repo_index,
    handle_get_golden_repo_indexes,
    manage_provider_indexes,
    bulk_add_provider_index,
    get_provider_health,
    _resolve_golden_repo_path,
    _resolve_golden_repo_base_clone,
    _append_provider_to_config,
    _remove_provider_from_config,
    _provider_index_job,
    _provider_temporal_index_job,
    _post_provider_index_snapshot,
)


def _is_git_repo(path: Path) -> bool:
    """Check if path is a valid git repository."""
    return path.exists() and (path / ".git").exists()


def _find_latest_versioned_repo(base_path: Path, repo_name: str) -> Optional[str]:
    """Find most recent versioned git repo in .versioned/{name}/v_*/ structure."""
    versioned_base = base_path / ".versioned" / repo_name
    if not versioned_base.exists():
        return None

    version_dirs = sorted(
        [d for d in versioned_base.iterdir() if d.is_dir() and d.name.startswith("v_")],
        key=lambda d: d.name,
        reverse=True,
    )

    for version_dir in version_dirs:
        if _is_git_repo(version_dir):
            return str(version_dir)

    return None


def _resolve_repo_path(repo_identifier: str, golden_repos_dir: str) -> Optional[str]:
    """Resolve repository identifier to filesystem path.

    Resolution priority:
    0. Alias JSON target_path (authoritative for read operations)
    1. Full path (if not -global and is a git repo)
    2. index_path from registry (if it has .git)
    3. golden-repos/{name} directory
    4. golden-repos/repos/{name} directory
    5. Versioned repos in .versioned/{name}/v_*/
    6. index_path fallback (directory exists)

    Args:
        repo_identifier: Repository alias or path
        golden_repos_dir: Path to golden repos directory

    Returns:
        Filesystem path to repository, or None if not found
    """
    # Step 0: Try alias JSON target_path (authoritative for read operations)
    # AliasManager.read_alias() returns the versioned snapshot path
    aliases_path = Path(golden_repos_dir) / "aliases"
    if aliases_path.is_dir():
        alias_manager = AliasManager(str(aliases_path))
        # Try the identifier directly (e.g. "cidx-meta-global")
        alias_path = alias_manager.read_alias(repo_identifier)
        if alias_path and Path(alias_path).is_dir():
            return str(alias_path)
        # If not -global, try with -global suffix
        if not repo_identifier.endswith("-global"):
            alias_path = alias_manager.read_alias(f"{repo_identifier}-global")
            if alias_path and Path(alias_path).is_dir():
                return str(alias_path)

    # Try as full path first
    if not repo_identifier.endswith("-global"):
        repo_path = Path(repo_identifier)
        if _is_git_repo(repo_path):
            return str(repo_path)

    # Look up in global registry
    repo_entry = _get_global_repo(repo_identifier)

    if not repo_entry:
        return None

    # Get repo name without -global suffix
    repo_name = repo_identifier.removesuffix("-global")

    # Try 1: index_path directly (might be a git repo in test environments)
    index_path = repo_entry.get("index_path")
    if index_path:
        index_path_obj = Path(index_path)
        if _is_git_repo(index_path_obj):
            return str(index_path)

    # Get base directory (.cidx-server/)
    base_dir = Path(golden_repos_dir).parent.parent

    # Try 2: Check golden-repos/{name}
    alt_path = base_dir / "golden-repos" / repo_name
    if _is_git_repo(alt_path):
        return str(alt_path)

    # Try 3: Check golden-repos/repos/{name}
    alt_path = base_dir / "golden-repos" / "repos" / repo_name
    if _is_git_repo(alt_path):
        return str(alt_path)

    # Try 4: Check versioned repos in data/golden-repos/.versioned
    versioned_path = _find_latest_versioned_repo(Path(golden_repos_dir), repo_name)
    if versioned_path:
        return versioned_path

    # Try 5: Check versioned repos in alternative location
    versioned_path = _find_latest_versioned_repo(
        base_dir / "data" / "golden-repos", repo_name
    )
    if versioned_path:
        return versioned_path

    # Fallback: Return index_path if it exists as a directory (for non-git operations like regex_search)
    if index_path:
        index_path_obj = Path(index_path)
        if index_path_obj.is_dir():
            return str(index_path)

    return None


def _resolve_git_repo_path(
    repository_alias: str, username: str
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve repository path for git operations.

    For global repos (ending in -global): validates that the resolved path
    has a .git directory. Local repos (e.g. cidx-meta-global backed by
    local://) have no .git and git operations are not meaningful.

    For user-activated repos: returns the activated-repo path if it exists.

    Returns:
        (path, error_message) tuple. If error_message is not None,
        the caller should return the error to the user.
    """
    # Bug #432: Validate repository_alias is a string (clients may pass a list)
    if not isinstance(repository_alias, str):
        return (
            None,
            "repository_alias must be a string, not a list. Use a single repository alias.",
        )
    if repository_alias.endswith("-global"):
        golden_repos_dir = _get_golden_repos_dir()

        # Check repo URL first — local:// repos never support git operations
        repo_entry = _get_global_repo(repository_alias)
        if repo_entry and repo_entry.get("repo_url", "").startswith("local://"):
            return None, (
                f"Repository '{repository_alias}' is a local repository "
                "and does not support git operations."
            )

        resolved = _resolve_repo_path(repository_alias, golden_repos_dir)
        if resolved is None:
            return None, f"Repository '{repository_alias}' not found."
        if not (Path(resolved) / ".git").exists():
            return None, (
                f"Repository '{repository_alias}' is a local repository "
                "and does not support git operations."
            )

        # Story #387: Group access check - invisible repo pattern
        # Strip -global suffix to match base name stored in accessible repos set
        access_filtering_service = _get_access_filtering_service()
        if access_filtering_service is not None:
            base_alias = repository_alias[: -len("-global")]
            accessible = access_filtering_service.get_accessible_repos(username)
            if base_alias not in accessible:
                return None, f"Repository '{repository_alias}' not found."

        return resolved, None

    activated_repo_manager = ActivatedRepoManager()
    repo_path = activated_repo_manager.get_activated_repo_path(
        username=username, user_alias=repository_alias
    )
    if repo_path is None:
        return None, f"User-activated repository '{repository_alias}' not found."
    if not (Path(repo_path) / ".git").exists():
        return None, (
            f"Repository '{repository_alias}' does not have a .git directory "
            "and does not support git operations."
        )
    return repo_path, None


# Extracted to files.py (Story #496)
from .files import handle_directory_tree  # noqa: F401, E402

# Register file operation handlers from files.py (Story #496)
from .files import _register as _files_register  # noqa: E402
from .memory import _register as _memory_register  # noqa: E402

_files_register(HANDLER_REGISTRY)
_memory_register(HANDLER_REGISTRY)
_search_register(HANDLER_REGISTRY)


# SSH Key Management Handlers (Story #572) — extracted to ssh_keys.py
from .ssh_keys import (  # noqa: F401, E402
    get_ssh_key_manager,
    handle_ssh_key_create,
    handle_ssh_key_list,
    handle_ssh_key_delete,
    handle_ssh_key_show_public,
    handle_ssh_key_assign_host,
)
from .ssh_keys import _register as _ssh_keys_register  # noqa: E402
from .guides import _register as _guides_register  # noqa: E402
from .scip import _register as _scip_register  # noqa: E402

_ssh_keys_register(HANDLER_REGISTRY)
_guides_register(HANDLER_REGISTRY)
_scip_register(HANDLER_REGISTRY)

# SCIP handlers extracted to scip.py (Story #496).
# Re-exported here so that tests importing directly from _legacy continue to work.
from .scip import (  # noqa: F401, E402
    scip_definition,
    scip_references,
    scip_dependencies,
    scip_dependents,
    scip_impact,
    scip_callchain,
    scip_context,
    get_scip_audit_log,
    handle_scip_pr_history,
    handle_scip_cleanup_history,
    handle_scip_cleanup_workspaces,
    handle_scip_cleanup_status,
    _filter_audit_entries as _filter_audit_entries,
    _parse_log_details as _parse_log_details,
    _get_pr_logs_from_service as _get_pr_logs_from_service,
    _get_cleanup_logs_from_service as _get_cleanup_logs_from_service,
    _execute_workspace_cleanup,
)


# =============================================================================
# Git write handlers extracted to git_write.py (Story #496)
# =============================================================================

from .git_write import _register as _git_write_register  # noqa: E402
from .git_write import (  # noqa: F401, E402
    git_stage,
    git_unstage,
    git_commit,
    git_push,
    git_pull,
    git_reset,
    git_clean,
    git_merge,
    git_mark_resolved,
    git_merge_abort,
    git_checkout_file,
    git_branch_create,
    git_branch_switch,
    git_branch_delete,
    git_stash,
    git_amend,
    configure_git_credential,
    list_git_credentials,
    delete_git_credential,
    _get_pat_credential_for_remote,
)

_git_write_register(HANDLER_REGISTRY)


# =============================================================================
# Pull request handlers extracted to pull_requests.py (Story #496)
# Stories #390, #446, #447, #448, #449, #450, #451, #452
# =============================================================================

from .pull_requests import _register as _pr_register  # noqa: E402
from .pull_requests import (  # noqa: F401, E402
    create_pull_request,
    list_pull_requests,
    get_pull_request,
    list_pull_request_comments,
    comment_on_pull_request,
    update_pull_request,
    merge_pull_request,
    close_pull_request,
)

_pr_register(HANDLER_REGISTRY)

# =============================================================================
# Git read handlers extracted to git_read.py (Story #496)
# Stories #34, #35, #555, #556, #558, #639, #653, #654, #658, #660, #686
# =============================================================================

from .git_read import _register as _git_read_register  # noqa: E402
from .git_read import (  # noqa: F401, E402
    handle_git_file_history,
    handle_git_log,
    handle_git_show_commit,
    handle_git_file_at_revision,
    handle_git_diff,
    handle_git_blame,
    handle_git_search_commits,
    handle_git_search_diffs,
    git_status,
    git_fetch,
    git_branch_list,
    git_conflict_status,
    git_diff,
    git_log,
    _serialize_file_history_commits,
    _compute_file_history_fetch_limit,
    _omni_git_log,
    _omni_git_search_commits,
)

_git_read_register(HANDLER_REGISTRY)


# git_stash and git_amend extracted to git_write.py (Story #496)


# =============================================================================
# CI/CD handlers extracted to cicd.py (Story #496)
# Stories #633, #634, #404
# =============================================================================

from .cicd import _register as _cicd_register  # noqa: E402
from .cicd import (  # noqa: F401, E402
    _derive_forge_host,
    _get_personal_credential_for_host,
    _resolve_cicd_project_access,
    _resolve_cicd_read_token,
    _resolve_cicd_write_token,
    handle_gh_actions_list_runs,
    handle_gh_actions_get_run,
    handle_gh_actions_search_logs,
    handle_gh_actions_get_job_logs,
    handle_gh_actions_retry_run,
    handle_gh_actions_cancel_run,
    handle_gitlab_ci_list_pipelines,
    handle_gitlab_ci_get_pipeline,
    handle_gitlab_ci_search_logs,
    handle_gitlab_ci_get_job_logs,
    handle_gitlab_ci_retry_pipeline,
    handle_gitlab_ci_cancel_pipeline,
    handle_github_actions_list_runs,
    handle_github_actions_get_run,
    handle_github_actions_search_logs,
    handle_github_actions_get_job_logs,
    handle_github_actions_retry_run,
    handle_github_actions_cancel_run,
)

_cicd_register(HANDLER_REGISTRY)


# ============================================================================
# Story #679: Semantic Search with Payload Control - Cache Retrieval Handler
# ============================================================================

# =============================================================================
# Delegation handlers extracted to delegation.py (Story #496)
# =============================================================================

from .delegation import _register as _delegation_register  # noqa: E402
from .delegation import (  # noqa: F401, E402
    handle_list_delegation_functions,
    handle_execute_delegation_function,
    handle_poll_delegation_job,
    handle_execute_open_delegation,
    handle_cs_register_repository,
    handle_cs_list_repositories,
    handle_cs_check_health,
    _get_delegation_function_repo_path,
    _get_user_groups,
    _get_delegation_config,
    _validate_function_parameters,
    _ensure_repos_registered,
    _get_cidx_callback_base_url,
    _load_packages_context,
    _resolve_guardrails,
    _get_repo_ready_timeout,
    _validate_collaborative_params,
    _validate_competitive_params,
    _validate_open_delegation_params,
    _register_open_delegation_callback,
    _submit_open_delegation_job,
    _submit_collaborative_delegation_job,
    _submit_competitive_delegation_job,
    _lookup_golden_repo_for_cs,
)

_delegation_register(HANDLER_REGISTRY)


# handle_query_audit_logs, handle_enter_maintenance_mode, handle_exit_maintenance_mode
# extracted to admin.py (Story #496)


# handle_get_maintenance_status extracted to admin.py (Story #496)
# handle_scip_pr_history, handle_scip_cleanup_history, handle_scip_cleanup_workspaces,
# handle_scip_cleanup_status, _cleanup_job_state, _execute_workspace_cleanup
# extracted to scip.py (Story #496)


# list_repo_categories extracted to repos.py (Story #496)

# Admin handlers extracted to admin.py (Story #496)
from .admin import _register as _admin_register  # noqa: E402
from .admin import (  # noqa: F401, E402
    handle_authenticate,
    list_users,
    create_user,
    handle_set_session_impersonation,
    _get_group_manager,
    _validate_group_id,
    handle_list_groups,
    handle_create_group,
    handle_get_group,
    handle_update_group,
    handle_delete_group,
    handle_add_member_to_group,
    handle_remove_member_from_group,
    handle_add_repos_to_group,
    handle_remove_repo_from_group,
    handle_bulk_remove_repos_from_group,
    handle_list_api_keys,
    handle_create_api_key,
    handle_delete_api_key,
    handle_list_mcp_credentials,
    handle_create_mcp_credential,
    handle_delete_mcp_credential,
    handle_admin_list_user_mcp_credentials,
    handle_admin_create_user_mcp_credential,
    handle_admin_delete_user_mcp_credential,
    handle_admin_list_all_mcp_credentials,
    handle_admin_list_system_mcp_credentials,
    handle_query_audit_logs,
    handle_enter_maintenance_mode,
    handle_exit_maintenance_mode,
    handle_get_maintenance_status,
    handle_admin_logs_query,
    admin_logs_export,
    get_job_statistics,
    get_job_details,
    handle_get_global_config,
    handle_set_global_config,
    trigger_reindex,
    get_index_status,
    handle_trigger_dependency_analysis,
)

_admin_register(HANDLER_REGISTRY)


# ---------------------------------------------------------------------------
# Module-level forwarding for mock-patch compatibility
# ---------------------------------------------------------------------------
# When domain handlers are extracted from this module into separate files
# (e.g. scip.py, guides.py), those modules import utilities like
# `_get_scip_query_service` directly from `_utils`.  Tests that patch
# `code_indexer.server.mcp.handlers._legacy._get_scip_query_service` would
# normally only update the binding in THIS module's global dict, leaving the
# domain module's binding untouched.
#
# The _ForwardingModule below intercepts every `setattr` on _legacy and
# mirrors the write into each extracted domain module (when the name exists
# there), preserving all existing test patches without requiring test changes.


class _LegacyForwardingModule(types.ModuleType):
    """Forward attribute writes on _legacy to extracted domain submodules."""

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if not name.startswith("__"):
            for _submod_name in (
                "code_indexer.server.mcp.handlers.scip",
                "code_indexer.server.mcp.handlers.guides",
                "code_indexer.server.mcp.handlers.ssh_keys",
                "code_indexer.server.mcp.handlers.delegation",
                "code_indexer.server.mcp.handlers.pull_requests",
                "code_indexer.server.mcp.handlers.git_read",
                "code_indexer.server.mcp.handlers.git_write",
                "code_indexer.server.mcp.handlers.admin",
                "code_indexer.server.mcp.handlers.cicd",
                "code_indexer.server.mcp.handlers.files",
                "code_indexer.server.mcp.handlers.repos",
                "code_indexer.server.mcp.handlers.search",
            ):
                _submod = sys.modules.get(_submod_name)
                if _submod is not None and name in _submod.__dict__:
                    _submod.__dict__[name] = value
            # app_module lives in _utils — forward writes so that
            # patches on _legacy.app_module also affect _utils.app_module
            if name == "app_module":
                _utils_mod = sys.modules.get("code_indexer.server.mcp.handlers._utils")
                if _utils_mod is not None:
                    _utils_mod.__dict__["app_module"] = value


sys.modules[__name__].__class__ = _LegacyForwardingModule
