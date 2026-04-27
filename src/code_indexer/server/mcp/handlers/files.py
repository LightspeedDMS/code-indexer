"""File operations — CRUD, directory browsing, write mode management.

Domain module for file operation handlers. Part of the handlers package
modularization (Story #496).
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import logging
from typing import Dict, Any, Optional, Tuple
from pathlib import Path

from code_indexer.server.auth.user_manager import User
from . import _utils
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.logging_utils import format_error_log
from ._utils import (
    CapBreach,
    cap_breach_response,
    _coerce_int,
    _expand_wildcard_patterns,
    _get_access_filtering_service,
    _get_available_repos,
    _error_with_suggestions,
    _mcp_response,
    _get_golden_repos_dir,
    _list_global_repos,
    _get_app_refresh_scheduler,
    _format_omni_response,
    _get_wiki_enabled_repos,
    _enrich_with_wiki_url,
    _parse_json_string_array,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------
_MAX_MCP_FILE_LIMIT = 500
_DEFAULT_PAGE = 1
_MIN_LIMIT = 1
_DEFAULT_TREE_DEPTH = 3
_DEFAULT_MAX_FILES_PER_DIR = 50
_VALID_SORT_FIELDS = ("path", "size", "modified_at")
_ACTIVATED_REPOS_PATH_SEGMENT = "/activated-repos/"


# ---------------------------------------------------------------------------
# Foundational helpers
# ---------------------------------------------------------------------------


def _get_legacy():
    """Lazy import of _legacy module to avoid circular imports."""
    from . import _legacy

    return _legacy


def _write_mode_strip_global(repo_alias: str) -> str:
    """Return alias without trailing '-global' suffix."""
    return (
        repo_alias[: -len("-global")] if repo_alias.endswith("-global") else repo_alias
    )


def _is_write_mode_active(repo_alias: str, golden_repos_dir: Optional[str]) -> bool:
    """Return True if a write-mode marker exists for repo_alias."""
    if not golden_repos_dir:
        return False
    alias = _write_mode_strip_global(repo_alias)
    marker_file = Path(golden_repos_dir) / ".write_mode" / f"{alias}.json"
    return marker_file.exists()


def _start_auto_watch_if_needed(
    repository_alias: str, user: User, error_code: str
) -> None:
    """Start auto-watch for a repository unless write mode is active.

    Shared by handle_create_file, handle_edit_file, handle_delete_file.
    Logs and continues on failure (auto-watch is enhancement, not critical).
    """
    from code_indexer.server.services.file_crud_service import file_crud_service
    from code_indexer.server.services.auto_watch_manager import auto_watch_manager
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    try:
        if file_crud_service.is_write_exception(repository_alias):
            repo_path = str(
                file_crud_service.get_write_exception_path(repository_alias)
            )
        else:
            activated_repo_manager = ActivatedRepoManager()
            repo_path = activated_repo_manager.get_activated_repo_path(
                username=user.username, user_alias=repository_alias
            )
        golden_repos_dir = getattr(
            _utils.app_module.app.state, "golden_repos_dir", None
        )
        if not _is_write_mode_active(repository_alias, golden_repos_dir):
            auto_watch_manager.start_watch(repo_path)
        else:
            logger.debug(
                f"Skipping auto-watch start for {repository_alias}: write mode active",
                extra={"correlation_id": get_correlation_id()},
            )
    except Exception as e:
        logger.warning(
            format_error_log(
                error_code,
                f"Failed to start auto-watch for {repository_alias}: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )


def _invalidate_wiki_cache(repository_alias: str, file_path: str) -> None:
    """Fire-and-forget wiki cache invalidation for file changes (Story #304)."""
    try:
        from code_indexer.server.wiki.wiki_cache_invalidator import (
            wiki_cache_invalidator,
        )

        wiki_cache_invalidator.invalidate_for_file_change(repository_alias, file_path)
    except Exception as e:
        logger.debug(
            f"Wiki cache invalidation skipped for {file_path}: {e}",
            extra={"correlation_id": get_correlation_id()},
        )


def _is_writable_repo(
    repo_alias: str, resolved_repo_path: Optional[str], golden_repos_dir: Optional[str]
) -> bool:
    """Return True if write operations are allowed for this repo.

    Bug #391: Activated repos (user workspaces) are always writable without a
    write-mode marker.

    NOTE: Called by git_write.py and pull_requests.py via _legacy._is_writable_repo.
    """
    if _is_write_mode_active(repo_alias, golden_repos_dir):
        return True
    if resolved_repo_path and _ACTIVATED_REPOS_PATH_SEGMENT in resolved_repo_path:
        return True
    return False


def _write_mode_acquire_lock(refresh_scheduler: Any, alias: str) -> Tuple[bool, str]:
    """Acquire write lock; return (acquired, owner_if_held)."""
    acquired = refresh_scheduler.acquire_write_lock(alias, owner_name="mcp_write_mode")
    if acquired:
        return True, ""
    wlm = getattr(refresh_scheduler, "write_lock_manager", None)
    owner = "unknown"
    if wlm is not None:
        info = wlm.get_lock_info(alias)
        if info:
            owner = info.get("owner", "unknown")
    return False, owner


def _write_mode_create_marker(
    golden_repos_dir: Path, alias: str, source_path: str
) -> None:
    """Create the .write_mode/{alias}.json marker file."""
    import json as _json
    from datetime import datetime, timezone

    write_mode_dir = golden_repos_dir / ".write_mode"
    write_mode_dir.mkdir(parents=True, exist_ok=True)
    marker_file = write_mode_dir / f"{alias}.json"
    marker_file.write_text(
        _json.dumps(
            {
                "alias": alias,
                "source_path": source_path,
                "entered_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )


def _write_mode_run_refresh(
    refresh_scheduler: Any, repo_alias: str, golden_repos_dir: Path, alias: str
) -> None:
    """Run synchronous refresh, delete marker, release lock, stop auto-watch."""
    import json as _json

    from code_indexer.server.services.auto_watch_manager import auto_watch_manager

    marker_file = golden_repos_dir / ".write_mode" / f"{alias}.json"
    source_path: Optional[str] = None
    try:
        marker_data = _json.loads(marker_file.read_text())
        source_path = marker_data.get("source_path")
    except Exception as e:
        logger.debug(
            f"Could not read write-mode marker for {alias}: {e}",
            extra={"correlation_id": get_correlation_id()},
        )

    try:
        marker_file.unlink()
    except FileNotFoundError:
        pass
    refresh_scheduler.release_write_lock(alias, owner_name="mcp_write_mode")

    if source_path:
        try:
            auto_watch_manager.stop_watch(source_path)
            logger.debug(
                f"Stopped auto-watch for {source_path} before write-mode refresh",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as e:
            logger.warning(
                f"Failed to stop auto-watch for {source_path} before refresh: {e}",
                extra={"correlation_id": get_correlation_id()},
            )

    refresh_scheduler._execute_refresh(repo_alias)


# ---------------------------------------------------------------------------
# Internal helpers for handler logic
# ---------------------------------------------------------------------------


def _resolve_global_repo_target(
    repository_alias: str,
    user: User,
    extra_error_fields: Optional[dict] = None,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Resolve a -global repo alias to its target path.

    Returns (target_path, None) on success, or (None, error_response) on failure.
    """
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
        if extra_error_fields:
            error_envelope.update(extra_error_fields)
        return None, _mcp_response(error_envelope)

    from code_indexer.global_repos.alias_manager import AliasManager

    alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
    target_path = alias_manager.read_alias(repository_alias)

    if not target_path:
        available_repos = _get_available_repos(user)
        error_envelope = _error_with_suggestions(
            error_msg=f"Alias for '{repository_alias}' not found",
            attempted_value=repository_alias,
            available_values=available_repos,
        )
        if extra_error_fields:
            error_envelope.update(extra_error_fields)
        return None, _mcp_response(error_envelope)

    return target_path, None


def _build_path_pattern(
    path: str, recursive: bool, user_path_pattern: Optional[str]
) -> Optional[str]:
    """Build path pattern for list_files from path + user pattern."""
    path = path.rstrip("/") if path else ""
    if path:
        if user_path_pattern:
            if recursive:
                return f"{path}/**/{user_path_pattern}"
            return f"{path}/{user_path_pattern}"
        return f"{path}/**/*" if recursive else f"{path}/*"
    if user_path_pattern:
        return user_path_pattern
    return None


def _serialize_file_results(result: Any) -> list:
    """Extract and serialize FileInfo objects from a service result."""
    if hasattr(result, "files"):
        files_data = result.files
    elif isinstance(result, dict):
        files_data = result.get("files", [])
    else:
        files_data = []

    return [
        f.model_dump(mode="json") if hasattr(f, "model_dump") else f for f in files_data
    ]


def _filter_cidx_meta_files(
    serialized_files: list, repository_alias: Any, user: User
) -> list:
    """Bug #336: Filter cidx-meta files to only show repos user can access.

    NOTE: When access_filtering_service is unavailable (e.g. during startup or
    in test environments), this preserves the existing _legacy.py fail-open
    behavior and returns files unfiltered with a warning log. Changing to
    fail-closed would be a behavioral change requiring a separate story.
    """
    if not repository_alias or "cidx-meta" not in str(repository_alias):
        return serialized_files
    access_filtering_service = _get_access_filtering_service()
    if not access_filtering_service:
        logger.warning(
            "Access filtering service unavailable for cidx-meta filtering; "
            "returning unfiltered results",
            extra={"correlation_id": get_correlation_id()},
        )
        return serialized_files
    filenames = [Path(f["path"]).name for f in serialized_files]
    allowed = set(
        access_filtering_service.filter_cidx_meta_files(filenames, user.username)
    )
    return [f for f in serialized_files if Path(f["path"]).name in allowed]


def _build_browse_path_pattern(
    path: str, recursive: bool, user_path_pattern: Optional[str]
) -> Optional[str]:
    """Build path pattern for browse_directory from path + user pattern.

    Differs from _build_path_pattern in handling absolute patterns.
    """
    path = path.rstrip("/") if path else ""

    is_absolute_pattern = False
    if user_path_pattern:
        is_absolute_pattern = "/" in user_path_pattern or user_path_pattern.startswith(
            "**"
        )

    if path:
        if user_path_pattern:
            if is_absolute_pattern:
                return user_path_pattern
            if recursive:
                return f"{path}/**/{user_path_pattern}"
            return f"{path}/{user_path_pattern}"
        return f"{path}/**/*" if recursive else f"{path}/*"
    if user_path_pattern:
        return user_path_pattern
    return None


def _parse_pagination_params(params: Dict[str, Any]) -> Any:
    """Parse and validate offset/limit pagination parameters.

    Returns (offset, limit) on success, or (error_response, None) on failure.
    """
    _offset_raw = params.get("offset")
    _limit_raw = params.get("limit")
    _offset_invalid = (
        isinstance(_offset_raw, float) and not float(_offset_raw).is_integer()
    )
    _limit_invalid = (
        isinstance(_limit_raw, float) and not float(_limit_raw).is_integer()
    )
    offset = (
        0
        if _offset_invalid
        else (_coerce_int(_offset_raw, 0) if _offset_raw is not None else None)
    )
    limit = (
        0
        if _limit_invalid
        else (_coerce_int(_limit_raw, 0) if _limit_raw is not None else None)
    )

    if offset is not None and offset < _MIN_LIMIT:
        return _mcp_response(
            {
                "success": False,
                "error": "offset must be an integer >= 1",
                "content": [],
                "metadata": {},
            }
        ), None

    if limit is not None and limit < _MIN_LIMIT:
        return _mcp_response(
            {
                "success": False,
                "error": "limit must be an integer >= 1",
                "content": [],
                "metadata": {},
            }
        ), None

    return offset, limit


def _build_file_content_response(
    result: dict, file_path: str, repository_alias: str
) -> Dict[str, Any]:
    """Build the MCP response for get_file_content with truncation support."""
    file_content = result.get("content", "")
    metadata = result.get("metadata", {})

    payload_cache = getattr(_utils.app_module.app.state, "payload_cache", None)
    config_service = get_config_service()
    content_limits = config_service.get_config().content_limits_config

    cache_handle = None
    truncated = False
    total_tokens = 0
    preview_tokens = 0
    total_pages = 0
    has_more = False

    if payload_cache is not None and file_content and content_limits is not None:
        from code_indexer.server.cache.truncation_helper import TruncationHelper

        truncation_helper = TruncationHelper(payload_cache, content_limits)
        truncation_result = truncation_helper.truncate_and_cache(
            content=file_content,
            content_type="file",
        )

        file_content = truncation_result.preview
        cache_handle = truncation_result.cache_handle
        truncated = truncation_result.truncated
        total_tokens = truncation_result.original_tokens
        preview_tokens = truncation_result.preview_tokens
        total_pages = truncation_result.total_pages
        has_more = truncation_result.has_more

    content_blocks = [{"type": "text", "text": file_content}] if file_content else []

    metadata["cache_handle"] = cache_handle
    metadata["truncated"] = truncated
    metadata["total_tokens"] = total_tokens
    metadata["preview_tokens"] = preview_tokens
    metadata["total_pages"] = total_pages
    metadata["has_more"] = has_more

    wiki_enabled_repos = _get_wiki_enabled_repos()
    _enrich_with_wiki_url(metadata, file_path, repository_alias, wiki_enabled_repos)

    return _mcp_response(
        {
            "success": True,
            "content": content_blocks,
            "metadata": metadata,
            "cache_handle": cache_handle,
            "truncated": truncated,
            "total_pages": total_pages,
            "has_more": has_more,
        }
    )


def _tree_node_to_dict(node: Any) -> dict:
    """Convert a TreeNode to a serializable dict."""
    result_dict = {
        "name": node.name,
        "path": node.path,
        "is_directory": node.is_directory,
        "truncated": node.truncated,
        "hidden_count": node.hidden_count,
    }
    if node.children is not None:
        result_dict["children"] = [_tree_node_to_dict(c) for c in node.children]
    else:
        result_dict["children"] = None
    return result_dict


def _filter_cidx_meta_tree(result: Any, user: User) -> Any:
    """Bug #336: Filter cidx-meta tree to only show repos user can access."""
    _tree_access_svc = _get_access_filtering_service()
    if not _tree_access_svc or result.root.children is None:
        return result

    _all_names = [node.name for node in result.root.children if not node.is_directory]
    _allowed = set(_tree_access_svc.filter_cidx_meta_files(_all_names, user.username))
    result.root.children = [
        node
        for node in result.root.children
        if node.is_directory or node.name in _allowed
    ]
    _filtered_lines = []
    for _line in result.tree_string.splitlines():
        _stripped = _line.strip()
        _name = _stripped.lstrip("|+- ")
        if not _name.endswith("/") and _name in _all_names and _name not in _allowed:
            continue
        _filtered_lines.append(_line)
    return result.__class__(
        root=result.root,
        tree_string="\n".join(_filtered_lines),
        total_directories=result.total_directories,
        total_files=len(result.root.children),
        max_depth_reached=result.max_depth_reached,
        root_path=result.root_path,
    )


# ---------------------------------------------------------------------------
# Omni-list helpers
# ---------------------------------------------------------------------------


def _omni_list_files_single_repo(
    params: Dict[str, Any], repo_alias: str, user: User
) -> Tuple[Optional[list], Optional[str]]:
    """Execute list_files for a single repo and parse the MCP response.

    Returns (files_list, None) on success, or (None, error_message) on failure.
    """
    import json as json_module

    single_params = dict(params)
    single_params["repository_alias"] = repo_alias

    single_result = list_files(single_params, user)

    content = single_result.get("content", [])
    if content and content[0].get("type") == "text":
        result_data = json_module.loads(content[0]["text"])
        if result_data.get("success"):
            files_list = result_data.get("files", [])
            for f in files_list:
                f["source_repo"] = repo_alias
            return files_list, None
        return None, result_data.get("error", "Unknown error")
    return None, "Empty or invalid response"


def _omni_filter_errors(errors: Dict[str, str], user: User) -> Dict[str, str]:
    """Story #331 AC7: Filter errors dict to hide unauthorized repo aliases."""
    _ac7_service = _get_access_filtering_service()
    if _ac7_service and not _ac7_service.is_admin_user(user.username):
        _ac7_accessible = _ac7_service.get_accessible_repos(user.username)
        return {
            k: v
            for k, v in errors.items()
            if k.removesuffix("-global") in _ac7_accessible or k in _ac7_accessible
        }
    return errors


def _omni_list_files(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handle omni-list-files across multiple repositories."""
    repo_aliases = params.get("repository_alias", [])
    repo_aliases = _expand_wildcard_patterns(repo_aliases, user)
    if isinstance(repo_aliases, CapBreach):
        return cap_breach_response(repo_aliases)

    if not repo_aliases:
        return _mcp_response(
            {
                "success": True,
                "files": [],
                "total_files": 0,
                "repos_searched": 0,
                "errors": {},
            }
        )

    all_files: list = []
    errors: Dict[str, str] = {}
    repos_searched = 0

    for repo_alias in repo_aliases:
        try:
            files_list, error_msg = _omni_list_files_single_repo(
                params, repo_alias, user
            )
            if files_list is not None:
                repos_searched += 1
                all_files.extend(files_list)
            elif error_msg:
                errors[repo_alias] = error_msg
        except Exception as e:
            errors[repo_alias] = str(e)
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-034",
                    f"Omni-list-files failed for {repo_alias}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

    errors = _omni_filter_errors(errors, user)

    response_format = params.get("response_format", "flat")
    formatted = _format_omni_response(
        all_results=all_files,
        response_format=response_format,
        total_repos_searched=repos_searched,
        errors=errors,
    )
    if response_format == "flat":
        formatted["files"] = formatted.pop("results")
        formatted["total_files"] = formatted.pop("total_results")
        formatted["repos_searched"] = formatted.pop("total_repos_searched")
    return _mcp_response(formatted)


def list_files(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """List files in a repository."""
    from code_indexer.server.models.api_models import FileListQueryParams

    try:
        repository_alias = params["repository_alias"]
        repository_alias = _parse_json_string_array(repository_alias)
        params["repository_alias"] = repository_alias

        if isinstance(repository_alias, list):
            return _omni_list_files(params, user)

        path = params.get("path", "")
        recursive = params.get("recursive", True)
        user_path_pattern = params.get("path_pattern")

        final_path_pattern = _build_path_pattern(path, recursive, user_path_pattern)

        query_params = FileListQueryParams(
            page=_DEFAULT_PAGE,
            limit=_MAX_MCP_FILE_LIMIT,
            path_pattern=final_path_pattern,
        )

        if repository_alias and repository_alias.endswith("-global"):
            target_path, error_resp = _resolve_global_repo_target(
                repository_alias, user, {"files": []}
            )
            if error_resp is not None:
                return error_resp

            result = _utils.app_module.file_service.list_files_by_path(
                repo_path=target_path,
                query_params=query_params,
            )
        else:
            result = _utils.app_module.file_service.list_files(
                repo_id=repository_alias,
                username=user.username,
                query_params=query_params,
            )

        serialized_files = _serialize_file_results(result)
        serialized_files = _filter_cidx_meta_files(
            serialized_files, repository_alias, user
        )

        return _mcp_response({"success": True, "files": serialized_files})
    except Exception as e:
        logger.exception(
            f"Unexpected error in list_files: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e), "files": []})


def get_file_content(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get content of a specific file with optional pagination."""
    try:
        repository_alias = params["repository_alias"]
        file_path = params["file_path"]

        # Bug #336: Check cidx-meta file-level access before returning content
        if repository_alias and "cidx-meta" in repository_alias:
            access_filtering_svc = _get_access_filtering_service()
            if access_filtering_svc:
                basename = Path(file_path).name
                allowed = access_filtering_svc.filter_cidx_meta_files(
                    [basename], user.username
                )
                if not allowed:
                    return _mcp_response(
                        {
                            "success": False,
                            "error": f"Access denied: you are not authorized to access '{basename}'",
                            "content": [],
                            "metadata": {},
                        }
                    )

        pagination = _parse_pagination_params(params)
        if isinstance(pagination[0], dict):
            return pagination[0]  # Early-return error response
        offset, limit = pagination

        if repository_alias and repository_alias.endswith("-global"):
            target_path, error_resp = _resolve_global_repo_target(
                repository_alias, user, {"content": [], "metadata": {}}
            )
            if error_resp is not None:
                return error_resp

            result = _utils.app_module.file_service.get_file_content_by_path(
                repo_path=target_path,
                file_path=file_path,
                offset=offset,
                limit=limit,
                skip_truncation=True,
            )
        else:
            result = _utils.app_module.file_service.get_file_content(
                repository_alias=repository_alias,
                file_path=file_path,
                username=user.username,
                offset=offset,
                limit=limit,
                skip_truncation=True,
            )

        return _build_file_content_response(result, file_path, repository_alias)
    except Exception as e:
        logger.exception(
            f"Unexpected error in get_file_content: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response(
            {"success": False, "error": str(e), "content": [], "metadata": {}}
        )


def _normalize_browse_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Extract, validate, and normalize browse_directory parameters."""
    limit = _coerce_int(params.get("limit"), _MAX_MCP_FILE_LIMIT)
    if limit < _MIN_LIMIT:
        limit = _MIN_LIMIT
    elif limit > _MAX_MCP_FILE_LIMIT:
        limit = _MAX_MCP_FILE_LIMIT

    sort_by = params.get("sort_by", "path")
    if sort_by not in _VALID_SORT_FIELDS:
        sort_by = "path"

    return {
        "repository_alias": params["repository_alias"],
        "path": params.get("path", ""),
        "recursive": params.get("recursive", True),
        "user_path_pattern": params.get("path_pattern"),
        "language": params.get("language"),
        "limit": limit,
        "sort_by": sort_by,
    }


def browse_directory(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Browse directory recursively."""
    from code_indexer.server.models.api_models import FileListQueryParams

    try:
        bp = _normalize_browse_params(params)
        repository_alias = bp["repository_alias"]

        is_global_repo = False
        if repository_alias and repository_alias.endswith("-global"):
            target_path, error_resp = _resolve_global_repo_target(
                repository_alias, user, {"structure": {}}
            )
            if error_resp is not None:
                return error_resp
            repository_alias = target_path
            is_global_repo = True

        final_path_pattern = _build_browse_path_pattern(
            bp["path"], bp["recursive"], bp["user_path_pattern"]
        )

        query_params = FileListQueryParams(
            page=_DEFAULT_PAGE,
            limit=bp["limit"],
            path_pattern=final_path_pattern,
            language=bp["language"],
            sort_by=bp["sort_by"],
        )

        if is_global_repo:
            result = _utils.app_module.file_service.list_files_by_path(
                repo_path=repository_alias,
                query_params=query_params,
            )
        else:
            result = _utils.app_module.file_service.list_files(
                repo_id=repository_alias,
                username=user.username,
                query_params=query_params,
            )

        serialized_files = _serialize_file_results(result)
        serialized_files = _filter_cidx_meta_files(
            serialized_files, repository_alias, user
        )

        path_normalized = (bp["path"].rstrip("/") if bp["path"] else "") or "/"
        structure = {
            "path": path_normalized,
            "files": serialized_files,
            "total": len(serialized_files),
        }

        return _mcp_response({"success": True, "structure": structure})
    except Exception as e:
        logger.exception(
            f"Unexpected error in browse_directory: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e), "structure": {}})


# ---------------------------------------------------------------------------
# CRUD shared helpers
# ---------------------------------------------------------------------------

# Exception type to (error_code, log_level, label) mapping for CRUD handlers.
# Used by _handle_crud_exception to produce consistent logging and responses.
_CRUD_EXCEPTION_MAP = {
    "create": {
        "FileExistsError": ("MCP-GENERAL-042", "warning", "file already exists"),
        "PermissionError": ("MCP-GENERAL-043", "warning", "permission denied"),
        "CRUDOperationError": ("MCP-GENERAL-044", "error", "CRUD operation error"),
        "ValueError": ("MCP-GENERAL-045", "warning", "invalid parameters"),
    },
    "edit": {
        "HashMismatchError": ("MCP-GENERAL-047", "warning", "hash mismatch"),
        "FileNotFoundError": ("MCP-GENERAL-048", "warning", "file not found"),
        "ValueError": ("MCP-GENERAL-049", "warning", "validation error"),
        "PermissionError": ("MCP-GENERAL-050", "warning", "permission denied"),
        "CRUDOperationError": ("MCP-GENERAL-051", "error", "CRUD operation error"),
    },
    "delete": {
        "HashMismatchError": ("MCP-GENERAL-053", "warning", "hash mismatch"),
        "FileNotFoundError": ("MCP-GENERAL-054", "warning", "file not found"),
        "PermissionError": ("MCP-GENERAL-055", "warning", "permission denied"),
        "CRUDOperationError": ("MCP-GENERAL-056", "error", "CRUD operation error"),
        "ValueError": ("MCP-GENERAL-057", "warning", "invalid parameters"),
    },
}


def _validate_crud_params(
    params: Dict[str, Any], require_content: bool = False
) -> Optional[Dict[str, Any]]:
    """Validate common CRUD params. Returns error response or None if valid."""
    if not params.get("repository_alias"):
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if not params.get("file_path"):
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: file_path"}
        )
    if require_content and params.get("content") is None:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: content"}
        )
    return None


def _handle_crud_exception(
    exc: Exception, operation: str, handler_name: str
) -> Dict[str, Any]:
    """Map a CRUD exception to an MCP error response with structured logging."""
    exc_type_name = type(exc).__name__
    exc_map = _CRUD_EXCEPTION_MAP.get(operation, {})
    mapping = exc_map.get(exc_type_name)

    if mapping:
        error_code, level, label = mapping
        log_msg = f"File {operation} failed - {label}: {exc}"
        log_fn = getattr(logger, level)
        log_fn(
            format_error_log(
                error_code,
                log_msg,
                extra={"correlation_id": get_correlation_id()},
            )
        )
    else:
        logger.exception(
            f"Unexpected error in {handler_name}: {exc}",
            extra={"correlation_id": get_correlation_id()},
        )
    return _mcp_response({"success": False, "error": str(exc)})


def handle_create_file(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Create new file in activated repository."""
    from code_indexer.server.services.file_crud_service import file_crud_service

    error_resp = _validate_crud_params(params, require_content=True)
    if error_resp is not None:
        return error_resp

    try:
        repository_alias = params["repository_alias"]
        file_path = params["file_path"]

        _start_auto_watch_if_needed(repository_alias, user, "MCP-GENERAL-041")

        result = file_crud_service.create_file(
            repo_alias=repository_alias,
            file_path=file_path,
            content=params["content"],
            username=user.username,
        )

        _invalidate_wiki_cache(repository_alias, file_path)
        return _mcp_response(result)
    except Exception as e:
        return _handle_crud_exception(e, "create", "handle_create_file")


def handle_edit_file(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Edit file using exact string replacement with optimistic locking."""
    from code_indexer.server.services.file_crud_service import file_crud_service

    error_resp = _validate_crud_params(params)
    if error_resp is not None:
        return error_resp

    if params.get("old_string") is None:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: old_string"}
        )
    if params.get("new_string") is None:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: new_string"}
        )
    if not params.get("content_hash"):
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: content_hash"}
        )

    try:
        repository_alias = params["repository_alias"]
        file_path = params["file_path"]

        _start_auto_watch_if_needed(repository_alias, user, "MCP-GENERAL-046")

        result = file_crud_service.edit_file(
            repo_alias=repository_alias,
            file_path=file_path,
            old_string=params["old_string"],
            new_string=params["new_string"],
            content_hash=params["content_hash"],
            replace_all=params.get("replace_all", False),
            username=user.username,
        )

        _invalidate_wiki_cache(repository_alias, file_path)
        return _mcp_response(result)
    except Exception as e:
        return _handle_crud_exception(e, "edit", "handle_edit_file")


def handle_delete_file(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Delete file from activated repository."""
    from code_indexer.server.services.file_crud_service import file_crud_service

    error_resp = _validate_crud_params(params)
    if error_resp is not None:
        return error_resp

    try:
        repository_alias = params["repository_alias"]
        file_path = params["file_path"]

        _start_auto_watch_if_needed(repository_alias, user, "MCP-GENERAL-052")

        result = file_crud_service.delete_file(
            repo_alias=repository_alias,
            file_path=file_path,
            content_hash=params.get("content_hash"),
            username=user.username,
        )

        _invalidate_wiki_cache(repository_alias, file_path)
        return _mcp_response(result)
    except Exception as e:
        return _handle_crud_exception(e, "delete", "handle_delete_file")


# ---------------------------------------------------------------------------
# Write-mode shared setup
# ---------------------------------------------------------------------------


def _prepare_write_mode_context(
    params: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    """Validate and prepare write-mode handler context.

    Returns (repo_alias, alias, noop_response). If noop_response is not None,
    the caller should return it directly (missing param or non-write-exception).
    """
    repo_alias = params.get("repo_alias")
    if not repo_alias:
        return (
            None,
            None,
            _mcp_response(
                {"success": False, "error": "Missing required parameter: repo_alias"}
            ),
        )

    from code_indexer.server.services.file_crud_service import file_crud_service

    if not file_crud_service.is_write_exception(repo_alias):
        return (
            repo_alias,
            None,
            _mcp_response(
                {
                    "success": True,
                    "message": f"no-op: '{repo_alias}' is not a write-exception repo",
                }
            ),
        )

    alias = _write_mode_strip_global(repo_alias)
    return repo_alias, alias, None


def handle_enter_write_mode(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Enter write mode for a write-exception repo (Story #231 C1)."""
    try:
        repo_alias, alias, noop = _prepare_write_mode_context(params)
        if noop is not None:
            return noop
        assert repo_alias is not None
        assert alias is not None

        refresh_scheduler = _get_app_refresh_scheduler()
        if refresh_scheduler is None:
            return _mcp_response(
                {"success": False, "error": "RefreshScheduler not available"}
            )

        acquired, owner = _write_mode_acquire_lock(refresh_scheduler, alias)
        if not acquired:
            return _mcp_response(
                {
                    "success": False,
                    "message": f"Write lock for '{alias}' is already held by '{owner}'",
                }
            )

        try:
            from code_indexer.server.services.file_crud_service import file_crud_service

            source_path = file_crud_service.get_write_exception_path(repo_alias)
            golden_repos_dir = Path(_get_golden_repos_dir())
            _write_mode_create_marker(golden_repos_dir, alias, str(source_path))
        except Exception:
            refresh_scheduler.release_write_lock(alias, owner_name="mcp_write_mode")
            raise

        logger.info(
            f"enter_write_mode: write mode active for '{repo_alias}', source={source_path}"
        )
        return _mcp_response(
            {"success": True, "alias": repo_alias, "source_path": str(source_path)}
        )
    except Exception as e:
        logger.exception(
            f"Unexpected error in handle_enter_write_mode: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_exit_write_mode(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Exit write mode for a write-exception repo (Story #231 C2).

    Best-effort wiki cache invalidation after refresh (Story #304 AC9).
    Wiki invalidation failure is non-critical and logged at debug level.
    """
    try:
        repo_alias, alias, noop = _prepare_write_mode_context(params)
        if noop is not None:
            return noop
        assert repo_alias is not None
        assert alias is not None

        golden_repos_dir = Path(_get_golden_repos_dir())
        marker_file = golden_repos_dir / ".write_mode" / f"{alias}.json"

        if not marker_file.exists():
            logger.warning(
                f"exit_write_mode: no marker for '{repo_alias}' — not in write mode"
            )
            return _mcp_response(
                {
                    "success": True,
                    "warning": f"Write mode was not active for '{repo_alias}'",
                    "message": "not in write mode — nothing to exit",
                }
            )

        refresh_scheduler = _get_app_refresh_scheduler()
        if refresh_scheduler is None:
            return _mcp_response(
                {"success": False, "error": "RefreshScheduler not available"}
            )

        logger.info(
            f"exit_write_mode: triggering synchronous refresh for '{repo_alias}'"
        )
        _write_mode_run_refresh(refresh_scheduler, repo_alias, golden_repos_dir, alias)
        logger.info(
            f"exit_write_mode: write mode exited for '{repo_alias}', refresh complete"
        )

        # Story #304 AC9: Best-effort wiki cache invalidation after write mode exit
        try:
            from code_indexer.server.wiki.wiki_cache_invalidator import (
                wiki_cache_invalidator,
            )

            wiki_cache_invalidator.invalidate_repo(repo_alias)
        except Exception as e:
            logger.debug(f"Wiki cache invalidation skipped for exit_write_mode: {e}")

        return _mcp_response(
            {
                "success": True,
                "message": f"Refresh complete, write mode exited for '{repo_alias}'",
            }
        )
    except Exception as e:
        logger.exception(
            f"Unexpected error in handle_exit_write_mode: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Directory tree handler (Story #557)
# ---------------------------------------------------------------------------


def handle_directory_tree(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for directory_tree tool - generate hierarchical tree view."""
    from code_indexer.global_repos.directory_explorer import DirectoryExplorerService

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        golden_repos_dir = _get_golden_repos_dir()
        repo_path = _get_legacy()._resolve_repo_path(repository_alias, golden_repos_dir)
        if repo_path is None:
            available_repos = _get_available_repos(user)
            return _mcp_response(
                _error_with_suggestions(
                    error_msg=f"Repository '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
            )

        service = DirectoryExplorerService(Path(repo_path))
        result = service.generate_tree(
            path=args.get("path"),
            max_depth=_coerce_int(args.get("max_depth"), _DEFAULT_TREE_DEPTH),
            max_files_per_dir=_coerce_int(
                args.get("max_files_per_dir"), _DEFAULT_MAX_FILES_PER_DIR
            ),
            include_patterns=args.get("include_patterns"),
            exclude_patterns=args.get("exclude_patterns"),
            show_stats=args.get("show_stats", False),
            include_hidden=args.get("include_hidden", False),
        )

        if repository_alias and "cidx-meta" in repository_alias:
            result = _filter_cidx_meta_tree(result, user)

        return _mcp_response(
            {
                "success": True,
                "tree_string": result.tree_string,
                "root": _tree_node_to_dict(result.root),
                "total_directories": result.total_directories,
                "total_files": result.total_files,
                "max_depth_reached": result.max_depth_reached,
                "root_path": result.root_path,
            }
        )
    except ValueError as e:
        logger.warning(
            f"Validation error in directory_tree: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Error in directory_tree: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def _register(registry: dict) -> None:
    """Register file operation handlers into the HANDLER_REGISTRY."""
    registry["list_files"] = list_files
    registry["get_file_content"] = get_file_content
    registry["browse_directory"] = browse_directory
    registry["create_file"] = handle_create_file
    registry["edit_file"] = handle_edit_file
    registry["delete_file"] = handle_delete_file
    registry["enter_write_mode"] = handle_enter_write_mode
    registry["exit_write_mode"] = handle_exit_write_mode
    registry["directory_tree"] = handle_directory_tree
