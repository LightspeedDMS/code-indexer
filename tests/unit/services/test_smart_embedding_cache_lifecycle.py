"""
Unit tests for Story #470: Smart Embedding Cache - lifecycle cache hit/miss logic.

Tests verify that during _process_file_clean_lifecycle:
1. Cache hit: unchanged chunk reuses existing vector (no API call)
2. Cache hit: the cached vector (not a new one) is what gets stored
3. Cache miss: changed chunk goes to VoyageAI API
4. No existing hash: first-time indexing always calls the API
5. Mixed: only uncached chunks sent to API; cached chunks reused

TDD: These tests are written BEFORE implementation. They should fail initially.
"""

# mypy: ignore-errors

from src.code_indexer.services.file_chunking_manager import (
    FileProcessingResult,
)
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


class TestCacheHitSkipsAPI:
    """When chunk content is unchanged, the embedding API must not be called."""

    def test_cache_hit_zero_api_calls(self, tmp_path):
        """Unchanged file content must result in zero VoyageAI batch calls."""
        chunk_text = "def unchanged(): return 42"
        existing_vector = [0.99] * 16
        existing_hash = make_sha256(chunk_text)

        src_file = tmp_path / "src.py"
        src_file.write_text(chunk_text)

        fake_client = FakeFilesystemClient()
        fake_client.seed_existing_hashes(
            file_path="src.py",
            chunk_index=0,
            content_hash=existing_hash,
            vector=existing_vector,
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
                        "line_end": 2,
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

        assert result.success, f"Expected success but got error: {result.error}"
        assert (
            len(vector_manager.batch_calls) == 0
        ), f"Expected 0 batch API calls for cache hit, got {len(vector_manager.batch_calls)}"

    def test_cache_hit_stores_existing_vector(self, tmp_path):
        """Cache hit must store the existing vector, not generate a new one."""
        chunk_text = "x = 1"
        existing_vector = [0.77] * 16
        existing_hash = make_sha256(chunk_text)

        src_file = tmp_path / "var.py"
        src_file.write_text(chunk_text)

        fake_client = FakeFilesystemClient()
        fake_client.seed_existing_hashes(
            file_path="var.py",
            chunk_index=0,
            content_hash=existing_hash,
            vector=existing_vector,
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

        assert result.success
        assert (
            len(fake_client.upserted_points) > 0
        ), "Must have upserted at least one point"
        upserted = fake_client.upserted_points[0]
        assert (
            upserted["vector"] == existing_vector
        ), "Cache hit must reuse the stored vector, not generate a new one"

    def test_cache_hit_result_is_success(self, tmp_path):
        """Cache hit processing must return a successful FileProcessingResult."""
        chunk_text = "pass"
        src_file = tmp_path / "empty.py"
        src_file.write_text(chunk_text)

        fake_client = FakeFilesystemClient()
        fake_client.seed_existing_hashes(
            file_path="empty.py",
            chunk_index=0,
            content_hash=make_sha256(chunk_text),
            vector=[0.1] * 16,
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


class TestCacheMissCallsAPI:
    """When chunk content has changed, the embedding API must be called."""

    def test_changed_content_triggers_api_call(self, tmp_path):
        """Changed chunk text must trigger a VoyageAI embedding API call."""
        old_text = "def old(): pass"
        new_text = "def new(): return 42"

        src_file = tmp_path / "changed.py"
        src_file.write_text(new_text)

        fake_client = FakeFilesystemClient()
        # Seed with OLD hash (content has changed)
        fake_client.seed_existing_hashes(
            file_path="changed.py",
            chunk_index=0,
            content_hash=make_sha256(old_text),
            vector=[0.1] * 16,
        )

        chunker = FakeChunker(
            chunks_by_path={
                str(src_file): [
                    {
                        "text": new_text,
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

        assert result.success, f"Processing failed: {result.error}"
        assert (
            len(vector_manager.batch_calls) > 0
        ), "Cache miss must trigger at least one embedding API call"
        all_sent = [t for batch in vector_manager.batch_calls for t in batch]
        assert new_text in all_sent, "New (changed) text must be sent to the API"

    def test_first_time_indexing_calls_api(self, tmp_path):
        """First-time indexing (no existing hash) must call the embedding API."""
        chunk_text = "brand new file content"
        src_file = tmp_path / "new_file.py"
        src_file.write_text(chunk_text)

        fake_client = FakeFilesystemClient()
        # No seeds - first time indexing

        chunker = FakeChunker(
            chunks_by_path={
                str(src_file): [
                    {
                        "text": chunk_text,
                        "chunk_index": 0,
                        "total_chunks": 1,
                        "file_extension": "py",
                        "line_start": 1,
                        "line_end": 2,
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

        assert result.success
        assert (
            len(vector_manager.batch_calls) > 0
        ), "First-time indexing must always call the embedding API"


class TestMixedCacheAndEmbedding:
    """When a file has mixed cached/uncached chunks, only uncached chunks hit the API."""

    def test_only_changed_chunks_sent_to_api(self, tmp_path):
        """Unchanged chunks are reused; only changed chunks go to the embedding API."""
        unchanged_text = "def stable(): return 1"
        changed_text = "def volatile(): return 999"

        src_file = tmp_path / "mixed.py"
        src_file.write_text(unchanged_text + "\n" + changed_text)

        fake_client = FakeFilesystemClient()
        # Chunk 0 is cached (unchanged)
        fake_client.seed_existing_hashes(
            file_path="mixed.py",
            chunk_index=0,
            content_hash=make_sha256(unchanged_text),
            vector=[0.42] * 16,
        )
        # Chunk 1 has an OLD hash (content changed)
        fake_client.seed_existing_hashes(
            file_path="mixed.py",
            chunk_index=1,
            content_hash=make_sha256("old content for chunk 1"),
            vector=[0.11] * 16,
        )

        chunker = FakeChunker(
            chunks_by_path={
                str(src_file): [
                    {
                        "text": unchanged_text,
                        "chunk_index": 0,
                        "total_chunks": 2,
                        "file_extension": "py",
                        "line_start": 1,
                        "line_end": 1,
                    },
                    {
                        "text": changed_text,
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

        assert result.success, f"Processing failed: {result.error}"

        all_sent = [t for batch in vector_manager.batch_calls for t in batch]
        assert changed_text in all_sent, "Changed chunk must be embedded via API"
        assert (
            unchanged_text not in all_sent
        ), "Unchanged chunk must NOT be re-embedded (cache hit)"

    def test_mixed_both_points_upserted(self, tmp_path):
        """Both cached and newly-embedded points must be written to the store."""
        unchanged_text = "def stable(): return 1"
        changed_text = "def volatile(): return 999"

        src_file = tmp_path / "mixed2.py"
        src_file.write_text(unchanged_text + "\n" + changed_text)

        fake_client = FakeFilesystemClient()
        fake_client.seed_existing_hashes(
            file_path="mixed2.py",
            chunk_index=0,
            content_hash=make_sha256(unchanged_text),
            vector=[0.42] * 16,
        )
        fake_client.seed_existing_hashes(
            file_path="mixed2.py",
            chunk_index=1,
            content_hash=make_sha256("old chunk 1"),
            vector=[0.11] * 16,
        )

        chunker = FakeChunker(
            chunks_by_path={
                str(src_file): [
                    {
                        "text": unchanged_text,
                        "chunk_index": 0,
                        "total_chunks": 2,
                        "file_extension": "py",
                        "line_start": 1,
                        "line_end": 1,
                    },
                    {
                        "text": changed_text,
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

        assert result.success, f"Processing failed: {result.error}"
        assert len(fake_client.upserted_points) == 2, (
            f"Both chunks (1 cached + 1 embedded) must be upserted; "
            f"got {len(fake_client.upserted_points)}"
        )
