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

Issue #1935 Part 2: this module used to be a single 1,407-line file. It is
now a package split by concern -- request parsing/validation
(`_request_parsing.py`), whole-repo candidate-file collection
(`_candidates.py`), and result truncation/multi-repo error shaping
(`_result_shaping.py`) -- with every symbol re-exported here so every
existing `from ...xray_graph import X` import keeps working unchanged.

The four functions that remain directly in this file
(`_resolve_repo_and_files`, `_run_analyze_graph_pipeline`,
`_run_multi_repo_analyze_graph`, `handle_analyze_graph`) do so because the
existing test suite monkeypatches several of their collaborators at the
flat `code_indexer.server.mcp.handlers.xray_graph.<name>` path (e.g.
`_resolve_repo_path`, `_resolve_repo_and_files`, `_get_xray_cell_limiter`,
`_run_analyze_graph_pipeline`, `_GRAPH_CANDIDATE_FILES_CAP`,
`time.monotonic`) to intercept a caller's behavior. Such a patch only takes
effect when the calling function's bare-name lookup resolves through the
exact module object being patched (a function's global name lookups always
go through the module it was DEFINED in, never the module it happens to be
called from) -- so keeping these four callers physically in this
`__init__.py`, with every collaborator name imported into ITS namespace,
preserves that patch semantics unchanged after the split.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import anyio

from code_indexer.server.auth.user_manager import User
from code_indexer.xray.sandbox import validate_rust_evaluator

from .._utils import (
    _enforce_repo_count_cap,
    _mcp_response,
    _parse_and_collapse_repo_alias,  # noqa: F401 -- re-exported for import-path parity
    cap_breach_response,
)
from .. import xray_truncation  # noqa: F401 -- re-exported for import-path parity
from ..xray import (
    _get_xray_cell_limiter,
    _lazy_singleton_app_or_none,  # noqa: F401 -- re-exported for import-path parity
    _pattern_scope_alias,
    _resolve_evaluator_code_off_loop,
    _resolve_repo_path,
)

from ._candidates import (  # noqa: F401 -- re-exported for import-path parity
    _GRAPH_CANDIDATE_FILES_CAP,
    _SKIP_DIR_NAMES,
    _collect_graph_candidate_files,
)
from ._request_parsing import (  # noqa: F401 -- re-exported for import-path parity
    _DEFAULT_TIMEOUT_SECONDS,
    _TIMEOUT_MIN,
    _TIMEOUT_MAX,
    _validate_glob_patterns,
    _parse_timeout_seconds,
    _parse_refine_flag,
    _parse_analyze_graph_request,
)
from ._result_shaping import (  # noqa: F401 -- re-exported for import-path parity
    _truncate_graph_result,
    _dedupe_preserve_order,
    _MULTI_REPO_ERROR_ALLOWED_FIELDS,
    _MULTI_REPO_ERROR_STRING_MAX_CHARS,
    _truncate_error_string,
    _sanitized_exception_message,
    _bound_repo_error_payload,
)

logger = logging.getLogger(__name__)


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
        the elapsed deadline had already been reached (`{"error": "multi_repo_deadline_
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
            # "ok" flag staying True. Its shape already has a truthy
            # "error" key, matching what _bound_repo_error_payload expects.
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
