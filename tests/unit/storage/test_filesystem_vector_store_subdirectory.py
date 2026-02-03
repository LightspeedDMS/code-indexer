"""Unit tests for FilesystemVectorStore subdirectory support.

Tests the ability to create collections in subdirectories (e.g., multimodal_index/)
to enable separate storage for code and multimodal indexes.

Story: Multimodal search support (AC2 - Storage layer)
"""

import json
import numpy as np
import pytest
from unittest.mock import Mock


class TestFilesystemVectorStoreSubdirectory:
    """Test FilesystemVectorStore subdirectory parameter support."""

    @pytest.fixture
    def test_vectors(self):
        """Generate deterministic test vectors."""
        np.random.seed(42)
        return np.random.randn(10, 1024)  # voyage-3 dimensions

    def test_create_collection_with_subdirectory(self, tmp_path):
        """GIVEN subdirectory parameter
        WHEN create_collection() is called with subdirectory
        THEN collection created in base_path/subdirectory/collection_name

        AC: create_collection() accepts optional subdirectory parameter
        AC: Collection path is base_path/subdirectory/collection_name when subdirectory provided
        AC: Projection matrix and metadata stored in subdirectory path
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path)

        result = store.create_collection(
            "test_coll", vector_size=1024, subdirectory="multimodal_index"
        )

        assert result is True, "create_collection should return True"

        # Verify collection created in subdirectory
        coll_path = tmp_path / "multimodal_index" / "test_coll"
        assert coll_path.exists(), "Collection should exist in subdirectory"

        # Verify projection matrix in subdirectory
        matrix_file = coll_path / "projection_matrix.npy"
        assert matrix_file.exists(), "Projection matrix should exist in subdirectory"

        # Verify metadata in subdirectory
        meta_file = coll_path / "collection_meta.json"
        assert meta_file.exists(), "Metadata should exist in subdirectory"

        # Verify metadata contains subdirectory info
        with open(meta_file) as f:
            metadata = json.load(f)

        assert metadata["name"] == "test_coll"
        assert metadata["vector_size"] == 1024
        assert metadata.get("subdirectory") == "multimodal_index"

    def test_upsert_and_query_with_subdirectory(self, tmp_path, test_vectors):
        """GIVEN collection in subdirectory
        WHEN upsert_points() and search() are called
        THEN operations work correctly with subdirectory paths

        AC: upsert_points() stores vectors in subdirectory collection
        AC: search() queries vectors from subdirectory collection
        AC: Point IDs and payloads preserved with subdirectory
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection(
            "test_coll", vector_size=1024, subdirectory="multimodal_index"
        )

        # Upsert points to subdirectory collection
        points = [
            {
                "id": "doc_001",
                "vector": test_vectors[0].tolist(),
                "payload": {
                    "path": "docs/guide.md",
                    "line_start": 1,
                    "line_end": 10,
                    "language": "markdown",
                    "type": "content",
                    "images": [
                        {"path": "images/diagram.png", "alt_text": "Architecture"}
                    ],
                },
            }
        ]

        store.begin_indexing("test_coll", subdirectory="multimodal_index")
        result = store.upsert_points(
            "test_coll", points, subdirectory="multimodal_index"
        )
        store.end_indexing("test_coll", subdirectory="multimodal_index")

        assert result["status"] == "ok", "upsert should succeed"

        # Verify JSON files created in subdirectory
        json_files = list((tmp_path / "multimodal_index" / "test_coll").rglob("*.json"))
        vector_files = [f for f in json_files if "collection_meta" not in f.name]
        assert len(vector_files) > 0, "Vector files should exist in subdirectory"

        # Verify stored data
        with open(vector_files[0]) as f:
            stored_point = json.load(f)

        assert stored_point["id"] == "doc_001"
        assert stored_point["payload"]["path"] == "docs/guide.md"
        assert stored_point["payload"]["images"][0]["path"] == "images/diagram.png"

        # Search in subdirectory collection
        mock_embedding_provider = Mock()
        mock_embedding_provider.get_embedding.return_value = test_vectors[0].tolist()

        search_results = store.search(
            query="database schema",
            embedding_provider=mock_embedding_provider,
            collection_name="test_coll",
            limit=5,
            subdirectory="multimodal_index",
        )

        assert len(search_results) > 0, "Should find results in subdirectory"
        assert search_results[0]["id"] == "doc_001"

    def test_subdirectory_isolation(self, tmp_path, test_vectors):
        """GIVEN collections in different subdirectories
        WHEN searching in one subdirectory
        THEN only results from that subdirectory returned

        AC: Collections in different subdirectories are isolated
        AC: Same collection name in different subdirectories coexists
        AC: Search in subdirectory A doesn't return results from subdirectory B
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path)

        # Create same collection name in two subdirectories
        store.create_collection(
            "test_coll", vector_size=1024, subdirectory="code_index"
        )
        store.create_collection(
            "test_coll", vector_size=1024, subdirectory="multimodal_index"
        )

        # Upsert to code_index
        code_points = [
            {
                "id": "code_001",
                "vector": test_vectors[0].tolist(),
                "payload": {"path": "src/main.py", "type": "content"},
            }
        ]
        store.begin_indexing("test_coll", subdirectory="code_index")
        store.upsert_points("test_coll", code_points, subdirectory="code_index")
        store.end_indexing("test_coll", subdirectory="code_index")

        # Upsert to multimodal_index
        multimodal_points = [
            {
                "id": "doc_001",
                "vector": test_vectors[1].tolist(),
                "payload": {
                    "path": "docs/guide.md",
                    "type": "content",
                    "images": [{"path": "diagram.png"}],
                },
            }
        ]
        store.begin_indexing("test_coll", subdirectory="multimodal_index")
        store.upsert_points(
            "test_coll", multimodal_points, subdirectory="multimodal_index"
        )
        store.end_indexing("test_coll", subdirectory="multimodal_index")

        # Search in code_index - should only find code_001
        mock_code_provider = Mock()
        mock_code_provider.get_embedding.return_value = test_vectors[0].tolist()

        code_results = store.search(
            query="main function",
            embedding_provider=mock_code_provider,
            collection_name="test_coll",
            limit=5,
            subdirectory="code_index",
        )

        assert len(code_results) == 1, "Should find exactly one result in code_index"
        assert code_results[0]["id"] == "code_001"
        assert "images" not in code_results[0]["payload"]

        # Search in multimodal_index - should only find doc_001
        mock_multimodal_provider = Mock()
        mock_multimodal_provider.get_embedding.return_value = test_vectors[1].tolist()

        multimodal_results = store.search(
            query="documentation guide",
            embedding_provider=mock_multimodal_provider,
            collection_name="test_coll",
            limit=5,
            subdirectory="multimodal_index",
        )

        assert (
            len(multimodal_results) == 1
        ), "Should find exactly one result in multimodal_index"
        assert multimodal_results[0]["id"] == "doc_001"
        assert "images" in multimodal_results[0]["payload"]

    def test_backward_compatibility_no_subdirectory(self, tmp_path, test_vectors):
        """GIVEN existing code without subdirectory parameter
        WHEN create_collection() called without subdirectory
        THEN collection created directly in base_path (backward compatible)

        AC: create_collection() without subdirectory works as before
        AC: Collections created at base_path/collection_name when no subdirectory
        AC: Existing code continues to work without changes
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path)

        # Create collection without subdirectory (existing behavior)
        result = store.create_collection("test_coll", vector_size=1024)

        assert result is True

        # Verify collection created directly in base_path (not in subdirectory)
        coll_path = tmp_path / "test_coll"
        assert coll_path.exists(), "Collection should exist directly in base_path"

        # Verify no subdirectory was created
        assert not (tmp_path / "multimodal_index").exists()

        # Verify metadata doesn't have subdirectory field
        meta_file = coll_path / "collection_meta.json"
        with open(meta_file) as f:
            metadata = json.load(f)

        assert (
            "subdirectory" not in metadata or metadata["subdirectory"] is None
        ), "No subdirectory in metadata"

        # Verify upsert and search work without subdirectory parameter
        points = [
            {
                "id": "test_001",
                "vector": test_vectors[0].tolist(),
                "payload": {"path": "src/test.py"},
            }
        ]

        store.begin_indexing("test_coll")  # No subdirectory parameter
        store.upsert_points("test_coll", points)  # No subdirectory parameter
        store.end_indexing("test_coll")  # No subdirectory parameter

        mock_embedding_provider = Mock()
        mock_embedding_provider.get_embedding.return_value = test_vectors[0].tolist()

        results = store.search(
            query="test code",
            embedding_provider=mock_embedding_provider,
            collection_name="test_coll",
            limit=5,
        )  # No subdirectory parameter

        assert len(results) > 0
        assert results[0]["id"] == "test_001"
