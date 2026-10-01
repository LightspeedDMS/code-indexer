"""TDD tests for Bug #1575 Part C -- the persisted ``hnsw_sync`` state
primitives (schema validation, durable read/write, epoch-overflow-safe
transition computation) and the in-memory ``HNSWSyncSession`` dataclass.

RED phase: every test in this file must FAIL against pre-Part-C code (the
``code_indexer.storage.shared.hnsw_sync_state`` module does not exist yet).

These are pure unit tests against the new module directly -- no
FilesystemVectorStore involved. Real filesystem I/O via ``tmp_path``, no
mocking. Plain module-level test functions (not test classes with many
methods) to keep each unit small and independently reviewable.
"""

import json

import pytest

from code_indexer.storage.shared.hnsw_sync_state import (
    HNSW_SYNC_SCHEMA_VERSION,
    HNSW_SYNC_STATE_FILENAME,
    HNSWSyncSession,
    HNSWSyncState,
    compute_dirty_transition,
    parse_hnsw_sync_state,
    read_hnsw_sync_state,
    write_hnsw_sync_state,
)
from code_indexer.storage.shared.chunk_layout import ChunkLayout


def _valid_clean_dict(
    epoch: int = 5, branch: str = "main", layout: str = "sharded_json"
):
    return {
        "schema_version": HNSW_SYNC_SCHEMA_VERSION,
        "mutation_epoch": epoch,
        "published_epoch": epoch,
        "status": "clean",
        "current_branch": branch,
        "layout": layout,
    }


# --- parse_hnsw_sync_state: valid shapes ------------------------------------


def test_valid_clean_state_parses():
    state = parse_hnsw_sync_state(_valid_clean_dict())
    assert state is not None
    assert state.status == "clean"
    assert state.mutation_epoch == 5
    assert state.published_epoch == 5
    assert state.current_branch == "main"
    assert state.layout == "sharded_json"


def test_valid_dirty_state_parses():
    raw = _valid_clean_dict(epoch=5)
    raw["mutation_epoch"] = 6
    raw["status"] = "dirty"
    state = parse_hnsw_sync_state(raw)
    assert state is not None
    assert state.status == "dirty"
    assert state.mutation_epoch == 6
    assert state.published_epoch == 5


def test_current_branch_none_is_valid():
    raw = _valid_clean_dict()
    raw["current_branch"] = None
    state = parse_hnsw_sync_state(raw)
    assert state is not None
    assert state.current_branch is None


def test_chunks_db_layout_value_is_valid():
    raw = _valid_clean_dict(layout=ChunkLayout.CHUNKS_DB.value)
    state = parse_hnsw_sync_state(raw)
    assert state is not None
    assert state.layout == "chunks_db"


# --- parse_hnsw_sync_state: fail-safe on malformed/inconsistent shapes ------
# Every malformed/inconsistent shape must resolve to None (fail-safe -> the
# caller forces a full rebuild), never raise.


def test_none_is_none():
    assert parse_hnsw_sync_state(None) is None


def test_non_dict_is_none():
    assert parse_hnsw_sync_state("not a dict") is None
    assert parse_hnsw_sync_state([1, 2, 3]) is None
    assert parse_hnsw_sync_state(42) is None


def test_wrong_schema_version_is_none():
    raw = _valid_clean_dict()
    raw["schema_version"] = 2
    assert parse_hnsw_sync_state(raw) is None


def test_missing_mutation_epoch_is_none():
    raw = _valid_clean_dict()
    del raw["mutation_epoch"]
    assert parse_hnsw_sync_state(raw) is None


def test_negative_mutation_epoch_is_none():
    raw = _valid_clean_dict()
    raw["mutation_epoch"] = -1
    raw["published_epoch"] = -1
    assert parse_hnsw_sync_state(raw) is None


def test_non_integer_epoch_is_none():
    raw = _valid_clean_dict()
    raw["mutation_epoch"] = "5"
    assert parse_hnsw_sync_state(raw) is None


def test_float_epoch_is_none():
    raw = _valid_clean_dict()
    raw["mutation_epoch"] = 5.0
    assert parse_hnsw_sync_state(raw) is None


def test_bool_epoch_is_none():
    # bool is technically an int subclass in Python -- must be excluded.
    raw = _valid_clean_dict()
    raw["mutation_epoch"] = True
    raw["published_epoch"] = True
    assert parse_hnsw_sync_state(raw) is None


def test_invalid_status_is_none():
    raw = _valid_clean_dict()
    raw["status"] = "sparkly"
    assert parse_hnsw_sync_state(raw) is None


def test_clean_status_with_mismatched_epochs_is_none():
    raw = _valid_clean_dict()
    raw["mutation_epoch"] = 5
    raw["published_epoch"] = 4
    raw["status"] = "clean"
    assert parse_hnsw_sync_state(raw) is None


def test_dirty_status_with_equal_epochs_is_none():
    raw = _valid_clean_dict()
    raw["mutation_epoch"] = 5
    raw["published_epoch"] = 5
    raw["status"] = "dirty"
    assert parse_hnsw_sync_state(raw) is None


def test_dirty_status_with_published_greater_is_none():
    raw = _valid_clean_dict()
    raw["mutation_epoch"] = 4
    raw["published_epoch"] = 5
    raw["status"] = "dirty"
    assert parse_hnsw_sync_state(raw) is None


def test_non_string_current_branch_is_none():
    raw = _valid_clean_dict()
    raw["current_branch"] = 12345
    assert parse_hnsw_sync_state(raw) is None


def test_invalid_layout_is_none():
    raw = _valid_clean_dict()
    raw["layout"] = "xml_files"
    assert parse_hnsw_sync_state(raw) is None


def test_missing_layout_is_none():
    raw = _valid_clean_dict()
    del raw["layout"]
    assert parse_hnsw_sync_state(raw) is None


def test_absurdly_large_epoch_is_none():
    raw = _valid_clean_dict()
    huge = 2**100
    raw["mutation_epoch"] = huge
    raw["published_epoch"] = huge
    assert parse_hnsw_sync_state(raw) is None


# --- durable read/write round trip ------------------------------------------


def test_read_missing_collection_meta_returns_none(tmp_path):
    assert read_hnsw_sync_state(tmp_path) is None


def test_read_collection_meta_with_no_hnsw_sync_key_returns_none(tmp_path):
    (tmp_path / "collection_meta.json").write_text(
        json.dumps({"name": "coll", "vector_size": 16})
    )
    assert read_hnsw_sync_state(tmp_path) is None


def test_write_then_read_round_trip(tmp_path):
    (tmp_path / "collection_meta.json").write_text(
        json.dumps({"name": "coll", "vector_size": 16})
    )
    state = HNSWSyncState(
        schema_version=HNSW_SYNC_SCHEMA_VERSION,
        mutation_epoch=3,
        published_epoch=3,
        status="clean",
        current_branch="main",
        layout="sharded_json",
    )
    write_hnsw_sync_state(tmp_path, state)

    read_back = read_hnsw_sync_state(tmp_path)
    assert read_back == state


def test_write_does_not_touch_collection_meta_json_content(tmp_path):
    """Bug #1619: write_hnsw_sync_state() must NOT read-merge-write the
    (potentially multi-MB, id_mapping-holding) collection_meta.json on every
    call -- that full-file rewrite under a global lock, on every single
    upsert_points() call, is exactly the throughput bottleneck the bug
    report proved live via py-spy. The fix moves the ``hnsw_sync`` payload
    to its own tiny dedicated file; collection_meta.json's bytes must be
    completely untouched by this call.
    """
    meta_file = tmp_path / "collection_meta.json"
    original_content = json.dumps(
        {
            "name": "coll",
            "vector_size": 16,
            "unique_file_count": 42,
            "hnsw_index": {"vector_count": 10},
        }
    )
    meta_file.write_text(original_content)
    state = HNSWSyncState(
        schema_version=HNSW_SYNC_SCHEMA_VERSION,
        mutation_epoch=1,
        published_epoch=0,
        status="dirty",
        current_branch=None,
        layout="chunks_db",
    )
    write_hnsw_sync_state(tmp_path, state)

    # collection_meta.json is byte-for-byte untouched -- proves the write
    # path no longer re-serializes the full file (and its id_mapping).
    assert meta_file.read_text() == original_content

    sync_file = tmp_path / HNSW_SYNC_STATE_FILENAME
    raw = json.loads(sync_file.read_text())
    assert raw["status"] == "dirty"
    assert raw["mutation_epoch"] == 1


def test_write_missing_collection_meta_raises(tmp_path):
    state = HNSWSyncState(
        schema_version=HNSW_SYNC_SCHEMA_VERSION,
        mutation_epoch=1,
        published_epoch=0,
        status="dirty",
        current_branch=None,
        layout="sharded_json",
    )
    with pytest.raises(FileNotFoundError):
        write_hnsw_sync_state(tmp_path, state)


def test_write_leaves_no_stray_tmp_files(tmp_path):
    (tmp_path / "collection_meta.json").write_text(
        json.dumps({"name": "coll", "vector_size": 16})
    )
    state = HNSWSyncState(
        schema_version=HNSW_SYNC_SCHEMA_VERSION,
        mutation_epoch=1,
        published_epoch=1,
        status="clean",
        current_branch="main",
        layout="sharded_json",
    )
    write_hnsw_sync_state(tmp_path, state)
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


# --- Bug #1619 backward compat: dedicated file vs legacy embedded key -------


def test_read_falls_back_to_legacy_embedded_key_when_dedicated_file_absent(
    tmp_path,
):
    """A collection indexed entirely by pre-#1619 code has ``hnsw_sync``
    embedded in collection_meta.json and no dedicated
    HNSW_SYNC_STATE_FILENAME file at all -- it must still be readable
    without any forced/eager migration step.
    """
    legacy_state = {
        "schema_version": HNSW_SYNC_SCHEMA_VERSION,
        "mutation_epoch": 7,
        "published_epoch": 7,
        "status": "clean",
        "current_branch": "main",
        "layout": "sharded_json",
    }
    (tmp_path / "collection_meta.json").write_text(
        json.dumps({"name": "coll", "hnsw_sync": legacy_state})
    )

    read_back = read_hnsw_sync_state(tmp_path)

    assert read_back == parse_hnsw_sync_state(legacy_state)
    assert not (tmp_path / HNSW_SYNC_STATE_FILENAME).exists(), (
        "reading must never silently create the dedicated file as a side effect"
    )


def test_read_prefers_dedicated_file_over_stale_legacy_embedded_key(tmp_path):
    """Once write_hnsw_sync_state() has migrated a collection to the
    dedicated file, that file is the sole source of truth -- a stale
    legacy embedded key left behind in collection_meta.json (never cleaned
    up, by design, to avoid a full-file rewrite) must be ignored.
    """
    (tmp_path / "collection_meta.json").write_text(
        json.dumps(
            {
                "name": "coll",
                "hnsw_sync": {
                    "schema_version": HNSW_SYNC_SCHEMA_VERSION,
                    "mutation_epoch": 99,
                    "published_epoch": 99,
                    "status": "clean",
                    "current_branch": None,
                    "layout": "sharded_json",
                },
            }
        )
    )
    state = HNSWSyncState(
        schema_version=HNSW_SYNC_SCHEMA_VERSION,
        mutation_epoch=1,
        published_epoch=0,
        status="dirty",
        current_branch=None,
        layout="sharded_json",
    )
    write_hnsw_sync_state(tmp_path, state)

    read_back = read_hnsw_sync_state(tmp_path)

    assert read_back == state  # NOT the stale embedded epoch=99 value


def test_read_malformed_dedicated_file_does_not_fall_back_to_legacy(tmp_path):
    """Once the dedicated file exists, it is authoritative -- a corrupt
    dedicated file must force 'no valid state' (full rebuild), never
    silently fall back to a legacy embedded key even if that key would
    itself parse as valid.
    """
    (tmp_path / "collection_meta.json").write_text(
        json.dumps(
            {
                "name": "coll",
                "hnsw_sync": {
                    "schema_version": HNSW_SYNC_SCHEMA_VERSION,
                    "mutation_epoch": 5,
                    "published_epoch": 5,
                    "status": "clean",
                    "current_branch": None,
                    "layout": "sharded_json",
                },
            }
        )
    )
    (tmp_path / HNSW_SYNC_STATE_FILENAME).write_text("not valid json{")

    assert read_hnsw_sync_state(tmp_path) is None


# --- compute_dirty_transition ------------------------------------------------


def test_no_prior_state_bootstraps_to_one_zero():
    mutation, published = compute_dirty_transition(None)
    assert (mutation, published) == (1, 0)


def test_normal_increment_preserves_published():
    prior = parse_hnsw_sync_state(_valid_clean_dict(epoch=5))
    mutation, published = compute_dirty_transition(prior)
    assert mutation == 6
    assert published == 5


def test_increment_from_dirty_prior_preserves_published():
    raw = _valid_clean_dict(epoch=5)
    raw["mutation_epoch"] = 7
    raw["status"] = "dirty"
    prior = parse_hnsw_sync_state(raw)
    mutation, published = compute_dirty_transition(prior)
    assert mutation == 8
    assert published == 5


def test_overflow_wraps_to_one_and_resets_published_to_zero():
    from code_indexer.storage.shared.hnsw_sync_state import _MAX_EPOCH

    raw = _valid_clean_dict(epoch=_MAX_EPOCH)
    prior = parse_hnsw_sync_state(raw)
    assert prior is not None
    mutation, published = compute_dirty_transition(prior)
    # Deterministic wrap-and-reset: must never write an unbounded/overflowing
    # epoch value, and the resulting pair must be unambiguously "dirty"
    # (published reset to 0, mutation reset to 1) so the next refresh is
    # forced through a full rebuild rather than silently comparing two huge
    # equal-looking numbers.
    assert (mutation, published) == (1, 0)


# --- HNSWSyncSession ---------------------------------------------------------


def test_fresh_session_has_empty_tracking_sets_and_complete_tracking_true(tmp_path):
    session = HNSWSyncSession(
        collection_path=tmp_path,
        collection_name="coll",
        layout=ChunkLayout.SHARDED_JSON,
        start_epoch=0,
    )
    assert session.current_branch is None
    assert session.visible_files == set()
    assert session.added == set()
    assert session.updated == set()
    assert session.deleted == set()
    assert session.visibility_changed == set()
    assert session.complete_change_tracking is True


def test_sessions_for_different_collection_paths_are_independent(tmp_path):
    path_a = tmp_path / "a"
    path_b = tmp_path / "b"
    path_a.mkdir()
    path_b.mkdir()
    session_a = HNSWSyncSession(
        collection_path=path_a,
        collection_name="coll",
        layout=ChunkLayout.SHARDED_JSON,
        start_epoch=0,
    )
    session_b = HNSWSyncSession(
        collection_path=path_b,
        collection_name="coll",
        layout=ChunkLayout.SHARDED_JSON,
        start_epoch=0,
    )
    session_a.added.add("p1")
    assert session_b.added == set()
