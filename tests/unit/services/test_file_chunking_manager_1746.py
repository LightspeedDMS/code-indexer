"""Bug #1746 Change 1: a fatal chunk-store-open/write failure must never be
silently converted into an ordinary per-file failure result.

Root cause (confirmed against production incident, see GitHub issue #1746):
when the target chunks.db is unwritable (root-owned, permission-denied,
disk-full, corrupt), FileChunkingManager._process_file_clean_lifecycle used
to catch the resulting exception and return
FileProcessingResult(success=False, ...) exactly like any ordinary per-file
failure (e.g. a corrupt source file or an embedding-provider error). The
caller (HighThroughputProcessor) then just counted it as `failed_files` and
kept submitting/processing every remaining file in the repo -- burning CPU
for hours in production before anyone noticed.

These tests prove:
  1. A fatal chunk-store failure (PermissionError / sqlite3.OperationalError
     raised by the vector store client's upsert_points) propagates OUT of
     _process_file_clean_lifecycle as ChunkStoreUnavailableError -- NOT as a
     returned FileProcessingResult.
  2. A non-storage per-file failure (e.g. an embedding-provider-style
     RuntimeError) is COMPLETELY UNCHANGED -- still returns
     FileProcessingResult(success=False, ...) exactly as before this fix.
"""

# mypy: ignore-errors
# Duck-typed fakes passed to FileChunkingManager's strictly-typed
# constructor params (vector_manager: VectorCalculationManager, etc.)
# work correctly at runtime but trip mypy's structural check -- matches
# the established convention in the sibling test_file_chunking_manager*.py.

import hashlib
import sqlite3
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock

import pytest

from code_indexer.services.clean_slot_tracker import CleanSlotTracker
from code_indexer.services.file_chunking_manager import FileChunkingManager
from code_indexer.storage.sqlite_chunk_store import ChunkStoreUnavailableError


class _FakeVectorManager:
    """Minimal VectorCalculationManager stand-in: every submitted chunk in
    a batch gets a distinct, deterministic, always-truthy embedding keyed
    by chunk text so different chunk texts never collide."""

    def __init__(self) -> None:
        self.cancellation_event = threading.Event()
        self.embedding_provider = Mock()
        self.embedding_provider.get_current_model.return_value = "voyage-large-2"
        self.embedding_provider._get_model_token_limit.return_value = 120000
        self.batches: List[List[str]] = []

    def submit_batch_task(
        self, chunk_texts: List[str], metadata: Dict[str, Any]
    ) -> "Future[Any]":
        from code_indexer.services.vector_calculation_manager import VectorResult

        self.batches.append(list(chunk_texts))
        future: "Future[Any]" = Future()
        embeddings = tuple(
            tuple(float(b) / 255.0 for b in hashlib.sha256(text.encode()).digest()[:8])
            for text in chunk_texts
        )
        result = VectorResult(
            task_id=f"batch_{len(self.batches)}",
            embeddings=embeddings,
            metadata=metadata.copy(),
            processing_time=0.0,
            error=None,
        )
        future.set_result(result)
        return future


class _RaisingFilesystemClient:
    """vector_store_client stand-in whose upsert_points() always raises a
    caller-supplied exception -- simulates the vector store client's
    upsert_points() hitting a fatal open_chunk_store_for_path() failure
    (Bug #1746 unit test scenario: "force ... the vector store client's
    upsert_points() to raise PermissionError/sqlite3.OperationalError via a
    test double")."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def upsert_points(self, points: List[Dict[str, Any]], collection_name=None) -> bool:
        raise self._exc

    def collection_exists(self, collection_name: str) -> bool:
        return True

    def create_collection(self, collection_name: str, vector_size: int) -> bool:
        return True

    def get_existing_content_hashes(
        self, file_path: str, collection_name: str
    ) -> Dict[int, Dict[str, Any]]:
        return {}


def _make_chunker(num_chunks: int) -> Mock:
    chunker = Mock()
    chunks = []
    for i in range(num_chunks):
        chunks.append(
            {
                "text": f"chunk number {i} unique content payload",
                "chunk_index": i,
                "total_chunks": num_chunks,
                "size": 40,
                "file_path": None,
                "file_extension": "py",
                "line_start": i * 10 + 1,
                "line_end": i * 10 + 10,
            }
        )
    chunker.chunk_file.return_value = chunks
    return chunker


def _process_file(
    tmp_path: Path,
    vector_manager: Any,
    filesystem_client: Any,
    num_chunks: int = 2,
) -> Any:
    tmp_path.mkdir(parents=True, exist_ok=True)
    test_file = tmp_path / "test_target.py"
    test_file.write_text("placeholder content for chunking")

    with FileChunkingManager(
        vector_manager=vector_manager,
        chunker=_make_chunker(num_chunks),
        vector_store_client=filesystem_client,
        thread_count=2,
        slot_tracker=CleanSlotTracker(max_slots=4),
        codebase_dir=tmp_path,
    ) as manager:
        metadata = {
            "project_id": "proj",
            "file_hash": "sha256:abc123",
            "collection_name": "col",
            "git_available": False,
        }
        future = manager.submit_file_for_processing(test_file, metadata, None)
        return future.result(timeout=10.0)


class TestBug1746ChunkStoreUnavailableErrorPropagates:
    """AC: Given the target collection's chunks.db cannot be opened for
    write (permission denied, corrupt file, or any
    sqlite3.OperationalError/PermissionError at open time), when a file's
    processing reaches the vector-storage write step, then the fatal error
    propagates out of _process_file_clean_lifecycle() as
    ChunkStoreUnavailableError, NOT as a returned
    FileProcessingResult(success=False)."""

    def test_permission_error_from_upsert_points_raises_chunk_store_unavailable_error(
        self, tmp_path: Path
    ) -> None:
        client = _RaisingFilesystemClient(
            PermissionError("[Errno 13] Permission denied")
        )

        with pytest.raises(ChunkStoreUnavailableError):
            _process_file(tmp_path, _FakeVectorManager(), client)

    def test_sqlite_operational_error_from_upsert_points_raises_chunk_store_unavailable_error(
        self, tmp_path: Path
    ) -> None:
        client = _RaisingFilesystemClient(
            sqlite3.OperationalError("unable to open database file")
        )

        with pytest.raises(ChunkStoreUnavailableError):
            _process_file(tmp_path, _FakeVectorManager(), client)

    def test_chunk_store_unavailable_error_raised_directly_propagates_unchanged(
        self, tmp_path: Path
    ) -> None:
        """If a lower layer already raises the typed error directly (e.g.
        a schema-init failure wrapped at the storage layer), it must
        propagate unchanged -- never double-wrapped or swallowed."""
        original = ChunkStoreUnavailableError("chunks.db schema init failed")
        client = _RaisingFilesystemClient(original)

        with pytest.raises(ChunkStoreUnavailableError):
            _process_file(tmp_path, _FakeVectorManager(), client)


class TestBug1746NonStorageFailureRegressionUnchanged:
    """AC (regression): Given a non-storage per-file failure (e.g. a
    corrupt/unreadable source file, an embedding-provider error), when that
    file is processed, then behavior is UNCHANGED -- it still returns
    FileProcessingResult(success=False, error=...) exactly as before this
    change."""

    def test_generic_exception_from_upsert_points_still_returns_failed_result(
        self, tmp_path: Path
    ) -> None:
        client = _RaisingFilesystemClient(RuntimeError("embedding provider exploded"))

        result = _process_file(tmp_path, _FakeVectorManager(), client)

        assert result.success is False
        assert result.error is not None
        assert "embedding provider exploded" in result.error

    def test_generic_os_error_now_fatal_per_h2_widening(self, tmp_path: Path) -> None:
        """H2 (code review finding) supersedes the original narrower scope:
        a plain OSError (e.g. disk-full ENOSPC, which sqlite/the OS raises
        as a bare OSError -- NOT PermissionError) must now be treated as
        fatal. Before H2, this was deliberately NOT fatal to keep the fix
        narrowly scoped; H2 widened the fatal-catch set to OSError
        (subsumes PermissionError) specifically to close this gap."""
        client = _RaisingFilesystemClient(OSError("some unrelated OS failure"))

        with pytest.raises(ChunkStoreUnavailableError):
            _process_file(tmp_path, _FakeVectorManager(), client)


class TestBug1746H1LockContentionIsNotFatal:
    """H1 (code review finding): a TRANSIENT sqlite3 lock-contention error
    -- expected under concurrent CHUNKS_DB writers, since a fresh
    connection is opened per upsert_points() call with no cross-thread
    application lock -- must fail only the ONE file, never abort the
    whole indexing run. Before this fix, ANY sqlite3.OperationalError was
    treated as fatal, which would take down an entire run on ordinary
    lock contention."""

    @pytest.mark.parametrize(
        "lock_message",
        ["database is locked", "database table is locked"],
    )
    def test_lock_contention_message_is_not_fatal(
        self, tmp_path: Path, lock_message: str
    ) -> None:
        client = _RaisingFilesystemClient(sqlite3.OperationalError(lock_message))

        result = _process_file(tmp_path, _FakeVectorManager(), client)

        assert result.success is False
        assert result.error is not None


class TestBug1746H2CorruptFileAndDiskFullAreFatal:
    """H2 (code review finding): the issue's own AC names "corrupt file"
    explicitly as a fatal condition. sqlite3.OperationalError IS-A (is a
    subclass of) sqlite3.DatabaseError -- but a corrupt chunks.db raises
    DatabaseError DIRECTLY (e.g. "file is not a database"), not via
    OperationalError, so the original narrower fix (which only caught
    OperationalError) never saw it. Disk-full raises a plain OSError (not
    PermissionError), also not caught originally. Both must be fatal
    (abort the run), applied AFTER H1's lock exclusion so this widening
    doesn't make lock-contention worse."""

    @pytest.mark.parametrize(
        "fatal_exception",
        [
            sqlite3.DatabaseError("file is not a database"),
            OSError(28, "No space left on device"),
        ],
        ids=["corrupt_database_file", "disk_full_os_error"],
    )
    def test_fatal_error_raises_chunk_store_unavailable_error(
        self, tmp_path: Path, fatal_exception: BaseException
    ) -> None:
        client = _RaisingFilesystemClient(fatal_exception)

        with pytest.raises(ChunkStoreUnavailableError):
            _process_file(tmp_path, _FakeVectorManager(), client)
