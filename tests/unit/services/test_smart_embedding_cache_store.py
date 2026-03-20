"""
Unit tests for Story #470: Smart Embedding Cache - FilesystemVectorStore.get_existing_content_hashes.

Tests verify that:
1. Returns empty dict for a file with no stored vectors
2. Returns correct chunk_index -> {content_hash, vector, point_id} mapping
3. Skips vectors that lack content_hash (graceful degradation for legacy vectors)
4. Returns all chunks when a file has multiple stored vectors

TDD: These tests are written BEFORE implementation. They should fail initially.
"""

# mypy: ignore-errors

import hashlib

from src.code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestGetExistingContentHashes:
    """FilesystemVectorStore.get_existing_content_hashes must work correctly."""

    def test_returns_empty_dict_for_unknown_file(self, tmp_path):
        """Returns empty dict when file has no stored vectors."""
        store = FilesystemVectorStore(base_path=tmp_path / "index")
        result = store.get_existing_content_hashes(
            "nonexistent/file.py", "voyage-code-3"
        )
        assert result == {}

    def test_returns_hash_data_for_stored_vector(self, tmp_path):
        """Returns chunk_index -> {content_hash, vector, point_id} for a stored vector."""
        index_path = tmp_path / "index"
        store = FilesystemVectorStore(base_path=index_path)
        collection_name = "voyage-code-3"
        store.create_collection(collection_name=collection_name, vector_size=16)

        chunk_text = "def cached_func(): pass"
        content_hash = _sha256(chunk_text)
        fake_vector = [0.1] * 16

        point = {
            "id": "point-001",
            "vector": fake_vector,
            "payload": {
                "path": "src/module.py",
                "content": chunk_text,
                "chunk_index": 0,
                "total_chunks": 1,
                "content_hash": content_hash,
                "language": "py",
                "project_id": "test",
                "file_hash": "hash123",
            },
        }
        store.upsert_points(points=[point], collection_name=collection_name)

        result = store.get_existing_content_hashes("src/module.py", collection_name)

        assert 0 in result, "chunk_index 0 must be present in result"
        assert result[0]["content_hash"] == content_hash
        assert result[0]["vector"] == fake_vector
        assert result[0]["point_id"] == "point-001"

    def test_skips_vectors_without_content_hash(self, tmp_path):
        """Graceful degradation: vectors without content_hash are excluded from result."""
        index_path = tmp_path / "index"
        store = FilesystemVectorStore(base_path=index_path)
        collection_name = "voyage-code-3"
        store.create_collection(collection_name=collection_name, vector_size=16)

        legacy_point = {
            "id": "legacy-point",
            "vector": [0.2] * 16,
            "payload": {
                "path": "src/old.py",
                "content": "old content",
                "chunk_index": 0,
                "total_chunks": 1,
                "language": "py",
                "project_id": "test",
                "file_hash": "hash456",
                # Note: NO content_hash key intentionally absent
            },
        }
        store.upsert_points(points=[legacy_point], collection_name=collection_name)

        result = store.get_existing_content_hashes("src/old.py", collection_name)

        assert (
            result == {}
        ), "Vectors without content_hash must be excluded (graceful degradation for legacy)"

    def test_returns_multiple_chunks_for_file(self, tmp_path):
        """Returns all chunks when a file has multiple stored vectors.

        All chunks for the same file must be upserted in a SINGLE call.
        The vector store treats separate per-file upserts as replacements,
        removing previous points for that file path as orphans.
        """
        index_path = tmp_path / "index"
        store = FilesystemVectorStore(base_path=index_path)
        collection_name = "voyage-code-3"
        store.create_collection(collection_name=collection_name, vector_size=16)

        file_path = "src/big_module.py"
        texts = ["chunk zero content", "chunk one content", "chunk two content"]

        # All chunks for same file must be upserted together in one call
        points = [
            {
                "id": f"point-{idx:03d}",
                "vector": [float(idx + 1) * 0.1] * 16,
                "payload": {
                    "path": file_path,
                    "content": text,
                    "chunk_index": idx,
                    "total_chunks": len(texts),
                    "content_hash": _sha256(text),
                    "language": "py",
                    "project_id": "test",
                    "file_hash": "hash789",
                },
            }
            for idx, text in enumerate(texts)
        ]
        store.upsert_points(points=points, collection_name=collection_name)

        result = store.get_existing_content_hashes(file_path, collection_name)

        assert len(result) == 3
        for idx, text in enumerate(texts):
            assert idx in result, f"chunk_index {idx} must be in result"
            assert result[idx]["content_hash"] == _sha256(text)

    def test_returns_correct_vector_values(self, tmp_path):
        """The vector stored in the result must match what was originally upserted."""
        index_path = tmp_path / "index"
        store = FilesystemVectorStore(base_path=index_path)
        collection_name = "voyage-code-3"
        store.create_collection(collection_name=collection_name, vector_size=16)

        chunk_text = "vector integrity test"
        original_vector = [float(i) * 0.05 for i in range(16)]

        point = {
            "id": "vector-check-point",
            "vector": original_vector,
            "payload": {
                "path": "src/integrity.py",
                "content": chunk_text,
                "chunk_index": 0,
                "total_chunks": 1,
                "content_hash": _sha256(chunk_text),
                "language": "py",
                "project_id": "test",
                "file_hash": "hashxyz",
            },
        }
        store.upsert_points(points=[point], collection_name=collection_name)

        result = store.get_existing_content_hashes("src/integrity.py", collection_name)

        assert 0 in result
        assert result[0]["vector"] == original_vector
