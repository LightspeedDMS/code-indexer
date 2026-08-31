"""Bug #1502: stable chunk_index / point_id identity in FileChunkingManager.

Root cause (confirmed, see GitHub issue #1502): the chunk_index used to
build a point's identity was assigned by enumerate() over the SUBSET of
chunks that survived to get a fresh embedding in a given run, instead of
the chunk's fixed positional index from the chunker. Cache hits (Story
#470) and skipped chunks shift that subset run-to-run, so the same label
lands on different chunks across runs -> same point_id, different
content -> duplicate points in the vector store.

These tests prove:
  1. point_id identity is STABLE across runs regardless of which chunks
     are cache-served vs freshly embedded (the killer test -- must FAIL
     on the pre-fix enumerate() bug, PASS after the fix).
  2. A falsy/invalid embedding fails the file loudly -- it is never
     silently skipped-and-renumbered.
"""

# mypy: ignore-errors
# Duck-typed fakes passed to FileChunkingManager's strictly-typed
# constructor params (vector_manager: VectorCalculationManager, etc.)
# work correctly at runtime but trip mypy's structural check -- matches
# the established convention in the sibling test_file_chunking_manager.py.

import hashlib
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock

from code_indexer.services.clean_slot_tracker import CleanSlotTracker
from code_indexer.services.file_chunking_manager import FileChunkingManager


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


class _FakeVectorManagerWithFalsyEmbedding(_FakeVectorManager):
    """Same as _FakeVectorManager, but the batch result always includes
    one falsy (empty-tuple) embedding to exercise the fail-loud path."""

    def submit_batch_task(
        self, chunk_texts: List[str], metadata: Dict[str, Any]
    ) -> "Future[Any]":
        from code_indexer.services.vector_calculation_manager import VectorResult

        self.batches.append(list(chunk_texts))
        future: "Future[Any]" = Future()
        embeddings: List[Any] = []
        for idx, text in enumerate(chunk_texts):
            if idx == 1:
                embeddings.append(())  # falsy embedding
            else:
                embeddings.append(
                    tuple(
                        float(b) / 255.0
                        for b in hashlib.sha256(text.encode()).digest()[:8]
                    )
                )
        result = VectorResult(
            task_id=f"batch_{len(self.batches)}",
            embeddings=tuple(embeddings),
            metadata=metadata.copy(),
            processing_time=0.0,
            error=None,
        )
        future.set_result(result)
        return future


class _FakeCacheAwareFilesystemClient:
    """Minimal vector_store_client stand-in that tracks upserted points and
    lets the test control get_existing_content_hashes() to simulate a
    Story #470 cache hit on an arbitrary chunk_index."""

    def __init__(self) -> None:
        self.upserted_points: List[Dict[str, Any]] = []
        self.existing_hashes: Dict[int, Dict[str, Any]] = {}

    def upsert_points(self, points: List[Dict[str, Any]], collection_name=None) -> bool:
        self.upserted_points.extend(points)
        return True

    def collection_exists(self, collection_name: str) -> bool:
        return True

    def create_collection(self, collection_name: str, vector_size: int) -> bool:
        return True

    def get_existing_content_hashes(
        self, file_path: str, collection_name: str
    ) -> Dict[int, Dict[str, Any]]:
        return self.existing_hashes


def _make_chunker(num_chunks: int) -> Mock:
    """Deterministic chunker returning num_chunks distinct chunks with a
    stable positional chunk_index, mirroring FixedSizeChunker's real
    per-chunk dict shape."""
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
    filesystem_client: _FakeCacheAwareFilesystemClient,
    num_chunks: int,
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


class TestBug1502StableChunkIndex:
    def test_point_id_identity_stable_across_cache_hit_and_full_fresh_runs(
        self, tmp_path: Path
    ) -> None:
        """Killer test: chunk_index=2's point_id must be IDENTICAL whether
        chunk_index=1 was a cache hit (removed from the fresh-embed batch)
        or every chunk was freshly embedded. Under the pre-fix
        enumerate()-over-survivors bug, removing chunk_index=1 from the
        fresh batch shifts chunk_index=2 and chunk_index=3's positional
        label in file_points, forging a DIFFERENT point_id than the
        all-fresh run produced for the exact same chunk content.
        """
        # Run A: ALL 4 chunks freshly embedded (no cache hits at all).
        client_a = _FakeCacheAwareFilesystemClient()
        result_a = _process_file(tmp_path / "a", _FakeVectorManager(), client_a, 4)
        assert result_a.success is True, result_a.error

        # Run B: chunk_index=1 is a cache hit (Story #470) -- removed from
        # the fresh-embed batch entirely, so only chunk_index 0, 2, 3 are
        # submitted for fresh embedding.
        chunk1_text = "chunk number 1 unique content payload"
        client_b = _FakeCacheAwareFilesystemClient()
        client_b.existing_hashes = {
            1: {
                "content_hash": hashlib.sha256(chunk1_text.encode()).hexdigest(),
                "vector": [0.42] * 8,
                "point_id": "irrelevant-cache-point-id",
            }
        }
        result_b = _process_file(tmp_path / "b", _FakeVectorManager(), client_b, 4)
        assert result_b.success is True, result_b.error

        def _point_ids_by_text(points: List[Dict[str, Any]]) -> Dict[str, str]:
            return {p["payload"]["content"]: p["id"] for p in points}

        ids_a = _point_ids_by_text(client_a.upserted_points)
        ids_b = _point_ids_by_text(client_b.upserted_points)

        # ALL 4 chunks -- including chunk_index=1, which took the Story
        # #470 cache-HIT path (_create_vector_point) in run B -- must have
        # stable point_ids across both runs. Chunks 2 and 3 are exactly
        # where the pre-fix enumerate() bug shifts the label; chunk 1
        # locks in that the pre-existing cache-hit path (which already
        # used the chunker's true positional chunk_index) stays correct.
        for i in (0, 1, 2, 3):
            text = f"chunk number {i} unique content payload"
            assert ids_a[text] == ids_b[text], (
                f"chunk_index={i}'s point_id must be stable across runs "
                f"regardless of cache hits elsewhere in the file "
                f"(run A id={ids_a[text]!r}, run B id={ids_b[text]!r})"
            )

        # And the point_id must actually match the deterministic identity
        # formula using the chunk's TRUE positional chunk_index (never a
        # position-among-survivors index).
        for i in (0, 1, 2, 3):
            text = f"chunk number {i} unique content payload"
            expected_id = hashlib.md5(f"proj_sha256:abc123_{i}".encode()).hexdigest()
            assert ids_a[text] == expected_id, (
                f"chunk_index={i}'s point_id must use the chunker's true "
                f"positional index {i}, got {ids_a[text]!r} "
                f"(expected {expected_id!r})"
            )

    def test_falsy_embedding_fails_file_loudly_never_silently_skipped(
        self, tmp_path: Path
    ) -> None:
        """A falsy (empty) embedding for one chunk must fail the WHOLE file
        loudly -- never silently drop that chunk and renumber survivors."""
        client = _FakeCacheAwareFilesystemClient()
        result = _process_file(
            tmp_path, _FakeVectorManagerWithFalsyEmbedding(), client, 4
        )

        assert result.success is False
        assert result.error is not None
        assert result.chunks_processed == 0
        # Nothing should have been written to storage for a failed file.
        assert client.upserted_points == []


class _UpsertTrackingFilesystemClient:
    """vector_store_client stand-in that tracks EVERY upsert_points call
    (collection_name + points) across BOTH the regular and multimodal
    collections, to prove file-atomicity across both. Intentionally does
    NOT implement get_existing_content_hashes -- FileChunkingManager
    already wraps that lookup in try/except and degrades to an empty
    dict on AttributeError."""

    def __init__(self) -> None:
        self.upsert_calls: List[Dict[str, Any]] = []

    def upsert_points(self, points: List[Dict[str, Any]], collection_name=None) -> bool:
        self.upsert_calls.append(
            {"collection_name": collection_name, "points": list(points)}
        )
        return True

    def collection_exists(self, collection_name: str) -> bool:
        return False

    def create_collection(self, collection_name: str, vector_size: int) -> bool:
        return True


class TestBug1502MultimodalFileAtomicity:
    """Codex M6: the multimodal branch used to upsert its points BEFORE
    regular embeddings were validated -- a later falsy regular embedding
    failed the file but left the multimodal points already persisted,
    violating file atomicity. Regular embeddings must be fully validated
    BEFORE any upsert of multimodal OR regular points."""

    def test_falsy_regular_embedding_leaves_no_multimodal_points_persisted(
        self, tmp_path: Path
    ) -> None:
        # Existence-only validation in the multimodal image-resolution
        # path (matches this same file's other multimodal fixtures) --
        # never PNG-magic-byte-checked.
        test_image_path = tmp_path / "diagram.png"
        test_image_path.write_bytes(b"placeholder-image-bytes")

        test_md_path = tmp_path / "docs.md"
        test_md_path.write_text(
            "# Architecture\n\n![Diagram](diagram.png)\n\nSome regular text."
        )

        mock_chunker = Mock()
        mock_chunker.chunk_file.return_value = [
            {
                "text": "# Architecture\n\n![Diagram](diagram.png)",
                "chunk_index": 0,
                "total_chunks": 5,
                "file_extension": "md",
                "size": 40,
                "line_start": 1,
                "line_end": 3,
                "file_path": None,
                "images": ["diagram.png"],
            },
            *[
                {
                    "text": f"regular chunk {i} unique text payload",
                    "chunk_index": i + 1,
                    "total_chunks": 5,
                    "file_extension": "md",
                    "size": 40,
                    "line_start": (i + 1) * 10 + 1,
                    "line_end": (i + 1) * 10 + 10,
                    "file_path": None,
                    "images": [],
                }
                for i in range(4)
            ],
        ]

        mock_multimodal_client = Mock()
        mock_multimodal_client.get_multimodal_embedding.return_value = [0.2] * 1024
        mock_multimodal_client.config = Mock()
        mock_multimodal_client.config.model = "voyage-multimodal-3"

        client = _UpsertTrackingFilesystemClient()

        with FileChunkingManager(
            vector_manager=_FakeVectorManagerWithFalsyEmbedding(),
            chunker=mock_chunker,
            vector_store_client=client,
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=tmp_path,
            multimodal_client=mock_multimodal_client,
        ) as manager:
            metadata = {
                "project_id": "proj",
                "file_hash": "sha256:abc123",
                "collection_name": "col",
                "git_available": False,
            }
            future = manager.submit_file_for_processing(test_md_path, metadata, None)
            result = future.result(timeout=10.0)

        assert result.success is False
        # File atomicity: NOTHING persisted to ANY collection (neither
        # the regular collection NOR the multimodal one) when a regular
        # embedding is falsy -- even though the multimodal chunk's
        # embedding was itself perfectly valid.
        assert client.upsert_calls == []


class TestBug1502MultimodalOnlyAllFailuresFailLoud:
    """Codex finding 4: a multimodal-ONLY file (zero regular chunks)
    whose multimodal embedding attempts ALL fail used to report silent
    success -- each per-chunk exception was logged-and-skipped, and with
    zero regular chunks the embedding-count check trivially passed
    (0 == 0), so the file completed with chunks_processed=0 and
    success=True despite NOTHING being persisted."""

    def test_multimodal_only_file_all_embeddings_fail_reports_failure(
        self, tmp_path: Path
    ) -> None:
        test_image_path = tmp_path / "diagram.png"
        test_image_path.write_bytes(b"placeholder-image-bytes")

        test_md_path = tmp_path / "docs.md"
        test_md_path.write_text("# Architecture\n\n![Diagram](diagram.png)\n")

        mock_chunker = Mock()
        mock_chunker.chunk_file.return_value = [
            {
                "text": "# Architecture\n\n![Diagram](diagram.png)",
                "chunk_index": 0,
                "total_chunks": 1,
                "file_extension": "md",
                "size": 40,
                "line_start": 1,
                "line_end": 3,
                "file_path": None,
                "images": ["diagram.png"],
            }
        ]

        mock_multimodal_client = Mock()
        mock_multimodal_client.get_multimodal_embedding.side_effect = RuntimeError(
            "simulated multimodal embedding provider failure"
        )
        mock_multimodal_client.config = Mock()
        mock_multimodal_client.config.model = "voyage-multimodal-3"

        client = _UpsertTrackingFilesystemClient()

        with FileChunkingManager(
            vector_manager=_FakeVectorManager(),
            chunker=mock_chunker,
            vector_store_client=client,
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=tmp_path,
            multimodal_client=mock_multimodal_client,
        ) as manager:
            metadata = {
                "project_id": "proj",
                "file_hash": "sha256:abc123",
                "collection_name": "col",
                "git_available": False,
            }
            future = manager.submit_file_for_processing(test_md_path, metadata, None)
            result = future.result(timeout=10.0)

        assert result.success is False, (
            "a multimodal-only file whose sole chunk's embedding failed "
            "must report failure, never silent success"
        )
        assert client.upsert_calls == []
