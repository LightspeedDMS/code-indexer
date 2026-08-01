"""Run-boundary durability flush + integrity gate for ordinary golden-repo
refresh (Bug #1506).

Ordinary refresh (`RefreshScheduler._execute_refresh` -> `_index_source`)
writes `chunks.db` in place against the live `master_path` with zero
integrity gating before publish. This test module proves the new gate
module (`code_indexer.global_repos.refresh_integrity_gate`):

1. Discovers only CHUNKS_DB-layout collections under an index dir (a
   SHARDED_JSON collection has no chunks.db to check -- must skip cleanly,
   never error).
2. Detects GENUINE SQLite corruption via flush_durable() + a fresh-connection
   PRAGMA integrity_check -- using a REAL chunks.db file that is REALLY
   corrupted (a targeted byte-flip at the file's midpoint, the exact
   technique Bug #1486's own test suite empirically confirmed produces a
   database that still OPENS and even answers simple queries, but fails
   integrity_check). No sqlite3 mocking anywhere in this file.
3. Self-heals a corrupt chunks.db by reflink-restoring it from the
   corresponding collection in a "last known good" snapshot directory, using
   the SAME `cp --reflink=auto` CoW primitive (`LocalCloneBackend`) already
   used throughout this codebase -- proven via a REAL restore (no mocking of
   the clone backend), verified by reading the restored file back with a
   real ChunkStore.
"""

from __future__ import annotations

import json
from pathlib import Path

from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
    write_chunks_db_discriminator,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore

from code_indexer.global_repos.refresh_integrity_gate import (
    discover_chunks_db_collection_dirs,
    flush_and_check_chunks_db_integrity,
    restore_chunks_db_via_reflink,
    run_refresh_integrity_gate,
)


def _write_collection_meta(collection_dir: Path) -> None:
    collection_dir.mkdir(parents=True, exist_ok=True)
    (collection_dir / "collection_meta.json").write_text(
        json.dumps({"name": collection_dir.name, "vector_size": 4})
    )


def _make_chunks_db_collection(collection_dir: Path, records: list) -> Path:
    """Build a real CHUNKS_DB-layout collection directory: collection_meta.json
    + a real chunks.db (written via the real ChunkStore write path, not
    hand-crafted bytes) + the durable discriminator flip. Returns the
    chunks.db path."""
    _write_collection_meta(collection_dir)
    chunks_db_path = collection_dir / "chunks.db"
    with ChunkStore(chunks_db_path) as store:
        store.write_batch(records)
    write_chunks_db_discriminator(collection_dir)
    assert resolve_chunk_layout(collection_dir) == ChunkLayout.CHUNKS_DB
    return chunks_db_path


def _make_sharded_json_collection(collection_dir: Path) -> None:
    """A legacy SHARDED_JSON collection: collection_meta.json with no
    chunks_db discriminator, and no chunks.db file at all."""
    _write_collection_meta(collection_dir)
    assert resolve_chunk_layout(collection_dir) == ChunkLayout.SHARDED_JSON


def _real_records(count: int) -> list:
    """Enough real records to span multiple SQLite pages -- required so a
    targeted mid-file byte-flip corrupts an INNER data page while leaving
    the header/schema openable (Bug #1486's own empirically-confirmed
    corruption technique)."""
    records = []
    for i in range(count):
        records.append(
            {
                "id": f"point-{i:04d}",
                "vector": [float(i), float(i + 1), float(i + 2), float(i + 3)],
                "payload": {"path": f"src/file_{i}.py"},
                "chunk_text": f"def function_{i}(): pass  " + ("x" * 200),
            }
        )
    return records


def _flip_bytes_at_midpoint(path: Path, span: int = 200) -> None:
    """Corrupt bytes at the file's midpoint -- empirically confirmed (Bug
    #1486's test suite) to produce a database that still OPENS fine, while
    PRAGMA integrity_check genuinely detects the corruption."""
    size = path.stat().st_size
    with open(path, "r+b") as f:
        f.seek(size // 2)
        data = f.read(span)
        f.seek(size // 2)
        f.write(bytes(b ^ 0xFF for b in data))


class TestDiscoverChunksDbCollectionDirs:
    def test_finds_only_chunks_db_layout_collections(self, tmp_path: Path) -> None:
        index_dir = tmp_path / "index"
        index_dir.mkdir()

        chunks_db_coll = index_dir / "semantic_coll"
        _make_chunks_db_collection(chunks_db_coll, _real_records(3))

        sharded_coll = index_dir / "legacy_coll"
        _make_sharded_json_collection(sharded_coll)

        result = discover_chunks_db_collection_dirs(index_dir)

        assert result == [chunks_db_coll]

    def test_missing_index_dir_returns_empty_list_not_error(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "does-not-exist"
        assert discover_chunks_db_collection_dirs(missing) == []

    def test_index_dir_with_no_collections_returns_empty_list(
        self, tmp_path: Path
    ) -> None:
        index_dir = tmp_path / "index"
        index_dir.mkdir()
        assert discover_chunks_db_collection_dirs(index_dir) == []


class TestFlushAndCheckChunksDbIntegrity:
    def test_healthy_db_passes(self, tmp_path: Path) -> None:
        collection_dir = tmp_path / "coll"
        chunks_db_path = _make_chunks_db_collection(collection_dir, _real_records(20))

        ok, detail = flush_and_check_chunks_db_integrity(chunks_db_path)

        assert ok is True
        assert detail == "ok"

    def test_missing_file_fails_cleanly(self, tmp_path: Path) -> None:
        ok, detail = flush_and_check_chunks_db_integrity(tmp_path / "chunks.db")

        assert ok is False
        assert "does not exist" in detail

    def test_genuinely_corrupted_db_is_detected(self, tmp_path: Path) -> None:
        """RED-then-GREEN discriminating case: a real chunks.db, corrupted
        via a targeted mid-file byte-flip, must fail this gate. This is
        NOT the trivial "file is not a database" case (which any check
        would catch) -- the corrupted file still opens fine; only a real
        PRAGMA integrity_check against a fresh connection detects it."""
        collection_dir = tmp_path / "coll"
        chunks_db_path = _make_chunks_db_collection(collection_dir, _real_records(200))

        _flip_bytes_at_midpoint(chunks_db_path)

        # Sanity: confirm the corruption technique produced a genuinely
        # openable-but-corrupt file (not a hard-broken file that any naive
        # check would trivially catch).
        import sqlite3

        conn = sqlite3.connect(str(chunks_db_path))
        try:
            row_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        finally:
            conn.close()
        assert row_count is not None, (
            "Fixture invariant violated: corrupted file must still be openable "
            "and answer simple queries -- this is the exact 'subtly corrupt "
            "but openable' scenario the gate exists to catch."
        )

        ok, detail = flush_and_check_chunks_db_integrity(chunks_db_path)

        assert ok is False
        assert detail != "ok"


class TestRestoreChunksDbViaReflink:
    def test_restores_corrupt_file_from_healthy_snapshot(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.shared.clone_backend import (
            LocalCloneBackend,
        )

        healthy_collection = tmp_path / "healthy" / "coll"
        healthy_chunks_db = _make_chunks_db_collection(
            healthy_collection, _real_records(10)
        )

        corrupt_collection = tmp_path / "corrupt" / "coll"
        corrupt_chunks_db = _make_chunks_db_collection(
            corrupt_collection, _real_records(10)
        )
        # Overwrite with garbage to simulate the corrupted master_path file.
        corrupt_chunks_db.write_bytes(b"not a valid sqlite database at all")

        clone_backend = LocalCloneBackend()
        restore_chunks_db_via_reflink(
            clone_backend, healthy_chunks_db, corrupt_chunks_db
        )

        # Verify via a REAL ChunkStore read -- the restored file is a valid,
        # readable chunks.db with the healthy snapshot's data.
        with ChunkStore(corrupt_chunks_db) as store:
            assert store.count() == 10
            record = store.read("point-0000")
        assert record["chunk_text"].startswith("def function_0")

    def test_raises_without_a_clone_backend(self, tmp_path: Path) -> None:
        import pytest

        with pytest.raises(RuntimeError):
            restore_chunks_db_via_reflink(
                None, tmp_path / "healthy.db", tmp_path / "corrupt.db"
            )


class TestRunRefreshIntegrityGate:
    def test_skips_cleanly_when_no_chunks_db_collections(self, tmp_path: Path) -> None:
        source_index_dir = tmp_path / "source" / "index"
        _make_sharded_json_collection(source_index_dir / "legacy_coll")

        result = run_refresh_integrity_gate(
            source_index_dir=source_index_dir,
            healthy_index_dir=None,
            clone_backend=None,
        )

        assert result.passed is True
        assert result.checked_collections == []
        assert result.failures == []

    def test_passes_when_all_chunks_db_collections_healthy(
        self, tmp_path: Path
    ) -> None:
        source_index_dir = tmp_path / "source" / "index"
        _make_chunks_db_collection(source_index_dir / "coll", _real_records(15))

        result = run_refresh_integrity_gate(
            source_index_dir=source_index_dir,
            healthy_index_dir=None,
            clone_backend=None,
        )

        assert result.passed is True
        assert len(result.checked_collections) == 1

    def test_fails_and_self_heals_from_healthy_snapshot(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.shared.clone_backend import (
            LocalCloneBackend,
        )

        healthy_index_dir = tmp_path / "healthy_snapshot" / ".code-indexer" / "index"
        _make_chunks_db_collection(healthy_index_dir / "coll", _real_records(50))

        source_index_dir = tmp_path / "master" / ".code-indexer" / "index"
        source_chunks_db = _make_chunks_db_collection(
            source_index_dir / "coll", _real_records(50)
        )
        _flip_bytes_at_midpoint(source_chunks_db)

        result = run_refresh_integrity_gate(
            source_index_dir=source_index_dir,
            healthy_index_dir=healthy_index_dir,
            clone_backend=LocalCloneBackend(),
        )

        assert result.passed is False
        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure.self_heal_attempted is True
        assert failure.self_heal_succeeded is True

        # master_path's chunks.db must now be genuinely readable again.
        with ChunkStore(source_chunks_db) as store:
            assert store.count() == 50

    def test_fails_without_self_heal_when_no_healthy_snapshot_available(
        self, tmp_path: Path
    ) -> None:
        """First-ever refresh case: no prior snapshot exists to restore
        from. The gate must still fail (refuse to publish) but must not
        attempt/claim a self-heal that cannot happen."""
        source_index_dir = tmp_path / "master" / ".code-indexer" / "index"
        source_chunks_db = _make_chunks_db_collection(
            source_index_dir / "coll", _real_records(50)
        )
        _flip_bytes_at_midpoint(source_chunks_db)

        result = run_refresh_integrity_gate(
            source_index_dir=source_index_dir,
            healthy_index_dir=None,
            clone_backend=None,
        )

        assert result.passed is False
        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure.self_heal_attempted is False
        assert failure.self_heal_succeeded is False

    def test_refuses_to_self_heal_when_healthy_snapshot_itself_is_corrupt(
        self, tmp_path: Path
    ) -> None:
        """Codex review Finding 3: the self-heal source must itself be
        integrity-checked BEFORE being trusted. If the "healthy" snapshot
        is ALSO corrupt, the gate must refuse to restore from it (never
        confidently copy corruption over master_path) and must surface
        this as a more severe condition than an ordinary single-sided
        failure."""
        from code_indexer.server.storage.shared.clone_backend import (
            LocalCloneBackend,
        )

        healthy_index_dir = tmp_path / "healthy_snapshot" / ".code-indexer" / "index"
        healthy_chunks_db = _make_chunks_db_collection(
            healthy_index_dir / "coll", _real_records(50)
        )
        _flip_bytes_at_midpoint(healthy_chunks_db)

        source_index_dir = tmp_path / "master" / ".code-indexer" / "index"
        source_chunks_db = _make_chunks_db_collection(
            source_index_dir / "coll", _real_records(50)
        )
        _flip_bytes_at_midpoint(source_chunks_db)
        corrupt_bytes_before = source_chunks_db.read_bytes()

        result = run_refresh_integrity_gate(
            source_index_dir=source_index_dir,
            healthy_index_dir=healthy_index_dir,
            clone_backend=LocalCloneBackend(),
        )

        assert result.passed is False
        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure.self_heal_attempted is False
        assert failure.self_heal_succeeded is False
        assert failure.source_snapshot_also_corrupt is True

        # Never restored -- master_path's corrupt bytes must be untouched.
        assert source_chunks_db.read_bytes() == corrupt_bytes_before

    def test_self_heal_restore_re_verified_post_restore_failure_is_reported(
        self, tmp_path: Path
    ) -> None:
        """Codex review Finding 3: even after the reflink clone call
        completes without raising, the RESTORED destination must be
        re-verified via a fresh integrity_check before self-heal is
        reported as successful. A restore whose destination fails this
        post-check must be reported as a self-heal FAILURE, never a
        success."""
        import shutil

        class _CorruptingCloneBackend:
            """Test double simulating a reflink clone that completes
            without raising but produces a corrupt destination (e.g. a
            racing write, or corruption introduced during the copy
            itself)."""

            def create_clone_at_path(self, source: str, dest: str) -> None:
                shutil.copyfile(source, dest)
                _flip_bytes_at_midpoint(Path(dest))

        healthy_index_dir = tmp_path / "healthy_snapshot" / ".code-indexer" / "index"
        _make_chunks_db_collection(healthy_index_dir / "coll", _real_records(50))

        source_index_dir = tmp_path / "master" / ".code-indexer" / "index"
        source_chunks_db = _make_chunks_db_collection(
            source_index_dir / "coll", _real_records(50)
        )
        _flip_bytes_at_midpoint(source_chunks_db)

        result = run_refresh_integrity_gate(
            source_index_dir=source_index_dir,
            healthy_index_dir=healthy_index_dir,
            clone_backend=_CorruptingCloneBackend(),
        )

        assert result.passed is False
        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure.self_heal_attempted is True
        assert failure.self_heal_succeeded is False
        assert failure.self_heal_error is not None
        assert "post-restore" in failure.self_heal_error.lower()


class TestMetadataRestoreAfterSelfHeal:
    """Bug #1509: a self-healed chunks.db (restored from the last-known-good
    snapshot) must NOT be left paired with a sibling metadata-{provider}.json
    that still claims the NEW commit was fully indexed -- _index_source()
    already wrote that stale claim to master_path's .code-indexer/ dir
    earlier in the SAME refresh cycle, before the integrity gate ever runs.
    Without also restoring metadata from the same healthy snapshot, the
    next scheduled refresh's git-ref-only has_changes() check is permanently
    fooled into believing chunks.db is already caught up."""

    def test_self_heal_also_restores_metadata_json_files(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.shared.clone_backend import (
            LocalCloneBackend,
        )

        # Healthy last-known-good snapshot: chunks.db reflects the OLD
        # commit, and its sibling metadata file (one level up from
        # .code-indexer/index/) records that same OLD commit.
        healthy_root = tmp_path / "healthy_snapshot" / ".code-indexer"
        healthy_index_dir = healthy_root / "index"
        _make_chunks_db_collection(healthy_index_dir / "coll", _real_records(50))
        healthy_metadata = healthy_root / "metadata-voyage-ai.json"
        healthy_metadata.write_text(
            json.dumps({"status": "completed", "current_commit": "old_sha"})
        )

        # master_path (source): chunks.db is corrupt, but _index_source()
        # already wrote a metadata file claiming the NEW commit is fully
        # indexed -- this is the pre-existing drift the gate must fix up.
        source_root = tmp_path / "master" / ".code-indexer"
        source_index_dir = source_root / "index"
        source_chunks_db = _make_chunks_db_collection(
            source_index_dir / "coll", _real_records(50)
        )
        _flip_bytes_at_midpoint(source_chunks_db)
        source_metadata = source_root / "metadata-voyage-ai.json"
        source_metadata.write_text(
            json.dumps({"status": "completed", "current_commit": "new_sha"})
        )

        result = run_refresh_integrity_gate(
            source_index_dir=source_index_dir,
            healthy_index_dir=healthy_index_dir,
            clone_backend=LocalCloneBackend(),
        )

        assert result.passed is False
        assert len(result.failures) == 1
        assert result.failures[0].self_heal_succeeded is True

        # The chunks.db self-heal succeeded (already covered by other
        # tests) -- the NEW assertion for Bug #1509 is that the sibling
        # metadata file was ALSO restored from the healthy snapshot, so
        # master_path's metadata now honestly reflects what chunks.db
        # actually contains (the OLD, restored commit) rather than the
        # stale "new commit fully indexed" claim.
        restored_metadata = json.loads(source_metadata.read_text())
        assert restored_metadata["current_commit"] == "old_sha"
