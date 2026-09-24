"""Shared infrastructure for the xray MCP handler package.

Timeout resolution, evaluator-code resolution, app-singleton/executor/
limiter/job-tracker accessors, and xray result truncation -- used by
every handler submodule in this package (store_xray_pattern, xray_search,
xray_explore, xray_dump_ast, cidx_fetch_cached_payload, cancel_job).

Issue #1935 Part 2: extracted out of the monolithic xray.py (originally,
and briefly out of xray/__init__.py during this same split) -- pure
relocation, zero behaviour change.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, NamedTuple, Optional, cast

if TYPE_CHECKING:
    from code_indexer.server.services.resizable_limiter import ResizableLimiter

from code_indexer.server.services.config_service import get_config_service

from .. import _utils, xray_truncation
from .._utils import _mcp_response

# Issue #1935 Part 2: hardcoded to the package's own name (not `__name__`,
# which for this submodule would be "...xray._infra") so every submodule
# in this package logs under the SAME single logger name the original
# flat xray.py module used -- this codebase's log-audit gate is keyed on
# logger names, and splitting that identity per-submodule would silently
# break it. Every other submodule imports this same `logger` object
# rather than constructing its own.
logger = logging.getLogger("code_indexer.server.mcp.handlers.xray")


def _get_cidx_meta_path() -> Path:
    """Return the mutable cidx-meta base path derived from the golden_repo_manager.

    Extracted as a module-level function so tests can patch it directly.

    Raises:
        RuntimeError: If golden_repo_manager is not configured in the app module.

    Bug #1709: probes via `_utils._lazy_module_attr_or_none()` instead of a
    bare `getattr(_utils.app_module, "golden_repo_manager", None)`, which
    would otherwise permanently construct the process-wide app singleton as
    a side effect of merely reading it.
    """
    grm = _utils._lazy_module_attr_or_none("golden_repo_manager")
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

    Follows the same peek_telemetry_manager() + is_active gating pattern as
    code_indexer.xray.rust_backend._record_identity_failure_metric: lazy
    import (this module is server-only, but keeping the import local avoids
    paying telemetry import cost on the hot path when telemetry is
    disabled) and never raises -- a metrics failure must never break xray
    timeout resolution.

    Args:
        reason: Short failure classification, e.g. "exception".
    """
    try:
        from code_indexer.server.telemetry.manager import (  # noqa: PLC0415
            peek_telemetry_manager,
        )
        from code_indexer.server.telemetry.metrics_instrumentation import (  # noqa: PLC0415
            get_application_metrics,
        )

        telemetry_manager = peek_telemetry_manager()
        if telemetry_manager is None:
            return
        app_metrics = get_application_metrics(telemetry_manager)
        if not app_metrics.is_active:
            return
        app_metrics.record_xray_timeout_config_read_failure(reason=reason)
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
    """Return the effective default xray timeout, read LIVE from ConfigService,
    plus whether the fail-soft fallback was used.

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
            seconds=int(
                get_config_service().get_config().xray_config.xray_timeout_seconds
            ),
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

    from code_indexer.server.services.xray_pattern_service import XrayPatternService

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
) -> "tuple[str, Optional[Dict[str, Any]]]":
    """Resolve evaluator_code from pattern_name or raw evaluator_code --
    see the comment immediately above for the full rationale."""
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

        cidx_meta = cidx_meta_path or _get_cidx_meta_path()
        svc = XrayPatternService(
            cidx_meta,
            refresh_scheduler=(
                None
                if cidx_meta_path is not None
                else _utils._get_app_refresh_scheduler()
            ),
        )
        try:
            if not _seeds_ensured:
                svc.ensure_seed_patterns()
                _seeds_ensured = True
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


def _lazy_singleton_app_or_none() -> Any:
    """The already-constructed `code_indexer.server.app` singleton, or None.

    Bug #1678: `code_indexer.server.app.app` is a PEP 562 `__getattr__` lazy
    singleton (Bug #1638) -- a bare `getattr(_utils.app_module, "app", None)`
    is NOT side-effect-free, since Python invokes `__getattr__` whenever
    normal attribute lookup fails, and `getattr()` only catches the resulting
    AttributeError afterward. Calling that from a lifespan startup path
    (as set_xray_executor/set_xray_cell_limiter do) permanently constructs
    and caches the process-wide singleton the first time an INDEPENDENTLY
    created app (e.g. a test fixture's own `create_app()` call) starts its
    own lifespan -- leaking that fixture's stale services into the singleton
    for the rest of the process.

    Bug #1709 (code review remediation of commit 45e7fa4e, Blocker 1): this
    is now a thin alias for `_utils._lazy_module_attr_or_none("app")` -- the
    generalized form of this exact function, which ALSO recovers via
    `app_module._lazy_values` after a `unittest.mock.patch.object(app_module,
    "app", ...)` (no `create=True`) delattr-then-hasattr teardown sequence
    elsewhere in a test session (see that helper's own docstring for the
    full rationale). This function's original raw `__dict__.get("app")`
    read missed that recovery path and permanently returned `None` for the
    rest of the process once such a teardown had occurred -- kept as a
    named alias (rather than inlined at each call site) so #1693's existing
    call sites need zero changes.
    """
    return _utils._lazy_module_attr_or_none("app")


def set_xray_executor(executor: ThreadPoolExecutor) -> None:
    """Store the dedicated xray ThreadPoolExecutor on app.state (called from lifespan).

    Bug #1070: xray compute must run on a dedicated pool isolated from the 5-worker
    BackgroundJobManager pool. lifespan calls this after constructing the executor.

    Bug #1678: this mirror-write onto the process-wide singleton is a no-op
    (not an error) when that singleton hasn't been constructed yet -- e.g. a
    test fixture that built its own app via `create_app()` directly, whose
    lifespan already set `app.state.xray_executor` on the real, correct app
    object immediately before calling this function.
    """
    app = _lazy_singleton_app_or_none()
    if app is None:
        logger.debug(
            "set_xray_executor: no process-wide app singleton yet -- skipping "
            "mirror write (expected for an independently-constructed app)"
        )
        return
    app.state.xray_executor = executor


def _get_xray_executor() -> ThreadPoolExecutor:
    """Return the dedicated xray ThreadPoolExecutor from app.state.

    Raises:
        RuntimeError: If app or xray_executor is not configured.

    Bug #1693: probes via `_lazy_singleton_app_or_none()` (the same
    side-effect-free helper #1678 introduced for the setters) instead of a
    bare `getattr(_utils.app_module, "app", None)`, which would otherwise
    permanently construct the process-wide app singleton as a side effect
    of merely reading it (see `_lazy_singleton_app_or_none()`'s docstring).
    """
    app = _lazy_singleton_app_or_none()
    if app is None:
        raise RuntimeError("xray_executor not available: app is not configured")
    executor = getattr(app.state, "xray_executor", None)
    if executor is None:
        raise RuntimeError(
            "xray_executor not available: set_xray_executor() was not called during startup"
        )
    return cast(ThreadPoolExecutor, executor)


def set_xray_cell_limiter(limiter: "ResizableLimiter") -> None:
    """Store the xray cell concurrency limiter on app.state (called from lifespan).

    All xray scan executions (xray_search, xray_explore, xray_search_batch cells)
    compete for the same N slots globally. N is driven by xray_worker_threads config.

    Bug #1678: this mirror-write onto the process-wide singleton is a no-op
    (not an error) when that singleton hasn't been constructed yet -- see
    `_lazy_singleton_app_or_none()` and `set_xray_executor()` for the full
    rationale (lifespan already set `app.state.xray_cell_limiter` on the
    real, correct app object immediately before calling this function).
    """
    app = _lazy_singleton_app_or_none()
    if app is None:
        logger.debug(
            "set_xray_cell_limiter: no process-wide app singleton yet -- "
            "skipping mirror write (expected for an independently-"
            "constructed app)"
        )
        return
    app.state.xray_cell_limiter = limiter


def _get_xray_cell_limiter() -> "Optional[ResizableLimiter]":
    """Return the xray cell limiter from app.state, or None if not wired (CLI/test).

    Bug #1693: probes via `_lazy_singleton_app_or_none()` instead of a bare
    `getattr(_utils.app_module, "app", None)`, which would otherwise
    permanently construct the process-wide app singleton as a side effect
    of merely reading it.
    """
    app = _lazy_singleton_app_or_none()
    if app is None:
        return None
    return getattr(app.state, "xray_cell_limiter", None)


def _get_job_tracker() -> Any:
    """Return the live JobTracker from the app module.

    Bug #1070: xray uses register_job() directly (no conflict check) instead of
    submit_job() which calls register_job_if_no_conflict() — that gate serializes
    concurrent xray calls on the same repo, which is wrong for read-only operations.

    Bug #1709: probes via `_utils._lazy_module_attr_or_none()` instead of a
    direct unconditional `_utils.app_module.job_tracker` attribute access,
    which would otherwise permanently construct the process-wide app
    singleton as a side effect of merely reading it (same fix already
    applied to `_get_background_job_manager()` immediately below).

    Return type is `Any` (not `JobTracker`) because importing that class
    here would pull `server.repositories` into this module, which is not
    safe -- the same circular-import constraint documented on
    `_resolve_repo_path()` immediately below.
    """
    return _utils._lazy_module_attr_or_none("job_tracker")


def _resolve_repo_path(alias: str) -> Optional[str]:
    """Resolve a global repo alias to its versioned snapshot path.

    Delegates to repos._resolve_golden_repo_path so the alias manager is
    exercised through the canonical code path.

    Returns None when the alias is unknown.

    The import is local (not module-level) because
    `code_indexer.server.mcp.handlers.repos` itself imports from this
    package's sibling modules, which would otherwise form an import cycle.
    """
    from code_indexer.server.mcp.handlers.repos import _resolve_golden_repo_path

    return cast(Optional[str], _resolve_golden_repo_path(alias))


def _get_background_job_manager() -> Any:
    """Return the live BackgroundJobManager from the app module.

    Extracted for easy mocking in unit tests.

    Bug #1709: probes via `_utils._lazy_module_attr_or_none()` instead of a
    direct unconditional `_utils.app_module.background_job_manager`
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
    return _utils._lazy_module_attr_or_none("background_job_manager")


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
    from code_indexer.services.path_pattern_matcher import (
        InvalidPatternError,
        PathPatternMatcher,
    )

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


# Bug #1928: truncation logic lives in xray_truncation.py (shared by
# xray.py/xray_graph.py/xray_batch.py) -- this is a thin wrapper that does
# this module's own app-state payload_cache lookup and delegates. See
# xray_truncation.py's module docstring for the full contract (whole,
# unmodified entries per cache page; each page its own PayloadCache row;
# inline budget = payload_max_fetch_size_chars; inline_entry_truncated).


def _truncate_xray_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Apply PayloadCache truncation to matches[]/evaluation_errors[].

    See xray_truncation.truncate_result_fields() for the shared
    mechanics -- INCLUDING its own `payload_cache is None -> return a
    bounded, cache_unavailable=True result` guard (Bug #1928 P3), so a
    missing/unconfigured payload_cache here is handled by the shared
    function's own guard.

    Bug #1928 round 3 (P1): a page-set write failure (whole-entry pages
    + the pages-v1 manifest, one atomic batch) raises PageSetStoreError
    -- caught here and surfaced as an explicit error, never a
    cache_handle pointing at data that was not durably written.
    """
    # Bug #1709: probes via _lazy_singleton_app_or_none() instead of a bare
    # _utils.app_module.app.state attribute chain, which would otherwise
    # permanently construct the process-wide app singleton as a side effect
    # of merely reading it (see _lazy_singleton_app_or_none()'s docstring).
    payload_cache = getattr(
        getattr(_lazy_singleton_app_or_none(), "state", None), "payload_cache", None
    )
    try:
        return xray_truncation.truncate_result_fields(
            result, payload_cache, ["matches", "evaluation_errors"]
        )
    except xray_truncation.PageSetStoreError as exc:
        logger.error("xray_search truncation: page-set store failed: %s", exc)
        # Bug #1928 final round (Opus P4.6): preserve the non-truncated
        # metadata the search already produced -- only matches/
        # evaluation_errors are genuinely undeliverable (the write failed).
        base_metadata = getattr(exc, "base_metadata", {})
        return {
            **base_metadata,
            "success": False,
            "error": "cache_store_failed",
            "message": f"Failed to store the truncated result in cache: {exc}",
        }
