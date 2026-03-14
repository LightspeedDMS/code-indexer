"""
Helper functions for the CIDX server application.

Extracted from app.py as part of Story #409 (app.py modularization) to break
the circular import between app.py and routers/inline_routes.py.

Previously these functions were defined at module level in app.py and used
module-level globals. They now accept their dependencies as parameters so they
can be imported without pulling in the full app module.

The _server_start_time global lives here as its authoritative home.
app.py sets it via set_server_start_time() during create_app().
"""

import json
import logging
import psutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

from .models.repos import ComponentRepoInfo, CompositeRepositoryDetails
from .models.activated_repository import ActivatedRepository
from .logging_utils import format_error_log
from .middleware.correlation import get_correlation_id

logger = logging.getLogger(__name__)

# Server startup time for health monitoring (authoritative location)
_server_start_time: Optional[str] = None


def set_server_start_time(ts: str) -> None:
    """Set the server startup timestamp (called from create_app)."""
    global _server_start_time
    _server_start_time = ts


def get_server_uptime() -> Optional[int]:
    """
    Get server uptime in seconds.

    Returns:
        Uptime in seconds, or None if startup time not available
    """
    if not _server_start_time:
        return None

    try:
        started_at = datetime.fromisoformat(_server_start_time)
        uptime = datetime.now(timezone.utc) - started_at
        return int(uptime.total_seconds())
    except (ValueError, TypeError) as e:
        logger.warning(f"Failed to parse server start time '{_server_start_time}': {e}")
        return None


def get_server_start_time() -> Optional[str]:
    """
    Get server startup timestamp.

    Returns:
        ISO format timestamp string, or None if not available
    """
    return _server_start_time


def get_system_resources() -> Optional[Dict[str, Any]]:
    """
    Get system resource usage information.

    Returns:
        Dictionary with memory and CPU usage, or None if unavailable
    """
    try:
        process = psutil.Process()
        memory_info = process.memory_info()
        memory_percent = process.memory_percent()

        # Get CPU usage (averaged over short period)
        cpu_percent = process.cpu_percent(interval=0.1)

        return {
            "memory_usage_mb": round(memory_info.rss / 1024 / 1024),
            "memory_usage_percent": round(memory_percent, 1),
            "cpu_usage_percent": round(cpu_percent, 1),
        }
    except Exception as e:
        logger.warning(f"Failed to get system resources: {e}")
        return None


def check_database_health(
    user_manager: Any = None,
    background_job_manager: Any = None,
) -> Optional[Dict[str, str]]:
    """
    Check health of database connections.

    Args:
        user_manager: UserManager instance (optional)
        background_job_manager: BackgroundJobManager instance (optional)

    Returns:
        Dictionary with database health status, or None if unavailable
    """
    try:
        health_status = {}

        # Check user manager database health
        if user_manager:
            try:
                # Simple check - get user count
                user_manager.get_all_users()
                health_status["users_db"] = "healthy"
            except Exception as e:
                logger.warning(f"User database health check failed: {e}")
                health_status["users_db"] = "unhealthy"

        # Check background job manager health
        if background_job_manager:
            try:
                # Simple check - get job count
                background_job_manager.get_active_job_count()
                health_status["jobs_db"] = "healthy"
            except Exception as e:
                logger.warning(f"Jobs database health check failed: {e}")
                health_status["jobs_db"] = "unhealthy"

        return health_status if health_status else None
    except Exception as e:
        logger.warning(f"Database health check failed: {e}")
        return None


def get_recent_errors() -> Optional[List[Dict[str, Any]]]:
    """
    Get recent error information.

    Returns:
        List of recent errors, or None if unavailable
    """
    try:
        # This is a placeholder - in a real implementation,
        # this would read from log files or error tracking system
        return []
    except Exception as e:
        logger.warning(f"Failed to get recent errors: {e}")
        return None


def _apply_rest_semantic_truncation(
    results: List[Dict[str, Any]],
    payload_cache: Any = None,
) -> List[Dict[str, Any]]:
    """Apply payload truncation to semantic search results from REST API.

    For results with large code_snippet content, replaces content with preview + cache_handle.
    This provides consistency with MCP handlers (Story #683 follow-up).

    Args:
        results: List of semantic search result dicts with 'code_snippet' field
        payload_cache: PayloadCache instance (optional, from app.state.payload_cache)

    Returns:
        Modified results list with truncation applied
    """
    if payload_cache is None:
        # Cache not available, return results unchanged
        return results

    preview_size = payload_cache.config.preview_size_chars

    for result_dict in results:
        code_snippet = result_dict.get("code_snippet")
        if code_snippet is None:
            # No content to truncate, add default metadata
            result_dict["cache_handle"] = None
            result_dict["has_more"] = False
            continue

        try:
            if len(code_snippet) > preview_size:
                # Large snippet: store and replace with preview
                cache_handle = payload_cache.store(code_snippet)
                result_dict["preview"] = code_snippet[:preview_size]
                result_dict["cache_handle"] = cache_handle
                result_dict["has_more"] = True
                result_dict["total_size"] = len(code_snippet)
                del result_dict["code_snippet"]
            else:
                # Small snippet: keep as-is, add metadata
                result_dict["cache_handle"] = None
                result_dict["has_more"] = False
        except Exception as e:
            logger.warning(
                format_error_log(
                    "APP-GENERAL-001",
                    f"Failed to truncate code_snippet in REST API: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            result_dict["cache_handle"] = None
            result_dict["has_more"] = False

    return results


def _apply_rest_fts_truncation(
    results: List[Dict[str, Any]],
    payload_cache: Any = None,
) -> List[Dict[str, Any]]:
    """Apply payload truncation to FTS search results from REST API.

    For FTS results with large snippet or match_text content, replaces content with
    preview + cache_handle. This provides consistency with MCP handlers
    (Story #680 follow-up, Bug Fix - Issue #2 from code review).

    Args:
        results: List of FTS search result dicts with 'snippet' and/or 'match_text' fields
        payload_cache: PayloadCache instance (optional, from app.state.payload_cache)

    Returns:
        Modified results list with truncation applied to FTS fields
    """
    if payload_cache is None:
        # Cache not available, return results unchanged
        return results

    preview_size = payload_cache.config.preview_size_chars

    for result_dict in results:
        # Handle snippet field (FTS-specific)
        snippet = result_dict.get("snippet")
        if snippet is not None:
            try:
                if len(snippet) > preview_size:
                    # Large snippet: store and replace with preview
                    cache_handle = payload_cache.store(snippet)
                    result_dict["snippet_preview"] = snippet[:preview_size]
                    result_dict["snippet_cache_handle"] = cache_handle
                    result_dict["snippet_has_more"] = True
                    result_dict["snippet_total_size"] = len(snippet)
                    del result_dict["snippet"]
                else:
                    # Small snippet: keep as-is, add metadata
                    result_dict["snippet_cache_handle"] = None
                    result_dict["snippet_has_more"] = False
            except Exception as e:
                logger.warning(
                    format_error_log(
                        "APP-GENERAL-002",
                        f"Failed to truncate FTS snippet in REST API: {e}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                result_dict["snippet_cache_handle"] = None
                result_dict["snippet_has_more"] = False

        # Handle match_text field (Issue #2 - MCP parity with _apply_fts_payload_truncation)
        match_text = result_dict.get("match_text")
        if match_text is not None:
            try:
                if len(match_text) > preview_size:
                    # Large match_text: store and replace with preview
                    cache_handle = payload_cache.store(match_text)
                    result_dict["match_text_preview"] = match_text[:preview_size]
                    result_dict["match_text_cache_handle"] = cache_handle
                    result_dict["match_text_has_more"] = True
                    result_dict["match_text_total_size"] = len(match_text)
                    del result_dict["match_text"]
                else:
                    # Small match_text: keep as-is, add metadata
                    result_dict["match_text_cache_handle"] = None
                    result_dict["match_text_has_more"] = False
            except Exception as e:
                logger.warning(
                    format_error_log(
                        "APP-GENERAL-003",
                        f"Failed to truncate FTS match_text in REST API: {e}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                result_dict["match_text_cache_handle"] = None
                result_dict["match_text_has_more"] = False

    return results


def _find_activated_repository(
    repo_id: str,
    username: str,
    activated_repo_manager: Any,
) -> Optional[str]:
    """
    Find an activated repository and return its user_alias.

    Args:
        repo_id: Repository identifier to find (user_alias or golden_repo_alias)
        username: Username whose repositories to search
        activated_repo_manager: ActivatedRepoManager instance

    Returns:
        user_alias string if found, None if not found
    """
    if not activated_repo_manager:
        return None

    activated_repos = activated_repo_manager.list_activated_repositories(username)
    for repo in activated_repos:
        if (
            repo["user_alias"] == repo_id
            or repo["golden_repo_alias"] == repo_id
        ):
            return repo["user_alias"]

    return None


def _execute_repository_sync(
    repo_id: str,
    username: str,
    options: Dict[str, Any],
    activated_repo_manager: Any = None,
    progress_callback: Optional[Callable[[int], None]] = None,
) -> Dict[str, Any]:
    """
    Execute repository synchronization in background job.

    Args:
        repo_id: Repository identifier to sync
        username: Username requesting the sync
        options: Sync options (incremental, force, pull_remote, etc.)
        activated_repo_manager: ActivatedRepoManager instance
        progress_callback: Optional callback for progress updates

    Returns:
        Sync result dictionary

    Raises:
        ActivatedRepoError: If repository not found or not accessible
        GitOperationError: If git operations fail
    """
    from .repositories.activated_repo_manager import ActivatedRepoError
    from .repositories.golden_repo_manager import GitOperationError

    if progress_callback:
        progress_callback(10)  # Starting sync

    try:
        user_alias = _find_activated_repository(repo_id, username, activated_repo_manager)

        if user_alias is None:
            raise ActivatedRepoError(
                f"Repository '{repo_id}' not found for user '{username}'"
            )

        if progress_callback:
            progress_callback(25)  # Repository found, starting sync

        # Handle git pull if requested
        if options.get("pull_remote", False):
            if progress_callback:
                progress_callback(40)  # Pulling remote changes
            # Git pull will be handled by sync_with_golden_repository

        if progress_callback:
            progress_callback(60)  # Starting repository sync

        # Execute the actual sync using existing functionality
        sync_result = activated_repo_manager.sync_with_golden_repository(
            username=username, user_alias=user_alias
        )

        if progress_callback:
            progress_callback(90)  # Sync completed, finalizing

        result = {
            "success": sync_result.get("success", True),
            "message": sync_result.get(
                "message", f"Repository '{repo_id}' synchronized successfully"
            ),
            "repository_id": repo_id,
            "changes_applied": sync_result.get("changes_applied", False),
            "files_changed": sync_result.get("files_changed", 0),
            "options_used": {
                "incremental": options.get("incremental", True),
                "force": options.get("force", False),
                "pull_remote": options.get("pull_remote", False),
            },
        }

        if progress_callback:
            progress_callback(100)  # Complete

        return result

    except Exception as e:
        from .repositories.activated_repo_manager import ActivatedRepoError
        from .repositories.golden_repo_manager import GitOperationError
        # Re-raise known exceptions
        if isinstance(e, (ActivatedRepoError, GitOperationError)):
            raise
        # Wrap unknown exceptions
        raise GitOperationError(f"Repository sync failed: {str(e)}")


# Helper functions for composite repository details (Story 3.2)
def _analyze_component_repo(repo_path: Path, name: str) -> ComponentRepoInfo:
    """
    Analyze a single component repository.

    Args:
        repo_path: Path to the component repository
        name: Name of the component repository

    Returns:
        ComponentRepoInfo with repository analysis
    """
    # Check for index
    index_dir = repo_path / ".code-indexer"
    has_index = index_dir.exists()

    # Get file count from metadata
    file_count = 0
    last_indexed = None
    if has_index:
        metadata_file = index_dir / "metadata.json"
        if metadata_file.exists():
            try:
                metadata = json.loads(metadata_file.read_text())
                file_count = metadata.get("indexed_files", 0)
                # Try to get last_indexed timestamp if available
                if "last_indexed" in metadata:
                    try:
                        last_indexed = datetime.fromisoformat(metadata["last_indexed"])
                    except (ValueError, TypeError) as e:
                        logger.warning(
                            f"Failed to parse last_indexed timestamp for repo {name}: {e}"
                        )
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to read metadata for repo {name}: {e}")
                file_count = 0

    # Calculate repo size
    total_size = 0
    for item in repo_path.rglob("*"):
        if item.is_file():
            try:
                total_size += item.stat().st_size
            except (OSError, IOError) as e:
                logger.warning(f"Failed to stat file {item}: {e}")
                continue

    return ComponentRepoInfo(
        name=name,
        path=str(repo_path),
        has_index=has_index,
        collection_exists=has_index,
        indexed_files=file_count,
        last_indexed=last_indexed,
        size_mb=total_size / (1024 * 1024),
    )


def _get_composite_details(repo: ActivatedRepository) -> CompositeRepositoryDetails:
    """
    Aggregate details from all component repositories.

    Args:
        repo: ActivatedRepository instance (must be composite)

    Returns:
        CompositeRepositoryDetails with aggregated information
    """
    from code_indexer.proxy.config_manager import ProxyConfigManager

    component_info = []
    total_files = 0
    total_size = 0.0

    # Use ProxyConfigManager to get component repos
    proxy_config = ProxyConfigManager(repo.path)

    try:
        discovered_repos = proxy_config.get_repositories()
    except Exception as e:
        logger.warning(
            f"Failed to load proxy config for composite repo {repo.user_alias}, "
            f"falling back to discovered_repos metadata: {e}"
        )
        discovered_repos = repo.discovered_repos

    for repo_name in discovered_repos:
        subrepo_path = repo.path / repo_name
        if subrepo_path.exists():
            info = _analyze_component_repo(subrepo_path, repo_name)
            component_info.append(info)
            total_files += info.indexed_files
            total_size += info.size_mb

    return CompositeRepositoryDetails(
        user_alias=repo.user_alias,
        is_composite=True,
        activated_at=repo.activated_at,
        last_accessed=repo.last_accessed,
        component_repositories=component_info,
        total_files=total_files,
        total_size_mb=total_size,
    )
