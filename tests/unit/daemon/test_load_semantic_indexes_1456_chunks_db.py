"""Story #1456 AC7: daemon _load_semantic_indexes() gate for CHUNKS_DB
collections -- must NOT block installing an otherwise-working semantic
index just because there is no id_index.bin (which is never written for
CHUNKS_DB collections). Real (non-mocked) collection built through the real
FilesystemVectorStore API, real HNSWIndexManager rebuild -- no mocking of
the storage layer under test.
"""

import numpy as np

from code_indexer.daemon.cache import CacheEntry
from code_indexer.daemon.service import CIDXDaemonService
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_DIM = 16


class TestLoadSemanticIndexesChunksDbGate:
    def test_installs_semantic_index_without_id_index_bin(self, tmp_path):
        project_path = tmp_path / "project"
        index_dir = project_path / ".code-indexer" / "index"

        # Bug #1730: _load_semantic_indexes now resolves the CONFIGURED
        # collection (via a real config.json) before selecting one. Set
        # the configured model to "coll" -- the exact collection this test
        # builds -- so resolution is genuinely real, no mocking needed.
        from code_indexer.config import ConfigManager

        config_manager = ConfigManager(project_path / ".code-indexer" / "config.json")
        config = config_manager.create_default_config(codebase_dir=project_path)
        config.voyage_ai.model = "coll"
        config_manager.save(config)

        store = FilesystemVectorStore(
            base_path=index_dir, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=VECTOR_DIM)

        store.begin_indexing("coll")
        rng = np.random.default_rng(5)
        points = [
            {
                "id": f"v{i}",
                "vector": rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist(),
                "payload": {"path": f"f{i}.py"},
            }
            for i in range(3)
        ]
        store.upsert_points("coll", points)
        store.end_indexing("coll")

        collection_path = index_dir / "coll"
        assert not (collection_path / "id_index.bin").exists()

        service = CIDXDaemonService()
        entry = CacheEntry(project_path=project_path)

        service._load_semantic_indexes(entry)

        assert entry.hnsw_index is not None
        assert entry.collection_name == "coll"
