"""Regression test for a Codex-reported connection leak in ChunkStore.__init__.

ChunkStore.__init__ opens the SQLite connection first, then runs post-open
initialization (durable-synchronous configuration, schema creation, persisted
vector-dimension load). If any of those post-open steps raises, the
already-opened connection was previously never closed -- a leaked sqlite3
connection (fd/handle leak; on repeated failures, resource exhaustion).

This test proves the fix using a GENUINE fault (a real SQLite schema
collision), never a mock/monkeypatch of ChunkStore's own methods -- the real,
unmodified ``_ensure_schema()`` is exercised end-to-end and genuinely raises.
"""

import sqlite3

import pytest

from code_indexer.storage.sqlite_chunk_store import ChunkStore


def _make_db_with_conflicting_chunks_view(db_path) -> None:
    """Create a real, valid, openable SQLite file whose "chunks" name is
    already taken by a VIEW instead of a TABLE.

    ChunkStore's real ``_ensure_schema()`` executes::

        CREATE TABLE IF NOT EXISTS chunks (...);      -- no-op: "chunks" exists (as a view)
        CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);  -- FAILS

    The second statement genuinely raises ``sqlite3.OperationalError:
    views may not be indexed`` -- a real SQLite fault, not a simulated one.
    Crucially, this fires strictly AFTER the connection is successfully
    opened (``PRAGMA journal_mode=DELETE`` succeeds fine against this
    valid file), isolating the failure to the post-open init phase this
    fix targets, never the connection-open step itself.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE VIEW chunks AS SELECT 1 as x")
        conn.commit()
    finally:
        conn.close()


class TestInitClosesConnectionOnPostOpenFailure:
    def test_init_closes_connection_when_schema_creation_genuinely_fails(
        self, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "chunks.db"
        _make_db_with_conflicting_chunks_view(db_path)

        captured = {}
        real_connect = sqlite3.connect

        def tracking_connect(*args, **kwargs):
            # Delegates to the REAL sqlite3.connect -- this only observes
            # which connection object ChunkStore opens, it does not fake
            # or alter any behavior.
            conn = real_connect(*args, **kwargs)
            captured["conn"] = conn
            return conn

        monkeypatch.setattr(sqlite3, "connect", tracking_connect)

        with pytest.raises(sqlite3.OperationalError, match="views may not be indexed"):
            ChunkStore(db_path)

        assert "conn" in captured, "ChunkStore never opened a connection"
        leaked_conn = captured["conn"]

        # A closed sqlite3.Connection raises ProgrammingError on any use --
        # the observable proof the connection was actually closed.
        with pytest.raises(sqlite3.ProgrammingError):
            leaked_conn.execute("SELECT 1")

    def test_init_success_path_unaffected(self, tmp_path):
        """Regression guard: the fix must not change normal construction --
        no exception, connection stays open and usable."""
        db_path = tmp_path / "chunks.db"

        store = ChunkStore(db_path)
        try:
            store._conn.execute("SELECT 1")
        finally:
            store.close()
