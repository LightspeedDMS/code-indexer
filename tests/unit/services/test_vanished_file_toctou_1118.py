"""Regression tests for Bug #1118 — vanished-file TOCTOU race.

A file enumerated during the walk phase can disappear before the hash phase
reads its stat. This must be a per-file skip (WARNING) not a fatal abort.

Covers:
- high_throughput_processor.py hash_worker: file vanishes before stat()
- file_chunking_manager.py _process_file_clean_lifecycle: file vanishes before stat()
"""

import logging
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from src.code_indexer.config import Config
from src.code_indexer.services.file_chunking_manager import FileProcessingResult
from src.code_indexer.services.high_throughput_processor import HighThroughputProcessor


# ---------------------------------------------------------------------------
# Minimal helpers — only external I/O is mocked (embedding provider and
# vector store are external services, not code under test).
# ---------------------------------------------------------------------------


def _make_mock_embedding_provider():
    """Minimal embedding provider mock (external API)."""
    provider = MagicMock()
    provider.get_provider_name.return_value = "voyage-ai"
    provider.get_current_model.return_value = "voyage-code-3"
    provider._get_model_token_limit.return_value = 120_000
    provider.api_key = "test-key"
    provider.embed.return_value = [[0.1] * 1024]
    provider.health_check.return_value = True
    return provider


def _make_mock_vector_store():
    """Minimal vector store mock (external persistence)."""
    store = MagicMock()
    store.resolve_collection_name.return_value = "test-col"
    store.upsert_points.return_value = True
    store.upsert_points_batched.return_value = True
    store.collection_exists.return_value = False
    store.create_collection.return_value = True
    store.begin_indexing.return_value = None
    store.end_indexing.return_value = {"vectors_indexed": 0}
    return store


def _make_processor(tmp_path: Path) -> HighThroughputProcessor:
    """Build a HighThroughputProcessor pointed at tmp_path."""
    config = Config(codebase_dir=tmp_path)
    embedding = _make_mock_embedding_provider()
    store = _make_mock_vector_store()
    return HighThroughputProcessor(
        config=config,
        embedding_provider=embedding,
        vector_store_client=store,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestVanishedFileToctou1118:
    """Bug #1118 — a file that disappears after enumeration must be skipped,
    not cause the entire indexing run to abort."""

    def test_vanished_file_skipped_not_fatal(self, tmp_path, caplog):
        """
        GIVEN  two paths: one real file (good.py) and one path that no longer
               exists (README.tmp — already deleted, simulating TOCTOU race)
        WHEN   process_files_high_throughput is called with both paths
        THEN   no RuntimeError is raised (indexing completes)
        AND    at least one WARNING-level log record mentions the vanished file
        """
        stable = tmp_path / "good.py"
        stable.write_text("print('hello')\n")

        # Simulate: the walk captured this path while it still existed,
        # but it vanished (atomic-write rename completed) before stat().
        transient = tmp_path / "README.tmp"
        transient.write_text("temporary atomic write in progress\n")
        transient.unlink()  # gone before processing starts

        processor = _make_processor(tmp_path)
        files_to_process = [stable, transient]

        with caplog.at_level(logging.WARNING):
            # Must NOT raise — a vanished file is a benign TOCTOU skip
            processor.process_files_high_throughput(
                files=files_to_process,
                vector_thread_count=2,
                batch_size=10,
            )

        # At least one WARNING record must mention the vanished file
        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        vanished_mentioned = any(
            "README.tmp" in r.message or "No such file" in r.message
            for r in warning_records
        )
        assert vanished_mentioned, (
            "Expected a WARNING mentioning the vanished file 'README.tmp', "
            "but got WARNING records:\n" + "\n".join(r.message for r in warning_records)
        )

    @patch(
        "src.code_indexer.services.high_throughput_processor.VectorCalculationManager"
    )
    @patch("src.code_indexer.services.high_throughput_processor.FileChunkingManager")
    def test_vanished_file_does_not_abort_good_files(
        self,
        mock_file_chunking_manager,
        mock_vector_manager,
        tmp_path,
        caplog,
    ):
        """
        GIVEN  three real indexable files plus one pre-deleted vanished path
        WHEN   process_files_high_throughput is called
        THEN   no RuntimeError is raised
        AND    stats are returned (not None)
        AND    failed_files is at most 1 (only the ghost; good files are not aborted)
        AND    files_processed is 3 (all good files successfully indexed)

        The ghost is skipped at the hash phase (FileNotFoundError -> WARNING + continue),
        so it never enters hash_results and is never submitted to FileChunkingManager.
        FileChunkingManager is mocked so the 3 good files complete without a real
        embedding pipeline.  If the source guard is reverted (re-raise or removed),
        the hash thread propagates the error into hash_errors -> RuntimeError ->
        the no-raise assertion fails, proving the test still guards the bug.
        """
        good_files = []
        for i in range(3):
            p = tmp_path / f"file_{i}.py"
            p.write_text(f"# file {i}\nprint({i})\n")
            good_files.append(p)

        # Ghost path — never existed on disk at call time
        ghost = tmp_path / "ghost.tmp"
        files_to_process = good_files + [ghost]

        # --- Mock VectorCalculationManager (context manager) ---
        mock_vm_instance = MagicMock()
        mock_vector_manager.return_value.__enter__.return_value = mock_vm_instance
        mock_vm_instance.embedding_provider = MagicMock()
        mock_vm_instance.embedding_provider.get_current_model.return_value = (
            "voyage-code-3"
        )
        mock_vm_instance.embedding_provider._get_model_token_limit.return_value = (
            120_000
        )
        mock_vm_instance.cancellation_event = threading.Event()

        # --- Mock FileChunkingManager (context manager) ---
        # Returns pre-built successful FileProcessingResult futures for good files.
        mock_fcm_instance = MagicMock()
        mock_file_chunking_manager.return_value.__enter__.return_value = (
            mock_fcm_instance
        )

        submitted: list[Any] = []

        def _submit(file_path, metadata, cb):
            f: Future[FileProcessingResult] = Future()
            f.set_result(
                FileProcessingResult(
                    success=True,
                    file_path=file_path,
                    chunks_processed=1,
                    processing_time=0.01,
                )
            )
            submitted.append(f)
            return f

        mock_fcm_instance.submit_file_for_processing.side_effect = _submit

        processor = _make_processor(tmp_path)

        with caplog.at_level(logging.WARNING):
            stats = processor.process_files_high_throughput(
                files=files_to_process,
                vector_thread_count=2,
                batch_size=10,
            )

        assert stats is not None, "process_files_high_throughput must return stats"
        assert stats.failed_files <= 1, (
            f"Only the vanished file should fail (at most 1), "
            f"but {stats.failed_files} files failed"
        )
        assert stats.files_processed == 3, (
            f"All 3 good files should be processed, "
            f"but only {stats.files_processed} were"
        )

    def test_file_chunking_manager_vanished_file_returns_failure_result(self, tmp_path):
        """
        GIVEN  a file path that does not exist (vanished before processing)
        WHEN   FileChunkingManager._process_file_clean_lifecycle is called
        THEN   it returns FileProcessingResult with success=False (not a crash)
        AND    result.error is set to a non-empty string
        """
        from src.code_indexer.services.file_chunking_manager import (
            FileChunkingManager,
            FileProcessingResult,
        )
        from src.code_indexer.services.clean_slot_tracker import CleanSlotTracker
        from src.code_indexer.indexing.fixed_size_chunker import FixedSizeChunker

        config = Config(codebase_dir=tmp_path)
        chunker = FixedSizeChunker(config)
        mock_vm = MagicMock()
        mock_vm.embedding_provider = MagicMock()
        mock_vm.embedding_provider.get_current_model.return_value = "voyage-code-3"
        mock_vm.embedding_provider._get_model_token_limit.return_value = 120_000
        mock_vm.cancellation_event = threading.Event()
        mock_store = _make_mock_vector_store()
        slot_tracker = CleanSlotTracker(max_slots=4)

        # File that does not exist — simulates vanished temp file
        ghost = tmp_path / "ghost_chunk.tmp"

        with FileChunkingManager(
            vector_manager=mock_vm,
            chunker=chunker,
            vector_store_client=mock_store,
            thread_count=2,
            slot_tracker=slot_tracker,
            codebase_dir=tmp_path,
        ) as manager:
            metadata = {
                "project_id": "test",
                "file_hash": "abc123",
                "git_available": False,
                "file_size": 0,
                "file_mtime": 0.0,
                "collection_name": "test-col",
            }
            future = manager.submit_file_for_processing(ghost, metadata, None)
            result: FileProcessingResult = future.result(timeout=10)

        # Must return a failure result — not raise or crash
        assert result is not None
        assert result.success is False
        assert result.error is not None and len(result.error) > 0
