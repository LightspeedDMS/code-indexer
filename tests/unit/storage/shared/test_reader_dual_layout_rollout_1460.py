"""AC1 acceptance proof: the dual-layout-aware reader tolerates BOTH the
old sharded-JSON layout and the new consolidated chunks.db layout
SIMULTANEOUSLY, within one fleet/repo (Story #1460, Epic #1454).

Stories #1455-#1459 already built the dual-layout-aware reader mechanism
(resolve_chunk_layout(), ChunkStore, IDIndexManager) -- this story's job is
NOT to reimplement it, but to prove it genuinely tolerates the THREE states
a real bake-window rollout produces side by side in the same fleet:

  1. never-migrated (pure legacy sharded-JSON) -- an old, un-upgraded node
     reads this correctly via the legacy scan path.
  2. bake-window / gated (Story #1460's deletion_authorized=False): chunks.db
     is fully built+verified+committed (ChunkLayout.CHUNKS_DB) but the
     legacy files are STILL PRESENT -- BOTH an old reader (legacy scan) and
     a new reader (ChunkStore) return correct data for it.
  3. fully migrated (deletion_authorized=True): ChunkLayout.CHUNKS_DB, zero
     legacy files -- only the new reader path applies, exactly Story
     #1458's steady-state.

A reader (or fleet) that cannot correctly distinguish and serve all three
states at once is not yet rollout-capable, per AC1's binding requirement
that the fleet-wide gate covers dual semantic layouts. Real files, real
SQLite (via the real ChunkStore) -- no mocking of the storage layer.
"""

import json
from pathlib import Path

from code_indexer.storage.id_index_manager import IDIndexManager
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)
from code_indexer.storage.shared.collection_migration import (
    consolidate_collection_in_place,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore


def _write_vector_json(collection_dir: Path, point_id: str, vector, path: str) -> Path:
    shard_dir = collection_dir / point_id[:2] / point_id[2:4]
    shard_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": point_id,
        "vector": vector,
        "metadata": {"language": "python"},
        "payload": {"path": path, "language": "python"},
        "chunk_text": f"content for {path}",
    }
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))
    return file_path


def _write_collection_meta(collection_dir: Path) -> None:
    (collection_dir / "collection_meta.json").write_text(
        json.dumps({"name": collection_dir.name, "vector_size": 2})
    )


class TestDualLayoutReaderToleratesAllThreeFleetStatesSimultaneously:
    def test_never_migrated_gated_and_fully_migrated_all_read_correctly(
        self, tmp_path: Path
    ) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)

        # 1. Never-migrated collection -- pure legacy sharded-JSON.
        never_migrated = index_path / "collection_never_migrated"
        never_migrated.mkdir()
        _write_collection_meta(never_migrated)
        _write_vector_json(never_migrated, "aaaa1111", [0.1, 0.2], "src/a.py")

        # 2. Bake-window/gated collection -- chunks.db built+committed, but
        # deletion withheld (Story #1460's rollout-safety gate).
        gated = index_path / "collection_gated"
        gated.mkdir()
        _write_collection_meta(gated)
        gated_vfile = _write_vector_json(gated, "bbbb2222", [0.3, 0.4], "src/b.py")
        gated_result = consolidate_collection_in_place(gated, deletion_authorized=False)
        assert gated_result.deletion_gated is True

        # 3. Fully migrated collection -- Story #1458's steady state.
        fully_migrated = index_path / "collection_fully_migrated"
        fully_migrated.mkdir()
        _write_collection_meta(fully_migrated)
        _write_vector_json(fully_migrated, "cccc3333", [0.5, 0.6], "src/c.py")
        migrated_result = consolidate_collection_in_place(fully_migrated)
        assert migrated_result.deletion_gated is False

        # -- The dual-layout resolver correctly distinguishes all three --
        assert resolve_chunk_layout(never_migrated) == ChunkLayout.SHARDED_JSON
        assert resolve_chunk_layout(gated) == ChunkLayout.CHUNKS_DB
        assert resolve_chunk_layout(fully_migrated) == ChunkLayout.CHUNKS_DB

        # -- An OLD reader (legacy sharded-JSON scan only) --
        # Correctly finds the never-migrated collection's data.
        never_migrated_ids = IDIndexManager().scan_vectors_for_id_map(never_migrated)
        assert "aaaa1111" in never_migrated_ids

        # Still correctly finds the GATED collection's data too, because
        # the rollout-safety gate deliberately withheld deletion -- this is
        # the entire point of AC1's bake window.
        assert gated_vfile.exists()
        gated_legacy_ids = IDIndexManager().scan_vectors_for_id_map(gated)
        assert "bbbb2222" in gated_legacy_ids

        # An old reader correctly finds NOTHING for the fully-migrated
        # collection via the legacy path (files are genuinely gone) --
        # this is why AC1 requires the reader to ship BEFORE deletion is
        # ever authorized fleet-wide.
        fully_migrated_legacy_ids = IDIndexManager().scan_vectors_for_id_map(
            fully_migrated
        )
        assert fully_migrated_legacy_ids == {}

        # -- A NEW dual-layout-aware reader (chunks.db-first) --
        # Correctly finds the gated collection's data via chunks.db too.
        with ChunkStore(gated / "chunks.db") as store:
            stored = store.read("bbbb2222")
        assert stored is not None
        assert stored["payload"]["path"] == "src/b.py"

        # And the fully-migrated collection's data.
        with ChunkStore(fully_migrated / "chunks.db") as store:
            stored = store.read("cccc3333")
        assert stored is not None
        assert stored["payload"]["path"] == "src/c.py"
