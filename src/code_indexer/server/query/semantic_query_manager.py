"""
Semantic Query Manager for CIDX Server.

Provides semantic search functionality for activated repositories with user isolation,
background job integration, and proper resource management.
"""

from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.logging_utils import format_error_log, get_log_extra

import json
import logging
import re
import io
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from dataclasses import dataclass

import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed

from code_indexer.services.query_strategy import (
    apply_score_gate,
    PARALLEL_FETCH_MULTIPLIER,
    MAX_PARALLEL_FETCH,
    PARALLEL_TIMEOUT_SECONDS,
)
from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

from ..repositories.activated_repo_manager import ActivatedRepoManager
from ..repositories.background_jobs import BackgroundJobManager
from ...search.query import SearchResult
from ...proxy.config_manager import ProxyConfigManager
from ...proxy.cli_integration import _execute_query

logger = logging.getLogger(__name__)


class SemanticQueryError(Exception):
    """Base exception for semantic query operations."""

    pass


@dataclass
class QueryResult:
    """Individual query result with standardized format.

    For composite repositories:
        - repository_alias: The composite repository name (parent)
        - source_repo: Which component repo this result came from
        - file_path: Relative path within source_repo

    For single repositories:
        - repository_alias: The repository name
        - source_repo: None (not a composite)
        - file_path: Relative path within repository

    For temporal queries (Story #503 - Temporal Metadata):
        - metadata: Contains commit_hash, commit_date, author_name, author_email,
                   commit_message, diff_type for each result
        - temporal_context: Contains first_seen, last_seen, commit_count, commits
    """

    file_path: str
    line_number: int
    code_snippet: str
    similarity_score: float
    repository_alias: str
    source_repo: Optional[str] = None  # Which component repo (for composite repos)
    # FTS-specific field (Story #680 - FTS Payload Control)
    match_text: Optional[str] = None  # The exact matched text from FTS search
    # Temporal metadata fields (Story #503 - MCP/REST API parity with CLI)
    metadata: Optional[Dict[str, Any]] = (
        None  # Commit info: hash, date, author, message, diff_type
    )
    temporal_context: Optional[Dict[str, Any]] = (
        None  # Aggregate: first_seen, last_seen, commit_count
    )
    # Embedding provider that served this result (Story #593)
    source_provider: str = ""
    # Fusion metadata (Story #618 - Score/Provenance Transparency)
    fusion_score: Optional[float] = None
    contributing_providers: Optional[List[str]] = None

    @classmethod
    def from_search_result(
        cls, search_result: SearchResult, repository_alias: str
    ) -> "QueryResult":
        """Create QueryResult from SearchResult dataclass."""
        return cls(
            file_path=search_result.file_path,
            line_number=1,  # SearchResult doesn't have line numbers, default to 1
            code_snippet=search_result.content,
            similarity_score=search_result.score,
            repository_alias=repository_alias,
            source_repo=None,  # Single repository, no source_repo
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API response."""
        result = {
            "file_path": self.file_path,
            "line_number": self.line_number,
            "code_snippet": self.code_snippet,
            "similarity_score": self.similarity_score,
            "repository_alias": self.repository_alias,
            "source_repo": self.source_repo,
            # Include source_provider always (Story #593 - multi-provider tracking)
            "source_provider": self.source_provider,
        }
        # Include FTS match_text if present (Story #680)
        if self.match_text is not None:
            result["match_text"] = self.match_text
        # Include temporal metadata if present (Story #503)
        if self.metadata is not None:
            result["metadata"] = self.metadata
        if self.temporal_context is not None:
            result["temporal_context"] = self.temporal_context
        # Include fusion metadata if present (Story #618)
        if self.fusion_score is not None:
            result["fusion_score"] = self.fusion_score
        if self.contributing_providers is not None:
            result["contributing_providers"] = self.contributing_providers
        return result


@dataclass
class QueryMetadata:
    """Metadata about query execution."""

    query_text: str
    execution_time_ms: int
    repositories_searched: int
    timeout_occurred: bool

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "query_text": self.query_text,
            "execution_time_ms": self.execution_time_ms,
            "repositories_searched": self.repositories_searched,
            "timeout_occurred": self.timeout_occurred,
        }


class SemanticQueryManager:
    """
    Manages semantic queries for CIDX server users.

    Provides semantic search capabilities with user isolation, background job integration,
    and proper resource management. Integrates with existing cidx query functionality.
    """

    def __init__(
        self,
        data_dir: Optional[str] = None,
        activated_repo_manager: Optional[ActivatedRepoManager] = None,
        background_job_manager: Optional[BackgroundJobManager] = None,
        query_timeout_seconds: int = 30,
        max_concurrent_queries_per_user: int = 5,
        max_results_per_query: int = 100,
    ):
        """
        Initialize semantic query manager.

        Args:
            data_dir: Data directory path (defaults to ~/.cidx-server/data)
            activated_repo_manager: Activated repo manager instance
            background_job_manager: Background job manager instance
            query_timeout_seconds: Query timeout in seconds
            max_concurrent_queries_per_user: Maximum concurrent queries per user
            max_results_per_query: Maximum results per query
        """
        if data_dir:
            self.data_dir = data_dir
        else:
            home_dir = Path.home()
            self.data_dir = str(home_dir / ".cidx-server" / "data")

        self.activated_repo_manager = activated_repo_manager or ActivatedRepoManager(
            data_dir
        )
        self.background_job_manager = background_job_manager or BackgroundJobManager()

        self.query_timeout_seconds = query_timeout_seconds
        self.max_concurrent_queries_per_user = max_concurrent_queries_per_user
        self.max_results_per_query = max_results_per_query

        # Set up logging
        self.logger = logging.getLogger(__name__)

        # Track concurrent queries per user (in production this would need persistence)
        self._active_queries_per_user: Dict[str, int] = {}

    def _is_composite_repository(self, repo_path: Path) -> bool:
        """
        Check if repository is in proxy mode (composite).

        Args:
            repo_path: Path to the repository

        Returns:
            True if repository is composite (proxy_mode=true), False otherwise

        Raises:
            json.JSONDecodeError: If config file contains invalid JSON
        """
        config_file = repo_path / ".code-indexer" / "config.json"
        if not config_file.exists():
            self.logger.debug(
                f"Config file not found at {config_file}, defaulting to single repository mode",
                extra={"correlation_id": get_correlation_id()},
            )
            return False

        try:
            config = json.loads(config_file.read_text())
            is_composite = bool(config.get("proxy_mode", False))
            self.logger.debug(
                f"Repository at {repo_path} detected as {'composite' if is_composite else 'single'}",
                extra={"correlation_id": get_correlation_id()},
            )
            return is_composite
        except json.JSONDecodeError as e:
            logger.error(
                format_error_log(
                    "QUERY-MIGRATE-001",
                    "Invalid JSON in config file",
                    config_file=config_file,
                    error=str(e),
                ),
                extra=get_log_extra("QUERY-MIGRATE-001"),
            )
            raise

    def search(
        self,
        repo_path: Path,
        query: str,
        limit: int = 10,
        min_score: Optional[float] = None,
        file_extensions: Optional[List[str]] = None,
        **kwargs,
    ) -> List[QueryResult]:
        """
        Main entry point for semantic search - routes to appropriate handler.

        Args:
            repo_path: Path to the repository
            query: Query text
            limit: Maximum results to return
            min_score: Minimum similarity score threshold
            file_extensions: List of file extensions to filter results
            **kwargs: Additional keyword arguments

        Returns:
            List of QueryResult objects

        Raises:
            SemanticQueryError: If routing or search fails
        """
        try:
            if self._is_composite_repository(repo_path):
                self.logger.info(
                    f"Routing query to composite handler for repository: {repo_path}",
                    extra={"correlation_id": get_correlation_id()},
                )
                return self.search_composite(
                    repo_path,
                    query,
                    limit=limit,
                    min_score=min_score,
                    file_extensions=file_extensions,
                    **kwargs,
                )

            self.logger.info(
                f"Routing query to single repository handler for: {repo_path}",
                extra={"correlation_id": get_correlation_id()},
            )
            return self.search_single(
                repo_path,
                query,
                limit=limit,
                min_score=min_score,
                file_extensions=file_extensions,
                **kwargs,
            )
        except Exception as e:
            logger.error(
                format_error_log(
                    "QUERY-MIGRATE-002",
                    "Search routing failed for repository",
                    repo_path=repo_path,
                    error=str(e),
                ),
                extra=get_log_extra("QUERY-MIGRATE-002"),
            )
            raise

    def search_single(
        self,
        repo_path: Path,
        query: str,
        limit: int = 10,
        min_score: Optional[float] = None,
        file_extensions: Optional[List[str]] = None,
        repository_alias: Optional[str] = None,
        **kwargs,
    ) -> List[QueryResult]:
        """
        Search a single repository (existing logic).

        Args:
            repo_path: Path to the repository
            query: Query text
            limit: Result limit
            min_score: Score threshold
            file_extensions: List of file extensions to filter results
            repository_alias: Repository alias for result annotation (defaults to repo name)
            **kwargs: Additional keyword arguments

        Returns:
            List of QueryResult objects from this repository
        """
        # If no alias provided, use the repository directory name
        if repository_alias is None:
            repository_alias = repo_path.name

        # This is the existing _search_single_repository logic
        return self._search_single_repository(
            str(repo_path), repository_alias, query, limit, min_score, file_extensions
        )

    def search_composite(
        self,
        repo_path: Path,
        query: str,
        limit: int = 10,
        min_score: Optional[float] = None,
        file_extensions: Optional[List[str]] = None,
        **kwargs,
    ) -> List[QueryResult]:
        """
        Search a composite repository using CLI's _execute_query.

        This is a thin wrapper around the CLI's existing parallel query
        execution infrastructure. It converts server parameters to CLI args,
        calls _execute_query, and parses the output.

        Args:
            repo_path: Path to the composite repository
            query: Query text
            limit: Maximum results to return
            min_score: Minimum similarity score threshold
            file_extensions: List of file extensions to filter results
            **kwargs: Additional keyword arguments (language, path, accuracy)

        Returns:
            List of QueryResult objects from all subrepos

        Raises:
            Exception: If CLI execution or parsing fails
        """
        self.logger.info(
            f"Composite repository search for {repo_path} using CLI integration",
            extra={"correlation_id": get_correlation_id()},
        )

        # Execute query using CLI integration
        return self._execute_cli_query(
            repo_path=repo_path,
            query=query,
            limit=limit,
            min_score=min_score,
            language=kwargs.get("language"),
            path=kwargs.get("path_filter"),
            accuracy=kwargs.get("accuracy"),
            exclude_language=kwargs.get("exclude_language"),
            exclude_path=kwargs.get("exclude_path"),
        )

    def query_user_repositories(
        self,
        username: str,
        query_text: str,
        repository_alias: Optional[str] = None,
        limit: int = 10,
        min_score: Optional[float] = None,
        file_extensions: Optional[List[str]] = None,
        language: Optional[str] = None,
        exclude_language: Optional[str] = None,
        path_filter: Optional[str] = None,
        exclude_path: Optional[str] = None,
        accuracy: Optional[str] = None,
        # Search mode parameter (Story #503 - FTS Bug Fix)
        search_mode: str = "semantic",
        # Temporal query parameters (Story #446)
        time_range: Optional[str] = None,
        time_range_all: bool = False,
        at_commit: Optional[str] = None,
        include_removed: bool = False,
        show_evolution: bool = False,
        evolution_limit: Optional[int] = None,
        # FTS-specific parameters (Story #503 Phase 2)
        case_sensitive: bool = False,
        fuzzy: bool = False,
        edit_distance: int = 0,
        snippet_lines: int = 5,
        regex: bool = False,
        # Temporal filtering parameters (Story #503 Phase 3)
        diff_type: Optional[Union[str, List[str]]] = None,
        author: Optional[str] = None,
        chunk_type: Optional[str] = None,
        # Query strategy parameters (Story #488 Phase 4)
        query_strategy: Optional[str] = None,
        score_fusion: Optional[str] = None,
        # Multi-provider routing (Story #593)
        preferred_provider: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Perform semantic query on user's activated repositories.

        Args:
            username: Username performing the query
            query_text: Natural language query text
            repository_alias: Specific repository to query (optional)
            limit: Maximum results to return
            min_score: Minimum similarity score threshold
            file_extensions: List of file extensions to filter results (e.g., ['.py', '.js'])
            language: Filter by programming language (e.g., 'python', 'js', 'typescript')
            exclude_language: Exclude files of specified language
            path_filter: Filter by file path pattern using glob syntax (e.g., '*/tests/*')
            exclude_path: Exclude files matching path pattern (e.g., '*/node_modules/*')
            accuracy: Search accuracy profile ('fast', 'balanced', 'high')
            search_mode: Search mode - 'semantic' (default), 'fts', or 'hybrid'
            time_range: Time range filter for temporal queries (format: YYYY-MM-DD..YYYY-MM-DD)
            time_range_all: Query across all git history without time range limit
            at_commit: Query code at specific commit hash or ref
            include_removed: Include files removed from current HEAD
            show_evolution: Include code evolution timeline with diffs
            evolution_limit: Limit evolution entries (user-controlled)
            case_sensitive: Enable case-sensitive FTS matching (FTS-only)
            fuzzy: Enable fuzzy matching with edit distance 1 (FTS-only, incompatible with regex)
            edit_distance: Fuzzy match tolerance level 0-3 (FTS-only)
            snippet_lines: Context lines around FTS matches 0-50 (FTS-only)
            regex: Interpret query as regex pattern (FTS-only, incompatible with fuzzy)

        Returns:
            Dictionary with results, total_results, and query_metadata

        Raises:
            SemanticQueryError: If query validation fails or repositories not found
        """
        # Validate query parameters
        self._validate_query_parameters(query_text, limit, min_score)

        # Get user's activated repositories
        user_repos = self.activated_repo_manager.list_activated_repositories(username)

        # ALSO get global repos from BackendRegistry (cluster-aware, database-backed)
        global_repos_list = []
        try:
            from code_indexer.server import app as app_module

            backend_registry = getattr(app_module.app.state, "backend_registry", None)
            if backend_registry:
                global_repos = list(backend_registry.global_repos.list_repos().values())

                # Format global repos to match user_repos structure
                for global_repo in global_repos:
                    global_repos_list.append(
                        {
                            "user_alias": global_repo["alias_name"],
                            "username": "global",
                            "is_global": True,
                            "repo_url": global_repo.get("repo_url", ""),
                        }
                    )
        except Exception as e:
            # Log but don't fail if global repos can't be loaded
            logger.warning(
                format_error_log(
                    "QUERY-MIGRATE-003", "Failed to load global repos", error=str(e)
                ),
                extra=get_log_extra("QUERY-MIGRATE-003"),
            )

        # Merge user repos and global repos
        all_repos = user_repos + global_repos_list

        if not all_repos:
            raise SemanticQueryError(
                f"No activated repositories found for user '{username}'"
            )

        # Filter to specific repository if requested
        if repository_alias:
            all_repos = [
                repo for repo in all_repos if repo["user_alias"] == repository_alias
            ]
            if not all_repos:
                raise SemanticQueryError(
                    f"Repository '{repository_alias}' not found for user '{username}'"
                )

        # Perform the search
        start_time = time.time()
        try:
            results = self._perform_search(
                username,
                all_repos,
                query_text,
                limit,
                min_score,
                file_extensions,
                language,
                exclude_language,
                path_filter,
                exclude_path,
                accuracy,
                # Search mode (Story #503 - FTS Bug Fix)
                search_mode=search_mode,
                # Temporal parameters (Story #446)
                time_range=time_range,
                time_range_all=time_range_all,
                at_commit=at_commit,
                include_removed=include_removed,
                show_evolution=show_evolution,
                evolution_limit=evolution_limit,
                # FTS-specific parameters (Story #503 Phase 2)
                case_sensitive=case_sensitive,
                fuzzy=fuzzy,
                edit_distance=edit_distance,
                snippet_lines=snippet_lines,
                regex=regex,
                # Temporal filtering parameters (Story #503 Phase 3)
                diff_type=diff_type,
                author=author,
                chunk_type=chunk_type,
                # Query strategy parameters (Story #488 Phase 4)
                query_strategy=query_strategy,
                score_fusion=score_fusion,
                # Multi-provider routing (Story #593)
                preferred_provider=preferred_provider,
            )
            execution_time_ms = int((time.time() - start_time) * 1000)
            timeout_occurred = False
        except TimeoutError as e:
            execution_time_ms = int((time.time() - start_time) * 1000)
            timeout_occurred = True
            raise SemanticQueryError(f"Query timed out: {str(e)}")
        except ValueError:
            # Propagate ValueError (e.g., temporal validation errors like invalid date format)
            execution_time_ms = int((time.time() - start_time) * 1000)
            raise
        except Exception as e:
            # Handle other exceptions that might indicate timeout or search failures
            execution_time_ms = int((time.time() - start_time) * 1000)
            if "timeout" in str(e).lower():
                raise SemanticQueryError(f"Query timed out: {str(e)}")
            raise SemanticQueryError(f"Search failed: {str(e)}")

        # Create metadata
        metadata = QueryMetadata(
            query_text=query_text,
            execution_time_ms=execution_time_ms,
            repositories_searched=len(all_repos),
            timeout_occurred=timeout_occurred,
        )

        # Handle case where mocked _perform_search returns dict instead of QueryResult list
        if isinstance(results, dict) and "results" in results:
            return results

        # Ensure results are QueryResult objects for normal list responses
        if results and len(results) > 0 and not isinstance(results[0], QueryResult):
            # This shouldn't happen in normal operation, but handle gracefully
            logger.warning(
                format_error_log(
                    "QUERY-MIGRATE-004", "Unexpected result format in query response"
                ),
                extra=get_log_extra("QUERY-MIGRATE-004"),
            )

        # Check if temporal parameters were used but no results (graceful fallback)
        has_temporal_params = any(
            [time_range, time_range_all, at_commit, show_evolution]
        )
        warning_message = None
        if has_temporal_params and len(results) == 0:
            warning_message = (
                "Temporal index not available. Showing results from current code only. "
                "Build temporal index with 'cidx index --index-commits' to enable temporal queries."
            )

        # Build response with temporal context in results
        response_results = []
        for r in results:
            result_dict = r.to_dict()
            # Add temporal_context if present
            if hasattr(r, "_temporal_context"):
                result_dict["temporal_context"] = getattr(r, "_temporal_context")
            response_results.append(result_dict)

        response = {
            "results": response_results,
            "total_results": len(results),
            "query_metadata": metadata.to_dict(),
        }

        # Add warning if temporal fallback occurred
        if warning_message:
            response["warning"] = warning_message

        return response

    def submit_query_job(
        self,
        username: str,
        query_text: str,
        repository_alias: Optional[str] = None,
        limit: int = 10,
        min_score: Optional[float] = None,
        file_extensions: Optional[List[str]] = None,
    ) -> str:
        """
        Submit a semantic query as a background job.

        Args:
            username: Username performing the query
            query_text: Natural language query text
            repository_alias: Specific repository to query (optional)
            limit: Maximum results to return
            min_score: Minimum similarity score threshold
            file_extensions: List of file extensions to filter results (e.g., ['.py', '.js'])

        Returns:
            Job ID for tracking query progress
        """
        # Submit background job
        job_id = self.background_job_manager.submit_job(
            "semantic_query",
            self.query_user_repositories,  # type: ignore[arg-type]
            username=username,
            query_text=query_text,
            repository_alias=repository_alias,
            limit=limit,
            min_score=min_score,
            file_extensions=file_extensions,
            submitter_username=username,
            repo_alias=repository_alias,  # AC5: Fix unknown repo bug
        )

        self.logger.info(
            f"Semantic query job {job_id} submitted for user {username}",
            extra={"correlation_id": get_correlation_id()},
        )
        return job_id

    def get_query_job_status(
        self, job_id: str, username: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get status of a background query job with user isolation.

        Args:
            job_id: Job ID to check status for
            username: Username for job authorization

        Returns:
            Job status dictionary or None if job not found or not authorized
        """
        return self.background_job_manager.get_job_status(job_id, username)

    def _validate_query_parameters(
        self, query_text: str, limit: int, min_score: Optional[float]
    ) -> None:
        """
        Validate query parameters.

        Args:
            query_text: Query text to validate
            limit: Result limit to validate
            min_score: Score threshold to validate

        Raises:
            SemanticQueryError: If parameters are invalid
        """
        if not query_text or not query_text.strip():
            raise SemanticQueryError("Query text cannot be empty")

        if limit <= 0:
            raise SemanticQueryError("Limit must be greater than 0")

        if min_score is not None and (min_score < 0.0 or min_score > 1.0):
            raise SemanticQueryError("Min score must be between 0.0 and 1.0")

    def _perform_search(
        self,
        username: str,
        user_repos: List[Dict[str, Any]],
        query_text: str,
        limit: int,
        min_score: Optional[float],
        file_extensions: Optional[List[str]],
        language: Optional[str] = None,
        exclude_language: Optional[str] = None,
        path_filter: Optional[str] = None,
        exclude_path: Optional[str] = None,
        accuracy: Optional[str] = None,
        # Search mode parameter (Story #503 - FTS Bug Fix)
        search_mode: str = "semantic",
        # Temporal parameters (Story #446)
        time_range: Optional[str] = None,
        time_range_all: bool = False,
        at_commit: Optional[str] = None,
        include_removed: bool = False,
        show_evolution: bool = False,
        evolution_limit: Optional[int] = None,
        # FTS-specific parameters (Story #503 Phase 2)
        case_sensitive: bool = False,
        fuzzy: bool = False,
        edit_distance: int = 0,
        snippet_lines: int = 5,
        regex: bool = False,
        # Temporal filtering parameters (Story #503 Phase 3)
        diff_type: Optional[Union[str, List[str]]] = None,
        author: Optional[str] = None,
        chunk_type: Optional[str] = None,
        # Query strategy parameters (Story #488 Phase 4)
        query_strategy: Optional[str] = None,
        score_fusion: Optional[str] = None,
        # Multi-provider routing (Story #593)
        preferred_provider: Optional[str] = None,
    ) -> List[QueryResult]:
        """
        Perform the actual search across user repositories.

        Supports three search modes:
        - 'semantic': Vector-based semantic similarity search (default)
        - 'fts': Full-text search using Tantivy index
        - 'hybrid': Combined FTS + semantic search with result fusion

        Args:
            username: Username performing the query
            user_repos: List of user's activated repositories
            query_text: Query text
            limit: Result limit
            min_score: Score threshold
            file_extensions: List of file extensions to filter results
            language: Filter by programming language
            exclude_language: Exclude files of specified language
            path_filter: Filter by file path pattern
            exclude_path: Exclude files matching path pattern
            accuracy: Search accuracy profile
            search_mode: Search mode - 'semantic' (default), 'fts', or 'hybrid'
            time_range: Time range filter for temporal queries
            time_range_all: Query across all git history without time range limit
            at_commit: Query at specific commit
            include_removed: Include removed files
            show_evolution: Include evolution timeline
            evolution_limit: Limit evolution entries
            case_sensitive: Enable case-sensitive FTS matching
            fuzzy: Enable fuzzy matching
            edit_distance: Fuzzy match tolerance 0-3
            snippet_lines: Context lines around FTS matches 0-50
            regex: Interpret query as regex pattern

        Returns:
            List of QueryResult objects sorted by similarity score
        """
        # Story #4 AC2: Track search metrics at service layer
        # This ensures both MCP and REST API calls are counted
        from code_indexer.server.services.api_metrics_service import api_metrics_service

        if search_mode == "semantic":
            api_metrics_service.increment_semantic_search()
        else:
            # FTS, hybrid, or temporal searches
            api_metrics_service.increment_other_index_search()

        all_results: List[QueryResult] = []
        repo_errors: List[str] = []

        # Search each repository
        for repo_info in user_repos:
            try:
                repo_alias = repo_info["user_alias"]

                # Handle global repos differently - resolve via AliasManager
                if repo_info.get("is_global"):
                    from code_indexer.global_repos.alias_manager import AliasManager

                    data_dir = Path(
                        self.activated_repo_manager.activated_repos_dir
                    ).parent
                    aliases_dir = data_dir / "golden-repos" / "aliases"
                    alias_manager = AliasManager(str(aliases_dir))

                    target_path = alias_manager.read_alias(repo_alias)
                    if not target_path:
                        logger.warning(
                            format_error_log(
                                "QUERY-MIGRATE-005",
                                "Global repo alias could not be resolved, skipping",
                                repo_alias=repo_alias,
                            ),
                            extra=get_log_extra("QUERY-MIGRATE-005"),
                        )
                        continue  # Skip if alias can't be resolved

                    repo_path = target_path
                # Check if repo_path is already provided
                elif "repo_path" in repo_info and repo_info["repo_path"]:
                    repo_path = repo_info["repo_path"]
                else:
                    # Fall back to activated repo manager for regular activated repos
                    repo_path = self.activated_repo_manager.get_activated_repo_path(
                        username, repo_alias
                    )

                # Create temporary config and search engine for this repository
                # This would need actual implementation with proper config management
                results = self._search_single_repository(
                    repo_path,
                    repo_alias,
                    query_text,
                    limit,
                    min_score,
                    file_extensions,
                    language,
                    exclude_language,
                    path_filter,
                    exclude_path,
                    accuracy,
                    # Search mode (Story #503 - FTS Bug Fix)
                    search_mode=search_mode,
                    # Temporal parameters (Story #446)
                    time_range=time_range,
                    time_range_all=time_range_all,
                    at_commit=at_commit,
                    include_removed=include_removed,
                    show_evolution=show_evolution,
                    evolution_limit=evolution_limit,
                    # FTS-specific parameters (Story #503 Phase 2)
                    case_sensitive=case_sensitive,
                    fuzzy=fuzzy,
                    edit_distance=edit_distance,
                    snippet_lines=snippet_lines,
                    regex=regex,
                    # Temporal filtering parameters (Story #503 Phase 3)
                    diff_type=diff_type,
                    author=author,
                    chunk_type=chunk_type,
                    # Query strategy parameters (Story #488 Phase 4)
                    query_strategy=query_strategy,
                    score_fusion=score_fusion,
                    # Multi-provider routing (Story #593)
                    preferred_provider=preferred_provider,
                )
                all_results.extend(results)

            except (TimeoutError, Exception) as e:
                # If it's a timeout or other critical error from one repo, propagate it
                if isinstance(e, TimeoutError) or "timeout" in str(e).lower():
                    raise TimeoutError(
                        f"Query timed out while searching repository {repo_info['user_alias']}: {str(e)}"
                    )
                # Propagate ValueError (e.g., temporal validation errors like invalid date format)
                if isinstance(e, ValueError):
                    raise
                # For other errors, log warning and continue with other repos
                logger.warning(
                    format_error_log(
                        "QUERY-MIGRATE-006",
                        "Failed to search repository",
                        repo_alias=repo_info["user_alias"],
                        error=str(e),
                    ),
                    extra=get_log_extra("QUERY-MIGRATE-006"),
                )
                repo_errors.append(str(e))
                continue

        # If ALL repos failed and we got zero results, propagate the error
        # instead of silently returning empty results
        if not all_results and repo_errors:
            raise Exception(repo_errors[-1])

        # Sort by similarity score (descending) and limit results
        all_results.sort(key=lambda r: r.similarity_score, reverse=True)

        # Apply global result limit
        effective_limit = min(limit, self.max_results_per_query)
        return all_results[:effective_limit]

    def _both_providers_configured(self, repo_path: str) -> bool:
        """Check if both VoyageAI and Cohere providers have API keys configured."""
        try:
            from code_indexer.services.embedding_factory import (
                EmbeddingProviderFactory,
            )

            # Try repo config first; fall back to empty config on failure.
            # get_configured_providers checks env vars first, so it works
            # even with an empty config (common for server-managed repos
            # where versioned snapshots have incomplete local configs).
            try:
                from code_indexer.config import ConfigManager

                config = ConfigManager.create_with_backtrack(
                    Path(repo_path)
                ).get_config()
            except Exception as cfg_exc:
                logger.debug(
                    "Config load failed for %s, using env-var fallback: %s",
                    repo_path,
                    cfg_exc,
                    extra=get_log_extra("QUERY-STRATEGY-003"),
                )
                config = type("EmptyConfig", (), {})()

            configured = EmbeddingProviderFactory.get_configured_providers(config)
            return "voyage-ai" in configured and "cohere" in configured
        except Exception as exc:
            logger.warning(
                "Provider config check failed for %s: %s",
                repo_path,
                exc,
                extra=get_log_extra("QUERY-STRATEGY-003"),
            )
            return False

    def _search_single_repository(
        self,
        repo_path: str,
        repository_alias: str,
        query_text: str,
        limit: int,
        min_score: Optional[float],
        file_extensions: Optional[List[str]],
        language: Optional[str] = None,
        exclude_language: Optional[str] = None,
        path_filter: Optional[str] = None,
        exclude_path: Optional[str] = None,
        accuracy: Optional[str] = None,
        # Search mode parameter (Story #503 - FTS Bug Fix)
        search_mode: str = "semantic",
        # Temporal parameters (Story #446)
        time_range: Optional[str] = None,
        time_range_all: bool = False,
        at_commit: Optional[str] = None,
        include_removed: bool = False,
        show_evolution: bool = False,
        evolution_limit: Optional[int] = None,
        # FTS-specific parameters (Story #503 Phase 2)
        case_sensitive: bool = False,
        fuzzy: bool = False,
        edit_distance: int = 0,
        snippet_lines: int = 5,
        regex: bool = False,
        # Temporal filtering parameters (Story #503 Phase 3)
        diff_type: Optional[Union[str, List[str]]] = None,
        author: Optional[str] = None,
        chunk_type: Optional[str] = None,
        # Query strategy parameters (Story #488 Phase 4)
        query_strategy: Optional[str] = None,
        score_fusion: Optional[str] = None,
        # Multi-provider routing (Story #593)
        preferred_provider: Optional[str] = None,
    ) -> List[QueryResult]:
        """
        Search a single repository using the appropriate search service.

        Supports three search modes:
        - 'semantic': Vector-based semantic similarity search (default)
        - 'fts': Full-text search using Tantivy index
        - 'hybrid': Combined FTS + semantic search with result fusion

        For temporal queries (when time_range, at_commit, or show_evolution provided),
        uses TemporalSearchService with graceful fallback to regular search if temporal
        index not available.

        For composite repositories (proxy_mode=true), delegates to CLI integration
        which supports all filter parameters (language, exclude_language, path_filter,
        exclude_path, accuracy).

        For regular repositories, uses SemanticSearchService with post-search filtering
        for file_extensions and min_score.

        Args:
            repo_path: Path to the repository
            repository_alias: Repository alias for result annotation
            query_text: Query text
            limit: Result limit
            min_score: Score threshold
            file_extensions: List of file extensions to filter results
            language: Filter by programming language
            exclude_language: Exclude files of specified language
            path_filter: Filter by file path pattern
            exclude_path: Exclude files matching path pattern
            accuracy: Search accuracy profile
            search_mode: Search mode - 'semantic' (default), 'fts', or 'hybrid'
            time_range: Time range filter for temporal queries
            time_range_all: Query across all git history without time range limit
            at_commit: Query at specific commit
            include_removed: Include removed files
            show_evolution: Include evolution timeline
            evolution_limit: Limit evolution entries
            case_sensitive: Enable case-sensitive FTS matching
            fuzzy: Enable fuzzy matching
            edit_distance: Fuzzy match tolerance 0-3
            snippet_lines: Context lines around FTS matches 0-50
            regex: Interpret query as regex pattern

        Returns:
            List of QueryResult objects from this repository
        """
        # Story #593: Handle SPECIFIC strategy routing before composite check
        if query_strategy == "specific" or preferred_provider:
            if not preferred_provider:
                raise ValueError("preferred_provider required for specific strategy")
            _supported_providers = {"voyage-ai", "cohere"}
            if preferred_provider not in _supported_providers:
                raise ValueError(
                    f"Provider '{preferred_provider}' not available. "
                    f"Supported providers: {sorted(_supported_providers)}"
                )
            results = self._search_with_provider(
                repo_path=repo_path,
                repository_alias=repository_alias,
                query_text=query_text,
                limit=limit,
                min_score=min_score,
                file_extensions=file_extensions,
                language=language,
                exclude_language=exclude_language,
                path_filter=path_filter,
                exclude_path=exclude_path,
                accuracy=accuracy,
                provider_name=preferred_provider,
            )
            for r in results:
                r.source_provider = preferred_provider
            return results

        # Story #618: Auto-default to parallel when both providers configured
        if query_strategy is None:
            if self._both_providers_configured(repo_path):
                query_strategy = "parallel"
                score_fusion = score_fusion or "rrf"
            else:
                query_strategy = "primary_only"

        # Log query strategy if non-default (Story #488 Phase 4)
        if query_strategy is not None and query_strategy != "primary_only":
            logger.info(
                "Query strategy '%s' requested",
                query_strategy,
                extra=get_log_extra("QUERY-STRATEGY-001"),
            )

        # Story #619: Execute failover query — primary (voyage-ai) with secondary
        # (cohere) fallback on error.
        if query_strategy == "failover":
            from code_indexer.services.query_strategy import (
                execute_failover_query,
                QueryResult as StrategyQueryResult,
            )

            def _primary() -> List[QueryResult]:
                return self._search_with_provider(
                    repo_path=repo_path,
                    repository_alias=repository_alias,
                    query_text=query_text,
                    limit=limit,
                    min_score=None,
                    file_extensions=file_extensions,
                    language=language,
                    exclude_language=exclude_language,
                    path_filter=path_filter,
                    exclude_path=exclude_path,
                    accuracy=accuracy,
                    provider_name="voyage-ai",
                )

            def _secondary() -> List[QueryResult]:
                return self._search_with_provider(
                    repo_path=repo_path,
                    repository_alias=repository_alias,
                    query_text=query_text,
                    limit=limit,
                    min_score=None,
                    file_extensions=file_extensions,
                    language=language,
                    exclude_language=exclude_language,
                    path_filter=path_filter,
                    exclude_path=exclude_path,
                    accuracy=accuracy,
                    provider_name="cohere",
                )

            def _to_strategy_failover(r: QueryResult) -> StrategyQueryResult:
                return StrategyQueryResult(
                    file_path=r.file_path,
                    score=r.similarity_score,
                    content=r.code_snippet,
                    chunk_id=f"{r.file_path}:{r.line_number}",
                    repository_alias=r.repository_alias,
                    source_provider=r.source_provider,
                )

            def _primary_strategy() -> List[StrategyQueryResult]:
                return [_to_strategy_failover(r) for r in _primary()]

            def _secondary_strategy() -> List[StrategyQueryResult]:
                return [_to_strategy_failover(r) for r in _secondary()]

            failover_strategy_results = execute_failover_query(
                _primary_strategy, _secondary_strategy, limit=limit
            )

            all_failover: List[QueryResult] = []
            for s in failover_strategy_results:
                all_failover.append(
                    QueryResult(
                        file_path=s.file_path,
                        line_number=1,
                        code_snippet=s.content,
                        similarity_score=s.score,
                        repository_alias=s.repository_alias,
                        source_provider=s.source_provider,
                    )
                )

            if min_score is not None:
                all_failover = [
                    r for r in all_failover if r.similarity_score >= min_score
                ]

            return all_failover[:limit]

        # Bug #614 fix: Execute parallel query against both providers.
        # Bug #615 fix: Pass min_score=None to each provider (correct sentinel —
        # _search_with_provider checks `if min_score is not None`). Apply user
        # min_score AFTER fusion on the merged output.
        if query_strategy == "parallel":
            from code_indexer.services.query_strategy import (
                fuse_rrf,
                fuse_multiply,
                fuse_average,
                QueryResult as StrategyQueryResult,
            )

            # Story #638: Over-fetch each provider to widen the candidate pool
            # before score-gated filtering and fusion.
            _provider_fetch_limit = min(
                limit * PARALLEL_FETCH_MULTIPLIER, MAX_PARALLEL_FETCH
            )

            # Story #619 Gap 1: health-gated parallel dispatch — skip "down" providers
            _health_monitor = ProviderHealthMonitor.get_instance()
            _all_providers = [
                (
                    "voyage-ai",
                    dict(
                        repo_path=repo_path,
                        repository_alias=repository_alias,
                        query_text=query_text,
                        limit=_provider_fetch_limit,
                        min_score=None,
                        file_extensions=file_extensions,
                        language=language,
                        exclude_language=exclude_language,
                        path_filter=path_filter,
                        exclude_path=exclude_path,
                        accuracy=accuracy,
                        provider_name="voyage-ai",
                    ),
                ),
                (
                    "cohere",
                    dict(
                        repo_path=repo_path,
                        repository_alias=repository_alias,
                        query_text=query_text,
                        limit=_provider_fetch_limit,
                        min_score=None,
                        file_extensions=file_extensions,
                        language=language,
                        exclude_language=exclude_language,
                        path_filter=path_filter,
                        exclude_path=exclude_path,
                        accuracy=accuracy,
                        provider_name="cohere",
                    ),
                ),
            ]
            provider_tasks: Dict[str, Any] = {}
            _degraded_in_query: List[str] = []
            for _pname, _kwargs in _all_providers:
                _health_info = _health_monitor.get_health(_pname)
                _pstatus = _health_info.get(_pname)
                if _pstatus is not None and _pstatus.status == "down":
                    logger.warning(
                        "Skipping provider '%s' (status: down) in parallel dispatch",
                        _pname,
                        extra=get_log_extra("QUERY-STRATEGY-003"),
                    )
                    _degraded_in_query.append(_pname)
                    continue
                _captured_kwargs = _kwargs
                provider_tasks[_pname] = (
                    lambda _kw=_captured_kwargs: self._search_with_provider(**_kw)
                )

            primary_results: List[QueryResult] = []
            secondary_results: List[QueryResult] = []
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {
                    executor.submit(fn): name for name, fn in provider_tasks.items()
                }
                try:
                    for future in as_completed(
                        futures, timeout=PARALLEL_TIMEOUT_SECONDS
                    ):
                        provider_name = futures[future]
                        try:
                            batch = future.result()
                            if provider_name == "voyage-ai":
                                primary_results = batch
                            else:
                                secondary_results = batch
                        except Exception as _e:
                            logger.warning(
                                "Parallel query provider '%s' failed: %s",
                                provider_name,
                                _e,
                                extra=get_log_extra("QUERY-STRATEGY-002"),
                            )
                except concurrent.futures.TimeoutError:
                    for future in futures:
                        future.cancel()
                    logger.warning(
                        "Parallel query timed out after %ds; some providers did not respond",
                        PARALLEL_TIMEOUT_SECONDS,
                        extra=get_log_extra("QUERY-STRATEGY-004"),
                    )

            # Story #638: Symmetric score-gated filtering — cull weak provider
            # results before fusion to prevent low-quality candidates from diluting
            # high-quality results. Operates on raw similarity_score values
            # (semantic QueryResult, not StrategyQueryResult).
            primary_results, secondary_results = apply_score_gate(
                primary_results, secondary_results, score_attr="similarity_score"
            )

            # Adapter: semantic QueryResult → query_strategy QueryResult.
            # chunk_id encodes file_path + line_number for deduplication.
            def _to_strategy(r: QueryResult) -> StrategyQueryResult:
                return StrategyQueryResult(
                    file_path=r.file_path,
                    score=r.similarity_score,
                    content=r.code_snippet,
                    chunk_id=f"{r.file_path}:{r.line_number}",
                    repository_alias=r.repository_alias,
                    source_provider=r.source_provider,
                )

            # Build one original_map keyed by the same key fuse_* uses:
            # f"{repository_alias}:{file_path}:{chunk_id}"
            original_map: Dict[str, QueryResult] = {}
            for r in primary_results + secondary_results:
                chunk_id = f"{r.file_path}:{r.line_number}"
                key = f"{r.repository_alias}:{r.file_path}:{chunk_id}"
                if key not in original_map:
                    original_map[key] = r

            s_primary = [_to_strategy(r) for r in primary_results]
            s_secondary = [_to_strategy(r) for r in secondary_results]

            # Select fusion function; default to RRF
            _fusion_methods = {
                "rrf": fuse_rrf,
                "multiply": fuse_multiply,
                "average": fuse_average,
            }
            _fuse_fn = _fusion_methods.get(score_fusion or "rrf", fuse_rrf)

            if not s_primary and not s_secondary:
                fused_strategy: List[StrategyQueryResult] = []
            elif not s_secondary:
                fused_strategy = s_primary[:limit]
            elif not s_primary:
                fused_strategy = s_secondary[:limit]
            else:
                fused_strategy = _fuse_fn(s_primary, s_secondary, limit)

            # Reverse adapter: strategy QueryResult → semantic QueryResult.
            # Look up original via the same key used in original_map, then
            # update score and source_provider (fused values).
            all_results: List[QueryResult] = []
            for s in fused_strategy:
                key = f"{s.repository_alias}:{s.file_path}:{s.chunk_id}"
                original = original_map.get(key)
                if original is not None:
                    # Keep original cosine similarity_score intact — min_score filtering
                    # compares against it. Fused ordering is already reflected in
                    # fused_strategy order; overwriting with RRF score (~0.016) would
                    # cause all results to be eliminated by any min_score > 0.016.
                    original.source_provider = s.source_provider
                    original.fusion_score = s.fusion_score
                    original.contributing_providers = s.contributing_providers
                    all_results.append(original)
                else:
                    all_results.append(
                        QueryResult(
                            file_path=s.file_path,
                            line_number=1,
                            code_snippet=s.content,
                            similarity_score=s.score,
                            repository_alias=s.repository_alias,
                            source_provider=s.source_provider,
                            fusion_score=s.fusion_score,
                            contributing_providers=s.contributing_providers,
                        )
                    )

            # Bug #615: Apply user min_score AFTER fusion (not per-provider)
            if min_score is not None:
                all_results = [
                    r for r in all_results if r.similarity_score >= min_score
                ]

            # Story #619 Gap 5: record which providers were skipped (down) this query
            if not hasattr(self, "_last_query_degraded_providers"):
                self._last_query_degraded_providers = []
            self._last_query_degraded_providers = _degraded_in_query

            return all_results[:limit]

        try:
            # Check if this is a composite repository
            repo_path_obj = Path(repo_path)
            if self._is_composite_repository(repo_path_obj):
                # Use CLI integration for composite repos (supports all filters)
                self.logger.debug(
                    f"Composite repository detected: {repo_path}. Using CLI integration for search.",
                    extra={"correlation_id": get_correlation_id()},
                )
                return self._execute_cli_query(
                    repo_path=repo_path_obj,
                    query=query_text,
                    limit=limit,
                    min_score=min_score,
                    language=language,
                    path=path_filter,
                    accuracy=accuracy,
                    exclude_language=exclude_language,
                    exclude_path=exclude_path,
                    # FTS-specific parameters (Story #503 Phase 2)
                    case_sensitive=case_sensitive,
                    fuzzy=fuzzy,
                    edit_distance=edit_distance,
                    snippet_lines=snippet_lines,
                    regex=regex,
                    # Temporal filtering parameters (Story #503 Phase 3)
                    diff_type=diff_type,
                    author=author,
                    chunk_type=chunk_type,
                )

            # TEMPORAL QUERY HANDLING (Story #446)
            # Check if temporal parameters are present
            has_temporal_params = any(
                [time_range, time_range_all, at_commit, show_evolution]
            )

            if has_temporal_params:
                return self._execute_temporal_query(
                    repo_path=repo_path_obj,
                    repository_alias=repository_alias,
                    query_text=query_text,
                    limit=limit,
                    min_score=min_score,
                    time_range=time_range,
                    time_range_all=time_range_all,
                    at_commit=at_commit,
                    include_removed=include_removed,
                    show_evolution=show_evolution,
                    evolution_limit=evolution_limit,
                    language=language,
                    exclude_language=exclude_language,
                    path_filter=path_filter,
                    exclude_path=exclude_path,
                )

            # FTS SEARCH HANDLING (Story #503 - FTS Bug Fix)
            # Execute FTS search when search_mode is 'fts' or 'hybrid'
            if search_mode in ["fts", "hybrid"]:
                fts_results = self._execute_fts_search(
                    repo_path=repo_path_obj,
                    repository_alias=repository_alias,
                    query_text=query_text,
                    limit=limit,
                    min_score=min_score,
                    language=language,
                    exclude_language=exclude_language,
                    path_filter=path_filter,
                    exclude_path=exclude_path,
                    case_sensitive=case_sensitive,
                    fuzzy=fuzzy,
                    edit_distance=edit_distance,
                    snippet_lines=snippet_lines,
                    regex=regex,
                )

                # For pure FTS mode, apply file_extensions filter and return
                if search_mode == "fts":
                    if file_extensions is not None:
                        fts_results = [
                            r
                            for r in fts_results
                            if Path(r.file_path).suffix.lower()
                            in [ext.lower() for ext in file_extensions]
                        ]
                    return fts_results

                # For hybrid mode, continue to semantic search and merge results
                # Fall through to semantic search below

            # SEMANTIC SEARCH
            # Import SemanticSearchService and related models
            from ..services.search_service import SemanticSearchService
            from ..models.api_models import SemanticSearchRequest

            # Create search service instance
            search_service = SemanticSearchService()

            # Create search request — Story #375: wire filter params through
            search_request = SemanticSearchRequest(
                query=query_text,
                limit=limit,
                include_source=True,
                path_filter=path_filter,
                language=language,
                exclude_language=exclude_language,
                exclude_path=exclude_path,
                accuracy=accuracy,
            )

            # Perform search on the repository using direct path
            search_response = search_service.search_repository_path(
                repo_path=repo_path, search_request=search_request
            )

            # Convert search results to QueryResult objects
            semantic_results = []
            for search_item in search_response.results:
                # Apply min_score filter if specified
                if min_score is not None and search_item.score < min_score:
                    continue

                # Apply file extension filter if specified
                if file_extensions is not None:
                    file_path = Path(search_item.file_path)
                    if file_path.suffix.lower() not in [
                        ext.lower() for ext in file_extensions
                    ]:
                        continue

                # Convert SearchResultItem to QueryResult
                # Annotate source_provider: named provider if given, else "primary" (Story #593)
                query_result = QueryResult(
                    file_path=search_item.file_path,
                    line_number=search_item.line_start,  # Use start line as line number
                    code_snippet=search_item.content,
                    similarity_score=search_item.score,
                    repository_alias=repository_alias,
                    source_repo=None,  # Single repository, no source_repo
                    source_provider=preferred_provider or "primary",
                )
                semantic_results.append(query_result)

            # For hybrid mode, merge FTS and semantic results
            if search_mode == "hybrid":
                return self._merge_hybrid_results(fts_results, semantic_results, limit)

            return semantic_results

        except Exception as e:
            logger.error(
                format_error_log(
                    "QUERY-MIGRATE-008",
                    "Failed to search repository",
                    repository_alias=repository_alias,
                    repo_path=str(repo_path),
                    error=str(e),
                ),
                extra=get_log_extra("QUERY-MIGRATE-008"),
            )
            # Re-raise exception to be handled by calling method
            raise

    def _search_with_provider(
        self,
        repo_path: str,
        repository_alias: str,
        query_text: str,
        limit: int,
        min_score: Optional[float],
        file_extensions: Optional[List[str]],
        language: Optional[str] = None,
        exclude_language: Optional[str] = None,
        path_filter: Optional[str] = None,
        exclude_path: Optional[str] = None,
        accuracy: Optional[str] = None,
        provider_name: Optional[str] = None,
    ) -> List[QueryResult]:
        """
        Search a single repository using an explicitly named embedding provider.

        This is the implementation backend for query_strategy='specific' (Story #593).
        Uses SemanticSearchService with a provider_name override so a named provider
        (e.g. 'cohere' or 'voyage-ai') is used instead of the repo-default provider.

        Args:
            repo_path: Path to the repository
            repository_alias: Repository alias for result annotation
            query_text: Query text
            limit: Result limit
            min_score: Score threshold
            file_extensions: List of file extensions to filter results
            language: Filter by programming language
            exclude_language: Exclude files of specified language
            path_filter: Filter by file path pattern
            exclude_path: Exclude files matching path pattern
            accuracy: Search accuracy profile
            provider_name: Embedding provider name override (e.g. 'cohere', 'voyage-ai')

        Returns:
            List of QueryResult objects from this repository
        """
        from ..services.search_service import SemanticSearchService
        from ..models.api_models import SemanticSearchRequest

        search_service = SemanticSearchService()
        search_request = SemanticSearchRequest(
            query=query_text,
            limit=limit,
            include_source=True,
            path_filter=path_filter,
            language=language,
            exclude_language=exclude_language,
            exclude_path=exclude_path,
            accuracy=accuracy,
        )
        search_response = search_service.search_repository_path_with_provider(
            repo_path=repo_path,
            search_request=search_request,
            provider_name=provider_name,
        )

        results = []
        for search_item in search_response.results:
            if min_score is not None and search_item.score < min_score:
                continue
            if file_extensions is not None:
                from pathlib import Path as _Path

                if _Path(search_item.file_path).suffix.lower() not in [
                    ext.lower() for ext in file_extensions
                ]:
                    continue
            results.append(
                QueryResult(
                    file_path=search_item.file_path,
                    line_number=search_item.line_start,
                    code_snippet=search_item.content,
                    similarity_score=search_item.score,
                    repository_alias=repository_alias,
                    source_repo=None,
                    source_provider=provider_name or "",
                )
            )
        return results

    def _build_cli_args(
        self,
        query: str,
        limit: int,
        min_score: Optional[float] = None,
        language: Optional[str] = None,
        path: Optional[str] = None,
        accuracy: Optional[str] = None,
        exclude_language: Optional[str] = None,
        exclude_path: Optional[str] = None,
        # FTS-specific parameters (Story #503 Phase 2)
        case_sensitive: bool = False,
        fuzzy: bool = False,
        edit_distance: int = 0,
        snippet_lines: int = 5,
        regex: bool = False,
        # Temporal filtering parameters (Story #503 Phase 3)
        diff_type: Optional[Union[str, List[str]]] = None,
        author: Optional[str] = None,
        chunk_type: Optional[str] = None,
    ) -> List[str]:
        """
        Convert server parameters to CLI args format.

        Args:
            query: Query text
            limit: Result limit
            min_score: Minimum score threshold
            language: Programming language filter
            path: Path pattern filter
            accuracy: Accuracy level (fast, balanced, high)
            exclude_language: Exclude specified language
            exclude_path: Exclude path pattern
            case_sensitive: Enable case-sensitive FTS matching
            fuzzy: Enable fuzzy matching
            edit_distance: Fuzzy match tolerance 0-3
            snippet_lines: Context lines around FTS matches 0-50
            regex: Interpret query as regex pattern

        Returns:
            List of CLI arguments
        """
        args = ["query", query]

        # Always set quiet mode for parsing
        args.append("--quiet")

        # Add limit
        args.extend(["--limit", str(limit)])

        # Add optional parameters
        if min_score is not None:
            args.extend(["--min-score", str(min_score)])

        if language is not None:
            args.extend(["--language", language])

        if path is not None:
            args.extend(["--path", path])

        if accuracy is not None:
            args.extend(["--accuracy", accuracy])

        if exclude_language is not None:
            args.extend(["--exclude-language", exclude_language])

        if exclude_path is not None:
            args.extend(["--exclude-path", exclude_path])

        # FTS-specific parameters (Story #503 Phase 2)
        if case_sensitive:
            args.append("--case-sensitive")

        if fuzzy:
            args.append("--fuzzy")

        if edit_distance > 0:
            args.extend(["--edit-distance", str(edit_distance)])

        if snippet_lines != 5:  # Only add if different from default
            args.extend(["--snippet-lines", str(snippet_lines)])

        if regex:
            args.append("--regex")

        # Temporal filtering parameters (Story #503 Phase 3)
        if diff_type is not None:
            # Handle diff_type: can be string, array, or comma-separated string
            if isinstance(diff_type, list):
                # Array: add --diff-type flag for each value
                for dt in diff_type:
                    args.extend(["--diff-type", dt])
            elif isinstance(diff_type, str):
                # String: check if comma-separated, split and add multiple flags
                if "," in diff_type:
                    for dt in diff_type.split(","):
                        args.extend(["--diff-type", dt.strip()])
                else:
                    # Single value
                    args.extend(["--diff-type", diff_type])

        if author is not None:
            args.extend(["--author", author])

        if chunk_type is not None:
            args.extend(["--chunk-type", chunk_type])

        return args

    def _execute_cli_query(
        self,
        repo_path: Path,
        query: str,
        limit: int,
        min_score: Optional[float] = None,
        language: Optional[str] = None,
        path: Optional[str] = None,
        accuracy: Optional[str] = None,
        exclude_language: Optional[str] = None,
        exclude_path: Optional[str] = None,
        # FTS-specific parameters (Story #503 Phase 2)
        case_sensitive: bool = False,
        fuzzy: bool = False,
        edit_distance: int = 0,
        snippet_lines: int = 5,
        regex: bool = False,
        # Temporal filtering parameters (Story #503 Phase 3)
        diff_type: Optional[Union[str, List[str]]] = None,
        author: Optional[str] = None,
        chunk_type: Optional[str] = None,
    ) -> List[QueryResult]:
        """
        Execute CLI query and parse results.

        This is a thin wrapper that:
        1. Loads ProxyConfigManager to get repository paths
        2. Converts parameters to CLI args
        3. Calls _execute_query from CLI
        4. Captures stdout and parses results
        5. Updates repository_alias to composite repo name

        Args:
            repo_path: Path to composite repository
            query: Query text
            limit: Result limit
            min_score: Score threshold
            language: Language filter
            path: Path filter
            accuracy: Accuracy level
            exclude_language: Exclude specified language
            exclude_path: Exclude path pattern

        Returns:
            List of QueryResult objects with:
                - repository_alias: Composite repo name (from repo_path)
                - source_repo: Component repo name (from file path)

        Raises:
            Exception: If ProxyConfigManager fails or CLI execution fails
        """
        # Load proxy configuration to get repository paths
        proxy_config_manager = ProxyConfigManager(repo_path)
        config = proxy_config_manager.load_config()
        discovered_repos = config.discovered_repos

        # Convert relative paths to absolute paths
        repo_paths = [str(repo_path / repo) for repo in discovered_repos]

        # Build CLI args
        args = self._build_cli_args(
            query=query,
            limit=limit,
            min_score=min_score,
            language=language,
            path=path,
            accuracy=accuracy,
            exclude_language=exclude_language,
            exclude_path=exclude_path,
            # FTS-specific parameters (Story #503 Phase 2)
            case_sensitive=case_sensitive,
            fuzzy=fuzzy,
            edit_distance=edit_distance,
            snippet_lines=snippet_lines,
            regex=regex,
            # Temporal filtering parameters (Story #503 Phase 3)
            diff_type=diff_type,
            author=author,
            chunk_type=chunk_type,
        )

        # Capture stdout
        old_stdout = sys.stdout
        sys.stdout = captured_output = io.StringIO()

        try:
            # Execute CLI query (this handles parallel execution, aggregation, etc.)
            _execute_query(args, repo_paths)

            # Get captured output
            cli_output = captured_output.getvalue()

        finally:
            # Restore stdout
            sys.stdout = old_stdout

        # Parse CLI output to QueryResult objects
        results = self._parse_cli_output(cli_output, repo_path)

        # Override repository_alias with composite repo name
        # (parser sets it to source_repo by default)
        composite_repo_name = repo_path.name
        for result in results:
            result.repository_alias = composite_repo_name

        return results

    def _parse_cli_output(self, cli_output: str, repo_path: Path) -> List[QueryResult]:
        """
        Parse CLI quiet mode output to QueryResult objects.

        CLI quiet mode format (from QueryResultAggregator):
            score path:line_range
              line_num: code
              line_num: code

            score path:line_range
              line_num: code

        For composite repositories, file_path includes subrepo prefix: "repo1/auth.py"
        For single repositories, file_path is just the path: "auth.py"

        Args:
            cli_output: CLI stdout output in quiet mode
            repo_path: Repository root path (used to determine composite repo name)

        Returns:
            List of QueryResult objects with source_repo populated for composite repos
        """
        if not cli_output or not cli_output.strip():
            return []

        results = []
        lines = cli_output.strip().split("\n")
        i = 0

        while i < len(lines):
            line = lines[i]

            # Skip empty lines
            if not line.strip():
                i += 1
                continue

            # Parse result header: "score path:line_range"
            # Example: "0.95 repo1/auth.py:10-20" (composite)
            # Example: "0.95 auth.py:10-20" (single)
            header_match = re.match(r"^([\d.]+)\s+(.+):(\d+)-(\d+)\s*$", line)

            if header_match:
                score = float(header_match.group(1))
                file_path = header_match.group(2)
                line_start = int(header_match.group(3))
                # line_end is part of the match but not used in QueryResult
                # (stored in code_snippet instead)

                # Extract source_repo from file path for composite repos
                # Format: "repo1/auth.py" -> source_repo is "repo1"
                # Format: "auth.py" -> source_repo is None (single repo)
                if "/" in file_path:
                    source_repo = file_path.split("/")[0]
                else:
                    source_repo = None

                # repository_alias will be set by calling context (search_composite)
                # For now, use source_repo as placeholder (will be updated by caller)
                repo_alias = source_repo if source_repo else repo_path.name

                # Collect code snippet lines
                code_lines = []
                i += 1

                # Read indented code lines
                while i < len(lines) and lines[i].startswith("  "):
                    code_lines.append(lines[i])
                    i += 1

                # Combine code snippet
                code_snippet = "\n".join(code_lines) if code_lines else ""

                # Create QueryResult with source_repo populated
                result = QueryResult(
                    file_path=file_path,
                    line_number=line_start,
                    code_snippet=code_snippet,
                    similarity_score=score,
                    repository_alias=repo_alias,
                    source_repo=source_repo,  # NEW: Extract from file path
                )
                results.append(result)

            else:
                # Non-matching line, skip it
                i += 1

        return results

    def _execute_temporal_query(
        self,
        repo_path: Path,
        repository_alias: str,
        query_text: str,
        limit: int,
        min_score: Optional[float],
        time_range: Optional[str],
        time_range_all: bool = False,
        at_commit: Optional[str] = None,
        include_removed: bool = False,
        show_evolution: bool = False,
        evolution_limit: Optional[int] = None,
        language: Optional[str] = None,
        exclude_language: Optional[str] = None,
        path_filter: Optional[str] = None,
        exclude_path: Optional[str] = None,
    ) -> List[QueryResult]:
        """Execute temporal query using TemporalSearchService with graceful fallback.

        Story #446: Temporal Query Parameters via API

        Integrates TemporalSearchService for time-based code searches. If temporal
        index not available, gracefully falls back to regular search with warning.

        Args:
            repo_path: Repository path
            repository_alias: Repository alias for results
            query_text: Search query
            limit: Result limit
            min_score: Minimum similarity score
            time_range: Time range filter (YYYY-MM-DD..YYYY-MM-DD)
            at_commit: Query at specific commit
            include_removed: Include removed files
            show_evolution: Show evolution timeline
            evolution_limit: Limit evolution entries
            language: Filter by language
            exclude_language: Exclude language
            path_filter: Path filter pattern
            exclude_path: Exclude path pattern

        Returns:
            List of QueryResult objects with temporal context
        """
        from ...proxy.config_manager import ConfigManager
        from ...backends.backend_factory import BackendFactory
        from ...services.temporal.temporal_fusion_dispatch import (
            execute_temporal_query_with_fusion,
        )
        from ...services.temporal.temporal_search_service import (
            ALL_TIME_RANGE,
            parse_date_range,
        )

        try:
            # Load repository configuration
            config_manager = ConfigManager.create_with_backtrack(repo_path)
            config = config_manager.get_config()

            # Create vector store (Story #526: pass server cache)
            from ..app import _server_hnsw_cache

            backend = BackendFactory.create(
                config=config, project_root=repo_path, hnsw_cache=_server_hnsw_cache
            )
            vector_store = backend.get_vector_store_client()
            index_path = repo_path / ".code-indexer" / "index"

            # Resolve time range tuple before calling fusion dispatch
            if time_range:
                time_range_tuple = parse_date_range(time_range)
            else:
                # time_range_all, at_commit, or default: query entire git history
                time_range_tuple = ALL_TIME_RANGE

            # Execute temporal query via fusion dispatch (Story #640)
            temporal_results = execute_temporal_query_with_fusion(
                config=config,
                index_path=index_path,
                vector_store=vector_store,
                query_text=query_text,
                limit=limit,
                time_range=time_range_tuple,
                file_path_filter=path_filter,
                show_evolution=show_evolution,
                at_commit=at_commit,
                include_removed=include_removed,
                language=language,
                exclude_language=exclude_language,
                evolution_limit=evolution_limit,
                exclude_path=exclude_path,
            )

            # If fusion dispatch found no temporal index, fall back gracefully
            if temporal_results.warning and not temporal_results.results:
                logger.warning(
                    format_error_log(
                        "QUERY-MIGRATE-009",
                        "Temporal index not available for repository, falling back to regular search",
                        repository_alias=repository_alias,
                    ),
                    extra=get_log_extra("QUERY-MIGRATE-009"),
                )
                return []

            # Convert temporal results to QueryResult objects
            query_results = []
            for temporal_result in temporal_results.results:
                # Extract individual result metadata (Story #503 - MCP/REST parity)
                # Contains commit-level info for each result
                result_metadata = {
                    "commit_hash": temporal_result.metadata.get("commit_hash"),
                    "commit_date": temporal_result.metadata.get("commit_date"),
                    "author_name": temporal_result.metadata.get("author_name"),
                    "author_email": temporal_result.metadata.get("author_email"),
                    "commit_message": temporal_result.metadata.get("commit_message"),
                    "diff_type": temporal_result.metadata.get("diff_type"),
                }

                # Build temporal context (Acceptance Criterion 7)
                # Contains aggregate info across all commits for this file
                temporal_context = {
                    "first_seen": temporal_result.temporal_context.get("first_seen"),
                    "last_seen": temporal_result.temporal_context.get("last_seen"),
                    "commit_count": temporal_result.temporal_context.get(
                        "appearance_count", 0
                    ),
                    "commits": temporal_result.temporal_context.get("commits", []),
                }

                # Add is_removed flag if applicable
                if (
                    include_removed
                    and temporal_result.metadata.get("diff_type") == "deleted"
                ):
                    temporal_context["is_removed"] = True

                # Add evolution data if requested (Acceptance Criterion 5 & 6)
                if show_evolution and "evolution" in temporal_result.temporal_context:
                    evolution_data = temporal_result.temporal_context["evolution"]
                    # Apply user-controlled evolution_limit (NO arbitrary max)
                    if evolution_limit and len(evolution_data) > evolution_limit:
                        evolution_data = evolution_data[:evolution_limit]
                    temporal_context["evolution"] = evolution_data

                # Create QueryResult with both metadata and temporal_context
                # (Story #503 - MCP/REST API parity with CLI)
                query_result = QueryResult(
                    file_path=temporal_result.file_path,
                    line_number=1,  # Temporal results don't have line numbers
                    code_snippet=temporal_result.content,
                    similarity_score=temporal_result.score,
                    repository_alias=repository_alias,
                    source_repo=None,
                    metadata=result_metadata,
                    temporal_context=temporal_context,
                )

                query_results.append(query_result)

            return query_results

        except ValueError as e:
            # Clear error messages for invalid parameters (Acceptance Criterion 10)
            logger.error(
                format_error_log(
                    "QUERY-MIGRATE-010", "Temporal query validation error", error=str(e)
                ),
                extra=get_log_extra("QUERY-MIGRATE-010"),
            )
            raise ValueError(str(e))
        except Exception as e:
            # Log error and fall back to regular search
            logger.error(
                format_error_log(
                    "QUERY-MIGRATE-011",
                    "Temporal query failed for repository",
                    repository_alias=repository_alias,
                    error=str(e),
                ),
                extra=get_log_extra("QUERY-MIGRATE-011"),
            )
            # Re-raise to let caller handle
            raise SemanticQueryError(f"Temporal query failed: {str(e)}")

    def _execute_fts_search(
        self,
        repo_path: Path,
        repository_alias: str,
        query_text: str,
        limit: int,
        min_score: Optional[float] = None,
        language: Optional[str] = None,
        exclude_language: Optional[str] = None,
        path_filter: Optional[str] = None,
        exclude_path: Optional[str] = None,
        case_sensitive: bool = False,
        fuzzy: bool = False,
        edit_distance: int = 0,
        snippet_lines: int = 5,
        regex: bool = False,
    ) -> List[QueryResult]:
        """
        Execute FTS search using TantivyIndexManager.

        Story #503 - FTS Bug Fix: Implements FTS search for MCP handler.

        Args:
            repo_path: Path to the repository
            repository_alias: Repository alias for result annotation
            query_text: Search query
            limit: Maximum results to return
            min_score: Minimum similarity score threshold
            language: Filter by programming language
            exclude_language: Exclude files of specified language
            path_filter: Filter by file path pattern
            exclude_path: Exclude files matching path pattern
            case_sensitive: Enable case-sensitive matching
            fuzzy: Enable fuzzy matching
            edit_distance: Fuzzy match tolerance 0-3
            snippet_lines: Context lines around matches
            regex: Interpret query as regex pattern

        Returns:
            List of QueryResult objects from FTS search

        Raises:
            SemanticQueryError: If FTS index not available or search fails
        """
        # Check if FTS index exists
        fts_index_dir = repo_path / ".code-indexer" / "tantivy_index"
        if not fts_index_dir.exists():
            raise SemanticQueryError(
                f"FTS index not available for repository '{repository_alias}'. "
                "Build FTS index with 'cidx index --fts' in the repository."
            )

        try:
            # Import TantivyIndexManager (lazy import to avoid startup overhead)
            from ...services.tantivy_index_manager import TantivyIndexManager

            # Initialize Tantivy manager
            tantivy_manager = TantivyIndexManager(fts_index_dir)
            tantivy_manager.initialize_index(create_new=False)

            # Handle fuzzy flag
            effective_edit_distance = edit_distance
            if fuzzy and edit_distance == 0:
                effective_edit_distance = 1

            # Execute FTS query
            fts_raw_results = tantivy_manager.search(
                query_text=query_text,
                case_sensitive=case_sensitive,
                edit_distance=effective_edit_distance,
                snippet_lines=snippet_lines,
                limit=limit,
                language_filter=language,
                path_filter=path_filter,
                exclude_languages=[exclude_language] if exclude_language else None,
                exclude_paths=[exclude_path] if exclude_path else None,
                use_regex=regex,
            )

            # Convert FTS results to QueryResult objects
            query_results: List[QueryResult] = []
            for result in fts_raw_results:
                # FTS doesn't have similarity scores in the same sense as semantic search
                # Use a normalized score based on result ordering (1.0 for first result)
                score = 1.0 - (len(query_results) * 0.01)  # Decreasing score

                # Apply min_score filter if specified
                if min_score is not None and score < min_score:
                    continue

                query_result = QueryResult(
                    file_path=result.get("path", ""),
                    line_number=result.get(
                        "line", 0
                    ),  # FIX: Tantivy returns 'line', not 'line_start'
                    code_snippet=result.get("snippet", ""),
                    similarity_score=score,
                    repository_alias=repository_alias,
                    source_repo=None,
                    # Story #680: Preserve match_text for FTS payload control
                    match_text=result.get("match_text"),
                )
                query_results.append(query_result)

            self.logger.debug(
                f"FTS search completed for '{repository_alias}': "
                f"{len(query_results)} results",
                extra={"correlation_id": get_correlation_id()},
            )
            return query_results

        except ImportError as e:
            raise SemanticQueryError(
                f"Tantivy library not available: {str(e)}. "
                "Install with: pip install tantivy==0.25.0"
            )
        except Exception as e:
            logger.error(
                format_error_log(
                    "QUERY-MIGRATE-012",
                    "FTS search failed for repository",
                    repository_alias=repository_alias,
                    error=str(e),
                ),
                extra=get_log_extra("QUERY-MIGRATE-012"),
            )
            raise SemanticQueryError(f"FTS search failed: {str(e)}")

    def _merge_hybrid_results(
        self,
        fts_results: List[QueryResult],
        semantic_results: List[QueryResult],
        limit: int,
    ) -> List[QueryResult]:
        """
        Merge FTS and semantic search results for hybrid mode.

        Uses reciprocal rank fusion (RRF) to combine results from both search types.

        Args:
            fts_results: Results from FTS search
            semantic_results: Results from semantic search
            limit: Maximum results to return

        Returns:
            Merged and deduplicated list of QueryResult objects
        """
        # Use file_path + line_number as key for deduplication
        merged_results = []
        rrf_scores: Dict[Tuple[str, int], float] = {}

        # Constant for RRF scoring (typically 60)
        k = 60

        # Calculate RRF scores for FTS results
        for rank, result in enumerate(fts_results, start=1):
            key = (result.file_path, result.line_number)
            rrf_score = 1.0 / (k + rank)
            rrf_scores[key] = rrf_scores.get(key, 0) + rrf_score

        # Calculate RRF scores for semantic results
        for rank, result in enumerate(semantic_results, start=1):
            key = (result.file_path, result.line_number)
            rrf_score = 1.0 / (k + rank)
            rrf_scores[key] = rrf_scores.get(key, 0) + rrf_score

        # Create a mapping of keys to results (prefer FTS for content)
        result_map = {}
        for result in semantic_results:
            key = (result.file_path, result.line_number)
            result_map[key] = result

        for result in fts_results:
            key = (result.file_path, result.line_number)
            result_map[key] = result  # FTS overwrites semantic for same key

        # Sort by RRF score and build merged results
        sorted_keys = sorted(
            rrf_scores.keys(), key=lambda k: rrf_scores[k], reverse=True
        )

        for key in sorted_keys[:limit]:
            if key in result_map:
                result = result_map[key]
                # Update similarity score to RRF score
                merged_result = QueryResult(
                    file_path=result.file_path,
                    line_number=result.line_number,
                    code_snippet=result.code_snippet,
                    similarity_score=rrf_scores[key],
                    repository_alias=result.repository_alias,
                    source_repo=result.source_repo,
                )
                merged_results.append(merged_result)

        return merged_results
