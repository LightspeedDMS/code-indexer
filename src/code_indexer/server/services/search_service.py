"""
Semantic Search Service.

Provides real semantic search operations following CLAUDE.md Foundation #1: No mocks.
All operations use real vector embeddings and vector store searches.
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import os
from pathlib import Path
from typing import List, Optional
import logging

from ..models.api_models import (
    SemanticSearchRequest,
    SemanticSearchResponse,
    SearchResultItem,
)
from ...config import ConfigManager
from ...backends.backend_factory import BackendFactory
from ...services.embedding_factory import EmbeddingProviderFactory
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)


def _get_golden_repos_dir() -> str:
    """Get golden repos directory from environment or default."""
    golden_repos_dir = os.environ.get("CIDX_GOLDEN_REPOS_DIR")
    if not golden_repos_dir:
        golden_repos_dir = str(Path.home() / ".cidx-server" / "data" / "golden-repos")
    return golden_repos_dir


# Language detection for search results
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
    ".vue": "vue",
    ".jsx": "jsx",
    ".tsx": "tsx",
}


class SemanticSearchService:
    """Service for semantic code search with repository-specific configuration."""

    def __init__(self):
        """Initialize the semantic search service."""
        # CLAUDE.md Foundation #1: Direct instantiation of real services only
        # NO dependency injection parameters that enable mocking

        # Note: We don't load any configuration here because each search operation
        # needs repository-specific configuration (different collection names)
        pass

    def search_repository(
        self, repo_id: str, search_request: SemanticSearchRequest
    ) -> SemanticSearchResponse:
        """
        Perform semantic search in repository using repository-specific configuration.

        Args:
            repo_id: Repository identifier
            search_request: Search request parameters

        Returns:
            Semantic search response with ranked results

        Raises:
            FileNotFoundError: If repository doesn't exist
            ValueError: If search request is invalid
        """
        repo_path = self._get_repository_path(repo_id)

        if not os.path.exists(repo_path):
            raise FileNotFoundError(f"Repository {repo_id} not found")

        return self.search_repository_path(repo_path, search_request)

    def search_repository_path(
        self, repo_path: str, search_request: SemanticSearchRequest
    ) -> SemanticSearchResponse:
        """
        Perform semantic search in repository using direct path.

        Args:
            repo_path: Direct path to repository directory
            search_request: Search request parameters

        Returns:
            Semantic search response with ranked results

        Raises:
            FileNotFoundError: If repository path doesn't exist
            ValueError: If search request is invalid
        """
        # Note: Metrics tracking is done at the MCP/REST entry point level
        # (semantic_query_manager._perform_search), NOT here.
        # Having it here caused double-counting bug - each search was counted twice.

        if not os.path.exists(repo_path):
            raise FileNotFoundError(f"Repository path {repo_path} not found")

        # CLAUDE.md Foundation #1: Real semantic search with vector embeddings
        # 1. Load repository-specific configuration
        # 2. Generate embeddings for the query
        # 3. Search vector store with correct collection name
        # 4. Rank results by semantic similarity

        search_results = self._perform_semantic_search(
            repo_path,
            search_request.query,
            search_request.limit,
            search_request.include_source,
        )

        return SemanticSearchResponse(
            query=search_request.query,
            results=search_results,
            total=len(search_results),
        )

    def _perform_semantic_search(
        self, repo_path: str, query: str, limit: int, include_source: bool
    ) -> List[SearchResultItem]:
        """
        Perform real semantic search using repository-specific configuration.

        CLAUDE.md Foundation #1: Real vector search, no text search fallbacks.
        Uses BackendFactory for vector storage.

        Args:
            repo_path: Path to repository directory
            query: Search query
            limit: Maximum number of results
            include_source: Whether to include source code in results

        Returns:
            List of search results ranked by semantic similarity

        Raises:
            RuntimeError: If embedding generation or vector search fails
        """
        try:
            # Load repository-specific configuration
            config_manager = ConfigManager.create_with_backtrack(Path(repo_path))
            config = config_manager.get_config()

            logger.info(
                f"Loaded repository config from {repo_path}",
                extra={"correlation_id": get_correlation_id()},
            )

            # Create backend using BackendFactory (Story #526: pass server cache)
            # Import here to avoid circular dependency
            from ..app import _server_hnsw_cache

            backend = BackendFactory.create(
                config=config,
                project_root=Path(repo_path),
                hnsw_cache=_server_hnsw_cache,
            )
            vector_store_client = backend.get_vector_store_client()

            logger.info(
                f"Using backend: {type(backend).__name__}",
                extra={"correlation_id": get_correlation_id()},
            )

            # Create repository-specific embedding service
            embedding_service = EmbeddingProviderFactory.create(config=config)

            # Resolve correct collection name based on repository configuration
            collection_name = vector_store_client.resolve_collection_name(
                config, embedding_service
            )

            logger.info(
                f"Using collection: {collection_name}",
                extra={"correlation_id": get_correlation_id()},
            )

            # Real vector search - different parameter patterns for different backends
            # FilesystemVectorStore: parallel execution (query + embedding_provider)
            # Backend: sequential execution (pre-computed query_vector)
            from ...storage.filesystem_vector_store import FilesystemVectorStore

            if isinstance(vector_store_client, FilesystemVectorStore):
                # FilesystemVectorStore: parallel execution with query string and provider
                # Embedding generation happens in parallel with index loading
                search_results, _ = vector_store_client.search(
                    query=query,
                    embedding_provider=embedding_service,
                    collection_name=collection_name,
                    limit=limit,
                    return_timing=True,
                )
            else:
                # Backend: sequential execution with pre-computed embedding
                query_embedding = embedding_service.get_embedding(query)
                search_results = vector_store_client.search(
                    query_vector=query_embedding,
                    limit=limit,
                    collection_name=collection_name,
                )

            logger.info(
                f"Found {len(search_results)} results",
                extra={"correlation_id": get_correlation_id()},
            )

            # Format results for response
            formatted_results = []
            for result in search_results:
                if not isinstance(result, dict):
                    continue  # Skip malformed results
                payload = result.get("payload", {})
                score = result.get("score", 0.0)

                # Extract source code if requested
                source_content = None
                if include_source and "content" in payload:
                    source_content = payload["content"]

                search_item = SearchResultItem(
                    file_path=payload.get("path", ""),
                    line_start=payload.get("line_start", 0),
                    line_end=payload.get("line_end", 0),
                    score=score,
                    content=source_content or payload.get("snippet", ""),
                    language=self._detect_language_from_path(payload.get("path", "")),
                    file_last_modified=payload.get("file_last_modified"),
                    indexed_timestamp=payload.get("indexed_timestamp"),
                )
                formatted_results.append(search_item)

            return formatted_results

        except Exception as e:
            logger.error(
                format_error_log(
                    "MCP-GENERAL-170",
                    f"Semantic search failed for repo {repo_path}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            raise RuntimeError(f"Semantic search failed: {e}")

    def _detect_language_from_path(self, file_path: str) -> Optional[str]:
        """
        Detect programming language from file extension.

        Args:
            file_path: Path to file

        Returns:
            Programming language name or None if unknown
        """
        if not file_path:
            return None

        path = Path(file_path)
        extension = path.suffix.lower()
        return LANGUAGE_EXTENSIONS.get(extension)

    def _get_repository_path(self, repo_id: str) -> str:
        """
        Get file system path for repository.

        Uses GlobalRegistry with alias_name lookup (e.g., "my-repo-global") and
        AliasManager to get the current target path (registry path becomes stale
        after refresh operations).

        Args:
            repo_id: Repository identifier (global alias name, e.g., "my-repo-global")

        Returns:
            File system path to repository

        Raises:
            FileNotFoundError: If repository not found in global repositories
        """
        from ..utils.registry_factory import get_server_global_registry
        from code_indexer.global_repos.alias_manager import AliasManager

        # Get golden_repos_dir from helper function
        golden_repos_dir = _get_golden_repos_dir()

        # Look up global repo in GlobalRegistry to verify it exists
        registry = get_server_global_registry(golden_repos_dir)
        global_repos = registry.list_global_repos()

        # Find the matching global repo by alias_name
        repo_entry = next(
            (r for r in global_repos if r.get("alias_name") == repo_id), None
        )

        if not repo_entry:
            raise FileNotFoundError(
                f"Repository '{repo_id}' not found in global repositories"
            )

        # Use AliasManager to get current target path (registry path becomes stale after refresh)
        alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
        target_path = alias_manager.read_alias(repo_id)

        if not target_path:
            raise FileNotFoundError(
                f"Alias for global repository '{repo_id}' not found"
            )

        # Verify the path exists
        if not Path(target_path).exists():
            raise FileNotFoundError(
                f"Repository path for '{repo_id}' does not exist: {target_path}"
            )

        # Type assertion: target_path is verified non-None above
        assert isinstance(target_path, str)
        return target_path


# Global service instance
search_service = SemanticSearchService()
