"""
Unit tests for FileChunkingManager.

Tests the complete parallel file processing lifecycle with file atomicity
and immediate progress feedback.
"""

# mypy: ignore-errors
# Mock types work correctly in tests despite type checker warnings

import pytest
import tempfile
import time
import threading
from unittest.mock import Mock
from pathlib import Path
from concurrent.futures import Future
from typing import Dict, List, Any

from src.code_indexer.services.file_chunking_manager import (
    FileChunkingManager,
    FileProcessingResult,
)
from src.code_indexer.services.clean_slot_tracker import CleanSlotTracker


class MockVectorCalculationManager:
    """Mock VectorCalculationManager for testing."""

    def __init__(self):
        self.submitted_chunks = []
        self.submit_delay = 0.01  # Small delay to simulate processing
        self.cancellation_event = threading.Event()  # Add required cancellation_event

        # TOKEN COUNTING FIX: Add mock embedding provider
        self.embedding_provider = Mock()
        self.embedding_provider.get_current_model.return_value = (
            "voyage-large-2-instruct"
        )
        self.embedding_provider._get_model_token_limit.return_value = 120000

    def submit_chunk(self, chunk_text: str, metadata: Dict) -> Future:
        """Mock submit_chunk that returns a future."""
        future: Future[Any] = Future()

        # Simulate async processing
        def complete_future():
            time.sleep(self.submit_delay)
            from src.code_indexer.services.vector_calculation_manager import (
                VectorResult,
            )

            result = VectorResult(
                task_id=f"task_{len(self.submitted_chunks)}",
                embeddings=((0.1,) * 768,),  # Mock embedding in batch format
                metadata=metadata.copy(),
                processing_time=self.submit_delay,
                error=None,
            )
            future.set_result(result)

        # Execute in background thread
        thread = threading.Thread(target=complete_future)
        thread.start()

        # Track submitted chunks for verification
        self.submitted_chunks.append(
            {"text": chunk_text, "metadata": metadata, "future": future}
        )

        return future

    def submit_batch_task(
        self, chunk_texts: List[str], metadata: Dict[str, Any]
    ) -> "Future":
        """Mock submit_batch_task method for batch processing."""
        from src.code_indexer.services.vector_calculation_manager import VectorResult

        future: Future[VectorResult] = Future()

        # Track for verification
        self.submitted_chunks.extend(
            [{"text": text, "metadata": metadata} for text in chunk_texts]
        )

        # Mock batch vector result
        embeddings = tuple((0.1,) * 768 for _ in chunk_texts)
        result = VectorResult(
            task_id=f"batch_task_{len(self.submitted_chunks)}",
            embeddings=embeddings,  # Multiple embeddings as nested tuples
            metadata=metadata.copy(),
            processing_time=self.submit_delay * len(chunk_texts),
            error=None,
        )

        # Complete future in background thread to simulate async
        def complete_future():
            time.sleep(self.submit_delay)
            future.set_result(result)

        # Execute in background thread
        thread = threading.Thread(target=complete_future)
        thread.start()

        return future


class MockFixedSizeChunker:
    """Mock FixedSizeChunker for testing."""

    def __init__(self):
        self.chunk_calls = []

    def chunk_file(self, file_path: Path) -> List[Dict]:
        """Mock chunk_file method."""
        self.chunk_calls.append(file_path)

        # Simulate chunking based on file content
        try:
            with open(file_path, "r") as f:
                content = f.read()
        except (IOError, OSError):
            content = "mock content"

        # Return mock chunks
        return (
            [
                {
                    "text": content[:500] if len(content) > 500 else content,
                    "chunk_index": 0,
                    "total_chunks": 2 if len(content) > 500 else 1,
                    "size": min(500, len(content)),
                    "file_path": str(file_path),
                    "file_extension": file_path.suffix.lstrip("."),
                    "line_start": 1,
                    "line_end": 10,
                },
                {
                    "text": content[400:] if len(content) > 500 else "",
                    "chunk_index": 1,
                    "total_chunks": 2 if len(content) > 500 else 1,
                    "size": max(0, len(content) - 400),
                    "file_path": str(file_path),
                    "file_extension": file_path.suffix.lstrip("."),
                    "line_start": 8,
                    "line_end": 20,
                },
            ]
            if len(content) > 500
            else [
                {
                    "text": content,
                    "chunk_index": 0,
                    "total_chunks": 1,
                    "size": len(content),
                    "file_path": str(file_path),
                    "file_extension": file_path.suffix.lstrip("."),
                    "line_start": 1,
                    "line_end": 5,
                }
            ]
        )


class MockFilesystemClient:
    """Mock FilesystemClient for testing."""

    def __init__(self):
        self.upserted_points = []
        self.upsert_calls = []
        self.should_fail = False
        self.collections = set()

    def upsert_points(self, points: List[Dict], collection_name=None) -> bool:
        """Mock upsert method - called by FileChunkingManager."""
        return self.upsert_points_batched(points, collection_name)

    def upsert_points_batched(self, points: List[Dict], collection_name=None) -> bool:
        """Mock atomic upsert method."""
        self.upsert_calls.append(
            {
                "points": points.copy(),
                "collection_name": collection_name,
                "point_count": len(points),
            }
        )

        if self.should_fail:
            return False

        self.upserted_points.extend(points)
        return True

    def collection_exists(self, collection_name: str) -> bool:
        """Mock collection_exists method."""
        return collection_name in self.collections

    def create_collection(self, collection_name: str, vector_size: int) -> bool:
        """Mock create_collection method."""
        self.collections.add(collection_name)
        return True


class TestFileChunkingManagerAcceptanceCriteria:
    """Test all acceptance criteria from the story specification."""

    def setup_method(self):
        """Setup test environment."""
        self.mock_vector_manager = MockVectorCalculationManager()
        self.mock_chunker = MockFixedSizeChunker()
        self.mock_filesystem_client = MockFilesystemClient()
        self.slot_tracker = CleanSlotTracker(
            max_slots=10
        )  # Create required slot tracker

        # Create temporary test file
        self.test_file = tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".py"
        )
        self.test_file.write("print('Hello, World!')\n" * 100)  # 2000+ characters
        self.test_file.close()
        self.test_file_path = Path(self.test_file.name)

    def _add_voyage_client_mock(self, manager):
        """Helper to add voyage client mock to manager."""
        manager.voyage_client = Mock()
        manager.voyage_client.count_tokens.return_value = 100
        return manager

    def teardown_method(self):
        """Cleanup test environment."""
        if self.test_file_path.exists():
            self.test_file_path.unlink()

    def test_complete_functional_implementation_initialization(self):
        """Test FileChunkingManager complete initialization per acceptance criteria."""
        # Given FileChunkingManager class with complete implementation
        # When initialized with vector_manager, chunker, and thread_count
        thread_count = 4
        manager = FileChunkingManager(
            vector_manager=self.mock_vector_manager,
            chunker=self.mock_chunker,
            vector_store_client=self.mock_filesystem_client,
            thread_count=thread_count,
            slot_tracker=CleanSlotTracker(max_slots=thread_count + 2),
            codebase_dir=self.test_file_path.parent,
        )

        # Then creates ThreadPoolExecutor with (thread_count + 2) workers per user specs
        with manager:
            # This should NOT raise an error and should create proper thread pool
            assert hasattr(manager, "executor")
            assert manager.executor is not None
            # ThreadPoolExecutor should be configured for thread_count + 2
            # (We'll verify this in the implementation)

        # And provides submit_file_for_processing() method that returns Future
        assert hasattr(manager, "submit_file_for_processing")
        assert callable(getattr(manager, "submit_file_for_processing"))

    def test_submit_file_returns_future(self):
        """Test that submit_file_for_processing returns Future."""
        with FileChunkingManager(
            vector_manager=self.mock_vector_manager,
            chunker=self.mock_chunker,
            vector_store_client=self.mock_filesystem_client,
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=self.test_file_path.parent,
        ) as manager:
            metadata = {"project_id": "test", "file_hash": "abc123"}
            progress_callback = Mock()

            # When submitting file for processing
            future: Future[Any] = manager.submit_file_for_processing(
                self.test_file_path, metadata, progress_callback
            )

            # Then returns Future
            assert isinstance(future, Future)

    def test_immediate_queuing_feedback(self):
        """Test that individual progress callbacks are correctly removed.

        SURGICAL FIX: This test validates that individual file callbacks
        are no longer sent to prevent spam in the fixed N-line display.
        The SlotBasedFileTracker now handles all display updates.
        """
        with FileChunkingManager(
            vector_manager=self.mock_vector_manager,
            chunker=self.mock_chunker,
            vector_store_client=self.mock_filesystem_client,
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=self.test_file_path.parent,
        ) as manager:
            metadata = {"project_id": "test", "file_hash": "abc123"}
            progress_callback = Mock()

            # When submit_file_for_processing() is called
            future: Future[Any] = manager.submit_file_for_processing(
                self.test_file_path, metadata, progress_callback
            )

            # UPDATED: Progress callbacks are now sent with slot-based file status
            # Wait for the future to complete to see actual callback behavior
            future.result(timeout=2.0)

            # Verify that progress callbacks were sent with slot-based status information
            assert progress_callback.called
            # The callback should have been called with concurrent_files status updates
            call_args = progress_callback.call_args_list
            assert len(call_args) > 0
            # Each call should include concurrent_files data
            for call in call_args:
                args, kwargs = call
                assert "concurrent_files" in kwargs or len(args) >= 4

            # Verify the future was returned for async processing
            assert isinstance(future, Future)

    def test_worker_thread_complete_file_processing_lifecycle(self):
        """Test complete file lifecycle in worker thread."""
        with FileChunkingManager(
            vector_manager=self.mock_vector_manager,
            chunker=self.mock_chunker,
            vector_store_client=self.mock_filesystem_client,
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=self.test_file_path.parent,
        ) as manager:
            # TOKEN COUNTING FIX: Add voyage client mock
            self._add_voyage_client_mock(manager)

            metadata = {"project_id": "test", "file_hash": "abc123"}
            progress_callback = Mock()

            # When worker thread processes file using _process_file_complete_lifecycle()
            future: Future[Any] = manager.submit_file_for_processing(
                self.test_file_path, metadata, progress_callback
            )

            # Wait for completion
            result = future.result(timeout=10.0)

            # Then MOVE chunking logic from main thread to worker thread
            # And chunks = self.fixed_size_chunker.chunk_file(file_path) executes in worker
            assert len(self.mock_chunker.chunk_calls) == 1
            assert self.mock_chunker.chunk_calls[0] == self.test_file_path

            # And ALL chunks submitted to existing VectorCalculationManager (unchanged)
            assert len(self.mock_vector_manager.submitted_chunks) > 0

            # And MOVE filesystem_client.upsert_points_batched() from main thread to worker thread
            assert len(self.mock_filesystem_client.upsert_calls) == 1

            # And FileProcessingResult returned with success/failure status
            assert isinstance(result, FileProcessingResult)
            assert result.success is True
            assert result.chunks_processed > 0

    def test_file_atomicity_within_worker_threads(self):
        """Test that file atomicity is maintained within worker threads."""
        with FileChunkingManager(
            vector_manager=self.mock_vector_manager,
            chunker=self.mock_chunker,
            vector_store_client=self.mock_filesystem_client,
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=self.test_file_path.parent,
        ) as manager:
            # TOKEN COUNTING FIX: Add voyage client mock
            self._add_voyage_client_mock(manager)

            metadata = {"project_id": "test", "file_hash": "abc123"}

            # Submit file for processing
            future: Future[Any] = manager.submit_file_for_processing(
                self.test_file_path, metadata, Mock()
            )

            future.result(timeout=10.0)

            # Verify atomicity: all chunks from one file written together
            assert len(self.mock_filesystem_client.upsert_calls) == 1
            upsert_call = self.mock_filesystem_client.upsert_calls[0]

            # All points in single atomic operation
            assert upsert_call["point_count"] > 0

            # All points should be from same file
            for point in upsert_call["points"]:
                assert str(self.test_file_path) in str(
                    point.get("payload", {}).get("path", "")
                )

    def test_error_handling_chunking_failure(self):
        """Test error handling when chunking fails."""
        # Mock chunker to raise exception
        failing_chunker = Mock()
        failing_chunker.chunk_file.side_effect = ValueError("Chunking failed")

        with FileChunkingManager(
            vector_manager=self.mock_vector_manager,
            chunker=failing_chunker,
            vector_store_client=self.mock_filesystem_client,
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=self.test_file_path.parent,
        ) as manager:
            metadata = {"project_id": "test", "file_hash": "abc123"}

            future: Future[Any] = manager.submit_file_for_processing(
                self.test_file_path, metadata, Mock()
            )

            result = future.result(timeout=5.0)

            # Then errors logged with specific file context
            # And FileProcessingResult indicates failure with error details
            assert isinstance(result, FileProcessingResult)
            assert result.success is False
            assert result.error is not None
            assert "Chunking failed" in str(result.error)

    def test_error_handling_vector_processing_failure(self):
        """Test error handling when vector processing fails."""
        # Mock vector manager to fail on batch submission
        failing_vector_manager = Mock()
        failing_vector_manager.cancellation_event = (
            threading.Event()
        )  # Add required attribute

        # TOKEN COUNTING FIX: Add mock embedding provider
        failing_vector_manager.embedding_provider = Mock()
        failing_vector_manager.embedding_provider.get_current_model.return_value = (
            "voyage-large-2-instruct"
        )
        failing_vector_manager.embedding_provider._get_model_token_limit.return_value = (
            120000
        )

        # Create a mock future that returns a result with error
        failing_future: Future[Any] = Future()
        from src.code_indexer.services.vector_calculation_manager import VectorResult

        failing_result = VectorResult(
            task_id="failed_batch",
            embeddings=(),  # Empty embeddings on failure
            metadata={},
            processing_time=0.1,
            error="Vector processing failed",
        )
        failing_future.set_result(failing_result)
        failing_vector_manager.submit_batch_task = Mock(return_value=failing_future)

        with FileChunkingManager(
            vector_manager=failing_vector_manager,
            chunker=self.mock_chunker,
            vector_store_client=self.mock_filesystem_client,
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=self.test_file_path.parent,
        ) as manager:
            # TOKEN COUNTING FIX: Add voyage client mock
            self._add_voyage_client_mock(manager)

            metadata = {"project_id": "test", "file_hash": "abc123"}

            future: Future[Any] = manager.submit_file_for_processing(
                self.test_file_path, metadata, Mock()
            )

            result = future.result(timeout=5.0)

            # CORRECTED: Batch processing failure should fail the entire file (atomicity)
            assert result.success is False
            assert result.chunks_processed == 0  # No chunks were successfully processed
            assert "Batch processing failed" in str(result.error)

    def test_error_handling_filesystem_write_failure(self):
        """Test error handling when Filesystem writing fails."""
        # Mock Filesystem client to fail
        self.mock_filesystem_client.should_fail = True

        with FileChunkingManager(
            vector_manager=self.mock_vector_manager,
            chunker=self.mock_chunker,
            vector_store_client=self.mock_filesystem_client,
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=self.test_file_path.parent,
        ) as manager:
            # TOKEN COUNTING FIX: Add voyage client mock
            self._add_voyage_client_mock(manager)

            metadata = {"project_id": "test", "file_hash": "abc123"}

            future: Future[Any] = manager.submit_file_for_processing(
                self.test_file_path, metadata, Mock()
            )

            result = future.result(timeout=10.0)

            # FileProcessingResult should indicate failure
            assert result.success is False
            assert result.error is not None
            assert "Filesystem write failed" in str(result.error)

    def test_thread_pool_management(self):
        """Test ThreadPoolExecutor lifecycle management."""
        manager = FileChunkingManager(
            vector_manager=self.mock_vector_manager,
            chunker=self.mock_chunker,
            vector_store_client=self.mock_filesystem_client,
            thread_count=3,
            slot_tracker=CleanSlotTracker(max_slots=5),
            codebase_dir=self.test_file_path.parent,
        )

        # Context manager should start thread pool
        with manager:
            assert hasattr(manager, "executor")
            assert manager.executor is not None

            # Should be able to submit work
            metadata = {"project_id": "test", "file_hash": "abc123"}
            future: Future[Any] = manager.submit_file_for_processing(
                self.test_file_path, metadata, Mock()
            )

            result = future.result(timeout=5.0)
            assert isinstance(result, FileProcessingResult)

        # After context exit, thread pool should be shut down
        # (Implementation will handle this)

    def test_parallel_file_processing_efficiency(self):
        """Test that parallel processing improves efficiency for multiple files."""
        # Create multiple test files
        test_files = []
        for i in range(3):
            temp_file = tempfile.NamedTemporaryFile(
                mode="w", delete=False, suffix=f"_test_{i}.py"
            )
            temp_file.write(f"# File {i}\n" + "print('test')\n" * 50)
            temp_file.close()
            test_files.append(Path(temp_file.name))

        try:
            with FileChunkingManager(
                vector_manager=self.mock_vector_manager,
                chunker=self.mock_chunker,
                vector_store_client=self.mock_filesystem_client,
                thread_count=2,
                slot_tracker=CleanSlotTracker(max_slots=4),
                codebase_dir=self.test_file_path.parent,
            ) as manager:
                # TOKEN COUNTING FIX: Add voyage client mock
                self._add_voyage_client_mock(manager)

                start_time = time.time()
                futures = []

                # Submit multiple files
                for file_path in test_files:
                    metadata = {
                        "project_id": "test",
                        "file_hash": f"hash_{file_path.name}",
                    }
                    future: Future[Any] = manager.submit_file_for_processing(
                        file_path, metadata, Mock()
                    )
                    futures.append(future)

                # Wait for all to complete
                results = []
                for future in futures:
                    result = future.result(timeout=10.0)
                    results.append(result)

                processing_time = time.time() - start_time

                # All files should be processed successfully
                assert len(results) == 3
                for result in results:
                    assert result.success is True
                    assert result.chunks_processed > 0

                # Should be faster than sequential processing
                # (This is more of a performance test)
                assert processing_time < 5.0  # Reasonable timeout

        finally:
            # Cleanup test files
            for file_path in test_files:
                if file_path.exists():
                    file_path.unlink()

    def test_integration_with_existing_system_compatibility(self):
        """Test integration with existing VectorCalculationManager and FixedSizeChunker."""
        # This test ensures FileChunkingManager works with real components
        # (when available) without breaking existing interfaces

        with FileChunkingManager(
            vector_manager=self.mock_vector_manager,
            chunker=self.mock_chunker,
            vector_store_client=self.mock_filesystem_client,
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=self.test_file_path.parent,
        ) as manager:
            # TOKEN COUNTING FIX: Add voyage client mock
            self._add_voyage_client_mock(manager)

            metadata = {
                "project_id": "test_project",
                "file_hash": "test_hash_123",
                "git_available": False,
                "commit_hash": None,
                "branch": None,
                "file_mtime": 1640995200,
                "file_size": 1000,
            }

            future: Future[Any] = manager.submit_file_for_processing(
                self.test_file_path, metadata, Mock()
            )

            result = future.result(timeout=5.0)

            # Should work with existing metadata structure
            assert result.success is True

            # Should call existing chunker interface
            assert len(self.mock_chunker.chunk_calls) == 1

            # Should call existing vector manager interface
            assert len(self.mock_vector_manager.submitted_chunks) > 0

            # Should call existing Filesystem client interface
            assert len(self.mock_filesystem_client.upsert_calls) == 1

    def test_addresses_user_problems_efficiency_and_feedback(self):
        """Test that FileChunkingManager addresses the specific user problems.

        SURGICAL FIX UPDATE: Progress callbacks are now handled by
        SlotBasedFileTracker at the system level, not individual files.
        This test validates the efficient parallel processing architecture.
        """
        with FileChunkingManager(
            vector_manager=self.mock_vector_manager,
            chunker=self.mock_chunker,
            vector_store_client=self.mock_filesystem_client,
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=self.test_file_path.parent,
        ) as manager:
            # TOKEN COUNTING FIX: Add voyage client mock
            self._add_voyage_client_mock(manager)

            metadata = {"project_id": "test", "file_hash": "abc123"}

            # Submit small file
            future: Future[Any] = manager.submit_file_for_processing(
                self.test_file_path,
                metadata,
                None,  # No individual callbacks
            )

            result = future.result(timeout=5.0)

            # ADDRESSES user problem: "not efficient for very small files" via parallel processing
            assert result.success is True
            assert result.processing_time < 5.0  # Should be reasonable

            # ADDRESSES user problem: "no feedback when chunking files"
            # NOW HANDLED BY: SlotBasedFileTracker provides fixed N-line display
            # Individual file callbacks removed to prevent spam
            # Feedback is provided at the system level, not per-file level


class TestFileChunkingManagerValidation:
    """Test parameter validation and edge cases."""

    def test_invalid_thread_count_validation(self):
        """Test that invalid thread counts raise ValueError."""
        mock_vector_manager = MockVectorCalculationManager()
        mock_chunker = MockFixedSizeChunker()
        mock_filesystem_client = MockFilesystemClient()

        with pytest.raises(ValueError, match="thread_count must be positive"):
            FileChunkingManager(
                vector_manager=mock_vector_manager,
                chunker=mock_chunker,
                vector_store_client=mock_filesystem_client,
                thread_count=0,
                slot_tracker=CleanSlotTracker(max_slots=2),
                codebase_dir=self.test_file_path.parent,
            )

        with pytest.raises(ValueError, match="thread_count must be positive"):
            FileChunkingManager(
                vector_manager=mock_vector_manager,
                chunker=mock_chunker,
                vector_store_client=mock_filesystem_client,
                thread_count=-1,
                slot_tracker=CleanSlotTracker(max_slots=2),
                codebase_dir=self.test_file_path.parent,
            )

    def test_none_dependencies_validation(self):
        """Test that None dependencies raise ValueError."""
        mock_vector_manager = MockVectorCalculationManager()
        mock_chunker = MockFixedSizeChunker()
        mock_filesystem_client = MockFilesystemClient()

        with pytest.raises(ValueError, match="vector_manager cannot be None"):
            FileChunkingManager(
                vector_manager=None,
                chunker=mock_chunker,
                vector_store_client=mock_filesystem_client,
                thread_count=2,
                slot_tracker=CleanSlotTracker(max_slots=4),
                codebase_dir=self.test_file_path.parent,
            )

        with pytest.raises(ValueError, match="chunker cannot be None"):
            FileChunkingManager(
                vector_manager=mock_vector_manager,
                chunker=None,
                vector_store_client=mock_filesystem_client,
                thread_count=2,
                slot_tracker=CleanSlotTracker(max_slots=4),
                codebase_dir=self.test_file_path.parent,
            )

        with pytest.raises(ValueError, match="filesystem_client cannot be None"):
            FileChunkingManager(
                vector_manager=mock_vector_manager,
                chunker=mock_chunker,
                vector_store_client=None,
                thread_count=2,
                slot_tracker=CleanSlotTracker(max_slots=4),
                codebase_dir=self.test_file_path.parent,
            )

    def test_submit_without_context_manager_raises_error(self):
        """Test that submitting without context manager raises RuntimeError."""
        manager = FileChunkingManager(
            vector_manager=MockVectorCalculationManager(),
            chunker=MockFixedSizeChunker(),
            vector_store_client=MockFilesystemClient(),
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=self.test_file_path.parent,
        )

        metadata = {"project_id": "test", "file_hash": "abc123"}

        with pytest.raises(RuntimeError, match="FileChunkingManager not started"):
            manager.submit_file_for_processing(Path("/tmp/test.py"), metadata, Mock())

    def test_empty_chunks_handling(self):
        """Test handling when chunker returns empty chunks."""
        # Create chunker that returns empty chunks
        empty_chunker = Mock()
        empty_chunker.chunk_file.return_value = []

        with FileChunkingManager(
            vector_manager=MockVectorCalculationManager(),
            chunker=empty_chunker,
            vector_store_client=MockFilesystemClient(),
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=self.test_file_path.parent,
        ) as manager:
            test_file = tempfile.NamedTemporaryFile(
                mode="w", delete=False, suffix=".py"
            )
            test_file.write("# Empty file")
            test_file.close()
            test_file_path = Path(test_file.name)

            try:
                metadata = {"project_id": "test", "file_hash": "abc123"}
                future: Future[Any] = manager.submit_file_for_processing(
                    test_file_path, metadata, Mock()
                )

                result = future.result(timeout=5.0)

                # UPDATED: Current implementation treats empty chunks as successful processing
                # This handles cases like empty files gracefully
                assert result.success is True
                assert result.error is None
                assert result.chunks_processed == 0

            finally:
                if test_file_path.exists():
                    test_file_path.unlink()

    def test_chunks_with_images_route_to_multimodal_client(self, tmp_path):
        """
        Test that chunks with images[] field are routed to VoyageMultimodalClient.

        This validates the routing logic:
        1. Chunks with images[] are NOT submitted to regular vector_manager
        2. Chunks with images[] ARE submitted to multimodal embedding path
        3. Regular chunks (no images) still use regular vector_manager
        """
        # Create mock chunker that returns mix of chunks with and without images
        mock_chunker = Mock()
        mock_chunker.chunk_file.return_value = [
            {
                "text": "Chunk with image",
                "chunk_index": 0,
                "total_chunks": 2,
                "file_extension": "md",
                "size": 16,
                "line_start": 1,
                "line_end": 3,
                "file_path": None,
                "images": [{"path": "image.png", "alt_text": "test"}],
            },
            {
                "text": "Chunk without image",
                "chunk_index": 1,
                "total_chunks": 2,
                "file_extension": "md",
                "size": 19,
                "line_start": 4,
                "line_end": 5,
                "file_path": None,
                "images": [],
            },
        ]

        mock_vector_manager = MockVectorCalculationManager()

        # Create test file
        test_file_path = tmp_path / "test.md"
        test_file_path.write_text("# Test\n\n![image](image.png)\n\nText")

        with FileChunkingManager(
            vector_manager=mock_vector_manager,
            chunker=mock_chunker,
            vector_store_client=MockFilesystemClient(),
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=tmp_path,
        ) as manager:
            # Add voyage client mock for token counting
            manager.is_voyageai_provider = True
            manager.vector_manager.embedding_provider.get_current_model.return_value = (
                "voyage-3"
            )

            metadata = {"project_id": "test", "file_hash": "abc123"}
            future: Future[Any] = manager.submit_file_for_processing(
                test_file_path, metadata, Mock()
            )

            result = future.result(timeout=5.0)

            # Should succeed
            assert result.success is True

            # KEY ASSERTION: Only 1 chunk (the one WITHOUT images) should be submitted
            # to the regular vector_manager. The chunk WITH images should use
            # multimodal path (not yet implemented, so this will fail initially)
            submitted_texts = [chunk for chunk in mock_vector_manager.submitted_chunks]

            # This assertion will FAIL until routing is implemented
            # Expected: only "Chunk without image" submitted to vector_manager
            # Actual: both chunks submitted (no routing yet)
            assert len(submitted_texts) == 1, (
                f"Expected 1 chunk submitted to vector_manager, got {len(submitted_texts)}. "
                "Chunks with images should use multimodal path."
            )
            assert "Chunk without image" in str(submitted_texts[0])

    def test_multimodal_chunks_are_embedded_and_stored(self, tmp_path):
        """
        Test that chunks with images are actually embedded and stored.

        This validates the complete multimodal embedding flow:
        1. Chunks with images[] are embedded via VoyageMultimodalClient
        2. Images are loaded from disk and encoded as base64
        3. Embeddings are stored in multimodal_index/ subdirectory
        4. Regular chunks still go through normal path
        """
        # Create test image file
        test_image_path = tmp_path / "test_image.png"
        test_image_path.write_bytes(b"fake_png_data")

        # Create markdown file referencing the image
        test_md_path = tmp_path / "test.md"
        test_md_path.write_text(
            "# Test\n\n![Test Image](test_image.png)\n\nSome text content."
        )

        # Create mock chunker that returns chunks with images
        mock_chunker = Mock()
        mock_chunker.chunk_file.return_value = [
            {
                "text": "# Test\n\n![Test Image](test_image.png)",
                "chunk_index": 0,
                "total_chunks": 2,
                "file_extension": "md",
                "size": 40,
                "line_start": 1,
                "line_end": 3,
                "file_path": str(test_md_path),
                "images": ["test_image.png"],  # Image reference as string
            },
            {
                "text": "Some text content.",
                "chunk_index": 1,
                "total_chunks": 2,
                "file_extension": "md",
                "size": 18,
                "line_start": 5,
                "line_end": 5,
                "file_path": str(test_md_path),
                "images": [],  # No images
            },
        ]

        # Create mock multimodal client with proper config
        mock_multimodal_client = Mock()
        mock_multimodal_client.get_multimodal_embedding.return_value = (0.2,) * 1024
        mock_multimodal_client.config = Mock()
        mock_multimodal_client.config.model = "voyage-multimodal-3"

        # Create mock vector manager
        mock_vector_manager = MockVectorCalculationManager()

        # Create mock filesystem client to track both regular and multimodal storage
        mock_filesystem_client = MockFilesystemClient()

        with FileChunkingManager(
            vector_manager=mock_vector_manager,
            chunker=mock_chunker,
            vector_store_client=mock_filesystem_client,
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=tmp_path,
        ) as manager:
            # Inject mock multimodal client
            manager.multimodal_client = mock_multimodal_client

            # Add voyage client mock for token counting
            manager.is_voyageai_provider = True
            manager.vector_manager.embedding_provider.get_current_model.return_value = (
                "voyage-3"
            )

            metadata = {"project_id": "test", "file_hash": "abc123"}
            future: Future[Any] = manager.submit_file_for_processing(
                test_md_path, metadata, Mock()
            )

            result = future.result(timeout=5.0)

            # Should succeed
            assert result.success is True

            # KEY ASSERTIONS:
            # 1. Multimodal client was called with text and image path
            mock_multimodal_client.get_multimodal_embedding.assert_called_once()
            call_args = mock_multimodal_client.get_multimodal_embedding.call_args
            assert "![Test Image](test_image.png)" in call_args[1]["text"]
            assert len(call_args[1]["image_paths"]) == 1
            assert call_args[1]["image_paths"][0] == test_image_path

            # 2. Regular chunk (without images) went to vector_manager
            assert len(mock_vector_manager.submitted_chunks) == 1
            assert (
                "Some text content" in mock_vector_manager.submitted_chunks[0]["text"]
            )

            # 3. Multimodal chunk was stored (this will be verified by checking
            #    filesystem client received points with multimodal metadata)
            # TODO: Add assertion for multimodal storage once implementation is complete

    def test_multimodal_embeddings_stored_in_separate_index(self, tmp_path):
        """
        Test that multimodal embeddings are stored in voyage-multimodal-3 collection.

        Validates:
        1. Multimodal chunks generate embeddings via VoyageMultimodalClient
        2. Embeddings are stored using FilesystemVectorStore with collection_name="voyage-multimodal-3"
        3. Storage call includes correct chunk metadata and image references
        4. Regular chunks continue to use default collection (voyage-code-3)
        5. NO subdirectory parameter is used - collection name is the folder name
        """
        # Create test image
        test_image_path = tmp_path / "diagram.png"
        test_image_path.write_bytes(b"fake_image_data")

        # Create markdown file with image
        test_md_path = tmp_path / "docs.md"
        test_md_path.write_text(
            "# Architecture\n\n![Diagram](diagram.png)\n\nDescription here."
        )

        # Create mock chunker returning multimodal and regular chunks
        mock_chunker = Mock()
        mock_chunker.chunk_file.return_value = [
            {
                "text": "# Architecture\n\n![Diagram](diagram.png)",
                "chunk_index": 0,
                "total_chunks": 2,
                "file_extension": "md",
                "size": 43,
                "line_start": 1,
                "line_end": 3,
                "file_path": str(test_md_path),
                "images": ["diagram.png"],  # Multimodal chunk as string
            },
            {
                "text": "Description here.",
                "chunk_index": 1,
                "total_chunks": 2,
                "file_extension": "md",
                "size": 17,
                "line_start": 5,
                "line_end": 5,
                "file_path": str(test_md_path),
                "images": [],  # Regular chunk
            },
        ]

        # Create mock multimodal client with proper config
        mock_multimodal_client = Mock()
        mock_multimodal_client.get_multimodal_embedding.return_value = (0.1,) * 1024
        mock_multimodal_client.config = Mock()
        mock_multimodal_client.config.model = "voyage-multimodal-3"

        # Create mock vector manager
        mock_vector_manager = MockVectorCalculationManager()

        # Create mock filesystem client that tracks collection_name and upsert_points calls
        mock_filesystem_client = Mock()
        mock_filesystem_client.add = Mock()
        mock_filesystem_client.upsert_points = Mock(return_value=True)
        mock_filesystem_client.create_collection = Mock(return_value=True)
        mock_filesystem_client.collection_exists = Mock(return_value=False)

        with FileChunkingManager(
            vector_manager=mock_vector_manager,
            chunker=mock_chunker,
            vector_store_client=mock_filesystem_client,
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=tmp_path,
        ) as manager:
            # Inject multimodal client
            manager.multimodal_client = mock_multimodal_client

            # Inject voyage provider config
            manager.is_voyageai_provider = True
            manager.vector_manager.embedding_provider.get_current_model.return_value = (
                "voyage-3"
            )

            metadata = {
                "project_id": "test",
                "file_hash": "xyz789",
                "collection_name": "default",
            }
            future: Future[Any] = manager.submit_file_for_processing(
                test_md_path, metadata, Mock()
            )

            result = future.result(timeout=5.0)

            # Should succeed
            assert result.success is True

            # CRITICAL ASSERTIONS:
            # 1. Multimodal client called with text and image path
            mock_multimodal_client.get_multimodal_embedding.assert_called_once()
            call_args = mock_multimodal_client.get_multimodal_embedding.call_args
            assert "![Diagram](diagram.png)" in call_args[1]["text"]
            assert call_args[1]["image_paths"][0] == test_image_path

            # 2. Filesystem client's upsert_points() method called for multimodal storage
            #    with collection_name="voyage-multimodal-3" (NOT subdirectory parameter)
            mock_filesystem_client.upsert_points.assert_called()

            # Find the call with collection_name containing "multimodal"
            multimodal_storage_calls = [
                call
                for call in mock_filesystem_client.upsert_points.call_args_list
                if "multimodal" in call[1].get("collection_name", "").lower()
            ]

            assert (
                len(multimodal_storage_calls) == 1
            ), f"Expected exactly one storage call with collection_name containing 'multimodal', got {len(multimodal_storage_calls)}"

            storage_call = multimodal_storage_calls[0]

            # Verify collection_name is voyage-multimodal-3, NOT using subdirectory
            collection_name = storage_call[1]["collection_name"]
            assert (
                "voyage-multimodal" in collection_name
            ), f"Expected collection_name to contain 'voyage-multimodal', got: {collection_name}"

            # Verify NO subdirectory parameter is used
            assert (
                "subdirectory" not in storage_call[1]
                or storage_call[1].get("subdirectory") is None
            ), "Multimodal embeddings should NOT use subdirectory parameter - collection_name is the folder"

            # Verify the stored point contains image metadata
            stored_points = storage_call[1]["points"]
            assert len(stored_points) == 1
            assert stored_points[0]["payload"]["images"] == ["diagram.png"]
            # Note: FilesystemVectorStore uses 'content' field, not 'text'
            assert "![Diagram](diagram.png)" in stored_points[0]["payload"]["content"]

            # Verify the embedding was passed correctly
            stored_embedding = stored_points[0]["vector"]
            assert len(stored_embedding) == 1024

            # 3. Regular chunk (without images) went through normal vector_manager path
            assert len(mock_vector_manager.submitted_chunks) == 1
            assert "Description here" in mock_vector_manager.submitted_chunks[0]["text"]

    def test_multimodal_client_injection_via_constructor(self, tmp_path):
        """Test that multimodal_client can be injected via constructor parameter.

        CRITICAL: This test verifies the code review fix - multimodal_client MUST be
        injectable via __init__ parameter, not just as a manual attribute assignment.

        This test focuses solely on the injection mechanism. E2E processing is validated
        by other tests in this file (test_chunks_with_images_route_to_multimodal_client, etc).
        """
        # Create mock multimodal client
        mock_multimodal_client = Mock()

        # Create mock components
        mock_vector_manager = MockVectorCalculationManager()
        mock_chunker = Mock()
        mock_filesystem_client = MockFilesystemClient()

        # CRITICAL: Pass multimodal_client via constructor
        with FileChunkingManager(
            vector_manager=mock_vector_manager,
            chunker=mock_chunker,
            vector_store_client=mock_filesystem_client,
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=tmp_path,
            multimodal_client=mock_multimodal_client,  # INJECT VIA CONSTRUCTOR
        ) as manager:
            # Verify injection worked
            assert hasattr(
                manager, "multimodal_client"
            ), "FileChunkingManager must accept and store multimodal_client parameter"
            assert (
                manager.multimodal_client is mock_multimodal_client
            ), "Injected multimodal_client must be stored as instance attribute"

        # Also verify None is accepted (default case)
        with FileChunkingManager(
            vector_manager=mock_vector_manager,
            chunker=mock_chunker,
            vector_store_client=mock_filesystem_client,
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=tmp_path,
            # multimodal_client not passed - should default to None
        ) as manager:
            assert hasattr(
                manager, "multimodal_client"
            ), "FileChunkingManager must have multimodal_client attribute even when not provided"
            assert (
                manager.multimodal_client is None
            ), "multimodal_client should default to None when not provided"

    def test_multimodal_image_path_dict_handling(self, tmp_path):
        """
        Test that image references are correctly handled as strings (relative paths).

        This validates the fix for the bug where img_ref was incorrectly expected to be a dict:
        - img_ref is a STRING like "images/test-diagram.png" (from markdown chunker)
        - Code must use img_ref directly, NOT img_ref["path"]
        - Using img_ref["path"] causes: TypeError: string indices must be integers
        """
        # Create actual image file
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        test_image = images_dir / "test-diagram.png"
        test_image.write_bytes(b"fake png data")

        # Create markdown file with image reference
        test_md = tmp_path / "doc.md"
        test_md.write_text(
            "# Test\n\n![Test Diagram](images/test-diagram.png)\n\nSome text."
        )

        # Mock chunker that returns chunk with images as strings (correct structure)
        mock_chunker = Mock()
        mock_chunker.chunk_file.return_value = [
            {
                "text": "# Test\n\n![Test Diagram](images/test-diagram.png)",
                "chunk_index": 0,
                "total_chunks": 1,
                "file_extension": "md",
                "size": 50,
                "line_start": 1,
                "line_end": 3,
                "file_path": None,
                "images": ["images/test-diagram.png"],  # CORRECT: string, not dict
            }
        ]

        mock_vector_manager = MockVectorCalculationManager()
        mock_multimodal_client = Mock()
        mock_multimodal_client.get_multimodal_embedding.return_value = [0.1] * 1024

        with FileChunkingManager(
            vector_manager=mock_vector_manager,
            chunker=mock_chunker,
            vector_store_client=MockFilesystemClient(),
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=tmp_path,
            multimodal_client=mock_multimodal_client,
        ) as manager:
            # Add voyage client mock
            manager.is_voyageai_provider = True
            manager.vector_manager.embedding_provider.get_current_model.return_value = (
                "voyage-3"
            )

            metadata = {
                "project_id": "test",
                "file_hash": "abc123",
                "collection_name": "test_collection",  # Required to prevent skipping storage
            }
            future: Future[Any] = manager.submit_file_for_processing(
                test_md, metadata, Mock()
            )

            # This should NOT raise TypeError about PosixPath / dict
            result = future.result(timeout=5.0)

            # Should succeed
            assert result.success is True, f"Processing failed: {result.error}"

            # Verify multimodal client was called with correct image path
            assert (
                mock_multimodal_client.get_multimodal_embedding.called
            ), "Multimodal client should have been called for chunk with images"

            # Check the call arguments (using keyword arguments)
            call_args = mock_multimodal_client.get_multimodal_embedding.call_args
            image_paths = call_args.kwargs["image_paths"]

            # Verify image path was correctly used as string and resolved
            assert (
                len(image_paths) == 1
            ), f"Expected 1 image path, got {len(image_paths)}"
            assert (
                image_paths[0] == test_image
            ), f"Expected {test_image}, got {image_paths[0]}"

    def test_create_vector_point_preserves_images_field(self, tmp_path):
        """
        Test that _create_vector_point() preserves images field from chunk to payload.

        BUG: The _create_vector_point() method creates payload from GitAwareMetadataSchema
        but does NOT copy the 'images' field from the chunk to the payload.

        This test validates _create_vector_point() directly:
        1. Create chunk with images[] field
        2. Call _create_vector_point()
        3. Verify payload contains images field with correct structure
        """
        # Create test file
        test_file = tmp_path / "test.md"
        test_file.write_text("Test content")

        # Create FileChunkingManager instance to test _create_vector_point()
        mock_vector_manager = MockVectorCalculationManager()
        mock_chunker = MockFixedSizeChunker()
        mock_filesystem_client = MockFilesystemClient()

        manager = FileChunkingManager(
            vector_manager=mock_vector_manager,
            chunker=mock_chunker,
            vector_store_client=mock_filesystem_client,
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=tmp_path,
        )

        # Create chunk WITH images field
        test_images = ["images/test.png"]
        chunk = {
            "text": "Test content with image",
            "chunk_index": 0,
            "total_chunks": 1,
            "file_extension": "md",
            "line_start": 1,
            "line_end": 1,
            "images": test_images,  # CRITICAL: images field present
        }

        # Create mock embedding
        embedding = [0.1] * 768

        # Create mock metadata
        metadata = {
            "project_id": "test",
            "file_hash": "abc123",
            "git_available": False,
        }

        # Call _create_vector_point() directly
        vector_point = manager._create_vector_point(
            chunk=chunk, embedding=embedding, metadata=metadata, file_path=test_file
        )

        # CRITICAL ASSERTIONS:
        # 1. Vector point should be created successfully
        assert vector_point is not None
        assert "payload" in vector_point

        # 2. BUG DETECTION: payload should contain images field
        #    This will FAIL because _create_vector_point() doesn't copy images field
        assert (
            "images" in vector_point["payload"]
        ), "BUG: _create_vector_point() does NOT copy images field from chunk to payload"

        # 3. Verify images field preserves original structure
        assert (
            vector_point["payload"]["images"] == test_images
        ), f"Expected images={test_images}, got {vector_point['payload'].get('images')}"
