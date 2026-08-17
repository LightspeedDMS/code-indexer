"""Bug #1584 regression tests: fleet migration must not report a healthy,
migrated-then-re-indexed collection as UNRECOVERABLE data corruption.

Root cause (see GitHub issue #1584): ``chunks_db_content_manifest.json`` is
written ONCE, at migration time, to make the destructive legacy-file
deletion safe. It is never updated by the ordinary indexing write path. Any
subsequent incremental refresh legitimately adds/updates/removes rows in
``chunks.db``. Once cleanup has fully completed (zero legacy files remain),
re-verifying the ENTIRE live chunks.db against that FROZEN manifest is no
longer a valid oracle -- every legitimate post-migration write is
classified, by construction, as unrecoverable data loss.

These tests drive the REAL migration primitives
(:func:`consolidate_collection_in_place`, :func:`verify_collection_fully_
migrated`) against REAL files and a REAL SQLite-backed ``ChunkStore`` --
no mocking of the storage layer under test.
"""

from pathlib import Path

import pytest

from code_indexer.storage.shared.collection_migration import (
    CHUNKS_DB_FILENAME,
    ConsolidationResult,
    UnrecoverableConsolidationCorruptionError,
    _is_migration_cleanup_completed,
    consolidate_collection_in_place,
    verify_collection_fully_migrated,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore
from tests.unit.storage.shared.test_collection_migration_1458 import (
    _write_collection_meta,
    _write_vector_json,
)

# Offset into a SQLite file's fixed 100-byte header (well past the
# "SQLite format 3\0" magic string at bytes 0-15) and the number of bytes
# to overwrite -- enough to corrupt the header fields PRAGMA
# integrity_check inspects without accidentally landing on the magic
# string itself.
_SQLITE_HEADER_CORRUPTION_OFFSET = 20
_SQLITE_HEADER_CORRUPTION_LENGTH = 20


def _migrate_fresh_collection(
    collection_dir: Path, point_id: str, vector: list, chunk_text: str
) -> ConsolidationResult:
    """Seed ONE legacy vector_*.json record and migrate it to CHUNKS_DB
    via the real fresh-path flow, asserting the migration actually
    completed (status "consolidated", zero legacy files remaining)."""
    _write_collection_meta(collection_dir)
    _write_vector_json(collection_dir, point_id, vector, chunk_text=chunk_text)

    result = consolidate_collection_in_place(collection_dir)
    assert result.status == "consolidated"
    assert next(collection_dir.rglob("vector_*.json"), None) is None
    return result


def _simulate_post_migration_reindex(collection_dir: Path) -> None:
    """Simulate an ordinary incremental refresh that runs AFTER a
    collection has already been fully migrated to CHUNKS_DB and cleaned
    up: add a brand-new row and delete a pre-existing one directly in
    ``chunks.db`` -- exactly the kind of legitimate row-SET drift the
    real ``cidx-meta``/``embed-v4.0`` repo exhibited in the issue's
    evidence table (10 in-manifest-not-db, 13 in-db-not-manifest)."""
    chunks_db_path = collection_dir / CHUNKS_DB_FILENAME
    with ChunkStore(chunks_db_path) as store:
        existing_ids = sorted(store.all_point_ids())
        assert existing_ids, "fixture must seed at least one record"
        store.delete([existing_ids[0]])
        store.write_batch(
            [
                {
                    "id": "ffff9999",
                    "vector": [9.0, 9.0, 9.0, 9.0],
                    "metadata": {"language": "python"},
                    "payload": {"path": "src/new_file.py", "language": "python"},
                    "chunk_text": "new content added by ordinary refresh",
                    "indexed_with_uncommitted_changes": True,
                }
            ]
        )


class TestManifestDriftAfterCleanupIsNotFalseCorruption:
    def test_verify_collection_fully_migrated_returns_true_after_post_migration_reindex(
        self, tmp_path: Path
    ) -> None:
        _migrate_fresh_collection(tmp_path, "aaaa1111", [0.1, 0.2, 0.3, 0.4], "hello")

        _simulate_post_migration_reindex(tmp_path)

        assert verify_collection_fully_migrated(tmp_path) is True

    def test_consolidate_collection_in_place_does_not_raise_after_post_migration_reindex(
        self, tmp_path: Path
    ) -> None:
        _migrate_fresh_collection(tmp_path, "cccc3333", [0.5, 0.5, 0.5, 0.5], "hi")

        _simulate_post_migration_reindex(tmp_path)

        # Mirrors the real incident: FleetMigrationScheduler re-checks this
        # collection on every tick. A healthy, actively-refreshed repo must
        # never trip the terminal UnrecoverableConsolidationCorruptionError.
        result = consolidate_collection_in_place(tmp_path)
        assert result.status == "already_consolidated"

    def test_genuine_corruption_after_cleanup_completion_still_detected(
        self, tmp_path: Path
    ) -> None:
        # Regression guard: fixing the false positive above must not
        # defeat genuine corruption detection. A structurally corrupt
        # chunks.db must still be caught even after legacy cleanup has
        # completed.
        _migrate_fresh_collection(tmp_path, "bbbb2222", [1.0, 2.0, 3.0, 4.0], "x")

        chunks_db_path = tmp_path / CHUNKS_DB_FILENAME
        with open(chunks_db_path, "r+b") as f:
            f.seek(_SQLITE_HEADER_CORRUPTION_OFFSET)
            f.write(b"\xff" * _SQLITE_HEADER_CORRUPTION_LENGTH)

        assert verify_collection_fully_migrated(tmp_path) is False

        raised = False
        try:
            consolidate_collection_in_place(tmp_path)
        except UnrecoverableConsolidationCorruptionError:
            raised = True
        assert raised, (
            "genuine structural corruption discovered after legacy "
            "cleanup completed must still raise "
            "UnrecoverableConsolidationCorruptionError -- it must never "
            "be silently swallowed by the Bug #1584 fix"
        )


class TestCrashWindowMarkerWrittenBeforeSubdirCleanup:
    """Bug #1584 dual-review finding (HIGH, opus F1): the completion
    marker was written AFTER _cleanup_old_sharded_files's deletion loop
    AND after its _remove_empty_subdirs walk completed. A crash DURING
    that walk (which can be genuinely slow on a large fleet repo's deep
    hash-shard tree) left legacy-file deletion complete but the marker
    unset; the NEXT call then fell through to the frozen-manifest
    comparison and could reproduce the ORIGINAL #1584 false-positive
    corruption report if drift had occurred meanwhile.

    Fixed narrowly: _cleanup_old_sharded_files now writes the marker
    BEFORE calling _remove_empty_subdirs, not after -- closing the
    specific window this finding named, without ever bypassing the
    Issue #1503/#1486 manifest-verification pipeline (an earlier,
    broader attempt to self-heal purely from an empty still_present_id_
    map was tried and reverted: it is structurally unable to distinguish
    a genuine crash-lost marker from a hand-fabricated/pre-existing
    corrupt-manifest state, and broke 10 real regression tests proving
    exactly that detection)."""

    def test_marker_write_ordering_precedes_remove_empty_subdirs_call(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Proves ORDERING only -- that the marker write happens BEFORE
        _remove_empty_subdirs runs, so a crash during that walk never
        costs the collection its completion record. This does NOT prove
        filesystem durability of the marker write itself (fsync/
        os.replace survival) -- see
        test_atomic_write_json_failure_during_marker_write_leaves_no_
        partial_or_torn_state below for that separate, genuinely
        durability-testing proof."""
        # Simulating a mid-walk crash requires monkeypatching this
        # internal ordering boundary -- there is no way to induce a real
        # process-level crash inside a unit test. This is the SAME
        # established, dual-reviewed pattern already used elsewhere in
        # this suite for crash-safety-ordering proofs (see
        # test_collection_migration_1458.py::
        # TestConsolidateCollectionInPlaceCrashSafetyAndResume::
        # test_crash_before_flip_leaves_old_representation_authoritative,
        # which monkeypatches write_chunks_db_discriminator the same way).
        import code_indexer.storage.shared.collection_migration as mod

        _write_collection_meta(tmp_path)
        _write_vector_json(tmp_path, "cw001111", [1.0, 2.0, 3.0, 4.0], chunk_text="x")

        def _boom(collection_dir: Path) -> None:
            raise RuntimeError("simulated crash during subdirectory cleanup")

        monkeypatch.setattr(mod, "_remove_empty_subdirs", _boom)

        with pytest.raises(RuntimeError, match="simulated crash"):
            consolidate_collection_in_place(tmp_path)

        # The marker landed BEFORE the (now-crashed) subdirectory walk --
        # a genuine deletion had already fully completed (zero legacy
        # vector_*.json files remain) by this point.
        assert next(tmp_path.rglob("vector_*.json"), None) is None
        assert _is_migration_cleanup_completed(tmp_path) is True, (
            "Bug #1584: the completion marker must be durably recorded "
            "BEFORE _remove_empty_subdirs runs, so a crash during that "
            "walk never costs the collection its completion record."
        )

    def test_atomic_write_json_failure_during_marker_write_leaves_no_partial_or_torn_state(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Genuine filesystem-durability proof (distinct from the
        ordering proof above): inject a failure INSIDE
        _atomic_write_json's own write sequence -- at the os.replace
        step, after the temp file has already been fully written and
        fsynced -- and confirm _mark_migration_cleanup_completed leaves
        no partial/torn state: the original collection_meta.json is
        byte-for-byte unchanged and no stray temp file is left behind.
        This is the accepted residual crash window documented at the
        _mark_migration_cleanup_completed() call site in
        collection_migration.py: a crash here can still reproduce the
        original #1584 false positive on the NEXT pass, but it can
        never corrupt collection_meta.json itself."""
        import code_indexer.storage.shared.collection_migration as mod

        _write_collection_meta(tmp_path)
        meta_path = tmp_path / "collection_meta.json"
        original_bytes = meta_path.read_bytes()

        original_replace = mod.os.replace

        def _failing_replace(src, dst, *args, **kwargs):
            if Path(dst) == meta_path:
                raise OSError("simulated crash during os.replace")
            return original_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(mod.os, "replace", _failing_replace)

        with pytest.raises(OSError, match="simulated crash"):
            mod._mark_migration_cleanup_completed(tmp_path)

        assert meta_path.read_bytes() == original_bytes, (
            "a failed marker write must never leave collection_meta.json "
            "partially/torn-written"
        )
        assert list(tmp_path.glob("*.tmp")) == [], (
            "a failed marker write must not leave a stray temp file behind"
        )

    def test_subsequent_call_after_remove_empty_subdirs_crash_survives_post_migration_drift(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Replaces a prior, non-discriminating version of this test
        (Codex confirmed it passed unchanged under BOTH the old and new
        marker-write orderings). Simulates a mid-cleanup crash by
        failing the external os.walk filesystem call
        _remove_empty_subdirs makes (its sole os.walk call site),
        applies ordinary post-migration drift, then makes a SECOND
        call. Under the FIXED ordering the marker survived the crash,
        so this second call takes the Bug #1584 fast path and succeeds
        despite the drift. Under the OLD ordering the marker would have
        been lost by the same crash, forcing manifest re-verification
        against now-stale data -- which unconditionally raises
        UnrecoverableConsolidationCorruptionError, reproducing the
        original #1584 false positive this fix exists to prevent."""
        import code_indexer.storage.shared.collection_migration as mod

        _write_collection_meta(tmp_path)
        _write_vector_json(tmp_path, "cw003333", [1.0, 2.0, 3.0, 4.0], chunk_text="z")

        original_walk = mod.os.walk

        def _boom_walk(top, *args, **kwargs):
            if str(top) == str(tmp_path):
                raise RuntimeError("simulated crash during subdirectory cleanup")
            return original_walk(top, *args, **kwargs)

        monkeypatch.setattr(mod.os, "walk", _boom_walk)
        with pytest.raises(RuntimeError, match="simulated crash"):
            consolidate_collection_in_place(tmp_path)
        monkeypatch.undo()

        assert _is_migration_cleanup_completed(tmp_path) is True

        _simulate_post_migration_reindex(tmp_path)

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "already_consolidated", (
            "Bug #1584: the marker must have survived the "
            "_remove_empty_subdirs crash, so this second call takes "
            "the fast path and tolerates ordinary post-migration drift "
            "instead of raising over a now-stale manifest."
        )
        assert next(tmp_path.rglob("vector_*.json"), None) is None


class TestContentManifestRetiredOnSelfHeal:
    """Bug #1584 dual-review finding (MEDIUM, Codex finding 3): the
    frozen chunks_db_content_manifest.json was never removed after its
    documented 'retirement' -- pure storage accumulation at the
    ~900-repo fleet scale (a single fleet repo's manifest can run tens
    of megabytes, per Bug #1562's own real numbers). Once the completion
    marker is durably (re-)confirmed, the manifest has discharged its
    only purpose and is removed."""

    def test_manifest_removed_once_a_later_call_confirms_completion(
        self, tmp_path: Path
    ) -> None:
        _migrate_fresh_collection(tmp_path, "rt001111", [1.0, 2.0, 3.0, 4.0], "x")
        manifest_path = tmp_path / "chunks_db_content_manifest.json"
        assert manifest_path.exists(), (
            "fixture assumption: the manifest survives the FIRST call "
            "that completes migration+cleanup"
        )

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "already_consolidated"
        assert not manifest_path.exists(), (
            "Bug #1584: once a later call re-confirms cleanup completion "
            "via the self-heal branch, the now-retired content manifest "
            "must be removed rather than accumulating on disk forever."
        )
