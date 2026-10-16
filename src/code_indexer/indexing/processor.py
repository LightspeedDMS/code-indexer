"""Document processing utilities for indexing."""

import logging
<<<<<<< HEAD
=======
import time
>>>>>>> upstream/master
from pathlib import Path
from typing import FrozenSet, List, Dict, Any, Optional, Callable, Iterable
from dataclasses import dataclass

from ..config import Config
from ..services.embedding_provider import EmbeddingProvider
from .file_finder import FileFinder
from .fixed_size_chunker import FixedSizeChunker
<<<<<<< HEAD
from ..utils.path_confinement import is_resolved_within_root
=======
from ..utils.path_confinement import is_indexable_location
>>>>>>> upstream/master

logger = logging.getLogger(__name__)


@dataclass
class ProcessingStats:
    """Statistics from document processing."""

    files_processed: int = 0
    chunks_created: int = 0
    failed_files: int = 0
    total_size: int = 0
    start_time: float = 0.0
    end_time: float = 0.0
    cancelled: bool = False
    #: Bug #1969 Round 6 (P1-3): relative paths of files this run reported
    #: as failed, when known. Distinct from `failed_files` (a bare count):
    #: the self-heal-reprocess sidecar clear logic needs to know WHICH
    #: path(s) failed, not just how many, since a stale never-deleted
    #: point can make a genuinely-failed path look "resolved" under a
    #: point-existence check alone. May under-count relative to
    #: `failed_files` for a failure with no attributable path (a rare
    #: internal executor error) -- callers must treat that as "some
    #: failure is unattributed" rather than assuming completeness.
    failed_paths: FrozenSet[str] = frozenset()
<<<<<<< HEAD
=======
    #: Index entries the run changed without processing a file: deleted or
    #: hidden paths, un-hidden paths, restored full-text documents. With
    #: `files_processed`, tells a run that changed nothing apart.
    index_entries_changed: int = 0
    #: Bug #2155: relative paths of the files a previous run recorded as
    #: failed that this run re-planned (``_merge_recorded_failures``).
    retried_failed_paths: FrozenSet[str] = frozenset()

    @property
    def failed_only_on_recorded_retries(self) -> bool:
        """Bug #2155: the run processed no file and EVERY failure is an
        attributed retry of a failure recorded before it started -- no new
        work failed. An unattributed failure is never such a retry."""
        return (
            self.files_processed == 0
            and self.failed_files > 0
            and not self.cancelled
            and self.failed_files == len(self.failed_paths)
            and self.failed_paths <= self.retried_failed_paths
        )
>>>>>>> upstream/master

    @property
    def duration(self) -> float:
        """Processing duration in seconds."""
        return (
            self.end_time - self.start_time if self.end_time > self.start_time else 0.0
        )

    @property
    def files_per_second(self) -> float:
        """Files processed per second."""
        return self.files_processed / self.duration if self.duration > 0 else 0.0


class DocumentProcessor:
    """Processes documents for indexing."""

    def __init__(
        self,
        config: Config,
        embedding_provider: EmbeddingProvider,
        vector_store_client: Any,  # FilesystemVectorStore (vector store backend)
    ):
        self.config = config
        self.embedding_provider = embedding_provider
        self.vector_store_client = vector_store_client
        self.file_finder = FileFinder(config)
        # Use model-aware fixed-size chunker for all processing
        self.fixed_size_chunker = FixedSizeChunker(config)

    def _filter_paths_within_codebase_root(
        self, candidates: Iterable[Path]
    ) -> List[Path]:
        """Drop any candidate whose resolved location is not inside the
        resolved codebase root before it is treated as a file to index.

        Shared by every path-list-building call site that joins an
        externally-sourced relative path string (a git-diff entry, a
        filesystem-watch event) onto codebase_dir without going through
        ``FileFinder.find_files()``'s own containment check: the
        incremental git-diff path, the watch-mode incremental-processing
        path, and the branch-changes high-throughput join. Rejected
        candidates are dropped silently except for a single WARNING
        naming the COUNT, never the path/content.

        Resolves ``self.config.codebase_dir`` directly rather than
        reading ``self.file_finder.resolved_codebase_dir`` -- several
        callers/tests replace the whole ``file_finder`` attribute with a
        test double, which would otherwise turn the containment root
        into an unrelated mock value and reject every real file.

<<<<<<< HEAD
        Server context only (``config.confined_to_codebase_root``); local
        CLI indexing keeps every candidate, following symlinks wherever
        they point.
        """
        if not self.config.confined_to_codebase_root:
            return list(candidates)
        resolved_root = Path(self.config.codebase_dir).resolve()
        kept: List[Path] = []
        rejected_count = 0
        for candidate in candidates:
            if is_resolved_within_root(candidate, resolved_root):
=======
        Server context (``config.confined_to_codebase_root``) drops a
        candidate resolving outside the root or into its ``.git``; local
        CLI indexing follows symlinks wherever they point, except into the
        repository's own ``.git``, which no context indexes.
        """
        resolved_root = Path(self.config.codebase_dir).resolve()
        confined = self.config.confined_to_codebase_root
        kept: List[Path] = []
        rejected_count = 0
        for candidate in candidates:
            if is_indexable_location(candidate, resolved_root, confined):
>>>>>>> upstream/master
                kept.append(candidate)
            else:
                rejected_count += 1
        if rejected_count:
            logger.warning(
<<<<<<< HEAD
                "%d candidate file path(s) resolve outside the codebase "
                "root; dropping them from this indexing pass.",
=======
                "%d candidate file path(s) do not resolve to an indexable "
                "location; dropping them from this indexing pass.",
>>>>>>> upstream/master
                rejected_count,
            )
        return kept

    def process_file(self, file_path: Path) -> List[Dict[str, Any]]:
        """DEPRECATED: Use process_files_high_throughput instead."""
        raise NotImplementedError(
            "process_file is deprecated. Use process_files_high_throughput for all processing."
        )

    def process_files(
        self,
        files: List[Path],
        batch_size: int = 50,
        progress_callback: Optional[Callable] = None,
    ) -> ProcessingStats:
        """DEPRECATED: Use process_files_high_throughput instead."""
        raise NotImplementedError(
            "process_files is deprecated. Use process_files_high_throughput for all processing."
        )

    def get_indexable_stats(self) -> Dict[str, Any]:
        """Get statistics about indexable files."""
        return self.file_finder.get_file_stats()
