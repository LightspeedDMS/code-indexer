"""Story #2119: the reuse lookup fails CLOSED, receives the writer's content
identity, and byte-identical copies run after their first copy.

The embedding provider boundary is replaced by a recording stand-in (an
external service); the code under test -- FileChunkingManager and the
high-throughput ordering helper -- runs for real."""

from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import Mock

import pytest

from code_indexer.services.clean_slot_tracker import CleanSlotTracker
from code_indexer.services.file_chunking_manager import FileChunkingManager
from code_indexer.services.high_throughput_processor import (
    _first_copies_then_duplicates,
)
from code_indexer.services.index_failure_exit_codes import (
    EXIT_CODE_CHUNK_STORE_CORRUPTION,
    index_failure_exit_code,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStoreUnavailableError

_METADATA = {
    "project_id": "example-repo",
    "file_hash": "sha256:feedface",
    "collection_name": "voyage-code-3",
    "git_available": False,
}


class _RecordingVectorManager:
    """Records every batch the file would send to the provider."""

    def __init__(self) -> None:
        self.submitted: List[List[str]] = []
        self.embedding_provider = SimpleNamespace(
            _get_model_token_limit=lambda: 32000,
            get_current_model=lambda: "voyage-code-3",
        )

    def submit_batch_task(self, texts: List[str], metadata: Dict[str, Any]) -> Any:
        self.submitted.append(list(texts))
        raise AssertionError("the provider must not be called in these tests")


class _LookupClient:
    """Vector-store stand-in: records each lookup's (positional path,
    keyword arguments) and answers with ``hits`` or raises ``error``."""

    def __init__(
        self,
        error: Optional[Exception] = None,
        hits: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.error = error
        self.hits = hits or {}
        self.calls: List[Dict[str, Any]] = []
        self.upserted: List[Dict[str, Any]] = []

    def get_existing_content_hashes(self, file_path, collection_name, **kwargs):
        self.calls.append({"file_path": file_path, **kwargs})
        if self.error is not None:
            raise self.error
        return self.hits

    def upsert_points(self, points, collection_name=None) -> bool:
        self.upserted.extend(points)
        return True


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _chunker(texts: List[str]) -> Mock:
    chunker = Mock()
    chunker.chunk_file.return_value = [
        {
            "text": text,
            "chunk_index": i,
            "total_chunks": len(texts),
            "file_extension": "py",
            "line_start": i + 1,
            "line_end": i + 1,
        }
        for i, text in enumerate(texts)
    ]
    return chunker


def _manager(
    tmp_path: Path, client: Any, vm: Any, chunker: Mock
) -> FileChunkingManager:
    return FileChunkingManager(
        vector_manager=vm,
        chunker=chunker,
        vector_store_client=client,
        thread_count=2,
        slot_tracker=CleanSlotTracker(max_slots=4),
        codebase_dir=tmp_path,
    )


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "module.py"
    path.write_text("def example():\n    return 1\n")
    return path


def test_lookup_corruption_aborts_with_corruption_code_and_sends_nothing(
    tmp_path: Path,
) -> None:
    client = _LookupClient(
        error=sqlite3.DatabaseError("database disk image is malformed")
    )
    vm = _RecordingVectorManager()
    with _manager(tmp_path, client, vm, _chunker(["a", "b"])) as manager:
        future = manager.submit_file_for_processing(
            _source(tmp_path), dict(_METADATA), None
        )
        with pytest.raises(ChunkStoreUnavailableError) as raised:
            future.result(timeout=10)
    assert index_failure_exit_code(raised.value) == EXIT_CODE_CHUNK_STORE_CORRUPTION
    assert vm.submitted == []
    assert client.upserted == []


def test_lookup_receives_the_writers_content_identity(tmp_path: Path) -> None:
    texts = ["first chunk", "second chunk", "third chunk"]
    hits = {
        _sha(t): {"vector": [0.25] * 8, "point_id": f"p{i}"}
        for i, t in enumerate(texts)
    }
    client = _LookupClient(hits=hits)
    vm = _RecordingVectorManager()
    with _manager(tmp_path, client, vm, _chunker(texts)) as manager:
        result = manager.submit_file_for_processing(
            _source(tmp_path), dict(_METADATA), None
        ).result(timeout=10)

    assert result.success, result.error
    # The path is the stored (codebase-relative) form; the keywords are
    # exactly the writer's point-id inputs plus the current chunk count.
    assert client.calls == [
        {
            "file_path": "module.py",
            "project_id": "example-repo",
            "file_hash": "sha256:feedface",
            "chunk_count": 3,
        }
    ]
    assert vm.submitted == []  # every chunk reused
    assert len(client.upserted) == 3


def test_first_copies_come_before_their_duplicates() -> None:
    a1, b1, a2, c1, b2, gone, a3 = (Path(n) for n in "a1 b1 a2 c1 b2 gone a3".split())
    hashes = {a1: "A", b1: "B", a2: "A", c1: "C", b2: "B", a3: "A"}
    hash_results = {p: ({"file_hash": h}, 10) for p, h in hashes.items()}

    ordered = _first_copies_then_duplicates(
        [a1, b1, a2, c1, b2, gone, a3], hash_results
    )

    assert ordered == [a1, b1, c1, gone, a2, b2, a3]


def test_a_copy_of_a_cancelled_first_copy_is_cancelled(tmp_path: Path) -> None:
    chunker = _chunker(["a"])
    first: Future = Future()
    first.cancel()
    with _manager(tmp_path, _LookupClient(), _RecordingVectorManager(), chunker) as m:
        result = m.submit_file_for_processing(
            _source(tmp_path), dict(_METADATA), None, after=first
        ).result(timeout=10)
    assert not result.success
    assert result.error == "Cancelled before processing"
    chunker.chunk_file.assert_not_called()


def test_a_copy_of_a_fatally_failed_first_copy_never_runs(tmp_path: Path) -> None:
    chunker = _chunker(["a"])
    first: Future = Future()
    first.set_exception(ChunkStoreUnavailableError("disk I/O error"))
    vm = _RecordingVectorManager()
    with _manager(tmp_path, _LookupClient(), vm, chunker) as m:
        future = m.submit_file_for_processing(
            _source(tmp_path), dict(_METADATA), None, after=first
        )
        with pytest.raises(ChunkStoreUnavailableError):
            future.result(timeout=10)
    chunker.chunk_file.assert_not_called()
    assert vm.submitted == []


class _FailingProviderVectorManager(_RecordingVectorManager):
    """Provider boundary that rejects every request (e.g. a 4xx): returns the
    production failed ``VectorResult`` and records the inputs it was sent."""

    def submit_batch_task(self, texts: List[str], metadata: Dict[str, Any]) -> Any:
        from code_indexer.services.vector_calculation_manager import VectorResult

        self.submitted.append(list(texts))
        future: Future = Future()
        future.set_result(
            VectorResult(
                task_id=f"batch_{len(self.submitted)}",
                embeddings=(),
                metadata=metadata,
                processing_time=0.0,
                error="provider rejected the request",
            )
        )
        return future


def test_a_copy_of_a_first_copy_that_failed_sends_nothing(tmp_path: Path) -> None:
    """Codex P2-2: the first copy failed at the provider (an unsuccessful
    FileProcessingResult, nothing raised). Its byte-identical copy must not
    send the same content again in this run; it is reported failed, so the
    run's failed-file accounting stays truthful and a later run retries."""
    vm = _FailingProviderVectorManager()
    client = _LookupClient()
    first_path = _source(tmp_path)
    copy_path = tmp_path / "module_copy.py"
    copy_path.write_text(first_path.read_text())
    with _manager(tmp_path, client, vm, _chunker(["a", "b"])) as m:
        first = m.submit_file_for_processing(first_path, dict(_METADATA), None)
        copy = m.submit_file_for_processing(
            copy_path, dict(_METADATA), None, after=first
        )
        first_file_result = first.result(timeout=10)
        copy_file_result = copy.result(timeout=10)

    assert not first_file_result.success
    assert vm.submitted == [["a", "b"]], "only the first copy may reach the provider"
    assert not copy_file_result.success
    assert "provider rejected the request" in (copy_file_result.error or "")
    assert client.upserted == []


@pytest.mark.parametrize("vanished", [False, True], ids=["succeeded", "vanished"])
def test_a_copy_runs_after_its_first_copy_finished(
    tmp_path: Path, vanished: bool
) -> None:
    """A copy runs once its first copy succeeded -- or vanished (a skip, not
    a failure: nothing was stored, nothing was paid)."""
    from code_indexer.services.file_chunking_manager import FileProcessingResult

    first: Future = Future()
    first.set_result(
        FileProcessingResult(
            success=not vanished,
            file_path=tmp_path / "first.py",
            chunks_processed=0 if vanished else 1,
            processing_time=0.0,
            vanished=vanished,
        )
    )
    client = _LookupClient(hits={_sha("a"): {"vector": [0.5] * 8, "point_id": "p0"}})
    with _manager(tmp_path, client, _RecordingVectorManager(), _chunker(["a"])) as m:
        result = m.submit_file_for_processing(
            _source(tmp_path), dict(_METADATA), None, after=first
        ).result(timeout=10)
    assert result.success, result.error
    assert len(client.upserted) == 1
