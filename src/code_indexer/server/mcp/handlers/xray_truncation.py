"""Shared PayloadCache truncation/pagination for xray-family results
(xray_search/xray_explore matches+evaluation_errors, analyze_graph
findings+refine, xray_search_batch matches+errors+evaluation_errors).

This module is the public orchestrator/facade -- whole-entry page
packing lives in xray_truncation_pages.py, and the atomic page-set
write + fetch-side manifest resolution live in xray_truncation_fetch.py
(split out to stay under the project's file-size limit, Rule #6). Both
are imported and re-exported here so every existing caller keeps using
`xray_truncation.pack_entries_into_pages`, `xray_truncation.fetch_cached_page`,
`xray_truncation.PageSetStoreError`, `xray_truncation.MalformedManifestError`,
and `xray_truncation._PAGES_V1_HANDLE_PREFIX` unchanged.

Bug #1928 round 2 (two independent reviewers rejected the first pass --
padded/shrunk pages -- and converged on this design):

- Cache pages hold WHOLE, UNMODIFIED entries -- never shrunk, never
  padded. An entry that alone exceeds the page budget gets its OWN,
  necessarily oversized, page rather than being cut.
- The inline budget IS the page budget (payload_max_fetch_size_chars) --
  no second, smaller preview_size_chars threshold.
- Field-level shrinking is used ONLY to build the INLINE response, and
  ONLY when the very first entry (across all fields, in field order)
  cannot fit alone within the budget -- flagged inline_entry_truncated.
  If even the shrunk form cannot fit, the inline arrays are empty and a
  WARNING is logged (never silent) -- the full entry remains available,
  unmodified, via the cache.

Bug #1928 round 3 (Codex REJECT on a real P1, plus Opus/Codex P2/P3
follow-ups) -- see xray_truncation_fetch.py's module docstring for the
full P1/P2 detail:

- P1: pages and the pages-v1 manifest are written as ONE atomic
  page-set batch, sharing a single timestamp/expiry, with any write
  failure PROPAGATING as `PageSetStoreError`.
- P2: manifest detection is a dedicated handle-prefix discriminator
  (never content-shape sniffing), with strict validation.
- P3 (Codex): when `payload_cache` is None, the result is still BOUNDED
  (never the full unbounded arrays) -- `cache_unavailable: True` is set,
  `cache_handle` stays None, and an ERROR is logged when data actually
  had to be bounded.
- P3 (Opus): page packing is O(n), not O(n^2) -- see
  xray_truncation_pages.py.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from code_indexer.server.cache.payload_cache import PayloadCacheConfig

from .xray_truncation_fetch import (  # noqa: F401 -- re-exported public API
    MalformedManifestError,
    PageSetStoreError,
    _PAGES_V1_FORMAT,
    _PAGES_V1_HANDLE_PREFIX,
    _parse_and_validate_manifest,
    build_fetch_tool_hint,
    fetch_cached_page,
    store_pages,
)
from .xray_truncation_pages import (  # noqa: F401 -- re-exported public API
    _entry_alone_size,
    _pair_size,
    pack_entries_into_pages,
)

logger = logging.getLogger(__name__)

# Fallback packing budget used ONLY when payload_cache is unavailable
# (Bug #1928 P3, Codex) -- there is no real cache to read
# max_fetch_size_chars from, so the facade's own documented default is
# the single source of truth here (never a duplicated magic number).
_CACHE_UNAVAILABLE_FALLBACK_BUDGET_CHARS = PayloadCacheConfig().max_fetch_size_chars

# Starting point for the adaptive INLINE-ONLY shrink (see
# _shrink_entry_to_fit) -- halved each round, bounded rounds (Rule 14,
# anti-unbounded-loop). Never applied to what gets cached.
_INLINE_SHRINK_STRING_CAP = 500
_INLINE_SHRINK_LIST_CAP = 5
_INLINE_SHRINK_MAX_ROUNDS = 16


# ---------------------------------------------------------------------------
# Inline-only adaptive shrink (never applied to what gets cached)
# ---------------------------------------------------------------------------


def _shrink_entry_to_fit(
    field_names: List[str], field_name: str, entry: Any, budget_chars: int
) -> Optional[Any]:
    """Try to shrink `entry` (for the INLINE preview ONLY) below
    budget_chars via adaptive halving of its top-level string/list field
    caps. Returns the shrunk entry if it now fits, or None if even a
    fully-emptied form still does not (a pathological budget). Never
    mutates what gets cached.
    """
    if not isinstance(entry, dict):
        return None
    string_cap = _INLINE_SHRINK_STRING_CAP
    list_cap = _INLINE_SHRINK_LIST_CAP
    for _ in range(_INLINE_SHRINK_MAX_ROUNDS):
        capped: Dict[str, Any] = {}
        for key, value in entry.items():
            if isinstance(value, list):
                capped[key] = value[: max(0, list_cap)]
            elif isinstance(value, str) and len(value) > max(0, string_cap):
                capped[key] = (value[:string_cap] + "...") if string_cap > 0 else ""
            else:
                capped[key] = value
        if _entry_alone_size(field_names, field_name, capped) <= budget_chars:
            return capped
        if string_cap <= 0 and list_cap <= 0:
            return None
        string_cap //= 2
        list_cap = max(0, list_cap - 1)
    return None


def _build_inline_preview(
    field_names: List[str],
    fields: Dict[str, List[Any]],
    pages: List[Dict[str, list]],
    budget_chars: int,
) -> Tuple[Dict[str, list], bool]:
    """Build the inline preview for an oversized result: page[0] as-is,
    UNLESS the very first entry (across all fields, in field order)
    cannot fit alone -- in which case it is shrunk (inline copy only;
    never mutates the cached page) and `inline_entry_truncated=True` is
    returned. If even the shrunk form does not fit, the inline preview
    is empty and a WARNING is logged (the full entry stays recoverable
    via the cache).
    """
    first_field_entry = next(
        ((f, e) for f in field_names for e in fields.get(f, [])), None
    )
    if first_field_entry is None:
        return pages[0], False

    first_field, first_entry = first_field_entry
    if _entry_alone_size(field_names, first_field, first_entry) <= budget_chars:
        return pages[0], False

    shrunk = _shrink_entry_to_fit(field_names, first_field, first_entry, budget_chars)
    inline_page: Dict[str, list] = {f: [] for f in field_names}
    if shrunk is not None:
        inline_page[first_field] = [shrunk]
    else:
        logger.warning(
            "xray truncation: inline preview is empty -- the first entry "
            "alone (field=%s) exceeds the %d-char budget even after "
            "adaptive shrinking; the full entry remains available via "
            "cache_handle",
            first_field,
            budget_chars,
        )
    return inline_page, True


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _finalize_truncated(
    base: Dict[str, Any],
    payload_cache: Any,
    field_names: List[str],
    fields: Dict[str, List[Any]],
    budget_chars: int,
) -> Dict[str, Any]:
    """Cache the FULL, unmodified pages (as ONE atomic page-set batch)
    and assemble the truncated response (inline preview + cache_handle +
    metadata).

    Raises:
        PageSetStoreError: propagated from store_pages() -- the caller
            must surface this as an explicit error, never a handle.
    """
    pages = pack_entries_into_pages(field_names, fields, budget_chars)
    inline_page, inline_entry_truncated = _build_inline_preview(
        field_names, fields, pages, budget_chars
    )
    cache_handle, total_pages = store_pages(payload_cache, field_names, pages)

    base.update(inline_page)
    base["cache_handle"] = cache_handle
    base["has_more"] = True
    base["truncated"] = True
    base["total_pages"] = total_pages
    base["inline_entry_truncated"] = inline_entry_truncated
    base["fetch_tool_hint"] = build_fetch_tool_hint(
        field_names, cache_handle, total_pages
    )
    return base


def _degrade_cache_unavailable(
    result: Dict[str, Any], field_names: List[str]
) -> Dict[str, Any]:
    """Bug #1928 P3 (Codex): payload_cache is None -- there is nothing to
    store overflow pages in, so the result MUST still be bounded rather
    than returned in full (the round-2 design silently returned the
    entire unbounded result here, defeating the whole truncation
    contract for the duration of any cache outage). Uses the facade's
    own documented default budget (no real cache to read the CURRENT
    config from). Always marks `cache_unavailable: True` so a caller
    can distinguish "genuinely small" from "cache was down".
    """
    fields = {f: result.get(f, []) for f in field_names}
    base = {k: v for k, v in result.items() if k not in field_names}
    budget_chars = _CACHE_UNAVAILABLE_FALLBACK_BUDGET_CHARS

    total_size = _pair_size(field_names, fields)
    if total_size <= budget_chars:
        base.update(fields)
        base["cache_handle"] = None
        base["has_more"] = False
        base["truncated"] = False
        base["cache_unavailable"] = True
        return base

    pages = pack_entries_into_pages(field_names, fields, budget_chars)
    inline_page, inline_entry_truncated = _build_inline_preview(
        field_names, fields, pages, budget_chars
    )
    logger.error(
        "xray truncation: PayloadCache unavailable -- returning a "
        "bounded inline preview only (1 of %d page(s)); the remaining "
        "data cannot be cached or fetched until the cache is available "
        "again",
        len(pages),
    )
    base.update(inline_page)
    base["cache_handle"] = None
    base["has_more"] = len(pages) > 1
    base["truncated"] = True
    base["total_pages"] = len(pages)
    base["inline_entry_truncated"] = inline_entry_truncated
    base["cache_unavailable"] = True
    return base


def truncate_result_fields(
    result: Dict[str, Any],
    payload_cache: Any,
    field_names: List[str],
) -> Dict[str, Any]:
    """Apply PayloadCache truncation to `field_names` array fields of an
    X-Ray-family result. Shared core behind xray_search/xray_explore
    (matches, evaluation_errors), analyze_graph (findings, refine), and
    xray_search_batch (matches, errors, evaluation_errors).

    See the module docstring for the full Bug #1928 contract.

    Bug #1928 P3 (Codex): when payload_cache is None, returns a BOUNDED
    degrade response (see _degrade_cache_unavailable) -- never the full
    unbounded result.

    Raises:
        PageSetStoreError: the atomic page-set write failed -- the
            caller must surface this as an explicit error, never return
            a handle for data that was not durably written.
    """
    if payload_cache is None:
        return _degrade_cache_unavailable(result, field_names)

    fields = {f: result.get(f, []) for f in field_names}
    budget_chars = payload_cache.config.max_fetch_size_chars
    base = {k: v for k, v in result.items() if k not in field_names}

    # Decided by the GENUINE serialized size, never by how many pages the
    # packer happens to produce -- a single entry that alone exceeds
    # budget_chars also packs into exactly one page, and must still be
    # treated as truncated (cached, flagged, possibly shrunk for the
    # inline copy) rather than returned verbatim as if it fit.
    total_size = _pair_size(field_names, fields)
    if total_size <= budget_chars:
        base.update(fields)
        base["cache_handle"] = None
        base["has_more"] = False
        base["truncated"] = False
        return base

    try:
        return _finalize_truncated(
            base, payload_cache, field_names, fields, budget_chars
        )
    except PageSetStoreError as exc:
        # Bug #1928 final round (Opus P4.6): `base` here is the SAME dict
        # `_finalize_truncated` was called with, and store_pages() (the
        # only thing that can raise) is called BEFORE `_finalize_truncated`
        # ever mutates `base` -- so it is still the pristine, non-array
        # metadata (fact_graph_complete, ok, degradation, cached,
        # compile_ms, repository_alias, ...) at this point. Attach a COPY
        # so the caller (one of the three thin wrapper functions) can
        # merge it into its {"success": False, "error": ...} response
        # instead of discarding everything the analysis already produced.
        exc.base_metadata = dict(base)  # type: ignore[attr-defined]
        raise
