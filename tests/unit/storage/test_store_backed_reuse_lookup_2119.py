"""Story #2119: the embedding-reuse lookup reads the committed rows of
``chunks.db`` (never ``path_index.bin``), opens it read-only, and lets every
error propagate. Real SQLite, real ``FilesystemVectorStore`` writer."""

from __future__ import annotations

import contextlib
import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List

import numpy as np
import pytest

from code_indexer.storage import sqlite_chunk_store
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.sqlite_chunk_store import ChunkStore, CorruptChunkDataError

VECTOR_DIM = 16
COLLECTION = "voyage-code-3"
PROJECT = "example-repo"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _point_id(file_hash: str, index: int) -> str:
    return hashlib.md5(f"{PROJECT}_{file_hash}_{index}".encode()).hexdigest()


def _points(path: str, file_hash: str, texts: List[str]) -> List[Dict[str, Any]]:
    rng = np.random.default_rng(len(texts))
    return [
        {
            "id": _point_id(file_hash, i),
            "vector": rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist(),
            "payload": {
                "path": path,
                "chunk_index": i,
                "total_chunks": len(texts),
                "content_hash": _sha256(text),
                "content": text,
                "file_hash": file_hash,
            },
        }
        for i, text in enumerate(texts)
    ]


def _completed_collection(base: Path, chunks_db: bool = True) -> None:
    """A collection a PRIOR run completed (discriminator committed)."""
    store = FilesystemVectorStore(
        base_path=base, use_chunks_db_for_new_collections=chunks_db
    )
    store.create_collection(COLLECTION, vector_size=VECTOR_DIM)
    store.begin_indexing(COLLECTION)
    store.upsert_points(COLLECTION, _points("src/base.py", "sha256:base", ["b0"]))
    store.end_indexing(COLLECTION)


def _lookup(store: FilesystemVectorStore, path: str, file_hash: str, n: int):
    return store.get_existing_content_hashes(
        path, COLLECTION, project_id=PROJECT, file_hash=file_hash, chunk_count=n
    )


# ---------------------------------------------------------------------------
# ChunkStore.read_many
# ---------------------------------------------------------------------------


def _record(point_id: str, path: str) -> Dict[str, Any]:
    return {"id": point_id, "vector": [0.5] * VECTOR_DIM, "payload": {"path": path}}


def test_read_many_returns_rows_by_id_and_by_path_once(tmp_path: Path) -> None:
    with ChunkStore(tmp_path / "chunks.db") as store:
        store.write_batch(
            [_record("a", "x.py"), _record("b", "y.py"), _record("c", "y.py")]
        )
        records = store.read_many(["a", "b", "missing"], also_path="y.py")
    assert sorted(r["id"] for r in records) == ["a", "b", "c"]
    assert all(len(r["vector"]) == VECTOR_DIM for r in records)


def test_read_many_batches_more_ids_than_one_statement_allows(tmp_path: Path) -> None:
    ids = [f"p{i:04d}" for i in range(1203)]
    with ChunkStore(tmp_path / "chunks.db") as store:
        store.write_batch([_record(i, "big.py") for i in ids])
        records = store.read_many(ids)
    assert sorted(r["id"] for r in records) == ids


def test_read_many_returns_a_row_matched_in_two_batches_once(tmp_path: Path) -> None:
    """The path clause rides on the first statement; a row it returns may be
    asked for again by id in a later batch -- it is still returned once."""
    ids = [f"p{i:04d}" for i in range(501)]
    with ChunkStore(tmp_path / "chunks.db") as store:
        store.write_batch([_record(i, "big.py") for i in ids])
        records = store.read_many(ids, also_path="big.py")
    assert sorted(r["id"] for r in records) == ids


def test_read_many_path_only_and_nothing_requested(tmp_path: Path) -> None:
    with ChunkStore(tmp_path / "chunks.db") as store:
        store.write_batch([_record("a", "x.py")])
        assert [r["id"] for r in store.read_many([], also_path="x.py")] == ["a"]
        assert store.read_many([]) == []


def test_read_many_on_a_store_without_chunks_table_is_empty(tmp_path: Path) -> None:
    db = tmp_path / "chunks.db"
    sqlite3.connect(str(db)).close()  # an empty SQLite file, no schema
    with ChunkStore(db, read_only=True) as store:
        assert store.read_many(["a"], also_path="x.py") == []


def test_read_many_raises_on_a_corrupt_blob(tmp_path: Path) -> None:
    db = tmp_path / "chunks.db"
    with ChunkStore(db) as store:
        store.write_batch([_record("a", "x.py")])
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE chunks SET data = ?", (b"garbage",))
    conn.commit()
    conn.close()
    with ChunkStore(db, read_only=True) as store:
        with pytest.raises(CorruptChunkDataError):
            store.read_many(["a"])


# ---------------------------------------------------------------------------
# FilesystemVectorStore.get_existing_content_hashes (CHUNKS_DB)
# ---------------------------------------------------------------------------


def test_finds_rows_an_interrupted_session_committed(tmp_path: Path) -> None:
    """A killed run commits rows to chunks.db but never persists
    path_index.bin (written only at end_indexing). The next process must
    still find them -- by content-addressed point id."""
    _completed_collection(tmp_path)
    killed = FilesystemVectorStore(base_path=tmp_path)
    killed.begin_indexing(COLLECTION)
    texts = ["n0", "n1", "n2"]
    killed.upsert_points(COLLECTION, _points("src/new.py", "sha256:new", texts))
    # no end_indexing(): the process was killed here

    rerun = FilesystemVectorStore(base_path=tmp_path)
    result = _lookup(rerun, "src/new.py", "sha256:new", 3)

    assert set(result) == {_sha256(t) for t in texts}
    assert result[_sha256("n1")]["point_id"] == _point_id("sha256:new", 1)
    assert len(result[_sha256("n0")]["vector"]) == VECTOR_DIM


def test_duplicate_content_at_another_path_is_found(tmp_path: Path) -> None:
    _completed_collection(tmp_path)
    store = FilesystemVectorStore(base_path=tmp_path)
    result = _lookup(store, "src/copy_of_base.py", "sha256:base", 1)
    assert set(result) == {_sha256("b0")}


def test_changed_file_reuses_same_path_rows(tmp_path: Path) -> None:
    """A changed file has a new file hash (new ids, none stored); its
    unchanged chunks are found through its path."""
    _completed_collection(tmp_path)
    store = FilesystemVectorStore(base_path=tmp_path)
    result = _lookup(store, "src/base.py", "sha256:base-edited", 2)
    assert result == {
        _sha256("b0"): {
            "vector": result[_sha256("b0")]["vector"],
            "point_id": _point_id("sha256:base", 0),
        }
    }


def test_same_path_chunk_is_found_at_another_index(tmp_path: Path) -> None:
    """Codex P2-1: candidates are keyed by content, so stored chunks that
    will sit at other chunk indexes (a chunk was prepended) are all found."""
    _completed_collection(tmp_path)
    writer = FilesystemVectorStore(base_path=tmp_path)
    writer.begin_indexing(COLLECTION)
    writer.upsert_points(COLLECTION, _points("src/m.py", "sha256:m1", ["A", "B"]))
    writer.end_indexing(COLLECTION)

    # The new version is [X, A, B]: a new file hash, A and B moved up one.
    result = _lookup(
        FilesystemVectorStore(base_path=tmp_path), "src/m.py", "sha256:m2", 3
    )

    assert set(result) == {_sha256("A"), _sha256("B")}


def test_own_content_row_wins_over_a_same_path_row(tmp_path: Path) -> None:
    _completed_collection(tmp_path)
    writer = FilesystemVectorStore(base_path=tmp_path)
    writer.begin_indexing(COLLECTION)
    # The SAME chunk text is stored both under this file's new content id
    # (written for a copy) and under src/base.py's old content id.
    writer.upsert_points(COLLECTION, _points("src/copy.py", "sha256:v2", ["b0"]))
    writer.end_indexing(COLLECTION)

    result = _lookup(
        FilesystemVectorStore(base_path=tmp_path), "src/base.py", "sha256:v2", 1
    )
    assert result[_sha256("b0")]["point_id"] == _point_id("sha256:v2", 0)


def test_rows_without_content_hash_are_not_candidates(tmp_path: Path) -> None:
    _completed_collection(tmp_path)
    writer = FilesystemVectorStore(base_path=tmp_path)
    writer.begin_indexing(COLLECTION)
    legacy = _points("src/legacy.py", "sha256:legacy", ["l0"])
    del legacy[0]["payload"]["content_hash"]
    writer.upsert_points(COLLECTION, legacy)
    writer.end_indexing(COLLECTION)
    assert (
        _lookup(
            FilesystemVectorStore(base_path=tmp_path),
            "src/legacy.py",
            "sha256:legacy",
            1,
        )
        == {}
    )


def test_absent_store_holds_nothing_and_is_not_created(tmp_path: Path) -> None:
    store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=True
    )
    store.create_collection(COLLECTION, vector_size=VECTOR_DIM)
    chunks_db = tmp_path / COLLECTION / "chunks.db"
    assert not chunks_db.exists()
    assert _lookup(store, "src/new.py", "sha256:new", 2) == {}
    assert not chunks_db.exists(), "the lookup must never create the store"


def test_missing_store_of_an_established_collection_is_fatal(tmp_path: Path) -> None:
    """Codex P2-3: once the CHUNKS_DB discriminator is committed, a missing
    chunks.db is its own fatal kind (Story #2126: missing store, exit 89,
    never the corruption code) -- not an empty cache -- and is not
    recreated."""
    from code_indexer.services.index_failure_exit_codes import (
        EXIT_CODE_CHUNK_STORE_MISSING,
        index_failure_exit_code,
    )
    from code_indexer.storage.sqlite_chunk_store import ChunkStoreUnavailableError

    _completed_collection(tmp_path)
    chunks_db = tmp_path / COLLECTION / "chunks.db"
    chunks_db.unlink()

    with pytest.raises(ChunkStoreUnavailableError) as raised:
        _lookup(FilesystemVectorStore(base_path=tmp_path), "src/base.py", "sha256:x", 1)

    assert index_failure_exit_code(raised.value) == EXIT_CODE_CHUNK_STORE_MISSING
    assert not chunks_db.exists()


def test_read_write_open_never_recreates_an_established_store(
    tmp_path: Path,
) -> None:
    """Codex round 2, P2-A: the central guard at the read-write open point."""
    from code_indexer.services.index_failure_exit_codes import (
        EXIT_CODE_CHUNK_STORE_MISSING,
        index_failure_exit_code,
    )
    from code_indexer.storage.sqlite_chunk_store import ChunkStoreUnavailableError

    _completed_collection(tmp_path)
    chunks_db = tmp_path / COLLECTION / "chunks.db"
    chunks_db.unlink()

    with pytest.raises(ChunkStoreUnavailableError) as raised:
        ChunkStore(chunks_db)
    assert index_failure_exit_code(raised.value) == EXIT_CODE_CHUNK_STORE_MISSING
    assert not chunks_db.exists()

    fresh = tmp_path / "fresh-collection" / "chunks.db"
    fresh.parent.mkdir()
    with ChunkStore(fresh):
        pass
    assert fresh.exists(), "a collection that is not established may create it"


def test_clear_collection_recreates_a_missing_established_store(
    tmp_path: Path,
) -> None:
    """An explicit clear is a legitimate creator: it clears the layout
    discriminator before creating the store and re-commits it after."""
    _completed_collection(tmp_path)
    chunks_db = tmp_path / COLLECTION / "chunks.db"
    chunks_db.unlink()

    assert FilesystemVectorStore(base_path=tmp_path).clear_collection(COLLECTION)

    assert chunks_db.exists()
    with ChunkStore(chunks_db, read_only=True) as store:
        assert store.count() == 0


@contextlib.contextmanager
def _exclusive_lock_held(db: Path, seconds: float) -> Iterator[None]:
    """A real writer connection holds an EXCLUSIVE lock on ``db`` (the writer
    uses a rollback journal: readers are blocked while it is held) for
    ``seconds`` in a thread; the body runs once the lock is held and the
    thread is joined on exit."""
    held = threading.Event()

    def writer() -> None:
        conn = sqlite3.connect(str(db), isolation_level=None)
        try:
            conn.execute("BEGIN EXCLUSIVE")
            held.set()
            time.sleep(seconds)
            conn.execute("COMMIT")
        finally:
            conn.close()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        assert held.wait(10), "the writer never obtained its lock"
        yield
    finally:
        thread.join(seconds + 30)


def test_lookup_waits_out_a_transient_write_lock(tmp_path: Path) -> None:
    """Codex round 2, P2-B: concurrent writers of the same run hold the
    store's lock while they commit. A lock held longer than SQLite's default
    5 s busy wait must not turn the lookup into a fatal error."""
    _completed_collection(tmp_path)
    with _exclusive_lock_held(tmp_path / COLLECTION / "chunks.db", 7.0):
        result = _lookup(
            FilesystemVectorStore(base_path=tmp_path), "src/base.py", "sha256:base", 1
        )
    assert set(result) == {_sha256("b0")}


class _RecordingProvider:
    """Provider boundary of the vector manager: records every batch."""

    def __init__(self) -> None:
        self.batches: List[List[str]] = []
        self.embedding_provider = type(
            "ProviderStandIn",
            (),
            {
                "_get_model_token_limit": lambda self: 32000,
                "get_current_model": lambda self: COLLECTION,
            },
        )()

    def submit_batch_task(self, texts: List[str], metadata: Dict[str, Any]) -> Any:
        self.batches.append(list(texts))
        raise AssertionError("the provider must not be called")


def test_lock_held_beyond_the_bound_is_fatal_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex round 2, P2-B: once the bounded wait is exhausted the lookup is
    still fatal (exit 87) and nothing reaches the provider."""
    from unittest.mock import Mock

    from code_indexer.services.clean_slot_tracker import CleanSlotTracker
    from code_indexer.services.file_chunking_manager import FileChunkingManager
    from code_indexer.services.index_failure_exit_codes import (
        EXIT_CODE_CHUNK_STORE_ENVIRONMENT,
        index_failure_exit_code,
    )
    from code_indexer.storage import filesystem_vector_store
    from code_indexer.storage.sqlite_chunk_store import ChunkStoreUnavailableError

    index_dir = tmp_path / "index"
    _completed_collection(index_dir)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    source = repo / "src" / "base.py"
    source.write_text("b0\n")
    chunker = Mock()
    chunker.chunk_file.return_value = [
        {
            "text": "b0",
            "chunk_index": 0,
            "total_chunks": 1,
            "file_extension": "py",
            "line_start": 1,
            "line_end": 1,
        }
    ]
    provider = _RecordingProvider()
    monkeypatch.setattr(filesystem_vector_store, "REUSE_LOOKUP_LOCK_WAIT_SECONDS", 0.5)

    with _exclusive_lock_held(index_dir / COLLECTION / "chunks.db", 3.0):
        with FileChunkingManager(
            vector_manager=provider,  # type: ignore[arg-type]
            chunker=chunker,
            vector_store_client=FilesystemVectorStore(base_path=index_dir),
            thread_count=1,
            slot_tracker=CleanSlotTracker(max_slots=3),
            codebase_dir=repo,
        ) as manager:
            future = manager.submit_file_for_processing(
                source,
                {
                    "project_id": PROJECT,
                    "file_hash": "sha256:base",
                    "collection_name": COLLECTION,
                    "git_available": False,
                },
                None,
            )
            with pytest.raises(ChunkStoreUnavailableError) as raised:
                future.result(timeout=30)

    assert index_failure_exit_code(raised.value) == EXIT_CODE_CHUNK_STORE_ENVIRONMENT
    assert provider.batches == []


def test_lookup_opens_the_store_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never a second writer connection: every ChunkStore the lookup opens
    is read-only (mode=ro)."""
    _completed_collection(tmp_path)
    opened: List[Dict[str, bool]] = []
    real_init = sqlite_chunk_store.ChunkStore.__init__

    def recording_init(self, db_path, **kwargs):
        opened.append(
            {
                "read_only": kwargs.get("read_only", False),
                "immutable": kwargs.get("immutable", False),
            }
        )
        real_init(self, db_path, **kwargs)

    store = FilesystemVectorStore(base_path=tmp_path)
    monkeypatch.setattr(sqlite_chunk_store.ChunkStore, "__init__", recording_init)
    assert _lookup(store, "src/base.py", "sha256:base", 1)
    assert opened == [{"read_only": True, "immutable": False}]


def test_corrupt_blob_propagates_instead_of_reading_as_a_miss(tmp_path: Path) -> None:
    _completed_collection(tmp_path)
    conn = sqlite3.connect(str(tmp_path / COLLECTION / "chunks.db"))
    conn.execute("UPDATE chunks SET data = ?", (b"garbage",))
    conn.commit()
    conn.close()
    with pytest.raises(CorruptChunkDataError):
        _lookup(
            FilesystemVectorStore(base_path=tmp_path), "src/base.py", "sha256:base", 1
        )


def test_unreadable_store_propagates(tmp_path: Path) -> None:
    _completed_collection(tmp_path)
    journal = tmp_path / COLLECTION / "chunks.db-journal"
    journal.mkdir()  # SQLite cannot open its journal: a real SQLITE_IOERR
    try:
        with pytest.raises(sqlite3.Error):
            _lookup(
                FilesystemVectorStore(base_path=tmp_path),
                "src/base.py",
                "sha256:base",
                1,
            )
    finally:
        journal.rmdir()


def test_sharded_json_keeps_the_path_index_lookup(tmp_path: Path) -> None:
    _completed_collection(tmp_path, chunks_db=False)
    store = FilesystemVectorStore(base_path=tmp_path)
    result = _lookup(store, "src/base.py", "sha256:ignored-for-sharded-json", 1)
    assert set(result) == {_sha256("b0")}
