"""Cache storage (atomic page-set write) and fetch-side (manifest
resolution) mechanics for xray_truncation.py (Bug #1928).

Split out of xray_truncation.py to keep that module under the project's
file-size limit (Rule #6, Anti-File-Bloat) -- imported and re-exported by
xray_truncation.py, so all existing callers/tests keep using
`xray_truncation.fetch_cached_page`, `xray_truncation.PageSetStoreError`,
`xray_truncation.MalformedManifestError`, and
`xray_truncation._PAGES_V1_HANDLE_PREFIX` unchanged.

Bug #1928 round 3 (Codex REJECT on a real P1, plus a P2 follow-up):

- P1: pages and the pages-v1 manifest are written as ONE atomic
  page-set batch (`PayloadCache.store_batch_with_keys()`), sharing a
  single timestamp/expiry, with any write failure PROPAGATING as
  `PageSetStoreError` -- never a cache_handle pointing at data that was
  never durably written.
- P2: a pages-v1 manifest handle is identified by a dedicated PREFIX
  (`_PAGES_V1_HANDLE_PREFIX`) -- never by sniffing whether its content
  happens to parse as manifest-shaped JSON. A handle carrying the
  prefix is strictly validated; malformed content raises
  `MalformedManifestError` loudly instead of silently falling back.
  `page` must be a real `int >= 1` -- fetch_cached_page raises
  `ValueError` rather than clamping a bad value to 1.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Tuple

from code_indexer.server.cache.payload_cache import CacheNotFoundError

_PAGES_V1_FORMAT = "pages-v1"

# Bug #1928 P2: dedicated discriminator namespace for pages-v1 manifest
# handles -- a cheap `str.startswith()` check decides whether a handle
# should even be treated as a manifest, BEFORE any retrieve_full()/
# json.loads() is attempted. Ordinary page handles and every OTHER cache
# consumer's handles (plain uuid4 strings, or other prefixes entirely)
# never carry this prefix.
_PAGES_V1_HANDLE_PREFIX = "xray-pv1-"


class PageSetStoreError(Exception):
    """Bug #1928 P1: raised when the atomic page-set write (whole-entry
    pages + the pages-v1 manifest, ONE batch) fails. The caller MUST
    surface this as an explicit error -- never fall back to returning a
    cache_handle for data that was not durably written.

    Bug #1928 final round (Opus P4.6): xray_truncation.py's
    truncate_result_fields() attaches a `base_metadata: Dict[str, Any]`
    attribute (the pristine, non-array-field metadata of the original
    result -- e.g. fact_graph_complete, ok, degradation, cached,
    compile_ms, repository_alias) before re-raising, so the three thin
    wrapper functions (xray.py/xray_graph.py/xray_batch.py) can merge it
    into their {"success": False, "error": "cache_store_failed", ...}
    response instead of discarding everything the analysis/search already
    produced. Optional -- code reading it must use
    `getattr(exc, "base_metadata", {})`."""


class MalformedManifestError(Exception):
    """Bug #1928 P2: raised when a handle carries the pages-v1
    discriminator prefix but its stored content fails strict validation
    (not valid JSON, not an object, wrong format, or page_handles/
    total_pages inconsistent). A loud, structured error -- never a
    silent fallback to legacy retrieval, which would return the raw
    manifest JSON as if it were ordinary content."""


def store_pages(
    payload_cache: Any, field_names: List[str], pages: List[Dict[str, list]]
) -> Tuple[str, int]:
    """Store every page AND the pages-v1 manifest that references them as
    ONE atomic batch (Bug #1928 P1) -- pre-generates every handle
    (including the manifest's own, prefix-discriminated one) so the
    manifest content can be built BEFORE any write happens, then submits
    pages+manifest together via `PayloadCache.store_batch_with_keys()`
    (one transaction, one shared timestamp/expiry, failures propagate).
    Returns (manifest_cache_handle, total_pages).

    Raises:
        ValueError: pages is empty -- a manifest with total_pages=0 is
            itself malformed (_parse_and_validate_manifest rejects it),
            so this must never be written in the first place.
        PageSetStoreError: the atomic write failed -- no page or the
            manifest was durably stored; the caller must surface this as
            an explicit error, never a handle.
    """
    if not pages:
        raise ValueError(
            "store_pages() requires at least one page -- an empty page "
            "set would produce a total_pages=0 manifest, which is itself "
            "malformed (see _parse_and_validate_manifest)"
        )

    page_contents = [
        json.dumps({f: page.get(f, []) for f in field_names}) for page in pages
    ]
    page_handles = [str(uuid.uuid4()) for _ in page_contents]
    manifest_handle = f"{_PAGES_V1_HANDLE_PREFIX}{uuid.uuid4()}"
    manifest = {
        "format": _PAGES_V1_FORMAT,
        "total_pages": len(page_handles),
        "page_handles": page_handles,
    }
    items = list(zip(page_handles, page_contents))
    items.append((manifest_handle, json.dumps(manifest)))

    try:
        payload_cache.store_batch_with_keys(items)
    except Exception as exc:
        raise PageSetStoreError(
            f"failed to durably store {len(page_handles)} page(s) + the "
            f"pages-v1 manifest as one atomic batch: {exc}"
        ) from exc

    return manifest_handle, len(page_handles)


def build_fetch_tool_hint(
    field_names: List[str], cache_handle: str, total_pages: int
) -> str:
    fields_desc = ", ".join(f'"{f}": [...]' for f in field_names)
    return (
        f"Result truncated; the full result ({total_pages} page(s)) is "
        f"available at cache_handle '{cache_handle}' -- fetch via the "
        f"`cidx_fetch_cached_payload` MCP tool with that handle, "
        f"page=1..{total_pages}. Each page is an independently parseable "
        f"JSON object {{{fields_desc}}} holding a whole-entry slice; "
        f"concatenate every page's fields, in page order, to reconstruct "
        f"the full arrays."
    )


def _parse_and_validate_manifest(content: str, cache_handle: str) -> Dict[str, Any]:
    """Strictly parse+validate a pages-v1 manifest's content (Bug #1928
    P2). Called ONLY for a handle that already carries the
    `_PAGES_V1_HANDLE_PREFIX` discriminator -- at that point the content
    is EXPECTED to be a valid manifest, so any failure here is a loud,
    structured error (MalformedManifestError), never a silent fallback.

    Raises:
        MalformedManifestError: content is not valid JSON, not an
            object, has the wrong `format`, `page_handles` is not a
            non-empty list[str], or `total_pages` is not a positive int
            equal to len(page_handles). A generated page-set always
            contains at least one page, so total_pages == 0 (with an
            empty page_handles) is itself malformed, not a valid
            degenerate case.
    """
    try:
        data = json.loads(content)
    except (ValueError, TypeError) as exc:
        raise MalformedManifestError(
            f"cache_handle {cache_handle!r} carries the pages-v1 prefix "
            f"but its content is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise MalformedManifestError(
            f"cache_handle {cache_handle!r}: manifest content must be a "
            f"JSON object, got {type(data).__name__}"
        )
    if data.get("format") != _PAGES_V1_FORMAT:
        raise MalformedManifestError(
            f"cache_handle {cache_handle!r}: manifest 'format' must be "
            f"{_PAGES_V1_FORMAT!r}, got {data.get('format')!r}"
        )

    page_handles = data.get("page_handles")
    if (
        not isinstance(page_handles, list)
        or not page_handles
        or not all(isinstance(h, str) for h in page_handles)
    ):
        raise MalformedManifestError(
            f"cache_handle {cache_handle!r}: manifest 'page_handles' must "
            f"be a non-empty list[str]"
        )

    total_pages = data.get("total_pages")
    if (
        not isinstance(total_pages, int)
        or isinstance(total_pages, bool)
        or total_pages < 1
    ):
        raise MalformedManifestError(
            f"cache_handle {cache_handle!r}: manifest 'total_pages' must "
            f"be an int >= 1, got {total_pages!r}"
        )
    if total_pages != len(page_handles):
        raise MalformedManifestError(
            f"cache_handle {cache_handle!r}: manifest 'total_pages' "
            f"({total_pages}) must equal len(page_handles) "
            f"({len(page_handles)})"
        )

    return data


def fetch_cached_page(
    payload_cache: Any, cache_handle: str, page: int
) -> Dict[str, Any]:
    """Resolve a cache_handle + 1-indexed page into
    {content, page, total_pages, has_more}.

    Bug #1928 P2: whether a handle is a pages-v1 manifest is decided by
    a cheap prefix check (`_PAGES_V1_HANDLE_PREFIX`) BEFORE any
    `retrieve_full()`/`json.loads()` is attempted -- a legacy handle
    (or any other cache consumer's handle entirely) never reaches
    manifest parsing at all. A handle that DOES carry the prefix is
    strictly validated; malformed content raises MalformedManifestError
    (never a silent fallback). Falls back to the legacy, windowed
    PayloadCache.retrieve() for any handle that does NOT carry the
    prefix.

    `page` must be a real int >= 1 -- no clamping/coercion (Bug #1928
    P2); callers (the MCP front doors) must validate/reject a raw,
    possibly non-int `page` parameter BEFORE calling this. `bool` is
    explicitly rejected too (it is an `int` subclass in Python).
    `cache_handle` must be a non-empty string.

    Raises:
        ValueError: page is not a real int (or is < 1), or cache_handle
            is not a non-empty string.
        MalformedManifestError: cache_handle carries the pages-v1 prefix
            but its content fails strict validation.
        CacheNotFoundError: handle not found/expired, or page out of
            range.
    """
    if not isinstance(cache_handle, str) or not cache_handle:
        raise ValueError(
            f"cache_handle must be a non-empty string, got {cache_handle!r}"
        )
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError(f"page must be an integer >= 1, got {page!r}")

    if cache_handle.startswith(_PAGES_V1_HANDLE_PREFIX):
        full_content = payload_cache.retrieve_full(cache_handle)
        manifest = _parse_and_validate_manifest(full_content, cache_handle)
        page_handles = manifest["page_handles"]
        total_pages = manifest["total_pages"]
        if not (1 <= page <= total_pages):
            raise CacheNotFoundError(
                f"Page {page} out of range for handle {cache_handle} "
                f"(total: {total_pages})"
            )
        page_content = payload_cache.retrieve_full(page_handles[page - 1])
        return {
            "content": page_content,
            "page": page,
            "total_pages": total_pages,
            "has_more": page < total_pages,
        }

    legacy = payload_cache.retrieve(cache_handle, page=page - 1)
    return {
        "content": legacy.content,
        "page": legacy.page + 1,
        "total_pages": legacy.total_pages,
        "has_more": legacy.has_more,
    }
