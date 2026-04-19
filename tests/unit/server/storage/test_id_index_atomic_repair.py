"""Tests for id_index.bin corruption detection, atomic write, and auto-repair.

Bug #861: id_index.bin corruption on interrupted write — golden repo refresh
fails until manual --reconcile.
"""

import json
import struct
from unittest.mock import Mock, patch

import numpy as np
import pytest

from code_indexer.storage.id_index_manager import CorruptIDIndexError, IDIndexManager


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def collection_dir(tmp_path):
    """Temp directory serving as the collection path for IDIndexManager tests."""
    return tmp_path


@pytest.fixture
def manager():
    """Fresh IDIndexManager instance."""
    return IDIndexManager()


@pytest.fixture
def vector_store(tmp_path):
    """FilesystemVectorStore rooted in tmp_path."""
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    return FilesystemVectorStore(base_path=tmp_path / "index", project_root=tmp_path)


@pytest.fixture
def store_with_corrupt_index(vector_store):
    """Collection directory with a zero-byte (corrupt) id_index.bin."""
    collection_name = "repair_collection"
    collection_path = vector_store.base_path / collection_name
    collection_path.mkdir(parents=True, exist_ok=True)
    (collection_path / "id_index.bin").write_bytes(b"")
    return vector_store, collection_name, collection_path


@pytest.fixture
def indexed_store_with_corrupt_index(tmp_path):
    """Real indexed collection whose id_index.bin has been corrupted post-indexing.

    The HNSW index and vector JSON files remain intact on disk so that
    rebuild_from_vectors() can reconstruct the id_index from real data and
    search() can complete normally after repair.
    """
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    store = FilesystemVectorStore(base_path=tmp_path / "index", project_root=tmp_path)
    collection_name = "search_repair_collection"

    store.create_collection(collection_name, vector_size=8)

    np.random.seed(0)
    points = [
        {
            "id": f"pt_{i}",
            "vector": np.random.randn(8).tolist(),
            "payload": {"path": f"file_{i}.py", "content": f"def f_{i}(): pass"},
        }
        for i in range(5)
    ]
    store.begin_indexing(collection_name)
    store.upsert_points(collection_name, points)
    store.end_indexing(collection_name)

    # Corrupt id_index.bin — vector JSON files and HNSW index remain intact
    collection_path = store.base_path / collection_name
    (collection_path / "id_index.bin").write_bytes(b"")

    # Clear the in-memory cache so the corrupted file will be read on next access
    store._id_index.pop(collection_name, None)

    mock_provider = Mock()
    mock_provider.get_embedding.return_value = np.random.randn(8).tolist()

    return store, collection_name, mock_provider


# ---------------------------------------------------------------------------
# Tests: CorruptIDIndexError raised for corrupt/truncated files
# ---------------------------------------------------------------------------


class TestCorruptIDIndexErrorRaised:
    """CorruptIDIndexError must be raised for all forms of file corruption."""

    def test_zero_byte_raises_corrupt_error(self, collection_dir, manager):
        """Zero-byte id_index.bin must raise CorruptIDIndexError."""
        (collection_dir / "id_index.bin").write_bytes(b"")

        with pytest.raises(CorruptIDIndexError):
            manager.load_index(collection_dir)

    @pytest.mark.parametrize("size", [1, 2, 3])
    def test_one_two_three_byte_raises_corrupt_error(
        self, collection_dir, manager, size
    ):
        """Files of 1, 2, or 3 bytes cannot hold the 4-byte entry-count header."""
        (collection_dir / "id_index.bin").write_bytes(b"\x01" * size)

        with pytest.raises(CorruptIDIndexError):
            manager.load_index(collection_dir)

    def test_unreasonable_entry_count_raises_corrupt_error(
        self, collection_dir, manager
    ):
        """Entry count > 10M is unreasonable and must raise CorruptIDIndexError."""
        (collection_dir / "id_index.bin").write_bytes(struct.pack("<I", 10_000_001))

        with pytest.raises(CorruptIDIndexError):
            manager.load_index(collection_dir)

    def test_truncated_entry_raises_corrupt_error(self, collection_dir, manager):
        """EOF mid-entry (declared ID length exceeds available bytes) raises CorruptIDIndexError."""
        buf = struct.pack("<I", 1)  # 1 entry
        buf += struct.pack("<H", 20)  # ID length = 20
        buf += b"short"  # Only 5 bytes, not 20
        (collection_dir / "id_index.bin").write_bytes(buf)

        with pytest.raises(CorruptIDIndexError):
            manager.load_index(collection_dir)


# ---------------------------------------------------------------------------
# Tests: Valid files still load correctly (regression guard)
# ---------------------------------------------------------------------------


class TestValidFileLoadsCorrectly:
    """Regression: valid files must still load without errors."""

    def test_valid_file_loads_correctly(self, collection_dir, manager):
        """Valid id_index.bin with multiple entries loads all entries correctly."""
        id_index = {
            "point_alpha": collection_dir / "vectors" / "alpha.json",
            "point_beta": collection_dir / "vectors" / "beta.json",
            "point_gamma": collection_dir / "vectors" / "gamma.json",
        }
        manager.save_index(collection_dir, id_index)

        loaded = manager.load_index(collection_dir)

        assert loaded == id_index


# ---------------------------------------------------------------------------
# Tests: Atomic save_index
# ---------------------------------------------------------------------------


class TestAtomicSaveIndex:
    """save_index() must use atomic write (temp file + os.replace)."""

    def test_save_index_atomic_write_cleans_temp(self, collection_dir, manager):
        """No .bin.tmp file must remain after a successful save_index() call."""
        id_index = {"id1": collection_dir / "path1.json"}

        manager.save_index(collection_dir, id_index)

        temp_file = collection_dir / "id_index.bin.tmp"
        assert not temp_file.exists(), "Temp file must be removed after atomic write"
        assert (collection_dir / "id_index.bin").exists(), "Final file must exist"


# ---------------------------------------------------------------------------
# Tests: _load_id_index auto-repair on CorruptIDIndexError
# ---------------------------------------------------------------------------


class TestLoadIdIndexAutoRepair:
    """FilesystemVectorStore._load_id_index must auto-repair on CorruptIDIndexError."""

    def test_load_id_index_repairs_on_corrupt_error(self, store_with_corrupt_index):
        """_load_id_index catches CorruptIDIndexError and calls rebuild_from_vectors."""
        store, collection_name, collection_path = store_with_corrupt_index
        expected_map = {"rebuilt_id": collection_path / "vector_rebuilt_id.json"}

        with patch(
            "code_indexer.storage.id_index_manager.IDIndexManager.rebuild_from_vectors",
            return_value=expected_map,
        ) as mock_rebuild:
            result = store._load_id_index(collection_name)

        mock_rebuild.assert_called_once()
        assert result == expected_map

    def test_load_id_index_reraises_generic_exception(self, vector_store):
        """_load_id_index must re-raise non-CorruptIDIndexError exceptions unchanged."""
        collection_name = "plain_collection"
        collection_path = vector_store.base_path / collection_name
        collection_path.mkdir(parents=True, exist_ok=True)

        with patch(
            "code_indexer.storage.id_index_manager.IDIndexManager.load_index",
            side_effect=RuntimeError("unexpected error"),
        ):
            with pytest.raises(RuntimeError, match="unexpected error"):
                vector_store._load_id_index(collection_name)


# ---------------------------------------------------------------------------
# Tests: rebuild_from_vectors JSON shape validation
# ---------------------------------------------------------------------------


class TestRebuildFromVectorsValidatesJsonShape:
    """rebuild_from_vectors must skip bad JSON files and validate the id field."""

    def test_rebuild_from_vectors_validates_json_shape(self, collection_dir, manager):
        """Corrupt JSON and valid-JSON-but-missing-id are both excluded from result.

        A valid vector file must still appear in the rebuilt map.
        """
        # Valid vector file
        good_data = {"id": "good_id", "vector": [0.1] * 8, "payload": {}}
        (collection_dir / "vector_good_id.json").write_text(json.dumps(good_data))

        # Syntactically invalid JSON — must be skipped
        (collection_dir / "vector_bad_json.json").write_text("{{not valid json")

        # Valid JSON but missing the 'id' field — must be skipped (wrong shape)
        no_id_data = {"vector": [0.2] * 8, "payload": {}}
        (collection_dir / "vector_no_id.json").write_text(json.dumps(no_id_data))

        result = manager.rebuild_from_vectors(collection_dir)

        assert "good_id" in result, "Valid entry must appear in rebuild result"
        assert len(result) == 1, (
            "Only the valid entry should be present; "
            "corrupt JSON and missing-id entries must be skipped"
        )


# ---------------------------------------------------------------------------
# Tests: Direct-load callers route through repair path
# ---------------------------------------------------------------------------


class TestDirectLoadCallersRouteThroughRepairPath:
    """Direct id_manager.load_index() callers must route through the repair-aware helper.

    After Bug #861 is fixed the load_index() inner function inside search()
    calls self._load_id_index(collection_name) instead of
    id_manager.load_index() directly.  This means a corrupt id_index.bin no
    longer propagates an unhandled exception out of search().
    """

    def test_direct_load_callers_route_through_repair_path(
        self, indexed_store_with_corrupt_index
    ):
        """search() on a collection whose id_index.bin is corrupt must complete
        without raising any exception.

        The fixture builds a real collection (with HNSW index and vector JSON
        files on disk), then corrupts id_index.bin and clears the in-memory
        cache.  When search() is called, the inner load_index() function must
        route through _load_id_index() — which catches CorruptIDIndexError and
        calls rebuild_from_vectors() to reconstruct the map from the intact
        vector JSON files — so that search() returns normally.
        """
        store, collection_name, mock_provider = indexed_store_with_corrupt_index

        # search() must complete without raising — repair is the contract
        results = store.search(
            query="test function",
            embedding_provider=mock_provider,
            collection_name=collection_name,
            limit=3,
        )

        assert isinstance(results, list), "search() must return a list after repair"
