"""
Git-aware document processor that extends the base DocumentProcessor.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, List, Dict, Any, Optional

from code_indexer.config import Config
from code_indexer.services.embedding_provider import EmbeddingProvider
from code_indexer.indexing.processor import DocumentProcessor
from code_indexer.services.file_identifier import FileIdentifier
from code_indexer.services.git_detection import GitDetectionService

if TYPE_CHECKING:
    from ..indexing.repo_manifest import RepoManifest

logger = logging.getLogger(__name__)


class GitAwareDocumentProcessor(DocumentProcessor):
    """Document processor with git-aware metadata enhancement."""

    def __init__(
        self,
        config: Config,
        embedding_provider: EmbeddingProvider,
        vector_store_client: Any,  # FilesystemVectorStore (vector store backend)
        content_backend: Optional[Any] = None,  # ContentBackend
        git_backend: Optional[Any] = None,  # GitBackend, for HEAD's commit/branch
    ):
        super().__init__(config, embedding_provider, vector_store_client)

        # The content seam. None (the default) and a LOCAL backend both mean
        # "read this container's disk", which is what every caller does today.
        #
        # The manifest is built ONLY for a delegated backend, and that is not a
        # tuning choice. Building it asks the backend to hash every file, and the
        # local backend hashes by reading -- so against local it would move the
        # per-file hash reads to the front of the pass rather than remove them.
        # Measured over a 20-file repository: 40 opens either way. Against a
        # delegated backend the hashes arrive over its own paged call and those
        # reads disappear: 20.
        self.content_backend = content_backend
        self.repo_manifest: Optional["RepoManifest"] = None
        # True between begin_manifest_pass and end_manifest_pass. Lets a pass
        # that may run inside another (the high-throughput pass, inside
        # smart_index) join the open one instead of opening its own, so files
        # are discovered and read against ONE snapshot. begin itself stays
        # unconditional: it always rebuilds.
        self.manifest_pass_open = False

        # The git seam: on a pod that does not hold the repository, HEAD's commit
        # and branch come from git-mcp -- otherwise every chunk and the index
        # metadata record "unknown", and refresh re-indexes every cycle.
        self.git_backend = git_backend
        self.file_identifier = FileIdentifier(
            config.codebase_dir, config, git_backend=git_backend
        )
        self.git_detection = GitDetectionService(
            config.codebase_dir, config, git_backend=git_backend
        )

    def begin_manifest_pass(self) -> None:
        """Build the manifest describing the repository as it is RIGHT NOW.

        Called at the start of each indexing pass, never from ``__init__``. The
        manifest is a snapshot, and an indexer outlives a pass: watch mode keeps
        one for a whole session and the reprocess pass reuses the same instance.
        Building it once per instance would serve the blob hash a file had before
        it was edited, and because the point UUID is seeded from that hash the
        new content would be written over the old version's ids.

        A no-op unless a DELEGATED backend is configured. Against the local
        backend the manifest is a measured wash -- building it reads every file,
        which is the work it would save -- so it is never built there, and the
        default path does no extra work at all.
        """
        self.manifest_pass_open = True
        if self.content_backend is None or getattr(
            self.content_backend, "is_local", True
        ):
            return

        # Every step below is a remote call or depends on one, and the callers
        # only reach their ``finally: end_manifest_pass()`` once this returns. A
        # failure here must not leave the pass marked open -- a later standalone
        # pass would then join it, find no manifest and read the local disk --
        # nor leave a half-installed manifest serving stale hashes.
        try:
            from ..indexing.file_finder import attach_delegated_manifest

            self.repo_manifest = attach_delegated_manifest(
                self.file_finder, self.content_backend, self.config.codebase_dir
            )
            self.file_identifier.set_manifest(self.repo_manifest)
        except BaseException:
            # attach_delegated_manifest may have installed a manifest on the
            # finder before failing; nothing may be left reading it.
            self.file_finder.set_manifest(None)
            self.end_manifest_pass()
            raise

    def end_manifest_pass(self) -> None:
        """Drop the snapshot, so nothing outside a pass can read a stale hash."""
        self.manifest_pass_open = False
        if self.repo_manifest is None:
            return
        self.repo_manifest = None
        self.file_identifier.set_manifest(None)
        self.file_finder.set_manifest(None)

    def _normalize_path_for_storage(self, file_path: Path) -> str:
        """
        Normalize file path to relative for portable database storage.

        CRITICAL FOR COW CLONING: All paths stored in Filesystem must be relative
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
                return str(file_path.relative_to(self.config.codebase_dir))
            except ValueError as e:
                logger.error(
                    f"Cannot normalize path {file_path} - not under codebase_dir "
                    f"{self.config.codebase_dir}: {e}"
                )
                raise
        return str(file_path)

    def _create_point_id(self, file_metadata: Dict[str, Any], chunk_index: int) -> str:
        """Create deterministic UUID-based point ID.

        Uses a deterministic UUID based on the file signature and chunk index
        to ensure the same file+chunk always gets the same ID.
        """
        import uuid

        project_id = file_metadata["project_id"]

        # Use git blob hash if available, otherwise file hash
        if file_metadata["git_available"] and file_metadata.get("git_hash"):
            signature = file_metadata["git_hash"]
        else:
            signature = file_metadata["file_hash"]

        # Create deterministic seed for UUID
        seed_string = f"{project_id}:{signature}:{chunk_index}"

        # Generate deterministic UUID using namespace UUID and seed
        point_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, seed_string)

        return str(point_uuid)

    def _create_unique_key(
        self, file_metadata: Dict[str, Any], chunk_index: int
    ) -> str:
        """Create human-readable unique key for tracking purposes."""
        project_id = file_metadata["project_id"]

        # Use git blob hash if available, otherwise file hash
        if file_metadata["git_available"] and file_metadata.get("git_hash"):
            signature = file_metadata["git_hash"][:12]  # Short hash
        else:
            signature = file_metadata["file_hash"][:12]  # Short hash

        return f"{project_id}:{signature}:{chunk_index}"

    def _validate_embedding(self, embedding: List[float]) -> bool:
        """Validate embedding dimensions match collection configuration."""
        if not embedding:
            return False

        # Get expected dimensions from embedding provider
        model_info = self.embedding_provider.get_model_info()
        expected_size = model_info.get("dimensions", 1024)
        actual_size = len(embedding)

        if actual_size != expected_size:
            print(
                f"Warning: Embedding dimension mismatch. Expected: {expected_size}, Got: {actual_size}"
            )
            return False

        # Check for invalid values
        if not all(
            isinstance(x, (int, float)) and not (x != x) for x in embedding
        ):  # NaN check
            print("Warning: Embedding contains invalid values (NaN or non-numeric)")
            return False

        return True

    def get_git_status(self) -> Dict[str, Any]:
        """Get current git status and metadata."""
        git_state = self.git_detection._get_current_git_state()
        file_stats = self.get_indexable_stats()

        return {
            "git_available": git_state["git_available"],
            "current_branch": git_state.get("branch", "unknown"),
            "current_commit": git_state.get("commit_hash", "unknown"),
            "project_id": self.file_identifier.get_project_id(),
            "file_stats": file_stats,
            "last_index_time": git_state.get("last_index_time", 0),
        }

    def get_git_status_fast(self) -> Dict[str, Any]:
        """Get git status WITHOUT expensive file stats (FAST for status display).

        Returns git branch/commit info only, skipping the expensive file scanning.
        Use this for status/monitoring where file stats aren't needed.

        Returns:
            Dict with git_available, current_branch, current_commit, project_id
        """
        git_state = self.git_detection._get_current_git_state()

        return {
            "git_available": git_state["git_available"],
            "current_branch": git_state.get("branch", "unknown"),
            "current_commit": git_state.get("commit_hash", "unknown"),
            "project_id": self.file_identifier.get_project_id(),
            "last_index_time": git_state.get("last_index_time", 0),
        }
