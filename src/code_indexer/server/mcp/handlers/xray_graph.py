"""analyze_graph MCP handler — Story #1811 (S5, AC3).

Epic #1786 (S2 #1787, S2b #1806, S3 #1792, S4 #1793) built the whole
multi-file graph substrate (CSR arena, binder, receiver-type resolution,
inheritance families, refine phase) with no CLI/Python/MCP caller -- this
module is the front door that makes it reachable. It is a thin shim:
validate inputs, pre-flight check the evaluator, resolve the repository
alias, collect candidate files, and delegate to
`RustNativeBackend.run_graph_analysis` (Story #1811 AC2), which itself
drives `xray-cli --compile-only` -> `--build-graph` -> `--analyze-graph`
(Story #1811 AC1) and, opt-in via the `refine` request parameter, ->
`--refine` (Bug #1909 -- S3 #1792 built the refine phase in Rust but it had
NO caller anywhere under `src/code_indexer/` until this fix).

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
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import anyio

from code_indexer.server.auth.user_manager import User
from code_indexer.xray.sandbox import validate_rust_evaluator

from ._utils import (
    _enforce_repo_count_cap,
    _mcp_response,
    _parse_and_collapse_repo_alias,
    cap_breach_response,
)
from . import xray_truncation
from .xray import (
    _get_xray_cell_limiter,
    _lazy_singleton_app_or_none,
    _pattern_scope_alias,
    _resolve_evaluator_code_off_loop,
    _resolve_repo_path,
)

logger = logging.getLogger(__name__)


def _truncate_graph_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Apply PayloadCache truncation to findings[]/refine[].

    Bug #1928: moved out of xray.py -- shared mechanics now live in
    xray_truncation.truncate_result_fields(), which owns its own
    `payload_cache is None -> return a bounded, cache_unavailable=True
    result` guard (Bug #1928 P3). Both call sites below invoke this via
    `anyio.to_thread.run_sync` (this function's own
    `payload_cache.store_batch_with_keys()` call is synchronous DB I/O
    and CPU-bound JSON serialization -- both handlers that call this are
    `async def` and run directly on the event loop, so calling this
    inline would block it for the duration of the pack/store for a large
    result).

    Bug #1928 round 3 (P1): a page-set write failure (whole-entry pages
    + the pages-v1 manifest, one atomic batch) raises PageSetStoreError
    -- caught here and surfaced as an explicit error, never a
    cache_handle pointing at data that was not durably written.
    """
    # Bug #1709: probes via _lazy_singleton_app_or_none() instead of a bare
    # _utils.app_module.app.state attribute chain, which would otherwise
    # permanently construct the process-wide app singleton as a side effect
    # of merely reading it (see that helper's own docstring in xray.py).
    payload_cache = getattr(
        getattr(_lazy_singleton_app_or_none(), "state", None), "payload_cache", None
    )
    try:
        return xray_truncation.truncate_result_fields(
            result, payload_cache, ["findings", "refine"]
        )
    except xray_truncation.PageSetStoreError as exc:
        logger.error("analyze_graph truncation: page-set store failed: %s", exc)
        # Bug #1928 final round (Opus P4.6): preserve the non-truncated
        # metadata (fact_graph_complete, ok, degradation, cached,
        # compile_ms, ...) the analysis already produced -- only
        # findings/refine are genuinely undeliverable (the write failed).
        base_metadata = getattr(exc, "base_metadata", {})
        return {
            **base_metadata,
            "success": False,
            "error": "cache_store_failed",
            "message": f"Failed to store the truncated result in cache: {exc}",
        }


_DEFAULT_TIMEOUT_SECONDS = 120
_TIMEOUT_MIN = 10
_TIMEOUT_MAX = 600

# Bug #1913 (Epic #1906 P9 follow-up; supersedes the Issue #1902 admission
# predicate this constant originally bounded). Reuses `_TIMEOUT_MAX` -- the
# SAME 600s a SINGLE-repo `timeout_seconds` already promises never to
# exceed -- as the threshold `_run_multi_repo_analyze_graph` checks
# `time.monotonic() - t0` against before STARTING each alias.
#
# WHAT THIS ACTUALLY IS (P2-B, dual review, corrected after both
# reviewers independently flagged the previous wording as a false wall-
# clock guarantee -- the exact defect class #1913 itself exists to fix,
# so this comment must not reproduce it): a BETWEEN-ALIAS ADMISSION GATE,
# not a deadline. It can refuse to START a later alias once the threshold
# is observed; it CANNOT bound the alias already in flight when the check
# last passed, because the check only runs between iterations, never
# inside one. Concretely: 2 aliases at `timeout_seconds=600`, alias A
# finishes at t=599 (gate passes, elapsed < 600), alias B then waits up to
# 600s on `limiter.acquire(timeout=...)` (OUTSIDE `run_graph_analysis`'s
# own deadline), runs up to another 600s inside `run_graph_analysis`, plus
# an entirely unclocked `_resolve_repo_and_files` walk -- roughly 1800s
# real wall clock, THREE TIMES the advertised ceiling, and strictly MORE
# exposure than the single-repo path this constant claims to match.
#
# Worse than merely long: `_resolve_repo_and_files`'s `os.walk` carries no
# clock of its own, and this project's shared storage is `hard` NFS,
# which can block a stat/readdir call FOREVER on a wedged host (see
# CLAUDE.md's "Treat `hard` NFS as able to block FOREVER" invariant). The
# in-flight alias in that case is not "long" -- it is UNBOUNDED, and the
# next-loop admission check is never reached at all. There is no finite
# number this constant, or any comment on it, can honestly claim as a
# hard bound on total request wall clock.
#
# A REAL bound would require deadline-aware, CANCELLABLE repo resolution
# (so an in-flight `os.walk`/NFS stall can be aborted at a deadline) and
# remaining-budget propagation through both `limiter.acquire(timeout=...)`
# and `backend.run_graph_analysis(timeout_seconds=...)` for every
# subsequent alias -- none of which this Python-only admission gate does.
# Carried forward from the deleted `_check_multi_repo_timeout_budget`
# (the previous admission predicate this superseded), whose docstring was
# the only place in this file that stated this caveat honestly: do not
# let it die with that function.
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
    extractor_extensions: Dict[str, str],
) -> Tuple[List[str], bool, int, int, List[str]]:
    """Whole-repo, glob-filtered file walk producing repo-relative POSIX
    paths for `--build-graph`'s `--files-from` list.

    Bug #1907 (the most dangerous defect in epic #1906): scoping
    `include_patterns`/`exclude_patterns` to one extractable language on a
    mixed-language repo used to make `analyze_graph` report
    `fact_graph_complete: true` with every degradation counter at zero,
    while the graph was missing every call site in the excluded language
    -- an excluded file never became a candidate, so no Rust-side counter
    could ever count it. Narrowing the scope did NOT improve completeness,
    it HID the incompleteness, and a method called only from the unread
    language could be reported `is_definitely_dead_code() == Some(true)`:
    a false dead verdict on live code.

    `extractor_extensions` (obtained from `xray-cli --print-graph-
    extractor-extensions` via `rust_backend.get_graph_extractor_
    extensions`, the single source of truth Rust's extractor registry
    owns) is what lets this walk classify a file it is about to exclude:
    `files_excluded_with_extractor` counts an excluded file whose language
    genuinely COULD have contributed a real call edge had it been read --
    the caller MUST downgrade `fact_graph_complete` on a nonzero count
    here. `files_excluded_without_extractor` counts an excluded file whose
    language has no extractor at all (e.g. `README.md`) -- excluding it
    changes nothing about completeness, since including it would not have
    produced any real call edges either way. Conflating the two would
    re-create this bug in a new shape. `languages_excluded_with_extractor`
    (sorted, deduplicated) names WHICH languages went unread, so a caller
    learns what is missing, not merely that something is (AC3).
    Classification is a bare extension lookup against the already-in-hand
    `extractor_extensions` dict -- no filesystem call per file, so this
    adds no measurable cost to the walk at fleet scale (~900 repos).

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
    files_excluded_with_extractor = 0
    files_excluded_without_extractor = 0
    languages_excluded_with_extractor: Set[str] = set()
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        for filename in sorted(filenames):
            rel = Path(dirpath, filename).relative_to(repo_path).as_posix()
            if not selector.select(rel):
                # Bug #1907: this file will NEVER reach Rust -- classify it
                # NOW, while its extension is still in hand, or it becomes
                # permanently invisible to every completeness counter.
                # Extension lookup only, no filesystem call.
                ext = Path(filename).suffix.lstrip(".").lower()
                language = extractor_extensions.get(ext) if ext else None
                if language is not None:
                    files_excluded_with_extractor += 1
                    languages_excluded_with_extractor.add(language)
                else:
                    files_excluded_without_extractor += 1
                continue
            if len(results) >= max_files:
                collection_truncated = True
                break
            results.append(rel)
        if collection_truncated:
            break
    results.sort()
    return (
        results,
        collection_truncated,
        files_excluded_with_extractor,
        files_excluded_without_extractor,
        sorted(languages_excluded_with_extractor),
    )


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
) -> Tuple[
    Optional[Path],
    Optional[List[str]],
    bool,
    int,
    int,
    List[str],
    Optional[Dict[str, Any]],
]:
    """Resolves `repo_alias` to a real path and collects its candidate
    files. Synchronous by design -- the caller (`_run_analyze_graph_
    pipeline`) MUST invoke this via `anyio.to_thread.run_sync`, since both
    alias resolution and the real file walk (plus the extractor-extension
    lookup subprocess below) are blocking operations. Returns `(repo_path,
    file_paths, collection_truncated, files_excluded_with_extractor,
    files_excluded_without_extractor, languages_excluded_with_extractor,
    None)` on success, or `(None, None, False, 0, 0, [], error_dict)` on
    any failure.

    R3-2 (Codex re-review, ROUND 3): `collection_truncated` is True when
    `_collect_graph_candidate_files` stopped early at
    `_GRAPH_CANDIDATE_FILES_CAP` -- the caller must OR this into the
    final result's `truncated_by_max_files`/`fact_graph_complete`
    degradation fields, since Rust's own `max_files` check would
    otherwise never independently detect it (Python already handed it a
    capped, not-oversized, file list).

    Bug #1907: before the candidate walk can honestly classify a file it
    is about to exclude, it must know which extensions have a REAL graph
    extractor -- asked here, once per request, via `rust_backend.
    get_graph_extractor_extensions()` (itself backed by `xray-cli
    --print-graph-extractor-extensions`, the single source of truth
    `graph::extract::graph_extractor_extensions` owns on the Rust side).
    A lookup failure fails the WHOLE request loudly (`extractor_extension_
    lookup_failed`, Rule 2 anti-fallback) rather than silently treating
    "unknown" as "no extractor here" -- that would reproduce exactly the
    false-completeness bug this function exists to prevent.
    """
    repo_path_str = _resolve_repo_path(repo_alias)
    if repo_path_str is None:
        return (
            None,
            None,
            False,
            0,
            0,
            [],
            {
                "error": "repository_not_found",
                "message": f"Repository alias {repo_alias!r} not found",
            },
        )
    repo_path = Path(repo_path_str)

    from code_indexer.xray.rust_backend import get_graph_extractor_extensions

    extractor_extensions, lookup_error = get_graph_extractor_extensions()
    if extractor_extensions is None:
        return (
            None,
            None,
            False,
            0,
            0,
            [],
            {
                "error": "extractor_extension_lookup_failed",
                "message": (
                    "could not determine which languages have a graph "
                    f"extractor: {lookup_error}"
                ),
            },
        )

    (
        file_paths,
        collection_truncated,
        files_excluded_with_extractor,
        files_excluded_without_extractor,
        languages_excluded_with_extractor,
    ) = _collect_graph_candidate_files(
        repo_path,
        include_patterns,
        exclude_patterns,
        max_files=_GRAPH_CANDIDATE_FILES_CAP,
        extractor_extensions=extractor_extensions,
    )
    if not file_paths:
        return (
            None,
            None,
            False,
            0,
            0,
            [],
            {
                "error": "no_candidate_files",
                "message": "no files matched include/exclude patterns",
            },
        )
    return (
        repo_path,
        file_paths,
        collection_truncated,
        files_excluded_with_extractor,
        files_excluded_without_extractor,
        languages_excluded_with_extractor,
        None,
    )


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


def _parse_refine_flag(refine_raw: Any) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Bug #1909: validates the new opt-in `refine` request parameter.

    Must be a real boolean (never a truthy string/int -- `bool` is itself
    a subtype of `int` in Python, so `isinstance(refine_raw, bool)` must be
    checked BEFORE any numeric check ever could accept it). Defaults to
    `False` when omitted -- `--refine` is opt-in, never the default,
    mirroring `_parse_timeout_seconds`'s fail-fast validation convention.
    """
    if not isinstance(refine_raw, bool):
        return False, {
            "error": "refine_invalid",
            "message": f"refine must be a boolean, got {refine_raw!r}",
        }
    return refine_raw, None


def _parse_analyze_graph_request(
    params: Any,
) -> Tuple[
    Union[str, List[str]],
    str,
    List[str],
    List[str],
    int,
    bool,
    Optional[Dict[str, Any]],
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
            False,
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
            False,
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
        return (
            "",
            "",
            [],
            [],
            0,
            False,
            {
                "error": "evaluator_code_required",
                "message": "Either evaluator_code or pattern_name must be provided",
            },
        )
    repo_alias_empty = repo_alias == "" or (
        isinstance(repo_alias, list)
        and (len(repo_alias) == 0 or any(item == "" for item in repo_alias))
    )
    if repo_alias_empty:
        return (
            "",
            "",
            [],
            [],
            0,
            False,
            {
                "error": "repository_alias_required",
                "message": "repository_alias must be a non-empty string, or a "
                "non-empty list/JSON array of non-empty strings",
            },
        )

    include_patterns, err = _validate_glob_patterns(
        params.get("include_patterns"), "include_patterns"
    )
    if err is not None:
        return "", "", [], [], 0, False, err
    exclude_patterns, err = _validate_glob_patterns(
        params.get("exclude_patterns"), "exclude_patterns"
    )
    if err is not None:
        return "", "", [], [], 0, False, err

    timeout_seconds, err = _parse_timeout_seconds(
        params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    )
    if err is not None:
        return "", "", [], [], 0, False, err

    refine, err = _parse_refine_flag(params.get("refine", False))
    if err is not None:
        return "", "", [], [], 0, False, err

    assert include_patterns is not None  # guaranteed by _validate_glob_patterns
    assert exclude_patterns is not None
    return (
        repo_alias,
        evaluator_code,
        include_patterns,
        exclude_patterns,
        timeout_seconds,
        refine,
        None,
    )


async def _run_analyze_graph_pipeline(
    evaluator_code: str,
    repo_alias: str,
    include_patterns: List[str],
    exclude_patterns: List[str],
    timeout_seconds: int,
    refine: bool = False,
) -> Dict[str, Any]:
    """Validates the evaluator, resolves the repo, collects candidate
    files, and delegates to `RustNativeBackend.run_graph_analysis` -- ALL
    off the event loop via `anyio.to_thread.run_sync`. Returns the raw (not
    yet `_mcp_response`-wrapped) result dict.

    Bug #1909: `refine` is forwarded UNCHANGED to `run_graph_analysis` --
    the opt-in gate deciding whether S3's `--refine` phase runs lives
    entirely there (`RustNativeBackend._maybe_run_refine`), never
    re-implemented here.
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
        Optional[Path],
        Optional[List[str]],
        bool,
        int,
        int,
        List[str],
        Optional[Dict[str, Any]],
    ] = await anyio.to_thread.run_sync(
        lambda: _resolve_repo_and_files(repo_alias, include_patterns, exclude_patterns)
    )
    (
        repo_path,
        file_paths,
        collection_truncated,
        files_excluded_with_extractor,
        files_excluded_without_extractor,
        languages_excluded_with_extractor,
        error,
    ) = resolve_result
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
                refine=refine,
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
    #
    # Bug #1897 P2-1 (code review finding): the guard below used to read
    # `"error" not in result`, checking KEY PRESENCE rather than the
    # error's VALUE. Both `_build_graph_analysis_result` (success path,
    # `"error": None`) and `_graph_error_result` (failure path, `"error":
    # {...}`) -- and every early-return shape in
    # `_run_backend_analysis_with_admission_control` above, including
    # `xray_cell_queue_timeout` -- ALWAYS include an `"error"` key on a
    # REAL `RustNativeBackend.run_graph_analysis` call, so `"error" not in
    # result` was unconditionally False for every real backend response:
    # this whole force-set block was DEAD CODE against production traffic
    # (only reachable when a test mocks the backend to return a dict that
    # omits "error" entirely, which no real code path ever does). Checking
    # the VALUE instead restores the original intent -- skip the
    # force-set only on a genuine failure (a non-None "error").
    if result.get("error") is None:
        result = dict(result)
        # Bug #1907: surface the two new candidate-collection counters
        # UNCONDITIONALLY (even when both are zero) so the response schema
        # is stable regardless of whether include/exclude patterns excluded
        # anything -- mirrors `degradation_keys`'s own always-present
        # fields in `_build_graph_analysis_result`.
        degradation = dict(result.get("degradation") or {})
        degradation["files_excluded_with_extractor"] = files_excluded_with_extractor
        degradation["files_excluded_without_extractor"] = (
            files_excluded_without_extractor
        )
        result["degradation"] = degradation
        result["languages_excluded_with_extractor"] = languages_excluded_with_extractor

        needs_incomplete_reason = False
        if collection_truncated:
            result["truncated_by_max_files"] = True
            needs_incomplete_reason = True
        if files_excluded_with_extractor:
            # THE core fix (Bug #1907): a file whose language HAS a graph
            # extractor was excluded by include/exclude patterns before it
            # ever became a candidate -- narrowing the scope can never be
            # allowed to manufacture a clean fact_graph_complete=true while
            # the graph is missing every call site in that language. A file
            # whose language has NO extractor at all
            # (files_excluded_without_extractor) does NOT trip this --
            # including it would not have produced any real call edge
            # either way, so its exclusion changes nothing about
            # completeness.
            needs_incomplete_reason = True
        if needs_incomplete_reason:
            result["fact_graph_complete"] = False
            # Bug #1897 P2-1: mirrors the `main.rs` `files_from_truncated`
            # fix (`run_build_graph`'s CLI wrapper) -- a Python-side
            # candidate-collection gap (truncation OR an excluded
            # extractor-backed file) is semantically the same "repo-level
            # file set was cut short" condition `RepoIndexResult.
            # completeness_reasons` already represents for a Rust-side
            # truncation, so it must ALSO carry a reason, never leave
            # fact_graph_complete=False with nothing pushed onto
            # completeness_reasons (the exact "false with no reason"
            # symptom the whole #1897 fix exists to kill) -- and Bug #1907
            # feeds this SAME existing list rather than inventing a
            # parallel undiscoverable channel.
            reasons = list(result.get("completeness_reasons") or [])
            if "repo_index_incomplete" not in reasons:
                reasons.append("repo_index_incomplete")
            result["completeness_reasons"] = reasons

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
    # extension or an escaped path -- e.g. `README.md`) are NOT disjoint
    # per-file counters, despite an earlier version of this comment
    # claiming `repo_index.rs::process_one_file` routes each candidate into
    # "at most one" of them (Bug #1903). `record_fused_result`
    # (`repo_index.rs`) uses four INDEPENDENT `if`s: `files_with_parse_
    # errors` is set from `fused_result.has_syntax_error` -- a generic
    # tree-sitter parse that runs regardless of whether a `LanguageExtractor`
    # exists for that language -- while `files_with_unsupported_language` is
    # set from `ExtractionStatus::LanguageNotSupported`. A file whose
    # extension has NO extractor can genuinely trip BOTH at once: its
    # language's real tree-sitter grammar still parses it (so `language_
    # for_extension` succeeds and `has_syntax_error` is measured for real),
    # and if that source is ALSO malformed under its own grammar,
    # `has_syntax_error` is true even though the file was never going to be
    # extracted either way. A file can therefore legitimately be counted
    # under BOTH `files_with_unsupported_language` and `files_with_parse_
    # errors` simultaneously -- these are orthogonal signals, not a clean
    # partition, and restructuring `record_fused_result` to force
    # disjointness is out of scope here (it would lose real information: a
    # file being both "wrong language" and "malformed enough that even a
    # fallback parse chokes" are both true facts worth keeping separate).
    #
    # `unsupported_language_count + unrecognized_extension_count ==
    # len(file_paths)` can therefore be reached "by accident" even when a
    # genuine parse failure occurred within that same candidate set (a file
    # double-counted into `files_with_unsupported_language` AND `files_
    # with_parse_errors` still only occupies ONE slot in this sum, so the
    # equality can hold while `files_with_parse_errors > 0`). The guard
    # below explicitly excludes any candidate set with a nonzero `files_
    # with_parse_errors` from `no_supported_files`, rather than relying on
    # an equality that doesn't actually prove "nothing here had a real
    # parse failure".
    #
    # Genuine read failures (`files_with_read_errors`, `files_with_
    # extractor_panics`) remain deliberately EXCLUDED from the sum itself --
    # those files have a recognized, supported-language extension (only
    # Java has one, and only Java files can produce these) and DID reach
    # the extractor; their failure is a different, already independently
    # signaled degradation, not "no supported files".
    if result.get("ok") is True and result.get("status") == "ran_ok" and file_paths:
        degradation = result.get("degradation") or {}
        unsupported_language_count = (
            degradation.get("files_with_unsupported_language") or 0
        )
        unrecognized_extension_count = (
            degradation.get("unreadable_or_unsupported_files") or 0
        )
        # Bug #1903: a nonzero genuine parse-error count means at least one
        # candidate file had a REAL syntax failure -- never label that
        # repo "no supported files", even if the (non-disjoint) counter sum
        # above happens to equal the file count.
        genuine_parse_error_count = degradation.get("files_with_parse_errors") or 0
        if (
            genuine_parse_error_count == 0
            and unsupported_language_count + unrecognized_extension_count
            == len(file_paths)
        ):
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

    MUST run before the repo-count cap check (N copies of one alias must
    not consume N cap slots).
    """
    return list(dict.fromkeys(aliases))


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


def _sanitized_exception_message(exc: BaseException) -> str:
    """Builds a public-safe message for an unhandled per-alias pipeline
    exception (Bug #1913 P1, dual review).

    Two failures in the pre-review version this replaces:
    1. `str(exc)` was returned RAW into a public MCP response. A
       `FileNotFoundError`/`OSError` raised by `_resolve_repo_and_files`
       (the live exception surface this guard exists for) carries an
       ABSOLUTE SERVER PATH in its message -- this repository's own
       disclosure rules forbid system internals (mount paths, hostnames)
       reaching a caller, and `RustNativeBackend.run_graph_analysis`'s own
       structured-error path already never does this (every
       `error_message` goes through `_sanitize_error_message` at
       `rust_backend.py:433` before it leaves the process). This is the
       SAME sanitiser, applied lazily (mirrors this module's existing
       lazy `RustNativeBackend` import) to keep parity with that path.
    2. `str(exc)` is EMPTY for a bare `RuntimeError()`/`KeyError()` --
       verified directly -- so a bare raise previously produced
       `"message": ""`, zero diagnostic content. The exception TYPE name
       is always included, so even a detail-free exception still tells
       the caller something concrete happened and what kind.
    """
    from code_indexer.xray.rust_backend import _sanitize_error_message

    exc_type = type(exc).__name__
    detail = _sanitize_error_message(str(exc))
    return f"{exc_type}: {detail}" if detail else exc_type


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

    Bug #1913 P2-A (dual review, both reviewers independently): also
    GUARANTEES a non-empty top-level `message`. `_graph_error_result`
    (rust_backend.py) -- the real compile/build/analyze failure shape,
    the single most likely real failure of this tool -- returns
    `{"error": {"error_type", "error_message"}, ...}` with NO top-level
    `message` key at all. Left unhandled, that shape produced an
    `errors[]` entry with `error` as a DICT and `message` silently
    absent: a caller doing `entry["message"]` got a `KeyError`, one
    treating `entry["error"]` as a string got a dict. When `message` is
    still missing/falsy after the allowlisted copy AND `error` is a dict,
    synthesize it from the (already-truncated) `error["error_message"]`,
    falling back to `error["error_type"]` -- so the `{repository_alias,
    error, message}` contract `analyze_graph.md` documents is actually
    TRUE for every entry, not merely described as true.
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
    if not bounded.get("message"):
        error_value = bounded.get("error")
        if isinstance(error_value, dict):
            bounded["message"] = (
                error_value.get("error_message")
                or error_value.get("error_type")
                or "analyze_graph pipeline failure (no further detail available)"
            )
    return bounded


async def _run_multi_repo_analyze_graph(
    aliases: List[str],
    evaluator_code: str,
    include_patterns: List[str],
    exclude_patterns: List[str],
    timeout_seconds: int,
    refine: bool = False,
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
      - Time: `timeout_seconds` is a PER-REPOSITORY budget, passed UNDIVIDED
        to every alias -- matching `xray_search`'s own multi-repo contract
        (handlers/xray.py:914-1072, which resolves timeout_seconds ONCE and
        hands the FULL value to every alias's job). Bug #1913 (supersedes
        Issue #1902 P9 review, P2's up-front admission predicate): rather
        than refusing a request whose THEORETICAL worst case
        (`alias_count * timeout_seconds`) exceeds a ceiling before running
        anything, this function tracks REAL elapsed wall clock from
        `t0 = time.monotonic()` and, before starting each alias, checks
        whether `time.monotonic() - t0` has already reached
        `_MULTI_REPO_TOTAL_TIMEOUT_CEILING_SECONDS`. If so, every remaining
        alias (the current one included) gets a `multi_repo_deadline_
        exceeded` entry in `errors[]` instead of running, and the loop
        stops -- aliases that already completed keep their real results.
        This admits the common case (10-15 repos at the default timeout,
        which the old worst-case predicate refused up front even though the
        typical run finishes in a fraction of the theoretical total).
        P2-B (dual review): this is a BETWEEN-ALIAS ADMISSION GATE, NOT a
        wall-clock bound -- it can refuse to START a later alias once the
        threshold is observed, but it CANNOT bound the alias already in
        flight when the check last passed. That in-flight alias can wait
        up to `timeout_seconds` on `limiter.acquire(timeout=...)` (OUTSIDE
        `run_graph_analysis`'s own deadline), then run up to another
        `timeout_seconds` inside it, on top of an entirely UNCLOCKED
        `_resolve_repo_and_files` walk that can block FOREVER on a wedged
        `hard`-NFS host -- see `_MULTI_REPO_TOTAL_TIMEOUT_CEILING_SECONDS`'s
        own comment for the concrete worst case and what a real bound
        would require. `aliases` arriving here is therefore ALREADY: (a)
        deduplicated (`_dedupe_preserve_order`) and (b) within the
        repo-count cap -- both enforced by the caller before dispatch; the
        admission gate is enforced HERE, per-alias, rather than by the
        caller up front.
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
        that resolved, ran, AND were successfully truncated/cached.
      - "errors": [{**_bound_repo_error_payload(error_dict),
        "repository_alias": alias}] for aliases that failed (unknown alias,
        no candidate files, a real compile error, an unhandled pipeline
        exception, a per-repo PayloadCache page-set write failure
        surfaced by `_truncate_graph_result` as {"success": False,
        "error": "cache_store_failed", ...} -- Bug #1928 final round,
        Opus P3.2 -- never left in results[alias] where the top-level
        "ok" flag would stay True, ...) or were never started because
        the elapsed deadline
        had already been reached (`{"error": "multi_repo_deadline_
        exceeded", "message": <ceiling + remediation text>,
        "repository_alias": alias}`) -- never silently dropped, and never
        an unbounded copy of the full per-alias result (Issue #1902 P9
        review, P3). `repository_alias` is added AFTER the allowlisted
        spread so it can never be clobbered by a same-named key inside the
        bounded payload. Every entry in `errors[]`, regardless of which
        code produced it, carries a non-empty `message` -- a bare
        `{"error": ...}` with no explanation of what happened or how to
        recover is never an acceptable shape for this served contract.
    """
    results: Dict[str, Any] = {}
    errors: List[Dict[str, Any]] = []
    t0 = time.monotonic()
    for index, alias in enumerate(aliases):
        # Bug #1913: gate STARTING the next alias on real elapsed wall
        # clock, rather than refusing the whole request up front on a
        # theoretical worst case. Every alias not yet started (this one
        # included) is recorded as deadline-exceeded and the loop stops --
        # aliases that already completed keep their real results.
        if time.monotonic() - t0 >= _MULTI_REPO_TOTAL_TIMEOUT_CEILING_SECONDS:
            deadline_message = (
                f"the request's elapsed wall clock reached the "
                f"{_MULTI_REPO_TOTAL_TIMEOUT_CEILING_SECONDS}s ceiling before "
                "this repository was started; earlier repositories' results "
                "are preserved -- re-request the remainder."
            )
            for remaining_alias in aliases[index:]:
                errors.append(
                    {
                        "error": "multi_repo_deadline_exceeded",
                        "message": deadline_message,
                        "repository_alias": remaining_alias,
                    }
                )
            break
        try:
            repo_result = await _run_analyze_graph_pipeline(
                evaluator_code,
                alias,
                include_patterns,
                exclude_patterns,
                timeout_seconds,
                refine,
            )
        except Exception as exc:  # noqa: BLE001 -- per-alias guard, Bug #1913
            # An unhandled exception from a single alias (e.g. a
            # `_resolve_repo_path`/thread-pool failure -- the live surface
            # noted in the issue) must not 500 the whole request and
            # discard every earlier alias's completed work. Record it into
            # `errors[]`, bounded the same way a structured pipeline
            # failure already is, and move on to the next alias.
            #
            # `logger.exception` (not `.warning`) -- this branch's WHOLE
            # definition is "an unhandled exception happened"; discarding
            # the traceback here throws away the only diagnostic this
            # guard will ever have (Bug #1913 P1, dual review).
            logger.exception(
                "analyze_graph multi-repo pipeline exception for alias %r",
                alias,
            )
            errors.append(
                {
                    **_bound_repo_error_payload(
                        {
                            "error": "multi_repo_pipeline_exception",
                            # Bug #1913 P1: NEVER the raw `str(exc)` -- see
                            # `_sanitized_exception_message`'s own
                            # docstring for why (server-path leakage, and
                            # the empty-message case for a bare raise).
                            "message": _sanitized_exception_message(exc),
                        }
                    ),
                    "repository_alias": alias,
                }
            )
            continue
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
            # Bug #1928 P2: this handler is `async def` and runs directly
            # on the event loop -- truncation now does real synchronous
            # DB I/O (PayloadCache.store_batch()/store()) plus JSON
            # serialization, which must never block the loop.
            truncated = await anyio.to_thread.run_sync(
                _truncate_graph_result, repo_result
            )
            # Bug #1928 final round (Opus P3.2): _truncate_graph_result can
            # itself fail (a page-set write failure surfaced as
            # {"success": False, "error": "cache_store_failed", ...}) --
            # that must be routed into errors[] like any other per-repo
            # failure, never land in results[alias] with the top-level
            # "ok" flag staying True (computed as len(errors) == 0). Its
            # shape already has a truthy "error" key, matching what
            # _bound_repo_error_payload expects.
            if truncated.get("success") is False:
                errors.append(
                    {
                        **_bound_repo_error_payload(truncated),
                        "repository_alias": alias,
                    }
                )
            else:
                results[alias] = truncated
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
       pre-flight the evaluator ONCE (`validate_rust_evaluator` -- a single
       top-level rejection, never one per alias), then delegate to
       `_run_multi_repo_analyze_graph` (one real analysis PER alias, gated
       by an ELAPSED WALL-CLOCK DEADLINE rather than an up-front admission
       predicate -- Bug #1913; see that function's docstring for the
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
        multi_repo_deadline_exceeded     -- multi-repo path only, per-alias in
                                             `errors[]` (never a top-level rejection):
                                             this alias was never started because the
                                             request's elapsed wall clock already
                                             reached `_MULTI_REPO_TOTAL_TIMEOUT_
                                             CEILING_SECONDS` (600s) when its turn
                                             came up (Bug #1913). Earlier aliases'
                                             real results are preserved.
        multi_repo_pipeline_exception    -- multi-repo path only, per-alias in
                                             `errors[]`: an unhandled exception was
                                             raised while running this alias's
                                             pipeline (e.g. a repo-resolution or
                                             thread-pool failure); the request is
                                             never failed wholesale and earlier
                                             aliases' real results are preserved
                                             (Bug #1913).
        include_patterns_invalid /
        exclude_patterns_invalid         -- not a list of strings, or a
                                             malformed glob.
        timeout_seconds_invalid          -- not a finite number.
        refine_invalid                   -- refine (Bug #1909) is present but not a
                                             boolean.
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
        extractor_extension_lookup_failed -- could not determine which file
                                             extensions have a graph extractor
                                             (Bug #1907; `xray-cli --print-graph-
                                             extractor-extensions` failed or the
                                             binary is missing). The request fails
                                             loudly rather than silently assuming no
                                             excluded file mattered (single-repo
                                             path; per-alias in `errors[]` for a
                                             multi-repo request).
    """
    if user is None or not user.has_permission("query_repos"):
        return _mcp_response(
            {
                "error": "auth_required",
                "message": "authentication required, or user lacks query_repos permission",
            }
        )

    (
        repo_alias,
        evaluator_code,
        include_patterns,
        exclude_patterns,
        timeout_seconds,
        refine,
        error,
    ) = _parse_analyze_graph_request(params)
    if error is not None:
        return _mcp_response(error)

    if isinstance(repo_alias, list):
        # Issue #1902 P9 review, P3: dedup BEFORE the cap check --
        # `_enforce_repo_count_cap`'s own docstring documents that it
        # expects a post-dedup list, and N copies of one alias must not
        # consume N cap slots.
        repo_alias = _dedupe_preserve_order(repo_alias)
        cap_breach = _enforce_repo_count_cap(repo_alias)
        if cap_breach is not None:
            return cap_breach_response(cap_breach)
        # Bug #1913: the aggregate wall-clock ceiling is no longer an
        # up-front admission predicate here -- it is enforced PER ALIAS,
        # against REAL elapsed time, inside `_run_multi_repo_analyze_graph`
        # itself (see that function's docstring).

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
            refine,
        )
        return _mcp_response(multi_result)

    result = await _run_analyze_graph_pipeline(
        evaluator_code,
        repo_alias,
        include_patterns,
        exclude_patterns,
        timeout_seconds,
        refine,
    )
    # H7 (consolidated review, Issue #1811/Bug #1812): route through the
    # same PayloadCache truncation every other xray result gets -- a
    # whole-repo graph analysis's findings[]/refine[] can be strictly
    # larger than a single-file search's matches[]/evaluation_errors[].
    # Bug #1928 P2: offloaded via anyio.to_thread.run_sync -- see
    # _truncate_graph_result's own docstring for why (real DB I/O +
    # serialization must never block this async handler's event loop).
    truncated = await anyio.to_thread.run_sync(_truncate_graph_result, result)
    return _mcp_response(truncated)


def _register(registry: Dict[str, Any]) -> None:
    """Register the analyze_graph handler in the HANDLER_REGISTRY."""
    registry["analyze_graph"] = handle_analyze_graph
