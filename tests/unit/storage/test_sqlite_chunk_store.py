"""Unit tests for the SQLite chunk-store engine (Story #1455, Epic #1454).

Covers AC1-AC5 from the story:
  AC1: Full field passthrough (current writer shape)
  AC2: Legacy record-shape passthrough
  AC3: Vectors as raw float32 BLOB with finiteness validation
  AC4: Mutable in-place mode (hidden_branches update, point deletion)
  AC5: Immutable read mode, gated by is_immutable_versioned_snapshot()

AC6 (NFS-equivalence) is covered by a separate, permanent integration test
file: ``tests/integration/storage/test_sqlite_chunk_store_nfs_ac6.py``. It
is gated by the ``CIDX_CHUNK_STORE_NFS_TEST_DIR`` environment variable and
skipped by default (no CI runner or dev machine has a real NFS mount); see
that file's docstring for how to run it for real, and the story's final
report for evidence of a real run against a live NFS mount.
"""

from typing import List

import numpy as np
import pytest

from code_indexer.storage.sqlite_chunk_store import ChunkStore


VECTOR_SIZE = 8


def _make_vector(seed: int = 0) -> List[float]:
    rng = np.random.RandomState(seed)
    result: List[float] = rng.rand(VECTOR_SIZE).astype(np.float32).tolist()
    return result


class TestFullFieldPassthroughCurrentShape:
    """AC1: A chunk record produced by today's writer round-trips with zero
    field loss, including the top-level `metadata` object as a sibling of
    `payload` (NOT nested inside it) plus exactly one content variant.
    """

    def test_chunk_text_variant_round_trips_every_field(self, tmp_path):
        db_path = tmp_path / "chunks.db"

        record = {
            "id": "point-1",
            "vector": _make_vector(1),
            "metadata": {"language": "python", "type": "content"},
            "payload": {
                "path": "src/foo.py",
                "line_start": 1,
                "line_end": 10,
                "hidden_branches": ["feature/x"],
                "language": "python",
                "type": "content",
            },
            "chunk_text": "def foo():\n    return 42\n",
        }

        with ChunkStore(db_path) as store:
            store.write_batch([record])
            result = store.read("point-1")

        assert result is not None
        assert result["id"] == "point-1"
        assert np.allclose(
            np.asarray(result["vector"], dtype="<f4"),
            np.asarray(record["vector"], dtype="<f4"),
        )
        # The top-level metadata object is a DISTINCT sibling of payload --
        # must survive, never dropped by a whitelist (the #1361 CIDX2 bug).
        assert result["metadata"] == record["metadata"]
        # The entire payload dict must survive byte-for-byte.
        assert result["payload"] == record["payload"]
        assert result["chunk_text"] == record["chunk_text"]
        # No content variant should be synthesized that wasn't present.
        assert "git_blob_hash" not in result
        assert "indexed_with_uncommitted_changes" not in result

    def test_git_blob_hash_variant_round_trips_without_synthetic_chunk_text(
        self, tmp_path
    ):
        """Clean git files store git_blob_hash + indexed_with_uncommitted_changes=False,
        and NEVER get a synthetic chunk_text (Technical Requirement, AC1)."""
        db_path = tmp_path / "chunks.db"

        record = {
            "id": "point-2",
            "vector": _make_vector(2),
            "metadata": {"language": "python", "type": "content"},
            "payload": {
                "path": "src/bar.py",
                "line_start": 5,
                "line_end": 20,
            },
            "git_blob_hash": "abc123def456",
            "indexed_with_uncommitted_changes": False,
        }

        with ChunkStore(db_path) as store:
            store.write_batch([record])
            result = store.read("point-2")

        assert result is not None
        assert result["git_blob_hash"] == "abc123def456"
        assert result["indexed_with_uncommitted_changes"] is False
        assert result["payload"] == record["payload"]
        assert "chunk_text" not in result

    def test_reconstruct_from_git_pointer_variant_round_trips(self, tmp_path):
        """Added/deleted temporal-diff files: no chunk_text, only a
        reconstruct_from_git pointer living inside payload (Technical
        Requirement, AC1)."""
        db_path = tmp_path / "chunks.db"

        record = {
            "id": "point-3",
            "vector": _make_vector(3),
            "metadata": {"language": "python", "type": "commit_diff"},
            "payload": {
                "path": "src/baz.py",
                "type": "commit_diff",
                "reconstruct_from_git": True,
                "commit_hash": "deadbeef",
            },
        }

        with ChunkStore(db_path) as store:
            store.write_batch([record])
            result = store.read("point-3")

        assert result is not None
        assert result["payload"]["reconstruct_from_git"] is True
        assert result["payload"] == record["payload"]
        assert "chunk_text" not in result
        assert "git_blob_hash" not in result

    def test_read_missing_point_returns_none(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            result = store.read("does-not-exist")
        assert result is None


class TestLegacyRecordShapePassthrough:
    """AC2: An older on-disk record shape (if one exists on the fleet) also
    round-trips. Survey finding (documented in the story report): a prior
    writer shape carried top-level `file_path`/`start_line`/`end_line`
    fields (duplicated from payload) that was removed by commit f177e4494
    (2025-10-28) -- see the comment at filesystem_vector_store.py:1903
    ("file_path, start_line, end_line removed - already in payload"). This
    legacy shape is a strict SUPERSET of the current shape, and the
    passthrough-by-construction design (exclude only id/vector) already
    handles it without any special-casing.
    """

    def test_legacy_top_level_file_path_fields_round_trip(self, tmp_path):
        db_path = tmp_path / "chunks.db"

        legacy_record = {
            "id": "legacy-point-1",
            "vector": _make_vector(4),
            # Legacy top-level duplicate fields (pre commit f177e4494):
            "file_path": "src/legacy.py",
            "start_line": 1,
            "end_line": 5,
            "metadata": {"language": "python", "type": "content"},
            "payload": {
                "path": "src/legacy.py",
                "line_start": 1,
                "line_end": 5,
            },
            "chunk_text": "legacy content",
        }

        with ChunkStore(db_path) as store:
            store.write_batch([legacy_record])
            result = store.read("legacy-point-1")

        assert result is not None
        assert result["file_path"] == "src/legacy.py"
        assert result["start_line"] == 1
        assert result["end_line"] == 5
        assert result["payload"] == legacy_record["payload"]
        assert result["chunk_text"] == "legacy content"


class TestVectorEncodingAndValidation:
    """AC3: Vectors are stored as raw float32 BLOB
    (np.asarray(v, '<f4').tobytes()), never JSON text, byte-identical on
    read-back. NaN/inf rejected loudly (NEW check). Existing dtype/dimension
    validation behavior is preserved.
    """

    def test_vector_stored_as_raw_float32_blob_byte_identical(self, tmp_path):
        import sqlite3

        db_path = tmp_path / "chunks.db"
        vector = _make_vector(5)
        expected_bytes = np.asarray(vector, dtype="<f4").tobytes()

        with ChunkStore(db_path) as store:
            store.write_batch(
                [
                    {
                        "id": "vec-1",
                        "vector": vector,
                        "payload": {"path": "a.py"},
                    }
                ]
            )

        # Inspect the raw stored bytes directly -- must be the float32 blob,
        # never a JSON-text representation of the vector.
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT vector FROM chunks WHERE point_id = ?", ("vec-1",)
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        stored_blob = row[0]
        assert isinstance(stored_blob, bytes)
        assert stored_blob == expected_bytes

        with ChunkStore(db_path) as store:
            result = store.read("vec-1")
        assert result is not None
        assert np.asarray(result["vector"], dtype="<f4").tobytes() == expected_bytes

    def test_nan_vector_rejected_loudly(self, tmp_path):
        from code_indexer.storage.sqlite_chunk_store import NonFiniteVectorError

        db_path = tmp_path / "chunks.db"
        bad_vector = _make_vector(6)
        bad_vector[0] = float("nan")

        with ChunkStore(db_path) as store:
            with pytest.raises(NonFiniteVectorError):
                store.write_batch(
                    [{"id": "nan-1", "vector": bad_vector, "payload": {}}]
                )
            # Rejected write must not silently coerce/persist a NaN row.
            assert store.read("nan-1") is None

    def test_inf_vector_rejected_loudly(self, tmp_path):
        from code_indexer.storage.sqlite_chunk_store import NonFiniteVectorError

        db_path = tmp_path / "chunks.db"
        bad_vector = _make_vector(7)
        bad_vector[3] = float("inf")

        with ChunkStore(db_path) as store:
            with pytest.raises(NonFiniteVectorError):
                store.write_batch(
                    [{"id": "inf-1", "vector": bad_vector, "payload": {}}]
                )
            assert store.read("inf-1") is None

    def test_non_numeric_vector_rejected_with_invalid_vector_error(self, tmp_path):
        from code_indexer.storage.sqlite_chunk_store import InvalidVectorError

        db_path = tmp_path / "chunks.db"

        with ChunkStore(db_path) as store:
            with pytest.raises(InvalidVectorError):
                store.write_batch(
                    [
                        {
                            "id": "bad-dtype-1",
                            "vector": ["not", "a", "number"],
                            "payload": {},
                        }
                    ]
                )

    def test_dimension_mismatch_rejected(self, tmp_path):
        from code_indexer.storage.sqlite_chunk_store import InvalidVectorError

        db_path = tmp_path / "chunks.db"

        with ChunkStore(db_path) as store:
            store.write_batch(
                [{"id": "dim-1", "vector": _make_vector(8), "payload": {}}]
            )
            with pytest.raises(InvalidVectorError):
                store.write_batch(
                    [
                        {
                            "id": "dim-2",
                            "vector": [0.1, 0.2, 0.3],  # wrong dimension
                            "payload": {},
                        }
                    ]
                )

    def test_dimension_consistency_persists_across_sessions(self, tmp_path):
        """The vector-dimension invariant survives closing and reopening the
        store (not just an in-memory check for the current session)."""
        from code_indexer.storage.sqlite_chunk_store import InvalidVectorError

        db_path = tmp_path / "chunks.db"

        with ChunkStore(db_path) as store:
            store.write_batch(
                [{"id": "dim-3", "vector": _make_vector(9), "payload": {}}]
            )

        with ChunkStore(db_path) as store:
            with pytest.raises(InvalidVectorError):
                store.write_batch(
                    [{"id": "dim-4", "vector": [1.0, 2.0], "payload": {}}]
                )


def _seed_mutation_point(store, point_id, hidden_branches=None):
    store.write_batch(
        [
            {
                "id": point_id,
                "vector": _make_vector(10),
                "metadata": {"language": "python", "type": "content"},
                "payload": {
                    "path": "src/x.py",
                    "line_start": 1,
                    "line_end": 2,
                    "hidden_branches": hidden_branches or [],
                },
                "chunk_text": "x = 1",
            }
        ]
    )


class TestMutableModeInvariants:
    """AC4: journal_mode=DELETE, single writer connection, never immutable=1."""

    def test_journal_mode_is_delete_in_mutable_mode(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "delete"

    def test_mutable_store_is_never_opened_immutable(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            assert store._immutable is False


class TestUpdatePayloadFields:
    """AC4: read-modify-write on payload.hidden_branches, mirroring
    FilesystemVectorStore._batch_update_payload_only -- vector and
    chunk_text are preserved EXACTLY as stored; only the specified payload
    keys are merged.
    """

    def test_update_payload_fields_merges_only_specified_keys(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            _seed_mutation_point(store, "hb-1", hidden_branches=[])
            original = store.read("hb-1")

            updated = store.update_payload_fields(
                "hb-1", {"hidden_branches": ["feature/y"]}
            )
            assert updated is True

            result = store.read("hb-1")

        assert result is not None
        assert original is not None
        # Only hidden_branches changed; everything else preserved exactly,
        # including the vector bytes.
        assert result["payload"]["hidden_branches"] == ["feature/y"]
        assert result["payload"]["path"] == "src/x.py"
        assert result["payload"]["line_start"] == 1
        assert result["chunk_text"] == "x = 1"
        assert result["metadata"] == {"language": "python", "type": "content"}
        assert (
            np.asarray(result["vector"], dtype="<f4").tobytes()
            == np.asarray(original["vector"], dtype="<f4").tobytes()
        )

    def test_update_payload_fields_missing_point_returns_false(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            updated = store.update_payload_fields(
                "does-not-exist", {"hidden_branches": ["x"]}
            )
        assert updated is False

    def test_update_payload_fields_batch(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            _seed_mutation_point(store, "hb-2", hidden_branches=[])
            _seed_mutation_point(store, "hb-3", hidden_branches=[])
            original_2 = store.read("hb-2")

            count = store.update_payload_fields_batch(
                [
                    ("hb-2", {"hidden_branches": ["a"]}),
                    ("hb-3", {"hidden_branches": ["b"]}),
                ]
            )
            r2 = store.read("hb-2")
            r3 = store.read("hb-3")

        assert count == 2
        assert r2 is not None
        assert r3 is not None
        assert original_2 is not None
        assert r2["payload"]["hidden_branches"] == ["a"]
        assert r3["payload"]["hidden_branches"] == ["b"]
        assert (
            np.asarray(r2["vector"], dtype="<f4").tobytes()
            == np.asarray(original_2["vector"], dtype="<f4").tobytes()
        )

    def test_update_payload_fields_batch_skips_missing_point_id(self, tmp_path):
        """A batch update containing an id that does not exist is skipped
        gracefully -- the existing point is still updated, and the missing
        one is excluded from the returned count (docstring: 'Points not
        found are skipped gracefully')."""
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            _seed_mutation_point(store, "hb-4", hidden_branches=[])

            count = store.update_payload_fields_batch(
                [
                    ("hb-4", {"hidden_branches": ["c"]}),
                    ("does-not-exist", {"hidden_branches": ["z"]}),
                ]
            )
            r4 = store.read("hb-4")

        assert count == 1
        assert r4 is not None
        assert r4["payload"]["hidden_branches"] == ["c"]


class TestPointDeletion:
    """AC4: individual and batch point deletion, mirroring
    FilesystemVectorStore.delete_points.
    """

    def test_delete_removes_points(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            _seed_mutation_point(store, "del-1")
            _seed_mutation_point(store, "del-2")

            deleted_count = store.delete(["del-1"])

            assert store.read("del-1") is None
            assert store.read("del-2") is not None

        assert deleted_count == 1

    def test_delete_batch_of_points(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            _seed_mutation_point(store, "del-3")
            _seed_mutation_point(store, "del-4")

            deleted_count = store.delete(["del-3", "del-4"])

            assert store.read("del-3") is None
            assert store.read("del-4") is None

        assert deleted_count == 2

    def test_delete_nonexistent_point_id_is_a_no_op(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            _seed_mutation_point(store, "del-5")
            deleted_count = store.delete(["does-not-exist"])
            assert store.read("del-5") is not None
        assert deleted_count == 0


class TestImmutableModeWriteGuard:
    """AC5: Opening a chunk store immutable=1 makes it read-only. Every
    write path must reject loudly rather than silently corrupt.
    """

    def test_write_batch_rejected_on_immutable_store(self, tmp_path):
        from code_indexer.storage.sqlite_chunk_store import (
            ImmutableChunkStoreError,
        )

        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            _seed_mutation_point(store, "imm-1")

        with ChunkStore(db_path, immutable=True) as ro_store:
            # Reads still work.
            assert ro_store.read("imm-1") is not None
            with pytest.raises(ImmutableChunkStoreError):
                ro_store.write_batch(
                    [{"id": "imm-2", "vector": _make_vector(11), "payload": {}}]
                )

    def test_delete_rejected_on_immutable_store(self, tmp_path):
        from code_indexer.storage.sqlite_chunk_store import (
            ImmutableChunkStoreError,
        )

        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            _seed_mutation_point(store, "imm-3")

        with ChunkStore(db_path, immutable=True) as ro_store:
            with pytest.raises(ImmutableChunkStoreError):
                ro_store.delete(["imm-3"])

    def test_update_payload_fields_rejected_on_immutable_store(self, tmp_path):
        from code_indexer.storage.sqlite_chunk_store import (
            ImmutableChunkStoreError,
        )

        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            _seed_mutation_point(store, "imm-4")

        with ChunkStore(db_path, immutable=True) as ro_store:
            with pytest.raises(ImmutableChunkStoreError):
                ro_store.update_payload_fields("imm-4", {"hidden_branches": ["x"]})


class TestImmutableGatingPredicateReuse:
    """AC5: Whether a reader may open with immutable=1 is decided by the
    EXACT existing predicate is_immutable_versioned_snapshot()
    (query_path_cache.py:324-370) -- the same predicate that already gates
    skip_staleness_check (Bug #1181). No parallel predicate is invented.
    """

    def test_proven_immutable_versioned_snapshot_path_opens_immutable(self, tmp_path):
        from code_indexer.storage.sqlite_chunk_store import (
            open_chunk_store_for_path,
        )

        # Seed a real chunks.db first (mutable), then reopen it via the
        # factory using a path string shaped like a canonical versioned
        # snapshot -- is_immutable_versioned_snapshot() must return True
        # for this shape.
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            _seed_mutation_point(store, "snap-1")

        collection_path = str(tmp_path / ".versioned" / "myalias" / "v_12345" / "coll")
        store = open_chunk_store_for_path(db_path, collection_path)
        try:
            assert store._immutable is True
        finally:
            store.close()

    def test_mutable_base_clone_path_does_not_open_immutable(self, tmp_path):
        from code_indexer.storage.sqlite_chunk_store import (
            open_chunk_store_for_path,
        )

        db_path = tmp_path / "chunks.db"
        collection_path = str(tmp_path / "index" / "voyage-code-3")
        store = open_chunk_store_for_path(db_path, collection_path)
        try:
            assert store._immutable is False
        finally:
            store.close()

    def test_factory_reuses_real_predicate_not_a_parallel_one(self):
        """Directly verify the factory imports and calls the SAME function
        object as query_path_cache.is_immutable_versioned_snapshot -- not a
        reimplementation. Imported via the SAME module path
        (``code_indexer...``, no ``src.`` prefix) the production code uses
        internally, so the identity comparison is against the one true
        sys.modules entry rather than a second copy registered under a
        different qualified name.
        """
        import code_indexer.storage.sqlite_chunk_store as chunk_store_module
        from code_indexer.server.services.query_path_cache import (
            is_immutable_versioned_snapshot,
        )

        assert (
            chunk_store_module._resolve_immutable_predicate()
            is is_immutable_versioned_snapshot
        )


class TestStreamAllAndCount:
    """Public API surface required by the epic ('stream-all') for later
    stories (HNSW rebuild, id/path-index rebuild) to consume without this
    story needing to implement those consumers itself.
    """

    def test_stream_all_yields_every_record(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            store.write_batch(
                [
                    {"id": "s-1", "vector": _make_vector(20), "payload": {"path": "a"}},
                    {"id": "s-2", "vector": _make_vector(21), "payload": {"path": "b"}},
                    {"id": "s-3", "vector": _make_vector(22), "payload": {"path": "c"}},
                ]
            )
            streamed_ids = {rec["id"] for rec in store.stream_all()}

        assert streamed_ids == {"s-1", "s-2", "s-3"}

    def test_count_returns_number_of_stored_chunks(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            assert store.count() == 0
            store.write_batch(
                [
                    {"id": "c-1", "vector": _make_vector(23), "payload": {}},
                    {"id": "c-2", "vector": _make_vector(24), "payload": {}},
                ]
            )
            assert store.count() == 2
            store.delete(["c-1"])
            assert store.count() == 1


class TestEdgeCasesEmptyBatches:
    """Empty-input edge cases for the batch write/update/delete paths."""

    def test_write_batch_empty_list_is_a_noop(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            store.write_batch([])
            assert store.count() == 0

    def test_update_payload_fields_batch_empty_list_returns_zero(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            assert store.update_payload_fields_batch([]) == 0

    def test_delete_empty_list_returns_zero(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            assert store.delete([]) == 0


class TestMalformedVectorAndLargeBatchDelete:
    def test_ragged_vector_rejected_with_invalid_vector_error(self, tmp_path):
        """A malformed/ragged vector (inconsistent nested shape) must raise
        the domain InvalidVectorError, never a raw numpy ValueError leaking
        out of the engine's public API."""
        from code_indexer.storage.sqlite_chunk_store import InvalidVectorError

        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            with pytest.raises(InvalidVectorError):
                store.write_batch(
                    [
                        {
                            "id": "ragged-1",
                            "vector": [[1, 2], [3]],
                            "payload": {},
                        }
                    ]
                )

    def test_delete_large_batch_spans_multiple_chunks(self, tmp_path):
        """Deleting more ids than SQLite's host-parameter limit allows in a
        single statement must still work correctly (bounded chunking)."""
        db_path = tmp_path / "chunks.db"
        point_ids = [f"bulk-{i}" for i in range(1200)]

        with ChunkStore(db_path) as store:
            store.write_batch(
                [
                    {"id": pid, "vector": _make_vector(i % 30), "payload": {}}
                    for i, pid in enumerate(point_ids)
                ]
            )
            assert store.count() == 1200

            deleted_count = store.delete(point_ids)

            assert store.count() == 0

        assert deleted_count == 1200
