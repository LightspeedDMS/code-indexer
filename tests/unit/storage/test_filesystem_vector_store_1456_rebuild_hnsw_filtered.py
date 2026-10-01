"""Story #1456 (real-CLI-run bug found): rebuild_hnsw_filtered() (branch
isolation) must pass a layout_override to its internal
HNSWIndexManager.rebuild_from_vectors() call. It runs BEFORE end_indexing()
commits the CHUNKS_DB discriminator (AC1 ordering), so without the override
it silently resolves SHARDED_JSON, finds zero legacy vector_*.json files,
and publishes a filtered index with vector_count=0 despite chunks.db having
real data -- exactly the bug caught by a real `cidx index` CLI run.
"""

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_DIM = 16


def _points(n: int) -> list:
    rng = np.random.default_rng(21)
    return [
        {
            "id": f"vec_{i}",
            "vector": rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist(),
            "payload": {"path": f"vec_{i}.py"},
        }
        for i in range(n)
    ]


class TestRebuildHnswFilteredChunksDbFreshBuild:
    def test_filtered_rebuild_before_discriminator_commit_finds_real_data(
        self, tmp_path
    ):
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=VECTOR_DIM)
        collection_path = store._get_collection_path("coll")

        store.begin_indexing("coll")
        points = _points(4)
        store.upsert_points("coll", points)

        # Discriminator NOT yet committed (end_indexing hasn't run) --
        # mirrors branch isolation's real call ordering during a fresh
        # `cidx index` run.
        from code_indexer.storage.shared.chunk_layout import (
            ChunkLayout,
            resolve_chunk_layout,
        )

        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        visible_files = {p["payload"]["path"] for p in points}
        count = store.rebuild_hnsw_filtered(
            "coll", visible_files=visible_files, current_branch="master"
        )

        assert count == 4
