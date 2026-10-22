"""File operations — CRUD, directory browsing, write mode management.

Domain module for file operation handlers. Part of the handlers package
modularization (Story #496).
"""

from code_indexer.server.telemetry.correlation_bridge import (
    get_current_correlation_id as get_correlation_id,
)

import logging
from typing import Dict, Any, Optional, Tuple
from pathlib import Path

from code_indexer.server.auth.user_manager import User
from . import _utils
from code_indexer.server.services.cidx_meta_access.catalog_requests import (
    resolve_served_repository,
)
from code_indexer.server.services.cidx_meta_access.file_doors import (
    CatalogFileAccess,
    bound_handles,
    judge_file_door,
    served_tree_view,
)
from code_indexer.server.services.cidx_meta_access.universe_provider import (
    registry_of,
)
from code_indexer.server.services.cidx_meta_access.tree_projection import (
    UNSCANNED_LIMIT,
    catalog_tree_root,
    restrict_catalog_tree,
)
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.file_listing_paths import (
    normalize_listing_path,
    compute_direct_children_of,
    escape_gitwildmatch_literal,
    is_absolute_path_pattern,
)
from code_indexer.server.services.query_admission_gate import (
    check_query_admission,
    memory_pressure_mcp_payload,
)
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

    Bug #1683 (round 1): previously constructed a bare, unwired
    `ActivatedRepoManager()` here -- its constructor hardcodes
    `Path.home()/".cidx-server"/"data"` and ignores `CIDX_SERVER_DATA_DIR`,
    so in cluster mode (or any deployment overriding the data dir) it read
    from the WRONG per-node store, found nothing, and (via the caller's
    fallback) mis-triggered auto-watch/indexing on the wrong directory.
    Fixed to resolve the DI-wired singleton from app.state instead
    (`_utils._get_activated_repo_manager()`, the same Bug #1533 pattern
    already used in this handler package for the same manager -- see
    `mcp/handlers/search.py`).

    Bug #1683 (round 2): `get_activated_repo_path` is a bare
    `os.path.join` with no existence check, so a bad/typo'd/stale
    `repository_alias` (never activated, or deactivated by another
    cluster node) resolves to a non-existent path that is still handed to
    `auto_watch_manager.start_watch`. That path's `ConfigManager.
    create_with_backtrack` finds no config up-tree and defaults
    `codebase_dir` to `"."`, which resolves against the SERVER PROCESS's
    CWD -- silently watching/indexing the server's own project tree
    instead of failing loudly. Round 2 added a `Path.is_dir()` existence
    guard, but this was REJECTED in review on two independently-reproduced
    grounds: (1) an activated-repo directory that EXISTS but was never
    indexed (an orphan clone from a partially-failed activation, or a repo
    deactivated on another cluster node -- the real-world `admin/tstwiki`
    shape) sails straight through `is_dir()` and still lands on the CWD
    fallback; (2) the guard is self-defeating -- for a genuinely
    non-existent alias, the FIRST errant `start_watch` call (via
    `BackendFactory...get_vector_store_client()`, downstream of
    `DaemonWatchManager.start_watch`) materializes
    `<repo_path>/.code-indexer/index` as a side effect, so `is_dir()`
    returns True on every subsequent call and the guard permanently
    disables itself for that alias.

    Bug #1683 (round 3): replaced the filesystem probe with the
    established, registry/DB-backed authority already used at 10+ other
    sites in this handler package --
    `activated_repo_manager.user_has_activated_repo(username, alias)`
    (`activated_repo_manager.py`, Story #1039) -- which answers "is this a
    real, currently-activated repo for this user" rather than "does
    something exist on disk at the path we'd compute", and is therefore
    immune to on-disk residue left by any prior errant run. The bare
    `Path(repo_path).is_dir()` check is retained as defense-in-depth (it
    also covers the `is_write_exception` branch, which
    `user_has_activated_repo` does not), but no longer the primary guard
    for the activated-repo branch (Messi Rule 2: fail loud, never
    substitute a fallback location). The root-cause fix for EVERY caller
    of `AutoWatchManager.start_watch` (not just this one) lives in
    `services/auto_watch_manager.py` itself.
    """
    from code_indexer.server.services.file_crud_service import file_crud_service
    from code_indexer.server.services.auto_watch_manager import auto_watch_manager

    try:
        if file_crud_service.is_write_exception(repository_alias):
            repo_path = str(
                file_crud_service.get_write_exception_path(repository_alias)
            )
        else:
            activated_repo_manager = _utils._get_activated_repo_manager()
            if activated_repo_manager is None:
                raise RuntimeError(
                    "activated_repo_manager not initialized on app.state"
                )
            if not activated_repo_manager.user_has_activated_repo(
                user.username, repository_alias
            ):
                # Bug #1683 round 3; round 2's code 220 collided with 9
                # pre-existing list_pull_requests sites and used an
                # is_dir() check that a prior errant run's own side
                # effects could permanently satisfy.
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-223",
                        f"Skipping auto-watch for {repository_alias}: not a "
                        f"registered activated repo for {user.username} -- "
                        f"refusing an orphan/stale directory that a "
                        f"filesystem-existence check alone cannot detect "
                        f"(Bug #1683 round 3)",
                    )
                )
                return
            repo_path = activated_repo_manager.get_activated_repo_path(
                username=user.username, user_alias=repository_alias
            )
        if not Path(repo_path).is_dir():
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-223",
                    f"Skipping auto-watch for {repository_alias}: resolved "
                    f"path does not exist ({repo_path}) -- refusing to let "
                    f"ConfigManager backtracking fall back to the server "
                    f"CWD (Bug #1683 round 2/3)",
                )
            )
            return
        # Bug #1709: safe lazy probe -- avoids PEP-562 construction side effect.
        golden_repos_dir = getattr(
            getattr(_utils._lazy_module_attr_or_none("app"), "state", None),
            "golden_repos_dir",
            None,
        )
        if not _is_write_mode_active(repository_alias, golden_repos_dir):
            auto_watch_manager.start_watch(repo_path)
    except Exception as e:
        logger.warning(
            format_error_log(
                error_code,
                f"Failed to start auto-watch for {repository_alias}: {e}",
            )
        )


def _write_mode_refusal(repository_alias: str) -> Optional[Dict[str, Any]]:
    """An error payload unless a write-mode write still owns its lock: renews
    the lease by the marker's token; anything but confirmed ownership refuses."""
    from code_indexer.server.services.file_crud_service import file_crud_service

    from ._write_mode_marker import renew_write_mode_lease

    golden = _golden_dir_or_none()
    if not file_crud_service.is_write_exception(repository_alias) or not (
        _is_write_mode_active(repository_alias, golden)
    ):
        return None
    scheduler = _get_app_refresh_scheduler()
    alias = _write_mode_strip_global(repository_alias)
    if scheduler is not None and renew_write_mode_lease(scheduler, Path(golden), alias):
        return None
    return {
        "success": False,
        "error": (
            f"Write mode for '{repository_alias}' no longer holds its write lock; "
            f"call enter_write_mode('{repository_alias}') again. Nothing was written."
        ),
    }


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


def _write_mode_acquire_lock(scheduler: Any, alias: str) -> Tuple[bool, str, str]:
    """Acquire write lock; return (acquired, owner_if_held, owner_token)."""
    import uuid

    token, wlm = uuid.uuid4().hex, scheduler.write_lock_manager
    if wlm.acquire(alias, owner_name="mcp_write_mode", owner_token=token):
        return True, "", token
    return False, (wlm.get_lock_info(alias) or {}).get("owner", "unknown"), ""


def _write_mode_create_marker(
    golden_repos_dir: Path, alias: str, source_path: str, owner_token: str
) -> None:
    """Create the private write-mode marker carrying the lock's owner token."""
    from ._write_mode_marker import write_marker

    write_marker(golden_repos_dir, alias, source_path, owner_token)


def _write_mode_run_refresh(
    refresh_scheduler: Any, repo_alias: str, golden_repos_dir: Path, alias: str
) -> Dict[str, Any]:
    """Remove the marker, stop auto-watch, release the lock with the marker's
    token and refresh; returns the real outcome as the response payload."""
    from code_indexer.server.services.auto_watch_manager import auto_watch_manager

    from ._write_mode_marker import finish_write_mode

    def stop_watch(source_path: str) -> None:
        try:
            auto_watch_manager.stop_watch(source_path)
        except Exception as e:
            logger.warning(
                f"Failed to stop auto-watch for {source_path} before refresh: {e}",
                extra={"correlation_id": get_correlation_id()},
            )

    return finish_write_mode(
        refresh_scheduler, repo_alias, golden_repos_dir, alias, stop_watch
    )


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
    # Bug #1886 (R2): see _build_browse_path_pattern's identical comment.
    path = normalize_listing_path(path)
    if path:
        # Bug #1886 (R4): escape gitwildmatch metacharacters in the LITERAL
        # path component (e.g. a Next.js dynamic-route dir "app/[slug]")
        # before splicing it into the pattern -- `user_path_pattern` is a
        # caller-supplied pattern by contract and is never auto-escaped.
        escaped_path = escape_gitwildmatch_literal(path)
        if user_path_pattern:
            if recursive:
                return f"{escaped_path}/**/{user_path_pattern}"
            return f"{escaped_path}/{user_path_pattern}"
        return f"{escaped_path}/**/*" if recursive else f"{escaped_path}/*"
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


def _activations() -> Any:
    return _utils._lazy_module_attr_or_none("activated_repo_manager")


def _judge(alias: Any, served_path: Optional[str], user: User) -> CatalogFileAccess:
    """The request's catalog judgement for the directory actually served
    (an unconfigured golden directory restricts a restricted caller)."""
    return judge_file_door(
        alias,
        served_path,
        user.username,
        _get_access_filtering_service(),
        _activations(),
        _golden_dir_or_none(),
        registry_of(_utils.app_module.app.state),
        may_write=user.has_permission("repository:write"),
    )


def _golden_dir_or_none() -> Optional[str]:
    try:
        return _get_golden_repos_dir()
    except RuntimeError:
        return None


def _promote_bare_alias(repository_alias: Any, user: User) -> Any:
    """Story #1039: the ``-global`` form of a bare alias the caller has not
    activated when its golden repository is globally active, else as given."""
    if not isinstance(repository_alias, str) or repository_alias.endswith("-global"):
        return repository_alias
    # Bug #1709: lazy probes never construct the process-wide app singletons.
    arm = _utils._lazy_module_attr_or_none("activated_repo_manager")
    grm = _utils._lazy_module_attr_or_none("golden_repo_manager")
    if arm is None or grm is None:
        return repository_alias
    if arm.user_has_activated_repo(user.username, repository_alias):
        return repository_alias
    from ._global_fallback import try_global_fallback

    promoted = try_global_fallback(repository_alias, grm)
    if promoted is None:
        return repository_alias
    logger.info(
        "bare-alias fallback: %r -> %r for user %r",
        repository_alias,
        promoted,
        user.username,
    )
    return promoted


def _activation_dir(alias: str, user: User) -> Optional[str]:
    """The caller's activation directory for *alias*, from the activation
    manager the file service serves it with (None without one)."""
    arm = _utils.app_module.file_service.activated_repo_manager
    if arm is None:
        return None
    return str(arm.get_activated_repo_path(user.username, alias))


def _build_browse_path_pattern(
    path: str, recursive: bool, user_path_pattern: Optional[str]
) -> Optional[str]:
    """Build path pattern for browse_directory from path + user pattern.

    Differs from _build_path_pattern in handling absolute patterns.
    """
    # Bug #1886 (R2): normalize leading/trailing "/" and a leading "./" so
    # a request like path="/src" or path="./src/" builds the same pattern
    # as path="src" -- pathspec's gitwildmatch does not treat a leading
    # "./" as a no-op (it silently fails to match).
    path = normalize_listing_path(path)

    is_absolute_pattern = is_absolute_path_pattern(user_path_pattern)

    if path:
        # Bug #1886 (R4): escape gitwildmatch metacharacters in the LITERAL
        # path component (e.g. a Next.js dynamic-route dir "app/[slug]")
        # before splicing it into the pattern -- `user_path_pattern` is a
        # caller-supplied pattern by contract and is never auto-escaped.
        escaped_path = escape_gitwildmatch_literal(path)
        if user_path_pattern:
            if is_absolute_pattern:
                return user_path_pattern
            if recursive:
                return f"{escaped_path}/**/{user_path_pattern}"
            return f"{escaped_path}/{user_path_pattern}"
        return f"{escaped_path}/**/*" if recursive else f"{escaped_path}/*"
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
                "file_content": [],
                "metadata": {},
            }
        ), None

    if limit is not None and limit < _MIN_LIMIT:
        return _mcp_response(
            {
                "success": False,
                "error": "limit must be an integer >= 1",
                "file_content": [],
                "metadata": {},
            }
        ), None

    return offset, limit


def _read_chunk(
    content: str,
    offset: int,
    total_lines: int,
    max_chars: int,
) -> tuple:
    """Select whole lines from content up to the character budget (Bug #1080).

    Args:
        content: Full text content starting at `offset` (may be many lines).
        offset: 1-indexed line number of the first line in `content`.
        total_lines: Total number of lines in the original file.
        max_chars: Maximum character budget for the returned chunk.

    Returns:
        (chunk_text, returned_lines, has_more, next_offset, truncated)

    Design rules:
    - Never byte-cut mid-line: the budget BREAK fires only when len(selected) > 0.
    - Pathological single unit > budget: emit it whole (one-response budget overrun).
    - next_offset strictly advances so pagination always terminates.
    """
    # Split on \n ONLY — matches f.readlines() / content.count("\n") semantics used by
    # FileListingService to compute total_lines.  str.splitlines() also splits on \x0c,
    # \v, \x1c–\x1e, \x85,  ,  , etc., yielding more "lines" than the service
    # reports, which breaks returned_lines > total_lines invariant (Bug #1080).
    _parts = content.split("\n")
    all_lines = [p + "\n" for p in _parts[:-1]]
    if _parts[-1]:
        all_lines.append(_parts[-1])
    if not all_lines:
        return "", 0, False, None, False

    selected: list[str] = []
    char_count = 0
    for line in all_lines:
        # Honor the budget, but always emit at least one line (termination guarantee).
        if char_count + len(line) > max_chars and len(selected) > 0:
            break
        selected.append(line)
        char_count += len(line)

    returned_lines = len(selected)
    last_line = offset + returned_lines - 1
    has_more = last_line < total_lines
    next_offset = offset + returned_lines if has_more else None
    chunk_text = "".join(selected)
    truncated = has_more  # truncated iff there are more lines beyond what was returned
    return chunk_text, returned_lines, has_more, next_offset, truncated


def _build_file_content_response(
    result: dict, file_path: str, repository_alias: str
) -> Dict[str, Any]:
    """Build the MCP response for get_file_content.

    Bug #1080 fix: replaces TruncationHelper byte-cut with line-aware read_chunk.
    The domain unit (file lines) is the single source of truth for pagination.
    cache_handle / total_pages are retired for this tool (line-offset pagination
    is self-contained; no opaque byte-envelope needed).
    """
    file_content = result.get("content", "")
    metadata = result.get("metadata", {})

    config_service = get_config_service()
    content_limits = config_service.get_config().content_limits_config

    # Apply line-aware chunk selection when we have a token budget configured.
    if file_content and content_limits is not None:
        max_chars = int(
            int(content_limits.file_content_max_tokens)
            * int(content_limits.chars_per_token)
        )
        offset = int(metadata.get("offset") or 1)
        total_lines = int(metadata.get("total_lines") or 0)
        estimated_tokens_before = len(file_content) // int(
            content_limits.chars_per_token
        )

        file_content, returned_lines, has_more, next_offset, truncated = _read_chunk(
            content=file_content,
            offset=offset,
            total_lines=total_lines,
            max_chars=max_chars,
        )

        # Recompute metadata from what was ACTUALLY returned.
        estimated_tokens = len(file_content) // int(content_limits.chars_per_token)
        metadata["returned_lines"] = returned_lines
        metadata["has_more"] = has_more
        metadata["next_offset"] = next_offset
        metadata["truncated"] = truncated
        metadata["truncated_at_line"] = (
            (offset + returned_lines - 1) if truncated else None
        )
        metadata["estimated_tokens"] = estimated_tokens
        metadata["requires_pagination"] = has_more
        metadata["pagination_hint"] = (
            f"More content available. Use offset={next_offset} to continue reading."
            if has_more
            else None
        )
        total_tokens = estimated_tokens_before
        preview_tokens = estimated_tokens
    else:
        total_tokens = 0
        preview_tokens = 0
        has_more = metadata.get("has_more", False)
        truncated = metadata.get("truncated", False)

    # cache_handle and total_pages are retired for get_file_content (Bug #1080):
    # pagination is purely line-offset based; no byte-envelope needed.
    cache_handle = None
    total_pages = 0

    content_blocks = [{"type": "text", "text": file_content}] if file_content else []

    metadata["cache_handle"] = cache_handle
    metadata["total_tokens"] = total_tokens
    metadata["preview_tokens"] = preview_tokens
    metadata["total_pages"] = total_pages

    wiki_enabled_repos = _get_wiki_enabled_repos()
    _enrich_with_wiki_url(metadata, file_path, repository_alias, wiki_enabled_repos)

    return _mcp_response(
        {
            "success": True,
            "file_content": content_blocks,
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
    _admission = check_query_admission()
    if not _admission.allowed:
        return _mcp_response(memory_pressure_mcp_payload(_admission))

    from code_indexer.server.models.api_models import FileListQueryParams

    try:
        repository_alias = params["repository_alias"]
        repository_alias = _parse_json_string_array(repository_alias)
        params["repository_alias"] = repository_alias

        if isinstance(repository_alias, list):
            return _omni_list_files(params, user)

        repository_alias = _promote_bare_alias(repository_alias, user)
        params["repository_alias"] = repository_alias

        path = params.get("path", "")
        recursive = params.get("recursive", True)
        user_path_pattern = params.get("path_pattern")

        normalized_list_path = normalize_listing_path(path)
        final_path_pattern = _build_path_pattern(path, recursive, user_path_pattern)

        # Bug #1886 (R3): recursive=False is documented ("direct children
        # only") but was never enforced -- same fix as browse_directory's
        # R1/R2, sharing the identical helper.
        #
        # Bug #1886 (R4): list_files NEVER overrides a non-empty `path`
        # with an absolute user pattern (path_pattern is always relative to
        # path, per its own tool doc -- see the round-3 review note); the
        # built pattern only stands alone, verbatim, when `path` itself is
        # empty and a pattern was supplied. That is the ONLY scenario in
        # which final_path_pattern is safe to re-parse for a depth base --
        # every other case was built by escaping+prefixing normalized_path,
        # so the base must come from normalized_path directly.
        pattern_overrides_path = bool(user_path_pattern) and not normalized_list_path
        direct_children_of = compute_direct_children_of(
            normalized_list_path, recursive, final_path_pattern, pattern_overrides_path
        )

        query_params = FileListQueryParams(
            page=_DEFAULT_PAGE,
            limit=_MAX_MCP_FILE_LIMIT,
            path_pattern=final_path_pattern,
            direct_children_of=direct_children_of,
        )

        if repository_alias and repository_alias.endswith("-global"):
            target_path, error_resp = _resolve_global_repo_target(
                repository_alias, user, {"files": []}
            )
            if error_resp is not None:
                return error_resp

            access = _judge(repository_alias, target_path, user)
            with bound_handles(access):
                result = _utils.app_module.file_service.list_files_by_path(
                    repo_path=target_path, query_params=query_params, access=access
                )
        else:
            access = _judge(
                repository_alias, _activation_dir(repository_alias, user), user
            )
            with bound_handles(access):
                result = _utils.app_module.file_service.list_files(
                    repo_id=repository_alias,
                    username=user.username,
                    query_params=query_params,
                    access=access,
                )

        serialized_files = _serialize_file_results(result)
        return _mcp_response({"success": True, "files": serialized_files})
    except Exception as e:
        logger.exception(
            f"Unexpected error in list_files: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e), "files": []})


def get_file_content(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get content of a specific file with optional pagination."""
    _admission = check_query_admission()
    if not _admission.allowed:
        return _mcp_response(memory_pressure_mcp_payload(_admission))

    try:
        repository_alias = _promote_bare_alias(params["repository_alias"], user)
        params["repository_alias"] = repository_alias
        file_path = params["file_path"]

        pagination = _parse_pagination_params(params)
        if isinstance(pagination[0], dict):
            return pagination[0]  # Early-return error response
        offset, limit = pagination

        if repository_alias and repository_alias.endswith("-global"):
            target_path, error_resp = _resolve_global_repo_target(
                repository_alias, user, {"file_content": [], "metadata": {}}
            )
            if error_resp is not None:
                return error_resp

            access = _judge(repository_alias, target_path, user)
            with bound_handles(access):
                result = _utils.app_module.file_service.get_file_content_by_path(
                    repo_path=target_path,
                    file_path=file_path,
                    offset=offset,
                    limit=limit,
                    skip_truncation=True,
                    access=access,
                )
        else:
            try:
                access = _judge(
                    repository_alias, _activation_dir(repository_alias, user), user
                )
                with bound_handles(access):
                    result = _utils.app_module.file_service.get_file_content(
                        repository_alias=repository_alias,
                        file_path=file_path,
                        username=user.username,
                        offset=offset,
                        limit=limit,
                        skip_truncation=True,
                        access=access,
                    )
            except FileNotFoundError as not_found:
                # Story #1039 recovery: the user's own activation of this bare
                # alias could not be resolved (e.g. a stale activation record
                # whose on-disk directory is gone).  If the golden repo is
                # globally active, recover via the ``-global`` form -- matching
                # search_code's bare-alias fallback.  Otherwise surface a clean
                # not-found (handled by the `except FileNotFoundError` below);
                # an EXPECTED not-found must never escalate to an unhandled
                # "Unexpected error" traceback (anti-silent-failure #13).
                recovered = _recover_file_content_via_global(
                    repository_alias=repository_alias,
                    file_path=file_path,
                    user=user,
                    offset=offset,
                    limit=limit,
                )
                if recovered is None:
                    raise not_found
                result, repository_alias = recovered

        return _build_file_content_response(result, file_path, repository_alias)
    except FileNotFoundError as not_found:
        # Expected condition -- clean MCP error, NO traceback / ERROR escalation.
        return _mcp_response(
            {
                "success": False,
                "error": str(not_found),
                "file_content": [],
                "metadata": {},
            }
        )
    except PermissionError as denied:
        # Bug #1891 D3: a caller-triggerable path-confinement escape
        # attempt (resolve_confined_path()) is an EXPECTED security
        # rejection, not a server error -- same shape as the
        # FileNotFoundError branch above, but logged at INFO with no
        # traceback so it cannot trip the E2E Phase 3/4 log-audit gate
        # (tests/e2e/log_audit_gate.py), which fails a phase on any new
        # ERROR/WARNING log entry. No caller-supplied path is logged.
        logger.info(
            "get_file_content: access denied (path confinement)",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response(
            {
                "success": False,
                "error": str(denied),
                "file_content": [],
                "metadata": {},
            }
        )
    except Exception as e:
        logger.exception(
            f"Unexpected error in get_file_content: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response(
            {"success": False, "error": str(e), "file_content": [], "metadata": {}}
        )


def _recover_file_content_via_global(
    repository_alias: str,
    file_path: str,
    user: User,
    offset: Optional[int],
    limit: Optional[int],
) -> Optional[Tuple[dict, str]]:
    """Recover file content via the ``-global`` form of a bare alias.

    Used when the user's own activation of *repository_alias* could not be
    resolved (Story #1039).  Returns ``(result, "<alias>-global")`` when the
    bare alias is globally active and its versioned path resolves, or ``None``
    when no global recovery is possible (caller then surfaces a clean
    not-found).  NEVER applied to an already ``-global`` alias.
    """
    if not isinstance(repository_alias, str) or repository_alias.endswith("-global"):
        return None

    # Bug #1709: safe lazy probe -- avoids PEP-562 construction side effect.
    _grm = _utils._lazy_module_attr_or_none("golden_repo_manager")
    if _grm is None:
        return None

    from ._global_fallback import try_global_fallback

    promoted = try_global_fallback(repository_alias, _grm)
    if promoted is None:
        return None

    target_path, error_resp = _resolve_global_repo_target(
        promoted, user, {"file_content": [], "metadata": {}}
    )
    if error_resp is not None or target_path is None:
        return None

    logger.info(
        "bare-alias recovery: %r -> %r for user %r (own activation unresolved)",
        repository_alias,
        promoted,
        user.username,
    )
    access = _judge(promoted, target_path, user)
    with bound_handles(access):
        result = _utils.app_module.file_service.get_file_content_by_path(
            repo_path=target_path,
            file_path=file_path,
            offset=offset,
            limit=limit,
            skip_truncation=True,
            access=access,
        )
    return result, promoted


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
    _admission = check_query_admission()
    if not _admission.allowed:
        return _mcp_response(memory_pressure_mcp_payload(_admission))

    from code_indexer.server.models.api_models import FileListQueryParams

    try:
        bp = _normalize_browse_params(params)
        repository_alias = bp["repository_alias"]

        repository_alias = _promote_bare_alias(repository_alias, user)
        params["repository_alias"] = bp["repository_alias"] = repository_alias

        target_path: Optional[str] = None
        if repository_alias and repository_alias.endswith("-global"):
            target_path, error_resp = _resolve_global_repo_target(
                repository_alias, user, {"structure": {}}
            )
            if error_resp is not None:
                return error_resp

        final_path_pattern = _build_browse_path_pattern(
            bp["path"], bp["recursive"], bp["user_path_pattern"]
        )

        # Bug #1886: recursive=False means "immediate children only" -- the
        # glob pattern built above cannot express that on its own (gitignore-
        # style matching doesn't anchor a bare "*" at "/"), so pass an
        # explicit depth restriction that FileListingService applies BEFORE
        # limit truncation. recursive=True (default) passes None: no
        # restriction, byte-for-byte unchanged behavior.
        #
        # R1 (round 2): when user_path_pattern is "absolute" (overrides
        # `path` entirely -- see _build_browse_path_pattern above), the
        # depth base must come from final_path_pattern's own literal
        # directory prefix, not from `path` (which may be irrelevant or
        # simply wrong, e.g. path="wrong/path" + an absolute pattern).
        #
        # Bug #1886 (R4): `pattern_overrides_path` is the explicit signal
        # for that override case -- an absolute user pattern, OR `path`
        # itself being empty (the pattern then stands alone, verbatim).
        # Every other pattern was built by escaping+prefixing
        # normalized_browse_path (escape_gitwildmatch_literal), so
        # re-parsing it for a depth base would either see an escaped
        # metachar as "still a glob" (wrongly disabling the restriction) or
        # yield a base that never equals a real file's unescaped dirname.
        normalized_browse_path = normalize_listing_path(bp["path"])
        user_path_pattern = bp["user_path_pattern"]
        pattern_overrides_path = bool(user_path_pattern) and (
            is_absolute_path_pattern(user_path_pattern) or not normalized_browse_path
        )
        direct_children_of = compute_direct_children_of(
            normalized_browse_path,
            bp["recursive"],
            final_path_pattern,
            pattern_overrides_path,
        )

        query_params = FileListQueryParams(
            page=_DEFAULT_PAGE,
            limit=bp["limit"],
            path_pattern=final_path_pattern,
            language=bp["language"],
            sort_by=bp["sort_by"],
            direct_children_of=direct_children_of,
        )

        access = _judge(
            repository_alias,
            target_path or _activation_dir(repository_alias, user),
            user,
        )
        with bound_handles(access):
            if target_path is not None:
                result = _utils.app_module.file_service.list_files_by_path(
                    repo_path=target_path, query_params=query_params, access=access
                )
            else:
                result = _utils.app_module.file_service.list_files(
                    repo_id=repository_alias,
                    username=user.username,
                    query_params=query_params,
                    access=access,
                )

        serialized_files = _serialize_file_results(result)

        path_normalized = normalized_browse_path or "/"
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

        if (refusal := _write_mode_refusal(repository_alias)) is not None:
            return _mcp_response(refusal)
        _start_auto_watch_if_needed(repository_alias, user, "MCP-GENERAL-041")

        result = file_crud_service.create_file(
            repo_alias=repository_alias,
            file_path=file_path,
            content=params["content"],
            username=user.username,
            access_for=lambda served: _judge(repository_alias, served, user),
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

        if (refusal := _write_mode_refusal(repository_alias)) is not None:
            return _mcp_response(refusal)
        _start_auto_watch_if_needed(repository_alias, user, "MCP-GENERAL-046")

        result = file_crud_service.edit_file(
            repo_alias=repository_alias,
            file_path=file_path,
            old_string=params["old_string"],
            new_string=params["new_string"],
            content_hash=params["content_hash"],
            replace_all=params.get("replace_all", False),
            username=user.username,
            access_for=lambda served: _judge(repository_alias, served, user),
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

        if (refusal := _write_mode_refusal(repository_alias)) is not None:
            return _mcp_response(refusal)
        _start_auto_watch_if_needed(repository_alias, user, "MCP-GENERAL-052")

        result = file_crud_service.delete_file(
            repo_alias=repository_alias,
            file_path=file_path,
            content_hash=params.get("content_hash"),
            username=user.username,
            access_for=lambda served: _judge(repository_alias, served, user),
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

        acquired, owner, token = _write_mode_acquire_lock(refresh_scheduler, alias)
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
            _write_mode_create_marker(golden_repos_dir, alias, str(source_path), token)
        except Exception:
            refresh_scheduler.release_write_lock(
                alias, owner_name="mcp_write_mode", owner_token=token
            )
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
        outcome = _write_mode_run_refresh(
            refresh_scheduler, repo_alias, golden_repos_dir, alias
        )
        if not outcome["success"]:
            logger.warning(f"exit_write_mode for '{repo_alias}': {outcome['error']}")
            return _mcp_response(outcome)
        logger.info(f"exit_write_mode: write mode exited for '{repo_alias}'")

        # Story #304 AC9: Best-effort wiki cache invalidation after write mode exit
        try:
            from code_indexer.server.wiki.wiki_cache_invalidator import (
                wiki_cache_invalidator,
            )

            wiki_cache_invalidator.invalidate_repo(repo_alias)
        except Exception as e:
            logger.debug(f"Wiki cache invalidation skipped for exit_write_mode: {e}")
        return _mcp_response(outcome)
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
    _admission = check_query_admission()
    if not _admission.allowed:
        return _mcp_response(memory_pressure_mcp_payload(_admission))

    from code_indexer.global_repos.directory_explorer import DirectoryExplorerService

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    access_service = _get_access_filtering_service()
    grm = _utils._lazy_module_attr_or_none("golden_repo_manager")
    try:
        from ._global_fallback import try_global_fallback

        golden_repos_dir = _get_golden_repos_dir()
        # Story #1039: bare-to-global promotion only without an activation.
        served = resolve_served_repository(
            repository_alias,
            user.username,
            access_service,
            _activations(),
            lambda alias: _get_legacy()._resolve_repo_path(alias, golden_repos_dir),
            lambda alias: try_global_fallback(alias, grm) if grm else None,
        )
        if served.path is None:
            return _mcp_response(
                _error_with_suggestions(
                    error_msg=f"Repository '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=_get_available_repos(user),
                )
            )
        args["repository_alias"] = served.alias
        files_per_dir = _coerce_int(
            args.get("max_files_per_dir"), _DEFAULT_MAX_FILES_PER_DIR
        )
        view = served_tree_view(
            served.visibility,
            served.path,
            user.username,
            access_service,
            _golden_dir_or_none(),
            registry_of(_utils.app_module.app.state),
            composite_activation=served.composite,
        )
        visibility = view.vis if view is not None else None
        base = args.get("path") or ""
        if view is not None:
            base = catalog_tree_root(view, base)
        result = DirectoryExplorerService(Path(served.path)).generate_tree(
            path=base or None,
            max_depth=_coerce_int(args.get("max_depth"), _DEFAULT_TREE_DEPTH),
            max_files_per_dir=UNSCANNED_LIMIT if visibility else files_per_dir,
            include_patterns=args.get("include_patterns"),
            exclude_patterns=args.get("exclude_patterns"),
            show_stats=args.get("show_stats", False),
            include_hidden=args.get("include_hidden", False),
        )
        if visibility is not None:
            result = restrict_catalog_tree(
                result,
                base,
                visibility,
                access_service,
                bool(args.get("show_stats")),
                files_per_dir,
                view.listable if view is not None else None,
            )

        return _mcp_response(
            {
                "success": True,
                "tree_string": result.tree_string,
                "root": _tree_node_to_dict(result.root),
                "total_directories": result.total_directories,
                "total_files": result.total_files,
                "max_depth_reached": result.max_depth_reached,
                "root_path": args.get("path") or "",
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
