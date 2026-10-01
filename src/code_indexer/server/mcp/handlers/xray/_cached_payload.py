"""cidx_fetch_cached_payload MCP handler (Issue #1935 Part 2).

Moved verbatim out of the monolithic xray.py -- pure relocation, zero
behaviour change. Every function below is an intact, unmodified copy of
its HEAD counterpart (git show HEAD:.../xray.py:2004-2152); none was
decomposed into new helper functions.
"""

from __future__ import annotations

from typing import Any, Dict

from code_indexer.server.auth.user_manager import User

from ._infra import (
    _lazy_singleton_app_or_none,
    logger,  # shared logger name -- see _infra.py's own comment
)

from .. import xray_truncation
from .._utils import _mcp_response


def _validate_pv1_page(raw_page: Any) -> Any:
    """Bug #1928 final round (Codex P1 / Opus P3.3): strict page
    validation for pages-v1 (xray-pv1-*) handles ONLY. Accepts a real
    int or a decimal-digit string (e.g. "2", coerced to int -- ordinary
    numeric-string parsing, not clamping); rejects 0, negative values,
    floats, and bools (bool is an int subclass in Python) outright, with
    NO clamping/coercion of an otherwise-invalid value.

    Uses `str.isdecimal()` rather than `str.isdigit()`: isdigit() also
    accepts non-decimal digit characters (e.g. superscript "²")
    that int() cannot parse and would raise ValueError on.

    Returns the validated int page, or a `str` error message on failure.
    """
    if isinstance(raw_page, bool):
        return f"page must be an integer >= 1, got {raw_page!r}"
    if isinstance(raw_page, int):
        candidate = raw_page
    elif isinstance(raw_page, str) and raw_page.isdecimal():
        candidate = int(raw_page)
    else:
        return f"page must be an integer >= 1, got {raw_page!r}"
    if candidate < 1:
        return f"page must be an integer >= 1, got {raw_page!r}"
    return candidate


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
        invalid_page    — page is not a real int >= 1, or a valid
                           decimal-digit string (Bug #1928 final round,
                           Codex P1: strict, no-clamp validation applies
                           ONLY to pages-v1/xray-pv1-* handles -- a
                           legacy handle keeps its pre-#1928 lenient
                           max(1, int(page or 1)) coercion instead).
        malformed_cache_entry — cache_handle carries the pages-v1 prefix
                           but its stored content fails strict validation
                           (Bug #1928 round 3, P2).
        cache_expired   — handle not found or expired.
        cache_unavailable — PayloadCache not configured.
    """
    if user is None or not user.has_permission("query_repos"):
        return _mcp_response({"error": "auth_required"})

    cache_handle: str = params.get("cache_handle", "")
    raw_page = params.get("page", 1)
    if raw_page is None:
        raw_page = 1

    # Bug #1928 (Codex, post-final-round): HEAD's missing-handle check must
    # run BEFORE any `.startswith()` call -- a falsy, non-string
    # cache_handle (None, False, 0, [], ...) is a legal value the caller
    # can pass, and `.startswith()` on any of those raises AttributeError.
    if not cache_handle:
        return _mcp_response(
            {
                "success": False,
                "error": "missing_handle",
                "message": "cache_handle parameter is required",
            }
        )

    # isinstance-guarded so a TRUTHY non-string value (e.g. an int or a
    # non-empty list) never reaches .startswith() either -- it simply
    # isn't a pv1 handle, and falls through to the legacy path exactly as
    # HEAD did (HEAD never isinstance-checked cache_handle at all).
    if isinstance(cache_handle, str) and cache_handle.startswith(
        xray_truncation._PAGES_V1_HANDLE_PREFIX
    ):
        validated = _validate_pv1_page(raw_page)
        if isinstance(validated, str):
            return _mcp_response(
                {"success": False, "error": "invalid_page", "message": validated}
            )
        page: int = validated
    else:
        # Bug #1928 final round (Codex P1): legacy (non pages-v1) handles
        # keep their EXACT pre-#1928 lenient coercion -- strict, no-clamp
        # validation is a pages-v1-only contract, out of scope for every
        # other cache consumer.
        page = max(1, int(raw_page or 1))

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
        result = xray_truncation.fetch_cached_page(payload_cache, cache_handle, page)
        return _mcp_response({"success": True, **result})
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
        if isinstance(exc, xray_truncation.MalformedManifestError):
            logger.error(
                "cidx_fetch_cached_payload: malformed pages-v1 manifest for %s: %s",
                cache_handle,
                exc,
            )
            return _mcp_response(
                {
                    "success": False,
                    "error": "malformed_cache_entry",
                    "message": str(exc),
                    "cache_handle": cache_handle,
                }
            )
        logger.warning("cidx_fetch_cached_payload error for %s: %s", cache_handle, exc)
        return _mcp_response({"success": False, "error": str(exc)})
