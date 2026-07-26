"""Transactional, durable, fail-closed stray-point deletion (Story #1457 AC7).

Temporal reconciliation's `reconcile_shard` currently deletes stray
`vector_*.json` FILES sequentially, then fsyncs each touched directory
afterward (file-level, not a single atomic transaction). AC7 requires the
SQLite-rewritten reconciliation to: (a) delete ALL stray point rows for ONE
partial commit inside a SINGLE SQLite transaction; (b) ROLL BACK the whole
transaction on ANY failure (no partial deletion left committed); (c) use an
explicitly DURABLE/synchronous commit mode (`PRAGMA synchronous=FULL`, not a
deferred/relaxed commit); (d) never swallow the failure -- callers translate
it into their own fail-closed error type.

`ChunkStore.delete_stray_points_fail_closed` is the new, additive method
implementing this contract. Real SQLite -- no mocking of ChunkStore's own
logic, and no mocking of `sqlite3.Connection` (verified impossible: it is a
read-only C extension type -- attribute assignment and `unittest.mock.patch`
both raise on it). Fault injection instead uses two genuine OS/SQLite-level
mechanisms: a real read-only file permission (`os.chmod`) to force a
"before commit" DELETE-statement failure, and a real SQLite lock held open
by a second connection (a SHARED lock blocks only the EXCLUSIVE upgrade
commit needs, not the RESERVED lock a DELETE needs) to force a "during
commit" failure specifically.
"""

from __future__ import annotations

import os
import stat
import sqlite3
from pathlib import Path

import pytest

from code_indexer.storage.sqlite_chunk_store import ChunkStore


def _record(point_id: str) -> dict:
    return {"id": point_id, "vector": [0.1, 0.2, 0.3], "payload": {}}


def test_deletes_all_given_points_in_one_transaction(tmp_path: Path) -> None:
    store = ChunkStore(tmp_path / "chunks.db")
    try:
        store.write_batch([_record("keep-1"), _record("stray-1"), _record("stray-2")])

        deleted = store.delete_stray_points_fail_closed(["stray-1", "stray-2"])

        assert deleted == 2
        assert store.read("stray-1") is None
        assert store.read("stray-2") is None
        assert store.read("keep-1") is not None  # untouched point survives
    finally:
        store.close()


def test_failure_before_commit_rolls_back_and_raises(tmp_path: Path) -> None:
    """A genuine OS-level failure during the DELETE statement itself
    (before commit is ever reached) must propagate and leave no partial
    deletion committed. Simulated via a REAL read-only file permission
    (never a mock of sqlite3.Connection, which cannot be monkeypatched --
    it is a read-only C extension type) -- a fresh connection opened
    against an already-read-only file genuinely fails to write."""
    db_path = tmp_path / "chunks.db"
    store = ChunkStore(db_path)
    store.write_batch([_record("keep-1"), _record("stray-1")])
    store.close()

    os.chmod(db_path, stat.S_IRUSR)
    try:
        readonly_store = ChunkStore(db_path)
        try:
            with pytest.raises(sqlite3.OperationalError):
                readonly_store.delete_stray_points_fail_closed(["stray-1"])
        finally:
            readonly_store.close()
    finally:
        os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)

    verify_store = ChunkStore(db_path)
    try:
        assert verify_store.read("stray-1") is not None  # rolled back
        assert verify_store.read("keep-1") is not None
    finally:
        verify_store.close()


def test_failure_during_commit_rolls_back_and_raises(tmp_path: Path) -> None:
    """A genuine SQLite lock-contention failure occurring specifically AT
    commit time (not during the DELETE statement) must propagate and leave
    no partial deletion committed. Real SQLite locking, not a mock: a
    second connection holds an open read transaction (SHARED lock), which
    does not block this connection's DELETE (only needs RESERVED) but DOES
    block the EXCLUSIVE lock upgrade required at commit -- reliably
    isolating a "during commit" failure from a "during delete" one."""
    db_path = tmp_path / "chunks.db"
    store = ChunkStore(db_path)
    store.write_batch([_record("keep-1"), _record("stray-1")])

    blocker_conn = sqlite3.connect(str(db_path))
    blocker_conn.execute("BEGIN")
    blocker_conn.execute("SELECT * FROM chunks")  # acquire + hold SHARED lock
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.delete_stray_points_fail_closed(["stray-1"])
    finally:
        blocker_conn.rollback()
        blocker_conn.close()
        store.close()

    verify_store = ChunkStore(db_path)
    try:
        assert verify_store.read("stray-1") is not None  # rolled back
        assert verify_store.read("keep-1") is not None
    finally:
        verify_store.close()
