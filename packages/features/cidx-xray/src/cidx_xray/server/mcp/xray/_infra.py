"""Shared infrastructure for the xray MCP handler package.

Timeout resolution, evaluator-code resolution, app-singleton/executor/
limiter/job-tracker accessors, and xray result truncation -- used by
every handler submodule in this package (store_xray_pattern, xray_search,
xray_explore, xray_dump_ast, cidx_fetch_cached_payload, cancel_job).

Issue #1935 Part 2: extracted out of the monolithic xray.py (originally,
and briefly out of xray/__init__.py during this same split) -- pure
relocation, zero behaviour change.

Modular plan phase 4c: moved into the cidx-xray feature. The timeout default
comes from the feature's config section (``config`` capability), the metric
through the ``telemetry`` capability, the executor and cell limiter from the
feature's own runtime (``on_startup``), and the server's app services
(job tracker, job manager, repository resolution, payload cache) through the
server platform (``cidx_xray._platform``).
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional, cast

from cidx_xray import _platform
from cidx_xray.config import TIMEOUT_SECONDS
from cidx_xray.server import runtime

from .._common import (
    _lazy_app_attr,
    _lazy_singleton_app_or_none as _lazy_singleton_app_or_none,
    _app_payload_cache,
    _mcp_response,
    truncate_paged_result,
)

# Issue #1935 Part 2: hardcoded to the package's own name (not `__name__`,
# which for this submodule would be "...xray._infra") so every submodule
# in this package logs under the SAME single logger name the original
# flat xray.py module used -- this codebase's log-audit gate is keyed on
# logger names, and splitting that identity per-submodule would silently
# break it. Every other submodule imports this same `logger` object
# rather than constructing its own.
logger = logging.getLogger("cidx_xray.server.mcp.xray")


def _get_cidx_meta_path() -> Path:
    """Return the mutable cidx-meta base path derived from the golden_repo_manager.

    Extracted as a module-level function so tests can patch it directly.

    Raises:
        RuntimeError: If golden_repo_manager is not configured in the app module.

    Bug #1709: probes the server platform's ``app_attr`` (a read of the
    already-built value) instead of a bare attribute read of the app module,
    which would otherwise permanently construct the process-wide app
    singleton as a side effect of merely reading it.
    """
    grm = _lazy_app_attr("golden_repo_manager")
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

# Story #1494 AC1 (Finding A2, GIL-blocking analysis report, HIGH):
# tree-sitter 0.21.3 holds the GIL for the ENTIRE parse duration (measured
# 0.89x thread scaling -- no release at all; 159ms whole-process freeze
# parsing a 759KB file). handle_xray_dump_ast() parses in-process with no
# size cap on the source file -- max_nodes bounds only the serialized
# output, not the parse itself. The Rust xray-cli subprocess (the safe
# scan path used by handle_xray_search/handle_xray_explore) has no
# AST-dump capability -- verified by inspecting rust/xray-cli/src/main.rs,
# which only supports --dynlib/--files/--json evaluator-scan mode -- so
# per the report's mitigation 2, this caps the in-process parse by file
# size instead of relocating it to a subprocess.
_DUMP_AST_MAX_FILE_SIZE_BYTES = 256 * 1024

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


def _record_xray_timeout_config_read_failure_metric(reason: str) -> None:
    """Record a cidx.xray.timeout_config_read_failures OTEL counter event
    (consolidated review, Issue #1811/Bug #1812, new finding #7).

    A WARNING log alone is insufficient observability at fleet scale
    (~900 repos): a node whose ConfigService stays permanently broken
    would silently ignore the operator-configured xray_timeout_seconds
    override on EVERY xray request, forever, with only a log line to
    notice. This never changes the fail-soft fallback itself (Bug #1399) --
    it only makes repeated failures observable beyond log-scraping.

    Recorded through the feature's ``telemetry`` capability, which answers
    None unless telemetry is running and active (the same gating as
    cidx_xray.rust_backend._record_identity_failure_metric), and never
    raises -- a metrics failure must never break xray timeout resolution.

    Args:
        reason: Short failure classification, e.g. "exception".
    """
    try:
        app_metrics = runtime.application_metrics()
        if app_metrics is None:
            return
        record = getattr(app_metrics, "record_xray_timeout_config_read_failure", None)
        if record is not None:
            record(reason=reason)
    except Exception as exc:  # never break xray timeout resolution
        logger.debug(
            "Failed to record xray timeout config read failure metric: %s", exc
        )


class _TimeoutResolution(NamedTuple):
    """Result of resolving the effective default xray timeout.

    Consolidated review (Issue #1811/Bug #1812, new finding #7 follow-up):
    `degraded=True` means the ConfigService read failed and the hardcoded
    `_DEFAULT_TIMEOUT_SECONDS` was silently substituted. Callers that reach
    this state must surface `configuration_degraded: true` in their
    immediate response -- an OTEL counter and a WARNING log are invisible
    to the caller making THIS specific request.
    """

    seconds: int
    degraded: bool


def _resolve_default_xray_timeout_seconds_detailed() -> _TimeoutResolution:
    """Return the effective default xray timeout, read LIVE from the feature's
    config section, plus whether the fail-soft fallback was used.

    Bug #1399: xray_config.xray_timeout_seconds was settable/validated via
    the Web UI Config screen but never consulted here -- the effective
    default was always the hardcoded _DEFAULT_TIMEOUT_SECONDS module
    constant. This helper closes that gap for every call site that falls
    back to a default when the caller omits timeout_seconds.

    Fails soft to _DEFAULT_TIMEOUT_SECONDS on any read failure (no
    config_service wired yet, DB outage, etc.) so a config-layer problem
    never blocks an xray search request -- but reports `degraded=True` so
    the caller can decide whether/how to surface that fact.
    """
    try:
        return _TimeoutResolution(
            seconds=int(cast(int, runtime.config_section()[TIMEOUT_SECONDS])),
            degraded=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "xray: failed to read configured xray_timeout_seconds, falling "
            "back to hardcoded default %ds: %s",
            _DEFAULT_TIMEOUT_SECONDS,
            exc,
        )
        _record_xray_timeout_config_read_failure_metric(reason="exception")
        return _TimeoutResolution(seconds=_DEFAULT_TIMEOUT_SECONDS, degraded=True)


def _resolve_default_xray_timeout_seconds() -> int:
    """Return the effective default xray timeout, read LIVE from ConfigService.

    Thin wrapper around `_resolve_default_xray_timeout_seconds_detailed()`
    for call sites that only need the timeout value (not the degraded
    flag). See that function's docstring for the fail-soft contract.
    """
    return _resolve_default_xray_timeout_seconds_detailed().seconds


def _resolve_effective_timeout(timeout_override: Optional[int]) -> "tuple[int, bool]":
    """Return (effective_timeout, configuration_degraded) for a call site.

    Centralizes the override-vs-config-default choice so every xray_search/
    xray_explore branch (single-repo, multi-repo) surfaces the SAME
    `configuration_degraded` semantics: an explicit caller override never
    degrades (the config default was never consulted); an omitted override
    degrades only when the ConfigService read itself failed.
    """
    if timeout_override is not None:
        return timeout_override, False
    resolution = _resolve_default_xray_timeout_seconds_detailed()
    return resolution.seconds, resolution.degraded


# Guard so ensure_seed_patterns() is called at most once per process lifetime.
_seeds_ensured = False


def _pattern_scope_alias(repo_alias_parsed: Any) -> str:
    """Return the alias to use for pattern-library resolution (Bug #1423).

    Pattern resolution (_resolve_evaluator_code -> XrayPatternService.
    _load_pattern) does ``Path / repo_alias`` under the hood, which requires
    a single string. When repo_alias_parsed is already a plain string
    (single-repo call, or an omni list that collapsed to one element),
    use it directly — this preserves repo-specific-scope priority over
    __any__ per XrayPatternService._load_pattern's documented resolution
    order.

    For a genuine multi-element list (omni multi-repo search), there is no
    single "owning" repo, so pattern resolution falls back to the
    cross-repo __any__ scope instead of crashing (the original bug) or
    arbitrarily picking one alias out of the list.
    """
    if isinstance(repo_alias_parsed, str):
        return repo_alias_parsed

    from cidx_xray.server.pattern_service import XrayPatternService

    return str(XrayPatternService.ANY_SCOPE)


# Resolve evaluator_code from pattern_name or raw evaluator_code.
#
# Returns ``(evaluator_code, None)`` on success, or ``("", error_response)``
# where *error_response* is a complete ``_mcp_response`` dict that the
# caller must return immediately.
#
# Consolidated review finding H5 (Issue #1811/Bug #1812): when NEITHER
# ``pattern_name`` nor ``evaluator_code`` is supplied, the SAFE-BY-DEFAULT
# behavior (``allow_default_evaluator=False``) is to reject the request
# with ``evaluator_code_required`` -- never silently substitute
# ``default_evaluator`` (Rule 2, anti-fallback). Pass
# ``allow_default_evaluator=True`` ONLY from a call site that has its own
# documented contract for this fallback (currently ``handle_xray_search``
# and ``handle_xray_explore`` -- both document "when omitted, the server
# substitutes a default..." in their tool_docs). This makes the rule live
# in exactly ONE place instead of being duplicated by every caller that
# does NOT want the fallback (e.g. the REST route, which no longer needs
# its own hand-rolled pre-check).
def _resolve_evaluator_code(
    params: Dict[str, Any],
    repo_alias: str,
    default_evaluator: str = _DEFAULT_EVALUATOR_CODE,
    allow_default_evaluator: bool = False,
    expected_execution_mode: Optional[str] = None,
    cidx_meta_path: Optional[Path] = None,
    # Any: the RefreshScheduler, untyped to keep global_repos out of this import
    refresh_scheduler: Optional[Any] = None,
) -> "tuple[str, Optional[Dict[str, Any]]]":
    """Resolve evaluator_code from pattern_name or raw evaluator_code --
    see the comment immediately above for the full rationale. A captured
    worker path (*cidx_meta_path*) brings its own *refresh_scheduler*, so
    seed writes take the catalog lock there too."""
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
        from cidx_xray.server.pattern_service import XrayPatternService

        cidx_meta = cidx_meta_path or _get_cidx_meta_path()
        svc = XrayPatternService(
            cidx_meta,
            refresh_scheduler=(
                refresh_scheduler
                if cidx_meta_path is not None
                else _platform.server_platform().refresh_scheduler()
            ),
        )
        seeds_refused = False
        try:
            if not _seeds_ensured:
                # ^ why False = refused under an exclusive catalog holder; retried next call
                _seeds_ensured = svc.ensure_seed_patterns()
                seeds_refused = not _seeds_ensured
            if expected_execution_mode is not None:
                declared_mode = svc.get_pattern_execution_mode(repo_alias, pattern_name)
                if declared_mode != expected_execution_mode:
                    return (
                        "",
                        _mcp_response(
                            {
                                "error": "pattern_mode_mismatch",
                                "message": (
                                    f"pattern {pattern_name!r} declares execution_mode "
                                    f"{declared_mode!r}, but this operation requires "
                                    f"{expected_execution_mode!r}"
                                ),
                            }
                        ),
                    )
            evaluator_code, _ = svc.resolve_and_prepare_pattern(
                repo_alias=repo_alias,
                pattern_name=pattern_name,
                pattern_params=pattern_params,
            )
        except ValueError as exc:
            error_key = str(exc).split(":")[0]
            if (
                seeds_refused
                and error_key == "pattern_not_found"
                and pattern_name in XrayPatternService.SEED_PATTERN_NAMES
            ):
                return "", _mcp_response(
                    {
                        "error": "catalog_locked",
                        "message": (
                            f"seed pattern {pattern_name!r} is not in place yet: the "
                            "cidx-meta catalog is briefly exclusive; retry shortly"
                        ),
                    }
                )
            return "", _mcp_response({"error": error_key, "message": str(exc)})
        return (evaluator_code, None)

    if raw_evaluator_code.strip():
        return (raw_evaluator_code, None)

    if allow_default_evaluator:
        return (default_evaluator, None)

    return (
        "",
        _mcp_response(
            {
                "error": "evaluator_code_required",
                "message": "Either evaluator_code or pattern_name must be provided",
            }
        ),
    )


# H3 (consolidated review, Issue #1811/Bug #1812, Codex): async wrapper
# that runs `_resolve_evaluator_code` on the DEDICATED `xray_executor`
# (this file's own established offloading idiom --
# `loop.run_in_executor(xray_executor, ...)`, already used for the real
# search execution below) instead of directly on the event loop thread.
#
# `_resolve_evaluator_code` can do real filesystem I/O
# (`XrayPatternService.ensure_seed_patterns`'s mkdir/write_text,
# `_load_pattern`'s YAML read) AND spawn a git subprocess
# (`ensure_seed_patterns`'s git add + git commit) whenever `pattern_name`
# is supplied. `cidx-meta` lives on a hard NFSv3 mount, where any of
# these calls can block FOREVER -- calling this directly inside an
# `async def` handler (as both `handle_xray_search`/`handle_xray_explore`
# previously did) blocks the WHOLE event loop, stalling every other
# request on this node (this project's explicit async-I/O invariant).
#
# Only offloads when `pattern_name` is supplied AND `evaluator_code` is
# NOT -- mirroring `_resolve_evaluator_code`'s own internal guard exactly
# (`if pattern_name and raw_evaluator_code.strip(): return
# mutually_exclusive_params`). That is the ONLY branch capable of any I/O
# at all: when both are given, the function returns the
# mutually-exclusive error immediately with zero I/O; when neither is
# given, it returns the caller-supplied/default evaluator verbatim, also
# zero I/O. Offloading either of those cases would be a pointless
# thread-pool round-trip for no safety benefit.
async def _resolve_evaluator_code_off_loop(
    params: Dict[str, Any],
    repo_alias: str,
    allow_default_evaluator: bool = False,
    expected_execution_mode: Optional[str] = None,
    cidx_meta_path: Optional[Path] = None,
) -> "tuple[str, Optional[Dict[str, Any]]]":
    """Runs `_resolve_evaluator_code` off the event loop when it might do
    real I/O -- see the comment immediately above for the full rationale."""
    pattern_name = params.get("pattern_name")
    raw_evaluator_code = (params.get("evaluator_code") or "").strip()
    resolver_kwargs: Dict[str, Any] = {
        "allow_default_evaluator": allow_default_evaluator,
        "expected_execution_mode": expected_execution_mode,
    }
    if cidx_meta_path is not None:
        resolver_kwargs["cidx_meta_path"] = cidx_meta_path
    if not (pattern_name and not raw_evaluator_code):
        return _resolve_evaluator_code(
            params,
            repo_alias,
            **resolver_kwargs,
        )
    loop = asyncio.get_running_loop()
    xray_executor = _get_xray_executor()
    return await loop.run_in_executor(
        xray_executor,
        lambda: _resolve_evaluator_code(
            params,
            repo_alias,
            **resolver_kwargs,
        ),
    )


# await_seconds range and poll interval.
# Bug #1070: _AWAIT_SECONDS_MAX lowered from 120.0 to 45.0. Handlers are now async,
# so the polling loop uses asyncio.sleep instead of time.sleep and no longer holds
# _mcp_executor threads. The cap of 45.0 avoids 504s at the ALB 60s hard timeout.
# Task #35 (v10.3.2): await_seconds accepts int OR float — typed as float.
_AWAIT_SECONDS_MIN: float = 0.0
_AWAIT_SECONDS_MAX: float = 45.0
_AWAIT_SECONDS_WARN_THRESHOLD: float = 30.0
_AWAIT_POLL_INTERVAL = 0.05


def set_xray_executor(executor: Optional[ThreadPoolExecutor]) -> None:
    """Install (None: remove) the dedicated X-Ray ThreadPoolExecutor.

    Bug #1070: xray compute runs on a dedicated pool isolated from the
    5-worker BackgroundJobManager pool. The feature's ``on_startup`` builds
    it and installs it here; ``on_shutdown`` removes it. Process wiring of
    this node only (``runtime.EXECUTOR``), never request state.
    """
    if executor is None:
        runtime.EXECUTOR.uninstall()
    else:
        runtime.EXECUTOR.install(executor)


def _get_xray_executor() -> ThreadPoolExecutor:
    """Return the dedicated xray ThreadPoolExecutor.

    Raises:
        RuntimeError: the feature has not started in this process (its
            ``on_startup`` installs the pool).
    """
    executor = runtime.executor()
    if executor is None:
        raise RuntimeError(
            "xray_executor not available: the xray feature has not started "
            "in this process (its on_startup installs the executor)"
        )
    return executor


def set_xray_cell_limiter(limiter: Optional[Any]) -> None:
    """Install (None: remove) the xray cell concurrency limiter.

    All xray scan executions (xray_search, xray_explore, xray_search_batch
    cells) compete for the same N slots on this node. N is the
    ``xray_worker_threads`` setting; the feature's ``on_startup`` builds it.
    """
    if limiter is None:
        runtime.CELL_LIMITER.uninstall()
    else:
        runtime.CELL_LIMITER.install(limiter)


def _get_xray_cell_limiter() -> Optional[Any]:
    """Return the xray cell limiter, or None if not wired (CLI/test)."""
    return runtime.cell_limiter()


def _get_job_tracker() -> Any:
    """Return the live JobTracker from the app module.

    Bug #1070: xray uses register_job() directly (no conflict check) instead of
    submit_job() which calls register_job_if_no_conflict() — that gate serializes
    concurrent xray calls on the same repo, which is wrong for read-only operations.

    Bug #1709: probes the server platform's ``app_attr`` instead of a
    direct unconditional ``app_module.job_tracker`` attribute access,
    which would otherwise permanently construct the process-wide app
    singleton as a side effect of merely reading it (same fix already
    applied to `_get_background_job_manager()` immediately below).

    Return type is `Any` (not `JobTracker`) because importing that class
    here would pull `server.repositories` into this module, which is not
    safe -- the same circular-import constraint documented on
    `_resolve_repo_path()` immediately below.
    """
    return _lazy_app_attr("job_tracker")


def _resolve_repo_path(alias: str) -> Optional[str]:
    """Resolve a global repo alias to its on-disk path.

    Uses the SAME registry-backed resolution as search_code/regex_search
    (the server's ``resolve_alias_or_index_path``, through the platform):
    the AliasManager pointer file first, falling back to the registry row's
    ``index_path`` when the pointer is missing (Bug #1315). The prior
    pointer-only path (``repos._resolve_golden_repo_path``) returned None for
    global repos whose alias pointer was never created by the seeder/bulk-
    provisioning path, so xray raised ``repository_not_found`` on repos that
    search_code/regex_search resolve fine (e.g. ``example-global``).

    Returns None when the alias is unknown to the registry and no ``index_path``
    fallback resolves.
    """
    return _platform.server_platform().resolve_repo_path(alias)


def _get_background_job_manager() -> Any:
    """Return the live BackgroundJobManager from the app module.

    Extracted for easy mocking in unit tests.

    Bug #1709: probes the server platform's ``app_attr`` instead of a
    direct unconditional ``app_module.background_job_manager``
    attribute access, which -- for the same PEP-562 `__getattr__` reason as
    the other lazy attribute probes fixed in this module -- would otherwise
    permanently construct the process-wide app singleton as a side effect
    of merely reading it. In real production usage this handler only ever
    runs after the app singleton is fully constructed, so the returned
    value is identical; only premature/test access now safely observes
    `None` instead of forcing construction.

    Return type is `Any` (not `BackgroundJobManager`) for the same
    circular-import reason as `_get_job_tracker()` immediately above --
    `server.repositories` cannot be imported at module level here.

    Returns:
        The live BackgroundJobManager. Callers in this module dereference
        the result unconditionally (e.g. `bjm.register_child_process(...)`,
        `bjm.cancel_job(...)`) without a None-check -- this is safe because
        a real MCP request handler only ever executes after server startup
        has genuinely constructed and wired the singleton; a `None` result
        is reachable only via premature/test access (see above), which no
        real request path can trigger. A guard here that raised
        `RuntimeError` (mirroring `_get_xray_executor()`'s shape) was
        deliberately NOT added: doing so would turn this function into an
        eager-construction trigger again for the exact test scenarios this
        fix targets, where `None` is the correct, expected, and harmless
        transient value.
    """
    return _lazy_app_attr("background_job_manager")


async def _await_xray_future(
    future: "asyncio.Future[Any]", await_seconds: float
) -> Optional[Dict[str, Any]]:
    """Await an xray compute future with a deadline, yielding the event loop between polls.

    Bug #1070: replaces the synchronous time.sleep polling loop. This async version
    uses asyncio.sleep so no _mcp_executor thread is held during the wait.

    `future` is typed `asyncio.Future[Any]` (not `asyncio.Future[Dict[str, Any]]`)
    because `loop.run_in_executor(...)` at every call site returns a bare
    `Future` whose result type mypy cannot narrow further than `Any` --
    the `cast(...)` below on `future.result()` is what recovers the
    concrete `Optional[Dict[str, Any]]` return type for this function.

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


def _validate_xray_search_patterns(
    include_patterns: Any, exclude_patterns: Any
) -> Optional[Dict[str, Any]]:
    """Validates and compiles xray_search's include_patterns/exclude_patterns
    ONCE at the front door (Bug #1876 item 4), mirroring
    `handlers.search._validate_regex_args`. Callers MUST pass the RAW
    `params.get(...)` value for each field, never an `or []`-normalized
    one -- normalizing first would silently turn a falsy-but-invalid input
    (`""`, `0`, `False`) into "no filter" before this function ever sees
    it. `None` (field omitted) is the only falsy value treated as valid.
    A bare string (e.g. `"*.py"` instead of `["*.py"]`) would otherwise
    silently iterate per CHARACTER through `PathPatternMatcher.
    compile_patterns`; a non-string item (e.g. `[None]`) or a malformed
    pattern (unbalanced brace, gitignore negation/comment syntax) would
    otherwise pass straight into the background job, where an exclude
    failing OPEN silently widens the search with no signal to the caller.

    `include_patterns`/`exclude_patterns` are typed `Any` (not
    `Optional[List[str]]`) because they arrive here as raw, unvalidated
    MCP request JSON -- this function's own job is to validate that they
    actually ARE a list of strings before anything downstream assumes so;
    a caller passing a non-list is exactly the case being guarded against
    (see the `isinstance` check below), so a precise static type would be
    a lie about what this function actually receives.

    Returns `None` on success, or a structured `{"error": ..., "message":
    ...}` dict on the first invalid field.
    """
    from cidx_xray._platform import InvalidPatternError, PathPatternMatcher

    matcher = PathPatternMatcher()
    for field_name, patterns in (
        ("include_patterns", include_patterns),
        ("exclude_patterns", exclude_patterns),
    ):
        if patterns is None:
            continue
        if not isinstance(patterns, list):
            return {
                "error": f"{field_name}_invalid",
                "message": f"{field_name} must be a list of strings",
            }
        try:
            matcher.compile_patterns(patterns)
        except InvalidPatternError as e:
            return {
                "error": f"{field_name}_invalid",
                "message": str(e),
            }
    return None


# Bug #1928: truncation logic lives in the core's paged payload cache
# (shared by xray.py/xray_graph.py/xray_batch.py through
# ``truncate_paged_result``) -- this is a thin wrapper that supplies this
# module's own app-state payload_cache lookup.


def _truncate_xray_result(
    result: Dict[str, Any], *, owner: Optional[str] = None
) -> Dict[str, Any]:
    """Apply PayloadCache truncation to matches[]/evaluation_errors[].

    `owner` is the username of the requesting user; only that user can
    fetch the cached pages. See ``truncate_paged_result`` for the shared
    mechanics, including the missing-payload-cache guard (Bug #1928 P3)
    and the page-set write failure (Bug #1928 round 3, P1).
    """
    return truncate_paged_result(
        result,
        ["matches", "evaluation_errors"],
        owner=owner,
        label="xray_search",
        payload_cache=_app_payload_cache(),
        logger=logger,
    )
