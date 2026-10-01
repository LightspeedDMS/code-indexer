"""TDD test for Bug #1583 -- ``_load_id_index()`` blindly trusts a stale
``id_index.bin`` (SHARDED_JSON layout only; CHUNKS_DB is a separate code
path and is not affected).

RED phase: this test MUST FAIL against pre-fix ``FilesystemVectorStore``,
because ``_load_id_index()`` returns ``id_index.bin``'s persisted contents
unconditionally whenever the file is present and non-corrupt -- it never
cross-checks against the actual ``vector_*.json`` files on disk. A vector
file written outside the normal ``upsert_points()``/``end_indexing()``
write path (or a crash between writing the vector file and persisting the
updated ``id_index.bin``) therefore becomes permanently invisible to
point-id lookups until the index happens to be rebuilt for some unrelated
reason.

Real filesystem I/O via ``FilesystemVectorStore`` + ``tmp_path`` throughout
-- no mocking of the code under test, following the established pattern in
sibling ``_1575_`` test files in this directory.
"""

import json

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_DIM = 16


def _vector(seed: int = 0):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist()


class TestLoadIdIndexStalenessDetection:
    """Bug #1583: id_index.bin is a CACHE, not an authority -- _load_id_index()
    must detect a stale/inconsistent cache relative to the on-disk
    vector_*.json files and rebuild from disk, rather than trusting the
    cached binary index unconditionally.
    """

    def test_point_written_outside_normal_write_path_is_discoverable_after_reload(
        self, tmp_path
    ):
        writer = FilesystemVectorStore(base_path=tmp_path)
        writer.create_collection("coll", vector_size=VECTOR_DIM)
        writer.begin_indexing("coll")
        writer.upsert_points(
            "coll",
            [
                {
                    "id": "p_0",
                    "vector": _vector(0),
                    "payload": {"path": "src/a.py", "type": "content"},
                }
            ],
        )
        writer.end_indexing("coll")

        # id_index.bin now exists on disk, persisted with exactly {"p_0": ...}.
        collection_path = tmp_path / "coll"
        assert (collection_path / "id_index.bin").exists()

        # Simulate a vector file written outside the normal write path (or a
        # crash between writing the vector file and persisting the updated
        # id_index.bin): write a NEW vector_*.json file directly to disk
        # without touching id_index.bin at all.
        bypass_point_id = "bypass_written_directly"
        bypass_file = collection_path / f"vector_{bypass_point_id}.json"
        bypass_file.write_text(
            json.dumps(
                {
                    "id": bypass_point_id,
                    "vector": _vector(1),
                    "payload": {"path": "src/bypass.py", "type": "content"},
                }
            )
        )

        # A FRESH store instance (no in-memory cache) must discover the
        # bypass-written point via a point-id lookup -- currently it is
        # silently missing because _load_id_index() trusts the stale
        # id_index.bin unconditionally.
        fresh_reader = FilesystemVectorStore(base_path=tmp_path)
        result = fresh_reader.get_point(bypass_point_id, "coll")

        assert result is not None, (
            "a point written directly to disk (bypassing the normal write "
            "path) must be discoverable after reload once id_index.bin "
            "staleness is detected and the index is rebuilt from disk"
        )
        assert result["id"] == bypass_point_id
        assert result["payload"]["path"] == "src/bypass.py"

        # The original, correctly-indexed point must remain discoverable
        # too -- no regression from the staleness-detection mechanism.
        original = fresh_reader.get_point("p_0", "coll")
        assert original is not None
        assert original["id"] == "p_0"

    def test_two_successive_bypass_writes_both_heal_in_same_process(self, tmp_path):
        """Dual-review Fix 2 discriminating test.

        Pre-fix, the reactive-rebuild marker is added to
        ``_id_index_reactive_rebuild_done`` UNCONDITIONALLY as soon as a scan
        is attempted -- regardless of whether the scan actually located the
        requested point. That means a SUCCESSFUL heal for the first
        bypass-written point permanently disarms the mechanism for the
        collection: a SECOND, later out-of-band bypass write in the same
        process is never picked up, because the marker already blocks any
        further scan.

        This test proves both bypass writes, performed successively against
        the SAME store instance (same process), must be independently
        discoverable.
        """
        writer = FilesystemVectorStore(base_path=tmp_path)
        writer.create_collection("coll", vector_size=VECTOR_DIM)
        writer.begin_indexing("coll")
        writer.upsert_points(
            "coll",
            [
                {
                    "id": "p_0",
                    "vector": _vector(0),
                    "payload": {"path": "src/a.py", "type": "content"},
                }
            ],
        )
        writer.end_indexing("coll")

        collection_path = tmp_path / "coll"

        # First out-of-band bypass write.
        first_id = "bypass_first"
        (collection_path / f"vector_{first_id}.json").write_text(
            json.dumps(
                {
                    "id": first_id,
                    "vector": _vector(1),
                    "payload": {"path": "src/first.py", "type": "content"},
                }
            )
        )

        reader = FilesystemVectorStore(base_path=tmp_path)
        first_result = reader.get_point(first_id, "coll")
        assert first_result is not None, "first bypass write must heal"
        assert first_result["id"] == first_id

        # Second, LATER out-of-band bypass write to the SAME collection, in
        # the SAME process (same `reader` instance).
        second_id = "bypass_second"
        (collection_path / f"vector_{second_id}.json").write_text(
            json.dumps(
                {
                    "id": second_id,
                    "vector": _vector(2),
                    "payload": {"path": "src/second.py", "type": "content"},
                }
            )
        )

        second_result = reader.get_point(second_id, "coll")
        assert second_result is not None, (
            "a SECOND successive out-of-band bypass write in the same "
            "process must ALSO self-heal -- a successful heal for the "
            "first point must not permanently disarm reactive rebuild for "
            "the whole collection"
        )
        assert second_result["id"] == second_id

    def test_failed_scan_does_not_permanently_suppress_a_later_successful_retry(
        self, tmp_path
    ):
        """Dual-review Fix 2 discriminating test (Codex repro shape).

        Pre-fix, the marker is added BEFORE the scan runs, so a scan that
        FAILS (e.g. a real ``DuplicateSourceIdError`` from two source files
        sharing the same point_id) still permanently marks the collection as
        done -- a LATER lookup, even after the underlying problem is fixed,
        is silently suppressed and returns a false miss forever.
        """
        writer = FilesystemVectorStore(base_path=tmp_path)
        writer.create_collection("coll", vector_size=VECTOR_DIM)
        writer.begin_indexing("coll")
        writer.upsert_points(
            "coll",
            [
                {
                    "id": "p_0",
                    "vector": _vector(0),
                    "payload": {"path": "src/a.py", "type": "content"},
                }
            ],
        )
        writer.end_indexing("coll")

        collection_path = tmp_path / "coll"

        # Inject a genuine duplicate-source-id condition: two distinct
        # vector_*.json files both claiming the SAME point id.
        dup_id = "dup_point"
        (collection_path / "vector_dupA.json").write_text(
            json.dumps(
                {
                    "id": dup_id,
                    "vector": _vector(3),
                    "payload": {"path": "src/dupA.py", "type": "content"},
                }
            )
        )
        (collection_path / "vector_dupB.json").write_text(
            json.dumps(
                {
                    "id": dup_id,
                    "vector": _vector(4),
                    "payload": {"path": "src/dupB.py", "type": "content"},
                }
            )
        )

        reader = FilesystemVectorStore(base_path=tmp_path)

        # Trigger the reactive scan by looking up a genuinely-missing id.
        # The duplicate-id condition makes the underlying scan raise --
        # get_point() must degrade this to a plain miss, not propagate a
        # new exception type out of a query-path method.
        target_id = "target_after_fix"
        miss_result = reader.get_point(target_id, "coll")
        assert miss_result is None, (
            "a scan failure (duplicate source ids) must degrade to a plain "
            "miss, not raise out of get_point()"
        )

        # Now CORRECT the underlying problem: remove one of the duplicate
        # source files, and add the genuinely-requested point on disk.
        (collection_path / "vector_dupB.json").unlink()
        (collection_path / f"vector_{target_id}.json").write_text(
            json.dumps(
                {
                    "id": target_id,
                    "vector": _vector(5),
                    "payload": {"path": "src/target.py", "type": "content"},
                }
            )
        )

        retry_result = reader.get_point(target_id, "coll")
        assert retry_result is not None, (
            "after the duplicate-source-id condition is corrected, a retry "
            "for the same point must succeed -- an earlier FAILED scan "
            "must never permanently suppress future reactive rebuilds for "
            "this collection"
        )
        assert retry_result["id"] == target_id

    def test_create_collection_on_existing_collection_clears_stale_marker(
        self, tmp_path
    ):
        """Dual-review Fix 3 discriminating test.

        ``create_collection()`` resets the in-memory ``_id_index`` entry for
        an EXISTING collection to an empty dict, but (pre-fix) leaves any
        pre-existing ``_id_index_reactive_rebuild_done`` marker for that same
        cache key untouched. If the marker was already set (e.g. from an
        earlier negative scan), a subsequent lookup for a point that
        genuinely exists on disk is silently suppressed forever, since the
        reset in-memory index is empty and the marker blocks the scan that
        would have found it.
        """
        writer = FilesystemVectorStore(base_path=tmp_path)
        writer.create_collection("coll", vector_size=VECTOR_DIM)
        writer.begin_indexing("coll")
        writer.upsert_points(
            "coll",
            [
                {
                    "id": "p_0",
                    "vector": _vector(0),
                    "payload": {"path": "src/a.py", "type": "content"},
                }
            ],
        )
        writer.end_indexing("coll")

        cache_key = writer._id_cache_key("coll", None)

        # Simulate a marker already set from an earlier negative scan.
        with writer._id_index_lock:
            writer._id_index_reactive_rebuild_done.add(cache_key)

        # Re-create the (existing) collection -- resets the in-memory
        # _id_index entry to {} without deleting on-disk vector files.
        writer.create_collection("coll", vector_size=VECTOR_DIM)

        # p_0 genuinely exists on disk; it must still be discoverable.
        result = writer.get_point("p_0", "coll")
        assert result is not None, (
            "create_collection() on an existing collection must clear any "
            "stale reactive-rebuild marker so a genuinely-existing point "
            "is not permanently suppressed"
        )
        assert result["id"] == "p_0"
