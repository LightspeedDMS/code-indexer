"""xray_search MCP handler.

Thin shim: validate inputs, pre-flight check evaluator, submit background job.
Heavy lifting lives in XRaySearchEngine (src/code_indexer/xray/search_engine.py).

Story #972: synchronous single-threaded XRaySearchEngine baseline.
Story #978: will add ThreadPoolExecutor parallelism and job-level timeout.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional, cast

from code_indexer.server.auth.user_manager import User
from code_indexer.xray.search_engine import XRaySearchEngine

from . import _utils
from ._utils import _mcp_response

logger = logging.getLogger(__name__)

# Timeout range enforced by the handler (seconds).
_TIMEOUT_MIN = 10
_TIMEOUT_MAX = 600

# Default timeout when the caller omits timeout_seconds.
_DEFAULT_TIMEOUT_SECONDS = 120

# await_seconds range and poll interval.
_AWAIT_SECONDS_MIN = 0
_AWAIT_SECONDS_MAX = 30
_AWAIT_POLL_INTERVAL = 0.05


def _resolve_repo_path(alias: str) -> Optional[str]:
    """Resolve a global repo alias to its versioned snapshot path.

    Delegates to repos._resolve_golden_repo_path so the alias manager is
    exercised through the canonical code path.

    Returns None when the alias is unknown.
    """
    from code_indexer.server.mcp.handlers.repos import _resolve_golden_repo_path

    return cast(Optional[str], _resolve_golden_repo_path(alias))


def _get_background_job_manager():
    """Return the live BackgroundJobManager from the app module.

    Extracted for easy mocking in unit tests.
    """
    return _utils.app_module.background_job_manager


def _await_job_result(
    bjm: Any, job_id: str, username: str, await_seconds: int
) -> Optional[Dict[str, Any]]:
    """Poll BackgroundJobManager until the job completes or the window expires.

    Args:
        bjm: BackgroundJobManager instance.
        job_id: The job to poll.
        username: Username of the submitter (required by get_job_status).
        await_seconds: Maximum seconds to poll.

    Returns:
        The job ``result`` dict when job reaches ``completed`` status within
        the window, or ``None`` if the window expires first.
    """
    deadline = time.monotonic() + await_seconds
    while time.monotonic() < deadline:
        status = bjm.get_job_status(job_id, username)
        if status is not None and status.get("status") == "completed":
            return cast(Optional[Dict[str, Any]], status.get("result"))
        time.sleep(_AWAIT_POLL_INTERVAL)
    return None


def handle_xray_search(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """MCP handler for the xray_search tool.

    1. Auth + permission check (query_repos).
    2. Parameter parse + validation.
    3. Repository alias resolution.
    4. Pre-flight evaluator validation via PythonEvaluatorSandbox.
    5. Job submission via BackgroundJobManager.
    6. Return {job_id}.

    Error codes:
        auth_required           — unauthenticated or missing query_repos.
        invalid_search_target   — search_target not 'content' or 'filename'.
        timeout_out_of_range    — timeout_seconds outside [10, 600].
        max_files_out_of_range  — max_files provided but < 1.
        repository_not_found    — alias cannot be resolved.
        xray_extras_not_installed — tree-sitter extras not available.
        xray_evaluator_validation_failed — evaluator AST whitelist violation.
    """
    # ------------------------------------------------------------------
    # 1. Auth + permission check
    # ------------------------------------------------------------------
    if user is None or not user.has_permission("query_repos"):
        return _mcp_response({"error": "auth_required"})

    # ------------------------------------------------------------------
    # 2. Parameter parse + validation
    # ------------------------------------------------------------------
    repo_alias: str = params.get("repository_alias", "")
    driver_regex: str = params.get("driver_regex", "")
    evaluator_code: str = params.get("evaluator_code", "")
    search_target: str = params.get("search_target", "")
    include_patterns = params.get("include_patterns") or []
    exclude_patterns = params.get("exclude_patterns") or []
    timeout_override = params.get("timeout_seconds")
    max_files = params.get("max_files")
    await_seconds_raw = params.get("await_seconds", 0)

    # await_seconds must be a plain integer in [0, 30].
    if not isinstance(await_seconds_raw, int) or isinstance(await_seconds_raw, bool):
        return _mcp_response(
            {
                "error": "await_seconds_invalid",
                "message": (
                    f"await_seconds must be an integer in "
                    f"[{_AWAIT_SECONDS_MIN}, {_AWAIT_SECONDS_MAX}], "
                    f"got {await_seconds_raw!r}"
                ),
            }
        )
    await_seconds: int = await_seconds_raw
    if not (_AWAIT_SECONDS_MIN <= await_seconds <= _AWAIT_SECONDS_MAX):
        return _mcp_response(
            {
                "error": "await_seconds_invalid",
                "message": (
                    f"await_seconds must be in "
                    f"[{_AWAIT_SECONDS_MIN}, {_AWAIT_SECONDS_MAX}], "
                    f"got {await_seconds}"
                ),
            }
        )

    if search_target not in ("content", "filename"):
        return _mcp_response(
            {
                "error": "invalid_search_target",
                "message": (
                    f"search_target must be 'content' or 'filename', got {search_target!r}"
                ),
            }
        )

    if max_files is not None and max_files < 1:
        return _mcp_response(
            {
                "error": "max_files_out_of_range",
                "message": "max_files must be >= 1 when provided",
            }
        )

    # ------------------------------------------------------------------
    # 3. Repository alias resolution
    # ------------------------------------------------------------------
    repo_path_str = _resolve_repo_path(repo_alias)
    if repo_path_str is None:
        return _mcp_response(
            {
                "error": "repository_not_found",
                "message": f"Repository alias {repo_alias!r} not found",
            }
        )

    # ------------------------------------------------------------------
    # 4. Effective timeout + range check
    # ------------------------------------------------------------------
    effective_timeout: int = (
        timeout_override if timeout_override is not None else _DEFAULT_TIMEOUT_SECONDS
    )
    if not (_TIMEOUT_MIN <= effective_timeout <= _TIMEOUT_MAX):
        return _mcp_response(
            {
                "error": "timeout_out_of_range",
                "message": (
                    f"timeout_seconds must be between {_TIMEOUT_MIN} and "
                    f"{_TIMEOUT_MAX}, got {effective_timeout}"
                ),
            }
        )

    # ------------------------------------------------------------------
    # 5. Pre-flight evaluator validation
    # ------------------------------------------------------------------
    engine = XRaySearchEngine()

    validation = engine.sandbox.validate(evaluator_code)
    if not validation.ok:
        return _mcp_response(
            {
                "error": "xray_evaluator_validation_failed",
                "message": validation.reason,
            }
        )

    # ------------------------------------------------------------------
    # 6. Submit background job
    # ------------------------------------------------------------------
    repo_path = Path(repo_path_str)

    def job_fn(progress_callback):  # type: ignore[no-untyped-def]
        from code_indexer.xray.search_engine import XRaySearchEngine as _Engine

        result = _Engine().run(
            repo_path=repo_path,
            driver_regex=driver_regex,
            evaluator_code=evaluator_code,
            search_target=search_target,
            include_patterns=list(include_patterns),
            exclude_patterns=list(exclude_patterns),
            timeout_seconds=effective_timeout,
            progress_callback=progress_callback,
            max_files=max_files,
        )
        return _truncate_xray_result(result)

    bjm = _get_background_job_manager()
    job_id: str = bjm.submit_job(
        operation_type="xray_search",
        func=job_fn,
        submitter_username=user.username,
        repo_alias=repo_alias,
    )

    if await_seconds > 0:
        inline = _await_job_result(bjm, job_id, user.username, await_seconds)
        if inline is not None:
            return _mcp_response(inline)

    return _mcp_response({"job_id": job_id})


# Default and range constants for max_debug_nodes (xray_explore).
_MAX_DEBUG_NODES_DEFAULT = 50
_MAX_DEBUG_NODES_MIN = 1
_MAX_DEBUG_NODES_MAX = 500


def handle_xray_explore(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """MCP handler for the xray_explore tool.

    Identical to handle_xray_search but additionally:
    - Validates max_debug_nodes (range 1..500, default 50).
    - Passes include_ast_debug=True and max_debug_nodes to XRaySearchEngine.run().

    Error codes:
        auth_required                  — unauthenticated or missing query_repos.
        invalid_search_target          — search_target not 'content' or 'filename'.
        timeout_out_of_range           — timeout_seconds outside [10, 600].
        max_files_out_of_range         — max_files provided but < 1.
        max_debug_nodes_out_of_range   — max_debug_nodes outside [1, 500].
        repository_not_found           — alias cannot be resolved.
        xray_extras_not_installed      — tree-sitter extras not available.
        xray_evaluator_validation_failed — evaluator AST whitelist violation.
    """
    # ------------------------------------------------------------------
    # 1. Auth + permission check
    # ------------------------------------------------------------------
    if user is None or not user.has_permission("query_repos"):
        return _mcp_response({"error": "auth_required"})

    # ------------------------------------------------------------------
    # 2. Parameter parse + validation
    # ------------------------------------------------------------------
    repo_alias: str = params.get("repository_alias", "")
    driver_regex: str = params.get("driver_regex", "")
    # evaluator_code is optional for xray_explore; default 'return True' accepts all
    # candidate files for AST exploration without requiring the caller to supply a filter.
    raw_evaluator_code: str = params.get("evaluator_code") or ""
    evaluator_code: str = (
        raw_evaluator_code if raw_evaluator_code.strip() else "return True"
    )
    search_target: str = params.get("search_target", "")
    include_patterns = params.get("include_patterns") or []
    exclude_patterns = params.get("exclude_patterns") or []
    timeout_override = params.get("timeout_seconds")
    max_files = params.get("max_files")
    max_debug_nodes = params.get("max_debug_nodes", _MAX_DEBUG_NODES_DEFAULT)
    await_seconds_raw = params.get("await_seconds", 0)

    # await_seconds must be a plain integer in [0, 30].
    if not isinstance(await_seconds_raw, int) or isinstance(await_seconds_raw, bool):
        return _mcp_response(
            {
                "error": "await_seconds_invalid",
                "message": (
                    f"await_seconds must be an integer in "
                    f"[{_AWAIT_SECONDS_MIN}, {_AWAIT_SECONDS_MAX}], "
                    f"got {await_seconds_raw!r}"
                ),
            }
        )
    await_seconds: int = await_seconds_raw
    if not (_AWAIT_SECONDS_MIN <= await_seconds <= _AWAIT_SECONDS_MAX):
        return _mcp_response(
            {
                "error": "await_seconds_invalid",
                "message": (
                    f"await_seconds must be in "
                    f"[{_AWAIT_SECONDS_MIN}, {_AWAIT_SECONDS_MAX}], "
                    f"got {await_seconds}"
                ),
            }
        )

    if search_target not in ("content", "filename"):
        return _mcp_response(
            {
                "error": "invalid_search_target",
                "message": (
                    f"search_target must be 'content' or 'filename', got {search_target!r}"
                ),
            }
        )

    if max_files is not None and max_files < 1:
        return _mcp_response(
            {
                "error": "max_files_out_of_range",
                "message": "max_files must be >= 1 when provided",
            }
        )

    if not (_MAX_DEBUG_NODES_MIN <= max_debug_nodes <= _MAX_DEBUG_NODES_MAX):
        return _mcp_response(
            {
                "error": "max_debug_nodes_out_of_range",
                "message": (
                    f"max_debug_nodes must be between {_MAX_DEBUG_NODES_MIN} and "
                    f"{_MAX_DEBUG_NODES_MAX}, got {max_debug_nodes}"
                ),
            }
        )

    # ------------------------------------------------------------------
    # 3. Repository alias resolution
    # ------------------------------------------------------------------
    repo_path_str = _resolve_repo_path(repo_alias)
    if repo_path_str is None:
        return _mcp_response(
            {
                "error": "repository_not_found",
                "message": f"Repository alias {repo_alias!r} not found",
            }
        )

    # ------------------------------------------------------------------
    # 4. Effective timeout + range check
    # ------------------------------------------------------------------
    effective_timeout: int = (
        timeout_override if timeout_override is not None else _DEFAULT_TIMEOUT_SECONDS
    )
    if not (_TIMEOUT_MIN <= effective_timeout <= _TIMEOUT_MAX):
        return _mcp_response(
            {
                "error": "timeout_out_of_range",
                "message": (
                    f"timeout_seconds must be between {_TIMEOUT_MIN} and "
                    f"{_TIMEOUT_MAX}, got {effective_timeout}"
                ),
            }
        )

    # ------------------------------------------------------------------
    # 5. Pre-flight evaluator validation
    # ------------------------------------------------------------------
    engine = XRaySearchEngine()

    validation = engine.sandbox.validate(evaluator_code)
    if not validation.ok:
        return _mcp_response(
            {
                "error": "xray_evaluator_validation_failed",
                "message": validation.reason,
            }
        )

    # ------------------------------------------------------------------
    # 6. Submit background job with include_ast_debug=True
    # ------------------------------------------------------------------
    repo_path = Path(repo_path_str)

    def job_fn(progress_callback):  # type: ignore[no-untyped-def]
        from code_indexer.xray.search_engine import XRaySearchEngine as _Engine

        return _Engine().run(
            repo_path=repo_path,
            driver_regex=driver_regex,
            evaluator_code=evaluator_code,
            search_target=search_target,
            include_patterns=list(include_patterns),
            exclude_patterns=list(exclude_patterns),
            timeout_seconds=effective_timeout,
            progress_callback=progress_callback,
            max_files=max_files,
            include_ast_debug=True,
            max_debug_nodes=max_debug_nodes,
        )

    bjm = _get_background_job_manager()
    job_id: str = bjm.submit_job(
        operation_type="xray_explore",
        func=job_fn,
        submitter_username=user.username,
        repo_alias=repo_alias,
    )

    if await_seconds > 0:
        inline = _await_job_result(bjm, job_id, user.username, await_seconds)
        if inline is not None:
            return _mcp_response(inline)

    return _mcp_response({"job_id": job_id})


def handle_xray_dump_ast(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """MCP handler for the xray_dump_ast tool (Issue #19).

    Synchronous single-file AST dump — no background job.  Returns the
    parse tree of a single file within a repository snapshot inline.

    Auth: query_repos permission required.

    Inputs:
        repository_alias (str): Global repository alias.
        file_path (str): Relative path within the repository.

    Output:
        {ast_tree: <BFS-serialised root node>} on success, or an error dict.

    Error codes:
        auth_required              — unauthenticated or missing query_repos.
        repository_not_found       — alias cannot be resolved.
        invalid_file_path          — file_path is empty or absolute.
        path_traversal_rejected    — file_path escapes the repository root.
        file_not_found             — resolved path does not exist.
        unsupported_language       — no tree-sitter grammar for the extension.
        xray_extras_not_installed  — tree-sitter extras not installed.
        parse_error                — unexpected failure during AST parsing.
    """
    # 1. Auth + permission check
    if user is None or not user.has_permission("query_repos"):
        return _mcp_response({"error": "auth_required"})

    # 2. Parameter extraction
    repo_alias: str = params.get("repository_alias", "")
    file_path_raw: str = params.get("file_path", "")

    if not file_path_raw:
        return _mcp_response(
            {"error": "invalid_file_path", "message": "file_path must not be empty"}
        )

    # 3. Repository alias resolution
    repo_path_str = _resolve_repo_path(repo_alias)
    if repo_path_str is None:
        return _mcp_response(
            {
                "error": "repository_not_found",
                "message": f"Repository alias {repo_alias!r} not found",
            }
        )

    repo_root = Path(repo_path_str)

    # 4. Path traversal protection — resolve and verify the path stays within repo root.
    target = (repo_root / file_path_raw).resolve()
    try:
        target.relative_to(repo_root.resolve())
    except ValueError:
        return _mcp_response(
            {
                "error": "path_traversal_rejected",
                "message": (
                    f"file_path {file_path_raw!r} resolves outside the repository root"
                ),
            }
        )

    # 5. File existence check
    if not target.is_file():
        return _mcp_response(
            {
                "error": "file_not_found",
                "message": f"File not found: {file_path_raw!r}",
            }
        )

    # 6. Parse and serialise
    try:
        engine = XRaySearchEngine()
        lang = engine.ast_engine.detect_language(target)
        if lang is None:
            return _mcp_response(
                {
                    "error": "unsupported_language",
                    "message": (
                        f"No tree-sitter grammar for extension {target.suffix!r}"
                    ),
                }
            )
        source_bytes = target.read_bytes()
        root = engine.ast_engine.parse(source_bytes, lang)
        ast_tree = XRaySearchEngine._serialize_ast(root, max_nodes=500)
        return _mcp_response(
            {
                "ast_tree": ast_tree,
                "file_path": file_path_raw,
                "language": lang,
            }
        )
    except ImportError as exc:
        return _mcp_response(
            {
                "error": "xray_extras_not_installed",
                "message": str(exc),
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("xray_dump_ast parse error for %s: %s", file_path_raw, exc)
        return _mcp_response(
            {
                "error": "parse_error",
                "message": str(exc),
            }
        )


def handle_cidx_fetch_cached_payload(
    params: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """MCP handler for the cidx_fetch_cached_payload tool (Issue #20).

    Retrieves a full payload stored in PayloadCache by its cache_handle.
    This is the discoverable tool to use when xray_search / xray_explore
    (or any other tool) returns a truncated result with a cache_handle.

    Auth: query_repos permission required.

    Inputs:
        cache_handle (str): Opaque handle returned in a truncated result.
        page (int, optional): 0-indexed page number. Defaults to 0.

    Output:
        {success: True, content: str, page: int, total_pages: int, has_more: bool}
        or {success: False, error: str, message: str}

    Error codes:
        auth_required   — unauthenticated or missing query_repos.
        missing_handle  — cache_handle parameter not provided.
        cache_expired   — handle not found or expired.
        cache_unavailable — PayloadCache not configured.
    """
    if user is None or not user.has_permission("query_repos"):
        return _mcp_response({"error": "auth_required"})

    cache_handle: str = params.get("cache_handle", "")
    page: int = max(0, int(params.get("page", 0) or 0))

    if not cache_handle:
        return _mcp_response(
            {
                "success": False,
                "error": "missing_handle",
                "message": "cache_handle parameter is required",
            }
        )

    payload_cache = getattr(_utils.app_module.app.state, "payload_cache", None)
    if payload_cache is None:
        return _mcp_response(
            {
                "success": False,
                "error": "cache_unavailable",
                "message": "Cache service not configured",
            }
        )

    try:
        result = payload_cache.retrieve(cache_handle, page=page)
        return _mcp_response(
            {
                "success": True,
                "content": result.content,
                "page": result.page,
                "total_pages": result.total_pages,
                "has_more": result.has_more,
            }
        )
    except Exception as exc:  # noqa: BLE001
        from code_indexer.server.cache.payload_cache import CacheNotFoundError as _CNF

        if isinstance(exc, _CNF):
            return _mcp_response(
                {
                    "success": False,
                    "error": "cache_expired",
                    "message": str(exc),
                    "cache_handle": cache_handle,
                }
            )
        logger.warning("cidx_fetch_cached_payload error for %s: %s", cache_handle, exc)
        return _mcp_response({"success": False, "error": str(exc)})


def _truncate_xray_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Apply PayloadCache truncation to the large fields of an X-Ray result.

    Serialises matches[] and evaluation_errors[] as a single JSON blob and
    delegates to PayloadCache.truncate_result().  When the combined payload
    exceeds payload_preview_size_chars (default 2000 chars) the full blob is
    stored in the cache and the response carries:
      - cache_handle: str           — use GET /api/cache/{handle} for full data
      - has_more: True
      - total_size: int             — full payload byte size
      - matches_and_errors_preview  — first N chars of the JSON
      - matches[]: first 3 entries  — inline quick-scan subset
      - evaluation_errors[]: first 3 entries — inline quick-scan subset
      - truncated: True

    When the payload is small (fits within preview_size_chars) the full
    matches and evaluation_errors arrays are returned inline:
      - cache_handle: None
      - has_more: False
      - truncated: False

    When PayloadCache is unavailable (not configured in app.state) the
    original result dict is returned unchanged.
    """
    import json

    payload_cache = getattr(_utils.app_module.app.state, "payload_cache", None)
    if payload_cache is None:
        return result

    large_payload = json.dumps(
        {
            "matches": result.get("matches", []),
            "evaluation_errors": result.get("evaluation_errors", []),
        }
    )

    truncation = payload_cache.truncate_result(large_payload)

    # Build base dict: preserve all top-level fields except matches/evaluation_errors
    truncated_result = {
        k: v for k, v in result.items() if k not in ("matches", "evaluation_errors")
    }

    if truncation.get("has_more"):
        truncated_result["matches_and_errors_preview"] = truncation["preview"]
        truncated_result["cache_handle"] = truncation["cache_handle"]
        truncated_result["has_more"] = True
        truncated_result["total_size"] = truncation["total_size"]
        truncated_result["matches"] = result.get("matches", [])[:3]
        truncated_result["evaluation_errors"] = result.get("evaluation_errors", [])[:3]
        truncated_result["truncated"] = True
        truncated_result["fetch_tool_hint"] = (
            f"Result truncated to first 3 entries; full result available at "
            f"cache_handle '{truncation['cache_handle']}' — fetch via the "
            f"`cidx_fetch_cached_payload` MCP tool with that handle."
        )
    else:
        truncated_result["matches"] = result.get("matches", [])
        truncated_result["evaluation_errors"] = result.get("evaluation_errors", [])
        truncated_result["cache_handle"] = None
        truncated_result["has_more"] = False
        truncated_result["truncated"] = False

    return truncated_result


def _register(registry: Dict[str, Any]) -> None:
    """Register xray handlers in the HANDLER_REGISTRY."""
    registry["xray_search"] = handle_xray_search
    registry["xray_explore"] = handle_xray_explore
    registry["xray_dump_ast"] = handle_xray_dump_ast
    registry["cidx_fetch_cached_payload"] = handle_cidx_fetch_cached_payload


