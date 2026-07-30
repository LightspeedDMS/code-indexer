"""Regression tests for two Codex-15 findings in sqlite_chunk_store.py.

Finding 1 (LOW) -- ``_open_connection()`` leaks the connection if the
post-``sqlite3.connect()`` PRAGMA raises. The round-32 post-open guard in
``ChunkStore.__init__`` wraps ``_ensure_schema()`` / ``_configure_durable_
synchronous()`` / ``_load_persisted_dim()``, but the ``PRAGMA
journal_mode=DELETE`` INSIDE ``_open_connection()`` itself runs BEFORE that
guard is even reached (``self._conn = self._open_connection()`` is the line
that assigns into the guarded block). If that PRAGMA raises after
``sqlite3.connect()`` succeeded, the already-opened connection is leaked.

Finding 2 (LOW) -- ``read()`` decodes the stored zstd/JSON ``data`` blob and
the float32 ``vector`` blob with no contextual error handling: corrupt bytes
escape as a raw ``zstandard.ZstdError`` / ``json.JSONDecodeError`` / numpy
``ValueError`` naming neither the point_id nor the failing field, so callers
(scroll / get_point) cannot translate it into a data-integrity error. A
genuinely-missing row (``read()`` -> ``None``) is a separate, unchanged
not-found contract and must NOT be treated as corruption.

Both RED faults are GENUINE (a real WAL lock; a real corrupt blob written
directly through sqlite) -- never a mock/monkeypatch of ChunkStore's own
methods. ``sqlite3.connect`` is only wrapped to OBSERVE which connection
object was opened; the real, unmodified ``_open_connection`` runs end-to-end.
"""

import json
import sqlite3

import pytest
import zstandard

from code_indexer.storage.sqlite_chunk_store import ChunkStore, ChunkStoreError


# ---------------------------------------------------------------------------
# Finding 1: _open_connection PRAGMA leak
# ---------------------------------------------------------------------------


def _make_wal_db_with_held_read_lock(db_path):
    """Create a real WAL-mode SQLite file and return an open connection that
    holds a read transaction on it.

    While that read lock is held, a FRESH connection can still be opened
    (``sqlite3.connect`` succeeds fine), but ``PRAGMA journal_mode=DELETE``
    on the fresh connection genuinely raises ``sqlite3.OperationalError:
    database is locked`` -- switching a WAL database out of WAL mode requires
    an exclusive lock that the held read transaction blocks. This isolates
    the fault to the post-``connect()`` PRAGMA statement inside
    ``_open_connection``, exactly the leak this fix targets, never the
    ``connect()`` step itself.

    The caller is responsible for closing the returned lock connection.
    """
    setup = sqlite3.connect(str(db_path))
    try:
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("CREATE TABLE probe(x)")
        setup.execute("INSERT INTO probe VALUES (1)")
        setup.commit()
    finally:
        setup.close()

    lock_conn = sqlite3.connect(str(db_path))
    lock_conn.execute("BEGIN")
    lock_conn.execute("SELECT * FROM probe").fetchall()  # acquire shared lock
    return lock_conn


class TestOpenConnectionClosesOnPragmaFailure:
    def test_open_connection_closes_connection_when_pragma_genuinely_fails(
        self, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "chunks.db"
        lock_conn = _make_wal_db_with_held_read_lock(db_path)

        captured = {}
        real_connect = sqlite3.connect

        def tracking_connect(*args, **kwargs):
            # Delegates to the REAL sqlite3.connect -- observes the connection
            # object ChunkStore opens, never fakes or alters any behavior.
            conn = real_connect(*args, **kwargs)
            captured["conn"] = conn
            return conn

        monkeypatch.setattr(sqlite3, "connect", tracking_connect)

        try:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                ChunkStore(db_path)

            assert "conn" in captured, "ChunkStore never opened a connection"
            leaked_conn = captured["conn"]

            # A closed sqlite3.Connection raises ProgrammingError on any use --
            # the observable proof the connection was actually closed by
            # _open_connection before the PRAGMA failure propagated.
            with pytest.raises(sqlite3.ProgrammingError):
                leaked_conn.execute("SELECT 1")
        finally:
            lock_conn.close()

    def test_open_connection_success_path_unaffected(self, tmp_path):
        """Regression guard: the fix must not change normal construction --
        no exception, connection stays open and usable."""
        db_path = tmp_path / "chunks.db"
        store = ChunkStore(db_path)
        try:
            store._conn.execute("SELECT 1")
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Finding 2: corrupt CHUNKS_DB opaque data escapes as raw exceptions
# ---------------------------------------------------------------------------


def _write_one_point(db_path):
    store = ChunkStore(db_path)
    try:
        store.write_batch(
            [
                {
                    "id": "p1",
                    "vector": [0.1, 0.2, 0.3],
                    "payload": {"path": "a.py"},
                    "chunk_text": "hello",
                }
            ]
        )
    finally:
        store.close()


def _corrupt_column(db_path, column, value, point_id="p1"):
    raw = sqlite3.connect(str(db_path))
    try:
        raw.execute(
            f"UPDATE chunks SET {column} = ? WHERE point_id = ?",
            (value, point_id),
        )
        raw.commit()
    finally:
        raw.close()


class TestReadCorruptBlobRaisesContextualError:
    def test_read_corrupt_data_blob_raises_contextual_chunkstore_error(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        _write_one_point(db_path)

        # Corrupt the opaque zstd/JSON data blob directly via sqlite -- a real
        # on-disk corruption, not a mock. Currently this escapes read() as a
        # raw zstandard.ZstdError.
        _corrupt_column(db_path, "data", b"not-a-valid-zstd-frame")

        store = ChunkStore(db_path)
        try:
            with pytest.raises(ChunkStoreError) as exc_info:
                store.read("p1")
            message = str(exc_info.value)
            assert "p1" in message, "corruption error must name the offending point_id"
            assert "data" in message, (
                "corruption error must name the failing 'data' field"
            )
            # Must NOT leak the raw underlying library exception type.
            assert not isinstance(exc_info.value, zstandard.ZstdError)
            assert not isinstance(exc_info.value, json.JSONDecodeError)
        finally:
            store.close()

    def test_read_corrupt_vector_blob_raises_contextual_chunkstore_error(
        self, tmp_path
    ):
        db_path = tmp_path / "chunks.db"
        _write_one_point(db_path)

        # A float32 vector blob whose byte length is not a multiple of 4 --
        # numpy.frombuffer genuinely raises ValueError. Leave the data blob
        # intact so we prove the VECTOR field specifically is named.
        _corrupt_column(db_path, "vector", b"abc")  # 3 bytes, not a multiple of 4

        store = ChunkStore(db_path)
        try:
            with pytest.raises(ChunkStoreError) as exc_info:
                store.read("p1")
            message = str(exc_info.value)
            assert "p1" in message
            assert "vector" in message
        finally:
            store.close()


class TestReadNotFoundAndSuccessUnaffected:
    def test_read_missing_id_still_returns_none(self, tmp_path):
        """The not-found contract is separate from corruption -- a genuinely
        missing row must still return None, never raise."""
        db_path = tmp_path / "chunks.db"
        _write_one_point(db_path)

        store = ChunkStore(db_path)
        try:
            assert store.read("does-not-exist") is None
        finally:
            store.close()

    def test_read_valid_point_success_path_unaffected(self, tmp_path):
        """Regression guard: an uncorrupted point still decodes byte-identically."""
        db_path = tmp_path / "chunks.db"
        _write_one_point(db_path)

        store = ChunkStore(db_path)
        try:
            record = store.read("p1")
            assert record is not None
            assert record["id"] == "p1"
            assert record["payload"]["path"] == "a.py"
            assert record["chunk_text"] == "hello"
            assert list(record["vector"]) == pytest.approx([0.1, 0.2, 0.3], abs=1e-6)
        finally:
            store.close()
