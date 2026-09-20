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

import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import anyio

from code_indexer.server.auth.user_manager import User
from code_indexer.xray.sandbox import validate_rust_evaluator

from ._utils import (
    _enforce_repo_count_cap,
    _mcp_response,
    _parse_and_collapse_repo_alias,
    cap_breach_response,
)
from .xray import (
    _get_xray_cell_limiter,
    _pattern_scope_alias,
    _resolve_evaluator_code_off_loop,
    _resolve_repo_path,
    _truncate_graph_result,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 120
_TIMEOUT_MIN = 10
_TIMEOUT_MAX = 600

# Issue #1902 P9 review (P2 remediation), Epic #1906 non-negotiable #2 (no
# NEW config setting): the ceiling a multi-repo request's THEORETICAL total
# (alias_count * timeout_seconds) must not exceed. Deliberately reuses
# `_TIMEOUT_MAX` -- the SAME 600s ceiling a SINGLE-repo `timeout_seconds`
# already promises never to exceed -- rather than inventing a new number:
# a multi-repo request run SEQUENTIALLY (this handler's design, see
# `_run_multi_repo_analyze_graph`'s docstring) has no principled claim to a
# LARGER total budget than one single-repo call is already allowed to
# legitimately take. `_AWAIT_SECONDS_MAX` (handlers/xray.py, 45.0s) was
# considered and rejected as the source: that constant bounds a DIFFERENT
# mechanism (xray_search's synchronous poll-then-fall-back-to-job-id
# window), and analyze_graph's own single-repo path already legitimately
# blocks up to `_TIMEOUT_MAX` (600s) with no such window -- reusing 45s here
# would reject requests the single-repo path already honors today.
_MULTI_REPO_TOTAL_TIMEOUT_CEILING_SECONDS = _TIMEOUT_MAX

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
    # Bug #1876: routes through the SAME PathPatternMatcher-backed compiled
    # selector (items 3/5/6) regex_search.py's indexed path and
    # xray_search's directory-walk (AST) path both use, keeping all three
    # X-Ray/search tools in agreement on one canonical glob normalization
    # policy (brace groups, bare-directory tokens, leading `./`/`*/`).
    # This is NOT "one glob implementation" end to end: xray_search's
    # content mode (ripgrep-backed) still hands its normalized globs to
    # ripgrep's own `-g` matcher, a separate glob engine fed by the same
    # normalization -- only directory-walk/AST callers like this one use
    # this Python selector directly. Built ONCE, outside the loop, so the
    # compiled PathSpec amortizes across every file visited during this
    # walk instead of recompiling the same include/exclude patterns per
    # file.
    from code_indexer.services.path_pattern_matcher import PathPatternMatcher

    selector = PathPatternMatcher().create_selector(include_patterns, exclude_patterns)
    results: List[str] = []
    collection_truncated = False
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        for filename in sorted(filenames):
            rel = Path(dirpath, filename).relative_to(repo_path).as_posix()
            if not selector.select(rel):
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
    """Validates and compiles `value` before it reaches the graph pipeline.

    A bare string (e.g. `"*.py"` instead of `["*.py"]`) would otherwise
    silently iterate per CHARACTER, producing nonsensical single-character
    glob patterns. A syntactically invalid glob would otherwise fail later
    during candidate collection, where an invalid exclude can fail open.

    Returns `(patterns, None)` on success (`patterns` is `[]` for `None`),
    or `(None, error_dict)` on a type or pattern violation.
    """
    from code_indexer.services.path_pattern_matcher import (
        InvalidPatternError,
        PathPatternMatcher,
    )

    if value is None:
        return [], None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None, {
            "error": f"{field_name}_invalid",
            "message": f"{field_name} must be a list of strings",
        }
    try:
        PathPatternMatcher().compile_patterns(value)
    except InvalidPatternError as exc:
        return None, {
            "error": f"{field_name}_invalid",
            "message": str(exc),
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
) -> Tuple[
    Union[str, List[str]], str, List[str], List[str], int, Optional[Dict[str, Any]]
]:
    """Parses and validates `params` for `handle_analyze_graph` -- factored
    out to keep that handler itself short. Returns either the 5 real
    parsed values with a `None` error, or empty/zero placeholders with a
    populated error dict (which the caller must check FIRST).

    Issue #1902: `repository_alias` now accepts a bare string, a native
    list of strings, OR a JSON-encoded string array -- matching
    `xray_search`'s documented contract EXACTLY, via the SAME
    `_parse_and_collapse_repo_alias` seam `handlers/xray.py`'s
    `handle_xray_search`/`handle_xray_explore` already use (no fourth copy
    of this parsing). A single-element list collapses to a plain string
    (mirrors xray.py's v10.4.5 Defect 5 ergonomic normalization), so
    callers of a single repo see the unchanged single-repo response shape
    regardless of which form they used.

    Issue #1902 P9 review, P3: an empty string ANYWHERE inside the list
    (`[""]`, or `["real-repo", ""]`) is rejected with the SAME
    `repository_alias_required` error a bare `""` already gets, rather
    than silently entering the multi-repo path where it would otherwise
    surface much later as a per-alias `repository_not_found` -- the same
    user mistake must not produce two different error shapes depending on
    whether it was wrapped in a list.
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

    repo_alias_raw = params.get("repository_alias", "")
    evaluator_code = params.get("evaluator_code", "")
    pattern_name = params.get("pattern_name")

    repo_alias = _parse_and_collapse_repo_alias(repo_alias_raw)

    repo_alias_type_valid = isinstance(repo_alias, str) or (
        isinstance(repo_alias, list)
        and all(isinstance(item, str) for item in repo_alias)
    )
    if (
        not isinstance(evaluator_code, str)
        or not repo_alias_type_valid
        or (pattern_name is not None and not isinstance(pattern_name, str))
    ):
        return (
            "",
            "",
            [],
            [],
            0,
            {
                "error": "invalid_params",
                "message": (
                    "repository_alias must be a string, a list of strings, or a "
                    "JSON-encoded list of strings, and evaluator_code must be a "
                    "string"
                ),
            },
        )
    if not evaluator_code and not pattern_name:
        return "", "", [], [], 0, {"error": "evaluator_code_required"}
    repo_alias_empty = repo_alias == "" or (
        isinstance(repo_alias, list)
        and (len(repo_alias) == 0 or any(item == "" for item in repo_alias))
    )
    if repo_alias_empty:
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

    # Bug #1860 (C2 remediation, #1858/#1859/#1860/#1861 changeset): an
    # ok=true, status="ran_ok", findings=[] response reads as a clean bill
    # of health, but when no candidate file ever reached a real extractor
    # with a supported language, the analysis had nothing to analyse at
    # all. Signal that distinctly via `status`, never via `ok` (nothing
    # failed -- Rule: no conflating a degraded-but-successful run with an
    # error).
    #
    # `files_with_unsupported_language` (recognized extension, no
    # LanguageExtractor implemented -- e.g. `main.py`, since only Java has
    # one) and `unreadable_or_unsupported_files` (genuinely unrecognized
    # extension or an escaped path -- e.g. `README.md`) are DISJOINT
    # per-file counters: `repo_index.rs::process_one_file` routes each
    # candidate into at most one of them. The original condition compared
    # only the first counter against the total file count, so it never
    # fired once a repo contained files from BOTH buckets (#1860's own
    # repro: files_with_unsupported_language=41,
    # unreadable_or_unsupported_files=11, neither equal to the file
    # count) -- exactly the shape almost every real repo has (source
    # files alongside READMEs, configs, etc). The fix sums both buckets:
    # "zero files reached the extractor with a supported language" is
    # true precisely when every candidate file landed in one of the two.
    #
    # Genuine read/parse failures (`files_with_read_errors`,
    # `files_with_parse_errors`, `files_with_extractor_panics`) are
    # deliberately EXCLUDED from this sum -- those files have a
    # recognized, supported-language extension (only Java has one, and
    # only Java files can produce these) and did reach the extractor;
    # their failure is a different, already independently signaled
    # degradation, not "no supported files". A broken-but-Java repo is
    # therefore never mislabelled `no_supported_files`.
    if result.get("ok") is True and result.get("status") == "ran_ok" and file_paths:
        degradation = result.get("degradation") or {}
        unsupported_language_count = (
            degradation.get("files_with_unsupported_language") or 0
        )
        unrecognized_extension_count = (
            degradation.get("unreadable_or_unsupported_files") or 0
        )
        if unsupported_language_count + unrecognized_extension_count == len(file_paths):
            result = dict(result)
            result["status"] = "no_supported_files"

    return result


def _dedupe_preserve_order(aliases: List[str]) -> List[str]:
    """Order-preserving de-duplication for a multi-repo alias list (Issue
    #1902 P9 review, P3).

    `["dup", "dup", "other"]` must analyze `"dup"` exactly ONCE, not twice:
    the previous behavior ran two full graph builds of the same repo and
    returned `len(results) == 2 != len(repositories) == 3` with `ok: true`,
    misleading any caller that uses that equality as a completeness check.
    `_enforce_repo_count_cap`'s own docstring already documents that it
    expects a POST-DEDUP list ("Final merged alias list (post-expansion,
    post-dedup)") -- this is the seam that makes that true for
    analyze_graph's multi-repo path.

    MUST run before both the repo-count cap check (N copies of one alias
    must not consume N cap slots) and the timeout-budget ceiling check (N
    copies must not multiply the theoretical total by N).
    """
    return list(dict.fromkeys(aliases))


def _check_multi_repo_timeout_budget(
    alias_count: int, timeout_seconds: int
) -> Optional[Dict[str, Any]]:
    """Rejects a multi-repo `analyze_graph` request up front when its
    THEORETICAL total (`alias_count * timeout_seconds` -- the sequential
    best case) already exceeds `_MULTI_REPO_TOTAL_TIMEOUT_CEILING_SECONDS`
    (Issue #1902 P9 review, P2).

    This replaces the previous `timeout_seconds // len(aliases)` division,
    which was measurably wrong on two fronts at once: the `_TIMEOUT_MIN`
    floor it fell back to (10s) cannot compile+build+analyze a fleet-scale
    repo (Epic #1906's own P0 baseline: repo-B is 280569 symbols / 10.2M
    edges), while above roughly N=12 aliases at the default 120s timeout
    the UNDIVIDED aggregate already exceeds what a single-repo call
    promises to bound -- the division's own stated invariant ("stays
    within the SAME budget a single-repo request already promises") was
    false for exactly the requests large enough to need dividing.

    The real per-alias cost this ceiling deliberately does NOT capture (an
    honest refusal beats a false promise, but it is still not the full
    story): `_run_analyze_graph_pipeline` passes the SAME `timeout_seconds`
    to BOTH `limiter.acquire(timeout=...)` (the xray cell queue wait) AND
    `backend.run_graph_analysis(timeout_seconds=...)` (the real
    compile/build/analyze deadline) -- the queue wait sits OUTSIDE that
    internal deadline, so a genuinely busy node can spend up to ~2x
    `timeout_seconds` on a single alias. `_resolve_repo_and_files` (alias
    resolution + a whole-repo `os.walk`) carries NO timeout of its own at
    all, on top of that. This handler cannot promise a hard ceiling on REAL
    wall-clock time; what it can and must do is refuse, loudly and up
    front, any request whose own best-case total already exceeds a stated
    bound, rather than silently reshaping the budget to fit (Rule 2,
    anti-fallback) or leaving the caller to discover an open-ended hang.

    Returns `None` when the request is within budget, or a structured
    error dict (never wrapped in the multi-repo `results`/`errors` shape --
    this is a single top-level rejection of the WHOLE request) naming the
    computed total, the ceiling, and the two remediations available to the
    caller (split the request, or lower `timeout_seconds`).
    """
    total_seconds = alias_count * timeout_seconds
    if total_seconds <= _MULTI_REPO_TOTAL_TIMEOUT_CEILING_SECONDS:
        return None
    return {
        "error": "multi_repo_timeout_budget_exceeded",
        "message": (
            f"{alias_count} repositories x {timeout_seconds}s timeout_seconds "
            f"= {total_seconds}s total (sequential best case), exceeding the "
            f"{_MULTI_REPO_TOTAL_TIMEOUT_CEILING_SECONDS}s ceiling a "
            "single-repo analyze_graph call already promises never to "
            "exceed. Split repository_alias into smaller batches, or lower "
            "timeout_seconds, and retry."
        ),
        "repository_count": alias_count,
        "timeout_seconds": timeout_seconds,
        "computed_total_seconds": total_seconds,
        "ceiling_seconds": _MULTI_REPO_TOTAL_TIMEOUT_CEILING_SECONDS,
    }


# Issue #1902 P9 review, P3: fields copied from a per-alias failure result
# into that alias's `errors[]` entry. An explicit ALLOWLIST (rather than
# spreading `repo_result` wholesale) means a field added to a FUTURE
# `_graph_error_result`/pipeline error shape must be deliberately added
# here too (Rule 13, anti-silent-failure: an unbounded field must be opted
# IN, never inherited by default). Never includes "ok" (always False on
# this path already, redundant), "findings"/"refine" (always empty on
# every KNOWN error path today, per `_graph_error_result`), "degradation"/
# "cached"/"compile_ms"/"fact_graph_complete" (meaningless on a failure).
_MULTI_REPO_ERROR_ALLOWED_FIELDS = (
    "error",
    "message",
    "error_code",
    "offending_construct",
    "offending_line",
    "status",
    "build_status",
)
# A real `RustNativeBackend.run_graph_analysis` compile/execution failure's
# "error" field is `{"error_type", "error_message"}`, and `error_message`
# carries the FULL rustc/xray-cli stderr verbatim -- `_sanitize_error_message`
# only redacts server paths, it never truncates. Appended once per FAILING
# alias, an unbounded string multiplies that cost by N.
_MULTI_REPO_ERROR_STRING_MAX_CHARS = 2000


def _truncate_error_string(value: str) -> str:
    """Caps a single string field's length for a bounded per-alias error
    payload (Issue #1902 P9 review, P3). Leaves short strings untouched."""
    if len(value) <= _MULTI_REPO_ERROR_STRING_MAX_CHARS:
        return value
    omitted = len(value) - _MULTI_REPO_ERROR_STRING_MAX_CHARS
    return f"{value[:_MULTI_REPO_ERROR_STRING_MAX_CHARS]}... [truncated {omitted} more chars]"


def _bound_repo_error_payload(repo_result: Dict[str, Any]) -> Dict[str, Any]:
    """Builds the bounded per-alias failure payload for `errors[]` (Issue
    #1902 P9 review, P3) -- the caller must already know `repo_result` is a
    failure (`repo_result.get("error")` truthy) before calling this.

    Copies ONLY `_MULTI_REPO_ERROR_ALLOWED_FIELDS`, never the full
    `repo_result` dict, and truncates any string value found (including
    inside a nested `error` dict, e.g. `error.error_message`) via
    `_truncate_error_string`. `repository_alias` is NEVER a key this
    function can produce -- the caller adds it AFTER spreading this
    function's return value, so a `repository_alias` key that happened to
    already exist inside `repo_result` (were it ever added to the
    allowlist) could never clobber the real one.
    """
    bounded: Dict[str, Any] = {}
    for field in _MULTI_REPO_ERROR_ALLOWED_FIELDS:
        if field not in repo_result:
            continue
        value = repo_result[field]
        if isinstance(value, str):
            bounded[field] = _truncate_error_string(value)
        elif isinstance(value, dict):
            bounded[field] = {
                k: (_truncate_error_string(v) if isinstance(v, str) else v)
                for k, v in value.items()
            }
        else:
            bounded[field] = value
    return bounded


async def _run_multi_repo_analyze_graph(
    aliases: List[str],
    evaluator_code: str,
    include_patterns: List[str],
    exclude_patterns: List[str],
    timeout_seconds: int,
) -> Dict[str, Any]:
    """Issue #1902 (Epic #1906 P9): runs the existing single-repo
    `_run_analyze_graph_pipeline` once PER alias, sequentially, and returns
    a per-repo-KEYED response -- never a merged findings list.

    Decision record (Issue #1902): the issue's own text proposed ONE graph
    over the union of repos, noting symbol ids are already globally scoped.
    That is a real `rust/` change (a genuine cross-repo arena/binder), which
    this Python-only fix is explicitly barred from making. The evaluator
    contract is also one `analyze_graph(g, facts)` call over one
    `GraphHandle` -- there is no Python-side way to hand it two graphs at
    once. Per-repo iteration is therefore the only honest thing achievable
    here: each alias gets its OWN real analysis, keyed by alias, so a
    finding's `involved`/dense ids are only ever compared against the graph
    they came from. If/when a union-graph build lands in `rust/`, this
    function is the seam to replace -- the request-level parsing/dispatch
    above it does not change.

    Fleet-scale cost (Epic #1906 non-negotiable #5 -- state cost at fleet
    scale before merging):
      - Memory: SEQUENTIAL, not concurrent. Each alias's
        `_run_analyze_graph_pipeline` call (real xray-cli subprocess) fully
        exits before the next alias starts, so peak memory stays bounded to
        ONE repo's graph at a time regardless of how many aliases are
        requested -- identical to today's single-repo memory profile, never
        N-multiplied. The existing shared xray cell limiter (acquired
        inside `_run_analyze_graph_pipeline` itself, H8) still gates
        admission against every other xray_search/xray_explore/
        analyze_graph call on the node.
      - Time (Issue #1902 P9 review, P2 -- corrects a false invariant this
        docstring previously stated): `timeout_seconds` is a PER-REPOSITORY
        budget, passed UNDIVIDED to every alias -- matching `xray_search`'s
        own multi-repo contract (handlers/xray.py:914-1072, which resolves
        timeout_seconds ONCE and hands the FULL value to every alias's job).
        The previous implementation divided `timeout_seconds` across the
        alias count (floored at `_TIMEOUT_MIN`) and claimed the total
        thereby "stays within the SAME budget a single-repo request already
        promises" -- measurably false: the `_TIMEOUT_MIN` floor (10s) cannot
        compile+build+analyze a fleet-scale repo (Epic #1906's own P0
        baseline: repo-B is 280569 symbols / 10.2M edges), and above
        roughly N=12 aliases at the default 120s timeout the UNDIVIDED
        aggregate already exceeded that same "single-repo budget" the
        division was supposed to protect. The caller (`handle_analyze_graph`)
        now rejects the WHOLE request up front, before this function ever
        runs, when `alias_count * timeout_seconds` exceeds
        `_MULTI_REPO_TOTAL_TIMEOUT_CEILING_SECONDS` (see
        `_check_multi_repo_timeout_budget`'s docstring for the honest
        queue-wait/file-walk cost this ceiling still cannot fully capture).
        `aliases` arriving here is therefore ALREADY: (a) deduplicated
        (`_dedupe_preserve_order`), (b) within the repo-count cap, and (c)
        within the timeout-budget ceiling -- all three enforced by the
        caller before dispatch.
      - Repo count itself is bounded by the SAME `omni_max_repos_per_search`
        config cap (default 50) `xray_search`'s own omni fan-out already
        enforces (Bug #894) -- no new setting introduced (Epic #1906
        non-negotiable #2).
      - Evaluator validation: the caller pre-flights `validate_rust_evaluator`
        ONCE, before calling this function, so a forbidden-construct
        evaluator never reaches this loop at all -- see
        `handle_analyze_graph`'s own docstring.

    Returns a dict with:
      - "ok": True only when EVERY alias resolved and analyzed successfully
        (Rule 13, anti-silent-failure -- a partial failure must never read
        as a clean multi-repo success).
      - "mode": "multi_repo" -- distinguishes this shape from the unwrapped
        single-repo response.
      - "repositories": `aliases` as received (already deduplicated by the
        caller), so `len(results) + len(errors) == len(repositories)` always
        holds -- a caller can use that as a completeness check.
      - "results": {alias: <that alias's own, individually PayloadCache-
        truncated, single-repo analyze_graph result>} -- only for aliases
        that resolved and ran.
      - "errors": [{**_bound_repo_error_payload(error_dict),
        "repository_alias": alias}] for aliases that failed (unknown alias,
        no candidate files, a real compile error, ...) -- never silently
        dropped, and never an unbounded copy of the full per-alias result
        (Issue #1902 P9 review, P3). `repository_alias` is added AFTER the
        allowlisted spread so it can never be clobbered by a same-named key
        inside the bounded payload.
    """
    results: Dict[str, Any] = {}
    errors: List[Dict[str, Any]] = []
    for alias in aliases:
        repo_result = await _run_analyze_graph_pipeline(
            evaluator_code, alias, include_patterns, exclude_patterns, timeout_seconds
        )
        # A per-repo failure (unresolvable alias, no candidate files, a
        # real compile error, ...) always carries a TRUTHY "error" value.
        # A successful `RustNativeBackend.run_graph_analysis` response also
        # carries an "error" key, explicitly set to `None` -- checking key
        # PRESENCE (`"error" in repo_result`) would misclassify every real
        # success as a failure; checking truthiness does not.
        if repo_result.get("error"):
            errors.append(
                {**_bound_repo_error_payload(repo_result), "repository_alias": alias}
            )
        else:
            results[alias] = _truncate_graph_result(repo_result)
    return {
        "ok": len(errors) == 0,
        "mode": "multi_repo",
        "repositories": aliases,
        "results": results,
        "errors": errors,
    }


async def handle_analyze_graph(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """MCP handler for the analyze_graph tool (Story #1811, S5, AC3).

    1. Auth + permission check (query_repos).
    2. Parameter parse + validation (`_parse_analyze_graph_request`) --
       `repository_alias` accepts a string, a list of strings, or a
       JSON-encoded string array (Issue #1902).
    3. For a multi-repo alias list: de-duplicate (order-preserving,
       `_dedupe_preserve_order`), enforce the shared omni repo-count cap,
       enforce the aggregate timeout-budget ceiling
       (`_check_multi_repo_timeout_budget`), pre-flight the evaluator ONCE
       (`validate_rust_evaluator` -- a single top-level rejection, never
       one per alias), then delegate to `_run_multi_repo_analyze_graph`
       (one real analysis PER alias, see that function's docstring for the
       multi-repo semantics and fleet-scale cost decision).
    4. For a single alias: delegate to `_run_analyze_graph_pipeline`
       (evaluator pre-flight, repo resolution, file collection, real
       graph-mode analysis) -- UNCHANGED from before Issue #1902.
    5. Return the structured result.

    Error codes:
        auth_required                    -- unauthenticated or missing query_repos.
        invalid_params                   -- params is not an object, or
                                             repository_alias is not a string/list of
                                             strings/JSON-encoded list of strings, or
                                             evaluator_code is not a string.
        evaluator_code_required          -- evaluator_code missing/empty.
        repository_alias_required        -- repository_alias missing/empty (string or
                                             list), or a list containing an empty
                                             string element (Issue #1902 P9 review).
        repo_count_cap_exceeded          -- repository_alias list (after dedup)
                                             exceeds the shared omni_max_repos_per_search
                                             cap (Issue #1902).
        multi_repo_timeout_budget_exceeded -- alias_count * timeout_seconds (after
                                             dedup) exceeds
                                             _MULTI_REPO_TOTAL_TIMEOUT_CEILING_SECONDS
                                             (Issue #1902 P9 review, P2) -- a single
                                             top-level rejection of the whole request,
                                             never wrapped in the multi-repo shape.
        include_patterns_invalid /
        exclude_patterns_invalid         -- not a list of strings, or a
                                             malformed glob.
        timeout_seconds_invalid          -- not a finite number.
        mutually_exclusive_params        -- evaluator_code and pattern_name
                                             were supplied together.
        pattern_mode_mismatch             -- stored pattern is not graph mode.
        xray_evaluator_validation_failed -- forbidden construct or missing entry
                                             point. For a multi-repo request this is
                                             now ONE top-level error (Issue #1902 P9
                                             review, P3), never one per alias.
        repository_not_found             -- alias cannot be resolved (single-repo path;
                                             surfaced per-alias in `errors[]` for a
                                             multi-repo request).
        no_candidate_files               -- include/exclude patterns matched nothing
                                             (single-repo path; per-alias in `errors[]`
                                             for a multi-repo request).
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

    if isinstance(repo_alias, list):
        # Issue #1902 P9 review, P3: dedup BEFORE the cap check --
        # `_enforce_repo_count_cap`'s own docstring documents that it
        # expects a post-dedup list, and N copies of one alias must not
        # consume N cap slots nor multiply the timeout-budget total by N.
        repo_alias = _dedupe_preserve_order(repo_alias)
        cap_breach = _enforce_repo_count_cap(repo_alias)
        if cap_breach is not None:
            return cap_breach_response(cap_breach)
        # Issue #1902 P9 review, P2: refuse a request whose own best-case
        # total already exceeds the aggregate ceiling, before resolving
        # any alias or running any analysis.
        timeout_budget_error = _check_multi_repo_timeout_budget(
            len(repo_alias), timeout_seconds
        )
        if timeout_budget_error is not None:
            return _mcp_response(timeout_budget_error)

    evaluator_code, resolution_error = await _resolve_evaluator_code_off_loop(
        params,
        _pattern_scope_alias(repo_alias),
        allow_default_evaluator=False,
        expected_execution_mode="graph",
    )
    if resolution_error is not None:
        return resolution_error

    if isinstance(repo_alias, list):
        # Issue #1902 P9 review, P3: pre-flight the evaluator ONCE here,
        # before the per-alias loop in `_run_multi_repo_analyze_graph` --
        # mirrors xray_search's own multi-repo pre-flight
        # (handlers/xray.py:944). Without this hoist, a forbidden-construct
        # evaluator produced one IDENTICAL xray_evaluator_validation_failed
        # error PER alias (8 aliases -> 8 identical errors, 50 at the cap)
        # instead of a single top-level rejection.
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
        multi_result = await _run_multi_repo_analyze_graph(
            repo_alias,
            evaluator_code,
            include_patterns,
            exclude_patterns,
            timeout_seconds,
        )
        return _mcp_response(multi_result)

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
