"""Real-index tests for TantivyIndexManager read-only search path (C6 FTS cache).

open_for_search() must open an existing index WITHOUT creating a writer (so
concurrent queries don't contend on the writer lock and immutable/NFS snapshots
aren't written), and search() must work off the resulting (index, schema).
attach_cached_index() must let a fresh manager serve search() from a shared
cached pair with no I/O.
"""

import pytest

pytest.importorskip("tantivy")

from code_indexer.services.tantivy_index_manager import (  # noqa: E402
    TantivyIndexManager,
)


def _build_index(index_dir):
    """Build a tiny FTS index via the normal write path."""
    writer = TantivyIndexManager(index_dir)
    writer.initialize_index(create_new=True)
    writer.add_document(
        {
            "path": "auth/login.py",
            "content": "def authenticate user password token",
            "content_raw": "def authenticate(user, password): return token",
            "identifiers": ["authenticate", "user", "password"],
            "line_start": 1,
            "line_end": 3,
            "language": "python",
        }
    )
    writer.commit()
    return writer


def test_open_for_search_creates_no_writer(tmp_path):
    index_dir = tmp_path / "tantivy_index"
    _build_index(index_dir)

    reader = TantivyIndexManager(index_dir)
    index, schema = reader.open_for_search()

    assert index is not None
    assert schema is not None
    assert reader._writer is None  # no writer lock taken on the read path
    results = reader.search(query_text="authenticate", limit=5)
    assert any("login.py" in r.get("path", "") for r in results)


def test_open_for_search_missing_index_raises(tmp_path):
    reader = TantivyIndexManager(tmp_path / "does_not_exist")
    with pytest.raises(FileNotFoundError):
        reader.open_for_search()


class _IndexSpy:
    """Delegates to a real tantivy Index but counts reload() calls.

    (tantivy's native Index.reload is read-only, so it can't be patched in
    place -- wrap it instead. search() only calls reload() + searcher().)
    """

    def __init__(self, real):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "reloads", 0)

    def reload(self):
        object.__setattr__(self, "reloads", self.reloads + 1)
        return self._real.reload()

    def __getattr__(self, name):
        # Delegate everything else (searcher, parse_query, ...) to the real index.
        return getattr(object.__getattribute__(self, "_real"), name)


def test_search_skip_reload_returns_results_without_reloading(tmp_path):
    index_dir = tmp_path / "tantivy_index"
    _build_index(index_dir)

    reader = TantivyIndexManager(index_dir)
    index, _ = reader.open_for_search()
    spy = _IndexSpy(index)
    reader._index = spy  # type: ignore[assignment]

    results = reader.search(query_text="authenticate", limit=5, skip_reload=True)
    assert any("login.py" in r.get("path", "") for r in results)
    assert spy.reloads == 0  # no reload on the cached serving path

    reader.search(query_text="authenticate", limit=5)  # default still reloads
    assert spy.reloads == 1


def test_attach_cached_index_serves_search_without_io(tmp_path):
    index_dir = tmp_path / "tantivy_index"
    _build_index(index_dir)

    # Open once (as the cache loader would) and share the pair.
    loader = TantivyIndexManager(index_dir)
    index, schema = loader.open_for_search()

    # A fresh manager attaches the shared pair -- no writer, no re-open.
    attached = TantivyIndexManager(index_dir)
    attached.attach_cached_index(index, schema)
    assert attached._writer is None

    results = attached.search(query_text="authenticate", limit=5)
    assert any("login.py" in r.get("path", "") for r in results)
