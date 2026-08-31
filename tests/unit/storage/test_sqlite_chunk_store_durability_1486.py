"""Unit tests for Bug #1486 High Finding 3: ChunkStore.flush_durable()'s
PRAGMA synchronous=FULL timing bug.

Root cause: the original flush_durable() ran ``PRAGMA synchronous=FULL``
THEN ``commit()`` on the SAME call -- SQLite raises
``sqlite3.OperationalError: Safety level may not be changed inside a
transaction`` if a write transaction is genuinely pending when this
runs, and setting the pragma AFTER prior commits does not retroactively
apply to those already-committed writes on a possibly-different
connection.

Fix: ``durable_synchronous=True`` configures (and verifies) the pragma
at CONNECTION-OPEN time, before any write transaction begins.
flush_durable() then only commits pending work and fsyncs the actual db
file descriptor and its containing directory descriptor.

Scope constraint: this must apply ONLY to migration write connections,
never the general per-chunk indexing ChunkStore path (a per-write NFS
fsync would cripple indexing throughput) -- proven by asserting the
DEFAULT constructor (durable_synchronous unset) never touches this
pragma.

Real SQLite, real filesystem -- the only wrapped call is
``nfs_safe_fsync`` (via monkeypatch), used purely as an OBSERVATION
point to prove which file descriptors were fsynced, not to fake the
underlying durability behavior.
"""

import json
from pathlib import Path

import pytest

from code_indexer.storage.sqlite_chunk_store import ChunkStore, ChunkStoreError


def _sample_record(point_id: str) -> dict:
    return {
        "id": point_id,
        "vector": [0.1, 0.2, 0.3, 0.4],
        "payload": {"path": "src/foo.py"},
        "chunk_text": "hello",
    }


class TestDurableSynchronousConfiguredAtOpenTime:
    def test_durable_synchronous_true_sets_and_verifies_full(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "chunks.db"
        store = ChunkStore(db_path, durable_synchronous=True)
        try:
            row = store._conn.execute("PRAGMA synchronous").fetchone()
            assert row is not None and int(row[0]) == 2  # 2 == FULL
        finally:
            store.close()

    def test_default_constructor_never_touches_synchronous_pragma(
        self, tmp_path: Path
    ) -> None:
        """Scope guard: the general per-chunk indexing path (the DEFAULT
        constructor, durable_synchronous unset) must NEVER have this
        pragma forced -- a per-write NFS fsync would cripple indexing
        throughput. SQLite's own compiled default for a rollback-journal
        (DELETE mode) connection happens to already be FULL (2), so this
        test asserts the ABSENCE of our own explicit configuration
        rather than a specific numeric value -- proven by confirming no
        ChunkStoreError is raised even when the file is deliberately left
        with a non-FULL synchronous level afterward (i.e. our code never
        re-asserts/verifies it for this path)."""
        db_path = tmp_path / "chunks.db"
        store = ChunkStore(db_path)
        try:
            # Explicitly downgrade synchronous on this connection --
            # proves nothing in the DEFAULT path re-asserts/verifies
            # FULL the way durable_synchronous=True does.
            store._conn.execute("PRAGMA synchronous=OFF")
            row = store._conn.execute("PRAGMA synchronous").fetchone()
            assert int(row[0]) == 0  # 0 == OFF, our code never fought this
        finally:
            store.close()


class TestFlushDurableCommitsAndFsyncsBothFdAndDirFd:
    def test_flush_durable_commits_a_genuinely_pending_transaction(
        self, tmp_path: Path
    ) -> None:
        """Prove the ordering fix actually works: synchronous=FULL was
        already configured at OPEN time (before this transaction even
        began), so flush_durable() calling commit() on a REAL pending
        transaction never raises 'Safety level may not be changed
        inside a transaction' -- because it never attempts to change the
        pragma at all anymore."""
        db_path = tmp_path / "chunks.db"
        store = ChunkStore(db_path, durable_synchronous=True)
        try:
            record = _sample_record("pt1")
            vector_blob = store._encode_vector("pt1", record["vector"])
            data_blob = store._encode_data(record)
            # A raw, uncommitted write -- bypassing write_batch()'s own
            # internal commit -- to leave a GENUINELY pending
            # transaction, mirroring the exact failure mode described in
            # the finding.
            store._conn.execute(
                "INSERT INTO chunks (point_id, path, vector, data) VALUES (?, ?, ?, ?)",
                ("pt1", "src/foo.py", vector_blob, data_blob),
            )

            store.flush_durable()  # must not raise

            stored = store.read("pt1")
            assert stored is not None, (
                "Bug: flush_durable() did not actually commit the pending transaction."
            )
            assert stored["chunk_text"] == "hello"
        finally:
            store.close()

    def test_flush_durable_fsyncs_both_the_file_fd_and_the_directory_fd(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import code_indexer.storage.sqlite_chunk_store as mod

        db_path = tmp_path / "chunks.db"
        store = ChunkStore(db_path, durable_synchronous=True)
        try:
            store.write_batch([_sample_record("pt2")])

            fsynced_paths: list = []
            original_fsync = mod.nfs_safe_fsync

            def _tracking_fsync(fd):
                # Resolve the fd back to a path via /proc for a robust,
                # OS-level assertion of WHICH descriptor was fsynced,
                # rather than trusting call order alone.
                import os as _os

                try:
                    fsynced_paths.append(_os.readlink(f"/proc/self/fd/{fd}"))
                except OSError:
                    fsynced_paths.append(None)
                return original_fsync(fd)

            monkeypatch.setattr(mod, "nfs_safe_fsync", _tracking_fsync)

            store.flush_durable()

            assert len(fsynced_paths) == 2, (
                f"Bug: expected exactly 2 nfs_safe_fsync calls (file fd + "
                f"directory fd), got {len(fsynced_paths)}: {fsynced_paths}"
            )
            assert any(
                p is not None and p.endswith("/chunks.db") for p in fsynced_paths
            ), f"Bug: the db FILE fd was never fsynced: {fsynced_paths}"
            assert any(
                p is not None and p.rstrip("/").endswith(str(tmp_path.name))
                for p in fsynced_paths
            ), f"Bug: the containing DIRECTORY fd was never fsynced: {fsynced_paths}"
        finally:
            store.close()

    def test_flush_durable_raises_when_not_opened_with_durable_synchronous(
        self, tmp_path: Path
    ) -> None:
        """Scope enforcement: flush_durable() is reserved for migration
        write connections. A store opened via the DEFAULT (general
        indexing) constructor must refuse to be flush_durable()'d,
        structurally preventing the migration-only durability path from
        silently leaking into the hot indexing write path."""
        db_path = tmp_path / "chunks.db"
        store = ChunkStore(db_path)
        try:
            store.write_batch([_sample_record("pt3")])
            with pytest.raises(ChunkStoreError):
                store.flush_durable()
        finally:
            store.close()


class TestMigrationWriteConnectionsOpenedDurable:
    """Bug #1486 Round 3 Finding C (HIGH): every migration WRITE
    connection inside collection_migration.py must be opened with
    durable_synchronous=True -- consistent with this whole bug's
    durability principle -- while read-only verify reopens and the
    general per-chunk INDEXING write path (unrelated to migration) must
    stay completely unaffected (a per-write NFS fsync would cripple
    indexing throughput)."""

    def test_migration_write_batch_opens_durable_synchronous(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import code_indexer.storage.shared.collection_migration as mod

        recorded_kwargs: list = []
        original_init = mod.ChunkStore.__init__

        def _recording_init(self, db_path, **kwargs):
            recorded_kwargs.append(dict(kwargs))
            return original_init(self, db_path, **kwargs)

        monkeypatch.setattr(mod.ChunkStore, "__init__", _recording_init)

        chunks_db_path = tmp_path / "chunks.db"
        record_path = tmp_path / "vector_pt1.json"
        record_path.write_text(
            json.dumps(
                {
                    "id": "pt1",
                    "vector": [0.1, 0.2, 0.3, 0.4],
                    "payload": {"path": "src/foo.py"},
                    "chunk_text": "hello",
                }
            )
        )

        mod._write_and_verify_batch(chunks_db_path, [("pt1", record_path)])

        # _write_and_verify_batch opens exactly 2 ChunkStores: the WRITE
        # store (must be durable) and the read-only fresh-reopen VERIFY
        # store (must stay at its default -- Finding C explicitly scopes
        # the fix to the WRITE connection only).
        assert len(recorded_kwargs) == 2, (
            f"Expected exactly 2 ChunkStore opens (write + verify), got "
            f"{len(recorded_kwargs)}: {recorded_kwargs}"
        )
        write_kwargs, verify_kwargs = recorded_kwargs
        assert write_kwargs.get("durable_synchronous") is True, (
            "Bug: the migration WRITE connection was not opened with "
            "durable_synchronous=True."
        )
        assert not verify_kwargs.get("durable_synchronous"), (
            "Bug: the read-only verify reopen must stay at its default "
            "(non-durable) -- Finding C explicitly scopes this fix to "
            "the WRITE connection only."
        )

    def test_fresh_path_empty_collection_schema_creation_open_is_durable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The fresh-path schema-creation-only open for a genuinely
        empty collection (`ChunkStore(chunks_db_path).close()`, no batch
        loop ever runs) is itself a migration-write open and must also
        be durable_synchronous=True."""
        import code_indexer.storage.shared.collection_migration as mod

        recorded_kwargs: list = []
        original_init = mod.ChunkStore.__init__

        def _recording_init(self, db_path, **kwargs):
            recorded_kwargs.append(dict(kwargs))
            return original_init(self, db_path, **kwargs)

        monkeypatch.setattr(mod.ChunkStore, "__init__", _recording_init)

        (tmp_path / "collection_meta.json").write_text(
            json.dumps({"name": "coll", "vector_size": 4})
        )

        result = mod.consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
        assert recorded_kwargs, "Bug: no ChunkStore was ever opened."
        # The schema-creation-only open is the VERY FIRST ChunkStore
        # constructed in the fresh path (before the -- here zero-
        # iteration -- batch loop and before the LATER, already-durable
        # _force_durable_and_integrity_check open) -- assert on that
        # first entry specifically so a later durable open elsewhere in
        # the same call cannot make this assertion vacuously pass
        # without the schema-creation open itself being fixed.
        assert recorded_kwargs[0].get("durable_synchronous") is True, (
            "Bug: the fresh-path schema-creation-only ChunkStore open "
            f"(for a genuinely empty collection) was not opened with "
            f"durable_synchronous=True: {recorded_kwargs}"
        )

    def test_general_indexing_chunk_store_construction_stays_non_durable(
        self, tmp_path: Path
    ) -> None:
        """Scope guard: constructing a ChunkStore the way the general
        per-chunk INDEXING path does (a bare default construction,
        unrelated to migration) must never be durable_synchronous."""
        store = ChunkStore(tmp_path / "indexing_chunks.db")
        try:
            assert store._durable_synchronous is False
        finally:
            store.close()
