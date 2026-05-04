"""xray_search MCP handler.

Thin shim: validate inputs, pre-flight check evaluator, submit background job.
Heavy lifting lives in XRaySearchEngine (src/code_indexer/xray/search_engine.py).

Story #972: synchronous single-threaded XRaySearchEngine baseline.
Story #978: will add ThreadPoolExecutor parallelism and job-level timeout.
"""

from __future__ import annotations

import logging
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

    return _mcp_response({"job_id": job_id})


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


