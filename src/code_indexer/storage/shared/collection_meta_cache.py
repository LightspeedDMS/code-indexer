"""mtime-keyed TTL cache of parsed ``collection_meta.json`` (Story #1492 AC1).

Finding C1 (SEVERE, report rank 2, "highest ROI fix in the audit"):
``FilesystemVectorStore.search()`` used to read+``json.loads`` its
collection's ``collection_meta.json`` 4-5 SEPARATE times per single search
call (``collection_exists()``, the ``vector_size`` read, ``HNSWIndexManager
.is_stale()``, and up to two ``resolve_chunk_layout()`` calls) -- on the
largest measured on-disk file (56.3 MB / 399,643 ``id_mapping`` entries)
that is ~1.4s of GIL-held time per query, purely from re-parsing metadata
that has not changed.

``CollectionMetaCache`` is built on the EXISTING ``TTLCache`` primitive
(``code_indexer.server.services.query_path_cache``, Story #1082) -- no new
caching mechanism is introduced. It reuses Story #1082's drift-safety
split exactly the way ``RepoConfigCache`` does: a path proven immutable via
``is_immutable_versioned_snapshot`` gets a NO-TTL bounded-LRU cache (a
golden-repo refresh/consolidation always produces either a new versioned
path or bumps this file's own mtime -- never an in-place mutation whose
content changes while the mtime stays fixed); every other (mutable) path
gets a SHORT-TTL bounded cache so a missed invalidation still self-heals.

Cache key is ``(str(collection_dir), mtime_ns)`` -- an actual ``os.stat()``
(cheap, microseconds) is performed on EVERY ``.get()`` call to read the
CURRENT mtime, so a real on-disk content change (which always changes the
file's mtime, since every writer in this codebase uses the atomic
temp-file + ``os.replace`` pattern) is a structural cache MISS on the very
next call, regardless of TTL. This is what makes the cache genuinely
drift-safe rather than merely TTL-bounded-stale: the file's own identity
(mtime) is part of the key, not just a coarse periodic re-check.

Path handling: ``collection_dir`` is normalized via ``Path(...).resolve()``
before use, collapsing any ``..``/symlink indirection into a canonical
absolute form -- this is an internal cache-key-stability normalization,
not a user-input-facing access-control boundary. Every real call site
(``FilesystemVectorStore._get_collection_path()`` and friends) already
constructs ``collection_dir`` from a server/CLI-internal, already-resolved
project/base path; this module never receives raw, unvalidated user input
as a path, so there is no separate "allowed base directory" for it to
confine against without duplicating that call site's own resolution logic.
``Path.resolve()`` defaults to ``strict=False`` and does not itself raise
for a nonexistent path, but the call is defensively wrapped anyway (any
unexpected ``OSError``/``TypeError``/``ValueError`` -- e.g. ``None`` or an
unreadable parent directory during resolution -- fails closed to
``None``, exactly like a missing ``collection_meta.json``).

Fail-closed contract (mirrors ``resolve_chunk_layout()``): a missing file,
empty file, invalid JSON, non-UTF8 content, or a non-dict top-level JSON
value all resolve to ``None`` -- never raises, never guesses. A missing
file is NEVER itself cached (no key can be constructed without a
successful ``os.stat()``), so its later appearance is observed
immediately on the next ``.get()`` call, with no need to wait out a TTL.

This module intentionally does NOT thread ``chunk_layout_token``/
``activation_id`` discriminators the way Story #1458 AC11's
``_activation_scoped_cache_key`` does for the HNSW/id_index caches --
THIS cache's value IS the full parsed ``collection_meta.json`` (the
authoritative source those discriminators are themselves derived FROM), so
consolidation/reactivation are naturally observed via the mtime change
alone: a fresh consolidation write to ``collection_meta.json`` bumps its
mtime, giving a fresh key and a fresh parse with the updated
``chunks_db``/``hnsw_index`` content immediately.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from code_indexer.server.services.query_path_cache import (
    TTLCache,
    is_immutable_versioned_snapshot,
)

_COLLECTION_META_FILENAME = "collection_meta.json"

# Story #1082 drift-safety: short TTL for mutable paths. Matches the
# established repo_config_cache_ttl_seconds default (30s,
# server/utils/config_manager.py) -- the SAME conservative bound already
# accepted for mutable/DB-metadata paths elsewhere in this codebase. Since
# the cache key already includes the file's mtime, this TTL only bounds
# how long a genuinely UNCHANGED file's parsed content is reused; any real
# content change is observed immediately regardless of this value.
_MUTABLE_TTL_SECONDS = 30.0
_MAX_ENTRIES = 512

# (collection_dir as str, mtime_ns) -- mtime_ns is always a real,
# just-stat'd value; see CollectionMetaCache.get().
_MetaCacheKey = Tuple[str, int]


def _read_collection_meta(key: _MetaCacheKey) -> Optional[Dict[str, Any]]:
    """Loader: read+parse collection_meta.json. Fail-closed to None.

    The ``mtime_ns`` component of ``key`` is used only for cache-key
    identity (see module docstring) -- the read itself always re-reads the
    CURRENT file content at the given path.
    """
    collection_dir_str, _mtime_ns = key
    meta_path = Path(collection_dir_str) / _COLLECTION_META_FILENAME
    try:
        content = meta_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not content.strip():
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _resolve_collection_dir(collection_dir: Union[str, Path]) -> Optional[str]:
    """Canonicalize ``collection_dir``, failing closed to ``None``.

    See module docstring: ``Path.resolve()`` is default ``strict=False``
    and does not itself raise for a nonexistent path, but the call is
    wrapped defensively (``OSError``/``TypeError``/``ValueError``) so any
    invalid input (``None``, an unreadable parent directory during
    resolution, etc.) fails closed rather than propagating out of a
    cache-lookup helper.
    """
    try:
        return str(Path(collection_dir).resolve())
    except (OSError, TypeError, ValueError):
        return None


class CollectionMetaCache:
    """Per-process mtime-keyed cache of parsed ``collection_meta.json``.

    Safe to construct as either a per-instance cache (CLI/solo -- fresh per
    ``FilesystemVectorStore`` construction, still eliminates the 4-5 intra-
    call redundant parses) or as a shared, injected, cross-request
    singleton (server mode -- also eliminates the parse on a REPEAT query
    against the same unchanged collection). Both usages are safe: TTLCache
    is thread-safe and single-flight (Story #1082).
    """

    def __init__(
        self,
        mutable_ttl_seconds: float = _MUTABLE_TTL_SECONDS,
        max_entries: int = _MAX_ENTRIES,
    ) -> None:
        if (
            not isinstance(mutable_ttl_seconds, (int, float))
            or isinstance(mutable_ttl_seconds, bool)
            or not math.isfinite(mutable_ttl_seconds)
            or mutable_ttl_seconds <= 0
        ):
            raise ValueError(
                f"mutable_ttl_seconds must be a finite positive number, "
                f"got {mutable_ttl_seconds!r}"
            )
        if (
            not isinstance(max_entries, int)
            or isinstance(max_entries, bool)
            or max_entries < 1
        ):
            raise ValueError(f"max_entries must be an int >= 1, got {max_entries!r}")

        self._immutable: TTLCache[_MetaCacheKey, Optional[Dict[str, Any]]] = TTLCache(
            ttl_seconds=None,
            max_entries=max_entries,
            loader=_read_collection_meta,
        )
        self._mutable: TTLCache[_MetaCacheKey, Optional[Dict[str, Any]]] = TTLCache(
            ttl_seconds=mutable_ttl_seconds,
            max_entries=max_entries,
            loader=_read_collection_meta,
        )

    def get(self, collection_dir: Union[str, Path]) -> Optional[Dict[str, Any]]:
        """Return the parsed collection_meta.json for ``collection_dir``.

        Returns ``None`` (fail-closed) if ``collection_dir`` is invalid, or
        the file is absent, empty, unreadable, or does not parse to a JSON
        object -- never raises.
        """
        # Canonicalize (collapses '..'/symlink indirection) so the cache
        # key is stable across equivalent path spellings of the SAME
        # directory -- see module docstring for why this is a key-
        # stability normalization rather than an access-control boundary.
        collection_dir_str = _resolve_collection_dir(collection_dir)
        if collection_dir_str is None:
            return None

        meta_path = Path(collection_dir_str) / _COLLECTION_META_FILENAME
        try:
            mtime_ns = os.stat(meta_path).st_mtime_ns
        except OSError:
            # Missing/unreadable RIGHT NOW -- never cached as a "real" key,
            # so a later appearance is observed on the very next call.
            return None

        key: _MetaCacheKey = (collection_dir_str, mtime_ns)
        result: Optional[Dict[str, Any]]
        if is_immutable_versioned_snapshot(collection_dir_str):
            result = self._immutable.get(key)
        else:
            result = self._mutable.get(key)
        return result

    def counters(self) -> Dict[str, Dict[str, int]]:
        return {
            "immutable": self._immutable.counters(),
            "mutable": self._mutable.counters(),
        }
