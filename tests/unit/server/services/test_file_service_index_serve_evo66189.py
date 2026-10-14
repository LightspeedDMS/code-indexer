"""EVO-66189: list_files/browse can be served from the index's path_index.bin
(indexed_only fast path) instead of stat-walking the working tree over NFS."""

import msgpack
import pytest

from code_indexer.server.services.file_service import FileListingService


def _svc():
    s = FileListingService.__new__(FileListingService)
    s.activated_repo_manager = None  # type: ignore[assignment]
    return s


def _write_path_index(repo, collection, paths):
    d = repo / ".code-indexer" / "index" / collection
    d.mkdir(parents=True)
    with open(d / "path_index.bin", "wb") as f:
        msgpack.dump({p: ["pt"] for p in paths}, f)


class TestIndexServe:
    def test_collect_from_index(self, tmp_path):
        _write_path_index(tmp_path, "voyage-code-3", ["a.py", "src/b.ts", "README.md"])
        files = _svc()._collect_files_from_index(str(tmp_path))
        assert files is not None
        assert sorted(f.path for f in files) == ["README.md", "a.py", "src/b.ts"]
        assert all(f.is_indexed for f in files)
        assert {f.language for f in files if f.path == "a.py"} == {"python"}

    def test_none_when_no_index(self, tmp_path):
        assert _svc()._collect_files_from_index(str(tmp_path)) is None

    def test_unions_and_dedups_collections(self, tmp_path):
        _write_path_index(tmp_path, "voyage-code-3", ["a.py"])
        _write_path_index(tmp_path, "voyage-multimodal-3", ["a.py", "img.png"])
        files = _svc()._collect_files_from_index(str(tmp_path))
        assert sorted(f.path for f in files) == ["a.py", "img.png"]  # a.py deduped

    def test_indexed_only_serves_from_index_not_the_tree(self, tmp_path):
        _write_path_index(tmp_path, "voyage-code-3", ["indexed.py"])
        # a real file that is NOT in the index — indexed_only must NOT return it.
        (tmp_path / "walk_only.py").write_text("x = 1")
        s = _svc()
        idx = sorted(
            f.path for f in s._collect_listing(str(tmp_path), indexed_only=True)
        )
        assert idx == ["indexed.py"]
        walk = sorted(
            f.path for f in s._collect_listing(str(tmp_path), indexed_only=False)
        )
        assert "walk_only.py" in walk

    def test_includes_legacy_multimodal_index_layout(self, tmp_path):
        """Repos not yet re-indexed keep a legacy sibling layout at
        .code-indexer/multimodal_index/<collection>/path_index.bin — it must be included."""
        _write_path_index(tmp_path, "voyage-code-3", ["a.py"])
        legacy = tmp_path / ".code-indexer" / "multimodal_index" / "voyage-multimodal-3"
        legacy.mkdir(parents=True)
        with open(legacy / "path_index.bin", "wb") as f:
            msgpack.dump({"legacy_img.png": ["pt"]}, f)
        files = _svc()._collect_files_from_index(str(tmp_path))
        assert sorted(f.path for f in files) == ["a.py", "legacy_img.png"]

    def test_indexed_only_falls_back_to_walk_when_no_index(self, tmp_path):
        (tmp_path / "only.py").write_text("x = 1")
        # No index present → indexed_only gracefully falls back to the walk.
        got = [
            f.path for f in _svc()._collect_listing(str(tmp_path), indexed_only=True)
        ]
        assert got == ["only.py"]

    def test_corrupt_index_falls_back(self, tmp_path):
        d = tmp_path / ".code-indexer" / "index" / "voyage-code-3"
        d.mkdir(parents=True)
        (d / "path_index.bin").write_bytes(b"\xff\xff not msgpack")
        assert _svc()._collect_files_from_index(str(tmp_path)) is None


def test_path_index_load_is_lenient_unless_strict(tmp_path):
    from code_indexer.storage.filesystem_vector_store import PathIndex

    bad = tmp_path / "path_index.bin"
    bad.write_bytes(b"\xff\xff not msgpack")
    assert PathIndex.load(bad).file_paths() == []  # indexing callers: unchanged
    with pytest.raises(ValueError, match="Corrupt"):
        PathIndex.load(bad, strict=True)
