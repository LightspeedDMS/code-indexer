"""Unit tests for Bug #1969 Round 6, finding P1-5 (BLOCKING): recovery
deletes records it can't durably name a replay path for.

In `recover_from_corrupt_id_index_by_wiping_files`, only records with a
valid non-empty `payload.path` get a sidecar entry, but every implicated
record is deleted regardless of whether ANY of its group's records had a
valid path. If an implicated (project_id, file_hash) group's records all
lack a nameable path (missing, empty, absolute, or a `..` path-traversal
escape), the fix must refuse with `DedupRepairAmbiguousError` (a new,
distinct `UNRESOLVABLE_REPLAY_PATH` reason) BEFORE writing the sidecar or
deleting anything -- zero mutation.

The pre-existing malformed-record precheck (`_scan_raw_records`) does
NOT already catch this: it only flags unreadable/undecodable JSON and a
missing/invalid `id` field, never `payload.path` (confirmed by reading
`_scan_raw_records`'s `malformed.append(...)` call sites) -- so this is
genuinely new logic, not just a missing test for existing behavior.
"""

import hashlib
import json
from pathlib import Path

import pytest

from code_indexer.storage.id_index_manager import IDIndexManager
from code_indexer.storage.shared.collection_dedup_repair import (
    DedupRepairAmbiguousError,
    DedupRepairAmbiguousReason,
    read_pending_self_heal_reprocess_paths,
    recover_from_corrupt_id_index_by_wiping_files,
)

VECTOR_DIM = 4


def _point_id(project_id: str, file_hash: str, index: int) -> str:
    return hashlib.md5(f"{project_id}_{file_hash}_{index}".encode()).hexdigest()


def _write_record(
    collection_dir: Path,
    *,
    project_id: str,
    file_hash: str,
    index: int,
    vector: list,
    shard_suffix: str,
    path=Ellipsis,
) -> Path:
    """Writes a real, well-formed vector_*.json record. `path=Ellipsis`
    (the default) omits the `path` key from the payload entirely; pass an
    explicit string (including "") to set it, matching the exact shapes
    P1-5 must refuse."""
    unique_key = f"{project_id}_{file_hash}_{index}"
    point_id = _point_id(project_id, file_hash, index)
    payload = {
        "content": f"chunk content {index}{shard_suffix}",
        "language": "python",
        "project_id": project_id,
        "file_hash": file_hash,
        "chunk_index": index,
        "total_chunks": 1,
        "line_start": 0,
        "line_end": 9,
        "point_id": point_id,
        "unique_key": unique_key,
    }
    if path is not Ellipsis:
        payload["path"] = path
    record = {"id": point_id, "vector": vector, "payload": payload}
    shard_dir = collection_dir / point_id[:2] / (point_id[2:4] + shard_suffix)
    shard_dir.mkdir(parents=True, exist_ok=True)
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))
    return file_path


def _write_collection_meta(
    collection_dir: Path, *, vector_dim: int = VECTOR_DIM, space: str = "cosine"
) -> None:
    meta = {
        "name": "coll",
        "vector_size": vector_dim,
        "hnsw_index": {
            "version": 1,
            "vector_dim": vector_dim,
            "space": space,
            "vector_count": 0,
            "id_mapping": {},
        },
    }
    (collection_dir / "collection_meta.json").write_text(json.dumps(meta))


def _corrupt_id_index(collection_dir: Path) -> None:
    (collection_dir / IDIndexManager.INDEX_FILENAME).write_bytes(b"\x05\x00\x00")


def _write_duplicate_pair(collection_dir: Path, *, file_hash: str, path) -> None:
    _write_record(
        collection_dir,
        project_id="proj",
        file_hash=file_hash,
        index=0,
        vector=[0.1, 0.2, 0.3, 0.4],
        shard_suffix="-a",
        path=path,
    )
    _write_record(
        collection_dir,
        project_id="proj",
        file_hash=file_hash,
        index=0,
        vector=[0.9, 0.9, 0.9, 0.9],
        shard_suffix="-b",
        path=path,
    )


def _file_snapshot(collection_dir: Path) -> dict[str, bytes]:
    """Capture every collection file so an ambiguity must leave zero mutation."""
    return {
        str(path.relative_to(collection_dir)): path.read_bytes()
        for path in collection_dir.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    "bad_path",
    [
        Ellipsis,  # missing entirely
        "",  # empty string
        "/example/replay.py",  # absolute path escape
        "../../outside/replay.py",  # relative path-traversal escape
    ],
    ids=["missing", "empty", "absolute", "traversal"],
)
def test_recover_refuses_unresolvable_replay_path_with_zero_mutation(
    tmp_path: Path, bad_path
) -> None:
    _write_collection_meta(tmp_path)
    _write_duplicate_pair(tmp_path, file_hash="sha256:p15unresolvable", path=bad_path)
    _corrupt_id_index(tmp_path)

    files_before = sorted(tmp_path.rglob("vector_*.json"))
    assert len(files_before) == 2
    snapshot_before = _file_snapshot(tmp_path)

    with pytest.raises(DedupRepairAmbiguousError) as exc_info:
        recover_from_corrupt_id_index_by_wiping_files(tmp_path)

    assert (
        exc_info.value.reason == DedupRepairAmbiguousReason.UNRESOLVABLE_REPLAY_PATH
    ), f"expected UNRESOLVABLE_REPLAY_PATH, got {exc_info.value.reason!r}"

    files_after = sorted(tmp_path.rglob("vector_*.json"))
    assert files_after == files_before, (
        "zero mutation expected: neither colliding record may be deleted "
        "when no group has a durably nameable replay path"
    )
    assert _file_snapshot(tmp_path) == snapshot_before
    assert read_pending_self_heal_reprocess_paths(tmp_path) == frozenset(), (
        "no sidecar entry may be written for a group whose replay path "
        "could not be validated"
    )


def test_recover_still_succeeds_for_a_group_with_a_valid_path(tmp_path: Path) -> None:
    _write_collection_meta(tmp_path)
    _write_duplicate_pair(
        tmp_path, file_hash="sha256:p15valid", path="src/valid_target.py"
    )
    _corrupt_id_index(tmp_path)

    result = recover_from_corrupt_id_index_by_wiping_files(tmp_path)

    assert result.wiped_relative_paths == frozenset({"src/valid_target.py"})
    assert list(tmp_path.rglob("vector_*.json")) == []
    assert read_pending_self_heal_reprocess_paths(tmp_path) == frozenset(
        {"src/valid_target.py"}
    )


def test_recover_refuses_when_one_group_valid_and_another_unresolvable(
    tmp_path: Path,
) -> None:
    """A mixed collection -- one duplicate group has a valid path, a
    SECOND, separate duplicate group has none -- must refuse the WHOLE
    operation with zero mutation, never partially wipe the resolvable
    group while silently dropping the unresolvable one."""
    _write_collection_meta(tmp_path)
    _write_duplicate_pair(
        tmp_path, file_hash="sha256:p15mixedvalid", path="src/mixed_valid.py"
    )
    _write_duplicate_pair(tmp_path, file_hash="sha256:p15mixedbad", path=Ellipsis)
    _corrupt_id_index(tmp_path)

    files_before = sorted(tmp_path.rglob("vector_*.json"))
    assert len(files_before) == 4
    snapshot_before = _file_snapshot(tmp_path)

    with pytest.raises(DedupRepairAmbiguousError) as exc_info:
        recover_from_corrupt_id_index_by_wiping_files(tmp_path)

    assert exc_info.value.reason == DedupRepairAmbiguousReason.UNRESOLVABLE_REPLAY_PATH
    files_after = sorted(tmp_path.rglob("vector_*.json"))
    assert files_after == files_before
    assert _file_snapshot(tmp_path) == snapshot_before
    assert read_pending_self_heal_reprocess_paths(tmp_path) == frozenset()
