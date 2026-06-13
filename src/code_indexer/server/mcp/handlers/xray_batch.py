"""xray_search_batch MCP handler (Story #1055).

Cross-repo, multi-expression X-Ray sweep in ONE background job.

Algorithm:
  1. Auth: require query_repos permission.
  2. Parse + validate (hard-fail on structural/static errors BEFORE any job).
  3. Resolve repos (graceful-partial) with global-alias fallback.
  4. Submit ONE job (repo_alias=None, no per-repo dedup).

Worker executes the repos × scans matrix:
  - Between-cell cancellation via bjm.jobs[job_id].cancelled.
  - Wall-clock timeout via deadline.
  - Per-cell: resolve_batch_evaluator → XRaySearchEngine.run.
  - Progress once per repo processed (integer percent).
  - Oversized result → _truncate_xray_batch_result.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

from code_indexer.server.auth.user_manager import User
from code_indexer.xray.sandbox import validate_rust_evaluator
from code_indexer.xray.search_engine import XRaySearchEngine

from . import _utils
from ._utils import _mcp_response, _parse_json_string_array

logger = logging.getLogger(__name__)

# Fan-out bounds (mirror omni caps).
_MAX_REPOS = 50
_MAX_SCANS = 50

# Timeout range for the whole matrix (wider than single-repo [10,600]).
_TIMEOUT_MIN = 10
_TIMEOUT_MAX = 7200
_DEFAULT_TIMEOUT_SECONDS = 600

# await_seconds range (capped low — a multi-cell batch rarely completes inline).
_AWAIT_MIN: float = 0.0
_AWAIT_MAX: float = 30.0
_AWAIT_WARN_THRESHOLD: float = 10.0
_AWAIT_POLL_INTERVAL = 0.05

# Default evaluator: accept-all (one finding per Phase-1 hit at root node).
_DEFAULT_EVALUATOR_CODE = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    "    vec![EvalFinding {\n"
    '        pattern: "match".to_string(),\n'
    "        line: node.start_line,\n"
    "        snippet: String::new(),\n"
    "    }]\n"
    "}"
)


# ---------------------------------------------------------------------------
# Pure helper: resolve evaluator code without accessing live app state
# ---------------------------------------------------------------------------


def resolve_batch_evaluator(
    scan: Dict[str, Any],
    repo_alias: str,
    cidx_meta_path: Path,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Resolve evaluator code for one scan bundle without live app state.

    Resolution order:
      1. Inline ``evaluator_code`` in the scan dict → return as-is.
      2. ``pattern_name`` → load from cidx_meta_path/xray-patterns/{repo}/{name}.yaml
         then fall back to __any__/{name}.yaml.
      3. Neither → return _DEFAULT_EVALUATOR_CODE.

    Returns:
        (evaluator_code, None) on success.
        ("", structured_error_dict) on failure (pattern_not_found etc.).
    """
    raw_code: str = (scan.get("evaluator_code") or "").strip()
    pattern_name: Optional[str] = scan.get("pattern_name") or None

    if raw_code:
        return (raw_code, None)

    if pattern_name:
        from code_indexer.server.services.xray_pattern_service import XrayPatternService

        # Build a service that reads from the captured cidx_meta_path.
        # Pass refresh_scheduler=None so no live scheduler dependency.
        svc = XrayPatternService(cidx_meta_path, refresh_scheduler=None)
        try:
            evaluator_code, _ = svc.resolve_and_prepare_pattern(
                repo_alias=repo_alias,
                pattern_name=pattern_name,
                pattern_params=scan.get("pattern_params") or None,
            )
            return (evaluator_code, None)
        except ValueError as exc:
            error_key = str(exc).split(":")[0]
            return (
                "",
                {"error": error_key, "message": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001
            return (
                "",
                {"error": "pattern_load_error", "message": str(exc)},
            )

    return (_DEFAULT_EVALUATOR_CODE, None)


# ---------------------------------------------------------------------------
# Truncation helper
# ---------------------------------------------------------------------------


def _truncate_xray_batch_result(
    result: Dict[str, Any],
    payload_cache: Any,
) -> Dict[str, Any]:
    """Apply PayloadCache truncation to a batch result.

    Serializes matches[], errors[], and evaluation_errors[] as a single JSON
    blob.  When oversized the full blob is stored in PayloadCache and the
    response carries cache_handle + preview.  When small the full arrays are
    returned inline.

    When payload_cache is None the original result is returned unchanged.
    """
    if payload_cache is None:
        return result

    large_payload = json.dumps(
        {
            "matches": result.get("matches", []),
            "errors": result.get("errors", []),
            "evaluation_errors": result.get("evaluation_errors", []),
        }
    )

    truncation = payload_cache.truncate_result(large_payload)

    # Preserve all top-level scalar fields.
    truncated: Dict[str, Any] = {
        k: v
        for k, v in result.items()
        if k not in ("matches", "errors", "evaluation_errors")
    }

    if truncation.get("has_more"):
        truncated["matches"] = result.get("matches", [])[:3]
        truncated["errors"] = result.get("errors", [])[:3]
        truncated["evaluation_errors"] = result.get("evaluation_errors", [])[:3]
        truncated["matches_and_errors_preview"] = truncation["preview"]
        truncated["cache_handle"] = truncation["cache_handle"]
        truncated["has_more"] = True
        truncated["total_size"] = truncation["total_size"]
        truncated["truncated"] = True
        truncated["fetch_tool_hint"] = (
            f"Result truncated to first 3 entries; full result available at "
            f"cache_handle '{truncation['cache_handle']}' — fetch via the "
            f"`cidx_fetch_cached_payload` MCP tool with that handle."
        )
    else:
        truncated["matches"] = result.get("matches", [])
        truncated["errors"] = result.get("errors", [])
        truncated["evaluation_errors"] = result.get("evaluation_errors", [])
        truncated["cache_handle"] = None
        truncated["has_more"] = False
        truncated["truncated"] = False

    return truncated


# ---------------------------------------------------------------------------
# App-state accessors (extracted for easy mocking in tests)
# ---------------------------------------------------------------------------


def _get_background_job_manager() -> Any:
    return _utils.app_module.background_job_manager


def _get_cidx_meta_path() -> Path:
    """Return the mutable cidx-meta base path."""
    grm = getattr(_utils.app_module, "golden_repo_manager", None)
    if grm is None:
        raise RuntimeError(
            "cidx-meta path not available: golden_repo_manager not configured"
        )
    return Path(grm.golden_repos_dir) / "cidx-meta"


def _get_arm_and_grm() -> Tuple[Any, Any]:
    """Return (activated_repo_manager, golden_repo_manager) or (None, None)."""
    arm = getattr(_utils.app_module, "activated_repo_manager", None)
    grm = getattr(_utils.app_module, "golden_repo_manager", None)
    return arm, grm


def _resolve_repo_path(alias: str) -> Optional[str]:
    """Resolve alias → versioned snapshot path. Returns None if unknown."""
    from code_indexer.server.mcp.handlers.repos import _resolve_golden_repo_path

    return cast(Optional[str], _resolve_golden_repo_path(alias))


def _get_xray_cell_limiter() -> Any:
    """Return the xray cell concurrency limiter from app.state, or None if not wired."""
    app = getattr(_utils.app_module, "app", None)
    if app is None:
        return None
    return getattr(getattr(app, "state", None), "xray_cell_limiter", None)


def _await_job_result(
    bjm: Any, job_id: str, username: str, await_seconds: float
) -> Optional[Dict[str, Any]]:
    deadline = time.monotonic() + await_seconds
    while time.monotonic() < deadline:
        job_status = bjm.get_job_status(job_id, username)
        if job_status is not None and job_status.get("status") in (
            "completed",
            "completed_partial",
        ):
            return cast(Optional[Dict[str, Any]], job_status.get("result"))
        time.sleep(_AWAIT_POLL_INTERVAL)
    return None


# ---------------------------------------------------------------------------
# Batch worker
# ---------------------------------------------------------------------------


def _run_xray_batch_job(
    resolved_repos: List[Dict[str, Any]],
    scans: List[Dict[str, Any]],
    repo_errors: List[Dict[str, Any]],
    cidx_meta_path: Path,
    max_results: Optional[int],
    timeout_seconds: int,
    job_id: str,
    bjm: Any,
    progress_callback: Any,
) -> Dict[str, Any]:
    """Execute the repos × scans matrix.

    Args:
        resolved_repos: List of {"alias": str, "path": Path}.
        scans: List of normalized scan dicts.
        repo_errors: Pre-flight repo resolution errors (already tagged).
        cidx_meta_path: Captured cidx-meta path (no live app state access).
        max_results: Optional per-cell max_files cap.
        timeout_seconds: Wall-clock cap for the entire matrix.
        job_id: Job ID for cancellation checks.
        bjm: BackgroundJobManager instance for cancellation checks.
        progress_callback: Injected by BackgroundJobManager.

    Returns:
        Unified result dict with matches, errors, evaluation_errors, counters,
        and partial/timeout/cancelled flags.
    """
    matches: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = list(repo_errors)
    evaluation_errors: List[Dict[str, Any]] = []

    total = len(resolved_repos)
    repos_completed = 0
    partial: bool = bool(repo_errors)
    timed_out = False
    cancelled = False
    deadline = time.monotonic() + timeout_seconds

    outer_break = False
    for repo in resolved_repos:
        # Between-repo cancellation check.
        job_obj = bjm.jobs.get(job_id)
        if job_obj is not None and job_obj.cancelled:
            cancelled = True
            partial = True
            outer_break = True
            break

        for scan_index, scan in enumerate(scans):
            # Between-cell cancellation check.
            job_obj = bjm.jobs.get(job_id)
            if job_obj is not None and job_obj.cancelled:
                cancelled = True
                partial = True
                outer_break = True
                break

            # Timeout check.
            if time.monotonic() >= deadline:
                timed_out = True
                partial = True
                outer_break = True
                break

            # Resolve evaluator (pure, repo-scoped).
            eval_code, err = resolve_batch_evaluator(
                scan, repo["alias"], cidx_meta_path
            )
            if err is not None:
                errors.append(
                    {
                        "error_level": "cell",
                        "repository_alias": repo["alias"],
                        "scan_index": scan_index,
                        **err,
                    }
                )
                partial = True
                continue

            # Acquire global xray cell limiter slot before executing.
            _limiter = _get_xray_cell_limiter()
            _slot = False
            if _limiter is not None:
                remaining = deadline - time.monotonic()
                _slot = _limiter.acquire(timeout=max(0.0, remaining))
                if not _slot:
                    timed_out = True
                    partial = True
                    outer_break = True
                    break

            # Execute cell — limiter slot held (if wired); release in finally.
            _cell_exec_error = False
            try:
                # Wire on_process_spawned so any Rust child process is
                # tracked in bjm — mirrors the xray.py single-repo pattern.
                # Mid-cell kill reliability is NOT promised (same as xray.py),
                # but registration must exist for parity with the MCP path.
                def _on_spawned(proc, _jid=job_id):  # type: ignore[no-untyped-def]
                    if _jid:
                        bjm.register_child_process(_jid, proc)

                cell = XRaySearchEngine().run(
                    repo_path=repo["path"],
                    driver_regex=scan["driver_regex"],
                    evaluator_code=eval_code,
                    search_target=scan.get("search_target", "content"),
                    case_sensitive=scan.get("case_sensitive", True),
                    multiline=scan.get("multiline", False),
                    pcre2=scan.get("pcre2", False),
                    max_files=max_results,
                    on_process_spawned=_on_spawned,
                )
                if job_id:
                    bjm.unregister_child_processes(job_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {
                        "error_level": "cell",
                        "repository_alias": repo["alias"],
                        "scan_index": scan_index,
                        "error": "cell_execution_error",
                        "message": str(exc),
                    }
                )
                partial = True
                _cell_exec_error = True
            finally:
                if _slot and _limiter is not None:
                    _limiter.release()
            if _cell_exec_error:
                continue

            # Capture phase1_failed.
            if cell.get("phase1_failed"):
                errors.append(
                    {
                        "error_level": "cell",
                        "repository_alias": repo["alias"],
                        "scan_index": scan_index,
                        "error": "phase1_failed",
                        "message": cell.get("phase1_error") or "",
                    }
                )
                partial = True

            # Tag and collect matches.
            pattern_name_val = scan.get("pattern_name") or None
            for m in cell.get("matches", []):
                m_copy = dict(m)
                m_copy["repository_alias"] = repo["alias"]
                m_copy["scan_index"] = scan_index
                m_copy["pattern_name"] = pattern_name_val
                matches.append(m_copy)

            # Tag and collect per-file evaluation errors.
            for ee in cell.get("evaluation_errors", []):
                ee_copy = dict(ee)
                ee_copy["repository_alias"] = repo["alias"]
                ee_copy["scan_index"] = scan_index
                evaluation_errors.append(ee_copy)
                partial = True

            if cell.get("partial"):
                partial = True

        if outer_break:
            break

        repos_completed += 1
        progress_callback(
            int(repos_completed / total * 100),
            "processing",
            f"{repos_completed}/{total} repos",
        )

    result: Dict[str, Any] = {
        "matches": matches,
        "errors": errors,
        "evaluation_errors": evaluation_errors,
        "total_repos": total,
        "total_scans": len(scans),
        "total_cells": total * len(scans),
        "repos_completed": repos_completed,
        "partial": partial,
        "timeout": timed_out,
        "cancelled": cancelled,
    }
    return result


# ---------------------------------------------------------------------------
# MCP handler
# ---------------------------------------------------------------------------


def handle_xray_search_batch(
    params: Dict[str, Any], user: Optional[User]
) -> Dict[str, Any]:
    """MCP handler for the xray_search_batch tool.

    1. Auth: query_repos permission required.
    2. Parse + validate inputs (hard-fail on structural/static errors).
    3. Resolve repos (graceful-partial) with global-alias fallback.
    4. Submit ONE background job.

    Error codes:
        auth_required                  — unauthenticated or missing query_repos.
        alias_required                 — repository_alias missing or empty.
        scans_required                 — scans missing, not a list, or empty.
        too_many_repositories          — len(aliases) > 50.
        too_many_scans                 — len(scans) > 50.
        timeout_out_of_range           — timeout_seconds outside [10, 7200].
        await_seconds_out_of_range     — await_seconds outside [0, 30].
        driver_regex_required          — scan.driver_regex missing or empty.
        mutually_exclusive_params      — both evaluator_code and pattern_name set.
        xray_evaluator_validation_failed — evaluator code fails Rust whitelist.
        no_repositories_resolved       — all aliases unresolvable.
    """
    # ------------------------------------------------------------------
    # 1. Auth
    # ------------------------------------------------------------------
    if user is None or not user.has_permission("query_repos"):
        return _mcp_response({"error": "auth_required"})

    # ------------------------------------------------------------------
    # 2. Parse + validate
    # ------------------------------------------------------------------
    raw_alias = params.get("repository_alias", "")
    raw_scans = params.get("scans")
    timeout_raw = params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    await_raw = params.get("await_seconds", 0)
    max_results: Optional[int] = params.get("max_results")

    # Parse repository_alias: str | list | JSON-encoded array.
    if raw_alias is None or raw_alias == "" or raw_alias == []:
        return _mcp_response(
            {"error": "alias_required", "message": "repository_alias is required"}
        )
    aliases_parsed = _parse_json_string_array(raw_alias)
    if isinstance(aliases_parsed, list):
        if not aliases_parsed:
            return _mcp_response(
                {
                    "error": "alias_required",
                    "message": "repository_alias must not be empty",
                }
            )
        # De-duplicate, preserve order.
        seen: set = set()
        aliases: List[str] = []
        for a in aliases_parsed:
            if a not in seen:
                seen.add(a)
                aliases.append(a)
    elif isinstance(aliases_parsed, str):
        if not aliases_parsed:
            return _mcp_response(
                {
                    "error": "alias_required",
                    "message": "repository_alias must not be empty",
                }
            )
        aliases = [aliases_parsed]
    else:
        return _mcp_response(
            {"error": "alias_required", "message": "repository_alias is required"}
        )

    # Validate scans.
    if raw_scans is None or not isinstance(raw_scans, list) or len(raw_scans) == 0:
        return _mcp_response(
            {"error": "scans_required", "message": "scans must be a non-empty list"}
        )

    # Fan-out bounds.
    if len(aliases) > _MAX_REPOS:
        return _mcp_response(
            {
                "error": "too_many_repositories",
                "message": f"repository_alias must have at most {_MAX_REPOS} entries, got {len(aliases)}",
            }
        )
    if len(raw_scans) > _MAX_SCANS:
        return _mcp_response(
            {
                "error": "too_many_scans",
                "message": f"scans must have at most {_MAX_SCANS} entries, got {len(raw_scans)}",
            }
        )

    # Timeout validation.
    try:
        timeout_seconds = int(timeout_raw)
    except (TypeError, ValueError):
        timeout_seconds = _DEFAULT_TIMEOUT_SECONDS
    if not (_TIMEOUT_MIN <= timeout_seconds <= _TIMEOUT_MAX):
        return _mcp_response(
            {
                "error": "timeout_out_of_range",
                "message": (
                    f"timeout_seconds must be in [{_TIMEOUT_MIN}, {_TIMEOUT_MAX}], "
                    f"got {timeout_seconds}"
                ),
            }
        )

    # await_seconds validation.
    try:
        await_seconds = float(await_raw)
    except (TypeError, ValueError):
        await_seconds = 0.0
    if not (_AWAIT_MIN <= await_seconds <= _AWAIT_MAX):
        return _mcp_response(
            {
                "error": "await_seconds_out_of_range",
                "message": (
                    f"await_seconds must be in [{_AWAIT_MIN}, {_AWAIT_MAX}], "
                    f"got {await_seconds}"
                ),
            }
        )
    if await_seconds > _AWAIT_WARN_THRESHOLD:
        logger.warning(
            "xray_search_batch: await_seconds=%s may saturate threadpool under load",
            await_seconds,
        )

    # Per-scan validation.
    scans: List[Dict[str, Any]] = []
    for i, scan in enumerate(raw_scans):
        driver_regex: str = (scan.get("driver_regex") or "").strip()
        if not driver_regex:
            return _mcp_response(
                {
                    "error": "driver_regex_required",
                    "message": f"scan[{i}].driver_regex is required and must not be empty",
                    "scan_index": i,
                }
            )

        raw_eval: str = (scan.get("evaluator_code") or "").strip()
        pname: Optional[str] = scan.get("pattern_name") or None

        if raw_eval and pname:
            return _mcp_response(
                {
                    "error": "mutually_exclusive_params",
                    "message": (
                        "evaluator_code and pattern_name are mutually exclusive — "
                        "provide one or the other, not both"
                    ),
                    "scan_index": i,
                }
            )

        if raw_eval:
            validation = validate_rust_evaluator(raw_eval)
            if not validation.ok:
                return _mcp_response(
                    {
                        "error": "xray_evaluator_validation_failed",
                        "scan_index": i,
                        "error_code": validation.error_code,
                        "offending_construct": validation.offending_construct,
                        "offending_line": validation.offending_line,
                        "message": validation.reason,
                    }
                )

        # Normalize per-bundle defaults.
        scans.append(
            {
                "driver_regex": driver_regex,
                "evaluator_code": raw_eval or None,
                "pattern_name": pname,
                "pattern_params": scan.get("pattern_params") or None,
                "search_target": scan.get("search_target") or "content",
                "case_sensitive": bool(scan.get("case_sensitive", True)),
                "multiline": bool(scan.get("multiline", False)),
                "pcre2": bool(scan.get("pcre2", False)),
            }
        )

    # ------------------------------------------------------------------
    # 3. Resolve repos (graceful-partial) with global-alias fallback
    # ------------------------------------------------------------------
    arm, grm = _get_arm_and_grm()
    repo_errors: List[Dict[str, Any]] = []
    resolved_repos: List[Dict[str, Any]] = []

    for alias in aliases:
        # Try direct resolution.
        path_str = _resolve_repo_path(alias)

        # Global-alias fallback: bare alias → '<alias>-global'.
        if path_str is None and arm is not None and grm is not None:
            if not alias.endswith("-global"):
                if not arm.user_has_activated_repo(user.username, alias):
                    from ._global_fallback import try_global_fallback

                    promoted = try_global_fallback(alias, grm)
                    if promoted is not None:
                        logger.info(
                            "batch bare-alias fallback: %r -> %r for user %r",
                            alias,
                            promoted,
                            user.username,
                        )
                        alias = promoted
                        path_str = _resolve_repo_path(alias)

        if path_str is not None:
            resolved_repos.append({"alias": alias, "path": Path(path_str)})
        else:
            repo_errors.append(
                {
                    "error_level": "repo",
                    "repository_alias": alias,
                    "error": "repository_not_found",
                    "message": f"Repository alias {alias!r} not found",
                }
            )

    if not resolved_repos:
        return _mcp_response(
            {
                "error": "no_repositories_resolved",
                "message": "None of the specified repository aliases could be resolved",
                "errors": repo_errors,
            }
        )

    # ------------------------------------------------------------------
    # 4. Submit ONE background job
    # ------------------------------------------------------------------
    bjm = _get_background_job_manager()
    cidx_meta = _get_cidx_meta_path()

    # Capture all needed values now (worker must not access request-time state).
    captured_repos = list(resolved_repos)
    captured_scans = list(scans)
    captured_repo_errors = list(repo_errors)
    captured_max_results = max_results
    captured_timeout = timeout_seconds
    captured_cidx_meta = cidx_meta

    def _job_fn(progress_callback) -> Dict[str, Any]:  # type: ignore[no-untyped-def]
        raw_result = _run_xray_batch_job(
            resolved_repos=captured_repos,
            scans=captured_scans,
            repo_errors=captured_repo_errors,
            cidx_meta_path=captured_cidx_meta,
            max_results=captured_max_results,
            timeout_seconds=captured_timeout,
            job_id=_jid[0] if _jid else "",
            bjm=bjm,
            progress_callback=progress_callback,
        )
        payload_cache = getattr(getattr(_utils.app_module, "app", None), "state", None)
        payload_cache = (
            getattr(payload_cache, "payload_cache", None) if payload_cache else None
        )
        return _truncate_xray_batch_result(raw_result, payload_cache)

    # Deferred job_id capture: submit_job requires `func` before it returns the
    # job_id, but the worker (_job_fn) needs the job_id for between-cell
    # cancellation checks (bjm.jobs[job_id].cancelled).  The mutable list
    # _jid[] breaks the circular dependency: _job_fn reads _jid[0] at call
    # time (after submit_job has returned and appended the id below).
    _jid: List[str] = []
    job_id: str = bjm.submit_job(
        operation_type="xray_search_batch",
        func=_job_fn,
        submitter_username=user.username,
        repo_alias=None,  # no per-repo dedup; concurrent batches allowed
    )
    _jid.append(job_id)

    if await_seconds > 0:
        inline = _await_job_result(bjm, job_id, user.username, await_seconds)
        if inline is not None:
            return _mcp_response(inline)

    return _mcp_response({"job_id": job_id})


# ---------------------------------------------------------------------------
# REST batch route helper (used by xray_routes.py)
# ---------------------------------------------------------------------------


def get_batch_handler() -> Any:
    """Return handle_xray_search_batch for wiring into the REST router."""
    return handle_xray_search_batch


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------


def _register(registry: Dict[str, Any]) -> None:
    """Register xray_search_batch handler."""
    registry["xray_search_batch"] = handle_xray_search_batch
