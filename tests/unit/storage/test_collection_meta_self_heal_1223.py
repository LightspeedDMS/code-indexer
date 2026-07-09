"""Tests for Bug #1223 — corrupt/0-byte collection_meta.json self-heal.

Two defects fixed:
  Defect A: non-atomic write creates 0-byte collection_meta.json on crash.
  Defect B: collection_exists() presence-only check never self-heals.

All tests use real FilesystemVectorStore on tmp_path — no filesystem mocking.
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(base_path: Path):
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    return FilesystemVectorStore(base_path=base_path)


def _meta_path(base_path: Path, collection_name: str) -> Path:
    return base_path / collection_name / "collection_meta.json"


def _make_mock_provider(dimensions: int, model_name: str) -> Mock:
    provider = Mock()
    provider.get_model_info.return_value = {"dimensions": dimensions}
    provider.get_current_model.return_value = model_name
    provider.get_provider_name.return_value = "voyage-ai"
    return provider


def _make_mock_config() -> Mock:
    config = Mock()
    config.embedding_provider = "voyage-ai"
    config.embedding_model = "voyage-code-3"
    return config


# ---------------------------------------------------------------------------
# Defect B: collection_exists() must validate JSON, not just .exists()
# ---------------------------------------------------------------------------


class TestCollectionExistsValidation:
    """collection_exists() must return False for empty/corrupt meta files."""

    def test_returns_false_for_zero_byte_meta(self, tmp_path):
        """GIVEN a collection directory with a 0-byte collection_meta.json
        WHEN collection_exists() is called
        THEN returns False (not True as with the old presence-only check).
        """
        store = _make_store(tmp_path)
        (tmp_path / "my_coll").mkdir()
        meta = _meta_path(tmp_path, "my_coll")
        meta.touch()  # 0 bytes
        assert meta.stat().st_size == 0

        assert store.collection_exists("my_coll") is False

    def test_returns_false_for_non_json_meta(self, tmp_path):
        """GIVEN a collection directory with garbage (non-JSON) meta
        WHEN collection_exists() is called
        THEN returns False.
        """
        store = _make_store(tmp_path)
        (tmp_path / "bad_coll").mkdir()
        _meta_path(tmp_path, "bad_coll").write_text("NOT JSON {{{")

        assert store.collection_exists("bad_coll") is False

    def test_returns_false_for_json_without_vector_size(self, tmp_path):
        """GIVEN a meta file that is valid JSON but missing 'vector_size'
        WHEN collection_exists() is called
        THEN returns False (incomplete meta treated as absent).
        """
        store = _make_store(tmp_path)
        (tmp_path / "incomplete_coll").mkdir()
        _meta_path(tmp_path, "incomplete_coll").write_text(
            json.dumps({"name": "incomplete_coll"})  # no vector_size
        )

        assert store.collection_exists("incomplete_coll") is False

    def test_returns_true_for_valid_meta(self, tmp_path):
        """GIVEN a properly created collection
        WHEN collection_exists() is called
        THEN returns True (regression: valid collections still found).
        """
        store = _make_store(tmp_path)
        store.create_collection("valid_coll", vector_size=1024)

        assert store.collection_exists("valid_coll") is True

    def test_returns_false_when_no_directory(self, tmp_path):
        """GIVEN no collection at all
        WHEN collection_exists() is called
        THEN returns False.
        """
        assert _make_store(tmp_path).collection_exists("nonexistent") is False


# ---------------------------------------------------------------------------
# Self-heal: index path recreates collection when meta is corrupt
# ---------------------------------------------------------------------------


class TestSelfHealOnCorruptMeta:
    """After corrupt meta, ensure_provider_aware_collection (index path)
    recreates a valid collection_meta.json."""

    def test_zero_byte_meta_self_heals(self, tmp_path):
        """GIVEN an existing collection whose meta was truncated to 0 bytes
        WHEN ensure_provider_aware_collection() is called (index path)
        THEN a valid collection_meta.json is written and collection_exists() is True.
        """
        store = _make_store(tmp_path)
        store.create_collection("voyage-code-3", vector_size=1024)

        # Simulate crash: truncate meta to 0 bytes
        meta = _meta_path(tmp_path, "voyage-code-3")
        meta.write_text("")
        assert store.collection_exists("voyage-code-3") is False

        store.ensure_provider_aware_collection(
            _make_mock_config(), _make_mock_provider(1024, "voyage-code-3")
        )

        parsed = json.loads(meta.read_text())
        assert parsed["vector_size"] == 1024
        assert store.collection_exists("voyage-code-3") is True

    def test_corrupt_json_meta_self_heals(self, tmp_path):
        """GIVEN an existing collection whose meta is corrupt JSON
        WHEN ensure_provider_aware_collection() is called (index path)
        THEN a valid collection_meta.json is written.
        """
        store = _make_store(tmp_path)
        store.create_collection("voyage-code-3", vector_size=1024)

        _meta_path(tmp_path, "voyage-code-3").write_text("}{not json}{")
        assert store.collection_exists("voyage-code-3") is False

        store.ensure_provider_aware_collection(
            _make_mock_config(), _make_mock_provider(1024, "voyage-code-3")
        )

        parsed = json.loads(_meta_path(tmp_path, "voyage-code-3").read_text())
        assert parsed["vector_size"] == 1024


# ---------------------------------------------------------------------------
# Defect A: atomic write — no 0-byte window on crash
# ---------------------------------------------------------------------------


class TestAtomicMetaWrite:
    """create_collection() must write collection_meta.json atomically."""

    def test_new_collection_crash_leaves_no_zero_byte_file(self, tmp_path):
        """GIVEN a new collection being created
        WHEN json.dump raises mid-write (simulating crash)
        THEN collection_meta.json is either absent or still valid — never 0 bytes.
        """
        store = _make_store(tmp_path)

        with patch("json.dump", side_effect=RuntimeError("simulated crash")):
            with pytest.raises(RuntimeError, match="simulated crash"):
                store.create_collection("crash_coll", vector_size=1024)

        meta = _meta_path(tmp_path, "crash_coll")
        if meta.exists():
            content = meta.read_text()
            assert len(content) > 0, "Meta file must not be 0 bytes after failed write"
            parsed = json.loads(content)
            assert "vector_size" in parsed

        # No leftover .tmp files
        coll_dir = tmp_path / "crash_coll"
        if coll_dir.exists():
            assert list(coll_dir.glob("*.tmp")) == []

    def test_existing_collection_meta_preserved_on_write_crash(self, tmp_path):
        """GIVEN a collection with a valid collection_meta.json on disk
        WHEN a subsequent create_collection() call crashes mid-write
        THEN the original valid file is still intact — never truncated to 0 bytes.
        """
        store = _make_store(tmp_path)
        store.create_collection("existing_coll", vector_size=1024)
        meta = _meta_path(tmp_path, "existing_coll")
        original_content = meta.read_text()
        assert len(original_content) > 0

        with patch("json.dump", side_effect=RuntimeError("simulated crash")):
            with pytest.raises(RuntimeError, match="simulated crash"):
                store.create_collection("existing_coll", vector_size=1024)

        current_content = meta.read_text()
        assert len(current_content) > 0, "Meta file was truncated to 0 bytes!"
        parsed = json.loads(current_content)
        assert "vector_size" in parsed

        # No leftover .tmp files
        assert list((tmp_path / "existing_coll").glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# Query path: corrupt meta still raises (read-path safety preserved)
# ---------------------------------------------------------------------------


class TestQueryPathRaisesOnCorruptMeta:
    """_get_vector_size() (query path) must still raise RuntimeError on corrupt meta.
    We weakened the INDEX-TIME path only."""

    def test_get_vector_size_raises_on_zero_byte_meta(self, tmp_path):
        """GIVEN a collection directory with a 0-byte collection_meta.json
        WHEN _get_vector_size() is called (query path)
        THEN RuntimeError is raised.
        """
        store = _make_store(tmp_path)
        (tmp_path / "corrupt_coll").mkdir()
        _meta_path(tmp_path, "corrupt_coll").touch()  # 0 bytes

        with pytest.raises(RuntimeError):
            store._get_vector_size("corrupt_coll")

    def test_get_vector_size_raises_on_invalid_json_meta(self, tmp_path):
        """GIVEN a collection directory with non-JSON meta
        WHEN _get_vector_size() is called
        THEN RuntimeError is raised.
        """
        store = _make_store(tmp_path)
        (tmp_path / "bad_json_coll").mkdir()
        _meta_path(tmp_path, "bad_json_coll").write_text("not json at all")

        with pytest.raises(RuntimeError):
            store._get_vector_size("bad_json_coll")


# ---------------------------------------------------------------------------
# Regression: normal round-trip still works
# ---------------------------------------------------------------------------


class TestNormalRoundTripRegression:
    """Valid collections must be completely unaffected by the fixes."""

    def test_create_and_read_collection_round_trip(self, tmp_path):
        """GIVEN a normal create_collection() call
        WHEN collection_exists() and _get_vector_size() are called
        THEN both work correctly.
        """
        store = _make_store(tmp_path)
        assert store.create_collection("roundtrip_coll", vector_size=1536) is True
        assert store.collection_exists("roundtrip_coll") is True
        assert store._get_vector_size("roundtrip_coll") == 1536

    def test_create_collection_meta_is_valid_json(self, tmp_path):
        """GIVEN a normal create_collection()
        WHEN the written collection_meta.json is read directly
        THEN it is valid JSON with the expected fields.
        """
        store = _make_store(tmp_path)
        store.create_collection("meta_check_coll", vector_size=1024)
        parsed = json.loads(_meta_path(tmp_path, "meta_check_coll").read_text())
        assert parsed["vector_size"] == 1024
        assert parsed["name"] == "meta_check_coll"
