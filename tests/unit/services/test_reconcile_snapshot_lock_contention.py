"""The reconcile snapshot read survives transient chunks.db lock contention.

A plain ``cidx index`` reconciles the store against disk once when stored
chunks exist but no processed file is on record (Issue #1975). That
reconcile reads every stored content point in one snapshot. A concurrent
writer holding the chunks.db lock longer than sqlite's 5 s busy timeout
must not turn the snapshot into an empty state (which re-embeds every
file and records the store as verified without having read it): the read
is retried a bounded number of times, and contention that outlasts the
bound fails the run loudly.

Real FilesystemVectorStore + real chunks.db + a real second sqlite3
connection holding BEGIN EXCLUSIVE from a background thread -- the same
technique as test_filesystem_vector_store_1829_temporal_lock_contention.py.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator, List, Tuple

import pytest

from code_indexer.config import Config
from code_indexer.services import smart_indexer as smart_indexer_module
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_DIM = 8
COLLECTION = "coll"
# Longer than sqlite3's default 5.0 s busy timeout, so the first snapshot
# read genuinely observes "database is locked".
_LOCK_HOLD_SECONDS = 6.0


def _content_point(rel_path: str) -> dict:
    return {
        "id": f"{rel_path}:0",
        "vector": [0.1] * VECTOR_DIM,
        "payload": {
            "type": "content",
            "path": rel_path,
            "indexed_at": "2026-01-01T00:00:00",
        },
    }


def _indexer_over(store: FilesystemVectorStore, codebase_dir: Path) -> SmartIndexer:
    """A SmartIndexer carrying only what the snapshot read uses."""
    indexer = SmartIndexer.__new__(SmartIndexer)
    indexer.vector_store_client = store
    indexer.config = Config(codebase_dir=codebase_dir)
    return indexer


@pytest.fixture
def seeded_store(tmp_path: Path) -> FilesystemVectorStore:
    store = FilesystemVectorStore(
        base_path=tmp_path / "index", use_chunks_db_for_new_collections=True
    )
    store.create_collection(COLLECTION, vector_size=VECTOR_DIM)
    store.begin_indexing(COLLECTION)
    store.upsert_points(COLLECTION, [_content_point("src/a.py")])
    assert (tmp_path / "index" / COLLECTION / "chunks.db").exists()
    return store


@pytest.fixture
def held_lock(tmp_path: Path) -> Iterator[threading.Thread]:
    """Hold a real EXCLUSIVE lock on chunks.db for _LOCK_HOLD_SECONDS."""
    chunks_db = tmp_path / "index" / COLLECTION / "chunks.db"
    acquired = threading.Event()

    def _hold() -> None:
        conn = sqlite3.connect(str(chunks_db))
        try:
            conn.execute("BEGIN EXCLUSIVE")
            acquired.set()
            time.sleep(_LOCK_HOLD_SECONDS)
        finally:
            try:
                conn.execute("ROLLBACK")
            finally:
                conn.close()

    thread = threading.Thread(target=_hold, daemon=True)
    thread.start()
    assert acquired.wait(timeout=5.0), "lock holder never acquired the lock"
    yield thread
    thread.join(timeout=_LOCK_HOLD_SECONDS + 5.0)


def test_snapshot_retries_through_transient_lock_and_logs_no_error(
    tmp_path: Path,
    seeded_store: FilesystemVectorStore,
    held_lock: threading.Thread,
    caplog: pytest.LogCaptureFixture,
) -> None:
    indexer = _indexer_over(seeded_store, tmp_path)

    with caplog.at_level(logging.DEBUG, logger=smart_indexer_module.__name__):
        snapshot = indexer._get_indexed_files_snapshot(COLLECTION)

    assert snapshot.keys() == {tmp_path / "src/a.py"}, (
        "a transient chunks.db lock must not replace the stored snapshot "
        f"with an empty state; got {snapshot!r}"
    )
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], [r.getMessage() for r in errors]
    retries = _retry_records(caplog)
    assert 1 <= len(retries) <= smart_indexer_module._SNAPSHOT_LOCK_MAX_ATTEMPTS - 1, (
        "the snapshot must have been read again after the lock was observed: "
        f"{[r.getMessage() for r in retries]}"
    )


def _retry_records(caplog: pytest.LogCaptureFixture) -> List[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "retrying" in r.getMessage()
    ]


class _AlwaysFailingScrollStore:
    """Recording stand-in for the store: every scroll raises ``error``."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.scroll_calls = 0

    def scroll_points(self, *args: Any, **kwargs: Any) -> Tuple[list, None]:
        self.scroll_calls += 1
        raise self.error


def test_persistent_lock_stops_at_the_real_attempt_bound(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    max_attempts = smart_indexer_module._SNAPSHOT_LOCK_MAX_ATTEMPTS
    assert max_attempts > 1, "the bound must allow at least one retry"
    store = _AlwaysFailingScrollStore(sqlite3.OperationalError("database is locked"))
    indexer = SmartIndexer.__new__(SmartIndexer)
    indexer.vector_store_client = store  # type: ignore[assignment]
    indexer.config = Config(codebase_dir=tmp_path)

    with caplog.at_level(logging.INFO, logger=smart_indexer_module.__name__):
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            indexer._get_indexed_files_snapshot(COLLECTION)

    assert store.scroll_calls == max_attempts
    assert len(_retry_records(caplog)) == max_attempts - 1


def test_non_lock_error_is_raised_without_a_retry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = _AlwaysFailingScrollStore(sqlite3.OperationalError("disk I/O error"))
    indexer = SmartIndexer.__new__(SmartIndexer)
    indexer.vector_store_client = store  # type: ignore[assignment]
    indexer.config = Config(codebase_dir=tmp_path)

    with caplog.at_level(logging.INFO, logger=smart_indexer_module.__name__):
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            indexer._get_indexed_files_snapshot(COLLECTION)

    assert store.scroll_calls == 1
    assert _retry_records(caplog) == []


def test_snapshot_raises_when_contention_outlasts_the_retry_bound(
    tmp_path: Path,
    seeded_store: FilesystemVectorStore,
    held_lock: threading.Thread,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One attempt: the lock is held past the single 5 s busy timeout.
    monkeypatch.setattr(
        smart_indexer_module, "_SNAPSHOT_LOCK_MAX_ATTEMPTS", 1, raising=False
    )
    indexer = _indexer_over(seeded_store, tmp_path)

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        indexer._get_indexed_files_snapshot(COLLECTION)
