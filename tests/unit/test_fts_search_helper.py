"""Unit tests for the cached, writer-free FTS search helper (C6 FTS cache).

Verifies that cached_fts_search:
- opens the index for SEARCH ONLY (open_for_search), never initialize_index
  (which would create a Tantivy writer and hold the writer lock);
- reuses the FTSIndexCache when one is provided (keyed by index dir), and
  bypasses it (still writer-free) when fts_cache is None -- the C6 shard
  cache-admission gate.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.server.cache.fts_search import cached_fts_search

_MGR = "code_indexer.services.tantivy_index_manager.TantivyIndexManager"


def test_bypass_cache_opens_readonly_and_searches():
    """fts_cache=None: open_for_search + search, no cache, no writer."""
    loader_mgr = MagicMock()
    loader_mgr.open_for_search.return_value = ("IDX", "SCHEMA")
    search_mgr = MagicMock()
    search_mgr.search.return_value = [{"path": "a.py"}]
    factory = MagicMock(side_effect=[loader_mgr, search_mgr])

    with patch(_MGR, factory):
        out = cached_fts_search(Path("/x/idx"), {"query_text": "q", "limit": 5})

    assert out == [{"path": "a.py"}]
    loader_mgr.open_for_search.assert_called_once()
    loader_mgr.initialize_index.assert_not_called()  # never creates a writer
    search_mgr.attach_cached_index.assert_called_once_with("IDX", "SCHEMA")
    # skip_reload=True: no redundant per-query index.reload() on the cached path
    search_mgr.search.assert_called_once_with(query_text="q", limit=5, skip_reload=True)


def test_uses_cache_when_provided():
    """fts_cache: (index, schema) come from get_or_load, keyed by index dir."""
    fts_cache = MagicMock()
    fts_cache.get_or_load.return_value = ("CIDX", "CSCHEMA")
    search_mgr = MagicMock()
    search_mgr.search.return_value = []
    # get_or_load returns cached pair without invoking the loader -> only the
    # search manager is constructed.
    factory = MagicMock(return_value=search_mgr)

    with patch(_MGR, factory):
        cached_fts_search(Path("/x/idx"), {"query_text": "q"}, fts_cache=fts_cache)

    key, loader = fts_cache.get_or_load.call_args[0]
    assert key == "/x/idx"  # cache keyed by index dir string
    assert callable(loader)
    search_mgr.attach_cached_index.assert_called_once_with("CIDX", "CSCHEMA")


def test_cache_loader_opens_readonly_never_writer():
    """The loader handed to the cache opens read-only, never initialize_index."""
    loader_mgr = MagicMock()
    loader_mgr.open_for_search.return_value = ("IDX", "SCHEMA")
    search_mgr = MagicMock()
    search_mgr.search.return_value = []
    factory = MagicMock(side_effect=[loader_mgr, search_mgr])

    fts_cache = MagicMock()
    # Emulate a cache MISS: invoke the loader, like FTSIndexCache does.
    fts_cache.get_or_load.side_effect = lambda key, loader: loader()

    with patch(_MGR, factory):
        cached_fts_search(Path("/x/idx"), {"query_text": "q"}, fts_cache=fts_cache)

    loader_mgr.open_for_search.assert_called_once()
    loader_mgr.initialize_index.assert_not_called()
    search_mgr.attach_cached_index.assert_called_once_with("IDX", "SCHEMA")
