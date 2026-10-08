"""A fresh chunks.db gets its whole schema in ONE transaction.

Each autocommitted DDL statement is its own durable transaction (three
fdatasync calls each under ``synchronous=FULL``); creating the schema one
statement at a time cost fifteen syncs per fresh store, which stalls for
seconds when the disk is busy.  The schema is now written in a single
``BEGIN IMMEDIATE`` transaction for a fresh store, while opening an existing
store still takes no write lock.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator, List

import pytest

from code_indexer.storage import sqlite_chunk_store
from code_indexer.storage.sqlite_chunk_store import ChunkStore

_WRITES = ("CREATE", "ALTER")


@pytest.fixture
def traced(monkeypatch: pytest.MonkeyPatch) -> Iterator[List[str]]:
    """Record every SQL statement the chunk store's connections run."""
    statements: List[str] = []
    real_connect = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn: sqlite3.Connection = real_connect(*args, **kwargs)
        conn.set_trace_callback(lambda sql: statements.append(sql.strip().upper()))
        return conn

    monkeypatch.setattr(sqlite_chunk_store.sqlite3, "connect", connect)
    yield statements


def _columns(db_path: Path) -> set:
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
    finally:
        conn.close()


@pytest.mark.parametrize("durable", [False, True])
def test_fresh_store_writes_its_schema_in_one_transaction(
    tmp_path: Path, traced: List[str], durable: bool
) -> None:
    ChunkStore(tmp_path / "chunks.db", durable_synchronous=durable).close()

    begins = [i for i, sql in enumerate(traced) if sql.startswith("BEGIN")]
    commits = [i for i, sql in enumerate(traced) if sql.startswith("COMMIT")]
    writes = [i for i, sql in enumerate(traced) if sql.startswith(_WRITES)]
    assert len(begins) == 1 and len(commits) == 1, traced
    assert traced[begins[0]].startswith("BEGIN IMMEDIATE"), traced
    assert writes, traced
    assert all(begins[0] < i < commits[0] for i in writes), traced
    assert "type" in _columns(tmp_path / "chunks.db")


def test_existing_store_open_takes_no_write_lock(
    tmp_path: Path, traced: List[str]
) -> None:
    ChunkStore(tmp_path / "chunks.db").close()
    traced.clear()

    ChunkStore(tmp_path / "chunks.db").close()

    assert not [sql for sql in traced if sql.startswith("BEGIN")], traced


def test_two_racing_creators_both_get_the_full_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "chunks.db"
    start = threading.Barrier(2)
    errors: List[BaseException] = []

    def create() -> None:
        try:
            start.wait(timeout=10)
            ChunkStore(db_path).close()
        except BaseException as exc:  # recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert errors == []
    assert {"point_id", "path", "vector", "data", "type"} <= _columns(db_path)
