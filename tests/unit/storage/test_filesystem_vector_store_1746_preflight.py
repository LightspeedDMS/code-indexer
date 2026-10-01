"""Bug #1746 Change 4: preflight the target chunk store's writability
BEFORE the file-submission loop begins.

Root cause (production incident, GitHub issue #1746): a `chunks.db`
placeholder file that cannot be opened for write (root-owned, permission
denied, disk full, corrupt) was only ever discovered when the FIRST file's
vector-storage write actually attempted the open -- by then chunking and
embedding work had already happened. Change 4 converges both defects at one
control point: verify write-access to chunks.db before any file is
chunked/embedded at all.

Real filesystem I/O via FilesystemVectorStore + tmp_path throughout -- no
mocking (chmod 000 is the real OS mechanism the issue's own reproduction
recipe uses).
"""

import os
import sqlite3

import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.sqlite_chunk_store import ChunkStoreUnavailableError

VECTOR_DIM = 8

_RUNNING_AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


class TestPreflightChunkStoreWritableFailsClosed:
    """AC: Given a target collection's chunks.db exists but cannot be
    opened for write by the running process, when an indexing/activation
    run starts, then the run fails within a bounded short time (no files
    chunked/embedded) with a clear error identifying the collection path
    and the underlying OS error."""

    @pytest.mark.skipif(
        _RUNNING_AS_ROOT,
        reason="chmod 000 does not deny a root/DAC-override process",
    )
    def test_unwritable_chunks_db_raises_chunk_store_unavailable_error(
        self, tmp_path
    ) -> None:
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=VECTOR_DIM)

        collection_path = tmp_path / "coll"
        chunks_db_path = collection_path / "chunks.db"
        chunks_db_path.touch()
        os.chmod(chunks_db_path, 0o000)

        try:
            with pytest.raises(ChunkStoreUnavailableError) as exc_info:
                store.preflight_chunk_store_writable("coll")

            message = str(exc_info.value)
            # AC: "clear error identifying the collection path and the
            # underlying OS error". sqlite3's real OperationalError text
            # for a chmod-000 file is "unable to open database file" --
            # not the literal words "permission denied" (sqlite3 does not
            # surface the raw OSError text) -- so that is the real
            # underlying-error signal to check for here.
            assert str(chunks_db_path) in message
            assert "unable to open database file" in message.lower()
        finally:
            os.chmod(chunks_db_path, 0o644)


class TestPreflightChunkStoreWritableHealthyPathUnchanged:
    """AC (regression): Given chunks.db does not exist yet (normal
    first-time indexing) or is writable, when a run starts, then behavior
    is unchanged -- no new failure mode is introduced for the healthy
    path."""

    def test_missing_chunks_db_is_a_no_op(self, tmp_path) -> None:
        """Normal first-time indexing: chunks.db doesn't exist yet."""
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=VECTOR_DIM)

        assert not (tmp_path / "coll" / "chunks.db").exists()

        # Must not raise.
        store.preflight_chunk_store_writable("coll")

    def test_writable_chunks_db_is_a_no_op(self, tmp_path) -> None:
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=VECTOR_DIM)
        store.begin_indexing("coll")
        store.upsert_points(
            "coll",
            [
                {
                    "id": "p1",
                    "vector": [0.1] * VECTOR_DIM,
                    "payload": {"path": "src/a.py", "type": "content"},
                }
            ],
        )

        assert (tmp_path / "coll" / "chunks.db").exists()

        # Must not raise -- the store is genuinely writable.
        store.preflight_chunk_store_writable("coll")

    def test_sharded_json_collection_is_a_no_op(self, tmp_path) -> None:
        """A collection using the legacy SHARDED_JSON layout has no
        chunks.db at all -- the preflight must not fabricate a failure for
        a layout it doesn't apply to."""
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=False
        )
        store.create_collection("coll", vector_size=VECTOR_DIM)

        assert not (tmp_path / "coll" / "chunks.db").exists()

        # Must not raise.
        store.preflight_chunk_store_writable("coll")


class TestPreflightTransientLockContentionDoesNotAbort:
    """B3 (code review finding): the preflight's own try/except used to
    raise ChunkStoreUnavailableError on ANY exception from
    open_chunk_store_for_path() -- a SECOND classification site that
    bypassed the is_fatal_chunk_store_write_error() classifier H1 already
    built. A real lock held by a separate process/connection (expected
    under concurrent CHUNKS_DB writers) is purely transient -- the
    preflight must NOT abort the whole run on it, exactly like the
    per-file write path (H1) already doesn't."""

    def test_real_exclusive_lock_from_separate_connection_does_not_abort(
        self, tmp_path
    ) -> None:
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=VECTOR_DIM)
        store.begin_indexing("coll")
        store.upsert_points(
            "coll",
            [
                {
                    "id": "p1",
                    "vector": [0.1] * VECTOR_DIM,
                    "payload": {"path": "src/a.py", "type": "content"},
                }
            ],
        )

        chunks_db_path = tmp_path / "coll" / "chunks.db"
        assert chunks_db_path.exists()

        # Hold a REAL exclusive lock from a separate connection -- exactly
        # the transient contention shape H1 already excludes on the
        # per-file write path.
        lock_conn = sqlite3.connect(str(chunks_db_path))
        lock_conn.execute("BEGIN EXCLUSIVE")
        try:
            # Must NOT raise -- purely transient lock contention.
            store.preflight_chunk_store_writable("coll")
        finally:
            lock_conn.execute("ROLLBACK")
            lock_conn.close()
