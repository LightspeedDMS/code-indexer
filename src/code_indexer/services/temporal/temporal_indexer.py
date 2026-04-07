"""TemporalIndexer - Index git history with commit message search.

BREAKING CHANGE (Story 2.1 Reimplementation): Payload structure changed.
Users MUST re-index with: cidx index --index-commits --force
Changes: Added 'type' field, removed 'chunk_text' storage, added commit message indexing.
"""

import json
import logging
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty
from typing import List, Optional, Callable

from ...config import ConfigManager
from ...indexing.fixed_size_chunker import FixedSizeChunker
from ...services.vector_calculation_manager import VectorCalculationManager
from ...services.file_identifier import FileIdentifier
from ...storage.filesystem_vector_store import FilesystemVectorStore

from .models import CommitInfo
from .temporal_collection_naming import LEGACY_TEMPORAL_COLLECTION
from .temporal_diff_scanner import TemporalDiffScanner
from .temporal_progressive_metadata import TemporalProgressiveMetadata

logger = logging.getLogger(__name__)

# Each indexed diff produces approximately this many vectors (diff chunk + commit
# message + metadata).  Used only for the approximate_vectors_created statistic
# written to temporal_meta.json — not a correctness-critical value.
_APPROX_VECTORS_PER_UNIT = 3


@dataclass
class IndexingResult:
    """Result of temporal indexing operation.

    Fields:
        total_commits: Number of commits processed
        files_processed: Number of changed files analyzed across all commits
        approximate_vectors_created: Approximate number of vectors created (includes diff chunk vectors and commit message vectors)
        skip_ratio: Ratio of commits skipped (0.0 = none skipped, 1.0 = all skipped)
        branches_indexed: List of branch names indexed
        commits_per_branch: Dictionary mapping branch names to commit counts
    """

    total_commits: int
    files_processed: int
    approximate_vectors_created: int
    skip_ratio: float
    branches_indexed: List[str]
    commits_per_branch: dict


class TemporalIndexer:
    """Orchestrates git history indexing with commit-based change tracking.

    This class coordinates the temporal indexing workflow:
    1. Get commit history from git
    2. Filter already-processed commits using progressive metadata
    3. For each new commit, extract file diffs and create vectors
    4. Track processed commits and store vectors with commit metadata
    """

    # Batch retry configuration
    MAX_RETRIES = 5
    RETRY_DELAYS = [2, 5, 10, 30, 60]  # Exponential backoff delays in seconds

    def __init__(
        self,
        config_manager: ConfigManager,
        vector_store: FilesystemVectorStore,
        collection_name: str = LEGACY_TEMPORAL_COLLECTION,
    ):
        """Initialize temporal indexer.

        Args:
            config_manager: Configuration manager
            vector_store: Filesystem vector store for storage
            collection_name: Logical collection identifier (not a filesystem path).
                Must be non-empty, contain no path separators (/ or \\),
                no parent-directory segments (. or ..), and not be an absolute path.
                Default: 'code-indexer-temporal' (legacy backward-compat).
        """
        from pathlib import PurePosixPath, PureWindowsPath

        name = collection_name.strip() if isinstance(collection_name, str) else ""
        if not name:
            raise ValueError("collection_name must not be empty")
        if name in {".", ".."}:
            raise ValueError(
                f"collection_name cannot be '.' or '..': {collection_name!r}"
            )
        if any(sep in name for sep in ("/", "\\")):
            raise ValueError(
                f"collection_name must be a plain name with no path separators: {collection_name!r}"
            )
        if PurePosixPath(name).is_absolute() or PureWindowsPath(name).is_absolute():
            raise ValueError(
                f"collection_name must be a plain name, not an absolute path: {collection_name!r}"
            )

        self.config_manager = config_manager
        self.config = config_manager.get_config()
        self.vector_store = vector_store
        self.collection_name = name

        # Use vector store's project_root as the codebase directory
        self.codebase_dir = vector_store.project_root

        # Initialize FileIdentifier for project_id lookup
        self.file_identifier = FileIdentifier(self.codebase_dir, self.config)

        # Initialize temporal directory using collection path to consolidate all data
        # This ensures metadata and vectors are in the same location
        self.temporal_dir = self.vector_store.base_path / self.collection_name
        self.temporal_dir.mkdir(parents=True, exist_ok=True)

        # Initialize override filter service if override config exists
        override_filter_service = None
        if (
            hasattr(self.config, "override_config")
            and self.config.override_config is not None
            and not str(type(self.config.override_config)).startswith(
                "<class 'unittest.mock"
            )
        ):
            try:
                from ...services.override_filter_service import OverrideFilterService

                override_filter_service = OverrideFilterService(
                    self.config.override_config
                )
            except (TypeError, AttributeError):
                # Skip override filtering if initialization fails (e.g., mock objects)
                pass

        # Initialize components with diff_context_lines from config
        diff_context_lines = self.config.temporal.diff_context_lines
        try:
            file_extensions = (
                list(self.config.file_extensions)
                if self.config.file_extensions
                else None
            )
        except TypeError:
            file_extensions = None
        self.diff_scanner = TemporalDiffScanner(
            self.codebase_dir,
            override_filter_service=override_filter_service,
            diff_context_lines=diff_context_lines,
            file_extensions=file_extensions,
        )
        self.chunker = FixedSizeChunker(self.config)

        # Initialize blob registry for tracking indexed content
        # Keyed by collection_name to support dual-provider indexing (Story #631)
        self.indexed_blobs: dict[str, set[str]] = {}

        # Initialize progressive metadata tracker for resume capability (primary collection)
        self.progressive_metadata = TemporalProgressiveMetadata(self.temporal_dir)

        # Per-collection progress trackers (Story #631 dual indexing)
        self._progress_by_collection: dict[str, TemporalProgressiveMetadata] = {}

        # Ensure temporal vector collection exists
        self._ensure_temporal_collection()

    def load_completed_commits(self):
        """Load completed commits from progressive metadata."""
        # Initialize progressive metadata if not already done
        if not hasattr(self, "progressive_metadata"):
            self.progressive_metadata = TemporalProgressiveMetadata(self.temporal_dir)
        return self.progressive_metadata.load_completed()

    def _classify_batch_error(self, error_message: str) -> str:
        """Classify error as transient, permanent, or rate_limit."""
        error_lower = error_message.lower()

        # Rate limit detection
        if "429" in error_message or "rate limit" in error_lower:
            return "rate_limit"

        # Permanent errors (client-side issues)
        permanent_patterns = ["400", "401", "403", "404", "unauthorized", "invalid"]
        if any(pattern in error_lower for pattern in permanent_patterns):
            return "permanent"

        # Transient errors (server/network - retryable)
        transient_patterns = [
            "timeout",
            "503",
            "502",
            "500",
            "connection reset",
            "connection refused",
            "network",
            "timed out",
        ]
        if any(pattern in error_lower for pattern in transient_patterns):
            return "transient"

        return "permanent"

    def _get_progress(self, collection_name: str) -> "TemporalProgressiveMetadata":
        """Return (or create) per-collection TemporalProgressiveMetadata instance.

        Creates the collection directory on demand if it does not yet exist.

        Args:
            collection_name: Logical collection identifier

        Returns:
            TemporalProgressiveMetadata instance for the given collection
        """
        if collection_name not in self._progress_by_collection:
            coll_dir = self.vector_store.base_path / collection_name
            coll_dir.mkdir(parents=True, exist_ok=True)
            self._progress_by_collection[collection_name] = TemporalProgressiveMetadata(
                coll_dir
            )
        return self._progress_by_collection[collection_name]

    def _get_all_provider_configs(self):
        """Get all configured provider collections for dual indexing.

        Returns a list of (collection_name, embedding_provider, model_name) tuples
        for every provider that has a valid API key. If no providers are configured,
        falls back to the primary collection so single-provider behaviour is unchanged.

        Returns:
            List of (collection_name, embedding_provider_instance, model_name) tuples
        """
        from .temporal_collection_naming import (
            resolve_temporal_collection_name,
            get_model_name_for_provider,
        )
        from ..embedding_factory import EmbeddingProviderFactory

        providers = []
        configured = EmbeddingProviderFactory.get_configured_providers(self.config)

        for provider_name in configured:
            try:
                model_name = get_model_name_for_provider(provider_name, self.config)
                coll_name = resolve_temporal_collection_name(model_name)
                provider = EmbeddingProviderFactory.create(
                    self.config, provider_name=provider_name
                )
                providers.append((coll_name, provider, model_name))
            except Exception as e:
                logger.warning(
                    "Failed to create provider %s for temporal indexing: %s",
                    provider_name,
                    e,
                )

        # Fallback: if no providers configured, use primary collection
        if not providers:
            fallback_provider = EmbeddingProviderFactory.create(config=self.config)
            providers.append((self.collection_name, fallback_provider, "unknown"))

        return providers

    def _ensure_temporal_collection(self):
        """Ensure temporal vector collection exists.

        Creates the temporal collection if it doesn't exist. Dimensions vary by model.
        """
        from ...services.embedding_factory import EmbeddingProviderFactory

        provider_info = EmbeddingProviderFactory.get_provider_model_info(self.config)
        vector_size = provider_info.get(
            "dimensions", 1024
        )  # Default to voyage-code-3 dims

        # Check if collection exists, create if not
        if not self.vector_store.collection_exists(self.collection_name):
            logger.info(
                f"Creating temporal collection '{self.collection_name}' with dimension={vector_size}"
            )
            self.vector_store.create_collection(self.collection_name, vector_size)

    def _count_tokens(self, text: str, vector_manager) -> int:
        """Count tokens using provider-specific token counting.

        For VoyageAI: Use official tokenizer for accurate counting
        For Voyage/other providers: Estimate based on character count
        """
        # Check if we're using VoyageAI provider
        provider_name = vector_manager.embedding_provider.__class__.__name__
        is_voyageai_provider = "VoyageAI" in provider_name

        if is_voyageai_provider:
            # Check if provider has the _count_tokens_accurately method (real provider)
            if hasattr(vector_manager.embedding_provider, "_count_tokens_accurately"):
                return int(
                    vector_manager.embedding_provider._count_tokens_accurately(text)
                )

            # Fallback: Use VoyageTokenizer directly
            from ..embedded_voyage_tokenizer import VoyageTokenizer

            model = vector_manager.embedding_provider.get_current_model()
            return VoyageTokenizer.count_tokens([text], model=model)

        # Fallback: Rough estimate (4 chars ≈ 1 token for English text)
        # This is conservative and works for batching purposes
        return len(text) // 4

    def index_commits(
        self,
        all_branches: bool = False,
        max_commits: Optional[int] = None,
        since_date: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
        reconcile: bool = False,
    ) -> IndexingResult:
        """Index git commit history with commit-based change tracking.

        Args:
            all_branches: If True, index all branches; if False, current branch only
            max_commits: Maximum number of commits to index per branch
            since_date: Index commits since this date (YYYY-MM-DD)
            progress_callback: Progress callback function
            reconcile: If True, reconcile disk state with git history (crash recovery)

        Returns:
            IndexingResult with statistics
        """
        # Step 1: Get commit history
        commits_from_git = self._get_commit_history(
            all_branches, max_commits, since_date
        )
        if not commits_from_git:
            return IndexingResult(
                total_commits=0,
                files_processed=0,
                approximate_vectors_created=0,
                skip_ratio=1.0,  # All commits skipped (none to process)
                branches_indexed=[],
                commits_per_branch={},
            )

        # Step 1.5: Reconciliation (if requested) - discover indexed commits from disk
        if reconcile:
            from .temporal_reconciliation import reconcile_temporal_index

            logger.info("Reconciling disk state with git history...")
            missing_commits = reconcile_temporal_index(
                self.vector_store, commits_from_git, self.collection_name
            )

            # Log reconciliation summary
            indexed_count = len(commits_from_git) - len(missing_commits)
            logger.info(
                f"Reconciliation complete: {indexed_count} indexed, "
                f"{len(missing_commits)} missing ({indexed_count * 100 // (len(commits_from_git) or 1)}% complete)"
            )

            # Replace commits_from_git with only missing commits
            commits_from_git = missing_commits

            # If all commits indexed, skip to index rebuild
            if not commits_from_git:
                logger.info("All commits already indexed, rebuilding indexes only...")
                # Still rebuild indexes (AC4)
                self.vector_store.end_indexing(collection_name=self.collection_name)
                return IndexingResult(
                    total_commits=0,
                    files_processed=0,
                    approximate_vectors_created=0,
                    skip_ratio=1.0,  # All commits already done
                    branches_indexed=[],
                    commits_per_branch={},
                )

        # Track total commits before filtering for skip_ratio calculation
        total_commits_before_filter = len(commits_from_git)

        # Filtering moved to _process_commits_parallel() for correct architecture
        # (Bug #8, #9 behavior maintained - verified by test_bug8_progressive_resume.py
        # and test_temporal_indexer_list_bounds.py)

        # Initialize incremental HNSW tracking for the temporal collection
        # This enables change tracking for efficient HNSW index updates
        self.vector_store.begin_indexing(self.collection_name)

        current_branch = self._get_current_branch()

        # Step 2: Process commits with parallel workers
        total_blobs_processed = 0
        total_vectors_created = 0

        # Import embedding provider
        from ...services.embedding_factory import EmbeddingProviderFactory

        embedding_provider = EmbeddingProviderFactory.create(config=self.config)

        # Use VectorCalculationManager for parallel processing
        # Select thread count based on the active embedding provider
        _provider = getattr(self.config, "embedding_provider", None)
        if _provider == "cohere" and hasattr(self.config, "cohere"):
            vector_thread_count = self.config.cohere.parallel_requests
        elif _provider == "voyage-ai" and hasattr(self.config, "voyage_ai"):
            vector_thread_count = self.config.voyage_ai.parallel_requests
        else:
            vector_thread_count = 4

        # Get config_dir for debug logging
        config_dir = self.config_manager.config_path.parent

        with VectorCalculationManager(
            embedding_provider, vector_thread_count, config_dir=config_dir
        ) as vector_manager:
            # Use parallel processing instead of sequential loop
            # Returns: (commits_processed_count, total_blobs_processed, total_vectors_created)
            commits_processed, total_blobs_processed, total_vectors_created = (
                self._process_commits_parallel(
                    commits_from_git,
                    embedding_provider,
                    vector_manager,
                    progress_callback,
                    reconcile,
                )
            )

        # Early return if no commits were processed (all filtered out)
        if total_blobs_processed == 0 and total_vectors_created == 0:
            return IndexingResult(
                total_commits=0,
                files_processed=0,
                approximate_vectors_created=0,
                skip_ratio=1.0,  # All commits skipped (already processed)
                branches_indexed=[],
                commits_per_branch={},
            )

        # Step 4: Calculate skip ratio (commits skipped due to already being processed)
        commits_skipped = total_commits_before_filter - commits_processed
        skip_ratio = (
            commits_skipped / total_commits_before_filter
            if total_commits_before_filter > 0
            else 1.0
        )

        # TODO: Get branches from git instead of database
        branches_indexed = [current_branch]  # Temporary fix - no SQLite

        self._save_temporal_metadata(
            last_commit=commits_from_git[-1].hash,
            total_commits=len(commits_from_git),
            files_processed=total_blobs_processed,
            approximate_vectors_created=total_vectors_created
            // _APPROX_VECTORS_PER_UNIT,
            branch_stats={"branches": branches_indexed, "per_branch_counts": {}},
            indexing_mode="all-branches" if all_branches else "single-branch",
            max_commits=max_commits,
            since_date=since_date,
        )

        return IndexingResult(
            total_commits=commits_processed,
            files_processed=total_blobs_processed,
            approximate_vectors_created=total_vectors_created,
            skip_ratio=skip_ratio,
            branches_indexed=branches_indexed,
            commits_per_branch={},
        )

    def _load_last_indexed_commit(self) -> Optional[str]:
        """Load last indexed commit from temporal_meta.json.

        Returns:
            Last indexed commit hash if available, None otherwise.
        """
        metadata_path = self.temporal_dir / "temporal_meta.json"
        if not metadata_path.exists():
            return None

        try:
            with open(metadata_path) as f:
                metadata = json.load(f)
            last_commit = metadata.get("last_commit")
            return last_commit if isinstance(last_commit, str) else None
        except (json.JSONDecodeError, IOError):
            logger.warning(f"Failed to load temporal metadata from {metadata_path}")
            return None

    def _get_commit_history(
        self, all_branches: bool, max_commits: Optional[int], since_date: Optional[str]
    ) -> List[CommitInfo]:
        """Get commit history from git."""
        # Load last indexed commit for incremental indexing
        last_indexed_commit = self._load_last_indexed_commit()

        # Use null byte delimiters to prevent pipe characters in commit messages from breaking parsing
        # Use %B (full body) instead of %s (subject only) to capture multi-paragraph commit messages
        # Use record separator (%x1e) at end of each record to enable correct parsing with multi-line messages
        cmd = [
            "git",
            "log",
            "--format=%H%x00%at%x00%an%x00%ae%x00%B%x00%P%x1e",
            "--reverse",
        ]

        # If we have a last indexed commit, only get commits after it
        if last_indexed_commit:
            # Use commit range to get only new commits
            cmd.insert(2, f"{last_indexed_commit}..HEAD")
            logger.info(
                f"Incremental indexing: Getting commits after {last_indexed_commit[:8]}"
            )

        if all_branches:
            cmd.append("--all")

        if since_date:
            cmd.extend(["--since", since_date])

        if max_commits:
            cmd.extend(["-n", str(max_commits)])

        result = subprocess.run(
            cmd,
            cwd=self.codebase_dir,
            capture_output=True,
            text=True,
            errors="replace",
            check=True,
        )

        commits = []
        # Split by record separator (%x1e) to handle multi-line commit messages correctly
        for record in result.stdout.strip().split("\x1e"):
            if record.strip():
                # Use null-byte delimiter to match git format (%x00)
                # This prevents pipe characters in commit messages from breaking parsing
                parts = record.split("\x00")
                if len(parts) >= 6:
                    # Strip trailing newline from message body (%B includes trailing newline)
                    message = parts[4].strip()
                    commits.append(
                        CommitInfo(
                            hash=parts[
                                0
                            ].strip(),  # Strip newlines from commit hash (BUG #1 FIX)
                            timestamp=int(parts[1]),
                            author_name=parts[2],
                            author_email=parts[3],
                            message=message,
                            parent_hashes=parts[
                                5
                            ].strip(),  # Strip newlines from parent hashes too
                        )
                    )

        return commits

    def _get_current_branch(self) -> str:
        """Get current branch name."""
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=self.codebase_dir,
            capture_output=True,
            text=True,
            errors="replace",
            check=True,
        )
        return result.stdout.strip() or "HEAD"

    def _process_commits_parallel(
        self,
        commits,
        embedding_provider,
        vector_manager,
        progress_callback=None,
        reconcile=None,
    ):
        """Process commits in parallel using queue-based architecture.

        Args:
            commits: List of commits to process
            embedding_provider: Embedding provider for vector generation
            vector_manager: Vector calculation manager
            progress_callback: Optional progress callback function
            reconcile: If False, use progressive metadata filtering for resume capability.
                      If True, skip filtering (disk reconciliation already filtered).
                      If None (default), skip filtering (no resume/reconciliation requested).
        """

        # Import CleanSlotTracker and related classes
        from ..clean_slot_tracker import CleanSlotTracker, FileStatus, FileData

        # Filter commits upfront using progressive metadata (only when reconcile=False for resume capability)
        # When reconcile=True, disk-based reconciliation already filtered commits in index_commits()
        # When reconcile=None (default), no filtering (normal indexing without resume)
        if reconcile is False:
            completed_commits = self.progressive_metadata.load_completed()
            commits = [c for c in commits if c.hash not in completed_commits]

        # Load existing point IDs to avoid duplicate processing
        # Create a copy to avoid mutating the store's data structure
        existing_ids = set(self.vector_store.load_id_index(self.collection_name))
        logger.info(
            f"Loaded {len(existing_ids)} existing temporal points to avoid re-indexing"
        )

        # Get thread count from config
        thread_count = (
            getattr(self.config.voyage_ai, "parallel_requests", 8)
            if hasattr(self.config, "voyage_ai")
            else 8
        )

        # Create slot tracker with max_slots = thread_count (not thread_count + 2)
        commit_slot_tracker = CleanSlotTracker(max_slots=thread_count)

        # Initialize with correct pattern - show actual total, not 0
        if progress_callback:
            try:
                progress_callback(
                    0,
                    len(commits),  # Actual total for progress bar
                    Path(""),
                    info=f"0/{len(commits)} commits (0%) | 0.0 commits/s | 0.0 KB/s | {thread_count} threads | 📝 ???????? - initializing",
                    concurrent_files=commit_slot_tracker.get_concurrent_files_data(),
                    slot_tracker=commit_slot_tracker,
                    item_type="commits",
                )
            except TypeError:
                # Fallback for old signature without slot_tracker
                progress_callback(
                    0,
                    len(commits),  # Actual total for progress bar
                    Path(""),
                    info=f"0/{len(commits)} commits (0%) | 0.0 commits/s | 0.0 KB/s | {thread_count} threads | 📝 ???????? - initializing",
                    item_type="commits",
                )

        # Track progress with thread-safe shared state
        completed_count = [0]  # Mutable list for thread-safe updates
        total_files_processed = [0]  # Track total number of files across all commits
        last_completed_commit = [None]  # Track last completed commit hash
        last_completed_file = [None]  # Track last completed file
        total_bytes_processed = [0]  # Thread-safe accumulator for KB/s calculation
        progress_lock = threading.Lock()
        start_time = time.time()

        # Create queue and add commits
        commit_queue = Queue()
        for commit in commits:
            commit_queue.put(commit)

        def worker():
            """Worker function to process commits from queue.

            ARCHITECTURE: Acquire slot with ACTUAL file info, not placeholder.
            1. Get diffs first
            2. Acquire slot with filename from first diff
            3. Process all diffs for commit
            """
            nonlocal total_bytes_processed  # Access shared byte counter
            while True:
                # TIMEOUT ARCHITECTURE FIX: Check cancellation before getting next commit
                if vector_manager.cancellation_event.is_set():
                    logger.info("Worker cancelled - exiting gracefully")
                    break

                try:
                    commit = commit_queue.get_nowait()
                except Empty:
                    break

                slot_id = None
                commit_had_errors = False  # Track if this commit had any errors
                commit_point_ids = []  # Track point IDs for potential rollback
                try:
                    # Acquire slot IMMEDIATELY (BEFORE get_diffs) with placeholder
                    placeholder_filename = f"{commit.hash[:8]} - Analyzing commit"
                    slot_id = commit_slot_tracker.acquire_slot(
                        FileData(
                            filename=placeholder_filename,
                            file_size=0,
                            status=FileStatus.STARTING,
                        )
                    )

                    # Update slot to show "Analyzing commit" status
                    commit_slot_tracker.update_slot(
                        slot_id,
                        FileStatus.STARTING,
                        filename=placeholder_filename,
                        file_size=0,
                    )

                    # Get diffs (potentially slow git operation)
                    diffs = self.diff_scanner.get_diffs_for_commit(commit.hash)

                    # AC1: Index commit message as searchable entity (Story #476)
                    # This creates commit_message chunks that can be searched alongside code diffs
                    project_id = self.file_identifier.get_project_id()
                    self._index_commit_message(commit, project_id, vector_manager)

                    # Track last file processed for THIS commit (local to this worker)
                    last_file_for_commit = Path(".")  # Default if no diffs

                    # Track file count for this commit (BUG #2 FIX: moved before if/else)
                    files_in_this_commit = len(diffs)

                    # If no diffs, mark complete and continue
                    if not diffs:
                        commit_slot_tracker.update_slot(slot_id, FileStatus.COMPLETE)
                    else:
                        # BATCHED EMBEDDINGS: Collect all chunks from all diffs first
                        # Then batch them into minimal API calls
                        all_chunks_data = []
                        project_id = self.file_identifier.get_project_id()
                        total_commit_size = (
                            0  # Accumulate total size of all diffs in commit
                        )

                        # Phase 1: Collect all chunks from all diffs
                        # (files_in_this_commit already set above)

                        for diff_info in diffs:
                            # Update slot with current file information (no release/reacquire)
                            current_filename = (
                                f"{commit.hash[:8]} - {Path(diff_info.file_path).name}"
                            )
                            diff_size = (
                                len(diff_info.diff_content)
                                if diff_info.diff_content
                                else 0
                            )
                            total_commit_size += (
                                diff_size  # Accumulate total size for this commit
                            )

                            # Accumulate bytes for KB/s calculation (thread-safe)
                            with progress_lock:
                                total_bytes_processed[0] += diff_size

                            commit_slot_tracker.update_slot(
                                slot_id,
                                FileStatus.CHUNKING,
                                filename=current_filename,
                                file_size=diff_size,
                            )

                            # Update last file for THIS commit (local variable)
                            last_file_for_commit = Path(diff_info.file_path)
                            # Skip binary and renamed files (metadata only)
                            if diff_info.diff_type in ["binary", "renamed"]:
                                continue

                            # Skip if blob already indexed (avoid duplicate processing)
                            # indexed_blobs is dict[collection_name, set[blob_hash]] (Story #631)
                            if (
                                diff_info.blob_hash
                                and diff_info.blob_hash
                                in self.indexed_blobs.get(self.collection_name, set())
                            ):
                                continue

                            # Chunk the diff content
                            chunks = self.chunker.chunk_text(
                                diff_info.diff_content, Path(diff_info.file_path)
                            )

                            if chunks:
                                # BUG #7 FIX: Check point existence BEFORE collecting chunks
                                # Build point IDs first to check existence
                                for j, chunk in enumerate(chunks):
                                    point_id = f"{project_id}:diff:{commit.hash}:{diff_info.file_path}:{j}"

                                    # Skip if point already exists
                                    if point_id not in existing_ids:
                                        # Collect chunk with all metadata needed for point creation
                                        all_chunks_data.append(
                                            {
                                                "chunk": chunk,
                                                "chunk_index": j,
                                                "diff_info": diff_info,
                                                "point_id": point_id,
                                            }
                                        )

                        # Phase 2: Batch all chunks and submit API calls with token-aware batching
                        if all_chunks_data:
                            # Show initial state with 0% progress
                            commit_slot_tracker.update_slot(
                                slot_id,
                                FileStatus.VECTORIZING,
                                filename=f"{commit.hash[:8]} - Vectorizing 0% (0/{len(all_chunks_data)} chunks)",
                                file_size=total_commit_size,  # Show total size of all diffs in commit
                            )

                            # Token-aware batching: split chunks into multiple batches if needed
                            # to respect 120,000 token limit (90% safety margin = 108,000)
                            model_limit = vector_manager.embedding_provider._get_model_token_limit()
                            TOKEN_LIMIT = int(model_limit * 0.9)  # 90% safety margin

                            # First, calculate all batch indices (don't submit yet)
                            batch_indices_list = []
                            current_batch_indices = []
                            current_tokens = 0

                            for i, chunk_data in enumerate(all_chunks_data):
                                chunk_text = chunk_data["chunk"]["text"]
                                chunk_tokens = self._count_tokens(
                                    chunk_text, vector_manager
                                )

                                # If this chunk would exceed TOKEN limit OR ITEM COUNT limit (1000), save current batch
                                if (
                                    current_tokens + chunk_tokens > TOKEN_LIMIT
                                    or len(current_batch_indices) >= 1000
                                ) and current_batch_indices:
                                    batch_indices_list.append(current_batch_indices)
                                    current_batch_indices = []
                                    current_tokens = 0

                                # Add chunk to current batch
                                current_batch_indices.append(i)
                                current_tokens += chunk_tokens

                            # Save final batch if not empty
                            if current_batch_indices:
                                batch_indices_list.append(current_batch_indices)

                            # Submit and process batches in waves to prevent monopolization
                            max_concurrent = getattr(
                                self.config.voyage_ai,
                                "max_concurrent_batches_per_commit",
                                10,
                            )
                            all_embeddings = []

                            # Process batches in waves of max_concurrent
                            for wave_start in range(
                                0, len(batch_indices_list), max_concurrent
                            ):
                                # TIMEOUT ARCHITECTURE FIX: Check cancellation between waves
                                if vector_manager.cancellation_event.is_set():
                                    logger.warning(
                                        f"Commit {commit.hash[:8]}: Cancelled mid-processing - exiting wave loop"
                                    )
                                    commit_had_errors = True
                                    break

                                wave_end = min(
                                    wave_start + max_concurrent, len(batch_indices_list)
                                )
                                wave_batches = batch_indices_list[wave_start:wave_end]

                                # Submit this wave of batches
                                wave_futures = []
                                for batch_indices in wave_batches:
                                    batch_texts = [
                                        all_chunks_data[idx]["chunk"]["text"]
                                        for idx in batch_indices
                                    ]
                                    batch_future = vector_manager.submit_batch_task(
                                        batch_texts, {}
                                    )
                                    # Store (future, batch_indices) tuple for progress tracking
                                    # Allows calculating percentage completion after each batch
                                    wave_futures.append((batch_future, batch_indices))

                                # Wait for this wave to complete
                                batch_num = 0
                                for batch_future, batch_indices in wave_futures:
                                    batch_num += 1

                                    # Retry loop for this batch
                                    attempt = 0
                                    success = False
                                    last_error = None

                                    while attempt < self.MAX_RETRIES and not success:
                                        try:
                                            batch_result = batch_future.result()

                                            if batch_result.error:
                                                last_error = batch_result.error
                                                error_type = self._classify_batch_error(
                                                    batch_result.error
                                                )

                                                if error_type == "permanent":
                                                    logger.error(
                                                        f"Commit {commit.hash[:8]}: Permanent error, no retry: {batch_result.error}"
                                                    )
                                                    break  # Exit retry loop

                                                if attempt >= self.MAX_RETRIES - 1:
                                                    logger.error(
                                                        f"Commit {commit.hash[:8]}: Retry exhausted after {self.MAX_RETRIES} attempts"
                                                    )
                                                    break

                                                # Determine delay
                                                if error_type == "rate_limit":
                                                    delay = 60
                                                    logger.warning(
                                                        f"Commit {commit.hash[:8]}: Rate limit detected, waiting {delay}s"
                                                    )
                                                else:  # transient
                                                    delay = self.RETRY_DELAYS[attempt]
                                                    logger.warning(
                                                        f"Commit {commit.hash[:8]}: Batch {batch_num} retry {attempt + 1}/{self.MAX_RETRIES} "
                                                        f"in {delay}s: {batch_result.error}"
                                                    )

                                                time.sleep(delay)
                                                attempt += 1

                                                # Resubmit batch
                                                batch_texts = [
                                                    all_chunks_data[idx]["chunk"][
                                                        "text"
                                                    ]
                                                    for idx in batch_indices
                                                ]
                                                batch_future = (
                                                    vector_manager.submit_batch_task(
                                                        batch_texts, {}
                                                    )
                                                )
                                                continue
                                            else:
                                                # Success
                                                success = True
                                                all_embeddings.extend(
                                                    batch_result.embeddings
                                                )

                                                # DYNAMIC PROGRESS UPDATE: Show percentage and chunk count
                                                chunks_vectorized = len(all_embeddings)
                                                total_chunks = len(all_chunks_data)
                                                progress_pct = (
                                                    (chunks_vectorized * 100)
                                                    // total_chunks
                                                    if total_chunks > 0
                                                    else 0
                                                )

                                                # Update slot with dynamic progress (shows movement)
                                                commit_slot_tracker.update_slot(
                                                    slot_id,
                                                    FileStatus.VECTORIZING,
                                                    filename=f"{commit.hash[:8]} - Vectorizing {progress_pct}% ({chunks_vectorized}/{total_chunks} chunks)",
                                                    file_size=total_commit_size,  # Keep total size consistent
                                                )

                                        except Exception as e:
                                            logger.error(
                                                f"Commit {commit.hash[:8]}: Batch exception: {e}",
                                                exc_info=True,
                                            )
                                            last_error = str(e)
                                            break

                                    if not success:
                                        # Batch failed after retries
                                        logger.error(
                                            f"Commit {commit.hash[:8]}: Batch {batch_num} FAILED after {attempt} attempts, "
                                            f"last error: {last_error}"
                                        )
                                        commit_had_errors = True
                                        break  # Exit wave loop

                                # If errors occurred in this wave, stop processing remaining waves
                                if commit_had_errors:
                                    break

                            # Anti-Fallback: Exit immediately if errors occurred
                            # No rollback needed - points not yet persisted to vector store
                            if commit_had_errors:
                                raise RuntimeError(
                                    f"Commit {commit.hash[:8]} processing failed after batch retry exhaustion. "
                                    f"No points were persisted to maintain index consistency."
                                )

                            # Create result object with merged embeddings
                            from types import SimpleNamespace

                            result = SimpleNamespace(embeddings=all_embeddings)

                            # Validate embedding count matches chunk count
                            if len(result.embeddings) != len(all_chunks_data):
                                raise RuntimeError(
                                    f"Embedding count mismatch: Expected {len(all_chunks_data)} embeddings, "
                                    f"got {len(result.embeddings)}. API may have returned partial results."
                                )

                            # Phase 3: Create points from results
                            if result.embeddings:
                                # Finalize (store)
                                commit_slot_tracker.update_slot(
                                    slot_id, FileStatus.FINALIZING
                                )
                                # Create points with correct payload structure
                                points = []

                                # Map embeddings back to chunks using all_chunks_data
                                for chunk_data, embedding in zip(
                                    all_chunks_data, result.embeddings
                                ):
                                    chunk = chunk_data["chunk"]
                                    chunk_index = chunk_data["chunk_index"]
                                    diff_info = chunk_data["diff_info"]
                                    point_id = chunk_data["point_id"]

                                    # Convert timestamp to date
                                    from datetime import datetime

                                    commit_date = datetime.fromtimestamp(
                                        commit.timestamp
                                    ).strftime("%Y-%m-%d")

                                    # Extract language and file extension for filter compatibility
                                    # MUST match regular indexing pattern from file_chunking_manager.py
                                    file_path_obj = Path(diff_info.file_path)
                                    file_extension = (
                                        file_path_obj.suffix.lstrip(".") or "txt"
                                    )  # Remove dot, same as regular indexing
                                    language = (
                                        file_path_obj.suffix.lstrip(".") or "txt"
                                    )  # Same format for consistency

                                    # Base payload structure
                                    payload = {
                                        "type": "commit_diff",
                                        "diff_type": diff_info.diff_type,
                                        "commit_hash": commit.hash,
                                        "commit_timestamp": commit.timestamp,
                                        "commit_date": commit_date,
                                        "commit_message": (
                                            commit.message[:200]
                                            if commit.message
                                            else ""
                                        ),
                                        "author_name": commit.author_name,
                                        "author_email": commit.author_email,
                                        "path": diff_info.file_path,  # FIX Bug #1: Use "path" for git-aware storage
                                        "chunk_index": chunk_index,  # Use stored index
                                        "char_start": chunk.get("char_start", 0),
                                        "char_end": chunk.get("char_end", 0),
                                        "project_id": project_id,
                                        # REMOVED: "content" field - wasteful create-then-delete pattern eliminated
                                        # Content now stored directly in chunk_text at point root
                                        "language": language,  # Add language for filter compatibility
                                        "file_extension": file_extension,  # Add file_extension for filter compatibility
                                    }

                                    # Storage optimization: added/deleted files use pointer-based storage
                                    if diff_info.diff_type in ["added", "deleted"]:
                                        payload["reconstruct_from_git"] = True

                                        # Add parent commit for deleted files (enables reconstruction)
                                        if (
                                            diff_info.diff_type == "deleted"
                                            and diff_info.parent_commit_hash
                                        ):
                                            payload["parent_commit_hash"] = (
                                                diff_info.parent_commit_hash
                                            )

                                    point = {
                                        "id": point_id,
                                        "vector": list(embedding),
                                        "payload": payload,
                                        "chunk_text": chunk.get(
                                            "text", ""
                                        ),  # Content at root from start (no create-then-delete)
                                    }
                                    points.append(point)

                                # Filter out existing points before upserting
                                new_points = [
                                    point
                                    for point in points
                                    if point["id"] not in existing_ids
                                ]

                                # Only upsert new points
                                if new_points:
                                    self.vector_store.upsert_points(
                                        collection_name=self.collection_name,
                                        points=new_points,
                                    )

                                    # Track point IDs for potential rollback
                                    commit_point_ids.extend(
                                        [p["id"] for p in new_points]
                                    )

                                    # Add new points to existing_ids to avoid duplicates within this run
                                    for point in new_points:
                                        existing_ids.add(point["id"])

                                    # Add blob hashes to registry after successful indexing
                                    # Collect unique blob hashes from all processed diffs
                                    coll_blob_set = self.indexed_blobs.setdefault(
                                        self.collection_name, set()
                                    )
                                    for chunk_data in all_chunks_data:
                                        if chunk_data["diff_info"].blob_hash:
                                            coll_blob_set.add(
                                                chunk_data["diff_info"].blob_hash
                                            )

                    # Mark complete
                    commit_slot_tracker.update_slot(slot_id, FileStatus.COMPLETE)

                    # TIMEOUT ARCHITECTURE FIX: Only save commit if no errors occurred
                    # Failed/cancelled commits should not be saved to progressive metadata
                    if not commit_had_errors:
                        # Save completed commit to progressive metadata (Bug #8 fix)
                        self.progressive_metadata.save_completed(commit.hash)
                    else:
                        logger.warning(
                            f"Commit {commit.hash[:8]}: Not saved to progressive metadata (errors or cancellation)"
                        )

                    # DEADLOCK FIX: Get expensive data BEFORE acquiring lock
                    # This prevents holding progress_lock during:
                    # 1. copy.deepcopy() - expensive deep copy operation
                    # 2. get_concurrent_files_data() - acquires slot_tracker._lock (nested lock)
                    # 3. progress_callback() - Rich terminal I/O operations
                    import copy

                    concurrent_files_snapshot = copy.deepcopy(
                        commit_slot_tracker.get_concurrent_files_data()
                    )

                    # Minimal critical section: ONLY simple value updates
                    with progress_lock:
                        completed_count[0] += 1
                        current = completed_count[0]

                        # Update file counter
                        total_files_processed[0] += files_in_this_commit

                        # Update shared state with last completed work
                        last_completed_commit[0] = commit.hash
                        last_completed_file[0] = last_file_for_commit

                        # Capture bytes for KB/s calculation
                        bytes_processed_snapshot = total_bytes_processed[0]

                    # Progress callback invoked OUTSIDE lock to avoid I/O contention
                    if progress_callback:
                        total = len(commits)
                        elapsed = time.time() - start_time
                        commits_per_sec = current / max(elapsed, 0.1)
                        # Calculate KB/s throughput from accumulated diff sizes
                        kb_per_sec = (bytes_processed_snapshot / 1024) / max(
                            elapsed, 0.1
                        )
                        pct = (100 * current) // total

                        # Get thread count
                        thread_count = (
                            getattr(self.config.voyage_ai, "parallel_requests", 8)
                            if hasattr(self.config, "voyage_ai")
                            else 8
                        )

                        # Use shared state for display (100ms lag acceptable per spec)
                        commit_hash = (
                            last_completed_commit[0][:8]
                            if last_completed_commit[0]
                            else "????????"
                        )
                        file_name = (
                            last_completed_file[0].name
                            if last_completed_file[0]
                            and last_completed_file[0] != Path(".")
                            else "initializing"
                        )

                        # Format with ALL Story 1 AC requirements including 📝 emoji and KB/s throughput
                        info = f"{current}/{total} commits ({pct}%) | {commits_per_sec:.1f} commits/s | {kb_per_sec:.1f} KB/s | {thread_count} threads | 📝 {commit_hash} - {file_name}"

                        # Call with new kwargs for slot-based tracking (backward compatible)
                        try:
                            progress_callback(
                                current,
                                total,
                                last_completed_file[0] or Path("."),
                                info=info,
                                concurrent_files=concurrent_files_snapshot,  # Tree view data
                                slot_tracker=commit_slot_tracker,  # For live updates
                                item_type="commits",
                            )
                        except TypeError:
                            # Fallback for old signature without slot_tracker/concurrent_files
                            progress_callback(
                                current,
                                total,
                                last_completed_file[0] or Path("."),
                                info=info,
                                item_type="commits",
                            )

                except Exception as e:
                    logger.error(
                        f"CRITICAL: Failed to index commit {commit.hash[:7]}: {e}",
                        exc_info=True,
                    )
                    raise
                finally:
                    # Release slot
                    commit_slot_tracker.release_slot(slot_id)

                commit_queue.task_done()

        # Get thread count from config (default 8)
        thread_count = (
            getattr(self.config.voyage_ai, "parallel_requests", 8)
            if hasattr(self.config, "voyage_ai")
            else 8
        )

        # FIX Issue 3: Add proper KeyboardInterrupt handling for graceful shutdown
        # Use ThreadPoolExecutor for parallel processing with multiple workers
        futures = []
        try:
            with ThreadPoolExecutor(max_workers=thread_count) as executor:
                # Submit multiple workers
                futures = [executor.submit(worker) for _ in range(thread_count)]

                # Wait for all workers to complete
                for future in as_completed(futures):
                    future.result()  # Wait for completion

        except KeyboardInterrupt:
            # Cancel all pending futures on Ctrl+C
            logger.info("KeyboardInterrupt received, cancelling pending tasks...")
            for future in futures:
                future.cancel()

            # Shutdown executor without waiting for running tasks
            # This prevents atexit handler errors
            raise  # Re-raise to propagate interrupt

        # Return actual totals: (commits_processed, files_processed, vectors_created)
        # Use completed_count[0] which tracks commits actually processed (not just passed in)
        total_vectors_created = completed_count[0] * 3  # Approximate vectors per commit
        return completed_count[0], total_files_processed[0], total_vectors_created

    def _index_commit_message(
        self, commit: CommitInfo, project_id: str, vector_manager
    ):
        """Index commit message as searchable entity.

        Commit messages are chunked using same logic as files and indexed
        as separate vector points. This allows searching by commit message.

        Args:
            commit: Commit object with hash, message, timestamp, author info
            project_id: Project identifier
            vector_manager: VectorCalculationManager for embedding generation
        """
        commit_msg = commit.message or ""
        if not commit_msg.strip():
            return  # Skip empty messages

        # Use chunker (FixedSizeChunker) to chunk commit message
        # Treat commit message like a markdown file for chunking
        chunks = self.chunker.chunk_text(
            commit_msg, Path(f"[commit:{commit.hash[:7]}]")
        )

        if not chunks:
            return

        # Get embeddings for commit message chunks
        chunk_texts = [chunk["text"] for chunk in chunks]

        try:
            # Use same vector manager as file chunks
            future = vector_manager.submit_batch_task(
                chunk_texts, {"commit_hash": commit.hash}
            )
            result = future.result(timeout=30)

            if not result.error and result.embeddings:
                # Convert timestamp to date (YYYY-MM-DD format)
                commit_date = datetime.fromtimestamp(commit.timestamp).strftime(
                    "%Y-%m-%d"
                )

                points = []
                for j, (chunk, embedding) in enumerate(zip(chunks, result.embeddings)):
                    point_id = f"{project_id}:commit:{commit.hash}:{j}"

                    # Note: chunks from FixedSizeChunker use char_start/char_end, not line_start/line_end
                    payload = {
                        "type": "commit_message",  # Distinguish from file chunks
                        "commit_hash": commit.hash,
                        "commit_timestamp": commit.timestamp,
                        "commit_date": commit_date,
                        "author_name": commit.author_name,
                        "author_email": commit.author_email,
                        "chunk_index": j,
                        "char_start": chunk.get("char_start", 0),
                        "char_end": chunk.get("char_end", len(commit_msg)),
                        "project_id": project_id,
                    }

                    point = {
                        "id": point_id,
                        "vector": list(embedding),
                        "payload": payload,
                        "chunk_text": chunk["text"],
                    }
                    points.append(point)

                # Store in temporal collection (NOT default collection)
                self.vector_store.upsert_points(
                    collection_name=self.collection_name, points=points
                )

        except Exception as e:
            logger.error(f"Error indexing commit message {commit.hash[:7]}: {e}")

    def _save_temporal_metadata(
        self,
        last_commit: str,
        total_commits: int,
        files_processed: int,
        approximate_vectors_created: int,
        branch_stats: dict,
        indexing_mode: str,
        max_commits: Optional[int] = None,
        since_date: Optional[str] = None,
    ):
        """Save temporal indexing metadata to JSON."""
        metadata = {
            "last_commit": last_commit,
            "total_commits": total_commits,
            "files_processed": files_processed,
            "approximate_vectors_created": approximate_vectors_created,
            "indexed_branches": branch_stats["branches"],
            "indexing_mode": indexing_mode,
            "indexed_at": datetime.now().isoformat(),
        }

        if max_commits is not None:
            metadata["max_commits"] = max_commits
        if since_date is not None:
            metadata["since_date"] = since_date

        metadata_path = self.temporal_dir / "temporal_meta.json"
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)

    def close(self):
        """Clean up resources and finalize HNSW index."""
        # Build HNSW index for temporal collection
        logger.info("Building HNSW index for temporal collection...")
        self.vector_store.end_indexing(collection_name=self.collection_name)

        # Temporal indexing cleanup complete
