"""Cached, writer-free FTS search for the server query path.

Historically every server FTS query newed a ``TantivyIndexManager`` and called
``initialize_index(create_new=False)``, which -- on *every* query -- write-tests
the index directory and creates a Tantivy *writer* (holding the single writer
lock). For golden repos served from the immutable ``.versioned/`` snapshot on
NFS that meant writer-lock contention and ``[Errno 9] Bad file descriptor`` on
what is a pure read. It also never used the server-wide ``FTSIndexCache`` (Story
#526), so each query re-opened the index from disk.

:func:`cached_fts_search` opens the index for SEARCH ONLY (no writer, no writes,
via ``TantivyIndexManager.open_for_search``) and caches the resulting
``(index, schema)`` in the injected ``FTSIndexCache`` keyed by index directory,
so repeated queries reuse one open index (the 5-50x cache win). Passing
``fts_cache=None`` bypasses the cache while keeping the writer-free read path --
used by the C6 shard cache-admission gate so a pod does not populate its per-pod
cache for repos it does not own (mirrors the ``hnsw_cache=None`` gate).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional


def cached_fts_search(
    fts_index_dir: Path,
    search_kwargs: Dict[str, Any],
    fts_cache: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Run an FTS search reusing a cached, writer-free Tantivy index.

    Args:
        fts_index_dir: The repository's ``.code-indexer/tantivy_index`` directory.
        search_kwargs: Keyword arguments forwarded verbatim to
            ``TantivyIndexManager.search`` (query_text, limit, filters, ...).
        fts_cache: Server-wide ``FTSIndexCache`` to cache/reuse the open index.
            ``None`` bypasses the cache (still writer-free) -- the C6 shard
            cache-admission gate passes ``None`` for repos this node does not own.

    Returns:
        Raw FTS result dicts (as returned by ``TantivyIndexManager.search``).
    """
    from ...services.tantivy_index_manager import TantivyIndexManager

    def _loader() -> Any:
        # Opens the index with no writer and no writes; returns (index, schema).
        return TantivyIndexManager(fts_index_dir).open_for_search()

    if fts_cache is not None:
        index, schema = fts_cache.get_or_load(str(fts_index_dir), _loader)
    else:
        index, schema = _loader()

    # A fresh, lightweight manager attached to the (possibly shared, cached)
    # open index -- no I/O, no writer -- serves the search. skip_reload=True:
    # the cache already manages freshness (loader opens fresh, reload_on_access
    # refreshes on hit) and served snapshots are immutable, so search()'s own
    # per-query index.reload() would be a redundant NFS metadata re-read (a p99
    # tail source) -- unless the caller explicitly asked to reload.
    search_manager = TantivyIndexManager(fts_index_dir)
    search_manager.attach_cached_index(index, schema)
    search_kwargs.setdefault("skip_reload", True)
    return search_manager.search(**search_kwargs)
