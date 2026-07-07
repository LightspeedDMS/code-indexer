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
