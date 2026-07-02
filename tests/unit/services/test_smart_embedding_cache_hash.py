"""
Unit tests for Story #470: Smart Embedding Cache - content_hash in vector payload.

Tests verify that:
1. _create_vector_point includes content_hash in payload (SHA-256 of chunk text)
2. content_hash is deterministic (same text = same hash)
3. Different texts produce different hashes

TDD: These tests are written BEFORE implementation. They should fail initially.
"""

# mypy: ignore-errors

import hashlib
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import Mock


from src.code_indexer.services.file_chunking_manager import FileChunkingManager
from src.code_indexer.services.clean_slot_tracker import CleanSlotTracker


# ---------------------------------------------------------------------------
# Shared helpers (imported by other test_smart_embedding_cache_*.py files)
# ---------------------------------------------------------------------------


def make_sha256(text: str) -> str:
    """Compute expected SHA-256 hex digest for a given text string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeVectorCalculationManager:
    """Minimal fake VectorCalculationManager that records API calls."""

    def __init__(self, embedding_dim: int = 16):
        self.embedding_dim = embedding_dim
        self.batch_calls: List[List[str]] = []
        self.submit_delay = 0.005
        self.cancellation_event = threading.Event()

        self.embedding_provider = Mock()
        self.embedding_provider.get_current_model.return_value = "voyage-code-3"
        self.embedding_provider._get_model_token_limit.return_value = 120_000

    def submit_batch_task(
        self, chunk_texts: List[str], metadata: Dict[str, Any]
    ) -> Future:
        """Record the call and return a future with fake embeddings."""
        self.batch_calls.append(list(chunk_texts))

        from src.code_indexer.services.vector_calculation_manager import VectorResult

        future: Future = Future()
        embeddings = tuple(
            tuple(float(i + 1) * 0.1 for i in range(self.embedding_dim))
            for _ in chunk_texts
        )
        result = VectorResult(
            task_id=f"batch_{len(self.batch_calls)}",
            embeddings=embeddings,
            metadata=metadata.copy(),
            processing_time=self.submit_delay * len(chunk_texts),
            error=None,
        )

        def _complete():
            time.sleep(self.submit_delay)
            future.set_result(result)

        threading.Thread(target=_complete, daemon=True).start()
        return future


class FakeChunker:
    """Fake FixedSizeChunker producing controllable chunks."""

    def __init__(self, chunks_by_path: Optional[Dict[str, List[Dict]]] = None):
        self._chunks = chunks_by_path or {}

    def chunk_file(
        self, file_path: Path, repo_root: Optional[Path] = None
    ) -> List[Dict]:
        key = str(file_path)
        if key in self._chunks:
            return self._chunks[key]
        try:
            content = file_path.read_text()
        except Exception:
            content = "default content"
        return [
            {
                "text": content,
                "chunk_index": 0,
                "total_chunks": 1,
                "file_extension": file_path.suffix.lstrip(".") or "txt",
                "line_start": 1,
                "line_end": 5,
            }
        ]


class FakeFilesystemClient:
    """Fake FilesystemVectorStore client for testing FileChunkingManager."""

    def __init__(self):
        self.upserted_points: List[Dict] = []
        self.upsert_calls: List[Dict] = []
        self._stored_hashes: Dict[str, Dict[int, Dict]] = {}

    def upsert_points(
        self, points: List[Dict], collection_name: Optional[str] = None
    ) -> bool:
        self.upsert_calls.append(
            {"points": list(points), "collection_name": collection_name}
        )
        self.upserted_points.extend(points)
        return True

    def collection_exists(self, collection_name: str) -> bool:
        return True

    def create_collection(self, collection_name: str, vector_size: int) -> bool:
        return True

    def get_existing_content_hashes(
        self, file_path: str, collection_name: str
    ) -> Dict[int, Dict[str, Any]]:
        return self._stored_hashes.get(file_path, {})

    def seed_existing_hashes(
        self,
        file_path: str,
        chunk_index: int,
        content_hash: str,
        vector: List[float],
        point_id: str = "fake-point-id",
    ) -> None:
        if file_path not in self._stored_hashes:
            self._stored_hashes[file_path] = {}
        self._stored_hashes[file_path][chunk_index] = {
            "content_hash": content_hash,
            "vector": vector,
            "point_id": point_id,
        }


def make_manager(
    tmp_dir: Path,
    chunker: FakeChunker,
    vector_manager: Optional[FakeVectorCalculationManager] = None,
    filesystem_client: Optional[FakeFilesystemClient] = None,
) -> FileChunkingManager:
    """Factory for FileChunkingManager under test."""
    if vector_manager is None:
        vector_manager = FakeVectorCalculationManager()
    if filesystem_client is None:
        filesystem_client = FakeFilesystemClient()

    slot_tracker = CleanSlotTracker(max_slots=4)
    return FileChunkingManager(
        vector_manager=vector_manager,
        chunker=chunker,
        vector_store_client=filesystem_client,
        thread_count=2,
        slot_tracker=slot_tracker,
        codebase_dir=tmp_dir,
    )


def make_metadata(collection_name: str = "voyage-code-3") -> Dict[str, Any]:
    """Create minimal metadata dict as expected by _process_file_clean_lifecycle."""
    return {
        "project_id": "test-project",
        "file_hash": "abc123",
        "git_available": False,
        "collection_name": collection_name,
        "file_mtime": 0.0,
        "file_size": 100,
    }


# ---------------------------------------------------------------------------
# Tests: content_hash in vector point payload
# ---------------------------------------------------------------------------


class TestCreateVectorPointIncludesContentHash:
    """_create_vector_point must include a SHA-256 content_hash in the payload."""

    def test_content_hash_present_in_payload(self, tmp_path):
        """content_hash key must exist in the vector point payload."""
        chunker = FakeChunker()
        manager = make_manager(tmp_path, chunker)

        chunk_text = "def hello():\n    return 'world'\n"
        chunk = {
            "text": chunk_text,
            "chunk_index": 0,
            "total_chunks": 1,
            "file_extension": "py",
            "line_start": 1,
            "line_end": 3,
        }
        file_path = tmp_path / "hello.py"
        file_path.write_text(chunk_text)
        metadata = make_metadata()

        vector_point = manager._create_vector_point(
            chunk, [0.1] * 16, metadata, file_path
        )

        assert "content_hash" in vector_point["payload"], (
            "_create_vector_point must add content_hash to payload (Story #470)"
        )

    def test_content_hash_is_sha256_of_chunk_text(self, tmp_path):
        """content_hash must be SHA-256 hex digest of the chunk text."""
        chunker = FakeChunker()
        manager = make_manager(tmp_path, chunker)

        chunk_text = "some code content here"
        expected_hash = make_sha256(chunk_text)

        chunk = {
            "text": chunk_text,
            "chunk_index": 0,
            "total_chunks": 1,
            "file_extension": "py",
            "line_start": 1,
            "line_end": 2,
        }
        file_path = tmp_path / "code.py"
        file_path.write_text(chunk_text)
        metadata = make_metadata()

        vector_point = manager._create_vector_point(
            chunk, [0.5] * 16, metadata, file_path
        )

        assert vector_point["payload"]["content_hash"] == expected_hash

    def test_content_hash_differs_for_different_text(self, tmp_path):
        """Different chunk texts must produce different content_hash values."""
        chunker = FakeChunker()
        manager = make_manager(tmp_path, chunker)

        text_a = "def alpha(): pass"
        text_b = "def beta(): pass"

        chunk_a = {
            "text": text_a,
            "chunk_index": 0,
            "total_chunks": 1,
            "file_extension": "py",
            "line_start": 1,
            "line_end": 1,
        }
        chunk_b = {
            "text": text_b,
            "chunk_index": 0,
            "total_chunks": 1,
            "file_extension": "py",
            "line_start": 1,
            "line_end": 1,
        }

        file_path = tmp_path / "f.py"
        file_path.write_text(text_a)
        metadata = make_metadata()

        vp_a = manager._create_vector_point(chunk_a, [0.1] * 16, metadata, file_path)
        vp_b = manager._create_vector_point(chunk_b, [0.1] * 16, metadata, file_path)

        assert vp_a["payload"]["content_hash"] != vp_b["payload"]["content_hash"]


# ---------------------------------------------------------------------------
# Tests: hash determinism
# ---------------------------------------------------------------------------


class TestContentHashDeterminism:
    """SHA-256 content hash must be deterministic across calls."""

    def test_same_text_produces_same_hash_in_payload(self, tmp_path):
        """Same chunk text must produce identical content_hash in payload."""
        chunker = FakeChunker()
        manager = make_manager(tmp_path, chunker)

        chunk_text = "class Foo:\n    pass\n"
        chunk = {
            "text": chunk_text,
            "chunk_index": 0,
            "total_chunks": 1,
            "file_extension": "py",
            "line_start": 1,
            "line_end": 2,
        }
        file_path = tmp_path / "foo.py"
        file_path.write_text(chunk_text)
        metadata = make_metadata()

        vp1 = manager._create_vector_point(chunk, [0.1] * 16, metadata, file_path)
        vp2 = manager._create_vector_point(chunk, [0.9] * 16, metadata, file_path)

        assert vp1["payload"]["content_hash"] == vp2["payload"]["content_hash"]

    def test_hash_is_64_char_hex_string(self, tmp_path):
        """SHA-256 produces a 64-character hexadecimal string."""
        chunker = FakeChunker()
        manager = make_manager(tmp_path, chunker)

        chunk_text = "any content"
        chunk = {
            "text": chunk_text,
            "chunk_index": 0,
            "total_chunks": 1,
            "file_extension": "py",
            "line_start": 1,
            "line_end": 1,
        }
        file_path = tmp_path / "any.py"
        file_path.write_text(chunk_text)
        metadata = make_metadata()

        vp = manager._create_vector_point(chunk, [0.1] * 16, metadata, file_path)
        content_hash = vp["payload"]["content_hash"]

        assert len(content_hash) == 64
        assert all(c in "0123456789abcdef" for c in content_hash)
