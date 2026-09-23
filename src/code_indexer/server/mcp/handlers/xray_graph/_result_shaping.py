"""Result truncation and multi-repo error shaping for analyze_graph
(Issue #1935 Part 2).

Moved verbatim out of the monolithic xray_graph.py -- pure relocation, zero
behaviour change. See that package's __init__.py module docstring for the
overall analyze_graph tool context.

`_bound_repo_error_payload`'s docstring runs long only because it
documents a real, previously-reviewed defect fix (Bug #1913 P2-A) -- it is
copied character-for-character from the currently-committed xray_graph.py,
not new code.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..xray import _lazy_singleton_app_or_none
from .. import xray_truncation

# Hardcoded to the package's own name (not `__name__`, which for this
# submodule would be "...xray_graph._result_shaping") so this shares the
# SAME logger identity as xray_graph/__init__.py's own logger -- this
# codebase's log-audit gate is keyed on logger names, and splitting that
# identity per-submodule would silently break it. A same-name
# logging.getLogger() call returns the identical cached Logger singleton
# regardless of which module constructs it, so this needs no import back
# to __init__.py (which imports this module first, at package load time).
logger = logging.getLogger("code_indexer.server.mcp.handlers.xray_graph")


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
