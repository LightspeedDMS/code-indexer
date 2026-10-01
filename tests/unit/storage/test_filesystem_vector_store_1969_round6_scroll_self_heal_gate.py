"""Unit tests for Bug #1969 Round 6, finding P1-1 (BLOCKING): `scroll_points()`
ran its destructive dedup-repair/whole-file-wipe escalation regardless of
the `self_heal` parameter Round 5 added. A DEFAULT `scroll_points()` call
(the overwhelmingly common case -- every read-only caller) could therefore
delete every chunk of a file just by hitting a pre-existing duplicate
point_id, with no opt-in whatsoever.

Round 4/5's own `test_scroll_points_now_self_heals_the_corrupt_index_case`
called `scroll_points()` WITHOUT `self_heal=True` and asserted the
collection ended up wiped -- that test CODIFIED the bug. It has been
fixed (in
`test_filesystem_vector_store_1969_round4_reprocess_and_scroll_escalation.py`)
to pass `self_heal=True` explicitly; this module adds the missing
discriminating coverage for the DEFAULT (`self_heal=False`) case.
"""

import hashlib
import json
import logging
from pathlib import Path

import pytest

from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    ScrollDataIntegrityError,
)

VECTOR_DIM = 4


def _write_repairable_duplicate_record(
    collection_dir: Path,
    *,
    project_id: str,
    file_hash: str,
    index: int,
    vector: list,
    shard_suffix: str,
    path: str = "src/foo.py",
) -> Path:
    unique_key = f"{project_id}_{file_hash}_{index}"
    point_id = hashlib.md5(unique_key.encode()).hexdigest()
    payload = {
        "path": path,
        "content": f"chunk content {index}{shard_suffix}",
        "language": "python",
        "project_id": project_id,
        "file_hash": file_hash,
        "chunk_index": index,
        "total_chunks": 1,
        "line_start": 1,
        "line_end": 10,
        "point_id": point_id,
        "unique_key": unique_key,
    }
    record = {"id": point_id, "vector": vector, "payload": payload}
    shard_dir = collection_dir / point_id[:2] / (point_id[2:4] + shard_suffix)
    shard_dir.mkdir(parents=True, exist_ok=True)
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))
    return file_path


def _add_hnsw_build_metadata(collection_dir: Path) -> None:
    meta_path = collection_dir / "collection_meta.json"
    meta = json.loads(meta_path.read_text())
    meta["hnsw_index"] = {
        "version": 1,
        "vector_dim": VECTOR_DIM,
        "space": "cosine",
        "vector_count": 0,
        "id_mapping": {},
    }
    meta_path.write_text(json.dumps(meta))


def _corrupt_id_index_bin(collection_dir: Path) -> None:
    (collection_dir / "id_index.bin").write_bytes(b"\xff\xff\xff\xff")


def _build_corrupt_collection_with_repairable_duplicate(tmp_path: Path, path: str):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = tmp_path / "coll"
    _add_hnsw_build_metadata(collection_path)
    winner_path = _write_repairable_duplicate_record(
        collection_path,
        project_id="proj",
        file_hash="sha256:round6",
        index=0,
        vector=[0.1, 0.2, 0.3, 0.4],
        shard_suffix="-a",
        path=path,
    )
    loser_path = _write_repairable_duplicate_record(
        collection_path,
        project_id="proj",
        file_hash="sha256:round6",
        index=0,
        vector=[0.9, 0.9, 0.9, 0.9],
        shard_suffix="-b",
        path=path,
    )
    _corrupt_id_index_bin(collection_path)
    return store, collection_path, winner_path, loser_path


class TestScrollPointsSelfHealGating:
    """P1-1: the destructive repair/wipe block inside scroll_points()'s
    ScrollDataIntegrityError handler must be gated on self_heal, exactly
    like _load_id_index()'s corrupt-index branch is."""

    def test_default_never_self_heals_zero_mutation(self, tmp_path: Path) -> None:
        (
            store,
            collection_path,
            winner_path,
            loser_path,
        ) = _build_corrupt_collection_with_repairable_duplicate(
            tmp_path, "src/round6_default.py"
        )

        with pytest.raises(ScrollDataIntegrityError):
            store.scroll_points(
                collection_name="coll", limit=100
            )  # self_heal omitted -> False

        # Zero mutation: neither colliding record was touched, and no
        # repair/wipe machinery ran at all.
        assert winner_path.exists()
        assert loser_path.exists()

    def test_explicit_self_heal_false_never_self_heals_zero_mutation(
        self, tmp_path: Path
    ) -> None:
        (
            store,
            collection_path,
            winner_path,
            loser_path,
        ) = _build_corrupt_collection_with_repairable_duplicate(
            tmp_path, "src/round6_explicit_false.py"
        )

        with pytest.raises(ScrollDataIntegrityError):
            store.scroll_points(collection_name="coll", limit=100, self_heal=False)

        assert winner_path.exists()
        assert loser_path.exists()

    def test_self_heal_true_still_self_heals_the_corrupt_index_case(
        self, tmp_path: Path, caplog
    ) -> None:
        (
            store,
            collection_path,
            winner_path,
            loser_path,
        ) = _build_corrupt_collection_with_repairable_duplicate(
            tmp_path, "src/round6_true.py"
        )

        with caplog.at_level(logging.WARNING):
            points, _next_offset = store.scroll_points(
                collection_name="coll", limit=100, self_heal=True
            )

        assert isinstance(points, list)
        wiped_paths = {p.get("payload", {}).get("path") for p in points}
        assert "src/round6_true.py" not in wiped_paths
        remaining_vector_files = list(collection_path.rglob("vector_*.json"))
        assert remaining_vector_files == []


class TestGetPointFailLoudOnAmbiguousRepair:
    """P1-1 review finding (Codex, turn 8): get_point()'s self_heal=True
    reactive reload must NOT silently degrade a genuine repair failure to a
    plain miss -- an opted-in write-path caller needs to know repair was
    refused, not be told the point doesn't exist."""

    def test_get_point_self_heal_true_propagates_ambiguous_repair_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, collection_path, winner_path, _ = (
            _build_corrupt_collection_with_repairable_duplicate(
                tmp_path, "src/round6_ambiguous.py"
            )
        )
        point_id = winner_path.stem[len("vector_") :]

        from code_indexer.storage.id_index_manager import IDIndexManager
        from code_indexer.storage.shared.collection_dedup_repair import (
            DedupRepairAmbiguousError,
            DedupRepairAmbiguousReason,
        )

        def _refuse_repair(self_mgr, collection_path_arg, *, self_heal: bool = False):
            raise DedupRepairAmbiguousError(
                "simulated ambiguous repair refusal",
                reason=DedupRepairAmbiguousReason.MALFORMED_RECORDS,
            )

        monkeypatch.setattr(IDIndexManager, "rebuild_from_vectors", _refuse_repair)

        with pytest.raises(DedupRepairAmbiguousError):
            store.get_point(point_id, "coll", self_heal=True)


class TestDirectMutationCallerAudit:
    @pytest.mark.parametrize(
        "operation",
        ["delete_points", "_batch_update_points", "_batch_update_payload_only"],
    )
    def test_mutation_entry_point_opts_in_to_corrupt_index_repair(
        self, tmp_path: Path, operation: str
    ) -> None:
        store, collection_path, winner_path, _ = (
            _build_corrupt_collection_with_repairable_duplicate(
                tmp_path, "src/round6_direct_mutation.py"
            )
        )
        point_id = winner_path.stem[len("vector_") :]

        if operation == "delete_points":
            result = store.delete_points("coll", [point_id])
            assert result["status"] == "ok"
        else:
            result = getattr(store, operation)(
                [{"id": point_id, "payload": {"hidden_branches": ["main"]}}],
                "coll",
            )
            assert result is True

        assert list(collection_path.rglob("vector_*.json")) == [], (
            f"{operation} must recover the implicated file before its "
            "mutation proceeds; leaving both colliding records in place "
            "would strand the write behind a corrupt id index."
        )
