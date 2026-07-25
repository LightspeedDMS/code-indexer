"""Unit tests for consolidate_collection_in_place()'s rollout-safety
deletion gate (Story #1460 AC1/AC2, Epic #1454).

Story #1458 built consolidate_collection_in_place() as an unconditional
build -> verify -> flip -> delete pipeline. Story #1460's job is to prove
that an explicit `deletion_authorized` gate, defaulting to True (so all of
Story #1458's own mechanism tests are byte-identical/unaffected), lets a
caller withhold ONLY the final destructive deletion step (AC3 step 5) while
still safely completing the non-destructive build/verify/flip steps
(1-4) -- producing the "bake window" mixed-layout state AC1 describes,
where an old sharded-JSON-only reader can still find every record via the
untouched legacy files while a new dual-layout-aware reader already sees
ChunkLayout.CHUNKS_DB.

Real files, real SQLite (via the real ChunkStore) -- no mocking of the
storage layer under test.
"""

import json
from pathlib import Path

from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)
from code_indexer.storage.shared.collection_migration import (
    consolidate_collection_in_place,
)
from code_indexer.storage.id_index_manager import IDIndexManager
from code_indexer.storage.sqlite_chunk_store import ChunkStore


def _write_vector_json(collection_dir: Path, point_id: str, vector) -> Path:
    shard_dir = collection_dir / point_id[:2] / point_id[2:4]
    shard_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": point_id,
        "vector": vector,
        "metadata": {"language": "python"},
        "payload": {"path": "src/foo.py", "language": "python"},
        "chunk_text": "hello",
    }
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))
    return file_path


def _write_collection_meta(collection_dir: Path) -> None:
    (collection_dir / "collection_meta.json").write_text(
        json.dumps({"name": "coll", "vector_size": 4})
    )


class TestDeletionAuthorizedDefaultTruePreservesStory1458Behavior:
    def test_default_call_still_deletes_legacy_files(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(tmp_path, "aaaa1111", [0.1, 0.2, 0.3, 0.4])

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
        assert not vfile.exists()
        assert result.deletion_gated is False


class TestDeletionAuthorizedFalseWithholdsDestructiveCleanup:
    def test_fresh_path_builds_chunks_db_but_keeps_legacy_files(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(tmp_path, "bbbb2222", [1.0, 2.0, 3.0, 4.0])

        result = consolidate_collection_in_place(tmp_path, deletion_authorized=False)

        # Non-destructive steps (build/verify/flip) still ran -- this is
        # the "bake window" mixed-layout state AC1 requires.
        assert result.status == "consolidated"
        assert result.records_written == 1
        assert result.old_files_deleted == 0
        assert result.deletion_gated is True
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.CHUNKS_DB

        # The destructive deletion (AC3 step 5) was withheld: an
        # old/un-upgraded reader that only understands the legacy
        # sharded-JSON layout can STILL find this record on disk.
        assert vfile.exists()
        legacy_id_map = IDIndexManager().scan_vectors_for_id_map(tmp_path)
        assert "bbbb2222" in legacy_id_map

        # A new dual-layout-aware reader already sees the consolidated
        # record too -- both readers are correct simultaneously.
        with ChunkStore(tmp_path / "chunks.db") as store:
            stored = store.read("bbbb2222")
        assert stored is not None
        assert stored["payload"]["path"] == "src/foo.py"

    def test_resume_path_also_withholds_cleanup(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(tmp_path, "cccc3333", [1.0, 2.0, 3.0, 4.0])

        # First pass: build the bake-window state (gate closed).
        first = consolidate_collection_in_place(tmp_path, deletion_authorized=False)
        assert first.deletion_gated is True
        assert vfile.exists()

        # Resume pass (already CHUNKS_DB on entry) -- still gated closed,
        # must NOT delete the legacy file either.
        second = consolidate_collection_in_place(tmp_path, deletion_authorized=False)

        assert second.status == "already_consolidated"
        assert second.old_files_deleted == 0
        assert second.deletion_gated is True
        assert vfile.exists()

    def test_gate_can_later_be_flipped_on_to_complete_cleanup(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(tmp_path, "dddd4444", [1.0, 2.0, 3.0, 4.0])

        gated = consolidate_collection_in_place(tmp_path, deletion_authorized=False)
        assert gated.deletion_gated is True
        assert vfile.exists()

        # Operator confirms fleet-wide reader rollout and flips the gate
        # on -- a later pass over the SAME (already CHUNKS_DB) collection
        # now completes the destructive cleanup.
        authorized = consolidate_collection_in_place(tmp_path, deletion_authorized=True)

        assert authorized.status == "already_consolidated"
        assert authorized.old_files_deleted == 1
        assert authorized.deletion_gated is False
        assert not vfile.exists()


class TestDeletionGatedReflectsPhysicalTruthNotJustTheFlag:
    """Codex review finding: deletion_gated must be True only when a REAL
    deletion target existed AND deletion_authorized=False caused it to be
    withheld -- never merely "the flag happened to be False this call",
    mirroring old_files_deleted's own physical-truth contract."""

    def test_resume_path_already_clean_reports_not_gated_despite_flag_false(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(tmp_path, "eeee5555", [1.0, 2.0, 3.0, 4.0])

        # Fully migrate with the gate OPEN -- legacy file genuinely deleted.
        consolidate_collection_in_place(tmp_path, deletion_authorized=True)
        assert not vfile.exists()

        # A LATER pass with the gate CLOSED has nothing left to withhold --
        # deletion_gated must be False, not misleadingly True.
        result = consolidate_collection_in_place(tmp_path, deletion_authorized=False)

        assert result.status == "already_consolidated"
        assert result.old_files_deleted == 0
        assert result.deletion_gated is False

    def test_fresh_path_empty_collection_reports_not_gated_despite_flag_false(
        self, tmp_path: Path
    ) -> None:
        # An empty collection (zero legacy records) has nothing to
        # withhold either, even on the very first (fresh) pass.
        _write_collection_meta(tmp_path)

        result = consolidate_collection_in_place(tmp_path, deletion_authorized=False)

        assert result.status == "consolidated"
        assert result.records_written == 0
        assert result.old_files_deleted == 0
        assert result.deletion_gated is False
