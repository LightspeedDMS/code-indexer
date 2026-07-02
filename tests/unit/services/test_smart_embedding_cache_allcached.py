"""
Unit tests for Story #470: Smart Embedding Cache - all-chunks-cached edge case.

Tests verify that when ALL chunks of a file are cache hits:
1. Zero batch API calls are made
2. All cached points are still written to the vector store
3. The result is a successful FileProcessingResult with no error

TDD: These tests verify the all-cached edge case where batch_futures is empty
but cached_points is not, which requires special handling in the lifecycle.
"""

# mypy: ignore-errors

from src.code_indexer.services.file_chunking_manager import FileProcessingResult
from src.code_indexer.services.clean_slot_tracker import CleanSlotTracker

# Re-use helpers from the hash test module
from tests.unit.services.test_smart_embedding_cache_hash import (
    FakeVectorCalculationManager,
    FakeChunker,
    FakeFilesystemClient,
    make_manager,
    make_metadata,
    make_sha256,
)


class TestAllChunksCached:
    """When all chunks are cache hits, no API calls and all points still upserted."""

    def test_all_cached_zero_api_calls(self, tmp_path):
        """All chunks cached must result in zero VoyageAI embedding API calls."""
        texts = ["def a(): pass", "def b(): pass"]
        src_file = tmp_path / "all_cached.py"
        src_file.write_text("\n".join(texts))

        fake_client = FakeFilesystemClient()
        for idx, text in enumerate(texts):
            fake_client.seed_existing_hashes(
                file_path="all_cached.py",
                chunk_index=idx,
                content_hash=make_sha256(text),
                vector=[float(idx + 1) * 0.5] * 16,
            )

        chunker = FakeChunker(
            chunks_by_path={
                str(src_file): [
                    {
                        "text": texts[0],
                        "chunk_index": 0,
                        "total_chunks": 2,
                        "file_extension": "py",
                        "line_start": 1,
                        "line_end": 1,
                    },
                    {
                        "text": texts[1],
                        "chunk_index": 1,
                        "total_chunks": 2,
                        "file_extension": "py",
                        "line_start": 2,
                        "line_end": 2,
                    },
                ]
            }
        )

        vector_manager = FakeVectorCalculationManager()
        manager = make_manager(tmp_path, chunker, vector_manager, fake_client)
        metadata = make_metadata()

        slot_tracker = CleanSlotTracker(max_slots=4)
        result = manager._process_file_clean_lifecycle(
            file_path=src_file,
            metadata=metadata,
            progress_callback=None,
            slot_tracker=slot_tracker,
        )

        assert result.success, f"All-cached file must succeed: {result.error}"
        assert len(vector_manager.batch_calls) == 0, (
            f"No API calls must be made when all chunks are cached, "
            f"got {len(vector_manager.batch_calls)}"
        )

    def test_all_cached_all_points_upserted(self, tmp_path):
        """All cached points must be written to the vector store."""
        texts = ["def a(): pass", "def b(): pass"]
        src_file = tmp_path / "all_cached2.py"
        src_file.write_text("\n".join(texts))

        fake_client = FakeFilesystemClient()
        for idx, text in enumerate(texts):
            fake_client.seed_existing_hashes(
                file_path="all_cached2.py",
                chunk_index=idx,
                content_hash=make_sha256(text),
                vector=[float(idx + 1) * 0.5] * 16,
            )

        chunker = FakeChunker(
            chunks_by_path={
                str(src_file): [
                    {
                        "text": texts[0],
                        "chunk_index": 0,
                        "total_chunks": 2,
                        "file_extension": "py",
                        "line_start": 1,
                        "line_end": 1,
                    },
                    {
                        "text": texts[1],
                        "chunk_index": 1,
                        "total_chunks": 2,
                        "file_extension": "py",
                        "line_start": 2,
                        "line_end": 2,
                    },
                ]
            }
        )

        vector_manager = FakeVectorCalculationManager()
        manager = make_manager(tmp_path, chunker, vector_manager, fake_client)
        metadata = make_metadata()

        slot_tracker = CleanSlotTracker(max_slots=4)
        result = manager._process_file_clean_lifecycle(
            file_path=src_file,
            metadata=metadata,
            progress_callback=None,
            slot_tracker=slot_tracker,
        )

        assert result.success
        assert len(fake_client.upserted_points) == 2, (
            f"Both cached points must be written to storage; "
            f"got {len(fake_client.upserted_points)}"
        )

    def test_all_cached_returns_success_result(self, tmp_path):
        """All-cached scenario must return FileProcessingResult with success=True."""
        chunk_text = "x = 42"
        src_file = tmp_path / "tiny.py"
        src_file.write_text(chunk_text)

        fake_client = FakeFilesystemClient()
        fake_client.seed_existing_hashes(
            file_path="tiny.py",
            chunk_index=0,
            content_hash=make_sha256(chunk_text),
            vector=[0.3] * 16,
        )

        chunker = FakeChunker(
            chunks_by_path={
                str(src_file): [
                    {
                        "text": chunk_text,
                        "chunk_index": 0,
                        "total_chunks": 1,
                        "file_extension": "py",
                        "line_start": 1,
                        "line_end": 1,
                    }
                ]
            }
        )

        vector_manager = FakeVectorCalculationManager()
        manager = make_manager(tmp_path, chunker, vector_manager, fake_client)
        metadata = make_metadata()

        slot_tracker = CleanSlotTracker(max_slots=4)
        result = manager._process_file_clean_lifecycle(
            file_path=src_file,
            metadata=metadata,
            progress_callback=None,
            slot_tracker=slot_tracker,
        )

        assert isinstance(result, FileProcessingResult)
        assert result.success is True
        assert result.error is None
