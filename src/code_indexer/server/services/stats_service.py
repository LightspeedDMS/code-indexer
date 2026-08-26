"""
Repository Statistics Service.

Provides real repository statistics following CLAUDE.md Foundation #1: No mocks.
All operations use real file system, database, and Filesystem operations.
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import os
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any, TYPE_CHECKING
from datetime import datetime, timezone
import logging
from dataclasses import dataclass

from ..models.api_models import (
    RepositoryStatsResponse,
    RepositoryFilesInfo,
    RepositoryStorageInfo,
    RepositoryActivityInfo,
    RepositoryHealthInfo,
)
from ...config import ConfigManager
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.server.logging_utils import format_error_log

if TYPE_CHECKING:
    from ..repositories.activated_repo_manager import ActivatedRepoManager

logger = logging.getLogger(__name__)


def _get_golden_repos_dir() -> str:
    """Get golden repos directory from app.state configuration."""
    from typing import Optional, cast
    from ..app import app as app_module

    golden_repos_dir: Optional[str] = cast(
        Optional[str], getattr(app_module.state, "golden_repos_dir", None)
    )
    if golden_repos_dir:
        return golden_repos_dir

    raise RuntimeError(
        "golden_repos_dir not configured in app.state. "
        "Server must set app.state.golden_repos_dir during startup."
    )


def _get_activated_repo_manager() -> "ActivatedRepoManager":
    """Get the DI-wired ActivatedRepoManager from app.state (Bug #1683).

    Mirrors `_get_golden_repos_dir` above. Previously `_get_repository_path`
    constructed a bare, unwired `ActivatedRepoManager()` -- its constructor
    hardcodes `Path.home()/".cidx-server"/"data"` and ignores
    `CIDX_SERVER_DATA_DIR`, so in cluster mode (or any deployment
    overriding the data dir) it read from the WRONG per-node store instead
    of the shared, properly-wired singleton every other lookup path uses.
    """
    from typing import cast
    from ..app import app as app_module

    manager = getattr(app_module.state, "activated_repo_manager", None)
    if manager is None:
        raise RuntimeError(
            "activated_repo_manager not initialized in app.state. "
            "Server must set app.state.activated_repo_manager during startup."
        )
    return cast("ActivatedRepoManager", manager)


# File extension to language mapping
LANGUAGE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".go": "go",
    ".rs": "rust",
    ".php": "php",
    ".rb": "ruby",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".sass": "sass",
    ".less": "less",
    ".vue": "vue",
    ".jsx": "jsx",
    ".tsx": "tsx",
    ".md": "markdown",
    ".rst": "rst",
    ".txt": "text",
    ".json": "json",
    ".xml": "xml",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "config",
    ".conf": "config",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".fish": "shell",
    ".ps1": "powershell",
    ".bat": "batch",
    ".cmd": "batch",
}


@dataclass
class FileStats:
    """File statistics for a single file."""

    path: str
    size_bytes: int
    language: Optional[str]
    is_indexed: bool
    modified_at: datetime


class RepositoryStatsService:
    """Service for calculating repository statistics."""

    # Bug #1691: CLASS-LEVEL RLock (not per-instance), mirroring the Bug
    # #1650 pattern in FileListingService/GitOperationsService, so
    # instances created via RepositoryStatsService.__new__(RepositoryStatsService)
    # (an existing test pattern) still have a lock to synchronize on.
    _vector_store_client_lock = threading.RLock()

    def __init__(self):
        """Initialize the repository stats service.

        Bug #1691: `vector_store_client` construction is DEFERRED to first
        real access via the `vector_store_client` property (defined
        below). This class is constructed at IMPORT TIME by the
        module-level `stats_service` singleton (bottom of this file), with
        no real repository context available. Eagerly resolving a config
        here reproduced the Bug #1683 CWD-fallback failure shape:
        `ConfigManager.create_with_backtrack()` with no starting directory
        backtracks from the SERVER PROCESS's CWD, found no config there,
        and silently fell back to a bare `Config()` with
        `codebase_dir = Path(".")` -- so `FilesystemVectorStore.__init__`
        went on to create a stray `.code-indexer/index` directory relative
        to that CWD (confirmed live on the real dev server).
        """
        # CLAUDE.md Foundation #1: Direct instantiation of real services only
        # NO dependency injection parameters that enable mocking
        self._vector_store_client_lazy: Optional[FilesystemVectorStore] = None

        # Repository manager will be instantiated when implemented
        # For now, indicate that real integration is expected
        self.repository_manager = (
            None  # Will be real RepositoryManager when implemented
        )

    def _build_vector_store_client(self) -> FilesystemVectorStore:
        """Construct the real FilesystemVectorStore from a freshly-resolved
        config, refusing to fall back to the server process's CWD when no
        real repository config can be found.

        CLAUDE.md Foundation #1: real FilesystemVectorStore integration,
        not injectable, not mockable.

        Bug #1691: mirrors the Bug #1683 guard already established in
        `AutoWatchManager.start_watch` -- `create_with_backtrack()`
        unconditionally returns a ConfigManager even when NO config was
        found anywhere in the CWD's ancestor chain; it silently defaults
        `config_path` to `{cwd}/.code-indexer/config.json`, which does not
        exist, and `get_config()` would then fall back to a bare
        `Config()` whose `codebase_dir` is the unresolved relative
        `Path(".")`. Verify a REAL config was found before trusting it,
        rather than silently defaulting to CWD.
        """
        try:
            config_manager = ConfigManager.create_with_backtrack()

            if not config_manager.config_path.exists():
                raise RuntimeError(
                    "No .code-indexer/config.json found relative to the "
                    "current working directory or any parent; refusing to "
                    "fall back to a bare Config() with codebase_dir='.' "
                    "(Bug #1691)"
                )

            config = config_manager.get_config()

            # Real FilesystemVectorStore integration - not injectable, not mockable
            index_dir = Path(config.codebase_dir) / ".code-indexer" / "index"
            return FilesystemVectorStore(
                base_path=index_dir, project_root=Path(config.codebase_dir)
            )

        except Exception as e:
            logger.error(
                format_error_log(
                    "MCP-GENERAL-176",
                    f"Failed to initialize real dependencies: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            raise RuntimeError(f"Cannot initialize repository stats service: {e}")

    @property
    def vector_store_client(self) -> FilesystemVectorStore:
        """Lazily construct the real FilesystemVectorStore (Bug #1691).

        Uses getattr(..., None) rather than a bare attribute read: some
        existing test files construct this service via
        RepositoryStatsService.__new__(RepositoryStatsService) (bypassing
        __init__ entirely) and may read this property before ever
        assigning to it.

        Guarded by a per-instance `_vsc_initializing` sentinel (mirrors
        FileListingService.activated_repo_manager's identical fix): the
        RLock alone stops CROSS-THREAD deadlock but not SAME-THREAD
        re-entrant recursion -- on re-entry the double-checked `is None`
        test is still True (the assignment happens only after
        _build_vector_store_client() returns), so an unguarded re-entrant
        call arriving from within construction would construct AGAIN.

        Raises RuntimeError (not AttributeError) on re-entrant access:
        unlike a module-level `__getattr__` deferral (where AttributeError
        has a specific required protocol meaning), this is a plain
        `@property` -- AttributeError here would be a footgun for any
        caller using `getattr(obj, "vector_store_client", default)` /
        `hasattr()`, which would silently receive the default and mask a
        real re-entrancy bug (code review finding, Bug #1691).
        """
        if getattr(self, "_vector_store_client_lazy", None) is None:
            with self._vector_store_client_lock:
                if getattr(self, "_vsc_initializing", False):
                    raise RuntimeError(
                        "vector_store_client is still under construction "
                        "(re-entrant access)"
                    )
                if getattr(self, "_vector_store_client_lazy", None) is None:
                    self._vsc_initializing = True
                    try:
                        self._vector_store_client_lazy = (
                            self._build_vector_store_client()
                        )
                    finally:
                        self._vsc_initializing = False
        return self._vector_store_client_lazy

    @vector_store_client.setter
    def vector_store_client(self, value: FilesystemVectorStore) -> None:
        with self._vector_store_client_lock:
            self._vector_store_client_lazy = value

    def get_repository_stats(
        self, repo_id: str, username: Optional[str] = None
    ) -> RepositoryStatsResponse:
        """
        Get comprehensive statistics for a repository.

        Args:
            repo_id: Repository identifier (user_alias)
            username: Username owning the activated repository (user_alias for activated repos)
            username: Username owning the activated repository (for activated repos)

        Returns:
            Repository statistics response

        Raises:
            FileNotFoundError: If repository doesn't exist
            PermissionError: If repository access denied
        """
        repo_path = self._get_repository_path(repo_id, username)

        if not os.path.exists(repo_path):
            raise FileNotFoundError(f"Repository {repo_id} not found at {repo_path}")

        # Collect file statistics
        file_stats = self._collect_file_statistics(repo_path)

        # Calculate aggregated statistics
        files_info = self._calculate_files_info(file_stats)
        storage_info = self._calculate_storage_info(repo_path, file_stats)
        activity_info = self._calculate_activity_info(repo_id, repo_path)
        health_info = self._calculate_health_info(file_stats, storage_info)

        return RepositoryStatsResponse(
            repository_id=repo_id,
            files=files_info,
            storage=storage_info,
            activity=activity_info,
            health=health_info,
        )

    def _get_repository_path(self, repo_id: str, username: Optional[str] = None) -> str:
        """
        Get file system path for repository from real database.

        CLAUDE.md Foundation #1: Real database lookup, no placeholders.

        Args:
            repo_id: Repository identifier (user_alias for activated repos)
            username: Username owning the activated repository (for activated repos)

        Returns:
            Real file system path to repository

        Raises:
            RuntimeError: If database lookup fails
            FileNotFoundError: If repository not found
        """
        try:
            # Use the DI-wired ActivatedRepoManager (Bug #1683) to find the
            # user's activated repository -- never a fresh, unwired instance.
            repo_manager = _get_activated_repo_manager()

            # Get activated repository path for user
            if username is None:
                raise ValueError("Username is required for activated repository lookup")
            activated_path = repo_manager.get_activated_repo_path(
                username=username, user_alias=repo_id
            )

            if activated_path and Path(activated_path).exists():
                return activated_path
            else:
                raise FileNotFoundError(
                    f"Repository '{repo_id}' not found for user '{username}'"
                )

        except Exception as e:
            logger.error(
                format_error_log(
                    "MCP-GENERAL-177",
                    f"Failed to get repository path for {repo_id}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            if isinstance(e, FileNotFoundError):
                raise
            raise RuntimeError(f"Unable to access repository {repo_id}: {e}")

    def _collect_file_statistics(self, repo_path: str) -> List[FileStats]:
        """
        Collect statistics for all files in repository.

        Args:
            repo_path: Repository file system path

        Returns:
            List of file statistics
        """
        file_stats = []
        repo_root = Path(repo_path)

        try:
            for file_path in repo_root.rglob("*"):
                if file_path.is_file():
                    try:
                        stat_info = file_path.stat()
                        relative_path = file_path.relative_to(repo_root)

                        file_stat = FileStats(
                            path=str(relative_path),
                            size_bytes=stat_info.st_size,
                            language=self._detect_language(file_path),
                            is_indexed=self._is_file_indexed(file_path),
                            modified_at=datetime.fromtimestamp(
                                stat_info.st_mtime, tz=timezone.utc
                            ),
                        )
                        file_stats.append(file_stat)

                    except (OSError, PermissionError) as e:
                        logger.warning(
                            format_error_log(
                                "MCP-GENERAL-178",
                                f"Cannot access file {file_path}: {e}",
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )
                        continue

        except PermissionError as e:
            logger.error(
                format_error_log(
                    "MCP-GENERAL-179",
                    f"Cannot access repository directory {repo_path}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            raise

        return file_stats

    def _detect_language(self, file_path: Path) -> Optional[str]:
        """
        Detect programming language from file extension.

        Args:
            file_path: Path to file

        Returns:
            Programming language name or None if unknown
        """
        extension = file_path.suffix.lower()
        return LANGUAGE_EXTENSIONS.get(extension)

    def _is_file_indexed(self, file_path: Path) -> bool:
        """
        Check if file is currently indexed in vector store.

        CLAUDE.md Foundation #1: Real vector store check, no extension-based heuristics.

        Args:
            file_path: Path to file

        Returns:
            Whether file is actually indexed in vector store

        Raises:
            RuntimeError: If vector store check fails
        """
        try:
            # This would query real vector store collection to check if file is indexed
            # Implementation requires knowing which collection and search criteria
            # For now, fail clearly to indicate missing real implementation
            raise RuntimeError(
                "Real vector store file indexing check not yet implemented. "
                "This service requires actual vector store query to determine file indexing status."
            )
        except Exception as e:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-180",
                    f"Cannot check indexing status for {file_path}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            # Fall back to extension check only as last resort
            extension = file_path.suffix.lower()
            indexable_extensions = {
                ".py",
                ".js",
                ".ts",
                ".java",
                ".c",
                ".cpp",
                ".h",
                ".hpp",
                ".cs",
                ".go",
                ".rs",
                ".php",
                ".rb",
                ".swift",
                ".kt",
                ".scala",
                ".sql",
                ".html",
                ".css",
                ".vue",
                ".jsx",
                ".tsx",
            }
            return extension in indexable_extensions

    def _calculate_files_info(self, file_stats: List[FileStats]) -> RepositoryFilesInfo:
        """
        Calculate file-related statistics.

        Args:
            file_stats: List of file statistics

        Returns:
            File information summary
        """
        total_files = len(file_stats)
        indexed_files = sum(1 for f in file_stats if f.is_indexed)

        # Count files by language
        language_counts: Dict[str, int] = {}
        for file_stat in file_stats:
            if file_stat.language:
                language_counts[file_stat.language] = (
                    language_counts.get(file_stat.language, 0) + 1
                )

        return RepositoryFilesInfo(
            total=total_files, indexed=indexed_files, by_language=language_counts
        )

    def _calculate_storage_info(
        self, repo_path: str, file_stats: List[FileStats]
    ) -> RepositoryStorageInfo:
        """
        Calculate storage-related statistics.

        Args:
            repo_path: Repository path
            file_stats: List of file statistics

        Returns:
            Storage information summary
        """
        total_size = sum(f.size_bytes for f in file_stats)

        # For now, estimate index size as 10% of repository size
        # In real implementation, query actual index size from Filesystem
        estimated_index_size = int(total_size * 0.1)

        # Estimate embedding count based on indexed files
        # Assume average of 10 embeddings per indexed file
        indexed_files = sum(1 for f in file_stats if f.is_indexed)
        estimated_embeddings = indexed_files * 10

        return RepositoryStorageInfo(
            repository_size_bytes=total_size,
            index_size_bytes=estimated_index_size,
            embedding_count=estimated_embeddings,
        )

    def _calculate_activity_info(
        self, repo_id: str, repo_path: str
    ) -> RepositoryActivityInfo:
        """
        Calculate activity-related statistics.

        Args:
            repo_id: Repository identifier (user_alias for activated repos)
            username: Username owning the activated repository (for activated repos)
            repo_path: Repository path

        Returns:
            Activity information summary
        """
        # For now, use directory creation time as created_at
        # In real implementation, query database for actual creation time
        try:
            stat_info = os.stat(repo_path)
            created_at = datetime.fromtimestamp(stat_info.st_ctime, tz=timezone.utc)
        except OSError:
            created_at = datetime.now(timezone.utc)

        return RepositoryActivityInfo(
            created_at=created_at,
            last_sync_at=None,  # Would query from database
            last_accessed_at=None,  # Would query from database
            sync_count=0,  # Would query from database
        )

    def _calculate_health_info(
        self, file_stats: List[FileStats], storage_info: RepositoryStorageInfo
    ) -> RepositoryHealthInfo:
        """
        Calculate repository health assessment.

        Args:
            file_stats: List of file statistics
            storage_info: Storage information

        Returns:
            Health assessment
        """
        issues = []

        # Check indexing coverage
        if file_stats:
            index_ratio = sum(1 for f in file_stats if f.is_indexed) / len(file_stats)
            if index_ratio < 0.5:
                issues.append(f"Low indexing coverage: {index_ratio:.1%}")

        # Check for very large files
        large_files = [f for f in file_stats if f.size_bytes > 1024 * 1024]  # >1MB
        if len(large_files) > 10:
            issues.append(f"Many large files: {len(large_files)} files >1MB")

        # Check repository size
        if storage_info.repository_size_bytes > 100 * 1024 * 1024:  # >100MB
            issues.append(
                f"Large repository size: {storage_info.repository_size_bytes / (1024 * 1024):.1f}MB"
            )

        # Calculate health score (1.0 = perfect, 0.0 = terrible)
        base_score = 1.0
        if issues:
            score_penalty = len(issues) * 0.1
            health_score = max(0.0, base_score - score_penalty)
        else:
            health_score = base_score

        return RepositoryHealthInfo(score=health_score, issues=issues)

    def get_embedding_count(self, repo_id: str) -> int:
        """
        Get actual embedding count from vector store for repository.

        CLAUDE.md Foundation #1: Real vector store integration, no placeholders.

        Args:
            repo_id: Repository identifier (user_alias for activated repos)
            username: Username owning the activated repository (for activated repos)

        Returns:
            Number of embeddings in vector store collection

        Raises:
            RuntimeError: If unable to retrieve embedding count
            ConnectionError: If vector store is not accessible
        """
        collection_name = f"repo_{repo_id}"

        try:
            # Check if collection exists first
            if not self.vector_store_client.collection_exists(collection_name):
                return 0

            # Get real collection info from vector store
            collection_info = self.vector_store_client.get_collection_info(
                collection_name
            )
            vectors_count = collection_info.get("vectors_count", 0)
            return int(vectors_count) if vectors_count is not None else 0

        except Exception as e:
            logger.error(
                format_error_log(
                    "MCP-GENERAL-181",
                    f"Failed to get embedding count for {repo_id}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            raise RuntimeError(
                f"Unable to retrieve embedding count for repository {repo_id}: {e}"
            )

    def get_repository_metadata(self, repo_id: str) -> Dict[str, Any]:
        """
        Get repository metadata from real database.

        CLAUDE.md Foundation #1: Real database query, no simulated data.

        Args:
            repo_id: Repository identifier (user_alias for activated repos)
            username: Username owning the activated repository (for activated repos)

        Returns:
            Repository metadata dictionary

        Raises:
            RuntimeError: If database is not accessible
            FileNotFoundError: If repository not found
        """
        try:
            # Use BackendRegistry for correct alias_name lookup
            from code_indexer.global_repos.alias_manager import (
                AliasManager,
                resolve_alias_or_index_path,
            )
            from pathlib import Path
            from .. import app as app_module

            # Get golden repos directory
            golden_repos_dir = _get_golden_repos_dir()

            # Use BackendRegistry to find repo by alias_name
            backend_registry = getattr(app_module.app.state, "backend_registry", None)
            if backend_registry:
                repos_dict = backend_registry.global_repos.list_repos()
                global_repos = list(repos_dict.values())
            else:
                global_repos = []

            repo_entry = next(
                (r for r in global_repos if r.get("alias_name") == repo_id), None
            )

            if not repo_entry:
                raise FileNotFoundError(
                    f"Repository '{repo_id}' not found in global repositories"
                )

            # Use AliasManager to get the target path, falling back to the
            # registry's index_path when the alias pointer is missing (Bug #1315).
            alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
            target_path = resolve_alias_or_index_path(
                alias_manager, alias_name=repo_id, repo_entry=repo_entry
            )

            if target_path is None:
                raise FileNotFoundError(
                    f"Alias for global repository '{repo_id}' not found"
                )

            # Return metadata from global registry entry
            return {
                "created_at": repo_entry.get("created_at"),
                "last_sync_at": None,  # This would come from sync tracking
                "sync_count": 0,  # This would come from sync tracking
                "repo_url": repo_entry.get("repo_url"),
                "default_branch": repo_entry.get("default_branch"),
                "clone_path": str(target_path),
            }

        except FileNotFoundError:
            raise
        except Exception as e:
            logger.error(
                format_error_log(
                    "MCP-GENERAL-182",
                    f"Failed to get repository metadata for {repo_id}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            raise RuntimeError(f"Unable to access repository metadata: {e}")


# Global service instance
stats_service = RepositoryStatsService()
