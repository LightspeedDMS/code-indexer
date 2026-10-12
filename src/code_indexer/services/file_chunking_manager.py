"""
FileChunkingManager for parallel file processing with vector integration and vector storage writing.

Handles complete file lifecycle: chunk → vector → wait → write to vector storage
with file atomicity and immediate progress feedback.

This implementation addresses the specific user problems:
1. "not efficient for very small files" - solved by parallel processing
2. "no feedback when chunking files" - solved by immediate progress callbacks

Architecture:
- ThreadPoolExecutor with (thread_count + 2) workers per specifications
- File atomicity: all chunks from one file written together
- Worker threads handle complete lifecycle: chunk → vector → wait → write
- Immediate queuing feedback before async processing
"""

import hashlib
import logging
import sqlite3
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor, Future
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Any, Optional, Callable, List
from dataclasses import dataclass

if TYPE_CHECKING:
    from .fts_file_documents import FtsWriteFailures

from .vector_calculation_manager import VectorCalculationManager
from ..indexing.fixed_size_chunker import FixedSizeChunker
from .clean_slot_tracker import CleanSlotTracker, FileData, FileStatus
from .run_spend_meter import get_run_spend_meter
from ..storage.sqlite_chunk_store import (
    ChunkStoreUnavailableError,
    is_fatal_chunk_store_write_error,
)
import threading

# Token counting for large file handling - using embedded tokenizer
# No imports needed here - lazy import in _count_tokens_accurately()

logger = logging.getLogger(__name__)


# Constants
# Note (Bug #1218): VECTOR_PROCESSING_TIMEOUT removed — no per-batch wall-clock
# bound on the indexing path; the only legitimate timeout is the per-request
# outbound embedding-provider HTTP call inside the provider itself.
THREAD_POOL_SHUTDOWN_TIMEOUT = 30.0  # 30 seconds for graceful shutdown


def _multimodal_content_hash(text: str, image_paths: List[Path]) -> str:
    """Bug #2153: the ``content_hash`` (reuse key) of a multimodal chunk.

    It covers everything the provider is sent: the chunk text AND the bytes
    of every image, in order, so a changed image under unchanged text is a
    miss, never a stale reuse. The text is length-prefixed and each image
    contributes a fixed-size digest, so the encoding is unambiguous."""
    text_bytes = text.encode("utf-8")
    digest = hashlib.sha256(b"cidx-multimodal-v1\0")
    digest.update(len(text_bytes).to_bytes(8, "big"))
    digest.update(text_bytes)
    for image_path in image_paths:
        digest.update(hashlib.sha256(image_path.read_bytes()).digest())
    return digest.hexdigest()


@dataclass
class FileProcessingResult:
    """Result from complete file processing lifecycle."""

    success: bool
    file_path: Path
    chunks_processed: int
    processing_time: float
    error: Optional[str] = None
    vanished: bool = False  # True when file disappeared before stat() (TOCTOU skip)


def _vector_storage_failure_result(
    file_path: Path, error: BaseException, start_time: float
) -> FileProcessingResult:
    """Bug #1746 H1/H2: build the ordinary per-file failure result for a
    NON-fatal vector-storage write error (e.g. transient sqlite lock
    contention, or any other non-fatal exception). Shared by both the
    reclassified-non-fatal branch and the generic except-Exception branch
    in _process_file_clean_lifecycle() so the two stay byte-identical.
    """
    logger.error(f"Vector storage write failed for {file_path}: {error}")
    return FileProcessingResult(
        success=False,
        file_path=file_path,
        chunks_processed=0,
        processing_time=time.time() - start_time,
        error=f"Vector storage write failed: {error}",
    )


class FileChunkingManager:
    """Manages parallel file processing with complete lifecycle management."""

    def __init__(
        self,
        vector_manager: VectorCalculationManager,
        chunker: FixedSizeChunker,
        vector_store_client,  # Vector storage client (FilesystemVectorStore)
        thread_count: int,
        slot_tracker: CleanSlotTracker,
        codebase_dir: Path,  # CRITICAL FOR COW CLONING: Needed for path normalization
        fts_manager=None,  # Optional FTS index manager
        multimodal_client=None,  # Optional VoyageMultimodalClient for multimodal embeddings
        fts_write_failures: Optional["FtsWriteFailures"] = None,
    ):
        """
        Initialize FileChunkingManager with complete functionality.

        Args:
            vector_manager: Existing VectorCalculationManager (unchanged)
            chunker: Existing FixedSizeChunker (unchanged)
            vector_store_client: Vector storage client for atomic writes (FilesystemVectorStore)
            thread_count: Number of worker threads (thread_count + 2 per specs)
            slot_tracker: CleanSlotTracker for progress tracking and slot management
            codebase_dir: Repository root directory for path normalization
            fts_manager: Optional FTS index manager
            multimodal_client: Optional VoyageMultimodalClient for multimodal embeddings
            fts_write_failures: Optional run-scoped record of files whose FTS
                documents could not be replaced (Bug #2056: the run's finish
                retries them from disk; still failing, they are missing from
                FTS and leave the index unmarked)

        Raises:
            ValueError: If thread_count is invalid or dependencies are None
        """
        if thread_count <= 0:
            raise ValueError(f"thread_count must be positive, got {thread_count}")
        if not vector_manager:
            raise ValueError("vector_manager cannot be None")
        if not chunker:
            raise ValueError("chunker cannot be None")
        if not vector_store_client:
            raise ValueError("vector_store_client cannot be None")
        if not slot_tracker:
            raise ValueError("slot_tracker cannot be None")
        if not codebase_dir:
            raise ValueError("codebase_dir cannot be None")

        self.vector_manager = vector_manager
        self.chunker = chunker
        self.vector_store_client = vector_store_client
        self.thread_count = thread_count
        self.slot_tracker = slot_tracker
        self.codebase_dir = codebase_dir
        self.fts_manager = fts_manager
        self.multimodal_client = multimodal_client
        self.fts_write_failures = fts_write_failures

        # CRITICAL FIX: Single cancellation event shared with VectorCalculationManager
        self._cancellation_requested = False
        self._shutdown_complete = threading.Event()

        # ThreadPoolExecutor with (thread_count + 2) workers per user specs
        self.executor: Optional[ThreadPoolExecutor] = None
        self._pending_futures: List[Future] = []  # Track futures for clean cancellation

        # Check if we're using VoyageAI provider for token counting
        provider_name = vector_manager.embedding_provider.__class__.__name__
        self.is_voyageai_provider = "VoyageAI" in provider_name

        logger.info(f"Initialized FileChunkingManager with {thread_count} base threads")

    def _replace_fts_documents(
        self, file_path: Path, chunks: List[Dict[str, Any]]
    ) -> None:
        """Bug #1761/#2056: replace the file's FTS documents by one per
        current chunk (none for a blank file) through the shared helper. A
        failure is logged at ERROR and recorded in the run's
        `fts_write_failures` (the run's finish retries it from disk; see
        fts_lifecycle.finish_fts_run); the file's semantic indexing still
        completes.
        RuntimeError (writer not initialized) propagates."""
        from .fts_file_documents import FtsReplaceError, replace_file_fts_documents

        try:
            replace_file_fts_documents(
                self.fts_manager, file_path, self.codebase_dir, chunks
            )
        except FtsReplaceError as e:
            logger.error(str(e))
            if self.fts_write_failures is not None:
                self.fts_write_failures.record(file_path, str(e))

    def _normalize_path_for_storage(self, file_path: Path) -> str:
        """
        Normalize file path to relative for portable database storage.

        CRITICAL FOR COW CLONING: All paths stored in vector store must be relative
        to codebase_dir to ensure database portability across different filesystem
        locations (CoW clones, repository moves, etc.).

        Args:
            file_path: File path (absolute or relative)

        Returns:
            Relative path string for database storage

        Raises:
            ValueError: If file_path is not under codebase_dir
        """
        if file_path.is_absolute():
            try:
                return str(file_path.relative_to(self.codebase_dir))
            except ValueError as e:
                logger.error(
                    f"Cannot normalize path {file_path} - not under codebase_dir "
                    f"{self.codebase_dir}: {e}"
                )
                raise
        return str(file_path)

    def __enter__(self):
        """Context manager entry - start thread pool."""
        self.executor = ThreadPoolExecutor(
            max_workers=self.thread_count + 2, thread_name_prefix="FileChunk"
        )
        logger.info(
            f"Started FileChunkingManager thread pool with {self.thread_count + 2} workers"
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - shutdown thread pool with proper cleanup."""
        if self.executor:
            # CRITICAL FIX: Clean shutdown without private attribute access

            try:
                # Step 1: Cancel all pending futures to signal threads to stop
                self._cancellation_requested = True
                for future in self._pending_futures:
                    if not future.done():
                        future.cancel()

                # Step 2: Shutdown with timeout using thread
                # Since ThreadPoolExecutor.shutdown() doesn't support timeout in Python < 3.9,
                # we use a thread to implement timeout behavior
                import threading

                shutdown_complete = threading.Event()

                def shutdown_thread():
                    try:
                        if self.executor is not None:
                            self.executor.shutdown(wait=True)
                        shutdown_complete.set()
                    except Exception as e:
                        logger.error(f"Error in shutdown thread: {e}")
                        shutdown_complete.set()

                shutdown_worker = threading.Thread(target=shutdown_thread, daemon=True)
                shutdown_worker.start()

                # Wait up to 10 seconds for shutdown
                if shutdown_complete.wait(timeout=10.0):
                    logger.info("FileChunkingManager thread pool shutdown complete")
                else:
                    # Timeout - force shutdown
                    logger.warning(
                        "FileChunkingManager graceful shutdown timeout - forcing shutdown"
                    )
                    self.executor.shutdown(wait=False)

            except Exception as e:
                logger.error(f"Error during FileChunkingManager shutdown: {e}")

            finally:
                self._shutdown_complete.set()

    def request_cancellation(self) -> None:
        """Request cancellation of all file processing."""
        self._cancellation_requested = True
        logger.info("FileChunkingManager cancellation requested")

    def _count_tokens(self, text: str) -> int:
        """Count tokens using provider-specific token counting.

        For VoyageAI: Use official count_tokens API
        For Voyage: Estimate based on character count (rough approximation)
        """
        # Get the model name from the embedding provider
        model = self.vector_manager.embedding_provider.get_current_model()

        # Use VoyageAI's accurate token counting if using VoyageAI provider
        if self.is_voyageai_provider:
            # Lazy import to avoid loading tokenizer at module import time
            from .embedded_voyage_tokenizer import VoyageTokenizer

            return VoyageTokenizer.count_tokens([text], model=model)

        # Fallback: Rough estimate (4 chars ≈ 1 token for English text)
        # This is conservative and works for batching purposes
        return len(text) // 4

    def submit_file_for_processing(
        self,
        file_path: Path,
        metadata: Dict[str, Any],
        progress_callback: Optional[Callable],
        after: Optional["Future[FileProcessingResult]"] = None,
    ) -> "Future[FileProcessingResult]":
        """
        Submit file for complete lifecycle processing.

        Args:
            file_path: Path to file to process
            metadata: File metadata for processing
            progress_callback: Callback for progress updates
            after: Story #2119 -- the future of an EARLIER-submitted file with
                the same content (file hash). This file starts only once that
                one has finished (``_process_after_first_copy``), so its reuse
                lookup sees the rows the first copy committed instead of
                paying for the same chunks again.

        Returns:
            Future that will contain FileProcessingResult when complete
        """
        if not self.executor:
            raise RuntimeError("FileChunkingManager not started - use context manager")

        # Check if cancellation was requested
        if self._cancellation_requested:
            # Return immediately with cancelled result
            cancelled_future: Future[FileProcessingResult] = Future()
            cancelled_future.set_result(
                FileProcessingResult(
                    success=False,
                    file_path=file_path,
                    chunks_processed=0,
                    processing_time=0.0,
                    error="Cancelled before processing",
                )
            )
            return cancelled_future

        # SURGICAL FIX: Remove individual callback spam - no more "📥 Queued" messages
        # CleanSlotTracker will handle the fixed N-line display

        # Submit to worker thread (immediate return)
        # Always use clean implementation
        process_method = self._process_file_clean_lifecycle

        if after is None:
            future = self.executor.submit(
                process_method,
                file_path,
                metadata,
                progress_callback,
                self.slot_tracker,
            )
        else:
            future = self.executor.submit(
                self._process_after_first_copy,
                after,
                file_path,
                metadata,
                progress_callback,
                self.slot_tracker,
            )

        # Track future for clean shutdown
        self._pending_futures.append(future)

        return future

    def _process_after_first_copy(
        self,
        first_copy: "Future[FileProcessingResult]",
        file_path: Path,
        metadata: Dict[str, Any],
        progress_callback: Optional[Callable],
        slot_tracker: CleanSlotTracker,
    ) -> FileProcessingResult:
        """Story #2119: process a byte-identical copy only after the first
        copy of its content has finished. The pool is FIFO and the first copy
        was submitted earlier, so it is running or done by the time this
        worker waits: no deadlock, and the wait ends when that file does.

        A cancelled first copy cancels this one too; a first copy that raised
        (only a fatal chunk-store failure escapes a file) aborts this one
        rather than letting it pay for content the run cannot store. A first
        copy that FAILED (an unsuccessful result, e.g. the provider rejected
        it) fails this copy too, without a provider call: the same content
        would be paid again in this run. It is counted as a failed file, so
        a later run retries both. A vanished first copy is a skip, not a
        failure: this copy is then processed normally.

        Waits through ``Future.exception()``, which raises ``CancelledError``
        for a cancelled future and also wakes on a plain ``cancel()`` --
        ``concurrent.futures.wait()`` never returns for a future cancelled
        before any worker picked it up."""
        try:
            first_copy_error = first_copy.exception()
        except CancelledError:
            return FileProcessingResult(
                success=False,
                file_path=file_path,
                chunks_processed=0,
                processing_time=0.0,
                error="Cancelled before processing",
            )
        if first_copy_error is not None:
            raise ChunkStoreUnavailableError(
                f"Not processing {file_path}: the first copy of its content "
                f"failed fatally: {first_copy_error}"
            ) from first_copy_error
        first_copy_result = first_copy.result()
        if not first_copy_result.success and not first_copy_result.vanished:
            return FileProcessingResult(
                success=False,
                file_path=file_path,
                chunks_processed=0,
                processing_time=0.0,
                error=(
                    f"Not processed: the first copy of its content "
                    f"({first_copy_result.file_path}) failed: "
                    f"{first_copy_result.error}"
                ),
            )
        return self._process_file_clean_lifecycle(
            file_path, metadata, progress_callback, slot_tracker
        )

    def _create_vector_point(
        self,
        chunk: Dict[str, Any],
        embedding: List[float],
        metadata: Dict[str, Any],
        file_path: Path,
    ) -> Dict[str, Any]:
        """
        Create vector point from chunk and embedding.

        This method creates the point structure expected by the vector store
        using the existing metadata patterns from HighThroughputProcessor.

        UNIVERSAL TIMESTAMP COLLECTION: Always collects file_last_modified
        and indexed_timestamp for staleness detection, regardless of git status.

        Args:
            chunk: Chunk data from FixedSizeChunker
            embedding: Vector embedding from VectorCalculationManager
            metadata: File metadata
            file_path: Path to source file

        Returns:
            Vector point dictionary
        """
        # Use existing metadata creation logic pattern
        # Import here to avoid circular imports
        from .metadata_schema import GitAwareMetadataSchema

        # UNIVERSAL TIMESTAMP COLLECTION: Always collect file modification timestamp
        try:
            file_stat = file_path.stat()
            file_last_modified = file_stat.st_mtime
            file_size = file_stat.st_size
        except (OSError, IOError) as e:
            # Don't fail indexing for timestamp issues - set to None and continue
            logger.warning(f"Failed to get file stats for {file_path}: {e}")
            file_last_modified = None
            file_size = metadata.get("file_size", 0)  # Fallback to metadata or 0

        # Issue #2013: indexed_timestamp is the time the file content was READ
        # (recorded by FileIdentifier.get_file_metadata just before hashing),
        # not this point's write time -- reconcile's racy-timestamp check
        # must cover the read second. Metadata without a read time keeps
        # the previous behaviour (the current time).
        indexed_timestamp = metadata.get("content_read_timestamp", time.time())

        # Create Git-aware metadata
        metadata_info = None
        if metadata.get("git_available", False):
            metadata_info = {
                "commit_hash": metadata.get("commit_hash"),
                "branch": metadata.get("branch"),
                "git_hash": metadata.get("git_hash"),
            }
        else:
            metadata_info = {
                "file_mtime": metadata.get("file_mtime"),
                "file_size": metadata.get("file_size", file_size),
            }

        # Create payload using existing schema
        payload = GitAwareMetadataSchema.create_git_aware_metadata(
            path=self._normalize_path_for_storage(file_path),
            content=chunk["text"],
            language=chunk["file_extension"],
            file_size=file_size,  # Use already collected file_size
            chunk_index=chunk["chunk_index"],
            total_chunks=chunk["total_chunks"],
            project_id=metadata["project_id"],
            file_hash=metadata["file_hash"],
            git_metadata=(
                metadata_info if metadata.get("git_available", False) else None
            ),
            line_start=chunk.get("line_start"),
            line_end=chunk.get("line_end"),
            file_last_modified=file_last_modified,
            indexed_timestamp=indexed_timestamp,
        )

        # Add filesystem metadata for non-git projects
        if not metadata.get("git_available", False) and metadata_info:
            if "file_mtime" in metadata_info:
                payload["filesystem_mtime"] = metadata_info["file_mtime"]
            if "file_size" in metadata_info:
                payload["filesystem_size"] = metadata_info["file_size"]

        # UNIVERSAL TIMESTAMP COLLECTION: Timestamp fields are now handled by GitAwareMetadataSchema

        # Add images field to payload for multimodal chunks
        if "images" in chunk and chunk["images"]:
            payload["images"] = chunk["images"]

        # Create point ID using hash of file and chunk (ensuring uniqueness)
        point_id_data = (
            f"{metadata['project_id']}_{metadata['file_hash']}_{chunk['chunk_index']}"
        )
        point_id = hashlib.md5(point_id_data.encode()).hexdigest()

        payload["point_id"] = point_id
        payload["unique_key"] = point_id_data

        # Story #470: Smart embedding cache - store content hash for future cache lookups
        content_hash = hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest()
        payload["content_hash"] = content_hash

        # Create vector point
        vector_point = {"id": point_id, "vector": embedding, "payload": payload}

        return vector_point

    def _lookup_reusable_embeddings(
        self,
        file_path: Path,
        collection_name: str,
        metadata: Dict[str, Any],
        chunk_count: int,
    ) -> Dict[str, Any]:
        """Story #2119: content_hash -> stored embedding of this file's
        content ids and its path in ``collection_name``. Matched by content,
        never by chunk index, so a chunk that moved (e.g. a prepended chunk)
        is reused. Bug #2153: the multimodal collection is looked up here
        too.

        Fails CLOSED: a lookup error is never "nothing stored" -- that would
        pay the provider for embeddings the store may already hold. It aborts
        the whole run with the typed fatal error (exit 86 when SQLite reports
        corruption, else 87; 89 for a missing established store, found in the
        cause chain) before any chunk of this file is sent."""
        file_path_for_lookup = self._normalize_path_for_storage(file_path)
        try:
            existing: Dict[str, Any] = (
                self.vector_store_client.get_existing_content_hashes(
                    file_path_for_lookup,
                    collection_name,
                    project_id=metadata["project_id"],
                    file_hash=metadata["file_hash"],
                    chunk_count=chunk_count,
                )
            )
        except Exception as e:
            logger.error(f"Embedding reuse lookup failed for {file_path}: {e}")
            raise ChunkStoreUnavailableError(
                f"Embedding reuse lookup failed for {file_path_for_lookup} "
                f"in {collection_name}; aborting before any provider call: {e}"
            ) from e
        return existing

    def _resolve_chunk_image_paths(self, chunk: Dict[str, Any]) -> List[Path]:
        """Absolute paths of a chunk's local images that exist inside the
        codebase. Image paths from ImageExtractorFactory are already relative
        to codebase_dir and validated (existence, format, size); the checks
        here are defense in depth."""
        image_paths: List[Path] = []
        for img_ref in chunk.get("images", []):
            # img_ref is a relative path string (TextChunker, legacy) or a
            # dict with a 'path' key (FixedSizeChunker + ImageExtractorFactory)
            img_ref_path = img_ref["path"] if isinstance(img_ref, dict) else img_ref
            if img_ref_path.startswith(("http://", "https://")):
                logger.debug(f"Skipping remote image URL: {img_ref_path}")
                continue
            img_path = (self.codebase_dir / img_ref_path).resolve()
            if not img_path.exists():
                logger.debug(f"Image not found: {img_path} - skipping")
                continue
            try:
                img_path.relative_to(self.codebase_dir)
            except ValueError:
                logger.warning(f"Image outside codebase: {img_path} - skipping")
                continue
            image_paths.append(img_path)
        return image_paths

    def _ensure_multimodal_collection(self, collection_name: str) -> None:
        """Create the multimodal collection (same level as the code
        embeddings) when it does not exist yet."""
        if self.vector_store_client.collection_exists(collection_name):
            return
        if hasattr(self.multimodal_client, "config") and hasattr(
            self.multimodal_client.config, "default_dimension"
        ):
            vector_size = self.multimodal_client.config.default_dimension
        else:
            vector_size = 1024  # VoyageAI default
        self.vector_store_client.create_collection(
            collection_name=collection_name, vector_size=vector_size
        )
        logger.info(
            f"Created multimodal collection '{collection_name}' at same level "
            f"as code embeddings"
        )

    def _multimodal_vector_point(
        self,
        chunk: Dict[str, Any],
        image_paths: List[Path],
        stored: Dict[str, Any],
        metadata: Dict[str, Any],
        file_path: Path,
    ) -> Dict[str, Any]:
        """Bug #2153: the point of one image-bearing chunk. A stored vector
        with the same content_hash (text + image bytes) is reused without a
        provider call; only a miss is registered with the run-spend meter
        (Story #2122) and sent."""
        content_hash = _multimodal_content_hash(chunk["text"], image_paths)
        reusable = stored.get(content_hash)
        if reusable:
            embedding = reusable["vector"]
        else:
            run_spend_meter = get_run_spend_meter()
            if run_spend_meter.is_armed():
                run_spend_meter.register_misses(
                    [hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest()],
                    max_retries=self.multimodal_client.request_max_retries(),
                )
            embedding = self.multimodal_client.get_multimodal_embedding(
                text=chunk["text"], image_paths=image_paths
            )
        chunk_data = {
            "text": chunk["text"],
            "chunk_index": chunk.get("chunk_index", 0),
            "total_chunks": 1,  # Multimodal chunks stored individually
            "line_start": chunk.get("line_start"),
            "line_end": chunk.get("line_end"),
            "file_extension": file_path.suffix.lstrip(".") or "txt",
            "images": chunk.get("images", []),  # Preserve images metadata
        }
        vector_point = self._create_vector_point(
            chunk_data, embedding, metadata, file_path
        )
        vector_point["payload"]["content_hash"] = content_hash
        return vector_point

    def _embed_multimodal_chunks(
        self,
        multimodal_chunks: List[Dict[str, Any]],
        chunk_count: int,
        metadata: Dict[str, Any],
        file_path: Path,
    ) -> List[Dict[str, Any]]:
        """Embed or reuse one file's image-bearing chunks; return the points
        to write as ``{"collection_name", "vector_point"}`` entries.

        Bug #1502 (Codex M6): points are only QUEUED here; the caller writes
        them after every regular embedding of the file is validated.
        Bug #2153: as on the text path (Story #2119), the multimodal store is
        consulted first with ONE fail-closed lookup per file. A per-chunk
        provider or image-read failure skips that chunk."""
        # Cohere: collection_name property ("embed-v4.0-multimodal");
        # VoyageAI: falls back to config.model ("voyage-multimodal-3").
        collection_name = getattr(
            self.multimodal_client,
            "collection_name",
            self.multimodal_client.config.model,
        )
        if not collection_name:
            logger.warning(
                f"No model name in multimodal client config, skipping the "
                f"multimodal chunks of {file_path}"
            )
            return []
        self._ensure_multimodal_collection(collection_name)
        stored = self._lookup_reusable_embeddings(
            file_path, collection_name, metadata, chunk_count
        )
        pending: List[Dict[str, Any]] = []
        for chunk in multimodal_chunks:
            image_paths = self._resolve_chunk_image_paths(chunk)
            if not image_paths:
                logger.warning(
                    f"No valid images for chunk {chunk.get('chunk_index')} - skipping"
                )
                continue
            try:
                vector_point = self._multimodal_vector_point(
                    chunk, image_paths, stored, metadata, file_path
                )
            except Exception as e:
                logger.error(f"Failed to generate or store multimodal embedding: {e}")
                continue
            pending.append(
                {"collection_name": collection_name, "vector_point": vector_point}
            )
        return pending

    def _process_file_clean_lifecycle(
        self,
        file_path: Path,
        metadata: Dict[str, Any],
        progress_callback: Optional[Callable],
        slot_tracker: CleanSlotTracker,
    ) -> FileProcessingResult:
        """
        CLEAN IMPLEMENTATION: Process file with proper resource management.

        DESIGN PRINCIPLES:
        1. Single acquire at start
        2. All work in try block
        3. Single release in finally block
        4. Direct slot_id usage throughout
        5. No thread_id tracking
        6. Call progress_callback to trigger display updates
        """
        start_time = time.time()
        try:
            file_size = file_path.stat().st_size
        except FileNotFoundError as e:
            # Bug #1118 — TOCTOU: file vanished between enumeration and chunking.
            # Return a vanished result (not raise) so the collector skips it gracefully.
            # vanished=True signals that this is NOT a hard failure — it must not
            # increment failed_files in the collector.
            logger.warning(f"Skipping file that vanished during chunking: {file_path}")
            return FileProcessingResult(
                success=False,
                vanished=True,
                file_path=file_path,
                chunks_processed=0,
                processing_time=time.time() - start_time,
                error=f"File vanished before processing: {e}",
            )
        filename = file_path.name

        # Single acquire at start - create clean FileData
        file_data = FileData(
            filename=filename,
            file_size=file_size,
            status=FileStatus.STARTING,
            start_time=start_time,
        )

        # Single acquire using CleanSlotTracker
        slot_id = slot_tracker.acquire_slot(file_data)

        # PROGRESS REPORTING ADJUSTMENT: Remove initial callback
        # HighThroughputProcessor handles file-level progress counting
        # We only call progress_callback when the file actually completes

        try:
            # ALL work in try block
            slot_tracker.update_slot(slot_id, FileStatus.CHUNKING)

            # Phase 1: Chunk the file
            logger.debug(f"Starting chunking for {file_path}")
            chunks = self.chunker.chunk_file(file_path, repo_root=self.codebase_dir)

            if not chunks:
                # Empty files are valid but don't need indexing
                logger.debug(f"Skipping empty file: {file_path}")
                # Bug #2056 (P2-2): a file that became blank keeps no FTS
                # documents -- remove whatever an earlier version left.
                if self.fts_manager:
                    self._replace_fts_documents(file_path, [])

                slot_tracker.update_slot(slot_id, FileStatus.COMPLETE)

                # PROGRESS REPORTING ADJUSTMENT: Empty file completion callback
                if progress_callback:
                    concurrent_files = (
                        slot_tracker.get_concurrent_files_data() if slot_tracker else []
                    )
                    progress_callback(
                        -1,  # Signal: display update only, no progress bar change
                        -1,  # Signal: display update only
                        file_path,
                        concurrent_files=concurrent_files,
                    )

                return FileProcessingResult(
                    success=True,
                    file_path=file_path,
                    chunks_processed=0,
                    processing_time=time.time() - start_time,
                    error=None,
                )

            logger.debug(f"Generated {len(chunks)} chunks for {file_path}")

            # MULTIMODAL ROUTING: Separate chunks into regular (code-only) and multimodal (with images)
            regular_chunks = []
            multimodal_chunks = []

            for chunk in chunks:
                if chunk.get("images") and len(chunk["images"]) > 0:
                    multimodal_chunks.append(chunk)
                    logger.debug(
                        f"Chunk {chunk.get('chunk_index')} has {len(chunk['images'])} images - "
                        "routing to multimodal embedding path"
                    )
                else:
                    regular_chunks.append(chunk)

            logger.debug(
                f"Separated {len(regular_chunks)} regular chunks and "
                f"{len(multimodal_chunks)} multimodal chunks for {file_path}"
            )

            # Update status after chunking
            slot_tracker.update_slot(slot_id, FileStatus.VECTORIZING)

            # PROGRESS REPORTING ADJUSTMENT: Remove intermediate callback
            # Status updates are tracked by CleanSlotTracker for display
            # HighThroughputProcessor handles file-level progress reporting

            # Story #470: Smart embedding cache - skip unchanged chunks
            cached_points = []
            uncached_regular_chunks = []
            chunks_skipped = 0

            collection_name = metadata.get("collection_name")
            # Story #2119: content_hash -> stored embedding (fail closed).
            existing_hashes: Dict[str, Any] = {}
            if collection_name:
                existing_hashes = self._lookup_reusable_embeddings(
                    file_path, collection_name, metadata, len(chunks)
                )

            # Story #2122: content_hash (sha256 hex) of every miss, the only
            # regular inputs this run may send.
            missed_keys: List[str] = []
            for chunk in regular_chunks:
                chunk_text = chunk["text"]
                new_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
                chunk_idx = chunk.get("chunk_index", 0)
                existing = existing_hashes.get(new_hash)

                if existing:
                    # Cache hit: reuse existing vector, rebuild payload with current metadata
                    chunk_data = {
                        "text": chunk_text,
                        "chunk_index": chunk_idx,
                        "total_chunks": chunk.get("total_chunks", 1),
                        "line_start": chunk.get("line_start"),
                        "line_end": chunk.get("line_end"),
                        "file_extension": file_path.suffix.lstrip(".") or "txt",
                    }
                    vector_point = self._create_vector_point(
                        chunk_data, existing["vector"], metadata, file_path
                    )
                    cached_points.append(vector_point)
                    chunks_skipped += 1
                else:
                    # Cache miss: needs embedding
                    uncached_regular_chunks.append(chunk)
                    missed_keys.append(new_hash)

            run_spend_meter = get_run_spend_meter()
            if missed_keys and run_spend_meter.is_armed():
                run_spend_meter.register_misses(
                    missed_keys,
                    max_retries=(
                        self.vector_manager.embedding_provider.request_max_retries()
                    ),
                )

            if chunks_skipped > 0:
                logger.info(
                    f"Smart cache: {file_path.name}: {chunks_skipped} cached, "
                    f"{len(uncached_regular_chunks)} to embed"
                )

            # Replace regular_chunks with only uncached ones for batching
            regular_chunks = uncached_regular_chunks

            # Phase 2: TOKEN-AWARE BATCHING - Count tokens as we chunk, submit when limit reached
            # Get token limit from VoyageAI client configuration (with safety margin from YAML)
            model_limit = (
                self.vector_manager.embedding_provider._get_model_token_limit()  # type: ignore[attr-defined]
            )
            TOKEN_LIMIT = int(
                model_limit * 0.9
            )  # Apply same 90% safety margin as VoyageAI client

            current_batch: List[str] = []
            current_tokens = 0
            batch_futures = []

            # Process ONLY regular chunks (no images) via code embedding path
            for chunk in regular_chunks:
                chunk_text = chunk["text"]
                chunk_tokens = self._count_tokens(chunk_text)

                # If this chunk would exceed limit, submit current batch
                if current_tokens + chunk_tokens > TOKEN_LIMIT and current_batch:
                    try:
                        batch_future = self.vector_manager.submit_batch_task(
                            current_batch, metadata
                        )
                        batch_futures.append(batch_future)
                        logger.debug(
                            f"Submitted batch of {len(current_batch)} chunks ({current_tokens} tokens) for {file_path}"
                        )
                    except RuntimeError as e:
                        if "Thread pool not started" in str(e):
                            logger.info(
                                f"Vector manager shut down, cancelling {file_path}"
                            )
                            return FileProcessingResult(
                                success=False,
                                file_path=file_path,
                                chunks_processed=0,
                                processing_time=time.time() - start_time,
                                error="Cancelled",
                            )
                        raise

                    # Reset for next batch
                    current_batch = []
                    current_tokens = 0

                # Add chunk to current batch
                current_batch.append(chunk_text)
                current_tokens += chunk_tokens

            # Submit final batch if not empty
            if current_batch:
                try:
                    batch_future = self.vector_manager.submit_batch_task(
                        current_batch, metadata
                    )
                    batch_futures.append(batch_future)
                    logger.debug(
                        f"Submitted final batch of {len(current_batch)} chunks ({current_tokens} tokens) for {file_path}"
                    )
                except RuntimeError as e:
                    if "Thread pool not started" in str(e):
                        logger.info(f"Vector manager shut down, cancelling {file_path}")
                        return FileProcessingResult(
                            success=False,
                            file_path=file_path,
                            chunks_processed=0,
                            processing_time=time.time() - start_time,
                            error="Cancelled",
                        )
                    raise

            # Bug #1502 (Codex M6): multimodal points are QUEUED here and
            # only actually persisted in Phase 4, AFTER all regular
            # embeddings have been validated -- never upserted eagerly,
            # which would violate file atomicity if a later regular
            # embedding turned out falsy and failed the whole file.
            pending_multimodal_points: List[Dict[str, Any]] = []

            # MULTIMODAL PROCESSING: Handle chunks with images
            if (
                multimodal_chunks
                and hasattr(self, "multimodal_client")
                and self.multimodal_client
            ):
                # Bug #2153: committed multimodal rows are reused (Story #2119).
                pending_multimodal_points = self._embed_multimodal_chunks(
                    multimodal_chunks, len(chunks), metadata, file_path
                )

            # Bug #1502 (Codex finding 4): pending_multimodal_points is the
            # POST-embedding-attempt result -- unlike multimodal_chunks
            # (the pre-embedding list, always non-empty when multimodal
            # chunks existed regardless of whether every one of their
            # embedding attempts later failed), it correctly reflects
            # whether anything was actually produced. A multimodal-only
            # file whose every embedding attempt failed must be detected
            # here, never silently reported as a successful no-op.
            if (
                not batch_futures
                and not pending_multimodal_points
                and not cached_points
            ):
                logger.warning(f"No batches created for {file_path}")
                return FileProcessingResult(
                    success=False,
                    file_path=file_path,
                    chunks_processed=0,
                    processing_time=time.time() - start_time,
                    error="No batches created",
                )

            logger.debug(f"Submitted {len(batch_futures)} batches for {file_path}")

            slot_tracker.update_slot(slot_id, FileStatus.FINALIZING)

            # PROGRESS REPORTING ADJUSTMENT: Remove intermediate callback
            # Status updates tracked by CleanSlotTracker, progress callback only on completion

            # Phase 3: MULTI-BATCH RESULT PROCESSING - Process all batch embeddings with chunk mapping
            file_points = []
            all_embeddings: List[List[float]] = []

            try:
                # Collect embeddings from all batches in order
                for batch_future in batch_futures:
                    batch_result = batch_future.result()

                    # CRITICAL: Validate each batch result
                    if (
                        batch_result
                        and not batch_result.error
                        and batch_result.embeddings
                    ):
                        all_embeddings.extend(
                            [list(emb) for emb in batch_result.embeddings]
                        )
                    else:
                        # ATOMIC FAILURE: Any batch fails, fail entire file
                        error_msg = (
                            batch_result.error if batch_result else "No batch result"
                        )
                        logger.error(
                            f"Batch vector processing failed for {file_path}: {error_msg}"
                        )
                        return FileProcessingResult(
                            success=False,
                            file_path=file_path,
                            chunks_processed=0,
                            processing_time=time.time() - start_time,
                            error=f"Batch processing failed: {error_msg}",
                        )

                # CRITICAL: Validate total embedding count matches REGULAR chunks only
                # (multimodal chunks are processed separately and not included in all_embeddings)
                if len(all_embeddings) != len(regular_chunks):
                    logger.error(
                        f"Total embedding count mismatch: {len(all_embeddings)} embeddings for {len(regular_chunks)} regular chunks in {file_path}"
                    )
                    return FileProcessingResult(
                        success=False,
                        file_path=file_path,
                        chunks_processed=0,
                        processing_time=time.time() - start_time,
                        error="Total embedding count mismatch",
                    )

                # Create points with preserved order: regular_chunks[i] → embeddings[i] → points[i]
                #
                # Bug #1502: chunk_index/total_chunks MUST be the chunker's
                # own positional values (chunk["chunk_index"], set once by
                # FixedSizeChunker before any embedding/caching happens),
                # NEVER an enumerate() position among whichever chunks
                # happened to survive to a fresh embedding this run. A
                # falsy embedding fails the WHOLE file loudly instead of
                # being silently dropped -- silently skipping a chunk used
                # to also shift every LATER chunk's re-derived
                # position-based label, forging colliding point_ids with
                # different content across runs (the confirmed root cause
                # of issue #1502).
                for chunk, embedding in zip(regular_chunks, all_embeddings):
                    if not embedding:  # Validate individual embedding
                        logger.error(
                            f"Invalid/empty embedding for chunk_index="
                            f"{chunk['chunk_index']} in {file_path} -- "
                            f"failing the file (Bug #1502: never silently "
                            f"skip a chunk, it forges duplicate point_ids "
                            f"by shifting every later chunk's label)"
                        )
                        return FileProcessingResult(
                            success=False,
                            file_path=file_path,
                            chunks_processed=0,
                            processing_time=time.time() - start_time,
                            error=(
                                f"Invalid embedding for chunk_index="
                                f"{chunk['chunk_index']}"
                            ),
                        )
                    file_points.append(
                        {
                            "text": chunk["text"],
                            "vector": embedding,  # Direct embedding from batch result
                            "chunk_index": chunk["chunk_index"],
                            "total_chunks": chunk["total_chunks"],
                            "metadata": {
                                **metadata,
                                "line_start": chunk["line_start"],
                                "line_end": chunk["line_end"],
                            },
                        }
                    )

            except Exception as e:
                logger.error(f"Batch vector processing failed for {file_path}: {e}")
                return FileProcessingResult(
                    success=False,
                    file_path=file_path,
                    chunks_processed=0,
                    processing_time=time.time() - start_time,
                    error=f"Batch processing failed: {e}",
                )

            # Phase 4: Atomic write to vector storage if we have valid vectors
            if file_points or cached_points or pending_multimodal_points:
                try:
                    points_data = []
                    for point in file_points:
                        # Bug #1502: chunk_index/total_chunks come from the
                        # chunker's own positional values carried through
                        # file_points above -- NEVER re-derived from this
                        # loop's own enumerate() position, which would
                        # reintroduce the exact same identity-shifting bug
                        # this fix closes.
                        chunk_data = {
                            "text": point["text"],
                            "chunk_index": point["chunk_index"],
                            "total_chunks": point["total_chunks"],
                            "line_start": point["metadata"].get("line_start"),
                            "line_end": point["metadata"].get("line_end"),
                            "file_extension": file_path.suffix.lstrip(".") or "txt",
                        }

                        # Use the existing _create_vector_point method to ensure proper formatting
                        vector_point = self._create_vector_point(
                            chunk_data, point["vector"], point["metadata"], file_path
                        )
                        points_data.append(vector_point)

                    # Story #470: Add cached points (reused vectors) to upsert batch
                    if cached_points:
                        points_data.extend(cached_points)

                    # Atomic write to vector storage (only if there ARE
                    # regular/cached points -- a file consisting solely of
                    # multimodal chunks legitimately has none).
                    if points_data:
                        success = self.vector_store_client.upsert_points(
                            points=points_data,
                            collection_name=metadata.get("collection_name"),
                        )
                        if not success:
                            raise RuntimeError(
                                f"Failed to write {len(points_data)} points to vector storage"
                            )

                    # Bug #1502 M6: multimodal points are written HERE --
                    # only now that every regular embedding has been
                    # validated (Phase 3) and, if any exist, successfully
                    # written above. Never earlier: writing them before
                    # regular-embedding validation would leave multimodal
                    # points persisted even when a later falsy regular
                    # embedding fails the whole file (file-atomicity
                    # violation).
                    if pending_multimodal_points:
                        multimodal_by_collection: Dict[str, List[Dict[str, Any]]] = {}
                        for entry in pending_multimodal_points:
                            multimodal_by_collection.setdefault(
                                entry["collection_name"], []
                            ).append(entry["vector_point"])

                        for (
                            mm_collection_name,
                            mm_points,
                        ) in multimodal_by_collection.items():
                            mm_success = self.vector_store_client.upsert_points(
                                points=mm_points,
                                collection_name=mm_collection_name,
                            )
                            if not mm_success:
                                raise RuntimeError(
                                    f"Failed to write {len(mm_points)} "
                                    f"multimodal point(s) to vector storage"
                                )
                        logger.debug(
                            f"Successfully wrote {len(pending_multimodal_points)} "
                            f"multimodal point(s) for {file_path}"
                        )

                    logger.debug(
                        f"Successfully wrote {len(points_data)} points for {file_path}"
                    )

                    # Bug #1761/#2056: replace EVERY FTS document of this
                    # file by one per current chunk -- the chunker's full
                    # output, whatever its vector path (newly embedded,
                    # reused from the embedding cache, multimodal). The ONE
                    # shared implementation (cidx watch uses it too); see
                    # its docstring for the delete-failure semantics.
                    if self.fts_manager:
                        self._replace_fts_documents(file_path, chunks)

                except (
                    sqlite3.DatabaseError,
                    OSError,
                    ChunkStoreUnavailableError,
                ) as e:
                    # Bug #1746 Change 1 (H1/H2 code-review refinements):
                    # a FATAL chunk-store-open/write failure (e.g. a
                    # root-owned/unwritable chunks.db, a corrupt database,
                    # or disk-full) must NEVER be silently converted into
                    # an ordinary per-file failure result -- doing so let
                    # the batch keep running to the end of the repo,
                    # burning CPU on every remaining file for hours before
                    # anyone noticed (production incident, see GitHub
                    # issue #1746). Re-raise (wrapped, if not already the
                    # typed error) so HighThroughputProcessor (Change 2)
                    # and SmartIndexer (Change 3) can distinguish this
                    # from a normal per-file failure and abort the whole
                    # run instead of continuing.
                    #
                    # H1: sqlite3.DatabaseError/OperationalError caught
                    # here can ALSO be transient lock contention (the
                    # CHUNKS_DB write path opens a fresh connection per
                    # upsert_points() call with no cross-thread
                    # application lock -- expected under concurrent
                    # writers, not exceptional). is_fatal_chunk_store_write_error()
                    # excludes "database is locked"/"database table is
                    # locked" from the fatal classification so a
                    # transient lock failure only fails this one file.
                    if isinstance(e, ChunkStoreUnavailableError):
                        logger.error(
                            f"Fatal chunk-store failure writing {file_path}: {e}"
                        )
                        raise
                    if is_fatal_chunk_store_write_error(e):
                        logger.error(
                            f"Fatal chunk-store failure writing {file_path}: {e}"
                        )
                        raise ChunkStoreUnavailableError(
                            f"Chunk store unavailable while writing {file_path}: {e}"
                        ) from e
                    # Not fatal (H1 transient lock contention) -- fail
                    # only this file, exactly like the generic
                    # except-Exception branch below.
                    return _vector_storage_failure_result(file_path, e, start_time)
                except Exception as e:
                    return _vector_storage_failure_result(file_path, e, start_time)

            processing_time = time.time() - start_time

            # Mark as complete
            slot_tracker.update_slot(slot_id, FileStatus.COMPLETE)

            # PROGRESS REPORTING ADJUSTMENT: File completion callback
            # This is the ONLY progress callback - when file truly completes
            # HighThroughputProcessor will handle file count updates and metrics
            if progress_callback:
                concurrent_files = (
                    slot_tracker.get_concurrent_files_data() if slot_tracker else []
                )
                progress_callback(
                    -1,  # Signal: display update only, no progress bar change
                    -1,  # Signal: display update only
                    file_path,
                    concurrent_files=concurrent_files,
                )

            return FileProcessingResult(
                success=True,
                file_path=file_path,
                chunks_processed=len(file_points),
                processing_time=processing_time,
                error=None,
            )

        except ChunkStoreUnavailableError:
            # Bug #1746 Change 1: never let this fatal error be re-caught
            # here and converted back into a per-file FileProcessingResult
            # -- it must propagate all the way out of this method. The
            # `finally` block below still runs (slot release) exactly as
            # on every other exception path.
            raise
        except Exception as e:
            processing_time = time.time() - start_time
            error_msg = f"File processing failed: {e}"
            logger.error(f"Error processing {file_path}: {error_msg}")

            # Mark file failed
            slot_tracker.update_slot(slot_id, FileStatus.COMPLETE)

            # PROGRESS REPORTING ADJUSTMENT: Error file completion callback
            if progress_callback:
                concurrent_files = (
                    slot_tracker.get_concurrent_files_data() if slot_tracker else []
                )
                progress_callback(
                    -1,  # Signal: display update only, no progress bar change
                    -1,  # Signal: display update only
                    file_path,
                    concurrent_files=concurrent_files,
                )

            return FileProcessingResult(
                success=False,
                file_path=file_path,
                chunks_processed=0,
                processing_time=processing_time,
                error=error_msg,
            )

        finally:
            # SINGLE release - guaranteed (CLAUDE.md Foundation #8 compliance)
            if slot_id is not None:
                slot_tracker.release_slot(slot_id)
