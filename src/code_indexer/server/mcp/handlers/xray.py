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
from typing import TYPE_CHECKING, Any, Callable, Dict, NamedTuple, Optional, cast

if TYPE_CHECKING:
    from code_indexer.server.services.resizable_limiter import ResizableLimiter

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.query_admission_gate import (
    check_query_admission,
    memory_pressure_mcp_payload,
)
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


def _resolve_evaluator_code(
    params: Dict[str, Any],
    repo_alias: str,
    default_evaluator: str = _DEFAULT_EVALUATOR_CODE,
    allow_default_evaluator: bool = False,
) -> "tuple[str, Optional[Dict[str, Any]]]":
    """Resolve evaluator_code from pattern_name or raw evaluator_code.

    Returns ``(evaluator_code, None)`` on success, or
    ``("", error_response)`` where *error_response* is a complete
    ``_mcp_response`` dict that the caller must return immediately.

    Consolidated review finding H5 (Issue #1811/Bug #1812): when NEITHER
    ``pattern_name`` nor ``evaluator_code`` is supplied, the SAFE-BY-DEFAULT
    behavior (``allow_default_evaluator=False``) is to reject the request
    with ``evaluator_code_required`` -- never silently substitute
    ``default_evaluator`` (Rule 2, anti-fallback). Pass
    ``allow_default_evaluator=True`` ONLY from a call site that has its own
    documented contract for this fallback (currently ``handle_xray_search``
    and ``handle_xray_explore`` -- both document "when omitted, the server
    substitutes a default..." in their tool_docs). This makes the rule live
    in exactly ONE place instead of being duplicated by every caller that
    does NOT want the fallback (e.g. the REST route, which no longer needs
    its own hand-rolled pre-check).
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


async def _resolve_evaluator_code_off_loop(
    params: Dict[str, Any],
    repo_alias: str,
    allow_default_evaluator: bool = False,
) -> "tuple[str, Optional[Dict[str, Any]]]":
    """H3 (consolidated review, Issue #1811/Bug #1812, Codex): async
    wrapper that runs `_resolve_evaluator_code` on the DEDICATED
    `xray_executor` (this file's own established offloading idiom --
    `loop.run_in_executor(xray_executor, ...)`, already used for the real
    search execution below) instead of directly on the event loop thread.

    `_resolve_evaluator_code` can do real filesystem I/O
    (`XrayPatternService.ensure_seed_patterns`'s mkdir/write_text,
    `_load_pattern`'s YAML read) AND spawn a git subprocess
    (`ensure_seed_patterns`'s git add + git commit) whenever `pattern_name`
    is supplied. `cidx-meta` lives on a hard NFSv3 mount, where any of
    these calls can block FOREVER -- calling this directly inside an
    `async def` handler (as both `handle_xray_search`/`handle_xray_explore`
    previously did) blocks the WHOLE event loop, stalling every other
    request on this node (this project's explicit async-I/O invariant).

    Only offloads when `pattern_name` is supplied AND `evaluator_code` is
    NOT -- mirroring `_resolve_evaluator_code`'s own internal guard
    exactly (`if pattern_name and raw_evaluator_code.strip(): return
    mutually_exclusive_params`). That is the ONLY branch capable of any
    I/O at all: when both are given, the function returns the
    mutually-exclusive error immediately with zero I/O; when neither is
    given, it returns the caller-supplied/default evaluator verbatim,
    also zero I/O. Offloading either of those cases would be a pointless
    thread-pool round-trip for no safety benefit.
    """
    pattern_name = params.get("pattern_name")
    raw_evaluator_code = (params.get("evaluator_code") or "").strip()
    if not (pattern_name and not raw_evaluator_code):
        return _resolve_evaluator_code(
            params, repo_alias, allow_default_evaluator=allow_default_evaluator
        )
    loop = asyncio.get_running_loop()
    xray_executor = _get_xray_executor()
    return await loop.run_in_executor(
        xray_executor,
        lambda: _resolve_evaluator_code(
            params, repo_alias, allow_default_evaluator=allow_default_evaluator
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
    applied to `_get_background_job_manager()` immediately above).
    """
    return _utils._lazy_module_attr_or_none("job_tracker")


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

    Bug #1709: probes via `_utils._lazy_module_attr_or_none()` instead of a
    direct unconditional `_utils.app_module.background_job_manager`
    attribute access, which -- for the same PEP-562 `__getattr__` reason as
    the other lazy attribute probes fixed in this module -- would otherwise
    permanently construct the process-wide app singleton as a side effect
    of merely reading it. In real production usage this handler only ever
    runs after the app singleton is fully constructed, so the returned
    value is identical; only premature/test access now safely observes
    `None` instead of forcing construction.

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
    4. Pre-flight evaluator validation via validate_rust_evaluator.
    5. Job submission via BackgroundJobManager.
    6. Return {job_id}.

    Error codes:
        auth_required           — unauthenticated or missing query_repos.
        invalid_search_target   — search_target not 'content' or 'filename'.
        timeout_out_of_range    — timeout_seconds outside [10, 600].
        max_files_out_of_range  — max_files provided but < 1.
        repository_not_found    — alias cannot be resolved.
        xray_extras_not_installed — tree-sitter extras not available.
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
    repo_alias_parsed = _parse_json_string_array(repo_alias)
    # v10.4.5 (Defect 5): normalize single-element list to single-string for
    # ergonomic single-repo response shape ({"job_id":"..."}). Multi-element
    # lists still take the multi-repo path ({"job_ids":[...], "errors":[...]}).
    if isinstance(repo_alias_parsed, list) and len(repo_alias_parsed) == 1:
        candidate = repo_alias_parsed[0]
        if isinstance(candidate, str) and candidate:
            repo_alias_parsed = candidate

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
    # repo_alias_parsed was already normalized above (Bug #1423), before
    # pattern resolution ran — accepts plain string, list of strings, or
    # JSON-encoded string array (e.g. '["repo-a", "repo-b"]').
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
        effective_timeout_multi, timeout_config_degraded_multi = (
            _resolve_effective_timeout(timeout_override)
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
        # Bug #1074: use register_job(repo_alias=None) + xray_executor to bypass
        # idx_active_job_per_repo and the 5-worker BJM pool (mirrors single-repo
        # fix from Bug #1073).
        # ------------------------------------------------------------------
        bjm_multi = _get_background_job_manager()
        job_tracker_multi = _get_job_tracker()
        xray_executor_multi = _get_xray_executor()
        loop_multi = asyncio.get_running_loop()
        job_ids: list = []
        errors: list = []

        def _make_search_job_fn(  # type: ignore[no-untyped-def]
            rp: Path, t: int, jid: str, bjm: Any
        ):
            def _job() -> Dict[str, Any]:  # type: ignore[no-untyped-def]
                from code_indexer.xray.search_engine import XRaySearchEngine as _E

                _limiter = _get_xray_cell_limiter()
                _slot = False
                if _limiter is not None:
                    _slot = _limiter.acquire(timeout=float(t))
                    if not _slot:
                        return {
                            "error": "xray_cell_queue_timeout",
                            "message": (
                                f"Timed out waiting for an xray worker slot after "
                                f"{t}s — server busy with other xray jobs."
                            ),
                        }

                def _on_spawned(proc) -> None:  # type: ignore[no-untyped-def]
                    bjm.register_child_process(jid, proc)

                try:
                    # Bug #1590 AC3: transition to "running" only once
                    # execution actually starts (after the cell-limiter slot
                    # is held, before Phase 1 begins) -- so the dashboard can
                    # distinguish "queued, no worker slot yet" from
                    # "actively executing". Code-review finding F2: this
                    # call MUST be inside this try block -- placed before it,
                    # a raising update_status() (e.g. a SQLite write
                    # failure) would skip the finally below and leak the
                    # held xray cell-limiter slot forever.
                    _get_job_tracker().update_status(jid, status="running")
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
                        progress_callback=None,
                        max_files=max_results,
                        on_process_spawned=_on_spawned,
                    )
                finally:
                    bjm.unregister_child_processes(jid)
                    if _slot and _limiter is not None:
                        _limiter.release()
                return _truncate_xray_result(result)

            return _job

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
            jid = str(uuid.uuid4())
            job_tracker_multi.register_job(
                job_id=jid,
                operation_type="xray_search",
                username=user.username,
                repo_alias=None,  # NULL bypasses idx_active_job_per_repo; xray is read-only
                metadata={"repo_alias": single_alias},
            )

            _job_fn = _make_search_job_fn(
                single_repo_path, effective_timeout_multi, jid, bjm_multi
            )
            _future = loop_multi.run_in_executor(xray_executor_multi, _job_fn)

            def _make_search_done_cb(j: str) -> Any:  # type: ignore[no-untyped-def]
                def _cb(fut: "asyncio.Future[Any]") -> None:  # type: ignore[no-untyped-def]
                    if fut.cancelled() or (
                        not fut.cancelled() and fut.exception() is not None
                    ):
                        exc = fut.exception() if not fut.cancelled() else None
                        job_tracker_multi.fail_job(j, str(exc) if exc else "cancelled")
                    else:
                        job_tracker_multi.complete_job(j, fut.result())

                return _cb

            _future.add_done_callback(_make_search_done_cb(jid))
            job_ids.append(jid)

        multi_response_body: Dict[str, Any] = {"job_ids": job_ids, "errors": errors}
        if timeout_config_degraded_multi:
            multi_response_body["configuration_degraded"] = True
        return _mcp_response(multi_response_body)

    # ------------------------------------------------------------------
    # Single-repo path (string alias)
    # ------------------------------------------------------------------

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

        def _on_spawned(proc) -> None:  # type: ignore[no-untyped-def]
            bjm.register_child_process(job_id, proc)

        try:
            # Bug #1590 AC3: transition to "running" only once execution
            # actually starts (after the cell-limiter slot is held, before
            # Phase 1 begins) -- so the dashboard can distinguish "queued,
            # no worker slot yet" from "actively executing". Code-review
            # finding F2: this call MUST be inside this try block --
            # placed before it, a raising update_status() (e.g. a SQLite
            # write failure) would skip the finally below and leak the
            # held xray cell-limiter slot forever.
            job_tracker.update_status(job_id, status="running")
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
            if _slot and _limiter is not None:
                _limiter.release()
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
            if timeout_config_degraded:
                inline = {**inline, "configuration_degraded": True}
            return _mcp_response(inline)

    response_body: Dict[str, Any] = {"job_id": job_id}
    if timeout_config_degraded:
        response_body["configuration_degraded"] = True
    return _mcp_response(response_body)


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
    repo_alias_parsed = _parse_json_string_array(repo_alias)
    # v10.4.5 (Defect 5): normalize single-element list to single-string for
    # ergonomic single-repo response shape ({"job_id":"..."}). Multi-element
    # lists still take the multi-repo path ({"job_ids":[...], "errors":[...]}).
    if isinstance(repo_alias_parsed, list) and len(repo_alias_parsed) == 1:
        candidate = repo_alias_parsed[0]
        if isinstance(candidate, str) and candidate:
            repo_alias_parsed = candidate

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
            if timeout_config_degraded:
                inline = {**inline, "configuration_degraded": True}
            return _mcp_response(inline)

    explore_response_body: Dict[str, Any] = {"job_id": job_id}
    if timeout_config_degraded:
        explore_response_body["configuration_degraded"] = True
    return _mcp_response(explore_response_body)


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
        # Bug #1709: probes via _utils._lazy_module_attr_or_none() (the
        # generalized form of Bug #1693's _lazy_singleton_app_or_none())
        # instead of a bare getattr(_utils.app_module, name, None), which
        # would otherwise permanently construct the process-wide app
        # singleton as a side effect of merely reading it.
        _arm = _utils._lazy_module_attr_or_none("activated_repo_manager")
        _grm = _utils._lazy_module_attr_or_none("golden_repo_manager")
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

    # 5.5. File-size cap (Story #1494 AC1, Finding A2): refuse BEFORE
    # touching tree-sitter at all -- os.stat() is a cheap syscall, so a
    # file above the cap is rejected near-instantly instead of triggering
    # an unbounded, GIL-held in-process parse (159ms measured on a 759KB
    # file per the report; scales with file size, with no upper bound).
    file_size = target.stat().st_size
    if file_size > _DUMP_AST_MAX_FILE_SIZE_BYTES:
        return _mcp_response(
            {
                "error": "file_too_large",
                "message": (
                    f"AST dump refused: file exceeds the in-process parse "
                    f"size limit of {_DUMP_AST_MAX_FILE_SIZE_BYTES} bytes "
                    f"(file is {file_size} bytes). Narrow to a smaller "
                    f"file, or use xray_search/xray_explore (the Rust "
                    f"subprocess scan path) which has no such limit."
                ),
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

    # Bug #1709: probes via _lazy_singleton_app_or_none() instead of a bare
    # _utils.app_module.app.state attribute chain, which would otherwise
    # permanently construct the process-wide app singleton as a side effect
    # of merely reading it (see _lazy_singleton_app_or_none()'s docstring).
    payload_cache = getattr(
        getattr(_lazy_singleton_app_or_none(), "state", None), "payload_cache", None
    )
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


_TRUNCATION_INLINE_LIMIT = 3

# R2-7 (Codex re-review): _TRUNCATION_INLINE_LIMIT bounds the OUTER
# findings/refine (or matches/evaluation_errors) array to 3 entries, but a
# SINGLE retained entry can itself be huge -- analyze_graph's
# ReduceFinding carries an `involved` array plus a parallel `signatures`
# array of full declaration lines, and any entry can carry an oversized
# `message`/text field. These bound what's INSIDE each of the 3 inlined
# entries, not just how many entries there are.
_NESTED_FIELD_LIST_LIMIT = 5
_NESTED_FIELD_STRING_LIMIT = 500


def _cap_nested_fields(entry: Any) -> Any:
    """Cap unbounded nested list/string fields within a single inline
    preview entry (e.g. a ReduceFinding's `involved`/`signatures` arrays
    or a long `message`) before it goes into the response.

    Deliberately generic (not hardcoded to specific field names) so it
    applies safely to both matches/evaluation_errors and findings/refine
    shapes: a single entry can be arbitrarily large even after the OUTER
    array is capped to _TRUNCATION_INLINE_LIMIT, if that one entry alone
    carries a big nested collection or string. Non-dict entries (e.g. the
    plain-int `refine` list) are returned unchanged.
    """
    if not isinstance(entry, dict):
        return entry
    capped: Dict[str, Any] = {}
    for key, value in entry.items():
        if isinstance(value, list):
            capped[key] = value[:_NESTED_FIELD_LIST_LIMIT]
        elif isinstance(value, str) and len(value) > _NESTED_FIELD_STRING_LIMIT:
            capped[key] = value[:_NESTED_FIELD_STRING_LIMIT] + "... [truncated]"
        else:
            capped[key] = value
    return capped


def _truncate_large_array_fields(
    result: Dict[str, Any], field_a: str, field_b: str, preview_key: str
) -> Dict[str, Any]:
    """Apply PayloadCache truncation to two large array fields of an
    X-Ray-family result. Shared core behind both `_truncate_xray_result`
    (matches/evaluation_errors) and `_truncate_graph_result`
    (findings/refine, H7 -- Issue #1811/Bug #1812) -- the truncation
    mechanics are identical, only which two fields hold the large arrays
    differs.

    Serialises `field_a`[] and `field_b`[] as a single JSON blob and
    delegates to PayloadCache.truncate_result(). When the combined payload
    exceeds payload_preview_size_chars (default 2000 chars) the full blob is
    stored in the cache and the response carries:
      - cache_handle: str           — use GET /api/cache/{handle} for full data
      - has_more: True
      - total_size: int             — full payload byte size
      - {preview_key}               — first N chars of the JSON
      - {field_a}[]: first 3 entries, nested fields capped (R2-7) — inline
        quick-scan subset
      - {field_b}[]: first 3 entries, nested fields capped (R2-7) — inline
        quick-scan subset
      - truncated: True

    When the payload is small (fits within preview_size_chars) the full
    field_a/field_b arrays are returned inline:
      - cache_handle: None
      - has_more: False
      - truncated: False

    When PayloadCache is unavailable (not configured in app.state) the
    original result dict is returned unchanged.
    """
    import json

    # Bug #1709: probes via _lazy_singleton_app_or_none() instead of a bare
    # _utils.app_module.app.state attribute chain, which would otherwise
    # permanently construct the process-wide app singleton as a side effect
    # of merely reading it (see _lazy_singleton_app_or_none()'s docstring).
    payload_cache = getattr(
        getattr(_lazy_singleton_app_or_none(), "state", None), "payload_cache", None
    )
    if payload_cache is None:
        return result

    large_payload = json.dumps(
        {
            field_a: result.get(field_a, []),
            field_b: result.get(field_b, []),
        }
    )

    truncation = payload_cache.truncate_result(large_payload)

    # Build base dict: preserve all top-level fields except field_a/field_b
    truncated_result = {k: v for k, v in result.items() if k not in (field_a, field_b)}

    if truncation.get("has_more"):
        truncated_result[preview_key] = truncation["preview"]
        truncated_result["cache_handle"] = truncation["cache_handle"]
        truncated_result["has_more"] = True
        truncated_result["total_size"] = truncation["total_size"]
        # R2-7: cap each RETAINED entry's own nested fields too -- the
        # outer slice alone doesn't stop one huge entry from reaching the
        # inline response unbounded.
        truncated_result[field_a] = [
            _cap_nested_fields(entry)
            for entry in result.get(field_a, [])[:_TRUNCATION_INLINE_LIMIT]
        ]
        truncated_result[field_b] = [
            _cap_nested_fields(entry)
            for entry in result.get(field_b, [])[:_TRUNCATION_INLINE_LIMIT]
        ]
        truncated_result["truncated"] = True
        truncated_result["fetch_tool_hint"] = (
            f"Result truncated to first {_TRUNCATION_INLINE_LIMIT} entries; "
            f"full result available at cache_handle "
            f"'{truncation['cache_handle']}' — fetch via the "
            f"`cidx_fetch_cached_payload` MCP tool with that handle."
        )
    else:
        truncated_result[field_a] = result.get(field_a, [])
        truncated_result[field_b] = result.get(field_b, [])
        truncated_result["cache_handle"] = None
        truncated_result["has_more"] = False
        truncated_result["truncated"] = False

    return truncated_result


def _truncate_xray_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Apply PayloadCache truncation to matches[]/evaluation_errors[] --
    see `_truncate_large_array_fields` for the shared mechanics."""
    return _truncate_large_array_fields(
        result, "matches", "evaluation_errors", "matches_and_errors_preview"
    )


def _truncate_graph_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """H7 (consolidated review, Issue #1811/Bug #1812): apply the SAME
    PayloadCache truncation `_truncate_xray_result` gives xray_search/
    xray_explore to `analyze_graph`'s findings[]/refine[] -- a whole-repo
    graph analysis produces strictly MORE output than a single-file
    search (each ReduceFinding carries `involved` plus a parallel
    `signatures` array of full declaration lines), so a dead-code sweep
    over a large repo can be a multi-megabyte MCP response without this.
    See `_truncate_large_array_fields` for the shared mechanics."""
    return _truncate_large_array_fields(
        result, "findings", "refine", "findings_and_refine_preview"
    )


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
