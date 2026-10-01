"""xray_explore MCP handler (Issue #1935 Part 2).

Moved verbatim out of the monolithic xray.py -- pure relocation, zero
behaviour change. Every function below is an intact, unmodified copy of
its HEAD counterpart (git show HEAD:.../xray.py:1243-1822); none was
decomposed into new helper functions.
"""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Optional, cast

from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.query_admission_gate import (
    check_query_admission,
    memory_pressure_mcp_payload,
)
from code_indexer.xray.sandbox import validate_rust_evaluator

from ._infra import (
    _AWAIT_SECONDS_MIN,
    _AWAIT_SECONDS_MAX,
    _AWAIT_SECONDS_WARN_THRESHOLD,
    _TIMEOUT_MIN,
    _TIMEOUT_MAX,
    _await_xray_future,
    _get_background_job_manager,
    _get_job_tracker,
    _get_xray_cell_limiter,
    _get_xray_executor,
    _pattern_scope_alias,
    _resolve_effective_timeout,
    _resolve_evaluator_code_off_loop,
    _resolve_repo_path,
    _validate_xray_search_patterns,
    logger,  # shared logger name -- see _infra.py's own comment
)

from .. import _utils
from .._utils import _mcp_response, _parse_and_collapse_repo_alias

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

        _limiter = _get_xray_cell_limiter()
        _slot = False
        if _limiter is not None:
            _slot = _limiter.acquire(timeout=float(effective_timeout))
            if not _slot:
                return {
                    "error": "xray_cell_queue_timeout",
                    "message": (
                        f"Timed out waiting for an xray worker slot after "
                        f"{effective_timeout}s — server busy with other xray jobs."
                    ),
                }

        def _on_spawned(proc):  # type: ignore[no-untyped-def]
            if bjm is not None and job_id_holder:
                bjm.register_child_process(job_id_holder[0], proc)

        try:
            # Bug #1590 AC3: transition to "running" only once execution
            # actually starts (after the cell-limiter slot is held, before
            # Phase 1 begins) -- so the dashboard can distinguish "queued,
            # no worker slot yet" from "actively executing". job_id_holder
            # is a single-element list populated by the caller before
            # job_fn runs (both single-repo and multi-repo/omni explore
            # paths). Code-review finding F2: this call MUST be inside
            # this try block -- placed before it, a raising
            # update_status() (e.g. a SQLite write failure) would skip the
            # finally below and leak the held xray cell-limiter slot
            # forever.
            if job_id_holder:
                _get_job_tracker().update_status(job_id_holder[0], status="running")
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
        finally:
            if bjm is not None and job_id_holder:
                bjm.unregister_child_processes(job_id_holder[0])
            if _slot and _limiter is not None:
                _limiter.release()
        return result

    return job_fn


def _make_explore_done_cb_omni(
    job_id: str,
    # job_tracker typed Any: JobTracker lives in server.repositories which cannot be
    # imported here without a circular import. Interface (fail_job/complete_job) is stable.
    job_tracker: Any,
) -> Callable[["asyncio.Future[Any]"], None]:
    """Factory: return a done-callback that updates job_tracker lifecycle for omni explore."""

    def _cb(fut: "asyncio.Future[Any]") -> None:
        if fut.cancelled() or (not fut.cancelled() and fut.exception() is not None):
            exc = fut.exception() if not fut.cancelled() else None
            job_tracker.fail_job(job_id, str(exc) if exc else "cancelled")
        else:
            job_tracker.complete_job(job_id, fut.result())

    return _cb


def _submit_one_explore_omni(
    single_alias: str,
    user: User,
    bjm: Any,  # BJM typed Any: same circular-import constraint as job_tracker
    loop: asyncio.AbstractEventLoop,
    job_tracker: Any,  # circular import constraint — see _make_explore_done_cb_omni
    xray_executor: ThreadPoolExecutor,
    explore_fn_kwargs: Dict[str, Any],
) -> Optional[str]:
    """Register, build, and submit one xray_explore job for a single alias.

    Returns the new job_id on success, or None if the alias cannot be resolved.
    Separated from _submit_xray_explore_omni to keep each function under 50 lines.
    """
    single_path_str = _resolve_repo_path(single_alias)
    if single_path_str is None:
        return None

    jid = str(uuid.uuid4())
    job_tracker.register_job(
        job_id=jid,
        operation_type="xray_explore",
        username=user.username,
        repo_alias=None,  # NULL bypasses idx_active_job_per_repo; xray is read-only
        metadata={"repo_alias": single_alias},
    )

    _explore_fn = _make_xray_explore_job_fn(
        repo_path=Path(single_path_str),
        job_id_holder=[jid],
        bjm=bjm,
        **explore_fn_kwargs,
    )

    def _worker() -> Dict[str, Any]:
        # cast: _make_xray_explore_job_fn is no-untyped-def so fn(None) returns Any;
        # safe — the function contract always produces Dict[str, Any].
        return cast(Dict[str, Any], _explore_fn(None))

    _future = loop.run_in_executor(xray_executor, _worker)
    _future.add_done_callback(_make_explore_done_cb_omni(jid, job_tracker))
    return jid


def _submit_xray_explore_omni(
    aliases: list,
    user: User,
    driver_regex: str,
    evaluator_code: str,
    search_target: str,
    include_patterns: list,
    exclude_patterns: list,
    case_sensitive: bool,
    context_lines: int,
    multiline: bool,
    pcre2: bool,
    path: Optional[str],
    effective_timeout: int,
    max_results: Optional[int],
    max_debug_nodes: int,
    loop: asyncio.AbstractEventLoop,
    job_tracker: Any,  # circular import constraint — see _make_explore_done_cb_omni
    xray_executor: ThreadPoolExecutor,
) -> Dict[str, Any]:
    """Submit one xray_explore background job per alias (Bug #1074 fix).

    Returns a {job_ids, errors} response dict (not yet wrapped in _mcp_response).
    Uses register_job(repo_alias=None) + xray_executor to bypass idx_active_job_per_repo
    and the 5-worker BJM pool (mirrors single-repo Bug #1073 fix).
    """
    bjm = _get_background_job_manager()
    job_ids: list = []
    errors: list = []

    explore_fn_kwargs: Dict[str, Any] = dict(
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
    )

    for single_alias in aliases:
        jid = _submit_one_explore_omni(
            single_alias=single_alias,
            user=user,
            bjm=bjm,
            loop=loop,
            job_tracker=job_tracker,
            xray_executor=xray_executor,
            explore_fn_kwargs=explore_fn_kwargs,
        )
        if jid is None:
            errors.append(
                {
                    "repository_alias": single_alias,
                    "error": "repository_not_found",
                    "message": f"Repository alias {single_alias!r} not found",
                }
            )
        else:
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
        xray_evaluator_validation_failed — Rust evaluator forbidden-construct violation.
    """
    _admission = check_query_admission()
    if not _admission.allowed:
        return _mcp_response(memory_pressure_mcp_payload(_admission))

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

    # ------------------------------------------------------------------
    # Bug #1423: alias normalisation MUST run BEFORE pattern resolution.
    # _resolve_evaluator_code -> XrayPatternService._load_pattern does
    # Path division with repo_alias, which crashed with a raw TypeError
    # when repo_alias was a list (even single-element) -- the omni
    # multi-repo parameter form. Normalize first so pattern resolution
    # always receives a string alias.
    # ------------------------------------------------------------------
    # Issue #1902 P9 review: shared parse+collapse seam (three-strike
    # anti-duplication rule) -- v10.4.5 (Defect 5)'s single-element-list ->
    # single-string ergonomic normalization ({"job_id":"..."}) now lives in
    # ONE place (`_utils._parse_and_collapse_repo_alias`), also used by the
    # sibling handler below and by `handle_analyze_graph`. Multi-element
    # lists still take the multi-repo path ({"job_ids":[...], "errors":[...]}).
    repo_alias_parsed = _parse_and_collapse_repo_alias(repo_alias)

    # Bug #1876 N5: xray_explore is functionally identical to xray_search
    # (same include/exclude-filtered candidate collection) but never called
    # _validate_xray_search_patterns at all -- a non-string item or
    # malformed glob passed straight into the background job with no
    # structured error, and a BARE STRING (e.g. "*.md" instead of
    # ["*.md"]) was silently exploded into one pattern per CHARACTER by
    # the `list(include_patterns)` call further below. Validate on the
    # RAW params.get(...) values (never an `or []`-normalized one, which
    # would mask a falsy-but-invalid input) before any repo-scoped
    # resolution or job submission, mirroring handle_xray_search exactly.
    pattern_validation_error = _validate_xray_search_patterns(
        params.get("include_patterns"), params.get("exclude_patterns")
    )
    if pattern_validation_error is not None:
        return _mcp_response(pattern_validation_error)

    # H5 (consolidated review, Issue #1811/Bug #1812): this handler's own
    # tool_docs document the default-evaluator fallback as intentional
    # ("when omitted, the server substitutes a default...") -- opt in
    # explicitly rather than relying on the function's old unconditional
    # behavior. H3: offloaded to the dedicated xray_executor -- see
    # _resolve_evaluator_code_off_loop's docstring for why.
    evaluator_code, err_resp = await _resolve_evaluator_code_off_loop(
        params,
        _pattern_scope_alias(repo_alias_parsed),
        allow_default_evaluator=True,
        expected_execution_mode="legacy",
    )
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

    # await_seconds accepts int or float in [0.0, 45.0]. Cap lowered from
    # 120.0 to 45.0 in v10.98.0 (Bug #1070) -- see top-of-file comment.
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
                    f"(cap lowered from 120.0 to 45.0 in v10.98.0 -- Bug "
                    f"#1070 -- for longer waits use the async {{job_id}} "
                    f"path), got {await_seconds}"
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
    # repo_alias_parsed was already normalized above (Bug #1423), before
    # pattern resolution ran (Bug 1 fix v10.4.1 origin: repo_alias must
    # never reach _resolve_repo_path un-normalized).

    # ------------------------------------------------------------------
    # 4. Effective timeout + range check  (shared — runs before alias branch)
    # ------------------------------------------------------------------
    effective_timeout, timeout_config_degraded = _resolve_effective_timeout(
        timeout_override
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
        _omni_loop = asyncio.get_running_loop()
        _omni_jt = _get_job_tracker()
        _omni_xe = _get_xray_executor()
        _omni_response_body = _submit_xray_explore_omni(
            aliases=repo_alias_parsed,
            user=user,
            loop=_omni_loop,
            job_tracker=_omni_jt,
            xray_executor=_omni_xe,
            **explore_kwargs,
        )
        if timeout_config_degraded:
            _omni_response_body["configuration_degraded"] = True
        return _mcp_response(_omni_response_body)

    # Single-repo path

    # Story #1039: bare-to-global alias fallback (read-only handler).
    if isinstance(repo_alias_parsed, str) and not repo_alias_parsed.endswith("-global"):
        # Bug #1709: probes via _utils._lazy_module_attr_or_none() (the
        # generalized form of Bug #1693's _lazy_singleton_app_or_none())
        # instead of a bare getattr(_utils.app_module, name, None), which
        # would otherwise permanently construct the process-wide app
        # singleton as a side effect of merely reading it.
        _arm = _utils._lazy_module_attr_or_none("activated_repo_manager")
        _grm = _utils._lazy_module_attr_or_none("golden_repo_manager")
        if _arm is not None and _grm is not None:
            if not _arm.user_has_activated_repo(user.username, repo_alias_parsed):
                from .._global_fallback import try_global_fallback

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
            if timeout_config_degraded:
                inline = {**inline, "configuration_degraded": True}
            return _mcp_response(inline)

    explore_response_body: Dict[str, Any] = {"job_id": job_id}
    if timeout_config_degraded:
        explore_response_body["configuration_degraded"] = True
    return _mcp_response(explore_response_body)
