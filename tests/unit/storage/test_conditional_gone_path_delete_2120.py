"""Story #2120 / Bug #2124: the storage primitives behind the reconcile's
post-processing removal of gone paths. Real SQLite, real
``FilesystemVectorStore`` (both chunk layouts)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    PathIndex,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 16
COLLECTION = "voyage-code-3"


def _record(point_id: str, path: str) -> Dict[str, Any]:
    return {"id": point_id, "vector": [0.5] * VECTOR_DIM, "payload": {"path": path}}


# ---------------------------------------------------------------------------
# ChunkStore.point_ids_by_path
# ---------------------------------------------------------------------------


def test_point_ids_by_path_groups_ids_by_exact_stored_path(tmp_path: Path) -> None:
    with ChunkStore(tmp_path / "chunks.db") as store:
        store.write_batch(
            [_record("a", "x.py"), _record("b", "y.py"), _record("c", "y.py")]
        )
        assert store.point_ids_by_path(["y.py", "missing.py"]) == {"y.py": {"b", "c"}}
        assert store.point_ids_by_path([]) == {}


def test_point_ids_by_path_batches_more_paths_than_one_statement_allows(
    tmp_path: Path,
) -> None:
    paths = [f"f{i:04d}.py" for i in range(1203)]
    with ChunkStore(tmp_path / "chunks.db") as store:
        store.write_batch([_record(f"id-{p}", p) for p in paths])
        result = store.point_ids_by_path(paths)
    assert result == {p: {f"id-{p}"} for p in paths}


def test_point_ids_by_path_on_a_store_without_chunks_table_is_empty(
    tmp_path: Path,
) -> None:
    db = tmp_path / "chunks.db"
    sqlite3.connect(str(db)).close()
    with ChunkStore(db, read_only=True) as store:
        assert store.point_ids_by_path(["x.py"]) == {}


# ---------------------------------------------------------------------------
# ChunkStore.delete_points_still_at_paths
# ---------------------------------------------------------------------------


def test_delete_still_at_paths_keeps_a_row_another_path_took_over(
    tmp_path: Path,
) -> None:
    with ChunkStore(tmp_path / "chunks.db") as store:
        store.write_batch(
            [
                _record("own", "gone.py"),
                _record("shared", "live_copy.py"),
                _record("other", "other.py"),
            ]
        )
        deleted = store.delete_points_still_at_paths(
            {"own": "gone.py", "shared": "gone.py", "absent": "gone.py"}
        )
        assert deleted == {"own": "gone.py"}
        assert store.all_point_ids() == {"shared", "other"}
        assert store.delete_points_still_at_paths({}) == {}


def test_delete_still_at_paths_batches_past_one_statement(tmp_path: Path) -> None:
    ids = [f"p{i:04d}" for i in range(1203)]
    with ChunkStore(tmp_path / "chunks.db") as store:
        store.write_batch([_record(i, "gone.py") for i in ids])
        deleted = store.delete_points_still_at_paths({i: "gone.py" for i in ids})
        assert set(deleted) == set(ids)
        assert store.count() == 0


def test_delete_still_at_paths_refuses_a_read_only_store(tmp_path: Path) -> None:
    db = tmp_path / "chunks.db"
    with ChunkStore(db) as store:
        store.write_batch([_record("own", "gone.py")])
    with ChunkStore(db, read_only=True) as store:
        with pytest.raises(Exception):
            store.delete_points_still_at_paths({"own": "gone.py"})
    with ChunkStore(db) as store:
        assert store.all_point_ids() == {"own"}


# ---------------------------------------------------------------------------
# FilesystemVectorStore.delete_points_still_at_paths (both layouts)
# ---------------------------------------------------------------------------


def _points(path: str, ids: List[str]) -> List[Dict[str, Any]]:
    rng = np.random.default_rng(len(ids))
    return [
        {
            "id": point_id,
            "vector": rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist(),
            "payload": {"path": path, "type": "content", "chunk_index": i},
        }
        for i, point_id in enumerate(ids)
    ]


def _store_with_a_taken_over_point(
    base: Path, chunks_db: bool
) -> FilesystemVectorStore:
    """``gone.py`` stored ``own`` and ``shared``; a live byte-identical copy
    then re-upserted ``shared`` under its own path (``live_copy.py``)."""
    store = FilesystemVectorStore(
        base_path=base, use_chunks_db_for_new_collections=chunks_db
    )
    store.create_collection(COLLECTION, vector_size=VECTOR_DIM)
    store.begin_indexing(COLLECTION)
    store.upsert_points(COLLECTION, _points("gone.py", ["own", "shared"]))
    store.upsert_points(COLLECTION, _points("keep.py", ["keep"]))
    store.end_indexing(COLLECTION)
    store.begin_indexing(COLLECTION)
    store.upsert_points(COLLECTION, _points("live_copy.py", ["shared"]))
    store.end_indexing(COLLECTION)
    return store


def _stored_path(store: FilesystemVectorStore, point_id: str):
    point = store.get_point(point_id, COLLECTION)
    return None if point is None else point["payload"]["path"]


@pytest.mark.parametrize("chunks_db", [True, False], ids=["chunks-db", "sharded-json"])
def test_store_deletes_only_points_still_under_the_gone_path(
    tmp_path: Path, chunks_db: bool
) -> None:
    store = _store_with_a_taken_over_point(tmp_path, chunks_db)

    deleted = store.delete_points_still_at_paths(
        COLLECTION, {"own": "gone.py", "shared": "gone.py"}
    )

    assert deleted == 1
    assert _stored_path(store, "own") is None
    assert _stored_path(store, "shared") == "live_copy.py"
    assert _stored_path(store, "keep") == "keep.py"
    persisted = PathIndex.load(tmp_path / COLLECTION / "path_index.bin")
    assert persisted.get_point_ids("gone.py") <= {"shared"}
    assert "own" not in persisted.get_point_ids("gone.py")
    assert store.delete_points_still_at_paths(COLLECTION, {}) == 0


def test_sharded_json_conditional_delete_is_atomic_against_a_concurrent_writer(
    tmp_path: Path,
) -> None:
    """Codex P2: on the legacy layout, a writer that moves a shared point to
    a live path between the delete's path check and its deletion must never
    lose that point. A spy around the store's real ``delete_points`` (the
    step right after the check) starts a real writer thread there and gives
    it up to 1 s: the delete must be atomic, so the writer waits for it and
    the point survives under its live path."""
    store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=False
    )
    store.create_collection(COLLECTION, vector_size=VECTOR_DIM)
    store.begin_indexing(COLLECTION)
    store.upsert_points(COLLECTION, _points("gone.py", ["own", "shared"]))
    store.end_indexing(COLLECTION)
    real_delete_points = store.delete_points
    writers: List[threading.Thread] = []

    def delete_points_with_a_writer_in_between(collection, point_ids):
        writer = threading.Thread(
            target=store.upsert_points,
            args=(COLLECTION, _points("live_copy.py", ["shared"])),
        )
        writers.append(writer)
        writer.start()
        writer.join(timeout=1.0)  # a racing writer finishes within this
        return real_delete_points(collection, point_ids)

    store.delete_points = delete_points_with_a_writer_in_between  # type: ignore[method-assign]

    store.delete_points_still_at_paths(
        COLLECTION, {"own": "gone.py", "shared": "gone.py"}
    )
    writers[0].join(timeout=30)
    assert not writers[0].is_alive()

    assert _stored_path(store, "own") is None
    assert _stored_path(store, "shared") == "live_copy.py", (
        "the shared point a concurrent writer moved to a live path was "
        "deleted by the gone path's conditional delete"
    )


def _stored_ids_at(base: Path, path: str) -> set:
    with ChunkStore(base / COLLECTION / "chunks.db") as chunk_store:
        return chunk_store.point_ids_by_path([path]).get(path, set())


def test_fatal_error_after_committed_batches_leaves_no_stale_path_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex r2 P2-2: batches that committed before a fatal error are in
    the on-disk path index even when the session is then aborted (no
    end_indexing), so a later run never sees owners chunks.db lost."""
    store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=True
    )
    store.create_collection(COLLECTION, vector_size=VECTOR_DIM)
    gone_ids = [f"g{i:05d}" for i in range(2500)]
    store.begin_indexing(COLLECTION)
    store.upsert_points(COLLECTION, _points("gone.py", gone_ids))
    store.end_indexing(COLLECTION)

    real_delete = ChunkStore.delete_points_still_at_paths
    calls = []

    def second_batch_fails(self, expected):
        calls.append(len(expected))
        if len(calls) > 1:
            raise sqlite3.OperationalError("disk I/O error (injected)")
        return real_delete(self, expected)

    monkeypatch.setattr(ChunkStore, "delete_points_still_at_paths", second_batch_fails)
    store.begin_indexing(COLLECTION)
    with pytest.raises(sqlite3.OperationalError):
        store.delete_points_still_at_paths(
            COLLECTION, {pid: "gone.py" for pid in gone_ids}
        )
    store.abort_indexing(COLLECTION)

    still_stored = _stored_ids_at(tmp_path, "gone.py")
    assert 0 < len(still_stored) < len(gone_ids), "fixture: one batch committed"
    persisted = PathIndex.load(tmp_path / COLLECTION / "path_index.bin")
    assert persisted.get_point_ids("gone.py") == still_stored


@pytest.mark.parametrize("chunks_db", [True, False], ids=["chunks-db", "sharded-json"])
def test_taken_over_point_loses_its_gone_owner_and_is_cleaned_as_an_orphan(
    tmp_path: Path, chunks_db: bool
) -> None:
    """Codex r2 P2-2: a point a live copy took over survives the gone path's
    delete, but the gone path must stop owning it in the path index;
    otherwise, when the live copy changes, that phantom owner blocks the
    removal of the copy's orphan row."""
    store = _store_with_a_taken_over_point(tmp_path, chunks_db=chunks_db)
    store.delete_points_still_at_paths(
        COLLECTION, {"own": "gone.py", "shared": "gone.py"}
    )

    store.begin_indexing(COLLECTION)
    store.upsert_points(COLLECTION, _points("live_copy.py", ["changed"]))
    store.end_indexing(COLLECTION)

    assert _stored_path(store, "shared") is None, (
        "the live copy's orphan row was kept for a gone owner"
    )
    assert _stored_path(store, "changed") == "live_copy.py"


def test_store_delete_inside_a_session_is_persisted_at_once(
    tmp_path: Path,
) -> None:
    """Codex r2 P2-2: committed deletes reach path_index.bin immediately,
    also inside a session (an aborted session never reaches end_indexing);
    the session still records them for the HNSW update."""
    store = _store_with_a_taken_over_point(tmp_path, chunks_db=True)
    path_index_file = tmp_path / COLLECTION / "path_index.bin"

    store.begin_indexing(COLLECTION)
    assert store.delete_points_still_at_paths(COLLECTION, {"own": "gone.py"}) == 1
    assert "own" in store._indexing_session_changes[COLLECTION]["deleted"]
    assert "own" not in PathIndex.load(path_index_file).get_point_ids("gone.py")
    store.end_indexing(COLLECTION)

    assert "own" not in PathIndex.load(path_index_file).get_point_ids("gone.py")
    assert _stored_path(store, "own") is None
