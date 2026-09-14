"""analyze_graph MCP handler — Story #1811 (S5, AC3).

Epic #1786 (S2 #1787, S2b #1806, S3 #1792, S4 #1793) built the whole
multi-file graph substrate (CSR arena, binder, receiver-type resolution,
inheritance families, refine phase) with no CLI/Python/MCP caller -- this
module is the front door that makes it reachable. It is a thin shim:
validate inputs, pre-flight check the evaluator, resolve the repository
alias, collect candidate files, and delegate to
`RustNativeBackend.run_graph_analysis` (Story #1811 AC2), which itself
drives `xray-cli --compile-only` -> `--build-graph` -> `--analyze-graph`
(Story #1811 AC1).

Execution model (deliberate simplification vs `xray_search`): the handler
runs SYNCHRONOUSLY within `timeout_seconds`, off the event loop via
`anyio.to_thread.run_sync` -- it is NOT wired into `BackgroundJobManager`/
`JobTracker`. CLAUDE.md's Background Jobs checklist requires confirming a
NEW background job's frontend/dashboard reporting pattern with the user
before implementing it, which this autonomous story cannot obtain; running
synchronously (bounded by `timeout_seconds`, still off-loop per the "never
block the event loop" architecture invariant -- BOTH repo-alias resolution
plus file-walk AND the real xray-cli subprocess pipeline run inside
`anyio.to_thread.run_sync`, never directly on the event loop) is the
conservative choice that avoids introducing an unconfirmed dashboard-
visible job while still satisfying the story's "async `await_seconds` like
`xray_search`" intent at reduced scope -- `await_seconds` is accepted for
forward compatibility but does not yet change behavior.
"""

from __future__ import annotations

import fnmatch
import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import anyio

from code_indexer.server.auth.user_manager import User
from code_indexer.xray.sandbox import validate_rust_evaluator

from ._utils import _mcp_response
from .xray import _get_xray_cell_limiter, _resolve_repo_path, _truncate_graph_result

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 120
_TIMEOUT_MIN = 10
_TIMEOUT_MAX = 600

# R3-2 (Codex re-review, ROUND 3): the SAME whole-repo candidate-file cap
# Rust's `GRAPH_INDEX_MAX_FILES` (rust/xray-cli/src/main.rs) enforces --
# must stay in sync with that constant. Capping HERE, during the Python-
# side walk that produces the `--files-from` list, is what actually closes
# the "millions of matching files" OOM vector: Rust's own `max_files`
# bound only ever sees what Python already wrote to that file, so without
# this the full unbounded path list would already be materialized (and
# handed to Rust) before Rust's limit ever gets a chance to engage.
_GRAPH_CANDIDATE_FILES_CAP = 50_000

# Directory names skipped during the whole-repo candidate walk -- common
# VCS/build-artifact directories every other xray tool's own file
# discovery implicitly avoids too.
_SKIP_DIR_NAMES = {
    ".git",
    ".code-indexer",
    "node_modules",
    ".venv",
    "__pycache__",
    "target",
    "dist",
    "build",
}


def _collect_graph_candidate_files(
    repo_path: Path,
    include_patterns: List[str],
    exclude_patterns: List[str],
    max_files: int,
) -> Tuple[List[str], bool]:
    """Whole-repo, glob-filtered file walk producing repo-relative POSIX
    paths for `--build-graph`'s `--files-from` list.

    Deliberately a plain path walk, not the CLI indexing pipeline's
    `Config`-bound `FileFinder` (Rule 4 anti-duplication does not apply
    here: `FileFinder` requires constructing a full indexing `Config` this
    spontaneous MCP tool has no reason to build). `include_patterns`/
    `exclude_patterns` follow `xray_search`'s own glob semantics.

    Synchronous by design -- callers MUST run this via
    `anyio.to_thread.run_sync`, never directly on the event loop (this
    walks the real filesystem and can take real wall-clock time on a large
    repo). Bounded by the repo's own finite file count (Rule 14).

    Consolidated review finding H6 (Issue #1811/Bug #1812): uses
    `os.walk` and prunes `_SKIP_DIR_NAMES` IN PLACE on `dirnames` so a
    skipped subtree (`.git`, `node_modules`, `.venv`, `target`, `dist`,
    `build`, `__pycache__`) is never descended into at all -- the
    previous `sorted(repo_path.rglob("*"))` implementation enumerated and
    `is_file()`-stat'd EVERY entry under those directories before
    discarding it as a post-hoc filter, which at production scale (hard
    NFSv3, ~5ms/op) can burn the tool's entire `timeout_seconds` budget on
    discarded work before a single real file is parsed.

    R3-2 (Codex re-review, ROUND 3): STOPS collecting once `max_files`
    matching paths have been found, rather than walking the whole repo
    and slicing afterward -- on a repo with millions of matching files,
    materializing the full path list before any limit engages risks
    OOM/timeout. Continues scanning only long enough to detect ONE
    additional match beyond the cap (then breaks immediately), returning
    `(paths, collection_truncated)` so the caller can honestly surface
    truncation via the existing degradation path rather than silently
    dropping files. Iterates `sorted(filenames)` per directory (rather
    than os.walk's raw, filesystem-dependent order) so WHICH files get
    truncated away, once the cap engages mid-directory, is deterministic
    and reproducible -- the final aggregate `results.sort()` already
    normalizes OUTPUT order regardless; this only affects the SELECTION
    made while a cap is hit mid-directory.

    Raises:
        ValueError: if `max_files < 1` -- a non-positive cap would make
            the first matching file trigger truncation and return zero
            paths, a surprising/invalid limit rather than a real bound.
    """
    if max_files < 1:
        raise ValueError(f"max_files must be >= 1, got {max_files}")
    results: List[str] = []
    collection_truncated = False
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        for filename in sorted(filenames):
            rel = Path(dirpath, filename).relative_to(repo_path).as_posix()
            if include_patterns and not any(
                fnmatch.fnmatch(rel, pat) for pat in include_patterns
            ):
                continue
            if exclude_patterns and any(
                fnmatch.fnmatch(rel, pat) for pat in exclude_patterns
            ):
                continue
            if len(results) >= max_files:
                collection_truncated = True
                break
            results.append(rel)
        if collection_truncated:
            break
    results.sort()
    return results, collection_truncated


def _validate_glob_patterns(
    value: Any, field_name: str
) -> Tuple[Optional[List[str]], Optional[Dict[str, Any]]]:
    """Validates `value` is `None` or a list of strings before it is ever
    handed to `fnmatch.fnmatch`. A bare string (e.g. `"*.py"` instead of
    `["*.py"]`) would otherwise silently iterate per CHARACTER, producing
    nonsensical single-character glob patterns instead of the caller's
    real intent -- rejected here as a structured error instead.

    Returns `(patterns, None)` on success (`patterns` is `[]` for `None`),
    or `(None, error_dict)` on a type violation.
    """
    if value is None:
        return [], None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None, {
            "error": f"{field_name}_invalid",
            "message": f"{field_name} must be a list of strings",
        }
    return value, None


def _resolve_repo_and_files(
    repo_alias: str, include_patterns: List[str], exclude_patterns: List[str]
) -> Tuple[Optional[Path], Optional[List[str]], bool, Optional[Dict[str, Any]]]:
    """Resolves `repo_alias` to a real path and collects its candidate
    files. Synchronous by design -- the caller (`_run_analyze_graph_
    pipeline`) MUST invoke this via `anyio.to_thread.run_sync`, since both
    alias resolution and the real file walk are blocking filesystem
    operations. Returns `(repo_path, file_paths, collection_truncated,
    None)` on success, or `(None, None, False, error_dict)` on any
    failure.

    R3-2 (Codex re-review, ROUND 3): `collection_truncated` is True when
    `_collect_graph_candidate_files` stopped early at
    `_GRAPH_CANDIDATE_FILES_CAP` -- the caller must OR this into the
    final result's `truncated_by_max_files`/`fact_graph_complete`
    degradation fields, since Rust's own `max_files` check would
    otherwise never independently detect it (Python already handed it a
    capped, not-oversized, file list).
    """
    repo_path_str = _resolve_repo_path(repo_alias)
    if repo_path_str is None:
        return (
            None,
            None,
            False,
            {
                "error": "repository_not_found",
                "message": f"Repository alias {repo_alias!r} not found",
            },
        )
    repo_path = Path(repo_path_str)
    file_paths, collection_truncated = _collect_graph_candidate_files(
        repo_path,
        include_patterns,
        exclude_patterns,
        max_files=_GRAPH_CANDIDATE_FILES_CAP,
    )
    if not file_paths:
        return (
            None,
            None,
            False,
            {
                "error": "no_candidate_files",
                "message": "no files matched include/exclude patterns",
            },
        )
    return repo_path, file_paths, collection_truncated, None


def _parse_timeout_seconds(timeout_raw: Any) -> Tuple[int, Optional[Dict[str, Any]]]:
    """Validates `timeout_raw` is a finite number (rejects bool, NaN,
    +/-inf, and non-numeric types) before ever calling `int(...)` on it --
    `int(float("nan"))`/`int(float("inf"))` raise `ValueError`/
    `OverflowError`, which must never surface as an unstructured failure.
    Clamps to `[_TIMEOUT_MIN, _TIMEOUT_MAX]` on success.
    """
    if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, (int, float)):
        return 0, {
            "error": "timeout_seconds_invalid",
            "message": f"timeout_seconds must be a number, got {timeout_raw!r}",
        }
    if isinstance(timeout_raw, float) and not math.isfinite(timeout_raw):
        return 0, {
            "error": "timeout_seconds_invalid",
            "message": f"timeout_seconds must be finite, got {timeout_raw!r}",
        }
    return max(_TIMEOUT_MIN, min(_TIMEOUT_MAX, int(timeout_raw))), None


def _parse_analyze_graph_request(
    params: Any,
) -> Tuple[str, str, List[str], List[str], int, Optional[Dict[str, Any]]]:
    """Parses and validates `params` for `handle_analyze_graph` -- factored
    out to keep that handler itself short. Returns either the 5 real
    parsed values with a `None` error, or empty/zero placeholders with a
    populated error dict (which the caller must check FIRST).
    """
    if not isinstance(params, dict):
        return (
            "",
            "",
            [],
            [],
            0,
            {
                "error": "invalid_params",
                "message": "params must be an object",
            },
        )

    repo_alias = params.get("repository_alias", "")
    evaluator_code = params.get("evaluator_code", "")
    if not isinstance(evaluator_code, str) or not isinstance(repo_alias, str):
        return (
            "",
            "",
            [],
            [],
            0,
            {
                "error": "invalid_params",
                "message": "repository_alias and evaluator_code must be strings",
            },
        )
    if not evaluator_code:
        return "", "", [], [], 0, {"error": "evaluator_code_required"}
    if not repo_alias:
        return "", "", [], [], 0, {"error": "repository_alias_required"}

    include_patterns, err = _validate_glob_patterns(
        params.get("include_patterns"), "include_patterns"
    )
    if err is not None:
        return "", "", [], [], 0, err
    exclude_patterns, err = _validate_glob_patterns(
        params.get("exclude_patterns"), "exclude_patterns"
    )
    if err is not None:
        return "", "", [], [], 0, err

    timeout_seconds, err = _parse_timeout_seconds(
        params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    )
    if err is not None:
        return "", "", [], [], 0, err

    assert include_patterns is not None  # guaranteed by _validate_glob_patterns
    assert exclude_patterns is not None
    return (
        repo_alias,
        evaluator_code,
        include_patterns,
        exclude_patterns,
        timeout_seconds,
        None,
    )


async def _run_analyze_graph_pipeline(
    evaluator_code: str,
    repo_alias: str,
    include_patterns: List[str],
    exclude_patterns: List[str],
    timeout_seconds: int,
) -> Dict[str, Any]:
    """Validates the evaluator, resolves the repo, collects candidate
    files, and delegates to `RustNativeBackend.run_graph_analysis` -- ALL
    off the event loop via `anyio.to_thread.run_sync`. Returns the raw (not
    yet `_mcp_response`-wrapped) result dict.
    """
    pre_validation = validate_rust_evaluator(evaluator_code)
    if not pre_validation.ok:
        return {
            "error": "xray_evaluator_validation_failed",
            "error_code": pre_validation.error_code,
            "offending_construct": pre_validation.offending_construct,
            "offending_line": pre_validation.offending_line,
            "message": pre_validation.reason,
        }

    resolve_result: Tuple[
        Optional[Path], Optional[List[str]], bool, Optional[Dict[str, Any]]
    ] = await anyio.to_thread.run_sync(
        lambda: _resolve_repo_and_files(repo_alias, include_patterns, exclude_patterns)
    )
    repo_path, file_paths, collection_truncated, error = resolve_result
    if error is not None:
        return error
    assert repo_path is not None  # guaranteed by _resolve_repo_and_files
    assert file_paths is not None

    from code_indexer.xray.rust_backend import RustNativeBackend
    from code_indexer.xray.search_engine import _get_cluster_cache

    backend = RustNativeBackend(xray_cache_backend=_get_cluster_cache())

    def _run_backend_analysis_with_admission_control() -> Dict[str, Any]:
        # H8 (consolidated review, Issue #1811/Bug #1812): mirrors
        # handlers/xray.py's xray_search/xray_explore cells -- acquire the
        # SHARED X-Ray cell limiter before the resource-heavy work
        # (candidate-path allocation, Rust graph memory, rayon workers,
        # compiler subprocesses) so concurrent whole-repo analyses cannot
        # exhaust node-level memory/CPU with zero admission control.
        limiter = _get_xray_cell_limiter()
        slot = False
        if limiter is not None:
            slot = limiter.acquire(timeout=float(timeout_seconds))
            if not slot:
                return {
                    "error": "xray_cell_queue_timeout",
                    "message": (
                        f"Timed out waiting for an xray worker slot after "
                        f"{timeout_seconds}s — server busy with other xray jobs."
                    ),
                }
        try:
            return backend.run_graph_analysis(
                evaluator_code=evaluator_code,
                repo_root=str(repo_path),
                file_paths=file_paths,
                timeout_seconds=timeout_seconds,
            )
        finally:
            if slot and limiter is not None:
                limiter.release()

    result: Dict[str, Any] = await anyio.to_thread.run_sync(
        _run_backend_analysis_with_admission_control
    )
    # R3-2 (Codex re-review, ROUND 3): if Python's OWN candidate
    # collection already truncated the file list before Rust ever saw
    # it, Rust's own max_files check cannot independently detect that --
    # OR this signal into the existing degradation path so the user is
    # honestly told, never silently truncated.
    if collection_truncated and "error" not in result:
        result = dict(result)
        result["truncated_by_max_files"] = True
        result["fact_graph_complete"] = False
    return result


async def handle_analyze_graph(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """MCP handler for the analyze_graph tool (Story #1811, S5, AC3).

    1. Auth + permission check (query_repos).
    2. Parameter parse + validation (`_parse_analyze_graph_request`).
    3. Delegate to `_run_analyze_graph_pipeline` (evaluator pre-flight,
       repo resolution, file collection, real graph-mode analysis).
    4. Return the structured result.

    Error codes:
        auth_required                    -- unauthenticated or missing query_repos.
        invalid_params                   -- params is not an object, or
                                             repository_alias/evaluator_code not strings.
        evaluator_code_required          -- evaluator_code missing/empty.
        repository_alias_required        -- repository_alias missing/empty.
        include_patterns_invalid /
        exclude_patterns_invalid         -- not a list of strings.
        timeout_seconds_invalid          -- not a finite number.
        xray_evaluator_validation_failed -- forbidden construct or missing entry point.
        repository_not_found             -- alias cannot be resolved.
        no_candidate_files               -- include/exclude patterns matched nothing.
    """
    if user is None or not user.has_permission("query_repos"):
        return _mcp_response({"error": "auth_required"})

    (
        repo_alias,
        evaluator_code,
        include_patterns,
        exclude_patterns,
        timeout_seconds,
        error,
    ) = _parse_analyze_graph_request(params)
    if error is not None:
        return _mcp_response(error)

    result = await _run_analyze_graph_pipeline(
        evaluator_code, repo_alias, include_patterns, exclude_patterns, timeout_seconds
    )
    # H7 (consolidated review, Issue #1811/Bug #1812): route through the
    # same PayloadCache truncation every other xray result gets -- a
    # whole-repo graph analysis's findings[]/refine[] can be strictly
    # larger than a single-file search's matches[]/evaluation_errors[].
    return _mcp_response(_truncate_graph_result(result))


def _register(registry: Dict[str, Any]) -> None:
    """Register the analyze_graph handler in the HANDLER_REGISTRY."""
    registry["analyze_graph"] = handle_analyze_graph
