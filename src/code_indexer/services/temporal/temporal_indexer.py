"""TemporalIndexer - Index git history as per-commit aggregated contextual documents.

Story #1290 (Epic #1289) HARD CUT: the legacy per-file-diff pipeline (which
built one vector per changed-file diff, plus a separate standalone
commit-message vector) has been REMOVED. Each commit is now aggregated into
ONE document (message once at the head + each changed file's
diff prefixed "--- <path> ---"), chunked with the active TemporalEmbedder's
overlap policy (0% for the contextual embedder), and embedded through that
embedder's contextualized endpoint -- producing a handful of vectors per
commit instead of vectors per changed file.
"""

import json
import logging
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone as _tz
from pathlib import Path
from queue import Queue, Empty
from typing import List, Optional, Callable

from ...config import ConfigManager
from ...services.vector_calculation_manager import VectorCalculationManager
from ...services.file_identifier import FileIdentifier
from ...storage.filesystem_vector_store import FilesystemVectorStore

from .models import CommitInfo
from .commit_aggregator import build_aggregated_document, get_file_changes
from .embedders.base import TemporalEmbedder
from .contextual_chunker import chunk_aggregated_document
from .embedders import registry as _embedder_registry_module  # noqa: F401  (self-registers adapters)
from .embedders.registry import create_embedder
from .temporal_blank_out import blank_out_legacy_temporal_collections
from .temporal_collection_naming import (
    LEGACY_TEMPORAL_COLLECTION,
    sanitize_model_name,
)
from .temporal_point_builder import build_chunk_payload, build_point_id
from .temporal_projection_matrix import _ensure_shard_has_projection_matrix
from .temporal_progressive_metadata import TemporalProgressiveMetadata
from .temporal_structure_marker import is_v2_structure, write_structure_marker

logger = logging.getLogger(__name__)


@dataclass
class IndexingResult:
    """Result of temporal indexing operation.

    Fields:
        total_commits: Number of commits processed
        files_processed: Number of changed files analyzed across all commits
        approximate_vectors_created: Number of vectors created (exact --
            the sum of chunks emitted per processed commit)
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


# Story #1158: Default parallelism for git-diff ThreadPoolExecutor sites.
_DEFAULT_PARALLEL_REQUESTS = 8

# Bug #1206 Fix 2: flush progressive metadata to disk every N commits (amortized).
# Per-commit flushing re-sorted and rewrote the entire completed list each time
# (O(N) cost per commit).  With lazy staging + periodic flush, each commit costs
# O(1) (in-memory set.add) and the disk write happens at most every
# _FLUSH_INTERVAL commits.  A final flush runs after all workers complete.
_FLUSH_INTERVAL = 10


class TemporalIndexer:
    """Orchestrates git history indexing as per-commit aggregated contextual documents.

    This class coordinates the temporal indexing workflow:
    1. Blank-out any legacy/version<2 temporal collection (AC19/20)
    2. Get commit history from git
    3. Filter already-processed commits using progressive metadata / reconcile
    4. For each new commit, aggregate message+diffs into ONE document, chunk
       it, embed it through the active TemporalEmbedder, and upsert the
       resulting handful of vectors under the unified point_id scheme
    5. Track processed commits with a durable per-commit completion marker
    """

    # Batch retry configuration (kept for the outbound-HTTP-call fault path;
    # the embedder's own client applies retry/backoff internally too).
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

        # Bug #1207 BLOCKER 1 (retained naming): guard for close() -- no monolith
        # is ever created by the per-commit pipeline, so this flag no longer
        # gates a destructive cleanup, but is kept as an informational marker
        # of "the shard loop completed without exception".
        self._indexing_complete: bool = False

        # Initialize progressive metadata tracker for resume capability (primary collection)
        self.progressive_metadata = TemporalProgressiveMetadata(self.temporal_dir)

        # Per-shard progress trackers -- Story #1290: the durable per-commit
        # completion marker (AC16) lives in the SHARD the commit's points
        # actually land in, not the base collection_name directory.
        self._progress_by_collection: dict[str, TemporalProgressiveMetadata] = {}

        # Resolved lazily inside index_commits()/_index_shard_commits() --
        # constructing the real embedder (a live provider client) eagerly in
        # __init__ would break every caller that only wants collection
        # bookkeeping without ever indexing a commit.
        self._active_embedder: Optional[TemporalEmbedder] = None
        self._active_embedder_name: Optional[str] = None

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
        Story #1290 AC16: this is the durable per-commit completion marker --
        mark_commit_indexed(commit.hash) is called on the SHARD's own tracker
        AFTER that commit's points have been flushed, so reconcile can
        distinguish "points present + marker present" (complete) from
        "points present, no marker" (partial -- crashed mid-flush).

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

    def _get_temporal_thread_count(self) -> int:
        """Return the thread count for commit-processing ThreadPoolExecutor sites."""
        if getattr(self.config, "embedding_provider", None) == "cohere" and hasattr(
            self.config, "cohere"
        ):
            base = self.config.cohere.parallel_requests
            temporal = getattr(self.config.cohere, "temporal_parallel_requests", None)
        elif hasattr(self.config, "voyage_ai"):
            base = getattr(
                self.config.voyage_ai, "parallel_requests", _DEFAULT_PARALLEL_REQUESTS
            )
            temporal = getattr(
                self.config.voyage_ai, "temporal_parallel_requests", None
            )
        else:
            base = _DEFAULT_PARALLEL_REQUESTS
            temporal = None
        return temporal if temporal is not None else base

    def _blank_out_legacy_collections(self) -> None:
        """Story #1290 AC19/AC20: hard-delete legacy/version<2 temporal collections
        BEFORE any read, reconcile, or write.

        Runs unconditionally at the start of every index_commits() call. This
        deliberately does NOT catch exceptions: blank_out_legacy_temporal_collections
        fails loud on a genuine deletion error (Messi #13 Anti-Silent-Failure) --
        a blank-out that could not actually clear a collection must not let
        indexing proceed to read/reconcile/write it, since that would risk
        mixing legacy and v2 points in the same collection.
        """
        index_path = self.vector_store.base_path
        deleted = blank_out_legacy_temporal_collections(index_path)
        if deleted:
            logger.info(
                "Story #1290 blank-out: hard-deleted %d legacy/version<2 temporal "
                "collection(s) before indexing: %s",
                len(deleted),
                deleted,
            )

    def index_commits(
        self,
        all_branches: bool = False,
        max_commits: Optional[int] = None,
        since_date: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
        reconcile: bool = False,
    ) -> IndexingResult:
        """Index git commit history as per-commit aggregated contextual documents.

        Args:
            all_branches: If True, index all branches; if False, current branch only
            max_commits: Maximum number of commits to index per branch
            since_date: Index commits since this date (YYYY-MM-DD)
            progress_callback: Progress callback function
            reconcile: If True, reconcile disk state with git history (crash recovery)

        Returns:
            IndexingResult with statistics
        """
        # Story #1290 AC19/AC20: blank-out runs BEFORE any read/reconcile/write.
        self._blank_out_legacy_collections()

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

        # Resolve the active temporal embedder NAME (a plain string, no live
        # client construction) up front -- used for shard placement.  Falls
        # back to the base collection_name when unresolvable (e.g. an
        # unconfigured/Mock config in a test), mirroring the pre-#1290
        # provider-model-name fallback.
        _raw_embedder_name = getattr(self.config, "temporal", None)
        _raw_embedder_name = getattr(_raw_embedder_name, "active_embedder", None)
        _active_embedder_name: str = (
            _raw_embedder_name
            if isinstance(_raw_embedder_name, str) and _raw_embedder_name
            else self.collection_name
        )
        self._active_embedder_name = _active_embedder_name

        # Step 1.5: Reconciliation (if requested) - discover indexed/partial
        # commits from disk, shard-aware (AC15/AC16).
        if reconcile:
            from .temporal_reconciliation import reconcile_temporal_index

            logger.info("Reconciling disk state with git history...")
            missing_commits = reconcile_temporal_index(
                self.vector_store,
                commits_from_git,
                _active_embedder_name,
            )

            indexed_count = len(commits_from_git) - len(missing_commits)
            logger.info(
                f"Reconciliation complete: {indexed_count} indexed, "
                f"{len(missing_commits)} missing ({indexed_count * 100 // (len(commits_from_git) or 1)}% complete)"
            )

            commits_from_git = missing_commits

            if not commits_from_git:
                logger.info("All commits already indexed, rebuilding indexes only...")
                # Story #1290 (E2E-discovered bug): under the per-commit
                # sharded architecture, self.collection_name is a base
                # bookkeeping identity that is NEVER the real collection
                # written to (only quarterly shards are, each already
                # end_indexing()'d inside the main sharding loop on the run
                # that created them). It carries no v2 marker, so AC19/20
                # blank-out (which runs unconditionally at the top of this
                # method, before this branch) hard-deletes it on the very
                # next run -- an unconditional end_indexing() call here would
                # then always raise "Collection does not exist". Guard with
                # collection_exists() so a rerun with nothing missing is a
                # true no-op (nothing needs rebuilding: shards are immutable
                # until new commits arrive).
                if self.vector_store.collection_exists(self.collection_name):
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

        current_branch = self._get_current_branch()

        # Step 2: Process commits with parallel workers
        total_blobs_processed = 0
        total_vectors_created = 0
        commits_processed = 0

        # Select thread count based on the active embedding provider (legacy
        # config knob, reused unchanged for commit-level parallelism).
        _provider = getattr(self.config, "embedding_provider", None)
        if _provider == "cohere" and hasattr(self.config, "cohere"):
            vector_thread_count = self.config.cohere.parallel_requests
        elif _provider == "voyage-ai" and hasattr(self.config, "voyage_ai"):
            vector_thread_count = self.config.voyage_ai.parallel_requests
        else:
            vector_thread_count = 4

        # Story #1290: a VectorCalculationManager is still constructed purely
        # for its cancellation_event lifecycle (cooperative worker shutdown) --
        # the contextual embedder makes its OWN HTTP call directly and does
        # NOT submit batch tasks through it.
        from ...services.embedding_factory import EmbeddingProviderFactory

        embedding_provider = EmbeddingProviderFactory.create(config=self.config)

        # Resolve the active embedder instance ONCE (used for shard vector
        # dimensions AND per-commit embedding).  Failure here is non-fatal at
        # this stage -- it degrades to a 1024-dim default for shard creation
        # and a later fail-loud error inside the worker IF a commit actually
        # needs embedding (Messi #2/#13: never silently skip real work, but
        # never crash bookkeeping-only callers either).
        try:
            self._active_embedder = create_embedder(_active_embedder_name, self.config)
            _shard_vector_size = self._active_embedder.dimensions
        except Exception as exc:
            logger.warning(
                "Could not construct temporal embedder '%s' (%s); shard creation "
                "will default to 1024 dims. Embedding will fail loud if a commit "
                "actually needs to be processed.",
                _active_embedder_name,
                exc,
            )
            self._active_embedder = None
            _shard_vector_size = 1024

        model_slug = (
            sanitize_model_name(_active_embedder_name)
            if isinstance(_active_embedder_name, str) and _active_embedder_name
            else sanitize_model_name(self.collection_name)
        )

        # Story #1171: Group commits by quarterly shard to bound peak RAM.
        # Each shard is processed sequentially so only one shard is in RAM at a time.
        from collections import defaultdict
        from .temporal_collection_naming import (
            get_shard_collection_name,
            base_collection_name,
        )

        try:
            shard_commit_map: dict = defaultdict(list)
            for _commit in commits_from_git:
                _shard = get_shard_collection_name(
                    _active_embedder_name,
                    datetime.fromtimestamp(_commit.timestamp, tz=_tz.utc),
                )
                shard_commit_map[_shard].append(_commit)
        except Exception as e:
            # Unknown/unconfigured temporal embedder (e.g. a Mock in tests).
            # Fall back to treating all commits as a single group under the
            # current collection.
            logger.warning(
                "Could not determine shard collection name for temporal embedder "
                "'%s' (%s); falling back to base collection '%s'.",
                _active_embedder_name,
                e,
                self.collection_name,
            )
            shard_commit_map = defaultdict(list)
            shard_commit_map[self.collection_name].extend(commits_from_git)

        sorted_shards = sorted(
            shard_commit_map.keys()
        )  # Chronological (lex = chron for YYYYQN)

        # Track shards processed so close() can call end_indexing per shard
        self._processed_shards: list = []

        _original_collection_name = self.collection_name
        try:
            with VectorCalculationManager(
                embedding_provider,
                vector_thread_count,
                config_dir=self.config_manager.config_path.parent,
            ) as vector_manager:
                for _shard_name in sorted_shards:
                    self.collection_name = _shard_name
                    if not self.vector_store.collection_exists(_shard_name):
                        logger.info(
                            "Creating temporal shard collection '%s' with dimension=%d",
                            _shard_name,
                            _shard_vector_size,
                        )
                        self.vector_store.create_collection(
                            _shard_name, _shard_vector_size
                        )
                        # AC27: v2 marker persisted at CREATE, before the first
                        # embed/flush -- a crash mid-index cannot leave this
                        # collection looking legacy.
                        _shard_path = self.vector_store._get_collection_path(
                            _shard_name
                        )
                        if isinstance(_shard_path, Path):
                            write_structure_marker(_shard_path, model_slug)
                    else:
                        # Bug #1242: Collection exists but may be missing
                        # projection_matrix.npy (deployed broken shards).
                        # Self-heal by copying from the base collection or
                        # regenerating so the first upsert_points() does not
                        # raise FileNotFoundError.
                        _shard_path = self.vector_store._get_collection_path(
                            _shard_name
                        )
                        if isinstance(_shard_path, Path):
                            if not (_shard_path / "projection_matrix.npy").exists():
                                _base_name = base_collection_name(_shard_name)
                                _base_path = self.vector_store._get_collection_path(
                                    _base_name
                                )
                                logger.info(
                                    "Bug #1242: self-healing missing projection_matrix.npy"
                                    " for shard '%s'",
                                    _shard_name,
                                )
                                _ensure_shard_has_projection_matrix(
                                    _shard_path,
                                    _base_path
                                    if (
                                        isinstance(_base_path, Path)
                                        and _base_path.exists()
                                    )
                                    else None,
                                    _shard_vector_size,
                                )
                            # Defensive belt-and-suspenders (AC27): an existing
                            # shard reached here without a v2 marker only if
                            # blank-out somehow missed it -- write it now so a
                            # later blank-out pass never mid-run deletes a
                            # shard this very run is writing into.
                            if not is_v2_structure(_shard_path):
                                write_structure_marker(_shard_path, model_slug)

                    # Initialize incremental HNSW tracking for this shard
                    self.vector_store.begin_indexing(_shard_name)

                    _shard_commits = shard_commit_map[_shard_name]
                    _c, _b, _v = self._index_shard_commits(
                        _shard_commits,
                        vector_manager,
                        progress_callback,
                        reconcile,
                    )
                    commits_processed += _c
                    total_blobs_processed += _b
                    total_vectors_created += _v

                    # Rebuild HNSW for this shard after processing completes
                    self.vector_store.end_indexing(collection_name=_shard_name)
                    self._processed_shards.append(_shard_name)
        finally:
            self.collection_name = _original_collection_name

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
            approximate_vectors_created=total_vectors_created,
            branch_stats={"branches": branches_indexed, "per_branch_counts": {}},
            indexing_mode="all-branches" if all_branches else "single-branch",
            max_commits=max_commits,
            since_date=since_date,
        )

        # Bug #1207 BLOCKER 1: mark successful completion so close() knows the
        # shard loop completed without exception.
        self._indexing_complete = True

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

    def _index_shard_commits(
        self,
        commits,
        vector_manager,
        progress_callback=None,
        reconcile=None,
    ):
        """Process one shard's commits as per-commit aggregated contextual documents.

        Story #1290: replaces the legacy per-file-diff chunk-batching pipeline.
        Each commit is aggregated into ONE document (message
        once at the head + each changed file's diff), chunked via the active
        embedder's overlap policy, embedded in ONE contextualized-embeddings
        call, and upserted under the unified point_id scheme. A durable
        per-commit completion marker is written to this shard's progress
        tracker AFTER the flush (AC16).

        Args:
            commits: List of commits to process (already grouped into this shard)
            vector_manager: VectorCalculationManager (cancellation_event only --
                the embedder makes its own HTTP calls, no batch task submission)
            progress_callback: Optional progress callback function
            reconcile: If False, use progressive metadata filtering for resume
                capability. If True, skip filtering (disk reconciliation already
                filtered). If None (default), skip filtering.
        """
        from ..clean_slot_tracker import CleanSlotTracker, FileStatus, FileData

        if reconcile is False:
            completed_commits = self.progressive_metadata.load_completed()
            commits = [c for c in commits if c.hash not in completed_commits]

        thread_count = self._get_temporal_thread_count()
        commit_slot_tracker = CleanSlotTracker(max_slots=thread_count)

        if progress_callback:
            try:
                progress_callback(
                    0,
                    len(commits),
                    Path(""),
                    info=f"0/{len(commits)} commits (0%) | 0.0 commits/s | 0.0 KB/s | {thread_count} threads | 📝 ???????? - initializing",
                    concurrent_files=commit_slot_tracker.get_concurrent_files_data(),
                    slot_tracker=commit_slot_tracker,
                    item_type="commits",
                )
            except TypeError:
                progress_callback(
                    0,
                    len(commits),
                    Path(""),
                    info=f"0/{len(commits)} commits (0%) | 0.0 commits/s | 0.0 KB/s | {thread_count} threads | 📝 ???????? - initializing",
                    item_type="commits",
                )

        completed_count = [0]
        total_files_processed = [0]
        total_vectors_created = [0]
        last_completed_commit = [None]
        total_bytes_processed = [0]
        progress_lock = threading.Lock()
        start_time = time.time()

        commit_queue = Queue()  # type: ignore[var-annotated]
        for commit in commits:
            commit_queue.put(commit)

        diff_context_lines = getattr(self.config.temporal, "diff_context_lines", 5)
        chunk_chars = getattr(self.config.temporal, "aggregation_chunk_chars", 4096)
        shard_progress = self._get_progress(self.collection_name)

        def worker():
            nonlocal total_bytes_processed
            while True:
                if vector_manager.cancellation_event.is_set():
                    logger.info("Worker cancelled - exiting gracefully")
                    break

                try:
                    commit = commit_queue.get_nowait()
                except Empty:
                    break

                slot_id = None
                try:
                    placeholder_filename = f"{commit.hash[:8]} - Analyzing commit"
                    slot_id = commit_slot_tracker.acquire_slot(
                        FileData(
                            filename=placeholder_filename,
                            file_size=0,
                            status=FileStatus.STARTING,
                        )
                    )
                    commit_slot_tracker.update_slot(
                        slot_id,
                        FileStatus.CHUNKING,
                        filename=placeholder_filename,
                        file_size=0,
                    )

                    file_changes = get_file_changes(
                        self.codebase_dir, commit, diff_context_lines
                    )
                    doc = build_aggregated_document(commit, file_changes)
                    chunks = chunk_aggregated_document(doc, chunk_chars)

                    doc_size = len(doc.text)
                    with progress_lock:
                        total_bytes_processed[0] += doc_size

                    commit_slot_tracker.update_slot(
                        slot_id,
                        FileStatus.VECTORIZING,
                        filename=f"{commit.hash[:8]} - Embedding ({len(chunks)} chunks)",
                        file_size=doc_size,
                    )

                    if chunks:
                        if self._active_embedder is None:
                            raise RuntimeError(
                                f"Temporal embedder '{self._active_embedder_name}' "
                                f"is not available -- cannot index commit "
                                f"{commit.hash[:8]}."
                            )

                        chunk_texts = [c.text for c in chunks]
                        embeddings = self._active_embedder.embed_commit_chunks(
                            chunk_texts
                        )

                        # AC21: fail loud on count mismatch -- never write a
                        # partial index.
                        if len(embeddings) != len(chunks):
                            raise RuntimeError(
                                f"Contextualized embedding count mismatch for "
                                f"commit {commit.hash}: expected {len(chunks)} "
                                f"chunks, got {len(embeddings)} embeddings. "
                                f"Refusing to write a partial index."
                            )

                        project_id = self.file_identifier.get_project_id()
                        points = []
                        for chunk, embedding in zip(chunks, embeddings):
                            point_id = build_point_id(
                                project_id, commit.hash, chunk.chunk_index
                            )
                            payload = build_chunk_payload(commit, chunk, project_id)
                            points.append(
                                {
                                    "id": point_id,
                                    "vector": list(embedding),
                                    "payload": payload,
                                    "chunk_text": chunk.text,
                                }
                            )

                        commit_slot_tracker.update_slot(slot_id, FileStatus.FINALIZING)
                        self.vector_store.upsert_points(
                            collection_name=self.collection_name, points=points
                        )

                        with progress_lock:
                            total_vectors_created[0] += len(points)

                    # AC16: durable per-commit completion marker AFTER the
                    # flush -- a crash before this line leaves the commit
                    # absent from this shard's completed set, so reconcile
                    # treats any points it did manage to write as PARTIAL.
                    shard_progress.mark_commit_indexed(commit.hash)

                    commit_slot_tracker.update_slot(slot_id, FileStatus.COMPLETE)

                    with progress_lock:
                        completed_count[0] += 1
                        current = completed_count[0]
                        if current % _FLUSH_INTERVAL == 0:
                            shard_progress.flush_pending()
                        total_files_processed[0] += len(doc.file_paths)
                        last_completed_commit[0] = commit.hash
                        bytes_processed_snapshot = total_bytes_processed[0]

                    if progress_callback:
                        total = len(commits)
                        elapsed = time.time() - start_time
                        commits_per_sec = current / max(elapsed, 0.1)
                        kb_per_sec = (bytes_processed_snapshot / 1024) / max(
                            elapsed, 0.1
                        )
                        pct = (100 * current) // total if total else 100
                        commit_hash = (
                            last_completed_commit[0][:8]
                            if last_completed_commit[0]
                            else "????????"
                        )
                        info = (
                            f"{current}/{total} commits ({pct}%) | "
                            f"{commits_per_sec:.1f} commits/s | {kb_per_sec:.1f} KB/s | "
                            f"{thread_count} threads | 📝 {commit_hash} - "
                            f"{len(doc.file_paths)} files, {len(chunks)} chunks"
                        )
                        try:
                            progress_callback(
                                current,
                                total,
                                Path(commit.hash),
                                info=info,
                                concurrent_files=commit_slot_tracker.get_concurrent_files_data(),
                                slot_tracker=commit_slot_tracker,
                                item_type="commits",
                            )
                        except TypeError:
                            progress_callback(
                                current,
                                total,
                                Path(commit.hash),
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
                    if slot_id is not None:
                        commit_slot_tracker.release_slot(slot_id)

                commit_queue.task_done()

        futures = []
        try:
            with ThreadPoolExecutor(max_workers=thread_count) as executor:
                futures = [executor.submit(worker) for _ in range(thread_count)]
                for future in as_completed(futures):
                    future.result()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received, cancelling pending tasks...")
            for future in futures:
                future.cancel()
            raise

        # Flush any staged commits that didn't hit the _FLUSH_INTERVAL boundary.
        shard_progress.flush_pending()

        return (
            completed_count[0],
            total_files_processed[0],
            total_vectors_created[0],
        )

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

        # Story #1290: blank-out (AC19) may have hard-deleted this directory
        # earlier in the same index_commits() call (the base collection_name
        # dir carries no v2 marker) -- recreate it defensively before writing.
        self.temporal_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = self.temporal_dir / "temporal_meta.json"
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)

    def close(self):
        """Clean up resources after temporal indexing.

        Story #1290: the per-commit pipeline always builds quarterly shards
        directly -- it never produces a monolithic HNSW collection, so there
        is nothing to migrate or clean up here anymore. end_indexing() is
        already called per-shard inside index_commits(). This method only
        handles the fallback path (shard grouping could not be determined,
        e.g. an unrecognized/unconfigured embedder), which still writes to
        self.collection_name directly and needs its own end_indexing() call.
        """
        processed_shards = getattr(self, "_processed_shards", [])
        if not processed_shards:
            # Story #1290 (E2E-discovered bug): _processed_shards is never
            # set when index_commits() took the reconcile "nothing missing"
            # early return (it fires before Step 2's sharding loop). In that
            # case self.collection_name was never a real collection to begin
            # with (or was already hard-deleted by AC19/20 blank-out, which
            # runs unconditionally at the top of index_commits()) -- guard
            # so this is a true no-op instead of raising "does not exist".
            if self.vector_store.collection_exists(self.collection_name):
                logger.info(
                    "Building HNSW index for temporal collection (fallback path)..."
                )
                self.vector_store.end_indexing(collection_name=self.collection_name)
            else:
                logger.info(
                    "No temporal collection to finalize (nothing was indexed this run)."
                )
        else:
            logger.info(
                "Sharded indexing complete — HNSW indexes already built per shard (%d shards).",
                len(processed_shards),
            )
