"""xray_search MCP handler.

Thin shim: validate inputs, pre-flight check evaluator, submit background job.
Heavy lifting lives in XRaySearchEngine (src/code_indexer/xray/search_engine.py).

Story #972: synchronous single-threaded XRaySearchEngine baseline.
Story #978: will add ThreadPoolExecutor parallelism and job-level timeout.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional, cast

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.xray.sandbox import validate_rust_evaluator
from code_indexer.xray.search_engine import XRaySearchEngine

from . import _utils
from ._utils import _mcp_response, _parse_json_string_array

logger = logging.getLogger(__name__)


def _get_cidx_meta_path() -> Path:
    """Return the mutable cidx-meta base path derived from the golden_repo_manager.

    Extracted as a module-level function so tests can patch it directly.

    Raises:
        RuntimeError: If golden_repo_manager is not configured in the app module.
    """
    grm = getattr(_utils.app_module, "golden_repo_manager", None)
    if grm is None:
        raise RuntimeError(
            "cidx-meta path not available: golden_repo_manager not configured in app module"
        )
    return Path(grm.golden_repos_dir) / "cidx-meta"


# Timeout range enforced by the handler (seconds).
_TIMEOUT_MIN = 10
_TIMEOUT_MAX = 600

# max_nodes range for xray_dump_ast (Finding 3.2, v10.4.4).
_DUMP_AST_MAX_NODES_DEFAULT = 500
_DUMP_AST_MAX_NODES_MIN = 1
_DUMP_AST_MAX_NODES_MAX = 2000

# Default Rust evaluator used when the caller omits evaluator_code.
# Returns one finding per file at the root node's start line.
# Semantically equivalent to the legacy "accept all Phase 1 hits" behavior.
_DEFAULT_EVALUATOR_CODE = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    "    vec![EvalFinding {\n"
    '        pattern: "match".to_string(),\n'
    "        line: node.start_line,\n"
    "        snippet: String::new(),\n"
    "    }]\n"
    "}"
)

# Default timeout when the caller omits timeout_seconds.
_DEFAULT_TIMEOUT_SECONDS = 120

# Guard so ensure_seed_patterns() is called at most once per process lifetime.
_seeds_ensured = False


def _resolve_evaluator_code(
    params: Dict[str, Any],
    repo_alias: str,
    default_evaluator: str = _DEFAULT_EVALUATOR_CODE,
) -> "tuple[str, Optional[Dict[str, Any]]]":
    """Resolve evaluator_code from pattern_name or raw evaluator_code.

    Returns ``(evaluator_code, None)`` on success, or
    ``("", error_response)`` where *error_response* is a complete
    ``_mcp_response`` dict that the caller must return immediately.
    """
    global _seeds_ensured

    raw_evaluator_code: str = params.get("evaluator_code") or ""
    pattern_name: Optional[str] = params.get("pattern_name") or None
    pattern_params: Optional[Dict[str, Any]] = params.get("pattern_params") or None

    if pattern_name and raw_evaluator_code.strip():
        return (
            "",
            _mcp_response(
                {
                    "error": "mutually_exclusive_params",
                    "message": (
                        "pattern_name and evaluator_code are mutually exclusive — "
                        "provide one or the other, not both"
                    ),
                }
            ),
        )

    if pattern_name:
        from code_indexer.server.services.xray_pattern_service import XrayPatternService

        cidx_meta = _get_cidx_meta_path()
        svc = XrayPatternService(
            cidx_meta,
            refresh_scheduler=_utils._get_app_refresh_scheduler(),
        )
        if not _seeds_ensured:
            svc.ensure_seed_patterns()
            _seeds_ensured = True
        try:
            evaluator_code, _ = svc.resolve_and_prepare_pattern(
                repo_alias=repo_alias,
                pattern_name=pattern_name,
                pattern_params=pattern_params,
            )
        except ValueError as exc:
            error_key = str(exc).split(":")[0]
            return (
                "",
                _mcp_response(
                    {
                        "error": error_key,
                        "message": str(exc),
                    }
                ),
            )
        return (evaluator_code, None)

    evaluator_code = (
        raw_evaluator_code if raw_evaluator_code.strip() else default_evaluator
    )
    return (evaluator_code, None)


# await_seconds range and poll interval.
# Bug #1070: _AWAIT_SECONDS_MAX lowered from 120.0 to 45.0. Handlers are now async,
# so the polling loop uses asyncio.sleep instead of time.sleep and no longer holds
# _mcp_executor threads. The cap of 45.0 avoids 504s at the ALB 60s hard timeout.
# Task #35 (v10.3.2): await_seconds accepts int OR float — typed as float.
_AWAIT_SECONDS_MIN: float = 0.0
_AWAIT_SECONDS_MAX: float = 45.0
_AWAIT_SECONDS_WARN_THRESHOLD: float = 30.0
_AWAIT_POLL_INTERVAL = 0.05


def set_xray_executor(executor: ThreadPoolExecutor) -> None:
    """Store the dedicated xray ThreadPoolExecutor on app.state (called from lifespan).

    Bug #1070: xray compute must run on a dedicated pool isolated from the 5-worker
    BackgroundJobManager pool. lifespan calls this after constructing the executor.

    Raises:
        RuntimeError: If the app instance is not yet available (startup wiring error).
    """
    app = getattr(_utils.app_module, "app", None)
    if app is None:
        raise RuntimeError(
            "set_xray_executor called before app is available — startup wiring error"
        )
    app.state.xray_executor = executor


def _get_xray_executor() -> ThreadPoolExecutor:
    """Return the dedicated xray ThreadPoolExecutor from app.state.

    Raises:
        RuntimeError: If app or xray_executor is not configured.
    """
    app = getattr(_utils.app_module, "app", None)
    if app is None:
        raise RuntimeError("xray_executor not available: app is not configured")
    executor = getattr(app.state, "xray_executor", None)
    if executor is None:
        raise RuntimeError(
            "xray_executor not available: set_xray_executor() was not called during startup"
        )
    return cast(ThreadPoolExecutor, executor)


def _get_job_tracker() -> Any:
    """Return the live JobTracker from the app module.

    Bug #1070: xray uses register_job() directly (no conflict check) instead of
    submit_job() which calls register_job_if_no_conflict() — that gate serializes
    concurrent xray calls on the same repo, which is wrong for read-only operations.
    """
    return _utils.app_module.job_tracker


def handle_store_xray_pattern(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """MCP handler for the store_xray_pattern tool.

    Stores a reusable xray evaluator pattern in the cidx-meta pattern library.

    Error codes:
        auth_required            — unauthenticated or missing query_repos.
        scope_required           — scope parameter missing or empty.
        pattern_yaml_required    — pattern_yaml parameter missing or empty.
        invalid_yaml             — pattern_yaml cannot be parsed as YAML.
        missing_required_field   — required field absent from pattern YAML.
        xray_evaluator_validation_failed — evaluator code fails Rust whitelist.
        pattern_already_exists   — pattern exists and overwrite=false.
        invalid_parameter        — unknown parameter name declared.
        invalid_parameter_type   — parameter type not in allowed set.
    """
    if user is None or not user.has_permission("query_repos"):
        return _mcp_response({"error": "auth_required"})

    scope: str = params.get("scope", "")
    pattern_yaml: str = params.get("pattern_yaml", "")
    overwrite: bool = bool(params.get("overwrite", False))

    if not scope:
        return _mcp_response(
            {"error": "scope_required", "message": "scope parameter is required"}
        )
    if not pattern_yaml:
        return _mcp_response(
            {
                "error": "pattern_yaml_required",
                "message": "pattern_yaml parameter is required",
            }
        )

    from code_indexer.server.services.xray_pattern_service import XrayPatternService

    cidx_meta = _get_cidx_meta_path()
    svc = XrayPatternService(
        cidx_meta,
        refresh_scheduler=_utils._get_app_refresh_scheduler(),
    )
    result = svc.store_xray_pattern(
        scope=scope,
        pattern_yaml=pattern_yaml,
        overwrite=overwrite,
    )
    return _mcp_response(result)


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


async def _await_xray_future(
    future: "asyncio.Future[Any]", await_seconds: float
) -> Optional[Dict[str, Any]]:
    """Await an xray compute future with a deadline, yielding the event loop between polls.

    Bug #1070: replaces the synchronous time.sleep polling loop. This async version
    uses asyncio.sleep so no _mcp_executor thread is held during the wait.

    Args:
        future: asyncio.Future returned by loop.run_in_executor(_xray_executor, ...).
        await_seconds: Maximum seconds to wait for the future to complete.

    Returns:
        The xray result dict if the future completes within the window, else None.
    """
    import asyncio as _asyncio
    import time as _time

    deadline = _time.monotonic() + await_seconds
    while _time.monotonic() < deadline:
        if future.done():
            return cast(Optional[Dict[str, Any]], future.result())
        await _asyncio.sleep(_AWAIT_POLL_INTERVAL)
    return None


async def handle_xray_search(params: Dict[str, Any], user: User) -> Dict[str, Any]:
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
    # 'pattern' is the regex_search-aligned name (was 'driver_regex')
    driver_regex: str = params.get("pattern", "")
    evaluator_code, err_resp = _resolve_evaluator_code(params, repo_alias)
    if err_resp is not None:
        return err_resp
    search_target: str = params.get("search_target", "")
    include_patterns = params.get("include_patterns") or []
    exclude_patterns = params.get("exclude_patterns") or []
    # regex_search-aligned params
    case_sensitive: bool = params.get("case_sensitive", True)
    context_lines_raw = params.get("context_lines", 0)
    multiline: bool = params.get("multiline", False)
    pcre2: bool = params.get("pcre2", False)
    path: Optional[str] = params.get("path")
    timeout_override = params.get("timeout_seconds")
    # 'max_results' is the regex_search-aligned name (was 'max_files')
    max_results = params.get("max_results")
    await_seconds_raw = params.get("await_seconds", 0)

    # 'pattern' is required — reject if missing or empty (catches old 'driver_regex' callers)
    if not driver_regex:
        return _mcp_response(
            {
                "error": "pattern_required",
                "message": "pattern is required (formerly 'driver_regex' — use 'pattern')",
            }
        )

    # await_seconds accepts int or float in [0.0, 10.0]. Cap lowered from 30
    # to 10 in v10.3.2 to bound threadpool occupancy (see top-of-file comment).
    if isinstance(await_seconds_raw, bool) or not isinstance(
        await_seconds_raw, (int, float)
    ):
        return _mcp_response(
            {
                "error": "await_seconds_invalid",
                "message": (
                    f"await_seconds must be a number (int or float) in "
                    f"[{_AWAIT_SECONDS_MIN}, {_AWAIT_SECONDS_MAX}], "
                    f"got {await_seconds_raw!r}"
                ),
            }
        )
    await_seconds: float = float(await_seconds_raw)
    if not (_AWAIT_SECONDS_MIN <= await_seconds <= _AWAIT_SECONDS_MAX):
        return _mcp_response(
            {
                "error": "await_seconds_invalid",
                "message": (
                    f"await_seconds must be in "
                    f"[{_AWAIT_SECONDS_MIN}, {_AWAIT_SECONDS_MAX}] "
                    f"(cap lowered from 30 in v10.3.2 — for longer waits "
                    f"use the async {{job_id}} path), got {await_seconds}"
                ),
            }
        )
    if await_seconds > _AWAIT_SECONDS_WARN_THRESHOLD:
        logger.warning(
            "xray_search: await_seconds=%s may saturate threadpool under load",
            await_seconds,
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

    # context_lines: must be int in [0, 10]
    try:
        context_lines: int = int(context_lines_raw)
    except (TypeError, ValueError):
        context_lines = 0
    if not (0 <= context_lines <= 10):
        return _mcp_response(
            {
                "error": "context_lines_out_of_range",
                "message": "context_lines must be between 0 and 10",
            }
        )

    if max_results is not None and max_results < 1:
        return _mcp_response(
            {
                "error": "max_results_out_of_range",
                "message": "max_results must be >= 1 when provided",
            }
        )

    # Pre-validate non-PCRE2 patterns at handler level (v10.4.3 fix).
    # PCRE2 syntax differs (lookbehind etc.) and is validated by ripgrep
    # at execution time; surface those errors via the job result.
    if not pcre2:
        import re as _re

        try:
            _re.compile(driver_regex, flags=_re.MULTILINE if multiline else 0)
        except _re.error as _exc:
            return _mcp_response(
                {
                    "error": "invalid_regex",
                    "message": f"Invalid regex pattern: {_exc}",
                }
            )

    # ------------------------------------------------------------------
    # 3. Repository alias resolution — omni-aware (string OR list)
    # ------------------------------------------------------------------
    # Parse alias: accepts plain string, list of strings, or JSON-encoded
    # string array (e.g. '["repo-a", "repo-b"]').
    repo_alias_parsed = _parse_json_string_array(repo_alias)

    # v10.4.5 (Defect 5): normalize single-element list to single-string for
    # ergonomic single-repo response shape ({"job_id":"..."}). Multi-element
    # lists still take the multi-repo path ({"job_ids":[...], "errors":[...]}).
    if isinstance(repo_alias_parsed, list) and len(repo_alias_parsed) == 1:
        candidate = repo_alias_parsed[0]
        if isinstance(candidate, str) and candidate:
            repo_alias_parsed = candidate

    if isinstance(repo_alias_parsed, list):
        # Multi-repo path — submit one job per alias.
        if not repo_alias_parsed:
            return _mcp_response(
                {
                    "error": "alias_required",
                    "message": "repository_alias must not be empty",
                }
            )

        # ------------------------------------------------------------------
        # 4. Effective timeout + range check (multi-repo path)
        # ------------------------------------------------------------------
        effective_timeout_multi: int = (
            timeout_override
            if timeout_override is not None
            else _DEFAULT_TIMEOUT_SECONDS
        )
        if not (_TIMEOUT_MIN <= effective_timeout_multi <= _TIMEOUT_MAX):
            return _mcp_response(
                {
                    "error": "timeout_out_of_range",
                    "message": (
                        f"timeout_seconds must be between {_TIMEOUT_MIN} and "
                        f"{_TIMEOUT_MAX}, got {effective_timeout_multi}"
                    ),
                }
            )

        # ------------------------------------------------------------------
        # 5. Pre-flight evaluator validation (multi-repo path)
        # ------------------------------------------------------------------
        validation_multi = validate_rust_evaluator(evaluator_code)
        if not validation_multi.ok:
            return _mcp_response(
                {
                    "error": "xray_evaluator_validation_failed",
                    "error_code": validation_multi.error_code,
                    "offending_construct": validation_multi.offending_construct,
                    "offending_line": validation_multi.offending_line,
                    "message": validation_multi.reason,
                }
            )

        # ------------------------------------------------------------------
        # 6. Submit one background job per alias
        # ------------------------------------------------------------------
        bjm_multi = _get_background_job_manager()
        job_ids: list = []
        errors: list = []

        for single_alias in repo_alias_parsed:
            single_path_str = _resolve_repo_path(single_alias)
            if single_path_str is None:
                errors.append(
                    {
                        "repository_alias": single_alias,
                        "error": "repository_not_found",
                        "message": f"Repository alias {single_alias!r} not found",
                    }
                )
                continue

            single_repo_path = Path(single_path_str)
            timeout_capture = effective_timeout_multi

            def _make_job_fn(rp: Path, t: int, jid_holder: list):  # type: ignore[no-untyped-def]
                def _job(progress_callback):  # type: ignore[no-untyped-def]
                    from code_indexer.xray.search_engine import XRaySearchEngine as _E

                    def _on_spawned(proc):  # type: ignore[no-untyped-def]
                        if jid_holder:
                            bjm_multi.register_child_process(jid_holder[0], proc)

                    result = _E().run(
                        repo_path=rp,
                        driver_regex=driver_regex,
                        evaluator_code=evaluator_code,
                        search_target=search_target,
                        include_patterns=list(include_patterns),
                        exclude_patterns=list(exclude_patterns),
                        case_sensitive=case_sensitive,
                        context_lines=context_lines,
                        multiline=multiline,
                        pcre2=pcre2,
                        path=path,
                        timeout_seconds=t,
                        progress_callback=progress_callback,
                        max_files=max_results,
                        on_process_spawned=_on_spawned,
                    )
                    if jid_holder:
                        bjm_multi.unregister_child_processes(jid_holder[0])
                    return _truncate_xray_result(result)

                return _job

            _jid_holder: list = []
            jid: str = bjm_multi.submit_job(
                operation_type="xray_search",
                func=_make_job_fn(single_repo_path, timeout_capture, _jid_holder),
                submitter_username=user.username,
                repo_alias=single_alias,
            )
            _jid_holder.append(jid)
            job_ids.append(jid)

        return _mcp_response({"job_ids": job_ids, "errors": errors})

    # ------------------------------------------------------------------
    # Single-repo path (string alias)
    # ------------------------------------------------------------------

    # Story #1039: bare-to-global alias fallback (read-only handler).
    if isinstance(repo_alias_parsed, str) and not repo_alias_parsed.endswith("-global"):
        _arm = getattr(_utils.app_module, "activated_repo_manager", None)
        _grm = getattr(_utils.app_module, "golden_repo_manager", None)
        if _arm is not None and _grm is not None:
            if not _arm.user_has_activated_repo(user.username, repo_alias_parsed):
                from ._global_fallback import try_global_fallback

                _promoted = try_global_fallback(repo_alias_parsed, _grm)
                if _promoted is not None:
                    logger.info(
                        "bare-alias fallback: %r -> %r for user %r",
                        repo_alias_parsed,
                        _promoted,
                        user.username,
                    )
                    repo_alias_parsed = _promoted
                    params["repository_alias"] = _promoted

    repo_path_str = _resolve_repo_path(repo_alias_parsed)
    if repo_path_str is None:
        return _mcp_response(
            {
                "error": "repository_not_found",
                "message": f"Repository alias {repo_alias_parsed!r} not found",
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
    validation = validate_rust_evaluator(evaluator_code)
    if not validation.ok:
        return _mcp_response(
            {
                "error": "xray_evaluator_validation_failed",
                "error_code": validation.error_code,
                "offending_construct": validation.offending_construct,
                "offending_line": validation.offending_line,
                "message": validation.reason,
            }
        )

    # ------------------------------------------------------------------
    # 6. Submit to dedicated xray executor (Bug #1070: bypass BJM worker pool)
    # ------------------------------------------------------------------
    repo_path = Path(repo_path_str)

    bjm = _get_background_job_manager()
    job_tracker = _get_job_tracker()
    xray_executor = _get_xray_executor()
    loop = asyncio.get_running_loop()

    job_id = str(uuid.uuid4())
    job_tracker.register_job(
        job_id=job_id,
        operation_type="xray_search",
        username=user.username,
        repo_alias=None,  # NULL bypasses idx_active_job_per_repo; xray is read-only
        metadata={"repo_alias": repo_alias_parsed},
    )

    def job_fn() -> Dict[str, Any]:  # type: ignore[no-untyped-def]
        from code_indexer.xray.search_engine import XRaySearchEngine as _Engine

        def _on_spawned(proc) -> None:  # type: ignore[no-untyped-def]
            bjm.register_child_process(job_id, proc)

        try:
            result = _Engine().run(
                repo_path=repo_path,
                driver_regex=driver_regex,
                evaluator_code=evaluator_code,
                search_target=search_target,
                include_patterns=list(include_patterns),
                exclude_patterns=list(exclude_patterns),
                case_sensitive=case_sensitive,
                context_lines=context_lines,
                multiline=multiline,
                pcre2=pcre2,
                path=path,
                timeout_seconds=effective_timeout,
                progress_callback=None,
                max_files=max_results,
                on_process_spawned=_on_spawned,
            )
        finally:
            bjm.unregister_child_processes(job_id)
        return _truncate_xray_result(result)

    future = loop.run_in_executor(xray_executor, job_fn)

    def _on_done_search(fut: "asyncio.Future[Any]") -> None:
        if fut.cancelled() or (not fut.cancelled() and fut.exception() is not None):
            exc = fut.exception() if not fut.cancelled() else None
            job_tracker.fail_job(job_id, str(exc) if exc else "cancelled")
        else:
            job_tracker.complete_job(job_id, fut.result())

    future.add_done_callback(_on_done_search)

    if await_seconds > 0:
        inline = await _await_xray_future(future, await_seconds)
        if inline is not None:
            return _mcp_response(inline)

    return _mcp_response({"job_id": job_id})


# Default and range constants for max_debug_nodes (xray_explore).
_MAX_DEBUG_NODES_DEFAULT = 50
_MAX_DEBUG_NODES_MIN = 1
_MAX_DEBUG_NODES_MAX = 500


def _make_xray_explore_job_fn(  # type: ignore[no-untyped-def]
    repo_path: "Path",
    driver_regex: str,
    evaluator_code: str,
    search_target: str,
    include_patterns: list,
    exclude_patterns: list,
    case_sensitive: bool,
    context_lines: int,
    multiline: bool,
    pcre2: bool,
    path: "Optional[str]",
    effective_timeout: int,
    max_results: "Optional[int]",
    max_debug_nodes: int,
    job_id_holder: "Optional[list]" = None,
    bjm: "Optional[Any]" = None,
):
    """Return a job function closure for xray_explore.

    Extracted to eliminate duplication between the single-repo and multi-repo
    job-submission paths in handle_xray_explore.
    """

    def job_fn(progress_callback):  # type: ignore[no-untyped-def]
        from code_indexer.xray.search_engine import XRaySearchEngine as _Engine

        def _on_spawned(proc):  # type: ignore[no-untyped-def]
            if bjm is not None and job_id_holder:
                bjm.register_child_process(job_id_holder[0], proc)

        result = _Engine().run(
            repo_path=repo_path,
            driver_regex=driver_regex,
            evaluator_code=evaluator_code,
            search_target=search_target,
            include_patterns=list(include_patterns),
            exclude_patterns=list(exclude_patterns),
            case_sensitive=case_sensitive,
            context_lines=context_lines,
            multiline=multiline,
            pcre2=pcre2,
            path=path,
            timeout_seconds=effective_timeout,
            progress_callback=progress_callback,
            max_files=max_results,
            include_ast_debug=True,
            max_debug_nodes=max_debug_nodes,
            on_process_spawned=_on_spawned,
        )
        if bjm is not None and job_id_holder:
            bjm.unregister_child_processes(job_id_holder[0])
        return result

    return job_fn


def _submit_xray_explore_omni(  # type: ignore[no-untyped-def]
    aliases: list,
    user: "Any",
    driver_regex: str,
    evaluator_code: str,
    search_target: str,
    include_patterns: list,
    exclude_patterns: list,
    case_sensitive: bool,
    context_lines: int,
    multiline: bool,
    pcre2: bool,
    path: "Optional[str]",
    effective_timeout: int,
    max_results: "Optional[int]",
    max_debug_nodes: int,
) -> Dict[str, Any]:
    """Submit one xray_explore background job per alias.

    Extracted from handle_xray_explore to keep that handler concise.
    Returns a {job_ids, errors} response dict (not yet wrapped in _mcp_response).
    """
    bjm = _get_background_job_manager()
    job_ids: list = []
    errors: list = []

    for single_alias in aliases:
        single_path_str = _resolve_repo_path(single_alias)
        if single_path_str is None:
            errors.append(
                {
                    "repository_alias": single_alias,
                    "error": "repository_not_found",
                    "message": f"Repository alias {single_alias!r} not found",
                }
            )
            continue

        _jid_holder: list = []
        jid: str = bjm.submit_job(
            operation_type="xray_explore",
            func=_make_xray_explore_job_fn(
                repo_path=Path(single_path_str),
                driver_regex=driver_regex,
                evaluator_code=evaluator_code,
                search_target=search_target,
                include_patterns=include_patterns,
                exclude_patterns=exclude_patterns,
                case_sensitive=case_sensitive,
                context_lines=context_lines,
                multiline=multiline,
                pcre2=pcre2,
                path=path,
                effective_timeout=effective_timeout,
                max_results=max_results,
                max_debug_nodes=max_debug_nodes,
                job_id_holder=_jid_holder,
                bjm=bjm,
            ),
            submitter_username=user.username,
            repo_alias=single_alias,
        )
        _jid_holder.append(jid)
        job_ids.append(jid)

    return {"job_ids": job_ids, "errors": errors}


async def handle_xray_explore(params: Dict[str, Any], user: User) -> Dict[str, Any]:
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
    # 'pattern' is the regex_search-aligned name (was 'driver_regex')
    driver_regex: str = params.get("pattern", "")
    evaluator_code, err_resp = _resolve_evaluator_code(params, repo_alias)
    if err_resp is not None:
        return err_resp
    search_target: str = params.get("search_target", "")
    include_patterns = params.get("include_patterns") or []
    exclude_patterns = params.get("exclude_patterns") or []
    # regex_search-aligned params
    case_sensitive: bool = params.get("case_sensitive", True)
    context_lines_raw = params.get("context_lines", 0)
    multiline: bool = params.get("multiline", False)
    pcre2: bool = params.get("pcre2", False)
    path: Optional[str] = params.get("path")
    timeout_override = params.get("timeout_seconds")
    # 'max_results' is the regex_search-aligned name (was 'max_files')
    max_results = params.get("max_results")
    max_debug_nodes = params.get("max_debug_nodes", _MAX_DEBUG_NODES_DEFAULT)
    await_seconds_raw = params.get("await_seconds", 0)

    # await_seconds accepts int or float in [0.0, 10.0]. Cap lowered from 30
    # to 10 in v10.3.2 to bound threadpool occupancy (see top-of-file comment).
    if isinstance(await_seconds_raw, bool) or not isinstance(
        await_seconds_raw, (int, float)
    ):
        return _mcp_response(
            {
                "error": "await_seconds_invalid",
                "message": (
                    f"await_seconds must be a number (int or float) in "
                    f"[{_AWAIT_SECONDS_MIN}, {_AWAIT_SECONDS_MAX}], "
                    f"got {await_seconds_raw!r}"
                ),
            }
        )
    await_seconds: float = float(await_seconds_raw)
    if not (_AWAIT_SECONDS_MIN <= await_seconds <= _AWAIT_SECONDS_MAX):
        return _mcp_response(
            {
                "error": "await_seconds_invalid",
                "message": (
                    f"await_seconds must be in "
                    f"[{_AWAIT_SECONDS_MIN}, {_AWAIT_SECONDS_MAX}] "
                    f"(cap lowered from 30 in v10.3.2 — for longer waits "
                    f"use the async {{job_id}} path), got {await_seconds}"
                ),
            }
        )
    if await_seconds > _AWAIT_SECONDS_WARN_THRESHOLD:
        logger.warning(
            "xray_explore: await_seconds=%s may saturate threadpool under load",
            await_seconds,
        )

    # 'pattern' is required — reject if missing or empty (catches old 'driver_regex' callers)
    if not driver_regex:
        return _mcp_response(
            {
                "error": "pattern_required",
                "message": "pattern is required (formerly 'driver_regex' — use 'pattern')",
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

    # context_lines: must be int in [0, 10]
    try:
        context_lines: int = int(context_lines_raw)
    except (TypeError, ValueError):
        context_lines = 0
    if not (0 <= context_lines <= 10):
        return _mcp_response(
            {
                "error": "context_lines_out_of_range",
                "message": "context_lines must be between 0 and 10",
            }
        )

    if max_results is not None and max_results < 1:
        return _mcp_response(
            {
                "error": "max_results_out_of_range",
                "message": "max_results must be >= 1 when provided",
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

    # Pre-validate non-PCRE2 patterns at handler level (v10.4.3 fix).
    # PCRE2 syntax differs (lookbehind etc.) and is validated by ripgrep
    # at execution time; surface those errors via the job result.
    if not pcre2:
        import re as _re

        try:
            _re.compile(driver_regex, flags=_re.MULTILINE if multiline else 0)
        except _re.error as _exc:
            return _mcp_response(
                {
                    "error": "invalid_regex",
                    "message": f"Invalid regex pattern: {_exc}",
                }
            )

    # ------------------------------------------------------------------
    # 3. Alias normalisation — omni-aware (string OR list)
    # ------------------------------------------------------------------
    # Bug 1 fix (v10.4.1): the original code passed repo_alias directly to
    # _resolve_repo_path without normalisation, crashing with AttributeError
    # ('list' object has no attribute 'endswith') on native-list or
    # JSON-encoded-array inputs.
    repo_alias_parsed = _parse_json_string_array(repo_alias)

    # v10.4.5 (Defect 5): normalize single-element list to single-string for
    # ergonomic single-repo response shape ({"job_id":"..."}). Multi-element
    # lists still take the multi-repo path ({"job_ids":[...], "errors":[...]}).
    if isinstance(repo_alias_parsed, list) and len(repo_alias_parsed) == 1:
        candidate = repo_alias_parsed[0]
        if isinstance(candidate, str) and candidate:
            repo_alias_parsed = candidate

    # ------------------------------------------------------------------
    # 4. Effective timeout + range check  (shared — runs before alias branch)
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
    # 5. Pre-flight evaluator validation  (shared — runs before alias branch)
    # ------------------------------------------------------------------
    validation = validate_rust_evaluator(evaluator_code)
    if not validation.ok:
        return _mcp_response(
            {
                "error": "xray_evaluator_validation_failed",
                "error_code": validation.error_code,
                "offending_construct": validation.offending_construct,
                "offending_line": validation.offending_line,
                "message": validation.reason,
            }
        )

    # ------------------------------------------------------------------
    # 6. Job submission — multi-repo OR single-repo
    # ------------------------------------------------------------------
    explore_kwargs: Dict[str, Any] = dict(
        driver_regex=driver_regex,
        evaluator_code=evaluator_code,
        search_target=search_target,
        include_patterns=list(include_patterns),
        exclude_patterns=list(exclude_patterns),
        case_sensitive=case_sensitive,
        context_lines=context_lines,
        multiline=multiline,
        pcre2=pcre2,
        path=path,
        effective_timeout=effective_timeout,
        max_results=max_results,
        max_debug_nodes=max_debug_nodes,
    )

    if isinstance(repo_alias_parsed, list):
        if not repo_alias_parsed:
            return _mcp_response(
                {
                    "error": "alias_required",
                    "message": "repository_alias must not be empty",
                }
            )
        return _mcp_response(
            _submit_xray_explore_omni(
                aliases=repo_alias_parsed, user=user, **explore_kwargs
            )
        )

    # Single-repo path

    # Story #1039: bare-to-global alias fallback (read-only handler).
    if isinstance(repo_alias_parsed, str) and not repo_alias_parsed.endswith("-global"):
        _arm = getattr(_utils.app_module, "activated_repo_manager", None)
        _grm = getattr(_utils.app_module, "golden_repo_manager", None)
        if _arm is not None and _grm is not None:
            if not _arm.user_has_activated_repo(user.username, repo_alias_parsed):
                from ._global_fallback import try_global_fallback

                _promoted = try_global_fallback(repo_alias_parsed, _grm)
                if _promoted is not None:
                    logger.info(
                        "bare-alias fallback: %r -> %r for user %r",
                        repo_alias_parsed,
                        _promoted,
                        user.username,
                    )
                    repo_alias_parsed = _promoted
                    params["repository_alias"] = _promoted

    repo_path_str = _resolve_repo_path(repo_alias_parsed)
    if repo_path_str is None:
        return _mcp_response(
            {
                "error": "repository_not_found",
                "message": f"Repository alias {repo_alias_parsed!r} not found",
            }
        )

    bjm = _get_background_job_manager()
    job_tracker = _get_job_tracker()
    xray_executor = _get_xray_executor()
    loop = asyncio.get_running_loop()

    job_id = str(uuid.uuid4())
    job_tracker.register_job(
        job_id=job_id,
        operation_type="xray_explore",
        username=user.username,
        repo_alias=None,  # NULL bypasses idx_active_job_per_repo; xray is read-only
        metadata={"repo_alias": repo_alias_parsed},
    )

    _explore_fn = _make_xray_explore_job_fn(
        repo_path=Path(repo_path_str),
        job_id_holder=[job_id],
        bjm=bjm,
        **explore_kwargs,
    )

    def _explore_worker() -> Dict[str, Any]:
        # _make_xray_explore_job_fn is no-untyped-def so mypy infers Any;
        # cast is safe — the function contract always returns Dict[str, Any].
        return cast(Dict[str, Any], _explore_fn(None))

    future = loop.run_in_executor(xray_executor, _explore_worker)

    def _on_done_explore(fut: "asyncio.Future[Any]") -> None:
        if fut.cancelled() or (not fut.cancelled() and fut.exception() is not None):
            exc = fut.exception() if not fut.cancelled() else None
            job_tracker.fail_job(job_id, str(exc) if exc else "cancelled")
        else:
            job_tracker.complete_job(job_id, fut.result())

    future.add_done_callback(_on_done_explore)

    if await_seconds > 0:
        inline = await _await_xray_future(future, await_seconds)
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

    # max_nodes: optional int, default 500, range [1, 2000] (Finding 3.2, v10.4.4)
    max_nodes_raw = params.get("max_nodes", _DUMP_AST_MAX_NODES_DEFAULT)
    try:
        max_nodes = int(max_nodes_raw)
    except (TypeError, ValueError):
        return _mcp_response(
            {
                "error": "max_nodes_invalid",
                "message": f"max_nodes must be int, got {max_nodes_raw!r}",
            }
        )
    if not (_DUMP_AST_MAX_NODES_MIN <= max_nodes <= _DUMP_AST_MAX_NODES_MAX):
        return _mcp_response(
            {
                "error": "max_nodes_out_of_range",
                "message": (
                    f"max_nodes must be in "
                    f"[{_DUMP_AST_MAX_NODES_MIN}, {_DUMP_AST_MAX_NODES_MAX}]"
                ),
            }
        )

    if not file_path_raw:
        return _mcp_response(
            {"error": "invalid_file_path", "message": "file_path must not be empty"}
        )

    # Story #1039: bare-to-global alias fallback (read-only handler).
    if isinstance(repo_alias, str) and not repo_alias.endswith("-global"):
        _arm = getattr(_utils.app_module, "activated_repo_manager", None)
        _grm = getattr(_utils.app_module, "golden_repo_manager", None)
        if _arm is not None and _grm is not None:
            if not _arm.user_has_activated_repo(user.username, repo_alias):
                from ._global_fallback import try_global_fallback

                _promoted = try_global_fallback(repo_alias, _grm)
                if _promoted is not None:
                    logger.info(
                        "bare-alias fallback: %r -> %r for user %r",
                        repo_alias,
                        _promoted,
                        user.username,
                    )
                    repo_alias = _promoted
                    params["repository_alias"] = _promoted

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
        ast_tree = XRaySearchEngine._serialize_ast(root, max_nodes=max_nodes)
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
        page (int, optional): 1-indexed page number. Defaults to 1.

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
    page: int = max(1, int(params.get("page", 1) or 1))

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
        result = payload_cache.retrieve(cache_handle, page=page - 1)
        return _mcp_response(
            {
                "success": True,
                "content": result.content,
                "page": result.page + 1,
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


def handle_cancel_job(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """MCP handler for the cancel_job tool.

    Cancels a running or pending background job. For xray_search/xray_explore
    jobs with registered child processes, sends SIGTERM then SIGKILL.
    """
    if user is None or not user.has_permission("query_repos"):
        return _mcp_response({"error": "auth_required"})

    job_id = params.get("job_id")
    if not job_id:
        return _mcp_response({"success": False, "message": "job_id is required"})

    bjm = _get_background_job_manager()
    is_admin = hasattr(user, "role") and user.role == UserRole.ADMIN
    result = bjm.cancel_job(job_id, user.username, is_admin)
    return _mcp_response(result)


def _register(registry: Dict[str, Any]) -> None:
    """Register xray handlers in the HANDLER_REGISTRY."""
    registry["xray_search"] = handle_xray_search
    registry["xray_explore"] = handle_xray_explore
    registry["xray_dump_ast"] = handle_xray_dump_ast
    registry["cidx_fetch_cached_payload"] = handle_cidx_fetch_cached_payload
    registry["cancel_job"] = handle_cancel_job
    registry["store_xray_pattern"] = handle_store_xray_pattern
