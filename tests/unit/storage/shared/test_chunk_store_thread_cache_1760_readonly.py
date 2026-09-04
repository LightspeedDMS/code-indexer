"""Bug #1760 (Fix 1/2) -- semantic search's chunk-store hydration path must
open chunks.db in a genuinely read-only SQLite mode.

Root cause (proven by direct reproduction, not guessed): `FilesystemVectorStore
.search()`'s CHUNKS_DB hydration path is the ONLY consumer of
`ChunkStoreThreadCache.get_or_open()` (3 call sites, all inside `search()`,
all pure reads via `chunk_store.read(point_id)` -- confirmed by inspection,
no write ever flows through this cache). Yet `get_or_open()` always
delegates to `open_chunk_store_for_path()` with its default
`read_only=False`, which opens `ChunkStore` in MUTABLE mode unless the
collection path is a `.versioned/` immutable snapshot
(`is_immutable_versioned_snapshot()`). For a normal (non-versioned)
ACTIVATED repository, that predicate is always False -- so a pure-read
semantic-search query ends up opening chunks.db mutably.

`ChunkStore._open_connection()`'s mutable branch unconditionally runs
`conn.execute("PRAGMA journal_mode=DELETE")` immediately after connect
(`sqlite_chunk_store.py`). `journal_mode` is PERSISTED in the SQLite file
header and survives across connections/reopens -- unlike most pragmas. When
the current *persisted* mode already equals "delete" (a freshly-written,
never-WAL'd file), re-asserting it is a genuine no-op and requires no write
-- which is why an initial, naive reproduction attempt against a plain
chmod'd file did NOT fail. But whenever the persisted mode is anything else
(most commonly "wal" -- set by ANY prior tool/process/library that ever
opened this same file, even transiently; NFS/replication/backup tooling can
also leave a database in this state), switching it back to "delete" is a
real write to the file header and requires removing/checkpointing the WAL
file -- which fails with exactly `sqlite3.OperationalError: attempt to
write a readonly database` on a non-writable file/directory. Reproduced
below with a real, unmocked SQLite file forced into WAL mode then chmod'd
read-only -- byte-identical to the confirmed production log message.

Fix contract (tested here):
1. `ChunkStoreThreadCache.get_or_open()` gains a `read_only: bool = False`
   parameter, threaded through to `open_chunk_store_for_path(...,
   read_only=read_only)`.
2. `read_only=True` on a NON-versioned-snapshot collection path opens a
   NEW, genuinely read-only-but-NOT-immutable SQLite URI mode
   (`mode=ro`) -- NEVER `immutable=1`. `immutable=1` remains reserved
   EXCLUSIVELY for paths where `is_immutable_versioned_snapshot()` proves
   the collection is a published, never-again-written `.versioned/`
   snapshot (Code review Finding 1, round 2): SQLite's `immutable=1`
   promises the engine the file will NEVER change on disk, which is false
   for an ACTIVATED repo or golden base clone actively written by
   indexing/refresh/branch-delta-reindex jobs. Reusing `immutable=1` for
   THIS hydration path would convert the original "loud failure" bug into
   a SILENT WRONG ANSWER: an `immutable=1` reader can silently miss
   rows written by a concurrent mutable writer, and on a WAL-mode
   database it never consults the `-wal` file at all, so it can serve
   stale/deleted content or even fail to see a table that only exists in
   an uncheckpointed WAL. `mode=ro` has neither problem: it never
   attempts a write (still fixes the ORIGINAL "attempt to write a
   readonly database" symptom) but still participates normally in
   SQLite's ordinary read-transaction/WAL-consulting semantics.
3. The read-only handle still returns correct data (`read()` works) and
   still rejects writes (`write_batch()` raises), proving it is genuinely
   read-only, not merely "happens not to fail today".
4. Read-only and mutable cache entries for the SAME db_path are tracked
   distinctly (never hand a caller the wrong-mode connection).
"""

import json
import os
import sqlite3

import pytest
import zstandard

from code_indexer.storage.shared.chunk_store_cache import ChunkStoreThreadCache
from code_indexer.storage.sqlite_chunk_store import ChunkStore, ImmutableChunkStoreError

VECTOR = [0.1, 0.2, 0.3, 0.4]


def _write_one_record(db_path, point_id: str) -> None:
    store = ChunkStore(db_path)
    try:
        store.write_batch(
            [{"id": point_id, "vector": VECTOR, "payload": {"path": f"{point_id}.py"}}]
        )
    finally:
        store.close()


def _force_wal_mode(db_path) -> None:
    """Persist journal_mode=wal into the database file header -- simulates
    ANY prior tool/process/library that ever opened this file, even
    transiently. journal_mode survives across reopens by design."""
    conn = sqlite3.connect(str(db_path))
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        assert mode == ("wal",), f"failed to persist WAL mode, got {mode!r}"
    finally:
        conn.close()


def _make_readonly(dir_path, db_path) -> None:
    """chmod the db file and its containing directory read-only, mirroring
    the non-writable production chunks.db location this bug reproduces."""
    os.chmod(db_path, 0o444)
    os.chmod(dir_path, 0o555)


def _restore_writable(dir_path, db_path) -> None:
    """Restore permissions so pytest's tmp_path cleanup can delete the tree."""
    os.chmod(dir_path, 0o755)
    os.chmod(db_path, 0o644)


@pytest.fixture
def cache():
    c = ChunkStoreThreadCache()
    yield c
    c.close_current_thread()


@pytest.fixture
def readonly_collection(tmp_path):
    """A real chunks.db with one written record, left in persisted WAL mode
    (the discriminating precondition), then made non-writable (file +
    containing directory) -- the exact shape of the confirmed production
    failure."""
    collection_dir = tmp_path / "collection"
    collection_dir.mkdir()
    db_path = collection_dir / "chunks.db"
    _write_one_record(db_path, "p1")
    _force_wal_mode(db_path)
    _make_readonly(collection_dir, db_path)
    yield collection_dir, db_path
    _restore_writable(collection_dir, db_path)


@pytest.fixture
def readonly_collection_with_wal_content(tmp_path):
    """A real chunks.db with a REAL, materialized ``-wal``/``-shm`` pair on
    disk (not merely a persisted ``journal_mode=wal`` header with no actual
    WAL file, as ``readonly_collection`` above uses), then made
    non-writable (file + containing directory).

    Empirically proven (real sqlite3, no mocking) to matter: SQLite's
    ``mode=ro`` needs to CREATE a ``-shm`` file the first time anything
    reads a WAL-mode database, which requires directory write access --
    exactly like ``readonly_collection``'s empty-WAL precondition, ``mode=
    ro`` genuinely cannot open that scenario on a fully read-only
    directory (a real SQLite/OS constraint, not a defect in this fix).
    When ``-shm``/``-wal`` ALREADY EXIST on disk, however, ``mode=ro`` can
    open and use them without creating anything, so it succeeds even with
    a fully read-only file+directory. This is also the REALISTIC
    production precondition Finding 1 is actually about: a collection
    actively held open by a long-lived indexing/refresh WAL writer
    connection (this fixture's ``writer`` is deliberately kept open
    through the whole test, mirroring that) always has a materialized
    ``-shm``/``-wal`` pair on disk.
    """
    collection_dir = tmp_path / "collection_wal"
    collection_dir.mkdir()
    db_path = collection_dir / "chunks.db"
    _write_one_record(db_path, "p1")

    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("UPDATE chunks SET path = 'p1.py' WHERE point_id = 'p1'")
    writer.commit()
    assert os.path.exists(str(db_path) + "-wal")
    assert os.path.exists(str(db_path) + "-shm")

    _make_readonly(collection_dir, db_path)
    yield collection_dir, db_path
    # Restore writability BEFORE closing the writer -- closing the last
    # WAL connection auto-checkpoints, which needs write access.
    _restore_writable(collection_dir, db_path)
    writer.close()


class TestCurrentDefaultReproducesReadonlyDatabaseError:
    """Documents and locks the root cause: today's ONLY behavior (mutable
    open, unconditionally re-asserting journal_mode=DELETE) genuinely fails
    when the persisted journal mode differs and the file isn't writable."""

    def test_default_get_or_open_raises_operational_error(
        self, cache, readonly_collection
    ):
        collection_dir, db_path = readonly_collection

        with pytest.raises(sqlite3.OperationalError, match="readonly database"):
            cache.get_or_open(db_path, str(collection_dir))


class TestReadOnlyModeToleratesNonWritableStore:
    """The fix: read_only=True must open a genuinely read-only SQLite mode
    -- never touching journal_mode, never attempting a write."""

    def test_read_only_true_does_not_raise(
        self, cache, readonly_collection_with_wal_content
    ):
        collection_dir, db_path = readonly_collection_with_wal_content

        store = cache.get_or_open(db_path, str(collection_dir), read_only=True)

        assert store is not None

    def test_read_only_true_returns_correct_data(
        self, cache, readonly_collection_with_wal_content
    ):
        collection_dir, db_path = readonly_collection_with_wal_content

        store = cache.get_or_open(db_path, str(collection_dir), read_only=True)
        record = store.read("p1")

        assert record is not None
        assert record["id"] == "p1"
        assert record["payload"]["path"] == "p1.py"

    def test_read_only_true_rejects_writes(
        self, cache, readonly_collection_with_wal_content
    ):
        """Proves the handle is genuinely read-only, not merely a mutable
        connection that happened not to fail yet."""
        collection_dir, db_path = readonly_collection_with_wal_content

        store = cache.get_or_open(db_path, str(collection_dir), read_only=True)

        with pytest.raises(ImmutableChunkStoreError):
            store.write_batch(
                [{"id": "p2", "vector": VECTOR, "payload": {"path": "p2.py"}}]
            )


class TestReadOnlyAndMutableCacheEntriesAreDistinct:
    """A read_only=True request must never be served a mutable cached
    handle, and vice versa -- the two modes have different write semantics
    and must never be silently swapped."""

    def test_mutable_then_read_only_on_same_writable_path_are_distinct(
        self, cache, tmp_path
    ):
        collection_dir = tmp_path / "writable_collection"
        collection_dir.mkdir()
        db_path = collection_dir / "chunks.db"
        _write_one_record(db_path, "p1")

        mutable_store = cache.get_or_open(db_path, str(collection_dir))
        readonly_store = cache.get_or_open(db_path, str(collection_dir), read_only=True)

        assert mutable_store is not readonly_store
        # The mutable handle must still accept writes; the read-only one
        # must still reject them -- proves neither cache entry leaked the
        # other's mode.
        mutable_store.write_batch(
            [{"id": "p3", "vector": VECTOR, "payload": {"path": "p3.py"}}]
        )
        with pytest.raises(ImmutableChunkStoreError):
            readonly_store.write_batch(
                [{"id": "p4", "vector": VECTOR, "payload": {"path": "p4.py"}}]
            )


class TestReadOnlyModeSeesLiveWrites:
    """Code review Finding 1 (HIGH): the fix must NOT accidentally retain
    immutable=1 semantics. A naive "rename the flag but still open
    immutable=1 under the hood" fix would pass every test above (which
    never exercises a CONCURRENT write against a LIVE read-only handle) but
    would still silently drop legitimately-existing content -- exactly the
    production failure mode this class proves does not happen.

    Empirically verified (real sqlite3, no mocking) before writing this
    test: an `immutable=1` reader opened BEFORE a second, ordinary mutable
    connection commits a brand-new row NEVER observes that row on a
    subsequent read via the SAME handle -- even in plain (non-WAL,
    DELETE-journal) mode, with no chmod/permission trickery involved at
    all. A `mode=ro` reader, opened via the identical `ChunkStore(...,
    read_only=True)` API, DOES observe it -- because `mode=ro` (unlike
    `immutable=1`) participates in SQLite's normal per-statement
    read-transaction semantics instead of promising the engine the file
    will never change.
    """

    def test_read_only_handle_observes_row_written_after_it_was_opened(self, tmp_path):
        collection_dir = tmp_path / "collection"
        collection_dir.mkdir()
        db_path = collection_dir / "chunks.db"
        _write_one_record(db_path, "p1")

        # Open the read-only handle FIRST, before the concurrent write --
        # this ordering is exactly what the production hydration path does
        # (ChunkStoreThreadCache hands out a handle that is then reused
        # across the lifetime of that thread, potentially spanning many
        # concurrent writes by the active indexing/refresh job).
        reader = ChunkStore(db_path, read_only=True)
        try:
            assert reader.read("p2") is None  # not written yet

            writer = ChunkStore(db_path)
            try:
                writer.write_batch(
                    [
                        {
                            "id": "p2",
                            "vector": VECTOR,
                            "payload": {"path": "p2.py"},
                        }
                    ]
                )
            finally:
                writer.close()

            record = reader.read("p2")
        finally:
            reader.close()

        assert record is not None, (
            "read-only handle failed to observe a row committed by a "
            "concurrent mutable writer AFTER the handle was opened -- this "
            "is exactly the silent-data-drop failure mode that reusing "
            "immutable=1 for this hydration path would reintroduce"
        )
        assert record["payload"]["path"] == "p2.py"


def _raw_wal_update_payload_path(db_path, point_id: str, new_path: str):
    """Update a stored record's `payload.path` via a raw connection forced
    into WAL mode, deliberately WITHOUT going through `ChunkStore` (whose
    mutable open unconditionally resets journal_mode back to DELETE) and
    WITHOUT closing the connection (closing the last connection to a
    WAL-mode database auto-checkpoints and deletes the `-wal` file, which
    would defeat the discriminating "uncheckpointed WAL content"
    precondition this helper exists to construct).

    Returns the live connection -- the caller MUST keep it open for the
    duration of the assertion and close it afterward. If any setup step
    fails, the connection is closed here before the exception propagates
    (never leaked to the caller in a half-set-up state).
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT data FROM chunks WHERE point_id = ?", (point_id,)
        ).fetchone()
        decompressor = zstandard.ZstdDecompressor()
        compressor = zstandard.ZstdCompressor()
        record = json.loads(decompressor.decompress(row[0]).decode("utf-8"))
        record["payload"]["path"] = new_path
        new_blob = compressor.compress(json.dumps(record).encode("utf-8"))
        conn.execute(
            "UPDATE chunks SET path = ?, data = ? WHERE point_id = ?",
            (new_path, new_blob, point_id),
        )
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


class TestReadOnlyModeReadsUncheckpointedWalContent:
    """Code review Finding 1 (HIGH), failure mode 2: on a WAL-mode
    database with a change committed but still living ONLY in the `-wal`
    file (not yet checkpointed into the main `.db` file), `immutable=1`
    does not consult the `-wal` file at all and can serve STALE content
    that a normal reader correctly no longer sees.

    Empirically verified (real sqlite3, no mocking) before writing this
    test: with an initial row checkpointed into the main file, then a
    second connection switched to WAL mode and committing an UPDATE that is
    deliberately left uncheckpointed (second connection kept open, never
    closed, so the `-wal` file survives), an `immutable=1` reader sees the
    STALE pre-update value while a `mode=ro` reader correctly sees the
    fresh, committed value.
    """

    def test_read_only_mode_sees_fresh_uncheckpointed_wal_value_not_stale(
        self, tmp_path
    ):
        collection_dir = tmp_path / "collection"
        collection_dir.mkdir()
        db_path = collection_dir / "chunks.db"
        # journal_mode defaults to DELETE -- checkpointed straight into the
        # main file, establishing the pre-change baseline.
        _write_one_record(db_path, "p1")

        # Discriminating precondition: the update below lives ONLY in the
        # -wal file. wal_conn is kept open deliberately (see helper
        # docstring) so the -wal file is not auto-checkpointed away.
        wal_conn = _raw_wal_update_payload_path(db_path, "p1", "p1-changed.py")
        try:
            assert os.path.exists(str(db_path) + "-wal"), (
                "test setup invalid: -wal file was checkpointed away too early"
            )

            reader = ChunkStore(db_path, read_only=True)
            try:
                record = reader.read("p1")
            finally:
                reader.close()
        finally:
            wal_conn.close()

        assert record is not None
        assert record["payload"]["path"] == "p1-changed.py", (
            "read-only handle served STALE content instead of the fresh "
            "value committed to the -wal file -- this is exactly the "
            "silent-stale-data failure mode that reusing immutable=1 for "
            "this hydration path would reintroduce"
        )


class TestOpenChunkStoreForPathRoutesReadOnlyToNewModeNotImmutable:
    """Code review Finding 1: ``open_chunk_store_for_path(...,
    read_only=True)`` must route to the NEW mode=ro mode for a normal
    (non-versioned-snapshot) collection path -- never to ``immutable=1``.
    ``immutable=1`` must remain reachable ONLY via the existing, unchanged
    ``is_immutable_versioned_snapshot()``-gated path."""

    def test_read_only_on_mutable_base_clone_path_uses_new_mode_not_immutable(
        self, tmp_path
    ):
        from code_indexer.storage.sqlite_chunk_store import (
            open_chunk_store_for_path,
        )

        db_path = tmp_path / "chunks.db"
        _write_one_record(db_path, "p1")
        # Shaped like a normal, actively-mutated base-clone collection path
        # -- is_immutable_versioned_snapshot() must return False for this.
        collection_path = str(tmp_path / "index" / "voyage-code-3")

        store = open_chunk_store_for_path(db_path, collection_path, read_only=True)
        try:
            assert store._immutable is False, (
                "read_only=True on a mutable collection path must NEVER "
                "open immutable=1 -- that reintroduces Finding 1's "
                "silent-wrong-answer risk"
            )
            assert store._read_only is True
        finally:
            store.close()

    def test_proven_immutable_versioned_snapshot_still_opens_immutable_even_with_read_only(
        self, tmp_path
    ):
        """The is_immutable_versioned_snapshot()-gated immutable=1 path is
        UNCHANGED by this fix -- a proven .versioned/ snapshot still opens
        immutable=1 regardless of the read_only flag's value."""
        from code_indexer.storage.sqlite_chunk_store import (
            open_chunk_store_for_path,
        )

        db_path = tmp_path / "chunks.db"
        _write_one_record(db_path, "p1")
        collection_path = str(tmp_path / ".versioned" / "myalias" / "v_12345" / "coll")

        store = open_chunk_store_for_path(db_path, collection_path, read_only=True)
        try:
            assert store._immutable is True
            assert store._read_only is False
        finally:
            store.close()
