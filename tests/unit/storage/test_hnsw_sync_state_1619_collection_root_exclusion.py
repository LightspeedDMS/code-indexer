"""Bug #1619 regression: introducing the dedicated ``hnsw_sync_state.json``
file (sibling of ``collection_meta.json`` in the collection root) must never
be misclassified as a vector/chunk record by any of the storage layer's
``rglob("*.json")`` scanners.

Several call sites (dedup-repair's record classifier, id-index rebuild,
path-index rebuild, file-timestamp collection, sample_vectors) walk the
collection directory for ``*.json`` files and excluded ONLY
``collection_meta.json`` by name/substring -- adding a second small
bookkeeping file in that same directory (Bug #1619's performance fix)
silently broke all of them: real end-to-end tests
(``test_scroll_self_heals_pre_existing_duplicate``,
``test_upsert_points_stores_json_at_quantized_paths``,
``test_upsert_and_query_with_subdirectory``,
``test_default_sample_size_is_five``) started failing with the new file
picked up as a "malformed vector record" or included in a random sample.

Each of the 5 fixed call sites gets a DIRECT test here (not just incidental
end-to-end coverage) -- a code-review follow-up after the sibling
``temporal_legacy_migration/verification.py`` site was found to have been
missed entirely in the first pass, specifically because only 1 of 5 sites
had direct coverage.
"""

import json
from datetime import datetime
from pathlib import Path

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.id_index_manager import IDIndexManager
from code_indexer.storage.shared.collection_dedup_repair import (
    _is_vector_record_file,
)
from code_indexer.storage.shared.hnsw_sync_state import HNSW_SYNC_STATE_FILENAME

VECTOR_DIM = 8


def _vector(seed: int = 0):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist()


def _point(point_id, path, seed):
    return {
        "id": point_id,
        "vector": _vector(seed),
        "payload": {"path": path, "type": "content", "hidden_branches": []},
    }


# --- Site 1: collection_dedup_repair._is_vector_record_file ---------------


def test_is_vector_record_file_excludes_dedicated_hnsw_sync_state_file():
    assert (
        _is_vector_record_file(Path(f"/some/collection/{HNSW_SYNC_STATE_FILENAME}"))
        is False
    )


def test_is_vector_record_file_still_accepts_real_vector_file():
    assert _is_vector_record_file(Path("/some/collection/vector_abc123.json")) is True


# --- Site 2: id_index_manager.IDIndexManager.scan_vectors_for_id_map ------


def test_scan_vectors_for_id_map_excludes_dedicated_hnsw_sync_state_file(tmp_path):
    """id-index rebuild must never treat hnsw_sync_state.json as a vector
    record needing an 'id' field -- neither included in the id map nor
    counted as a malformed rejection."""
    collection_path = tmp_path / "coll"
    collection_path.mkdir()
    (collection_path / "collection_meta.json").write_text(json.dumps({"name": "coll"}))
    (collection_path / "vector_abc.json").write_text(
        json.dumps({"id": "abc", "payload": {"path": "src/a.py"}})
    )
    (collection_path / HNSW_SYNC_STATE_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mutation_epoch": 1,
                "published_epoch": 0,
                "status": "dirty",
                "current_branch": None,
                "layout": "sharded_json",
            }
        )
    )

    manager = IDIndexManager()
    id_map, rejected_count = manager.scan_vectors_for_id_map_verbose(collection_path)

    assert set(id_map.keys()) == {"abc"}
    assert rejected_count == 0, (
        "hnsw_sync_state.json must be silently skipped as a known "
        "bookkeeping sidecar, never counted as a malformed rejection"
    )


# --- Site 3: FilesystemVectorStore._rebuild_path_index_from_disk ----------


def test_rebuild_path_index_excludes_dedicated_hnsw_sync_state_file(tmp_path):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points("coll", [_point("a", "src/a.py", 1)])
    store.end_indexing("coll")

    collection_path = tmp_path / "coll"
    assert (collection_path / HNSW_SYNC_STATE_FILENAME).exists(), (
        "test setup invalid: hnsw_sync_state.json should exist after a real upsert"
    )

    path_index = store._rebuild_path_index_from_disk("coll")

    assert path_index.all_paths() == {"src/a.py"}
    assert path_index.get_point_ids("src/a.py") == {"a"}


# --- Site 4: FilesystemVectorStore.get_file_index_timestamps --------------


def test_get_file_index_timestamps_excludes_dedicated_hnsw_sync_state_file(tmp_path):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points("coll", [_point("a", "src/a.py", 1)])
    store.end_indexing("coll")

    timestamps = store.get_file_index_timestamps("coll")

    assert set(timestamps.keys()) == {"src/a.py"}
    assert isinstance(timestamps["src/a.py"], datetime)


# --- Site 5: FilesystemVectorStore.sample_vectors -------------------------


def test_sample_vectors_excludes_dedicated_hnsw_sync_state_file(tmp_path):
    """Discriminating boundary case (code-review round 3 fix): the
    population is 5 real vector files + 1 hnsw_sync_state.json sidecar = 6
    files, and ``sample_size`` is the DEFAULT (5) -- strictly LESS than the
    population, so ``random.sample()`` genuinely has to choose a subset.

    Pre-fix, ``all_vector_files`` includes the sidecar (6 candidates), so
    a 5-of-6 draw pulls it in ~83% of the time; it then fails
    ``data["id"]``/``data["vector"]`` inside the per-file try/except and
    gets silently dropped, returning only 4 results instead of 5. A
    ``sample_size`` covering the WHOLE population (e.g. 10) cannot expose
    this -- ``min(sample_size, len(all_vector_files))`` would sample every
    file regardless of the fix, masking the bug entirely.

    Looped 20 times against the random draw: P(the sidecar is never drawn
    in any single trial) = 1/6, so P(false green across all 20 trials) =
    (1/6)**20, negligible.
    """
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points("coll", [_point(f"p{i}", f"src/f{i}.py", i) for i in range(5)])
    store.end_indexing("coll")

    for _ in range(20):
        sampled = store.sample_vectors("coll")  # default sample_size=5
        assert {record["id"] for record in sampled} == {f"p{i}" for i in range(5)}
