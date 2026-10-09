"""
Smart incremental indexer that combines index and update functionality.

⚠️  CRITICAL PROGRESS REPORTING WARNING:
This module calls progress_callback with setup messages using total=0.
These MUST use total=0 to show as ℹ️ messages in CLI, not progress bar.
See HighThroughputProcessor for file progress patterns (total>0).
"""

from __future__ import annotations

import logging
import os
import time
import datetime
import subprocess
from pathlib import Path
from typing import (
    List,
    Dict,
    Any,
    FrozenSet,
    Optional,
    Callable,
    Set,
    Tuple,
    TYPE_CHECKING,
)
from dataclasses import dataclass

from ..config import Config, VOYAGE_MULTIMODAL_MODEL, COHERE_MULTIMODAL_MODEL
from ..services.embedding_provider import EmbeddingProvider
from ..indexing.processor import ProcessingStats
from .progressive_metadata import ProgressiveMetadata
from .resume_state_seal import load_or_create_resume_seal_key
from .git_topology_service import GitTopologyService

# Removed: SmartBranchIndexer (abandoned code)
# Removed: BranchAwareIndexer (replaced with HighThroughputProcessor)
from .indexing_lock import IndexingLockError, create_indexing_lock
from .high_throughput_processor import HighThroughputProcessor
from .git_hook_manager import GitHookManager
from ..utils.enhanced_messaging import OperationType, create_enhanced_callback
from ..utils.path_confinement import resolve_if_within_root
from ..storage.sqlite_chunk_store import (
    ChunkStoreUnavailableError,
    is_chunk_store_lock_contention,
)

# CRITICAL: Lazy import for FTS - only load when --fts flag used
# This prevents Tantivy from loading on every cidx command (including --help)
if TYPE_CHECKING:
    from .tantivy_index_manager import TantivyIndexManager

logger = logging.getLogger(__name__)

# Pre-flight availability flag for hnswlib (custom fork, Story #54).
# Patched to False in tests that simulate a missing hnswlib installation.
try:
    import hnswlib as _hnswlib  # noqa: F401

    HNSWLIB_AVAILABLE = True
except ImportError:
    HNSWLIB_AVAILABLE = False

# Number of files between periodic progress callbacks during deletion
PROGRESS_BATCH_SIZE = 100


# Value prefix FileIdentifier._get_file_content_hash returns when the file
# cannot be read (never a real content hash).
_CONTENT_HASH_READ_ERROR_PREFIX = "sha256:error-"

# File mtimes come from the filesystem's clock (an NFS server), read times
# from the indexing host's clock: tolerate the filesystem being this far behind.
_RACY_MTIME_CLOCK_SKEW_MARGIN_SECONDS = 2

# Reads of the reconcile snapshot while another connection holds the
# chunks.db lock. Each attempt already waits up to sqlite's busy timeout
# (5 s), so three attempts ride out ~15 s of contention; past that the run
# fails loudly instead of reconciling against an empty snapshot.
_SNAPSHOT_LOCK_MAX_ATTEMPTS = 3


def working_dir_content_id(relative_path: str, mtime: int, size: int) -> str:
    """The single format of an mtime/size-identified (working_dir) content id.

    Issue #2013: reconcile compares the id derived from a stored point's
    payload with the id built from the file on disk. Both sides MUST go
    through this function. `mtime` is the INTEGER-second mtime the indexer
    stores (`FileIdentifier._get_filesystem_metadata` writes
    `int(st_mtime)` into the `filesystem_mtime` payload field), so callers
    building the disk side pass `int(stat.st_mtime)`.
    """
    return f"{relative_path}:working_dir:{mtime}:{size}"


@dataclass
class ThroughputStats:
    """Statistics for tracking indexing throughput and throttling."""

    files_per_minute: float = 0.0
    chunks_per_minute: float = 0.0
    embedding_requests_per_minute: float = 0.0
    is_throttling: bool = False
    throttle_reason: str = ""
    average_processing_time_per_file: float = 0.0
    estimated_time_remaining_seconds: float = 0.0


@dataclass
class GitDelta:
    """Represents changes between two git commits."""

    added: List[str]  # Files added
    modified: List[str]  # Files modified
    deleted: List[str]  # Files deleted
    renamed: List[tuple]  # Files renamed (old_path, new_path)


@dataclass
class RollingAverage:
    """Maintains a rolling average for more stable time estimates."""

    def __init__(self, window_size: int = 10):
        self.window_size = window_size
        self.values: List[float] = []
        self.sum = 0.0

    def add(self, value: float):
        """Add a new value to the rolling average."""
        self.values.append(value)
        self.sum += value

        # Remove oldest value if window is full
        if len(self.values) > self.window_size:
            self.sum -= self.values.pop(0)

    def get_average(self) -> float:
        """Get the current rolling average."""
        if not self.values:
            return 0.0
        return self.sum / len(self.values)

    def get_count(self) -> int:
        """Get the number of values in the window."""
        return len(self.values)


class SmartIndexer(HighThroughputProcessor):
    """Smart indexer with progressive metadata and resumability using high-throughput queue-based processing."""

    #: Set by each smart_index() run: it created or rebuilt the FTS index,
    #: so it changed the index even if it processed no file (_finish_run).
    _run_rebuilt_fts: bool = False
    #: Files this run's processing failed (its stats, set by _finish_run);
    #: those it cannot name count as missing from FTS (Bug #2056).
    _run_failed_files: int = 0
    #: The failed files it names (relative paths): the FTS finish rebuilds
    #: their documents from disk -- FTS needs no embedding (Bug #2056).
    _run_failed_paths: FrozenSet[str] = frozenset()
    #: This run's processing was cancelled (its stats): its FTS content is
    #: incomplete, so the FTS finish treats it like a run that raised and
    #: leaves the index unmarked (Bug #2056).
    _run_cancelled: bool = False

    def __init__(
        self,
        config: Config,
        embedding_provider: EmbeddingProvider,
        vector_store_client: Any,  # FilesystemVectorStore (vector store backend)
        metadata_path: Path,
    ):
        super().__init__(config, embedding_provider, vector_store_client)
        self.progressive_metadata = ProgressiveMetadata(metadata_path)

        # Initialize branch topology services
        self.git_topology_service = GitTopologyService(
            config.codebase_dir, config=config
        )
        # Removed: SmartBranchIndexer initialization (abandoned code)

        # Initialize structured progress logging
        from .indexing_progress_log import IndexingProgressLog

        self.progress_log = IndexingProgressLog(
            config_dir=Path(config.codebase_dir) / ".code-indexer"
        )

        # Note: BranchAwareIndexer replaced with HighThroughputProcessor git-aware methods
        # All branch-aware functionality is now handled by HighThroughputProcessor

        # Initialize git hook manager for branch change detection
        self.git_hook_manager = GitHookManager(config.codebase_dir, metadata_path)

    def _get_git_deltas_since_commit(
        self, last_commit: str, current_commit: str
    ) -> GitDelta:
        """Get file changes between two git commits using git diff.

        Args:
            last_commit: The commit hash to compare from
            current_commit: The commit hash to compare to

        Returns:
            GitDelta with lists of added, modified, deleted, and renamed files
        """
        try:
            # Use git diff --name-status to get file changes
            cmd = ["git", "diff", "--name-status", f"{last_commit}..{current_commit}"]

            result = subprocess.run(
                cmd,
                cwd=self.config.codebase_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.error(f"Git diff failed: {result.stderr}")
                return GitDelta(added=[], modified=[], deleted=[], renamed=[])

            added = []
            modified = []
            deleted = []
            renamed = []

            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue

                parts = line.strip().split("\t")
                if len(parts) < 2:
                    continue

                status = parts[0]

                # Codex round-6 MEDIUM finding (Bug #1467 follow-up):
                # classify a RENAME record's old and new paths
                # INDEPENDENTLY, before the common exclude-filter check
                # below -- never after. parts[1] for a rename is the OLD
                # path; applying the common filter to it FIRST would
                # `continue` past the whole line (dropping new_path too)
                # whenever the old path happens to be excluded, even if
                # the new path is genuinely included and should be
                # indexed as new.
                if status.startswith("R"):
                    # Renamed file: R100\told_path\tnew_path
                    if len(parts) >= 3:
                        old_path = parts[1]
                        new_path = parts[2]
                        renamed.append((old_path, new_path))
                        # Treat rename as delete old + add new for indexing
                        if self._should_index_file(old_path):
                            deleted.append(old_path)
                        if self._should_index_file(new_path):
                            added.append(new_path)
                    continue

                file_path = parts[1]

                # Filter files based on our indexing criteria
                if not self._should_index_file(file_path):
                    continue

                if status == "A":
                    added.append(file_path)
                elif status == "M":
                    modified.append(file_path)
                elif status == "D":
                    deleted.append(file_path)

            logger.info(
                f"Git delta: +{len(added)} ~{len(modified)} -{len(deleted)} R{len(renamed)}"
            )
            return GitDelta(
                added=added, modified=modified, deleted=deleted, renamed=renamed
            )

        except Exception as e:
            logger.error(f"Failed to get git deltas: {e}")
            return GitDelta(added=[], modified=[], deleted=[], renamed=[])

    def _should_index_file(self, file_path: str) -> bool:
        """Check if a file should be indexed based on configuration."""
        try:
            path = Path(file_path)

            base_result = True

            # Check file extension (lstrip dot: .java -> java to match config format)
            if path.suffix.lower().lstrip(".") not in self.config.file_extensions:
                base_result = False

            # Check exclude patterns using path component boundaries
            # (not substring: "build" must match "build/" dir, not "builder/")
            if base_result:
                parts = Path(file_path).parts
                for exclude_dir in self.config.exclude_dirs:
                    if exclude_dir in parts:
                        base_result = False
                        break

            # Bug #1467: also apply the canonical FileFinder exclude_spec
            # (e.g. .code-indexer-override.yaml and other generated
            # config artifacts). This git-diff-based incremental
            # discovery path previously reimplemented only the
            # extension/exclude_dirs subset of file_finder.py's
            # filtering, silently diverging from the first (full-walk)
            # index run's scope -- letting a generated artifact slip
            # through as a real "changed file" once committed to git.
            # Pure string matching, safe for deleted files too (no
            # filesystem access, unlike file_finder's other checks).
            if base_result and self.file_finder.matches_exclude_pattern(file_path):
                base_result = False

            # Codex Finding #10 (MEDIUM): apply the SAME force-include/
            # force-exclude override parity full discovery
            # (file_finder.py's _should_include_file) already has, reusing
            # the SAME OverrideFilterService instance rather than
            # reimplementing its pattern logic -- without this, a file
            # matching .code-indexer-override.yaml's force_include_patterns
            # is correctly indexed on the first (full-walk) run but
            # silently rejected once committed and picked up incrementally.
            override_filter_service = getattr(
                self.file_finder, "override_filter_service", None
            )
            if override_filter_service is not None:
                return bool(
                    override_filter_service.should_include_file(path, base_result)
                )

            return base_result
        except Exception as exc:
            # Fail-soft is intentional here: a single malformed git-diff
            # path must not abort the whole delta computation loop. The
            # exception is logged (not silently swallowed) so a genuine
            # filtering bug remains diagnosable.
            logger.debug(
                f"_should_index_file: treating '{file_path}' as not-indexed "
                f"after unexpected error: {exc}"
            )
            return False

    def _delete_files_from_backend(
        self,
        deleted_files: List[str],
        collection_name: str,
        progress_callback: Optional[Callable] = None,
    ) -> int:
        """Delete files from vector store backend.

        Args:
            deleted_files: List of file paths to delete
            collection_name: Collection name
            progress_callback: Optional progress callback, called every PROGRESS_BATCH_SIZE files

        Returns:
            Number of files successfully deleted
        """
        deleted_count = 0

        for i, file_path in enumerate(deleted_files):
            try:
                # Use the existing branch-aware deletion logic
                success = self.delete_file_branch_aware(
                    file_path, collection_name, watch_mode=False
                )
                if success:
                    deleted_count += 1
                    logger.info(f"🗑️  Deleted from index: {file_path}")
                else:
                    logger.warning(f"Failed to delete from index: {file_path}")
            except Exception as e:
                logger.error(f"Error deleting {file_path}: {e}")

            processed = i + 1
            if progress_callback and processed % PROGRESS_BATCH_SIZE == 0:
                progress_callback(
                    processed,
                    len(deleted_files),
                    Path(""),
                    info=f"Deleting files... {processed}/{len(deleted_files)}",
                )

        return deleted_count

    def smart_index(
        self,
        force_full: bool = False,
        reconcile_with_database: bool = False,
        batch_size: int = 50,
        progress_callback: Optional[Callable] = None,
        safety_buffer_seconds: int = 60,
        files_count_to_process: Optional[int] = None,
        quiet: bool = False,
        vector_thread_count: Optional[int] = None,
        detect_deletions: bool = False,
        enable_fts: bool = False,
        trust_resume_state: bool = True,
    ) -> ProcessingStats:
        """
        Smart indexing that automatically chooses between full and incremental indexing.

        Args:
            force_full: Force full reindex (like --clear)
            reconcile_with_database: Reconcile disk files with database contents
            batch_size: Batch size for processing
            progress_callback: Optional progress callback
            safety_buffer_seconds: Safety buffer for incremental indexing
            files_count_to_process: Limit number of files to process (for testing)
            quiet: Suppress progress output
            vector_thread_count: Number of threads for vector calculation
            detect_deletions: Detect and handle files deleted from filesystem but still in database
            enable_fts: Build full-text search index alongside semantic index
            trust_resume_state: When False (server-spawned indexing), the
                repository-stored resume metadata is trusted ONLY when it
                carries a valid server-held seal, i.e. it was written by a
                previous server-context run (see resume_state_seal.py);
                every save of this run is sealed so an interruption can be
                resumed. State without a valid server seal never drives
                the resume branch, and its stored file lists are discarded;
                an interrupted operation then completes via a reconcile.
                Deliberately not force_full/--clear (avoids a full
                re-embed). Default True preserves the local CLI behavior.

        Returns:
            ProcessingStats with operation results
        """
        # Pre-flight: hnswlib must be importable before we do any work
        if not HNSWLIB_AVAILABLE:
            raise RuntimeError(
                "hnswlib is not installed. "
                "Install the custom fork: "
                "git submodule update --init && pip install -e ."
            )

        # Create indexing lock to prevent concurrent operations
        metadata_dir = self.progressive_metadata.metadata_path.parent
        indexing_lock = create_indexing_lock(metadata_dir)

        try:
            # Acquire lock before starting indexing
            indexing_lock.acquire(str(self.config.codebase_dir))
        except IndexingLockError as e:
            raise RuntimeError(str(e))

        # Initialize FTS manager variable before try block to ensure it's defined for finally block
        fts_manager: Optional[TantivyIndexManager] = None
        # Initialized here so it's always in scope when passed to _do_incremental_index,
        # even when enable_fts=False (in which case fts_manager is also None).
        create_new_fts: bool = False
        # Bug #2056: the empty-FTS guard in `finally` runs only for a run
        # that is not already propagating an exception (never masks it).
        run_raised = False
        # Bug #2056: files the FTS bootstrap could not index; they leave the
        # rebuilt index unmarked, so the next run rebuilds it again.
        fts_bootstrap_failures: List[Tuple[Path, str]] = []

        try:
            # Server context: trust stored resume state only when the server
            # itself sealed it, and seal every save of this run. Done before
            # anything else touches the metadata.
            resume_state_trusted = trust_resume_state or (
                self._enable_server_resume_seal()
            )

            # Get current git status
            git_status = self.get_git_status()
            provider_name = self.embedding_provider.get_provider_name()
            model_name = self.embedding_provider.get_current_model()

            # FTS requested: open (or rebuild once from disk) the index --
            # the lifecycle lives in fts_lifecycle (Bugs #1763, #2056). An
            # FTS infrastructure failure fails the run; per-file failures
            # follow its per-file rule (reported, index left unmarked).
            if enable_fts:
                from .fts_lifecycle import open_fts_index_for_run

                fts_manager, create_new_fts = open_fts_index_for_run(
                    self.config, force_full=force_full
                )
                # CRITICAL-1 (#1763): a new or rebuilt index is empty, and
                # an incremental run only re-adds CHANGED files -- fill it
                # from every file on disk first (per-file supersession keeps
                # re-processed files duplicate-free). A full re-index walks
                # every file itself, so it skips this.
                if create_new_fts and not force_full:
                    fts_bootstrap_failures = self._populate_fts_from_all_files(
                        fts_manager, progress_callback
                    )

                if progress_callback:
                    if create_new_fts:
                        info_message = (
                            "✅ FTS indexing enabled - Creating new Tantivy index"
                        )
                    else:
                        info_message = "✅ FTS indexing enabled - Opening existing Tantivy index for incremental updates"
                    progress_callback(
                        0,
                        0,
                        Path(""),
                        info=info_message,
                    )
                logger.info(f"FTS indexing enabled (create_new={create_new_fts})")

            self._run_rebuilt_fts = fts_manager is not None and create_new_fts
            self._run_failed_files = 0  # set by this run's _finish_run
            self._run_failed_paths = frozenset()  # likewise
            self._run_cancelled = False  # likewise
            # Bug #2056: every FileChunkingManager of this run records the
            # files whose FTS documents could not be replaced; the finish
            # retries them from disk (_finish_fts_run).
            from .fts_file_documents import FtsWriteFailures

            self._fts_write_failures = (
                FtsWriteFailures() if fts_manager is not None else None
            )

            # Ensure git hook is installed for branch change detection
            try:
                self.git_hook_manager.ensure_hook_installed()
            except Exception as e:
                logger.warning(f"Failed to install git hook for branch tracking: {e}")
                # Continue without hook - branch tracking will fall back to git subprocess

            # Check for branch topology optimization (only if not forcing full, not reconciling, and collection exists)
            if not force_full and not reconcile_with_database:
                # Invalidate cache to get fresh branch info
                self.git_topology_service.invalidate_cache()
                current_branch = self.git_topology_service.get_current_branch()
                collection_name = self.vector_store_client.resolve_collection_name(
                    self.config, self.embedding_provider
                )

                # Check for branch change by comparing stored branch with current branch
                stored_branch = self.progressive_metadata.metadata.get("current_branch")
                logger.info(
                    f"Branch change detection: stored_branch={stored_branch}, current_branch={current_branch}"
                )

                if (
                    current_branch
                    and stored_branch
                    and stored_branch != current_branch
                    and self.vector_store_client.collection_exists(collection_name)
                ):
                    # Branch change detected - use graph-optimized branch indexing
                    old_branch = stored_branch
                    if progress_callback:
                        progress_callback(
                            0,
                            0,
                            Path(""),
                            info=f"Branch change detected: {old_branch} -> {current_branch}, using graph-optimized indexing",
                        )

                    try:
                        # Analyze branch change to determine what needs indexing
                        analysis = self.git_topology_service.analyze_branch_change(
                            old_branch, current_branch
                        )

                        # Use high-throughput parallel branch processing (4-8x faster)
                        branch_result = self.process_branch_changes_high_throughput(
                            old_branch=old_branch,
                            new_branch=current_branch,
                            changed_files=analysis.files_to_reindex,
                            unchanged_files=analysis.files_to_update_metadata,
                            collection_name=collection_name,
                            progress_callback=progress_callback,
                            vector_thread_count=vector_thread_count,
                            fts_manager=fts_manager,
                        )

                        # Convert to ProcessingStats format
                        stats = ProcessingStats()
                        stats.files_processed = branch_result.files_processed
                        stats.chunks_created = branch_result.content_points_created
                        stats.failed_files = 0  # BranchAwareIndexer doesn't track failures in the same way
                        stats.start_time = time.time() - branch_result.processing_time
                        stats.end_time = time.time()
                        stats.cancelled = branch_result.cancelled

                        # Update progressive metadata with new git status
                        updated_git_status = git_status.copy()
                        updated_git_status["current_branch"] = current_branch
                        self.progressive_metadata.start_fresh_indexing(
                            provider_name, model_name, updated_git_status
                        )

                        # Mark as completed only if not cancelled
                        if not stats.cancelled:
                            self.progressive_metadata.complete_indexing()
                            logger.info(
                                f"Graph-optimized branch indexing completed: "
                                f"{branch_result.content_points_created} content points created, "
                                f"{branch_result.content_points_reused} content points reused"
                            )
                        else:
                            logger.info(
                                "Graph-optimized branch indexing was cancelled, not marking as completed for resume capability"
                            )

                        # Bug #2056: returns without _finish_run; the FTS
                        # finish still needs its cancellation.
                        self._run_cancelled = stats.cancelled
                        return stats

                    except Exception as e:
                        logger.error(
                            f"Graph-optimized branch indexing failed in git project: {e}"
                        )
                        # NO FALLBACK - fail fast in git projects
                        raise RuntimeError(
                            f"Git-aware graph-optimized indexing failed and fallbacks are disabled. "
                            f"Original error: {e}"
                        ) from e

            # Bug #1969 Round 6 (P1-4): a corrupt/unreadable self-heal-
            # reprocess sidecar means its replay record is lost -- rather
            # than silently trusting normal incremental/resume file
            # selection (which has no way to know what a lost sidecar
            # might have listed), force THIS run into reconcile-with-
            # database mode: a complete superset of whatever the corrupt
            # sidecar could have listed, since a self-heal-wiped file has
            # zero points regardless of whether the sidecar remembers it.
            if not force_full and not reconcile_with_database:
                _p14_collection_name = self.vector_store_client.resolve_collection_name(
                    self.config, self.embedding_provider
                )
                if self.vector_store_client.collection_exists(
                    _p14_collection_name
                ) and self.vector_store_client.is_self_heal_reprocess_sidecar_corrupt(
                    _p14_collection_name
                ):
                    logger.error(
                        "Bug #1969 Round 6 (P1-4): the self-heal-reprocess "
                        "sidecar for collection %r is corrupt/unreadable -- "
                        "its replay record is lost, so this run is forced "
                        "into reconcile-with-database mode (a complete "
                        "superset: any file with zero points is detected as "
                        "missing and reindexed) instead of trusting normal "
                        "incremental/resume file selection.",
                        _p14_collection_name,
                    )
                    reconcile_with_database = True

            # Check for interrupted operations first - highest priority (unless forcing full)
            if (
                not force_full
                and resume_state_trusted
                and self.progressive_metadata.can_resume_interrupted_operation()
            ):
                # NOTE: The "Resuming interrupted operation" progress message is
                # emitted by _do_resume_interrupted() itself (see line ~1780) using
                # the post-filesystem-check count, which is the accurate number.
                # We intentionally do NOT emit it here to avoid the duplicate that
                # previously appeared in `cidx index` output on resume runs.
                return self._finish_run(
                    self._do_resume_interrupted(
                        batch_size,
                        progress_callback,
                        git_status,
                        provider_name,
                        model_name,
                        quiet,
                        vector_thread_count,
                        fts_manager,
                    ),
                    git_status,
                )

            # Untrusted (unsealed) resume state after a genuinely
            # interrupted operation must not stall forever (mtime scan
            # compares against an already-advanced last_index_timestamp).
            # Fall back to a fresh disk-vs-database reconcile instead.
            #
            # `not force_full` is REQUIRED here. force_full=True (--clear)
            # must always fall straight through to the force_full branch
            # below (progressive_metadata.clear() + _do_full_index(), which
            # actually calls vector_store_client.clear_collection()) --
            # never be silently downgraded to this reconcile fallback,
            # which only diffs disk-vs-database and can decide there is
            # nothing to do at all, defeating --clear's contract of
            # clearing old content.
            #
            # Checking status alone is sufficient: can_resume_interrupted_
            # operation() requires status in ("in_progress", "failed") as
            # part of its own definition, so it can never be true while the
            # status check below is false -- an explicit
            # `can_resume_interrupted_operation() or` here would be
            # redundant.
            if (
                not resume_state_trusted
                and not force_full
                and self.progressive_metadata.metadata.get("status")
                in ("in_progress", "failed")
            ):
                logger.info(
                    "Stored resume state is not server-sealed; completing the "
                    "interrupted operation with a reconcile instead of resuming."
                )
                return self._finish_run(
                    self._reconcile_and_verify(
                        batch_size,
                        progress_callback,
                        git_status,
                        provider_name,
                        model_name,
                        files_count_to_process,
                        quiet,
                        vector_thread_count,
                        fts_manager,
                    ),
                    git_status,
                )

            # Check for reconcile operation
            if reconcile_with_database:
                reconcile_stats = self._reconcile_and_verify(
                    batch_size,
                    progress_callback,
                    git_status,
                    provider_name,
                    model_name,
                    files_count_to_process,
                    quiet,
                    vector_thread_count,
                    fts_manager,
                )
                # Bug #1969 Round 6 (P1-4): only after the reconcile
                # completes SUCCESSFULLY (not cancelled) is it safe to
                # quarantine a corrupt sidecar -- covers both this run
                # being FORCED into reconcile mode above, and an
                # explicit user-run `--reconcile` that happens to also
                # see a corrupt sidecar sitting there for an unrelated
                # reason.
                if not reconcile_stats.cancelled:
                    _p14_collection_name = (
                        self.vector_store_client.resolve_collection_name(
                            self.config, self.embedding_provider
                        )
                    )
                    if self.vector_store_client.is_self_heal_reprocess_sidecar_corrupt(
                        _p14_collection_name
                    ):
                        quarantine_path = self.vector_store_client.quarantine_corrupt_self_heal_reprocess_sidecar(
                            _p14_collection_name
                        )
                        logger.warning(
                            "Bug #1969 Round 6 (P1-4): quarantined corrupt "
                            "self-heal-reprocess sidecar for collection %r "
                            "after a successful reconcile -> %s",
                            _p14_collection_name,
                            quarantine_path,
                        )
                return self._finish_run(reconcile_stats, git_status)

            # Handle deletion detection for standard indexing (when not doing reconcile)
            # PERFORMANCE FIX (Bug 3): Skip deletion detection for git-aware projects
            # Git-aware projects use branch isolation AFTER indexing, so deletion detection
            # before indexing is redundant and wastes 10-30 minutes scanning the database
            if (
                detect_deletions
                and not reconcile_with_database
                and not self.is_git_aware()
            ):
                self._detect_and_handle_deletions(progress_callback)

            # Determine indexing strategy
            if force_full:
                # CRITICAL: Clear progressive metadata immediately when force_full=True (--clear flag)
                # This ensures that even if indexing is cancelled, stale metadata is cleared
                self.progressive_metadata.clear()
                return self._finish_run(
                    self._do_full_index(
                        batch_size,
                        progress_callback,
                        git_status,
                        provider_name,
                        model_name,
                        quiet,
                        vector_thread_count,
                        fts_manager,
                    ),
                    git_status,
                )

            # Check if we need to force full index due to configuration changes
            if self.progressive_metadata.should_force_full_index(
                provider_name, model_name, git_status
            ):
                if progress_callback:
                    enhanced_callback = create_enhanced_callback(
                        progress_callback,
                        OperationType.CONFIGURATION_CHANGE,
                        provider_name=provider_name,
                    )
                    enhanced_callback(
                        0,
                        0,
                        Path(""),
                        info="Configuration changed, performing full index",
                    )
                # Clear progressive metadata for configuration-triggered full index
                self.progressive_metadata.clear()
                return self._finish_run(
                    self._do_full_index(
                        batch_size,
                        progress_callback,
                        git_status,
                        provider_name,
                        model_name,
                        quiet,
                        vector_thread_count,
                        fts_manager,
                    ),
                    git_status,
                )

            # Try incremental indexing
            return self._finish_run(
                self._do_incremental_index(
                    batch_size,
                    progress_callback,
                    git_status,
                    provider_name,
                    model_name,
                    safety_buffer_seconds,
                    quiet,
                    vector_thread_count,
                    fts_manager,
                    trust_resume_state=resume_state_trusted,
                ),
                git_status,
            )

        except KeyboardInterrupt:
            # Only tells `finally` an exception is propagating, so the
            # empty-FTS guard never masks it (metadata stays resumable).
            run_raised = True
            # User cancellation should NOT mark as failed - leave in resumable state
            logger.info("Indexing operation was cancelled by user - can be resumed")
            raise
        except Exception as e:
            run_raised = True  # see the KeyboardInterrupt branch
            # Bug #467: Don't poison metadata on process interruptions.
            # Interruptions leave status="in_progress" so resume works on next run.
            # Only genuine errors (import failures, config errors, etc.) get "failed".
            error_str = str(e).lower()
            is_interruption = any(
                kw in error_str
                for kw in [
                    "timeout",
                    "interrupt",
                    "killed",
                    "signal",
                    "sigterm",
                    "sigkill",
                    "broken pipe",
                    "process",
                    "shutdown",
                ]
            )
            if is_interruption:
                logger.warning(f"Indexing interrupted (will resume on next run): {e}")
            else:
                self.progressive_metadata.fail_indexing(str(e))
            raise
        except BaseException:
            run_raised = True  # see the KeyboardInterrupt branch
            raise
        finally:
            try:
                if fts_manager is not None:
                    self._finish_fts_run(
                        fts_manager,
                        # A cancelled run's FTS content is incomplete: it is
                        # settled like one that raised (left unmarked).
                        run_raised=run_raised or self._run_cancelled,
                        bootstrap_failures=fts_bootstrap_failures,
                        progress_callback=progress_callback,
                    )
            finally:
                # Always release the lock, even on exception
                indexing_lock.release()

    def _finish_fts_run(
        self,
        fts_manager: "TantivyIndexManager",
        *,
        run_raised: bool,
        bootstrap_failures: List[Tuple[Path, str]],
        progress_callback: Optional[Callable],
    ) -> None:
        """Complete, commit and settle the run's FTS index (Bug #2056,
        fts_lifecycle.finish_fts_run): files whose processing or FTS write
        failed are rebuilt from disk, files still missing follow the
        per-file rule. An infrastructure failure fails the run: the metadata
        records it first, so the stale-index retry sees a failed run."""
        from .fts_lifecycle import finish_fts_run

        codebase_dir = Path(self.config.codebase_dir)
        retry_files = {codebase_dir / path for path in self._run_failed_paths}
        if self._fts_write_failures is not None:
            retry_files.update(self._fts_write_failures.paths())
        try:
            finish_fts_run(
                fts_manager,
                self.config,
                run_raised=run_raised,
                failed_files=bootstrap_failures,
                retry_files=sorted(retry_files),
                unknown_failures=max(
                    0, self._run_failed_files - len(self._run_failed_paths)
                ),
                source_files=self.file_finder.find_files(),
                progress_callback=progress_callback,
            )
        except Exception as e:
            self.progressive_metadata.fail_indexing(str(e))
            raise

    def _finish_run(
        self, stats: ProcessingStats, git_status: Dict[str, Any]
    ) -> ProcessingStats:
        """Every strategy's finished run records its outcome: the HEAD it
        indexed (so the refresh scheduler's drift signal clears even when
        the run found nothing to index; ignored unless the run is left
        completed) and whether it changed the index at all (processed
        files, hidden/deleted/un-hidden paths, a rebuilt full-text index)."""
        self._run_failed_files = stats.failed_files
        self._run_failed_paths = stats.failed_paths
        self._run_cancelled = stats.cancelled
        if not stats.cancelled:
            changed = (
                stats.files_processed > 0
                or stats.index_entries_changed > 0
                or self._run_rebuilt_fts
            )
            self.progressive_metadata.record_finished_run(
                git_status.get("current_commit"), changed
            )
        return stats

    def _clear_current_provider_multimodal_collection(self, provider_name: str) -> None:
        """Bug #1979 P1: a `--clear` full run must clear THIS run's
        provider's multimodal collection too, or a removed image's stale
        point (and the collection's pre-clear layout) survives what the
        maintainer defines as "indexing from scratch". Only the CURRENT
        provider's fixed-name collection is cleared -- never both
        unconditionally -- because a multi-provider `cidx index` run
        constructs and runs one SmartIndexer per provider sequentially
        (cli.py's `--extra-provider` loop); clearing every multimodal
        collection here would wipe out a provider that already finished
        its own full-index pass earlier in the same run."""
        multimodal_collection = {
            "voyage-ai": VOYAGE_MULTIMODAL_MODEL,
            "cohere": COHERE_MULTIMODAL_MODEL,
        }.get(provider_name)
        if multimodal_collection and self.vector_store_client.collection_exists(
            multimodal_collection
        ):
            if not self.vector_store_client.clear_collection(multimodal_collection):
                raise RuntimeError(
                    f"Failed to clear existing multimodal collection '{multimodal_collection}'"
                )

    def _abort_multimodal_collections(self) -> None:
        """Bug #1746 Change 3: abort_indexing() for every existing
        multimodal collection (discard, mirrors the finalize-side loop)."""
        for multimodal_collection in [
            VOYAGE_MULTIMODAL_MODEL,
            COHERE_MULTIMODAL_MODEL,
        ]:
            if self.vector_store_client.collection_exists(multimodal_collection):
                self.vector_store_client.abort_indexing(multimodal_collection)

    def _finalize_multimodal_collections(
        self, progress_callback: Optional[Callable]
    ) -> None:
        """Pre-existing multimodal end_indexing loop, extracted unchanged."""
        for multimodal_collection in [
            VOYAGE_MULTIMODAL_MODEL,
            COHERE_MULTIMODAL_MODEL,
        ]:
            if self.vector_store_client.collection_exists(multimodal_collection):
                multimodal_result = self.vector_store_client.end_indexing(
                    multimodal_collection, progress_callback
                )
                logger.info(
                    f"Multimodal index finalization complete "
                    f"({multimodal_collection}): "
                    f"{multimodal_result.get('vectors_indexed', 0)} vectors indexed"
                )

    def _finalize_or_abort_indexing_session(
        self,
        collection_name: str,
        fatal_chunk_store_error: Optional[BaseException],
        progress_callback: Optional[Callable],
        log_prefix: str = "Index",
    ) -> None:
        """Bug #1746 Change 3: abort_indexing() on a fatal chunk-store
        failure instead of end_indexing() -- no watermark advance for a
        run that never actually indexed the repository."""
        if fatal_chunk_store_error is not None:
            logger.error(
                f"{log_prefix}: fatal chunk-store failure -- aborting "
                f"instead of finalizing: {fatal_chunk_store_error}"
            )
            self.vector_store_client.abort_indexing(collection_name)
            self._abort_multimodal_collections()
            return

        if progress_callback:
            progress_callback(0, 0, Path(""), info="Finalizing indexing session...")
        end_result = self.vector_store_client.end_indexing(
            collection_name, progress_callback
        )
        logger.info(
            f"{log_prefix} finalization complete: "
            f"{end_result.get('vectors_indexed', 0)} vectors indexed"
        )
        self._finalize_multimodal_collections(progress_callback)

    def _fold_in_pending_self_heal_paths(
        self, collection_name: str, files: List[Path]
    ) -> Tuple[FrozenSet[str], List[Path]]:
        """Bug #1969 Round 5 (R4-F1): consult the DURABLE self-heal
        reprocess sidecar (recorded by ``recover_from_corrupt_id_index_
        by_wiping_files()`` itself, the single choke point EVERY self-heal
        call site -- ``upsert_points``, ``end_indexing``, ``scroll_points``
        -- funnels through, in THIS run or a PRIOR one) and merge any
        pending relative path not already in `files` and still present on
        disk into the file list BEFORE this strategy's own "anything to
        do?" check.

        This is what makes even a "plain rerun" with zero git/mtime
        changes still reprocess a file a PAST run's self-heal wiped: the
        durable record survives the process boundary Round 4's in-memory
        queue could not.

        Returns:
            ``(pending_paths_consulted, merged_files)`` -- the full
            pending set (for the caller to clear once this run completes
            successfully) and the possibly-larger file list to process.
        """
        pending = self.vector_store_client.get_pending_self_heal_reprocess_paths(
            collection_name
        )
        if not pending:
            return frozenset(), files
        codebase = Path(self.config.codebase_dir)
        covered = {str(f) for f in files}
        merged = list(files)
        for rel_path in pending:
            abs_path = codebase / rel_path
            if str(abs_path) in covered:
                continue
            if not abs_path.exists():
                continue
            merged.append(abs_path)
            covered.add(str(abs_path))
        return pending, merged

    def _reprocess_newly_pending_self_heal_paths(
        self,
        collection_name: str,
        already_covered_files: List[Path],
        stats: ProcessingStats,
        vector_thread_count: int,
        progress_callback: Optional[Callable],
        fts_manager: Optional[TantivyIndexManager],
    ) -> Tuple[ProcessingStats, FrozenSet[str]]:
        """Bug #1969 Round 5 (R4-F1): consult the durable sidecar AGAIN,
        AFTER this run's own primary processing pass -- catches a wipe
        that fired DURING that pass itself (the exact Round-4-confirmed
        same-run scenario: a corrupt ``id_index.bin`` + a dormant
        duplicate on an UNRELATED file gets discovered only while
        processing THIS run's own selected files), which
        ``_fold_in_pending_self_heal_paths`` could not have known about
        before the pass ran.

        For every pending relative path not already covered and still on
        disk, runs a second ``process_files_high_throughput`` pass in
        THIS SAME run, merging its stats into the caller's.

        Bug #1969 Round 5 (R4-F3): propagates a `cancelled` reprocess
        pass into `stats.cancelled` -- a cancelled reprocess must not let
        the caller treat the run as cleanly completed (which would
        otherwise clear the sidecar for a wipe that was never actually
        resolved).

        Returns:
            ``(stats, pending_paths_consulted)``.
        """
        pending = self.vector_store_client.get_pending_self_heal_reprocess_paths(
            collection_name
        )
        if not pending:
            return stats, frozenset()

        codebase = Path(self.config.codebase_dir)
        covered = {str(f) for f in already_covered_files}
        reprocess_files: List[Path] = []
        for rel_path in pending:
            abs_path = codebase / rel_path
            if str(abs_path) in covered or not abs_path.exists():
                continue
            reprocess_files.append(abs_path)

        if reprocess_files:
            logger.warning(
                "Bug #1969 Round 5: self-heal wiped %d file(s) this run's "
                "primary pass did not select -- reprocessing them now so "
                "their content does not become silently unsearchable: %s",
                len(reprocess_files),
                [str(f) for f in reprocess_files],
            )
            if progress_callback:
                progress_callback(
                    0,
                    0,
                    Path(""),
                    info=(
                        f"🔧 Reprocessing {len(reprocess_files)} file(s) "
                        f"whose index was self-healed this run..."
                    ),
                )
            reprocess_stats = self.process_files_high_throughput(
                files=reprocess_files,
                vector_thread_count=vector_thread_count,
                batch_size=50,
                progress_callback=progress_callback,
                fts_manager=fts_manager,
            )
            stats.files_processed += reprocess_stats.files_processed
            stats.chunks_created += reprocess_stats.chunks_created
            stats.failed_files += reprocess_stats.failed_files
            # Bug #1969 Round 6 (P1-3): merge the second pass's per-path
            # failure attribution too (a set UNION of both passes' failed
            # paths), not just its aggregate count -- otherwise the
            # combined stats always look "unattributed" to
            # _clear_self_heal_reprocess_paths_if_safe's consistency
            # check, forcing it to (safely, but wrongly) retain every
            # pending path instead of clearing the one that genuinely
            # succeeded.
            stats.failed_paths = stats.failed_paths | reprocess_stats.failed_paths
            stats.total_size += reprocess_stats.total_size
            stats.cancelled = stats.cancelled or reprocess_stats.cancelled

        return stats, pending

    def _self_heal_path_has_points(self, collection_name: str, rel_path: str) -> bool:
        """Bug #1969 Round 6 (P1-3): true if `rel_path` currently has at
        least one point in the collection -- used to verify a
        self-heal-pending path was ACTUALLY successfully reprocessed
        before clearing its durable replay record, rather than trusting
        the run's aggregate `stats.failed_files` count (which cannot say
        WHICH path failed). Tries both the relative form (the sidecar's
        own representation) and the absolute form (Bug #1575 AC6: a
        stored `payload.path` may be absolute) since either can be the
        on-disk representation. Fails SAFE: any error while checking is
        treated as "not yet resolved" -- never silently clears on doubt.
        """
        candidates = [rel_path, str(Path(self.config.codebase_dir) / rel_path)]
        try:
            for candidate in candidates:
                points, _ = self.vector_store_client.scroll_points(
                    collection_name=collection_name,
                    filter_conditions={
                        "must": [{"key": "path", "match": {"value": candidate}}]
                    },
                    limit=1,
                    with_payload=False,
                    with_vectors=False,
                )
                if points:
                    return True
            return False
        except Exception as exc:
            logger.warning(
                "Bug #1969 Round 6 (P1-3): failed to verify reprocessed "
                "state for %r (%s) -- treating as still pending.",
                rel_path,
                exc,
            )
            return False

    def _clear_self_heal_reprocess_paths_if_safe(
        self,
        collection_name: str,
        pending_paths: FrozenSet[str],
        stats: ProcessingStats,
    ) -> None:
        """Bug #1969 Round 5 (R4-F3) / Round 6 (P1-3): clear the durable
        sidecar entries this run consulted -- but ONLY when the run
        completed without being cancelled, AND only PER-PATH: a path is
        cleared only when it now actually has points again (verified via
        ``_self_heal_path_has_points``), never just because the run's
        aggregate ``stats.failed_files`` happened to be zero or because
        SOME OTHER unrelated path in the same run failed.

        A cancelled run must NOT mark ANY wipe as resolved: the durable
        record stays intact for every consulted path, exactly the
        crash-safety property the sidecar exists for.

        A path that still has zero points (its own reprocess attempt
        genuinely failed, or was never attempted) stays durably recorded
        -- it gets re-attempted once per future run (a WARNING is logged
        here, and the sidecar itself is only ever appended/removed, never
        looped over from this method) rather than being lost or causing
        an unbounded retry loop.

        (P1-3 review, Codex counter-example) A self-heal wipe records the
        sidecar entry BEFORE deleting the old (pre-wipe) points -- a
        crash, or this run's own failure, can leave those stale points in
        place, never genuinely replaced. "Has points" alone cannot
        distinguish a fresh success from that leftover, and an AGGREGATE
        failure count cannot either: two pending paths, one with a stale
        leftover point and one that genuinely succeeded with zero chunks,
        produce the exact same (still-pending count, failed_files) pair
        as one pending path that failed and one that succeeded --
        opposite correct outcomes from identical aggregate evidence. Only
        `stats.failed_paths` (Bug #1969 Round 6 P1-3, per-file failure
        attribution from the real processing loop) can break that tie: a
        pending path is trusted as resolved via "has points" ONLY when
        it is NOT in `stats.failed_paths` AND `stats.failed_paths`
        itself accounts for every reported failure
        (`len(stats.failed_paths) >= stats.failed_files`). If some
        failure has no attributed path at all (a rare internal executor
        error, or a hand-built `ProcessingStats` in a test), nothing is
        trusted as resolved this call -- fail safe.
        """
        if not pending_paths:
            return
        if stats.cancelled:
            logger.warning(
                "Bug #1969 Round 5 (R4-F3): this run was cancelled -- "
                "leaving %d self-heal-pending path(s) durably recorded "
                "for a future run: %s",
                len(pending_paths),
                sorted(pending_paths),
            )
            return

        if len(stats.failed_paths) < stats.failed_files:
            logger.warning(
                "Bug #1969 Round 6 (P1-3): this run reported %d failed "
                "file(s) but only %d are attributed to a specific path -- "
                "cannot confirm which self-heal-pending path(s) genuinely "
                "failed, so none are cleared this run: %s",
                stats.failed_files,
                len(stats.failed_paths),
                sorted(pending_paths),
            )
            return

        resolved = {
            rel_path
            for rel_path in pending_paths
            if rel_path not in stats.failed_paths
            and self._self_heal_path_has_points(collection_name, rel_path)
        }
        still_pending = pending_paths - resolved

        if still_pending:
            logger.warning(
                "Bug #1969 Round 6 (P1-3): %d self-heal-pending path(s) "
                "were not confirmed reprocessed this run (failed_files=%d) "
                "-- leaving them durably recorded for a future attempt: "
                "%s",
                len(still_pending),
                stats.failed_files,
                sorted(still_pending),
            )

        if resolved:
            self.vector_store_client.clear_self_heal_reprocess_paths(
                collection_name, resolved
            )

    def _do_full_index(
        self,
        batch_size: int,
        progress_callback: Optional[Callable],
        git_status: Dict[str, Any],
        provider_name: str,
        model_name: str,
        quiet: bool = False,
        vector_thread_count: Optional[int] = None,
        fts_manager=None,
    ) -> ProcessingStats:
        """Perform full indexing."""
        # Debug: Log start of full index
        import os
        import datetime

        debug_file = os.path.expanduser("~/.tmp/cidx_debug.log")
        os.makedirs(os.path.dirname(debug_file), exist_ok=True)
        with open(debug_file, "a") as f:
            f.write(f"[{datetime.datetime.now().isoformat()}] _do_full_index started\n")
            f.flush()

        # Ensure provider-aware collection exists and get info before clearing
        # Skip migration for full index (clear) since we'll clear all data anyway
        collection_name = self.vector_store_client.ensure_provider_aware_collection(
            self.config, self.embedding_provider, quiet, skip_migration=True
        )

        # Get collection info before clearing for meaningful feedback
        try:
            collection_info = self.vector_store_client.get_collection_info(
                collection_name
            )
            points_before_clear = collection_info.get("points_count", 0)
        except Exception:
            points_before_clear = 0

        # Create enhanced progress callback for clear operation
        if progress_callback:
            enhanced_callback = create_enhanced_callback(
                progress_callback,
                OperationType.CLEAR,
                collection_name=collection_name,
                documents_before_clear=points_before_clear,
                provider_name=provider_name,
            )
        else:
            enhanced_callback = None

        # Clear collection - enhanced callback will provide clear, non-duplicate messaging
        # Bug #1979 P1 (round 5): clear=true is "index from scratch" -- a
        # failed clear of the TEXT collection must abort the run exactly
        # like a failed multimodal-collection clear already does below,
        # instead of silently leaving stale rows in place while indexing
        # proceeds and later reports success.
        if not self.vector_store_client.clear_collection(collection_name):
            raise RuntimeError(
                f"Failed to clear existing collection '{collection_name}'"
            )
        # Bug #1979 P1: clear=true is "index from scratch" -- this
        # provider's multimodal collection must not survive with stale
        # points/layout just because only the TEXT collection was cleared
        # above.
        self._clear_current_provider_multimodal_collection(provider_name)
        if enhanced_callback and points_before_clear > 0:
            enhanced_callback(
                0,
                0,
                Path(""),
                info=f"🗑️  Cleared collection '{collection_name}' ({points_before_clear} documents removed)",
            )
        elif enhanced_callback:
            enhanced_callback(
                0,
                0,
                Path(""),
                info=f"🗑️  Cleared collection '{collection_name}' (collection was empty)",
            )

        # Recreate collection with fresh metadata after clearing
        # This ensures new quantization_range and other metadata are properly initialized
        self.vector_store_client.ensure_provider_aware_collection(
            self.config, self.embedding_provider, quiet, skip_migration=True
        )

        # NOTE: progressive_metadata.clear() is now called earlier in smart_index() when force_full=True

        # Start indexing
        self.progressive_metadata.start_fresh_indexing(
            provider_name, model_name, git_status
        )

        # Find all files
        if progress_callback:
            progress_callback(
                0, 0, Path(""), info="🔍 Discovering files in repository..."
            )
        with open(debug_file, "a") as f:
            f.write(f"[{datetime.datetime.now().isoformat()}] Finding files...\n")
            f.flush()
        files_to_index = list(self.file_finder.find_files())
        if progress_callback:
            progress_callback(
                0,
                0,
                Path(""),
                info=f"📁 Found {len(files_to_index)} files for indexing",
            )
        with open(debug_file, "a") as f:
            f.write(
                f"[{datetime.datetime.now().isoformat()}] Found {len(files_to_index)} files\n"
            )
            f.flush()

        if not files_to_index:
            # An empty repository (no eligible file) is "completed, nothing
            # to index", never a failure: a failed status would make the
            # refresh scheduler force a reconcile on every cycle. No session
            # is started for zero files.
            self.progressive_metadata.complete_indexing()
            if progress_callback:
                progress_callback(0, 0, Path(""), info="No files found to index")
            return ProcessingStats()

        # Store file list for resumability
        self.progressive_metadata.set_files_to_index(files_to_index)

        # Initialize structured logging session for file-by-file tracking
        operation_type = "full"  # This is _do_full_index so it's always full
        session_id = self.progress_log.start_session(
            operation_type=operation_type,
            embedding_provider=provider_name,
            embedding_model=model_name,
            files_to_index=[str(f) for f in files_to_index],
            git_branch=git_status.get("current_branch"),
            git_commit=git_status.get("current_commit"),
        )
        logger.info(f"Started structured logging session: {session_id}")

        # Get current branch for indexing
        if progress_callback:
            progress_callback(
                0, 0, Path(""), info="🌿 Analyzing git repository structure..."
            )
        current_branch = self.git_topology_service.get_current_branch() or "master"

        # BEGIN INDEXING SESSION (O(n) optimization - defer index rebuilding)
        self.vector_store_client.begin_indexing(collection_name)

        # Use BranchAwareIndexer for git-aware processing with parallel embeddings
        fatal_chunk_store_error: Optional[BaseException] = None
        try:
            # Convert absolute paths to relative paths for BranchAwareIndexer
            relative_files = []
            for file_path in files_to_index:
                try:
                    # If path is absolute and within codebase_dir, make it relative
                    if file_path.is_absolute():
                        relative_files.append(
                            str(file_path.relative_to(self.config.codebase_dir))
                        )
                    else:
                        # Already relative, use as-is
                        relative_files.append(str(file_path))
                except ValueError:
                    # Path is not within codebase_dir, use as-is (shouldn't happen in normal usage)
                    relative_files.append(str(file_path))

            with open(debug_file, "a") as f:
                f.write(
                    f"[{datetime.datetime.now().isoformat()}] Calling high-throughput parallel processing with {len(relative_files)} files\n"
                )
                f.flush()

            # Use direct high-throughput parallel processing for full index (4-8x faster)
            # Bypass branch processing wrapper to maximize parallel utilization

            # Use config.json setting directly
            if vector_thread_count is None:
                resolved_thread_count = self.config.voyage_ai.parallel_requests
            else:
                resolved_thread_count = vector_thread_count

            high_throughput_stats = self.process_files_high_throughput(
                files=files_to_index,  # Use absolute paths directly
                vector_thread_count=resolved_thread_count,
                batch_size=50,
                progress_callback=progress_callback,
                fts_manager=fts_manager,
            )

            with open(debug_file, "a") as f:
                f.write(
                    f"[{datetime.datetime.now().isoformat()}] high-throughput parallel processing completed\n"
                )
                f.flush()

            # For full indexing, hide all files that don't exist in current branch
            # This ensures proper branch isolation
            # IMPORTANT: Use ALL files in current branch, not just the ones being processed
            all_files_in_branch = list(self.file_finder.find_files())
            all_relative_files = []
            for file_path in all_files_in_branch:
                try:
                    if file_path.is_absolute():
                        all_relative_files.append(
                            str(file_path.relative_to(self.config.codebase_dir))
                        )
                    else:
                        all_relative_files.append(str(file_path))
                except ValueError:
                    all_relative_files.append(str(file_path))

            # Use thread-safe branch isolation directly from high-throughput processor
            # Only apply branch isolation for git repositories
            if self.git_topology_service.is_git_available():
                if progress_callback:
                    progress_callback(
                        0, 0, Path(""), info="Applying branch isolation cleanup..."
                    )
                self.hide_files_not_in_branch_thread_safe(
                    current_branch,
                    all_relative_files,
                    collection_name,
                    progress_callback,
                    fts_manager=fts_manager,
                )

            # Use ProcessingStats directly from high-throughput processor
            stats = high_throughput_stats
            if progress_callback:
                progress_callback(
                    0, 0, Path(""), info="Processing completed, starting cleanup..."
                )

        except Exception as e:
            logger.error(f"High-throughput processor failed during full index: {e}")
            if isinstance(e, ChunkStoreUnavailableError):
                # Bug #1746 Change 3: propagate the fatal error UNWRAPPED
                # (not folded into the generic RuntimeError below) so the
                # finally block aborts instead of finalizing.
                fatal_chunk_store_error = e
                raise
            # NO FALLBACK - fail fast in git projects
            raise RuntimeError(
                f"Git-aware indexing failed and fallbacks are disabled. "
                f"Original error: {e}"
            ) from e
        finally:
            # CRITICAL: Always finalize (or abort) the session, even on
            # exception -- this ensures FilesystemVectorStore rebuilds
            # HNSW/ID indexes on success, or discards the in-memory
            # session on a fatal chunk-store failure (Bug #1746 Change 3).
            self._finalize_or_abort_indexing_session(
                collection_name,
                fatal_chunk_store_error,
                progress_callback,
                log_prefix="Index",
            )

        # Update metadata with actual processing results
        if progress_callback:
            progress_callback(0, 0, Path(""), info="Updating progress metadata...")
        self.progressive_metadata.update_progress(
            files_processed=stats.files_processed,
            chunks_added=stats.chunks_created,
            failed_files=stats.failed_files,
        )

        # Update commit watermark AFTER successful processing
        current_branch = git_status.get("current_branch", "master")
        current_commit = git_status.get("current_commit")
        if git_status.get("git_available", False) and current_commit:
            if progress_callback:
                progress_callback(
                    0, 0, Path(""), info="Updating git commit watermark..."
                )
            self.progressive_metadata.update_commit_watermark(
                current_branch, current_commit
            )
            logger.info(
                f"✅ Updated commit watermark: {current_branch} -> {current_commit[:8]}"
            )

        # Mark as completed only if not cancelled
        if not stats.cancelled:
            if progress_callback:
                progress_callback(0, 0, Path(""), info="Finalizing indexing session...")
            self.progressive_metadata.complete_indexing()
            self.progress_log.complete_session()
        else:
            logger.info(
                "Indexing was cancelled, not marking as completed for resume capability"
            )
            self.progress_log.mark_session_cancelled()

        return stats

    def _do_incremental_index(
        self,
        batch_size: int,
        progress_callback: Optional[Callable],
        git_status: Dict[str, Any],
        provider_name: str,
        model_name: str,
        safety_buffer_seconds: int,
        quiet: bool = False,
        vector_thread_count: Optional[int] = None,
        fts_manager=None,
        trust_resume_state: bool = True,
    ) -> ProcessingStats:
        """Perform incremental indexing."""

        # 🔧 FIX: Check for interrupted operation first (before timestamp check)
        # trust_resume_state=False must skip this
        # branch too (defense in depth alongside smart_index()'s gate).
        if (
            trust_resume_state
            and self.progressive_metadata.can_resume_interrupted_operation()
        ):
            if progress_callback:
                # Get preview stats for feedback
                metadata_stats = self.progressive_metadata.get_stats()
                completed = metadata_stats.get("files_processed", 0)
                total = metadata_stats.get("total_files_to_index", 0)
                remaining = metadata_stats.get("remaining_files", 0)
                chunks_so_far = metadata_stats.get("chunks_indexed", 0)

                progress_callback(
                    0,
                    0,
                    Path(""),
                    info=f"🔄 Resuming interrupted operation: {completed}/{total} files completed ({chunks_so_far} chunks), {remaining} files remaining",
                )
            return self._do_resume_interrupted(
                batch_size,
                progress_callback,
                git_status,
                provider_name,
                model_name,
                quiet,
                vector_thread_count,
                fts_manager,
            )

        # Get resume timestamp with safety buffer (for completed operations)
        resume_timestamp = self.progressive_metadata.get_resume_timestamp(
            safety_buffer_seconds
        )

        if resume_timestamp == 0.0:
            # No previous index found, do full index
            if progress_callback:
                progress_callback(
                    0,
                    0,
                    Path(""),
                    info="No previous index found, performing full index",
                )
            return self._do_full_index(
                batch_size,
                progress_callback,
                git_status,
                provider_name,
                model_name,
                quiet,
            )

        # NOTE: start_indexing() moved to after work determination to fix idempotency bug

        # DUAL-TRACK APPROACH: Git log + filesystem timestamps
        current_branch = git_status.get("current_branch", "master")
        current_commit = git_status.get("current_commit")

        committed_files = []
        deleted_files = []

        # TRACK 1: Git log for committed changes (handles deletions!)
        if git_status.get("git_available", False) and current_commit:
            last_indexed_commit = self.progressive_metadata.get_last_indexed_commit(
                current_branch
            )

            if last_indexed_commit and last_indexed_commit != current_commit:
                logger.info(
                    f"Git commits: {last_indexed_commit[:8]} -> {current_commit[:8]}"
                )

                if progress_callback:
                    progress_callback(
                        0, 0, Path(""), info="Scanning git history for changes..."
                    )

                # Get git deltas - this is the KEY improvement for deletion detection
                git_delta = self._get_git_deltas_since_commit(
                    last_indexed_commit, current_commit
                )

                # Handle deletions FIRST (critical for git pull scenarios)
                if git_delta.deleted:
                    self.vector_store_client.ensure_provider_aware_collection(
                        self.config, self.embedding_provider, quiet
                    )
                    collection_name = self.vector_store_client.resolve_collection_name(
                        self.config, self.embedding_provider
                    )
                    if progress_callback:
                        progress_callback(
                            0,
                            len(git_delta.deleted),
                            Path(""),
                            info=f"Cleaning up {len(git_delta.deleted)} deleted files...",
                        )
                    deleted_count = self._delete_files_from_backend(
                        git_delta.deleted, collection_name, progress_callback
                    )
                    logger.info(
                        f"🗑️  Deleted {deleted_count}/{len(git_delta.deleted)} files from index"
                    )

                # Collect committed files that need indexing
                committed_files = git_delta.added + git_delta.modified
                deleted_files = git_delta.deleted

        if progress_callback:
            progress_callback(
                0, 0, Path(""), info="Scanning filesystem for untracked changes..."
            )

        # TRACK 2: Filesystem timestamp for uncommitted changes
        working_dir_files = list(self.file_finder.find_modified_files(resume_timestamp))

        # Combine both tracks, de-duplicating on the absolutized path.
        # committed_files (Track 1, git delta) are REPO-RELATIVE strings;
        # working_dir_files (Track 2, mtime scan) are already ABSOLUTE.
        # Absolutize every entry FIRST, then de-duplicate -- otherwise the
        # same physical file reported by both tracks in two different
        # string formats (e.g. "src/a.py" vs "/repo/src/a.py") survives as
        # two distinct set members and gets indexed twice (Bug #1574).
        # NOTE: deliberately NOT calling .resolve() here -- both tracks
        # already share the same codebase_dir prefix, so plain
        # absolutization (prefixing with codebase_dir) is sufficient to
        # unify them. Calling .resolve() would follow symlinks and could
        # break the downstream relative_to(codebase_dir) computation if
        # codebase_dir itself is a symlinked path.
        codebase = Path(self.config.codebase_dir)
        unique_files_to_index = {
            codebase / f if not Path(f).is_absolute() else Path(f)
            for f in [str(f) for f in committed_files]
            + [str(f) for f in working_dir_files]
        }
        # committed_files (Track 1, git delta) entries are repo-relative
        # strings taken from git diff output; _should_index_file only
        # applies string-only eligibility filtering (extension/exclude),
        # never containment. Reject any entry whose resolved location is
        # not inside the codebase root before it is treated as work to do.
        files_to_index = self._filter_paths_within_codebase_root(unique_files_to_index)

        # Bug #1969 Round 5 (R4-F1): fold in any DURABLY pending self-heal
        # reprocess paths (from THIS collection's sidecar, recorded by
        # recover_from_corrupt_id_index_by_wiping_files() itself -- covers
        # every trigger site, in this run or a past one) BEFORE the
        # "nothing to do" check below, so even a plain rerun with zero
        # git/mtime changes still reprocesses a file a past wipe left
        # behind instead of early-returning as "up to date".
        _self_heal_collection_name = self.vector_store_client.resolve_collection_name(
            self.config, self.embedding_provider
        )
        pending_self_heal_before, files_to_index = (
            self._fold_in_pending_self_heal_paths(
                _self_heal_collection_name, files_to_index
            )
        )
        # Files the previous run could not index are retried even when
        # nothing changed since, so they never stay missing from the index.
        retried_failures, files_to_index = self._merge_recorded_failures(files_to_index)

        if not files_to_index and not deleted_files:
            # SAFETY CHECK (Issue #1975): stored chunks with a zero processed-
            # file count are ambiguous. `files_processed` is a PER-RUN counter
            # (start_indexing() resets it), so it is 0 both after a completed
            # zero-file run and after an interrupted run that left a partial
            # store -- and "completed" status cannot tell them apart either.
            # Neither wipe-and-re-embed nor blind trust: reconcile the store
            # against disk (only missing/changed files are embedded), and
            # record that verification durably so it runs once.
            ambiguous_store = False
            try:
                collection_name = self.vector_store_client.resolve_collection_name(
                    self.config, self.embedding_provider
                )
                vector_points = self.vector_store_client.count_points(collection_name)
                metadata = self.progressive_metadata.metadata
                ambiguous_store = (
                    vector_points > 0
                    and metadata.get("files_processed", 0) == 0
                    and not metadata.get("store_verified_by_reconcile", False)
                )
            except Exception as e:
                logger.warning(
                    f"Index consistency check could not count stored points, "
                    f"skipping it this run: {e}"
                )

            if ambiguous_store:
                if progress_callback:
                    progress_callback(
                        0,
                        0,
                        Path(""),
                        info=f"🔍 {vector_points} stored chunks but no processed files on record - reconciling the index against disk",
                    )
                reconcile_stats = self._reconcile_and_verify(
                    batch_size,
                    progress_callback,
                    git_status,
                    provider_name,
                    model_name,
                    None,
                    quiet,
                    vector_thread_count,
                    fts_manager,
                )
                if (
                    not reconcile_stats.cancelled
                    and git_status.get("git_available", False)
                    and current_commit
                ):
                    self.progressive_metadata.update_commit_watermark(
                        current_branch, current_commit
                    )
                return reconcile_stats

            # No changes at all - system is up-to-date, don't touch metadata
            if progress_callback:
                progress_callback(
                    0,
                    0,
                    Path(""),
                    info="No files modified since last index - nothing to do",
                )
            # Update commit watermark even if no files to index
            if git_status.get("git_available", False) and current_commit:
                self.progressive_metadata.update_commit_watermark(
                    current_branch, current_commit
                )
            # FTS bootstrap: NOTE, #1763 code review (CRITICAL-1) -- the
            # full-from-disk repopulation for a brand-new/rebuilt FTS
            # index now happens EAGERLY and UNCONDITIONALLY in
            # smart_index() itself, immediately after
            # fts_manager.initialize_index(create_new=create_new_fts),
            # rather than only here in the zero-changed-files branch.
            # Calling _populate_fts_from_all_files() a second time here
            # would double-add every file's FTS document (this call site
            # has no delete-before-add supersession, unlike the per-file
            # processing path) -- so this is deliberately NOT called
            # again.
            # CRITICAL: Don't call complete_indexing() here as no indexing session was started
            # This preserves existing metadata when system is already up-to-date
            return ProcessingStats()

        # A zero-change run must not create a collection that will never reach
        # end_indexing(), which commits a fresh CHUNKS_DB layout on disk.
        self.vector_store_client.ensure_provider_aware_collection(
            self.config, self.embedding_provider, quiet
        )

        # Issue #1975: a delete-only run (deletions already applied above, no
        # file to index) records its completion without starting a fresh
        # session, which would reset the processed-file counters of the
        # index it leaves intact. set_files_to_index() below still replaces
        # any stale work list with the (empty) one, so a completed status
        # never sits next to a prior run's file tracking.
        delete_only = not files_to_index
        if not delete_only and self.progressive_metadata.metadata["status"] != (
            "in_progress"
        ):
            # CRITICAL: Now that we know work is needed, start the indexing session
            self.progressive_metadata.start_fresh_indexing(
                provider_name, model_name, git_status
            )

        # Initialize structured logging session for incremental indexing
        session_id = self.progress_log.start_session(
            operation_type="incremental",
            embedding_provider=provider_name,
            embedding_model=model_name,
            files_to_index=[str(f) for f in files_to_index],
            git_branch=git_status.get("current_branch"),
            git_commit=git_status.get("current_commit"),
        )
        logger.info(f"Started incremental indexing session: {session_id}")

        # ⚠️  CRITICAL: Setup message MUST use total=0 to show as ℹ️ message, not progress bar
        if progress_callback:
            change_summary = []
            if committed_files:
                change_summary.append(f"{len(committed_files)} git changes")
            if working_dir_files:
                change_summary.append(f"{len(working_dir_files)} working dir changes")
            if deleted_files:
                change_summary.append(f"{len(deleted_files)} deletions")

            info_msg = f"Incremental update: {' + '.join(change_summary)}"
            progress_callback(0, 0, Path(""), info=info_msg)

        # Store file list for resumability
        self.progressive_metadata.set_files_to_index(files_to_index)

        # Use HighThroughputProcessor directly for git-aware processing (STORY 3 MIGRATION)
        fatal_chunk_store_error: Optional[BaseException] = None
        try:
            # Get current branch for indexing
            current_branch = self.git_topology_service.get_current_branch() or "master"

            # Ensure collection exists
            collection_name = self.vector_store_client.resolve_collection_name(
                self.config, self.embedding_provider
            )

            # BEGIN INDEXING SESSION (O(n) optimization - defer index rebuilding)
            self.vector_store_client.begin_indexing(collection_name)

            # Use direct high-throughput parallel processing for incremental indexing (4-8x faster)
            # STORY 3: Use process_files_high_throughput() directly instead of branch wrapper

            # Use config.json setting directly
            if vector_thread_count is None:
                resolved_thread_count = self.config.voyage_ai.parallel_requests
            else:
                resolved_thread_count = vector_thread_count

            if delete_only:
                high_throughput_stats = ProcessingStats()
            else:
                high_throughput_stats = self.process_files_high_throughput(
                    files=files_to_index,  # Use absolute paths directly
                    vector_thread_count=resolved_thread_count,
                    batch_size=50,
                    progress_callback=progress_callback,
                    fts_manager=fts_manager,
                )

            # Bug #1969 Round 5 (R4-F1): a same-run self-heal escalation
            # triggered while processing the files above may have wiped a
            # DIFFERENT file's indexed chunks -- one this run's own
            # git-diff/mtime detection never selected. Guarantee it gets
            # reprocessed in THIS SAME run before the session finalizes.
            (
                high_throughput_stats,
                pending_self_heal_after,
            ) = self._reprocess_newly_pending_self_heal_paths(
                collection_name,
                files_to_index,
                high_throughput_stats,
                resolved_thread_count,
                progress_callback,
                fts_manager,
            )

            # For incremental indexing, also hide files that don't exist in current branch
            # This ensures proper branch isolation even during incremental updates
            # IMPORTANT: Use ALL files in current branch, not just the ones being processed
            all_files_in_branch = list(self.file_finder.find_files())
            all_relative_files = []
            for file_path in all_files_in_branch:
                try:
                    if file_path.is_absolute():
                        all_relative_files.append(
                            str(file_path.relative_to(self.config.codebase_dir))
                        )
                    else:
                        all_relative_files.append(str(file_path))
                except ValueError:
                    all_relative_files.append(str(file_path))

            # Use thread-safe branch isolation directly from high-throughput processor
            # Only apply branch isolation for git repositories
            if self.git_topology_service.is_git_available():
                self.hide_files_not_in_branch_thread_safe(
                    current_branch,
                    all_relative_files,
                    collection_name,
                    progress_callback,
                    fts_manager=fts_manager,
                )

            # Use ProcessingStats directly from high-throughput processor
            stats = high_throughput_stats

        except Exception as e:
            logger.error(
                f"HighThroughputProcessor failed during incremental indexing in git project: {e}"
            )
            if isinstance(e, ChunkStoreUnavailableError):
                # Bug #1746 Change 3: propagate unwrapped so finally aborts
                # instead of finalizing.
                fatal_chunk_store_error = e
                raise
            # NO FALLBACK - fail fast in git projects
            raise RuntimeError(
                f"Git-aware incremental indexing failed and fallbacks are disabled. "
                f"Original error: {e}"
            ) from e
        finally:
            # CRITICAL: Always finalize (or abort) the session, even on
            # exception (Bug #1746 Change 3).
            self._finalize_or_abort_indexing_session(
                collection_name,
                fatal_chunk_store_error,
                progress_callback,
                log_prefix="Incremental index",
            )

        # Update metadata with actual processing results
        if progress_callback:
            progress_callback(
                0, 0, Path(""), info="Updating incremental progress metadata..."
            )
        self.progressive_metadata.update_progress(
            files_processed=stats.files_processed,
            chunks_added=stats.chunks_created,
            failed_files=stats.failed_files,
        )

        # Update commit watermark AFTER successful processing
        current_branch = git_status.get("current_branch", "master")
        current_commit = git_status.get("current_commit")

        if git_status.get("git_available", False) and current_commit:
            self.progressive_metadata.update_commit_watermark(
                current_branch, current_commit
            )
            logger.info(
                f"✅ Updated commit watermark: {current_branch} -> {current_commit[:8]}"
            )

        # Mark as completed only if not cancelled
        if not stats.cancelled:
            self._record_failures_of_run(stats, retried_failures, [])
            self.progressive_metadata.complete_indexing()
            self.progress_log.complete_session()
        else:
            logger.info(
                "Indexing was cancelled, not marking as completed for resume capability"
            )
            self.progress_log.mark_session_cancelled()

        # Bug #1969 Round 5 (R4-F1/R4-F3): clear the durable sidecar
        # entries this run consulted (both the upfront fold-in and the
        # post-pass re-check). _clear_self_heal_reprocess_paths_if_safe()
        # itself checks stats.cancelled and no-ops when cancelled -- see
        # that method's own docstring/implementation.
        self._clear_self_heal_reprocess_paths_if_safe(
            _self_heal_collection_name,
            pending_self_heal_before | pending_self_heal_after,
            stats,
        )

        # Git-delta deletions applied above changed the index too.
        stats.index_entries_changed += len(deleted_files)
        return stats

    def _populate_fts_from_all_files(
        self, fts_manager, progress_callback=None
    ) -> List[Tuple[Path, str]]:
        """Fill a new or rebuilt FTS index from every file on disk (chunk
        level, no embedding). Returns the (file, error) pairs it could not
        index, for finish_fts_run's per-file rule (#2056): reported and the
        index left unmarked; the run fails only if nothing was indexed."""
        from .fts_lifecycle import bootstrap_fts_from_disk

        return bootstrap_fts_from_disk(
            fts_manager, self.config, self.file_finder.find_files(), progress_callback
        )

    def _reconcile_and_verify(
        self,
        batch_size: int,
        progress_callback: Optional[Callable],
        git_status: Dict[str, Any],
        provider_name: str,
        model_name: str,
        files_count_to_process: Optional[int],
        quiet: bool,
        vector_thread_count: Optional[int],
        fts_manager: Optional[TantivyIndexManager],
    ) -> ProcessingStats:
        """Reconcile, then record that the whole store was verified against
        disk (Issue #1975) -- only for a run that was neither cancelled nor
        limited to a subset of files."""
        stats = self._do_reconcile_with_database(
            batch_size,
            progress_callback,
            git_status,
            provider_name,
            model_name,
            files_count_to_process,
            quiet,
            vector_thread_count,
            fts_manager,
        )
        if not stats.cancelled and files_count_to_process is None:
            self.progressive_metadata.mark_store_verified()
        return stats

    def _retry_pending_fts_restores(
        self,
        fts_manager,
        disk_files: Set[str],
        queued: Set[str],
        branch: str,
        attempted: Set[str],
        failed: Set[str],
    ) -> int:
        """Retry FTS restores a past reconcile could not complete, and
        persist what is still owed (this run's failures included). A file
        no longer on disk/eligible, queued for re-indexing (which writes its
        FTS documents), or hidden again on the branch is dropped. Returns
        the number of restores completed by this call."""
        pending = self.progressive_metadata.get_fts_restore_pending()
        still_owed = set(failed)
        restored = 0
        for path in pending:
            if path in attempted or path not in disk_files or path in queued:
                continue
            if branch in self._reconcile_hidden_on_all_points.get(path, set()):
                continue
            if self._restore_file_in_fts(fts_manager, path):
                restored += 1
            else:
                still_owed.add(path)
        if pending or still_owed:
            self.progressive_metadata.set_fts_restore_pending(sorted(still_owed))
        return restored

    def _restore_file_in_fts(self, fts_manager, relative_path: str) -> bool:
        """Re-add an un-hidden file's FTS documents from its current content,
        through the ONE shared replacement (every stale document of the path
        deleted first, then one per current chunk: never listed twice).
        Returns False after logging a per-file failure at ERROR (the caller
        records it for retry); RuntimeError (writer not initialized -- a
        wiring bug) propagates, as on the per-file indexing path."""
        from .fts_file_documents import FileFtsDocuments

        try:
            FileFtsDocuments(self.config).replace_in_index(
                fts_manager, Path(self.config.codebase_dir) / relative_path
            )
            return True
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"FTS restore failed for '{relative_path}': {e}")
            return False

    def _do_reconcile_with_database(
        self,
        batch_size: int,
        progress_callback: Optional[Callable],
        git_status: Dict[str, Any],
        provider_name: str,
        model_name: str,
        files_count_to_process: Optional[int] = None,
        quiet: bool = False,
        vector_thread_count: Optional[int] = None,
        fts_manager: Optional[TantivyIndexManager] = None,
    ) -> ProcessingStats:
        """Reconcile disk files with database contents and index missing/modified files.

        ``fts_manager``
        is accepted (not hardcoded to None) so a caller with
        ``enable_fts=True`` -- in particular the ``trust_resume_state=False``
        interrupted-operation fallback in ``smart_index()`` -- keeps
        feeding the SAME already-initialized FTS index reconcile's
        semantic-side files go into, instead of every reconciled file's
        FTS update being silently dropped.
        """

        # Ensure provider-aware collection exists
        collection_name = self.vector_store_client.ensure_provider_aware_collection(
            self.config, self.embedding_provider, quiet
        )

        # Get current branch early for working directory aware reconcile
        current_branch = "master"  # Default branch name
        if self.git_topology_service.is_git_available():
            current_branch = self.git_topology_service.get_current_branch() or "master"

        # Get all files that should be indexed (from disk)
        all_files_to_index = list(self.file_finder.find_files())

        if not all_files_to_index and progress_callback:
            # ⚠️  CRITICAL: total=0 makes this show as ℹ️ message in CLI
            progress_callback(0, 0, Path(""), info="No files found to index")
        # Issue #1999: no early return on an empty scan -- stored files that
        # a filter now excludes (possibly every file) must still go through
        # the deletion/hide logic below. The nothing-to-index path further
        # down still marks the operation completed, so a lingering
        # "in_progress"/"failed" status (e.g. the trust_resume_state=False
        # fallback after an interrupted run) is cleared as before.

        # Query database to see what files are already indexed with timestamps
        if progress_callback:
            progress_callback(
                0,
                0,
                Path(""),
                info=f"Checking database collection '{collection_name}' for indexed files...",
            )

        # Issue #1505: defensive default so a mocked/failed snapshot never
        # leaves this attribute missing before the main reconcile loop reads it.
        self._reconcile_db_content_ids: Dict[str, str] = {}
        # Codex #1505 review, Finding 1: same defensive default for the
        # hidden_branches map the branch-visibility loop reads.
        self._reconcile_hidden_branches: Dict[str, List[str]] = {}
        self._reconcile_hidden_on_all_points: Dict[str, Set[str]] = {}
        # Issue #2013: same defensive default for the racy-timestamp index.
        self._reconcile_working_dir_index: Dict[str, Tuple[float, Optional[str]]] = {}

        # Get indexed files using efficient snapshot approach (no infinite loops, minimal memory)
        indexed_files_with_timestamps = self._get_indexed_files_snapshot(
            collection_name, progress_callback
        )

        # Show what was found in database with meaningful reconcile feedback
        if progress_callback:
            progress_callback(
                0,
                0,
                Path(""),
                info=f"📊 Found {len(indexed_files_with_timestamps)} files in database collection '{collection_name}', {len(all_files_to_index)} files on disk",
            )

        # Bug #471: Batch-check all modified files in one subprocess call
        self._reconcile_modified_files = self._get_modified_files_set()

        # Issue #1505: Batch-fetch every tracked file's committed blob hash in
        # ONE `git ls-tree -r HEAD` subprocess call, instead of spawning a
        # `git log -1 -- path` subprocess PER unchanged file inside the loop
        # below. Only meaningful for git-available repos; non-git repos keep
        # their existing mtime/size-only comparison scheme untouched.
        if self.git_topology_service.is_git_available():
            self._reconcile_head_blob_hashes = self._get_head_blob_hash_map()
        else:
            self._reconcile_head_blob_hashes = {}

        # Codex #1505 review, Finding 2: track how many files fall back to
        # the slow per-file `git log` content-id computation during this
        # reconcile run, so a high fallback rate (map failed/mostly-empty)
        # can be surfaced LOUDLY after the main per-file loop below instead
        # of silently degrading to the O(N) stall Issue #1505 fixed.
        self._reconcile_fallback_count = 0

        # Find files that need to be indexed using working directory aware comparison
        files_to_index = []
        modified_files = 0
        missing_files = 0
        # Files whose analysis THROWS were never verified. The run is still
        # marked completed (leaving it open would make every following
        # server-context run reconcile again), but these files are recorded
        # as failed so the partial result is never silent.
        analysis_failed_paths: List[str] = []

        # Bug #1998: normalize the indexed paths into a set ONCE. Rebuilding
        # (and linearly scanning) this collection per file on disk made the
        # analysis O(files_on_disk x indexed_files).
        # A stored absolute path outside the codebase root is keyed by its
        # own string, exactly as _get_indexed_files_snapshot keys it -- it
        # can never match an on-disk relative path, and must not abort the
        # run (reconcile is also the crash-recovery path).
        indexed_relative_paths: Set[Any] = set()
        for p in indexed_files_with_timestamps.keys():
            try:
                indexed_relative_paths.add(
                    str(Path(p).relative_to(self.config.codebase_dir))
                    if Path(p).is_absolute()
                    else p
                )
            except ValueError:
                indexed_relative_paths.add(str(p))

        for file_path in all_files_to_index:
            try:
                # Get relative path for content ID generation
                relative_path = str(file_path.relative_to(self.config.codebase_dir))

                # Get what the current effective content ID should be
                current_effective_id = self._get_effective_content_id_for_reconcile(
                    relative_path
                )

                # RECONCILE FIX: Check if file exists in database AT ALL, not just visible in current branch
                # Reconcile is about disk-to-database consistency, not branch visibility
                file_in_db = relative_path in indexed_relative_paths

                if not file_in_db:
                    # File exists on disk but NOT in database at all
                    files_to_index.append(file_path)
                    missing_files += 1
                else:
                    # File exists in database - check if content changed.
                    # Issue #1505: look up the pre-computed batched content-id
                    # map (built once in `_get_indexed_files_snapshot` from
                    # the same bulk scroll already used for the timestamp
                    # snapshot) instead of issuing a per-file scroll_points
                    # query (the former per-file lookup, since removed).
                    db_content_id = self._reconcile_db_content_ids.get(relative_path)

                    if db_content_id and current_effective_id != db_content_id:
                        # Content changed - needs re-indexing
                        files_to_index.append(file_path)
                        modified_files += 1
                        logger.info(
                            f"RECONCILE: Content ID mismatch for {relative_path}: "
                            f"current='{current_effective_id}' vs db='{db_content_id}' - will re-index"
                        )
                    elif db_content_id and self._working_dir_file_racily_modified(
                        relative_path
                    ):
                        # Issue #2013: ids match but the file was rewritten
                        # within the second it was read in.
                        files_to_index.append(file_path)
                        modified_files += 1
                        logger.info(
                            f"RECONCILE: {relative_path} changed within the "
                            "second it was read in (content hash differs) - will re-index"
                        )
                    # else: file is up-to-date, don't re-index

            except Exception as e:
                # File might have issues, log and skip
                logger.warning(f"Failed to analyze file {file_path} for reconcile: {e}")
                analysis_failed_paths.append(str(file_path))
                continue

        # Codex #1505 review, Finding 2: a degraded reconcile run (batched
        # HEAD blob-hash map failed/mostly-empty, forcing most or all
        # committed files onto the slow per-file `git log` fallback) must
        # be LOUD and observable -- never a silent multi-hour O(N) stall,
        # per this project's anti-silent-failure rule. Correctness is
        # unaffected either way; only visibility is added here, and
        # reconcile is never hard-aborted (a degraded-but-working reconcile
        # beats a failed one).
        if self.git_topology_service.is_git_available() and all_files_to_index:
            fallback_count = getattr(self, "_reconcile_fallback_count", 0)
            total_files = len(all_files_to_index)
            fallback_ratio = fallback_count / total_files
            map_entirely_empty = not self._reconcile_head_blob_hashes
            if (map_entirely_empty and fallback_count > 0) or fallback_ratio > 0.10:
                logger.error(
                    "RECONCILE DEGRADED: %d/%d files (%.1f%%) fell back to "
                    "the slow per-file `git log` content-id lookup this "
                    "run because the batched HEAD blob-hash map "
                    "(_get_head_blob_hash_map) was empty or missing "
                    "entries for most files. This can reintroduce the "
                    "O(N) per-file git-subprocess stall Issue #1505 fixed "
                    "-- investigate git ls-tree failures.",
                    fallback_count,
                    total_files,
                    fallback_ratio * 100,
                )

        # NEW: For git projects, unhide files that should be visible in current branch
        files_unhidden = 0  # Initialize for all code paths
        fts_restores_retried = 0

        if self.git_topology_service.is_git_available():
            # CRITICAL FIX: Check ALL files in database for unhiding, not just files_to_index
            # This fixes the bug where files hidden in other branches don't get unhidden
            # when switching back to the branch where they should be visible
            disk_files_set = {
                str(f.relative_to(self.config.codebase_dir)) for f in all_files_to_index
            }
            queued_relative_paths = {
                str(f.relative_to(self.config.codebase_dir)) for f in files_to_index
            }
            # Issue #1999: FTS restores of un-hidden files, for retry state.
            restore_attempted: Set[str] = set()
            restore_failed: Set[str] = set()
            for indexed_file_path in indexed_files_with_timestamps:
                # Convert indexed file path to relative string for comparison
                try:
                    if hasattr(indexed_file_path, "relative_to"):
                        relative_file_path = str(
                            indexed_file_path.relative_to(self.config.codebase_dir)
                        )
                    else:
                        relative_file_path = str(indexed_file_path)
                except ValueError:
                    continue

                # Check if this file exists on disk in current branch (should be visible)
                if relative_file_path in disk_files_set:
                    # Codex #1505 review, Finding 1: derive hidden_branches
                    # from the bulk snapshot already fetched in
                    # `_get_indexed_files_snapshot` instead of issuing a
                    # fresh `scroll_points` query PER indexed file -- this
                    # loop previously defeated the O(1)-ish DB-call goal of
                    # Issue #1505's fix by reintroducing a per-file query in
                    # the same reconcile pass.
                    hidden_branches = self._reconcile_hidden_branches.get(
                        relative_file_path, []
                    )
                    if current_branch in hidden_branches:
                        # File exists on disk but is hidden for current branch - unhide it
                        self._ensure_file_visible_in_branch_thread_safe(
                            relative_file_path, current_branch, collection_name
                        )
                        files_unhidden += 1
                        # Issue #1999: hiding dropped the file's FTS document;
                        # restore it from current content (no embedding).
                        # A file queued for re-indexing gets its FTS
                        # documents from that processing instead.
                        if (
                            fts_manager is not None
                            and relative_file_path not in queued_relative_paths
                        ):
                            restore_attempted.add(relative_file_path)
                            if not self._restore_file_in_fts(
                                fts_manager, relative_file_path
                            ):
                                restore_failed.add(relative_file_path)

            if fts_manager is not None:
                fts_restores_retried = self._retry_pending_fts_restores(
                    fts_manager,
                    disk_files_set,
                    queued_relative_paths,
                    current_branch,
                    restore_attempted,
                    restore_failed,
                )

            if files_unhidden > 0 and progress_callback:
                progress_callback(
                    0,
                    0,
                    Path(""),
                    info=f"👁️  Made {files_unhidden} files visible in current branch '{current_branch}'",
                )

        # DIAGNOSTIC: Log reconcile progress after visibility update
        logger.info(
            f"Reconcile visibility update completed - made {files_unhidden} files visible in branch '{current_branch}', proceeding to deletion detection"
        )

        # NEW: Detect files that exist in database but were deleted from filesystem
        deleted_files = []
        disk_files_set = {
            str(f.relative_to(self.config.codebase_dir)) for f in all_files_to_index
        }

        for indexed_file_path in indexed_files_with_timestamps:
            # Convert to string for comparison. A stored path outside the
            # codebase root keeps its own absolute string (snapshot
            # semantics).
            outside_root = False
            try:
                indexed_file_str = (
                    str(indexed_file_path.relative_to(self.config.codebase_dir))
                    if hasattr(indexed_file_path, "relative_to")
                    else str(indexed_file_path)
                )
            except ValueError:
                indexed_file_str = str(indexed_file_path)
                outside_root = True

            if outside_root:
                # An out-of-root path can never appear in the relative
                # on-disk scan, so "not scanned" says nothing about it: remove
                # it (git hide / non-git hard delete) only when the file is
                # really gone from its own absolute location.
                if not Path(indexed_file_str).exists():
                    deleted_files.append(indexed_file_str)
            elif indexed_file_str not in disk_files_set:
                # CRITICAL: Check if file genuinely deleted from filesystem vs just branch switch
                if self.is_git_aware():
                    # Git projects hide (never hard-delete) such a file on the
                    # current branch: either it was genuinely deleted from
                    # disk, or (Issue #1999) it is on disk but no longer
                    # eligible (exclusion filter / extension change) -- branch
                    # isolation would hide it, but it never runs when nothing
                    # needs indexing. A path already hidden on this branch (a
                    # past deletion or exclusion) is skipped, so each one
                    # costs one hide, once, not one per reconcile. "Hidden"
                    # means hidden on EVERY point: a path is still visible
                    # while any of its points is.
                    if current_branch not in self._reconcile_hidden_on_all_points.get(
                        indexed_file_str, set()
                    ):
                        deleted_files.append(indexed_file_str)
                else:
                    # File exists in database but not on disk - was deleted (non-git projects)
                    deleted_files.append(indexed_file_str)

        # Handle deleted files using branch-aware strategy
        if deleted_files:
            collection_name = self.vector_store_client.resolve_collection_name(
                self.config, self.embedding_provider
            )

            for deleted_file in deleted_files:
                self.delete_file_branch_aware(
                    deleted_file, collection_name, watch_mode=False
                )
                # Issue #1999: branch isolation (which also drops hidden files'
                # FTS documents) does not run when nothing needs indexing, so
                # the full-text side is cleaned here too. Deferred: the single
                # commit happens in smart_index()'s finally.
                if fts_manager is not None:
                    try:
                        fts_manager.delete_document_deferred(deleted_file)
                    except Exception as e:
                        logger.warning(
                            f"FTS delete failed for '{deleted_file}' during reconcile: {e}"
                        )

            if progress_callback:
                progress_callback(
                    0,
                    0,
                    Path(""),
                    info=f"🗑️  Cleaned up {len(deleted_files)} deleted or no longer indexed files from database",
                )

        # Bug #1969 Round 5 (R4-F1): fold in any durably pending self-heal
        # reprocess paths -- defense-in-depth. Reconcile's OWN missing-
        # file detection above already catches a wholly-wiped file
        # naturally (zero DB points -> file_in_db=False -> already in
        # files_to_index), but this makes the consultation explicit and
        # ensures the sidecar entry gets cleared below once this run
        # actually reprocesses (or confirms up-to-date) every pending
        # path.
        pending_self_heal_reconcile, files_to_index = (
            self._fold_in_pending_self_heal_paths(collection_name, files_to_index)
        )

        # Index entries changed without processing a file (see _finish_run).
        visibility_changes = files_unhidden + fts_restores_retried + len(deleted_files)

        if not files_to_index:
            if progress_callback:
                progress_callback(
                    0,
                    0,
                    Path(""),
                    info=f"✅ All {len(all_files_to_index)} files up-to-date - no reconciliation needed",
                )
            if analysis_failed_paths:
                # Nothing was queued, but not because every file was
                # verified up-to-date -- some candidates' analysis THREW.
                # Complete with those files recorded as failed and reported
                # in the returned stats (never a silent completion); the
                # next run retries them. Pending self-heal entries are kept,
                # since those files were never actually checked.
                logger.warning(
                    "Reconcile found nothing to index, but %d file(s) "
                    "failed analysis and were skipped; marking the operation "
                    "completed with them recorded as failed files.",
                    len(analysis_failed_paths),
                )
                self.progressive_metadata.set_failed_file_paths(
                    analysis_failed_paths, failed_count=len(analysis_failed_paths)
                )
                self.progressive_metadata.complete_indexing()
                failure_stats = self._analysis_failure_stats(analysis_failed_paths)
                failure_stats.index_entries_changed = visibility_changes
                return failure_stats
            # Same as the "no files on disk" early
            # return above -- finding nothing to reconcile must still mark
            # the operation completed. Every file was verified, so no
            # recorded failure remains.
            self.progressive_metadata.set_failed_file_paths([], failed_count=0)
            self.progressive_metadata.complete_indexing()
            # Nothing to reprocess -- but any pending self-heal entries
            # were, by construction (reconcile's own full disk/DB
            # comparison just ran), either already covered above or
            # genuinely up-to-date/deleted. Safe to clear.
            self._clear_self_heal_reprocess_paths_if_safe(
                collection_name, pending_self_heal_reconcile, ProcessingStats()
            )
            return ProcessingStats(index_entries_changed=visibility_changes)

        # Apply files count limit if specified (for testing)
        if files_count_to_process is not None and files_to_index:
            original_count = len(files_to_index)
            files_to_index = files_to_index[:files_count_to_process]
            if progress_callback and len(files_to_index) < original_count:
                progress_callback(
                    0,
                    0,
                    Path(""),
                    info=f"TESTING: Limited processing to {len(files_to_index)} files (of {original_count} total)",
                )

        # Show what we're reconciling
        if progress_callback:
            total_files = len(all_files_to_index)
            to_index = len(files_to_index)
            already_indexed = total_files - to_index  # Files that are truly up-to-date

            status_parts = []
            if missing_files > 0:
                status_parts.append(f"{missing_files} missing")
            if modified_files > 0:
                status_parts.append(f"{modified_files} modified")

            if status_parts:
                status_str = " + ".join(status_parts)
                progress_callback(
                    0,
                    0,
                    Path(""),
                    info=f"🔍 Reconcile: {already_indexed}/{total_files} files up-to-date, indexing {to_index} files ({status_str})",
                )
            else:
                progress_callback(
                    0,
                    0,
                    Path(""),
                    info=f"🔍 Reconcile: {already_indexed}/{total_files} files up-to-date, indexing {to_index} files",
                )

        # Start/update indexing metadata
        if self.progressive_metadata.metadata["status"] != "in_progress":
            self.progressive_metadata.start_fresh_indexing(
                provider_name, model_name, git_status
            )

        # Store file list for resumability
        self.progressive_metadata.set_files_to_index(files_to_index)

        # BEGIN INDEXING SESSION (O(n) optimization - defer index rebuilding).
        # Bug #1575 Fix 4: hoisted BEFORE the non-git modified-file delete
        # loop below (previously called only after it) so every delete
        # this loop triggers lands INSIDE the session -- tracked via
        # _indexing_session_changes and persisted ONCE at end_indexing(),
        # instead of each delete independently persisting path_index.bin
        # (and co-persisting the entire id_index.bin) on its own. Measured
        # 5.6x slower incremental refresh on a 4000-file collection.
        self.vector_store_client.begin_indexing(collection_name)

        # CRITICAL FIX: In non-git mode, delete old chunks for modified files before re-indexing
        # This ensures old content doesn't persist alongside new content
        #
        # Bug #1575 round 6, item 2: this whole section runs AFTER
        # begin_indexing() opened a session but BEFORE the BranchAwareIndexer
        # try/finally below (whose finally calls end_indexing()) starts. An
        # exception here (delete_by_filter() or progress_callback() raising)
        # used to propagate WITHOUT ever finalizing the session --
        # permanently leaking it (self._indexing_session_changes and this
        # collection's cached PathIndex/id_index stay open until process
        # restart, disabling out-of-session Gap D/B persistence). Wrapped in
        # its own try/except so end_indexing() always runs before any
        # exception from this section propagates.
        try:
            if not self.is_git_aware() and files_to_index:
                # Get relative paths that are being modified (not newly added)
                modified_relative_files = []
                for file_path in files_to_index:
                    try:
                        # Check if this file was previously indexed (exists in database)
                        # indexed_files_with_timestamps has Path objects as keys
                        if file_path in indexed_files_with_timestamps:
                            if file_path.is_absolute():
                                relative_path = str(
                                    file_path.relative_to(self.config.codebase_dir)
                                )
                            else:
                                relative_path = str(file_path)
                            modified_relative_files.append(relative_path)
                    except ValueError:
                        continue

                # Delete old chunks for modified files
                if modified_relative_files:
                    deleted_count = 0
                    for relative_file_path in modified_relative_files:
                        # Bug #1575 Fix 4 investigation: was previously called
                        # as (filter_dict, collection_name) -- the WRONG order
                        # against the real FilesystemVectorStore.delete_by_filter
                        # (self, collection_name, filter_conditions) signature.
                        # Confirmed live: the swapped call raised TypeError
                        # inside scroll_points, caught by delete_by_filter's own
                        # broad except-Exception and silently turned into
                        # `return False` -- this cleanup had never actually
                        # deleted anything. Fixed to the correct order.
                        success = self.vector_store_client.delete_by_filter(
                            collection_name,
                            {
                                "must": [
                                    {
                                        "key": "path",
                                        "match": {"value": relative_file_path},
                                    }
                                ]
                            },
                        )
                        if success:
                            deleted_count += 1

                    if progress_callback and deleted_count > 0:
                        progress_callback(
                            0,
                            0,
                            Path(""),
                            info=f"🗑️  Cleaned up old content for {deleted_count} modified files",
                        )
        except Exception:
            self.vector_store_client.end_indexing(collection_name, progress_callback)
            raise

        # Use BranchAwareIndexer for git-aware processing with parallel embeddings (SINGLE PROCESSING PATH)
        fatal_chunk_store_error: Optional[BaseException] = None
        try:
            # Convert absolute paths to relative paths for BranchAwareIndexer
            relative_files = []
            for file_path in files_to_index:
                try:
                    # If path is absolute and within codebase_dir, make it relative
                    if file_path.is_absolute():
                        relative_files.append(
                            str(file_path.relative_to(self.config.codebase_dir))
                        )
                    else:
                        # Already relative, use as-is
                        relative_files.append(str(file_path))
                except ValueError:
                    # Path is not within codebase_dir, use as-is (shouldn't happen in normal usage)
                    relative_files.append(str(file_path))

            # Get current branch for indexing
            current_branch = self.git_topology_service.get_current_branch() or "master"

            # Calculate unchanged files (all disk files NOT in files_to_index)
            # This is critical for branch isolation - prevents hiding files that should be visible
            files_to_index_set = set(files_to_index)
            unchanged_file_paths = []
            for file_path in all_files_to_index:
                if file_path not in files_to_index_set:
                    try:
                        # Convert to relative path (same pattern as changed_files above)
                        if file_path.is_absolute():
                            unchanged_file_paths.append(
                                str(file_path.relative_to(self.config.codebase_dir))
                            )
                        else:
                            unchanged_file_paths.append(str(file_path))
                    except ValueError:
                        # Path is not within codebase_dir, use as-is
                        unchanged_file_paths.append(str(file_path))

            # Use high-throughput parallel processing for reconcile (4-8x faster)
            branch_result = self.process_branch_changes_high_throughput(
                old_branch="",  # No old branch for reconcile
                new_branch=current_branch,
                changed_files=relative_files,
                unchanged_files=unchanged_file_paths,  # ✅ FIX: Pass all unchanged files!
                collection_name=collection_name,
                progress_callback=progress_callback,
                vector_thread_count=vector_thread_count,
                fts_manager=fts_manager,  # type: ignore[name-defined]  # noqa: F821 (lazy-loaded FTS manager)
            )

            # Convert BranchIndexingResult to ProcessingStats
            stats = self._analysis_failure_stats(analysis_failed_paths)
            stats.files_processed = branch_result.files_processed
            stats.chunks_created = branch_result.content_points_created
            stats.start_time = time.time() - branch_result.processing_time
            stats.end_time = time.time()
            stats.cancelled = branch_result.cancelled

        except Exception as e:
            logger.error(
                f"BranchAwareIndexer failed during reconcile in git project: {e}"
            )
            if isinstance(e, ChunkStoreUnavailableError):
                # Bug #1746 Change 3 (extended): propagate unwrapped so
                # finally aborts instead of finalizing.
                fatal_chunk_store_error = e
                raise
            # NO FALLBACK - fail fast in git projects
            raise RuntimeError(
                f"Git-aware reconcile failed and fallbacks are disabled. "
                f"Original error: {e}"
            ) from e
        finally:
            # CRITICAL: Always finalize (or abort) the session, even on
            # exception (Bug #1746 Change 3).
            self._finalize_or_abort_indexing_session(
                collection_name,
                fatal_chunk_store_error,
                progress_callback,
                log_prefix="Index",
            )

        # Update metadata with actual processing results
        if progress_callback:
            progress_callback(
                0, 0, Path(""), info="Updating reconcile progress metadata..."
            )
        self.progressive_metadata.update_progress(
            files_processed=stats.files_processed,
            chunks_added=stats.chunks_created,
            failed_files=stats.failed_files,
        )

        # CRITICAL FIX: After reconcile indexing is complete, clean up multiple visible content points
        # This fixes the git restore scenario where both working_dir and committed content are visible
        if self.is_git_aware():
            collection_name = self.vector_store_client.resolve_collection_name(
                self.config, self.embedding_provider
            )
            self._cleanup_multiple_visible_content_points(
                collection_name, current_branch, progress_callback
            )

        # Mark as completed only if not cancelled
        if not stats.cancelled:
            self._record_failures_of_run(stats, [], analysis_failed_paths)
            self.progressive_metadata.complete_indexing()
            self.progress_log.complete_session()
        else:
            logger.info(
                "Indexing was cancelled, not marking as completed for resume capability"
            )
            self.progress_log.mark_session_cancelled()

        # Bug #1969 Round 5 (R4-F1/R4-F3): clear the durable sidecar
        # entries this reconcile run consulted.
        self._clear_self_heal_reprocess_paths_if_safe(
            collection_name, pending_self_heal_reconcile, stats
        )

        stats.index_entries_changed += visibility_changes
        return stats

    @staticmethod
    def _reanchor_resume_path(stored_path: str, codebase_dir: Path) -> Path:
        """Re-anchor a stored resume path onto the CURRENT walk root.

        Resume reads file paths persisted by a PRIOR run
        (``ProgressiveMetadata.set_files_to_index`` stores ``str(path)``).
        Those strings are absolute paths carrying whatever filesystem prefix
        the prior run accessed the repo under. On the cow-daemon backend the
        same logical directory is reachable under TWO different absolute
        prefixes -- the NFS *mount* path (e.g.
        ``/mnt/cow-storage/golden-repos/<repo>``) and the daemon-LOCAL path
        (e.g. ``/home/opuser/cow-storage/golden-repos/<repo>``). If the
        prior run stored the mount prefix but the current run's
        ``codebase_dir`` is the daemon-local prefix (or vice versa), using the
        stored path verbatim makes ``file_path.relative_to(codebase_dir)``
        raise ``ValueError: ... is not in the subpath of ...`` -> the staging
        "Hash calculation failed" / "Git-aware resume failed" loop.

        Re-anchoring rules (path-domain agnostic, walk-root authoritative):
        - A RELATIVE stored path joins onto ``codebase_dir`` (legacy behavior).
        - An ABSOLUTE stored path already under ``codebase_dir`` is returned
          unchanged -- the normal / local-backend case stays byte-identical.
        - An ABSOLUTE stored path with a stale prefix is re-anchored by
          locating the ``codebase_dir`` leaf name within the stored path's
          parts and joining the trailing subpath onto ``codebase_dir``.
        - If no leaf match is found the stored path is returned UNCHANGED so
          the downstream ``.exists()`` filter (or a genuine error) still
          surfaces -- we never fabricate a wrong mapping (anti-silent-failure).

        Args:
            stored_path: Path string as persisted by a prior run.
            codebase_dir: The current walk root (``config.codebase_dir``).

        Returns:
            A Path re-anchored under ``codebase_dir`` when possible.
        """
        candidate = Path(stored_path)

        # Relative stored path -> join onto the current walk root (legacy).
        if not candidate.is_absolute():
            return codebase_dir / candidate

        # Already under the current walk root -> no change (normal/local case).
        try:
            candidate.relative_to(codebase_dir)
            return candidate
        except ValueError:
            pass

        # Stale-prefix case: re-anchor by matching the codebase leaf name.
        # Scan FORWARD so that when the leaf appears more than once (bug #1087,
        # e.g. golden-repos/fastapi/tests/fastapi/conftest.py) we try each
        # candidate in order and return the first one that exists on disk.
        # When no candidate exists (e.g. synthetic paths in tests, or the file
        # was genuinely deleted) we fall back to the FIRST match found, which
        # preserves the single-occurrence behavior that callers relied on before.
        leaf = codebase_dir.name
        parts = candidate.parts
        first_match: Optional[Path] = None
        for index in range(len(parts)):
            if parts[index] == leaf:
                tail = parts[index + 1 :]
                anchored = codebase_dir.joinpath(*tail) if tail else codebase_dir
                if anchored.exists():
                    return anchored
                if first_match is None:
                    first_match = anchored

        # Return the first (shallowest) match if any were found; otherwise
        # return unchanged so downstream .exists()/.relative_to can surface it.
        return first_match if first_match is not None else candidate

    def _enable_server_resume_seal(self) -> bool:
        """Server-context runs: seal this run's resume-state saves with the
        server-held key, and report whether the stored state was sealed by
        a previous server-context run. False whenever the key is unavailable,
        so unsealed state is never trusted. Untrusted state also loses its
        stored file lists (work list, completed and failed files), so none of
        them is acted on or carried into this run's sealed saves."""
        key = load_or_create_resume_seal_key()
        if key is not None:
            self.progressive_metadata.enable_resume_seal(key)
        trusted = key is not None and self.progressive_metadata.loaded_state_is_sealed()
        if not trusted:
            self.progressive_metadata.discard_file_tracking()
        return trusted

    def _merge_recorded_failures(
        self, files: List[Path]
    ) -> Tuple[List[Path], List[Path]]:
        """Merge the files the previous run recorded as failed into ``files``.

        Each recorded path must still exist and pass the same containment
        and eligibility checks as a resumed path; any other recorded path
        drops out of the stored list. Returns ``(retried, merged_files)``.
        """
        recorded = [
            str(p)
            for p in self.progressive_metadata.metadata.get("failed_file_paths", [])
        ]
        if not recorded:
            return [], files
        codebase = Path(self.config.codebase_dir)
        resolved_codebase = codebase.resolve()
        retried: List[Path] = []
        for stored in recorded:
            candidate = self._reanchor_resume_path(stored, codebase)
            if candidate.exists() and self._resume_candidate_is_safe(
                candidate, resolved_codebase
            ):
                retried.append(candidate)
        if len(retried) != len(recorded):
            self.progressive_metadata.set_failed_file_paths([str(p) for p in retried])
        covered = {str(f) for f in files}
        merged = list(files) + [p for p in retried if str(p) not in covered]
        if retried:
            logger.info(
                "Retrying %d file(s) that the previous indexing run could not index.",
                len(retried),
            )
        return retried, merged

    def _relative_to_codebase(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.config.codebase_dir))
        except ValueError:
            return str(path)

    def _analysis_failure_stats(self, failed_paths: List[str]) -> ProcessingStats:
        """Stats reporting the files whose reconcile analysis failed, so the
        caller (and the CLI exit code) sees them."""
        return ProcessingStats(
            failed_files=len(failed_paths),
            failed_paths=frozenset(
                self._relative_to_codebase(Path(p)) for p in failed_paths
            ),
        )

    def _record_failures_of_run(
        self,
        stats: ProcessingStats,
        retried: List[Path],
        extra_failed: List[str],
    ) -> None:
        """Replace the recorded failure list with every attributable failure
        of this run: ``extra_failed`` plus every path in
        ``stats.failed_paths`` (first-time candidates and retries alike).
        When some failure has no attributed path, every retried file also
        stays recorded."""
        codebase = Path(self.config.codebase_dir)
        failed = list(extra_failed) + [
            str(codebase / rel) for rel in sorted(stats.failed_paths)
        ]
        if stats.failed_files > len(stats.failed_paths):
            failed += [str(p) for p in retried]
        self.progressive_metadata.set_failed_file_paths(failed)
        if failed:
            logger.warning(
                "%d file(s) could not be indexed in this run; they are recorded "
                "and retried on the next run.",
                len(failed),
            )

    def _resume_candidate_is_safe(
        self, candidate: Path, resolved_codebase: Path
    ) -> bool:
        """Reject a resume-path candidate unless it resolves
        (collapsing '..', following symlinks) strictly inside
        codebase_dir, and passes the SAME eligibility decision a fresh
        FileFinder walk would apply. Fails closed (returns False) on any
        resolve error.

        Containment is checked on the RESOLVED candidate against the
        RESOLVED codebase root -- this is what actually proves the
        candidate cannot escape codebase_dir via '..' or a symlink.
        Eligibility (size/extension/exclude/text-file checks), however, is
        checked on the candidate reconstructed in codebase_dir's OWN,
        possibly-unresolved form: ``FileFinder.is_eligible()`` computes
        ``file_path.relative_to(self.config.codebase_dir)`` internally
        against the CONFIGURED (unresolved) codebase_dir, which
        ``ConfigManager.load()`` deliberately leaves unresolved when it is
        itself a symlink (Bug #1087's mount-path case). Feeding it the
        fully-resolved candidate instead would raise ValueError there for
        every legitimate in-tree file whenever codebase_dir is a symlink,
        rejecting them all rather than just the ones that actually escape.

        Uses FileFinder's own eligibility method (the exact one
        ``find_files()`` itself calls) rather than the git-diff based
        ``_should_index_file()`` filter, which never applies FileFinder's
        max-file-size gate or its extension-gated text-file check.

        Also requires the candidate's own, unresolved link NAME to be
        eligible -- a symlink whose resolved TARGET is a perfectly
        eligible, in-tree file can still have a NAME that a fresh
        ``find_files()`` walk would never reach at all (inside an excluded
        directory such as ``node_modules/``, or matching a ``.gitignore``
        pattern), since directory pruning and pattern exclusion happen by
        NAME, before ``find_files()`` ever resolves a symlink's target.
        """
        # resolve_if_within_root is the ONE shared implementation of the
        # resolve+containment decision -- it both performs the check and
        # hands back the resolved path, so there is no second,
        # independent resolve() call here.
        resolved_candidate = resolve_if_within_root(candidate, resolved_codebase)
        if resolved_candidate is None:
            # Local CLI context only: a fresh walk includes an in-tree
            # symlink whose target lies outside the root (judged by its link
            # name below), but never descends into a symlinked directory.
            # Server context never follows a symlink out of the root.
            if self.config.confined_to_codebase_root or not (
                candidate.is_symlink()
                and resolve_if_within_root(candidate.parent, resolved_codebase)
                is not None
            ):
                return False
        else:
            relative_path = resolved_candidate.relative_to(resolved_codebase)

            eligibility_candidate = Path(self.config.codebase_dir) / relative_path
            if not self.file_finder.is_eligible(eligibility_candidate):
                return False

        try:
            link_relative_path = candidate.relative_to(Path(self.config.codebase_dir))
        except ValueError:
            return False

        normalized_link_relative = os.path.normpath(str(link_relative_path))
        if normalized_link_relative == os.curdir or normalized_link_relative.startswith(
            ".."
        ):
            return False

        link_eligibility_candidate = (
            Path(self.config.codebase_dir) / normalized_link_relative
        )
        return bool(self.file_finder.is_eligible(link_eligibility_candidate))

    def _do_resume_interrupted(
        self,
        batch_size: int,
        progress_callback: Optional[Callable],
        git_status: Dict[str, Any],
        provider_name: str,
        model_name: str,
        quiet: bool = False,
        vector_thread_count: Optional[int] = None,
        fts_manager: Optional[TantivyIndexManager] = None,
    ) -> ProcessingStats:
        """Resume a previously interrupted indexing operation."""

        # Bug #1862 follow-up: resuming a "failed" run (Bug #467's
        # can_resume_interrupted_operation() explicitly accepts it) is one
        # of the run transitions covered by the error_message invariant
        # stated on ProgressiveMetadata.start_indexing() -- neither
        # start_indexing() nor complete_indexing() run on this path below,
        # so drop the PRIOR run's error here now, or it sits next to this
        # run's fresh progress counters indefinitely.
        self.progressive_metadata.resume_indexing()

        # Ensure provider-aware collection exists for resuming
        self.vector_store_client.ensure_provider_aware_collection(
            self.config, self.embedding_provider, quiet
        )

        # Bug #1969 Round 5 (R4-F1): resolve collection_name EARLY (before
        # either early-return below) so a durably-pending self-heal
        # reprocess path can be folded in regardless of whether there is
        # any OTHER remaining/resumable work -- the reviewer flagged
        # resuming an interrupted run as the MOST likely real-world
        # trigger for this corruption in the first place (an interrupted
        # run is itself a primary way id_index.bin gets corrupted).
        _self_heal_collection_name = self.vector_store_client.resolve_collection_name(
            self.config, self.embedding_provider
        )

        # Get remaining files from metadata
        remaining_file_strings = self.progressive_metadata.get_remaining_files()

        # Convert strings back to Path objects, re-anchoring each onto the
        # CURRENT walk root. Stored paths come from a PRIOR run and may carry a
        # stale filesystem prefix (cow-daemon mount-vs-daemon-local split);
        # using them verbatim makes the downstream
        # file_path.relative_to(config.codebase_dir) hash step raise
        # "not in the subpath" -> "Hash calculation failed" -> resume failure.
        codebase = Path(self.config.codebase_dir)
        remaining_files = [
            self._reanchor_resume_path(f, codebase) for f in remaining_file_strings
        ]

        # Reject any re-anchored candidate that does
        # not resolve inside codebase_dir or fails FileFinder's own
        # eligibility filters, before the existence check below. Rejected
        # entries are dropped (resume continues with the rest); only the
        # count is logged, never the path/content.
        resolved_codebase = codebase.resolve()
        safe_files = []
        rejected_count = 0
        for candidate in remaining_files:
            if self._resume_candidate_is_safe(candidate, resolved_codebase):
                safe_files.append(candidate)
            else:
                rejected_count += 1
        if rejected_count:
            logger.warning(
                "Resume metadata contained %d file path candidate(s) that "
                "are outside the codebase root or ineligible for indexing; "
                "dropping them and continuing the resume with the "
                "remaining files.",
                rejected_count,
            )

        # Filter out files that no longer exist
        existing_files = [f for f in safe_files if f.exists()]

        # Bug #1969 Round 5 (R4-F1): fold in any durably pending self-heal
        # reprocess paths BEFORE the "nothing to do" check below -- a
        # resume with zero genuinely-remaining files must still pick up a
        # file a self-heal wiped (during THIS or a prior interrupted run).
        pending_self_heal_before, existing_files = (
            self._fold_in_pending_self_heal_paths(
                _self_heal_collection_name, existing_files
            )
        )
        retried_failures, existing_files = self._merge_recorded_failures(existing_files)

        if (
            not remaining_file_strings
            and not pending_self_heal_before
            and not retried_failures
        ):
            # No files left to process, and nothing durably pending either.
            self.progressive_metadata.complete_indexing()
            self.progress_log.complete_session()
            return ProcessingStats()

        if not existing_files:
            # All remaining files have been deleted (and nothing self-heal
            # -pending exists on disk to reprocess either) -- still safe
            # to clear any pending entries for those since-deleted paths
            # (nothing left to reprocess for a file that no longer
            # exists), so they don't stay durably stuck forever.
            self._clear_self_heal_reprocess_paths_if_safe(
                _self_heal_collection_name, pending_self_heal_before, ProcessingStats()
            )
            self.progressive_metadata.complete_indexing()
            self.progress_log.complete_session()
            return ProcessingStats()

        # Show what we're resuming with detailed feedback
        if progress_callback:
            metadata_stats = self.progressive_metadata.get_stats()
            completed = metadata_stats.get("files_processed", 0)
            total = metadata_stats.get("total_files_to_index", 0)
            chunks_so_far = metadata_stats.get("chunks_indexed", 0)

            progress_callback(
                0,
                0,
                Path(""),
                info=f"🔄 Resuming interrupted operation: {completed}/{total} files completed ({chunks_so_far} chunks), {len(existing_files)} files remaining",
            )

        # Get collection name before begin_indexing
        collection_name = self.vector_store_client.resolve_collection_name(
            self.config, self.embedding_provider
        )

        # BEGIN INDEXING SESSION (O(n) optimization - defer index rebuilding)
        self.vector_store_client.begin_indexing(collection_name)

        # Use HighThroughputProcessor directly for git-aware processing (STORY 3 MIGRATION)
        fatal_chunk_store_error: Optional[BaseException] = None
        try:
            # Use direct high-throughput parallel processing for resume (4-8x faster)
            # STORY 3: Use process_files_high_throughput() directly instead of branch wrapper

            # Use config.json setting directly
            if vector_thread_count is None:
                resolved_thread_count = self.config.voyage_ai.parallel_requests
            else:
                resolved_thread_count = vector_thread_count

            high_throughput_stats = self.process_files_high_throughput(
                files=existing_files,  # Use absolute paths directly
                vector_thread_count=resolved_thread_count,
                batch_size=50,
                progress_callback=progress_callback,
                fts_manager=fts_manager,  # type: ignore[name-defined]
            )

            # Bug #1969 Round 5 (R4-F1): the reviewer's flagged MOST likely
            # real-world trigger -- a self-heal escalation firing inside
            # upsert_points() during THIS resume, wiping a DIFFERENT
            # file's chunks. Consult the durable sidecar again after the
            # primary pass to catch it.
            (
                high_throughput_stats,
                pending_self_heal_after,
            ) = self._reprocess_newly_pending_self_heal_paths(
                _self_heal_collection_name,
                existing_files,
                high_throughput_stats,
                resolved_thread_count,
                progress_callback,
                fts_manager,
            )

            # Use ProcessingStats directly from high-throughput processor
            stats = high_throughput_stats

        except Exception as e:
            logger.error(
                f"HighThroughputProcessor failed during resume in git project: {e}"
            )
            if isinstance(e, ChunkStoreUnavailableError):
                # Bug #1746 Change 3: propagate unwrapped so finally aborts
                # instead of finalizing.
                fatal_chunk_store_error = e
                raise
            # NO FALLBACK - fail fast in git projects
            raise RuntimeError(
                f"Git-aware resume failed and fallbacks are disabled. "
                f"Original error: {e}"
            ) from e
        finally:
            # CRITICAL: Always finalize (or abort) the session, even on
            # exception (Bug #1746 Change 3).
            self._finalize_or_abort_indexing_session(
                collection_name,
                fatal_chunk_store_error,
                progress_callback,
                log_prefix="Index",
            )

        # Update metadata with actual processing results
        if progress_callback:
            progress_callback(
                0, 0, Path(""), info="Updating resume progress metadata..."
            )
        self.progressive_metadata.update_progress(
            files_processed=stats.files_processed,
            chunks_added=stats.chunks_created,
            failed_files=stats.failed_files,
        )

        # Mark as completed only if not cancelled
        if not stats.cancelled:
            if progress_callback:
                progress_callback(0, 0, Path(""), info="Finalizing resume session...")
            self._record_failures_of_run(stats, retried_failures, [])
            self.progressive_metadata.complete_indexing()
            self.progress_log.complete_session()
        else:
            logger.info(
                "Indexing was cancelled, not marking as completed for resume capability"
            )
            self.progress_log.mark_session_cancelled()

        # Bug #1969 Round 5 (R4-F1/R4-F3): clear the durable sidecar
        # entries this resume consulted -- only if not cancelled.
        self._clear_self_heal_reprocess_paths_if_safe(
            _self_heal_collection_name,
            pending_self_heal_before | pending_self_heal_after,
            stats,
        )

        return stats

    def _process_files_with_metadata(
        self,
        files: List[Path],
        batch_size: int,
        progress_callback: Optional[Callable],
        resumable: bool = False,
        vector_thread_count: Optional[int] = None,
    ) -> ProcessingStats:
        """Process files with progressive metadata updates and throughput monitoring.

        Bug #1746 Change 3 (extended, code review finding B2): this method
        is confirmed DEAD/ORPHAN code -- zero production call sites
        (verified by exhaustive grep across src/), exercised only by a
        unit test that invokes it directly. It calls
        process_files_high_throughput() below with NO surrounding
        try/except and never calls begin_indexing()/end_indexing()/
        abort_indexing() itself -- unlike every real session-managing
        entry point (_do_full_index, _do_incremental_index,
        _do_resume_interrupted, _do_reconcile_with_database,
        process_files_incrementally), which all own an indexing session
        lifecycle and were fixed to abort instead of finalize on a fatal
        ChunkStoreUnavailableError. Because this method never begins a
        session, there is no incorrect finalize to prevent here: a fatal
        error already propagates uncaught to whatever future caller
        invokes it. If this method is ever wired into a real call site,
        that caller becomes responsible for the same abort-vs-finalize
        decision the other five entry points make (see
        _finalize_or_abort_indexing_session()).
        """

        stats = ProcessingStats()
        stats.start_time = time.time()

        def update_metadata(file_path: Path, chunks_count=0, failed=False):
            """Update metadata after each file."""
            if resumable:
                # Use resumable tracking
                if failed:
                    self.progressive_metadata.mark_file_failed(str(file_path))
                else:
                    self.progressive_metadata.mark_file_completed(
                        str(file_path), chunks_count
                    )
            else:
                # Use legacy tracking
                self.progressive_metadata.update_progress(
                    files_processed=1,
                    chunks_added=chunks_count,
                    failed_files=1 if failed else 0,
                )

        # CRITICAL: Delete old chunks for files being re-indexed during reconcile
        # This prevents old content from remaining in the database when files are modified
        collection_name = self.vector_store_client.resolve_collection_name(
            self.config, self.embedding_provider
        )

        if progress_callback:
            progress_callback(
                0,
                0,
                Path(""),
                info=f"🧹 Cleaning old chunks for {len(files)} files before re-indexing...",
            )

        files_cleaned = 0
        for file_path in files:
            # Convert to relative path for database lookup
            try:
                if file_path.is_absolute():
                    relative_path = str(file_path.relative_to(self.config.codebase_dir))
                else:
                    relative_path = str(file_path)
            except ValueError:
                # File outside codebase_dir, use as-is
                relative_path = str(file_path)

            # Delete all existing chunks for this file
            # Bug #1575 follow-up: was previously called as
            # (filter_dict, collection_name) -- the WRONG order against the
            # real FilesystemVectorStore.delete_by_filter(self,
            # collection_name, filter_conditions) signature. The swapped
            # call raised TypeError inside scroll_points, caught by
            # delete_by_filter's own broad except-Exception and silently
            # turned into `return False` -- this cleanup had never actually
            # deleted anything. Fixed to the correct order.
            success = self.vector_store_client.delete_by_filter(
                collection_name,
                {"must": [{"key": "path", "match": {"value": relative_path}}]},
            )
            if success:
                files_cleaned += 1
                logger.debug(f"Deleted old chunks for file: {relative_path}")

        if progress_callback:
            progress_callback(
                0,
                0,
                Path(""),
                info=f"✅ Cleaned old chunks for {files_cleaned}/{len(files)} files",
            )

        # Use queue-based high-throughput processing for all code paths
        # Use config.json setting directly
        if vector_thread_count is None:
            vector_thread_count = self.config.voyage_ai.parallel_requests
            logger.info(
                f"Using vector thread count: {vector_thread_count} (from config.json)"
            )

        # Process all files using queue-based high-throughput approach
        high_throughput_stats = self.process_files_high_throughput(
            files,
            vector_thread_count=vector_thread_count,
            batch_size=batch_size,
            progress_callback=progress_callback,
            fts_manager=fts_manager,  # type: ignore[name-defined]  # noqa: F821 (lazy-loaded FTS manager)
        )

        # Update metadata for all files based on success/failure
        successful_files = high_throughput_stats.files_processed

        # For successful files, estimate chunks per file
        chunks_per_file = (
            high_throughput_stats.chunks_created // successful_files
            if successful_files > 0
            else 0
        )

        for i, file_path in enumerate(files):
            if i < successful_files:
                # File was processed successfully
                update_metadata(file_path, chunks_count=chunks_per_file, failed=False)
                stats.total_size += file_path.stat().st_size
            else:
                # File was not processed successfully
                update_metadata(file_path, chunks_count=0, failed=True)

        # Convert high-throughput stats to processing stats format
        stats.files_processed = high_throughput_stats.files_processed
        stats.chunks_created = high_throughput_stats.chunks_created
        stats.failed_files = high_throughput_stats.failed_files
        stats.cancelled = high_throughput_stats.cancelled

        stats.end_time = time.time()
        return stats

    def get_indexing_status(self) -> Dict[str, Any]:
        """Get current indexing status and statistics."""
        return self.progressive_metadata.get_stats()

    def can_resume(self) -> bool:
        """Check if indexing can be resumed."""
        # Check both interrupted operations and general incremental resume capability
        stats = self.progressive_metadata.get_stats()
        can_resume_incremental = stats.get("can_resume", False)
        can_resume_interrupted = (
            self.progressive_metadata.can_resume_interrupted_operation()
        )

        return can_resume_incremental or can_resume_interrupted

    def clear_progress(self):
        """Clear progress metadata (for fresh start)."""
        self.progressive_metadata.clear()

    def cleanup_branch_data(self, branch: str) -> Dict[str, int]:
        """
        Clean up branch data by hiding content points that don't exist in the branch.

        Returns a dictionary with cleanup statistics.
        """
        try:
            collection_name = self.vector_store_client.resolve_collection_name(
                self.config, self.embedding_provider
            )

            # Get all content points for this branch
            content_points, _ = self.vector_store_client.scroll_points(
                collection_name=collection_name,
                filter_conditions={
                    "must": [{"key": "visible_branches", "match": {"value": branch}}]
                },
                limit=10000,  # Process in batches if needed
                self_heal=True,
            )

            content_points_hidden = 0
            content_points_preserved = 0

            # Get current files in branch from git
            try:
                result = subprocess.run(
                    ["git", "ls-tree", "-r", "--name-only", branch],
                    cwd=self.config.root_path,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                current_files_in_branch = set(result.stdout.strip().split("\n"))
            except subprocess.CalledProcessError:
                logger.warning(
                    f"Could not get files for branch {branch}, skipping cleanup"
                )
                return {"content_points_hidden": 0, "content_points_preserved": 0}

            # Check each content point
            for point in content_points:
                file_path = point.get("payload", {}).get("file_path", "")
                if file_path and file_path not in current_files_in_branch:
                    # Hide this file from the branch
                    self._hide_file_in_branch_thread_safe(
                        file_path, branch, collection_name
                    )
                    content_points_hidden += 1
                else:
                    content_points_preserved += 1

            cleanup_result = {
                "content_points_hidden": content_points_hidden,
                "content_points_preserved": content_points_preserved,
            }

            logger.info(
                f"Branch cleanup completed for {branch}: "
                f"{cleanup_result['content_points_hidden']} content points hidden, "
                f"{cleanup_result['content_points_preserved']} preserved"
            )

            return cleanup_result

        except Exception as e:
            logger.error(f"Failed to cleanup branch {branch}: {e}")
            return {"content_points_hidden": 0, "content_points_preserved": 0}

    def process_files_incrementally(
        self,
        file_paths: List[str],
        force_reprocess: bool = False,
        quiet: bool = False,
        vector_thread_count: Optional[int] = None,
        watch_mode: bool = False,
    ) -> ProcessingStats:
        """Process specific files incrementally using git-aware indexing.

        Args:
            file_paths: List of relative file paths to process
            force_reprocess: Force reprocessing even if files seem up to date
            quiet: Suppress progress output
            vector_thread_count: Number of threads for vector calculation
            watch_mode: If True, use verified deletion for reliability

        Returns:
            ProcessingStats with processing results
        """
        # Initialize FTS manager to None (FTS not supported in incremental processing)
        fts_manager: Optional[TantivyIndexManager] = None

        stats = ProcessingStats()
        stats.start_time = time.time()

        try:
            # Convert relative paths to absolute paths
            absolute_paths = []
            for file_path in file_paths:
                abs_path = self.config.codebase_dir / file_path
                if abs_path.exists():
                    absolute_paths.append(abs_path)
                else:
                    # File was deleted - handle cleanup using branch-aware strategy
                    logger.info(
                        f"🗑️  WATCH MODE: Processing deletion of {file_path} (watch_mode={watch_mode})"
                    )
                    collection_name = self.vector_store_client.resolve_collection_name(
                        self.config, self.embedding_provider
                    )
                    success = self.delete_file_branch_aware(
                        file_path, collection_name, watch_mode
                    )
                    if not success and watch_mode:
                        logger.error(
                            f"Watch mode deletion verification failed for {file_path}"
                        )
                        # Continue processing other files even if one deletion fails

            # NOTE: absolute_paths is re-joined onto codebase_dir and
            # re-filtered for containment inside
            # process_branch_changes_high_throughput() below (the single
            # choke point shared by every relative-path-list caller) --
            # no separate check is needed here.

            if not absolute_paths:
                stats.end_time = time.time()
                return stats

            # Convert to relative paths for indexer
            relative_files = []
            for abs_path in absolute_paths:
                try:
                    relative_files.append(
                        str(abs_path.relative_to(self.config.codebase_dir))
                    )
                except ValueError:
                    continue

            if absolute_paths:
                # Use BranchAwareIndexer for git-aware processing with parallel embeddings (SINGLE PROCESSING PATH)
                collection_name = None
                # Bug #1746 Change 3 (extended): set when the fatal
                # ChunkStoreUnavailableError propagates from
                # process_branch_changes_high_throughput() below, so the
                # inner finally aborts instead of finalizing, and the
                # outer except (further below) re-raises instead of
                # silently swallowing it into stats.failed_files.
                fatal_chunk_store_error: Optional[BaseException] = None
                try:
                    # Get current branch for indexing
                    current_branch = (
                        self.git_topology_service.get_current_branch() or "master"
                    )

                    # Ensure collection exists
                    collection_name = self.vector_store_client.resolve_collection_name(
                        self.config, self.embedding_provider
                    )

                    # Use high-throughput parallel processing for incremental files (4-8x faster)
                    # Bug #1575 Part C Defect 1 (dual-review corroborated):
                    # defer_finalization=True -- this method (not
                    # process_branch_changes_high_throughput itself) applies
                    # branch isolation below via
                    # hide_files_not_in_branch_thread_safe(), which is what
                    # registers the branch-visibility context
                    # end_indexing()'s decision engine needs. Finalizing
                    # INSIDE process_branch_changes_high_throughput's own
                    # finally block (the pre-fix behavior) closed the
                    # indexing session BEFORE that context existed,
                    # orphaning it -- exactly the ghost-vector regression
                    # this fix closes. The SAME finalization pass that
                    # closes this session now happens in THIS method's own
                    # finally block below, AFTER
                    # hide_files_not_in_branch_thread_safe runs.
                    branch_result = self.process_branch_changes_high_throughput(
                        old_branch="",  # No old branch for process files incrementally
                        new_branch=current_branch,
                        changed_files=relative_files,
                        unchanged_files=[],
                        collection_name=collection_name,
                        progress_callback=None,  # No progress callback for incremental processing
                        vector_thread_count=vector_thread_count,
                        watch_mode=watch_mode,  # Pass through watch_mode
                        fts_manager=fts_manager,  # type: ignore[name-defined]  # noqa: F821 (lazy-loaded FTS manager)
                        skip_branch_isolation=True,  # Branch isolation handled separately below
                        defer_finalization=True,  # Bug #1575 Part C Defect 1
                    )

                    # For incremental file processing, also ensure branch isolation
                    # IMPORTANT: Use ALL files in current branch, not just the ones being processed
                    all_files_in_branch = list(self.file_finder.find_files())
                    all_relative_files = []
                    for f in all_files_in_branch:
                        try:
                            if f.is_absolute():
                                all_relative_files.append(
                                    str(f.relative_to(self.config.codebase_dir))
                                )
                            else:
                                all_relative_files.append(str(f))
                        except ValueError:
                            all_relative_files.append(str(f))

                    # Only apply branch isolation for git repositories
                    if self.git_topology_service.is_git_available():
                        self.hide_files_not_in_branch_thread_safe(
                            current_branch,
                            all_relative_files,
                            collection_name,
                            fts_manager=fts_manager,
                        )

                    # Convert BranchIndexingResult to ProcessingStats
                    stats.files_processed = branch_result.files_processed
                    stats.chunks_created = branch_result.content_points_created
                    stats.failed_files = 0

                except Exception as e:
                    logger.error(
                        f"BranchAwareIndexer failed during process_files_incrementally in git project: {e}"
                    )
                    if isinstance(e, ChunkStoreUnavailableError):
                        # Bug #1746 Change 3 (extended): propagate
                        # unwrapped so the inner finally aborts instead of
                        # finalizing, and the outer except (below) does
                        # NOT swallow this into stats.failed_files.
                        fatal_chunk_store_error = e
                        raise
                    # NO FALLBACK - fail fast in git projects
                    raise RuntimeError(
                        f"Git-aware incremental processing failed and fallbacks are disabled. "
                        f"Original error: {e}"
                    ) from e
                finally:
                    # Bug #1575 Part C Defect 1: finalize (end_indexing) the
                    # SAME indexing session process_branch_changes_high_throughput
                    # began (defer_finalization=True above), now that
                    # branch-isolation context has been established by
                    # hide_files_not_in_branch_thread_safe(). Guarded on
                    # collection_name being resolved -- if
                    # resolve_collection_name() itself raised,
                    # begin_indexing() was never called and there is
                    # nothing to finalize. Runs on the exception path too,
                    # matching the pre-fix "always finalize indexes, even
                    # on exception" contract. Bug #1746 Change 3 (extended):
                    # aborts instead when fatal_chunk_store_error is set.
                    if collection_name is not None:
                        self._finalize_indexing_session(
                            collection_name,
                            progress_callback=None,
                            watch_mode=watch_mode,
                            fatal_chunk_store_error=fatal_chunk_store_error,
                        )

                if not quiet:
                    logger.info(
                        f"Processed {stats.files_processed} files incrementally"
                    )

        except ChunkStoreUnavailableError:
            # Bug #1746 Change 3 (extended): never swallow a fatal
            # chunk-store failure into an ordinary failed_files count and
            # return normally -- that reproduces the exact silent-failure
            # shape #1746 exists to kill. Propagate to the caller.
            raise
        except Exception as e:
            logger.error(f"Incremental processing failed: {e}")
            stats.failed_files = len(file_paths)

        stats.end_time = time.time()
        return stats

    def is_git_aware(self) -> bool:
        """Determine if this is a git-aware project."""
        return (
            self.git_topology_service is not None
            and self.git_topology_service.get_current_branch() is not None
        )

    def _get_indexed_files_snapshot(
        self, collection_name: str, progress_callback: Optional[Callable] = None
    ) -> Dict[Path, float]:
        """Get all indexed files with timestamps using efficient snapshot approach.

        This method loads only the minimal required data (file paths + timestamps)
        without vectors or full content, preventing memory issues and infinite loops.

        Returns:
            Dict mapping file paths to their timestamps
        """
        indexed_files_with_timestamps: Dict[Path, float] = {}
        # Issue #1505: derived in the SAME bulk-scroll pass as the timestamp
        # snapshot above, so the main reconcile loop can look up each file's
        # DB-side content id via a plain in-memory dict lookup instead of a
        # per-file `scroll_points` query.
        db_content_ids: Dict[str, str] = {}
        # Codex #1505 review, Finding 1: derived in this SAME bulk-scroll
        # pass too, so the branch-visibility ("unhide") check in
        # `_do_reconcile_with_database` can look up each file's
        # `hidden_branches` via an in-memory dict lookup instead of issuing
        # a fresh `scroll_points` query PER indexed file.
        db_hidden_branches: Dict[str, List[str]] = {}
        db_hidden_on_all_points: Dict[str, Set[str]] = {}
        # Issue #2013: relative path -> (earliest indexed_timestamp, stored
        # file_hash) for mtime/size-identified points (racy-timestamp check).
        db_working_dir_index: Dict[str, Tuple[float, Optional[str]]] = {}

        if progress_callback:
            progress_callback(
                0, 0, Path(""), info="📸 Taking snapshot of indexed files..."
            )

        # Every content point in one pass. Any read error propagates: a
        # reconcile against an empty or partial snapshot would re-embed the
        # files it cannot see and record the store as verified.
        all_points = self._scroll_content_points_through_lock_contention(
            collection_name
        )

        if progress_callback:
            progress_callback(
                0,
                0,
                Path(""),
                info=f"📊 Processing {len(all_points)} points from database snapshot",
            )

        # Process all points in memory (no database access = no consistency issues)
        for point in all_points:
            payload = point.get("payload", {})

            if "path" not in payload:
                continue

            # Extract file path
            path_from_db = payload["path"]
            if Path(path_from_db).is_absolute():
                file_path = Path(path_from_db)
            else:
                file_path = self.config.codebase_dir / path_from_db

            # Extract best available timestamp
            timestamp = self._extract_best_timestamp(payload)

            # Keep the most recent timestamp per file (multiple chunks per file)
            if (
                file_path not in indexed_files_with_timestamps
                or timestamp > indexed_files_with_timestamps[file_path]
            ):
                indexed_files_with_timestamps[file_path] = timestamp

            # Issue #1505: derive this file's DB-side content id once,
            # first point encountered per relative path wins (mirrors
            # the pre-existing `limit=1` first-match semantics of the
            # per-file scroll query this replaces).
            try:
                relative_key = str(file_path.relative_to(self.config.codebase_dir))
            except ValueError:
                relative_key = str(path_from_db)
            if relative_key not in db_content_ids:
                db_content_ids[relative_key] = self._derive_db_content_id_from_point(
                    relative_key, point
                )

            # Codex #1505 review, Finding 1: capture hidden_branches for
            # this path once too, first point wins -- mirrors the
            # pre-existing `limit=1` first-match semantics of the
            # per-file scroll query this replaces.
            if relative_key not in db_hidden_branches:
                db_hidden_branches[relative_key] = payload.get("hidden_branches", [])
            # Issue #1999 review: branches hidden on EVERY point of the
            # path -- a path is visible on a branch while ANY point is.
            point_hidden = set(payload.get("hidden_branches") or [])
            if relative_key in db_hidden_on_all_points:
                db_hidden_on_all_points[relative_key] &= point_hidden
            else:
                db_hidden_on_all_points[relative_key] = point_hidden

            # Issue #2013 (racy timestamp): for an mtime/size-identified
            # point keep the file's EARLIEST content-read time
            # (`indexed_timestamp`) and its stored whole-file content
            # hash. A point without one counts as 0.0, so that file is
            # hash-checked on every reconcile.
            if "filesystem_mtime" in payload:
                indexed_ts = float(payload.get("indexed_timestamp") or 0.0)
                previous = db_working_dir_index.get(relative_key)
                if previous is None or indexed_ts < previous[0]:
                    db_working_dir_index[relative_key] = (
                        indexed_ts,
                        payload.get("file_hash"),
                    )

        if progress_callback:
            progress_callback(
                0,
                0,
                Path(""),
                info=f"✅ Snapshot complete: {len(indexed_files_with_timestamps)} unique files found",
            )

        self._reconcile_db_content_ids = db_content_ids
        self._reconcile_hidden_branches = db_hidden_branches
        self._reconcile_hidden_on_all_points = db_hidden_on_all_points
        self._reconcile_working_dir_index = db_working_dir_index
        return indexed_files_with_timestamps

    def _derive_db_content_id_from_point(
        self, relative_path: str, point: Dict[str, Any]
    ) -> str:
        """Derive the DB-side content id for a file from an already-fetched
        content point's payload, without any additional store query.

        The single DB-side derivation: working_dir (mtime/size-identified)
        points use `working_dir_content_id` (Issue #2013); committed
        points prefer the git blob hash (Issue #1505 -- precise,
        single-batch-derivable) and fall back to the legacy git commit hash
        for older data that predates the blob-hash field.

        Codex #1505 review, Finding 3: real point ids are deterministic
        UUID5 strings (see `git_aware_processor.py` /
        `high_throughput_processor.py`'s `_create_point_id`) and NEVER
        contain a "working_dir" substring -- a previous string-match check
        on the point id was dead code against real persisted data. The
        authoritative signal is the payload shape itself:
        `high_throughput_processor.py`'s `_create_vector_point` writes
        `filesystem_mtime`/`filesystem_size` payload fields ONLY for a
        non-git-available (mtime/size-identified) file, and never alongside
        `git_blob_hash`/`git_commit_hash`.
        """
        payload = point.get("payload", {})

        if "filesystem_mtime" in payload:
            return working_dir_content_id(
                relative_path,
                payload["filesystem_mtime"],
                payload.get("filesystem_size", 0),
            )

        blob_hash = payload.get("git_blob_hash")
        if blob_hash:
            return f"{relative_path}:blob:{blob_hash}"

        git_commit = payload.get("git_commit_hash", "unknown")
        return f"{relative_path}:{git_commit}"

    def _scroll_content_points_through_lock_contention(
        self, collection_name: str
    ) -> List[Dict[str, Any]]:
        """``_scroll_all_content_points``, retried while another connection
        holds the chunks.db lock (at most ``_SNAPSHOT_LOCK_MAX_ATTEMPTS``
        attempts, each waiting up to sqlite's busy timeout). Any other error,
        and contention outlasting the last attempt, is raised."""
        for attempt in range(1, _SNAPSHOT_LOCK_MAX_ATTEMPTS + 1):
            try:
                return self._scroll_all_content_points(collection_name)
            except Exception as exc:
                if (
                    not is_chunk_store_lock_contention(exc)
                    or attempt == _SNAPSHOT_LOCK_MAX_ATTEMPTS
                ):
                    raise
                logger.info(
                    "Index snapshot read of %r found chunks.db locked by another "
                    "writer (attempt %d of %d); retrying",
                    collection_name,
                    attempt,
                    _SNAPSHOT_LOCK_MAX_ATTEMPTS,
                )
        raise AssertionError("unreachable: the last attempt returns or raises")

    def _scroll_all_content_points(self, collection_name: str) -> List[Dict[str, Any]]:
        """Scroll through all content points and return them as a list.

        This method uses only content points (not metadata) and excludes vectors
        for maximum memory efficiency.
        """
        all_points = []
        offset = None

        while True:
            # Bug #1969 Round 6 (P1-1 caller audit): the sole caller of
            # this method is _get_indexed_files_snapshot ->
            # _do_reconcile_with_database -- self_heal=True so
            # `cidx index --reconcile` self-heals a corrupt-duplicate
            # collection instead of hard-failing on the exact corruption
            # this whole fix exists to resolve.
            points, next_offset = self.vector_store_client.scroll_points(
                collection_name=collection_name,
                filter_conditions={
                    "must": [{"key": "type", "match": {"value": "content"}}]
                },
                limit=5000,  # Larger batches for efficiency
                offset=offset,
                with_payload=True,
                with_vectors=False,  # CRITICAL: No vectors = massive memory savings
                self_heal=True,
            )

            if not points:
                break

            all_points.extend(points)

            # Bug #1971: the stuck-pagination safety check MUST compare
            # `next_offset` against the PREVIOUS `offset` BEFORE it is
            # reassigned -- comparing after assignment is always True
            # (self-comparison), which silently truncated every
            # multi-page scroll to page 1. Mirrors the already-correct
            # sibling loop in
            # HighThroughputProcessor._fetch_all_content_points.
            # A stuck cursor leaves the snapshot partial; reconciling against
            # it would re-embed the unseen files, so fail loudly.
            if next_offset is not None and next_offset == offset:
                raise RuntimeError(
                    f"Pagination stuck at offset {offset} while reading the "
                    f"content points of {collection_name!r}"
                )

            offset = next_offset
            if offset is None:
                # Normal completion - no more data
                break

        return all_points

    def _extract_best_timestamp(self, payload: Dict[str, Any]) -> float:
        """Extract the best available timestamp from payload."""
        # Priority 1: file_mtime from new architecture (most accurate)
        if "file_mtime" in payload:
            return float(payload["file_mtime"])
        # Priority 2: filesystem_mtime from legacy architecture
        elif "filesystem_mtime" in payload:
            return float(payload["filesystem_mtime"])
        # Priority 3: created_at (indexing time, less accurate for file changes)
        elif "created_at" in payload:
            return float(payload["created_at"])
        # Priority 4: indexed_at as last resort
        elif "indexed_at" in payload:
            try:
                dt = datetime.datetime.strptime(
                    payload["indexed_at"], "%Y-%m-%dT%H:%M:%SZ"
                )
                return dt.timestamp()
            except (ValueError, TypeError):
                return 0.0
        return 0.0

    def _cleanup_multiple_visible_content_points(
        self,
        collection_name: str,
        current_branch: str,
        progress_callback: Optional[Callable] = None,
    ) -> None:
        """
        Clean up situations where multiple content points are visible for the same file.

        This handles the git restore scenario where both working_dir and committed content
        are visible in the current branch, which should not happen.
        """
        try:
            # Get all content points visible in current branch
            all_points, _ = self.vector_store_client.scroll_points(
                filter_conditions={
                    "must": [
                        {"key": "type", "match": {"value": "content"}},
                    ],
                    "must_not": [
                        {"key": "hidden_branches", "match": {"any": [current_branch]}}
                    ],
                },
                limit=10000,  # Get all visible content points
                collection_name=collection_name,
                self_heal=True,
            )

            if not all_points:
                return

            # Group points by file path
            files_with_points: Dict[str, List[Dict[str, Any]]] = {}
            for point in all_points:
                payload = point.get("payload", {})
                file_path = payload.get("path", "")
                if file_path:
                    if file_path not in files_with_points:
                        files_with_points[file_path] = []
                    files_with_points[file_path].append(point)

            # Find files with multiple visible content points
            files_to_cleanup = []
            for file_path, points in files_with_points.items():
                if len(points) > 1:
                    files_to_cleanup.append((file_path, points))

            if not files_to_cleanup:
                logger.debug(
                    "No multiple visible content points found - cleanup not needed"
                )
                return

            logger.info(
                f"Found {len(files_to_cleanup)} files with multiple visible content points - cleaning up"
            )

            # Clean up each file with multiple content points
            hidden_count = 0
            for file_path, points in files_to_cleanup:
                # Separate working_dir and committed content
                working_dir_points = []
                committed_points = []

                for point in points:
                    payload = point.get("payload", {})
                    git_commit_hash = payload.get("git_commit_hash", "")

                    if git_commit_hash.startswith("working_dir_"):
                        working_dir_points.append(point)
                    else:
                        committed_points.append(point)

                # If we have both types, hide working_dir content and keep committed content
                if working_dir_points and committed_points:
                    logger.info(
                        f"Hiding {len(working_dir_points)} working_dir points for {file_path} (keeping {len(committed_points)} committed)"
                    )

                    # Hide working directory content points
                    points_to_update = []
                    for point in working_dir_points:
                        point_id = point["id"]
                        payload = point.get("payload", {})
                        hidden_branches = payload.get("hidden_branches", [])

                        if current_branch not in hidden_branches:
                            new_hidden = hidden_branches + [current_branch]
                            points_to_update.append(
                                {
                                    "id": point_id,
                                    "payload": {"hidden_branches": new_hidden},
                                }
                            )

                    # Apply the updates. Payload-only: _batch_update_points
                    # upserts point by point and each upsert drops the
                    # file's other chunk points as orphans.
                    if points_to_update:
                        success = self.vector_store_client._batch_update_payload_only(
                            points_to_update,
                            collection_name,
                        )
                        if success:
                            hidden_count += len(points_to_update)
                            logger.debug(
                                f"Successfully hid {len(points_to_update)} working_dir points for {file_path}"
                            )
                        else:
                            logger.warning(
                                f"Failed to hide working_dir points for {file_path}"
                            )

            if progress_callback and hidden_count > 0:
                progress_callback(
                    0,
                    0,
                    Path(""),
                    info=f"🧹 Hidden {hidden_count} obsolete working directory content points",
                )

        except Exception as e:
            logger.warning(f"Failed to cleanup multiple visible content points: {e}")

    def delete_file_branch_aware(
        self, file_path: str, collection_name: str, watch_mode: bool = False
    ) -> bool:
        """Delete file using appropriate strategy based on project type.

        Args:
            file_path: Relative path of file to delete
            collection_name: Filesystem collection name
            watch_mode: If True, use verification for reliable watch mode deletion

        Returns:
            True if deletion was successful, False otherwise
        """
        if self.is_git_aware():
            # Use branch-aware soft delete for git projects
            current_branch = self.git_topology_service.get_current_branch()
            if current_branch:
                # DEADLOCK FIX: Always use fast deletion without verification
                # Trust synchronous operations - verification was causing 5+ minute hangs
                self._hide_file_in_branch_thread_safe(
                    file_path, current_branch, collection_name
                )
                logger.info(f"Hidden file in branch '{current_branch}': {file_path}")
                return True
            else:
                logger.warning(
                    f"Could not determine current branch for file deletion: {file_path}"
                )
                return False
        else:
            # DEADLOCK FIX: Use hard delete without verification
            # Trust synchronous operations - verification was causing 5+ minute hangs
            # Bug #1575 follow-up: was previously called as
            # (filter_dict, collection_name) -- the WRONG order against the
            # real FilesystemVectorStore.delete_by_filter(self,
            # collection_name, filter_conditions) signature. The swapped
            # call raised TypeError inside scroll_points, caught by
            # delete_by_filter's own broad except-Exception and silently
            # turned into `return False` -- this cleanup had never actually
            # deleted anything. Fixed to the correct order.
            success = bool(
                self.vector_store_client.delete_by_filter(
                    collection_name,
                    {"must": [{"key": "path", "match": {"value": file_path}}]},
                )
            )
            if success:
                logger.info(f"Deleted vectors for removed file: {file_path}")
            else:
                logger.error(f"Failed to delete vectors for removed file: {file_path}")
            return success

    # DEADLOCK FIX: Removed _delete_file_hard_delete_with_verification method
    # The verification was causing 5+ minute hangs. Trust synchronous operations instead.

    def _detect_and_handle_deletions(
        self, progress_callback: Optional[Callable] = None
    ) -> None:
        """Detect and handle files that exist in database but were deleted from filesystem."""
        try:
            # Get collection name
            collection_name = self.vector_store_client.resolve_collection_name(
                self.config, self.embedding_provider
            )

            # Get all files that should be indexed (from disk)
            disk_files = list(self.file_finder.find_files())
            disk_files_set = {
                str(f.relative_to(self.config.codebase_dir)) for f in disk_files
            }

            # Get all files from database (simplified version of reconcile logic)
            indexed_files = set()
            offset = None

            # DEADLOCK FIX: Add pagination safety to prevent infinite loops
            max_iterations = 1000  # Safety limit: max 1M points (1000 * 1000 limit)
            iteration_count = 0

            while True:
                try:
                    # Bug #1969 Round 6 (P1-1 caller audit): this scan
                    # drives delete_file_branch_aware for genuinely-
                    # deleted files -- self_heal=True so a corrupt-
                    # duplicate collection is repaired here instead of
                    # this loop's own except-Exception handler silently
                    # masking the failure ("Database query failed during
                    # deletion detection") and leaving the corruption in
                    # place forever.
                    points, next_offset = self.vector_store_client.scroll_points(
                        filter_conditions={
                            "should": [
                                {"key": "type", "match": {"value": "content"}},
                                {"key": "type", "match": {"value": "visibility"}},
                            ]
                        },
                        limit=1000,
                        offset=offset,
                        with_payload=True,
                        with_vectors=False,
                        collection_name=collection_name,
                        self_heal=True,
                    )

                    for point in points:
                        if "path" in point["payload"]:
                            path_from_db = point["payload"]["path"]
                            # Normalize path - handle both relative and absolute paths
                            if Path(path_from_db).is_absolute():
                                file_path = Path(path_from_db)
                            else:
                                file_path = self.config.codebase_dir / path_from_db

                            # Convert to relative path string
                            try:
                                relative_path = str(
                                    file_path.relative_to(self.config.codebase_dir)
                                )
                                indexed_files.add(relative_path)
                            except ValueError:
                                # Path is outside codebase directory, skip
                                pass

                    offset = next_offset
                    iteration_count += 1

                    # DEADLOCK FIX: Check for infinite pagination loops
                    if iteration_count >= max_iterations:
                        logger.warning(
                            f"Pagination safety limit reached ({max_iterations} iterations). "
                            f"Breaking out of deletion detection loop."
                        )
                        if progress_callback:
                            progress_callback(
                                0,
                                0,
                                Path(""),
                                info=f"⚠️ Pagination limit reached, continuing with {len(indexed_files)} files found",
                            )
                        break

                    if offset is None:
                        break

                except Exception as e:
                    if progress_callback:
                        progress_callback(
                            0,
                            0,
                            Path(""),
                            info=f"Database query failed during deletion detection: {e}",
                        )
                    return

            # Find deleted files
            deleted_files = []
            for indexed_file in indexed_files:
                if indexed_file not in disk_files_set:
                    deleted_files.append(indexed_file)

            # Handle deleted files
            if deleted_files:
                # DEADLOCK FIX: Add progress feedback during deletion processing
                for i, deleted_file in enumerate(deleted_files):
                    if progress_callback:
                        progress_callback(
                            i + 1,
                            len(deleted_files),
                            Path(deleted_file),
                            info=f"🗑️ Cleaning up deleted files ({i + 1}/{len(deleted_files)}): {deleted_file}",
                        )

                    self.delete_file_branch_aware(
                        deleted_file, collection_name, watch_mode=False
                    )

                if progress_callback:
                    progress_callback(
                        0,
                        0,
                        Path(""),
                        info=f"🗑️  Detected and cleaned up {len(deleted_files)} deleted files",
                    )
                logger.info(
                    f"Deletion detection: cleaned up {len(deleted_files)} deleted files"
                )
            else:
                if progress_callback:
                    progress_callback(
                        0,
                        0,
                        Path(""),
                        info="🔍 Deletion detection: no deleted files found",
                    )
                logger.info("Deletion detection: no deleted files found")

        except Exception as e:
            logger.error(f"Deletion detection failed: {e}")
            if progress_callback:
                progress_callback(
                    0, 0, Path(""), info=f"Deletion detection failed: {e}"
                )

    def _working_dir_file_racily_modified(self, relative_path: str) -> bool:
        """Racy-timestamp rule (Issue #2013), as git applies to its index.

        An mtime/size-identified file whose int-second mtime and size match
        its stored id is trusted as unchanged ONLY when that mtime second is
        strictly older than the second the file's content was READ in
        (`indexed_timestamp`, recorded by `FileIdentifier.get_file_metadata`
        just before hashing). Otherwise a same-size rewrite within that
        second is indistinguishable by mtime/size, so the whole-file sha256
        stored in the payload (`file_hash`) is compared. Only such files pay
        a content read; points without an `indexed_timestamp` are
        hash-checked on every reconcile. The mtime must be more than
        `_RACY_MTIME_CLOCK_SKEW_MARGIN_SECONDS` older than the read second,
        since it comes from the filesystem's clock and the read time from
        this host's clock.

        Raises OSError when the file cannot be read for the check, so the
        caller records a failed analysis instead of treating it as changed.
        """
        entry = self._reconcile_working_dir_index.get(relative_path)
        if entry is None:
            return False
        earliest_read_ts, stored_hash = entry
        full_path = Path(self.config.codebase_dir) / relative_path
        trusted_before_second = (
            int(earliest_read_ts) - _RACY_MTIME_CLOCK_SKEW_MARGIN_SECONDS
        )
        if int(full_path.stat().st_mtime) < trusted_before_second:
            return False
        current_hash = self.file_identifier._get_file_content_hash(full_path)
        if current_hash.startswith(_CONTENT_HASH_READ_ERROR_PREFIX):
            raise OSError(
                f"could not read {relative_path} to verify its content "
                "(modified in the second it was read)"
            )
        return bool(current_hash != stored_hash)

    def _disk_working_dir_content_id(self, file_path: str) -> str:
        """Disk-side mtime/size content id for reconcile.

        Issue #2013: built by the SAME helper as the stored-payload side
        (`_derive_db_content_id_from_point`), with the integer mtime the
        indexer stores -- otherwise no file ever compares equal and every
        non-git reconcile deletes and re-embeds the whole repository. A file
        that cannot be stat'ed gets an id that never matches, so it is
        re-indexed (and the failure is logged).
        """
        try:
            file_stat = (Path(self.config.codebase_dir) / file_path).stat()
        except OSError as e:
            logger.warning(
                "Reconcile could not stat %s (%s); it will be re-indexed",
                file_path,
                e,
            )
            return f"{file_path}:working_dir_error"
        return working_dir_content_id(
            file_path, int(file_stat.st_mtime), file_stat.st_size
        )

    def _get_effective_content_id_for_reconcile(self, file_path: str) -> str:
        """Get content ID that represents current working directory state.

        This method replaces the BranchAwareIndexer equivalent for reconciliation.
        """
        # Check if this is a git repository
        is_git_repo = self.git_topology_service.is_git_available()

        # Non-git projects always use mtime/size content ids; in a git
        # repository so does a file with working directory changes.
        if not is_git_repo or self._file_differs_from_committed_version(file_path):
            return self._disk_working_dir_content_id(file_path)
        else:
            # File matches committed version - use blob-hash based ID.
            # Issue #1505: prefer the O(1) batched HEAD blob-hash lookup
            # (built once per reconcile run via `_get_head_blob_hash_map`)
            # over the per-file `git log -1 -- path` subprocess call. Only
            # fall back to the original per-file computation for the rare
            # file with no entry in the committed tree (e.g. untracked via
            # `git rm --cached` while left on disk) -- graceful degradation
            # for just that one file, never a silent skip.
            head_blob_hashes = getattr(self, "_reconcile_head_blob_hashes", {})
            blob_hash = head_blob_hashes.get(file_path)
            if blob_hash is not None:
                return f"{file_path}:blob:{blob_hash}"

            # Codex #1505 review, Finding 2: count every fallback so the
            # main reconcile loop can detect and loudly report a
            # degraded (map-failed/mostly-empty) run. `_reconcile_fallback_count`
            # is initialized to 0 in `_do_reconcile_with_database` alongside
            # `_reconcile_head_blob_hashes`; `getattr` is a defensive
            # fallback for any direct caller of this method in isolation
            # (e.g. unit tests) that never ran that init.
            self._reconcile_fallback_count = (
                getattr(self, "_reconcile_fallback_count", 0) + 1
            )
            commit = self._get_file_commit(file_path)
            return f"{file_path}:{commit}"

    def _get_head_blob_hash_map(self) -> Dict[str, str]:
        """Batch-fetch every tracked file's committed blob hash at HEAD.

        Issue #1505: a single `git ls-tree -r HEAD` invocation returns the
        blob hash for every path in the committed tree, replacing what used
        to be a `git log -1 -- path` subprocess spawned PER unchanged file.
        Uses `-z` (NUL-terminated records) so paths containing unusual
        characters (spaces, newlines) are parsed correctly.

        Returns:
            Dict mapping relative file path -> 40-character blob hash SHA.
            Empty dict on any failure (callers gracefully fall back to the
            original per-file `_get_file_commit` computation for any path
            missing from the map -- never silently skip a file).
        """
        try:
            result = subprocess.run(
                ["git", "ls-tree", "-r", "-z", "HEAD"],
                cwd=self.config.codebase_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                # Codex #1505 review, Finding 2: a silent `{}` here made
                # every committed file fall back to the per-file `git log`
                # path with NO indication why -- log LOUDLY (return code +
                # stderr) so this failure mode is diagnosable instead of
                # silently reintroducing Issue #1505's O(N) stall.
                logger.warning(
                    "_get_head_blob_hash_map: `git ls-tree -r HEAD` failed "
                    "(return code %s): %s; reconcile will fall back to the "
                    "slow per-file `git log` content-id lookup for every "
                    "committed file in this run",
                    result.returncode,
                    result.stderr.strip() if result.stderr else "(no stderr)",
                )
                return {}

            mapping: Dict[str, str] = {}
            # Codex #1505 review, Finding 5: count malformed/unparsable
            # entries instead of silently skipping them.
            malformed_count = 0
            for entry in result.stdout.split("\0"):
                if not entry:
                    continue
                try:
                    meta, path = entry.split("\t", 1)
                except ValueError:
                    malformed_count += 1
                    continue
                meta_parts = meta.split(" ")
                if len(meta_parts) < 3:
                    malformed_count += 1
                    continue
                blob_sha = meta_parts[2]
                mapping[path] = blob_sha
            if malformed_count:
                logger.warning(
                    "_get_head_blob_hash_map: skipped %d malformed `git "
                    "ls-tree` entr%s while building the HEAD blob-hash map",
                    malformed_count,
                    "y" if malformed_count == 1 else "ies",
                )
            return mapping
        except Exception as e:
            logger.warning(f"Failed to batch-fetch HEAD blob hashes: {e}")
            return {}

    def _get_modified_files_set(self) -> set:
        """Get set of files that differ from HEAD (unstaged + staged) in one batch call.

        Bug #471: Replaces per-file git diff subprocess calls with a single batched check.
        """
        modified: set = set()
        try:
            # Unstaged changes
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=self.config.codebase_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                modified.update(
                    line.strip()
                    for line in result.stdout.strip().splitlines()
                    if line.strip()
                )

            # Staged changes
            result = subprocess.run(
                ["git", "diff", "--name-only", "--staged", "HEAD"],
                cwd=self.config.codebase_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                modified.update(
                    line.strip()
                    for line in result.stdout.strip().splitlines()
                    if line.strip()
                )
        except Exception as e:
            logger.warning(f"Failed to batch-check modified files: {e}")
        return modified

    def _file_differs_from_committed_version(self, file_path: str) -> bool:
        """Check if file differs from committed version using cached batch result.

        Bug #471: Uses pre-computed set from _get_modified_files_set() instead of
        spawning a per-file subprocess.
        """
        if hasattr(self, "_reconcile_modified_files"):
            return file_path in self._reconcile_modified_files
        return False

    def _get_file_commit(self, file_path: str) -> str:
        """Get current commit hash for file, or working directory indicator if modified."""
        try:
            # Check if this is a git repository
            is_git_repo = self.git_topology_service.is_git_available()

            if not is_git_repo:
                # For non-git projects, always use timestamp-based IDs for consistency
                try:
                    file_path_obj = Path(self.config.codebase_dir) / file_path
                    file_stat = file_path_obj.stat()
                    return f"working_dir_{file_stat.st_mtime}_{file_stat.st_size}"
                except Exception:
                    # Fallback if stat fails
                    import time

                    return f"working_dir_{time.time()}_error"

            # For git repositories, use the git-aware logic
            # Check if file differs from committed version
            if self._file_differs_from_committed_version(file_path):
                # File has working directory changes - generate unique ID based on mtime/size
                try:
                    file_path_obj = Path(self.config.codebase_dir) / file_path
                    file_stat = file_path_obj.stat()
                    return f"working_dir_{file_stat.st_mtime}_{file_stat.st_size}"
                except Exception:
                    # Fallback if stat fails
                    import time

                    return f"working_dir_{time.time()}_error"

            # File matches committed version - use commit hash
            result = subprocess.run(
                ["git", "log", "-1", "--format=%H", "--", file_path],
                cwd=self.config.codebase_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            commit = result.stdout.strip() if result.returncode == 0 else ""
            # If no commit found, use "unknown"
            return commit if commit else "unknown"
        except Exception:
            return "unknown"
