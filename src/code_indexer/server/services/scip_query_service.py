"""
SCIP Query Service - Unified SCIP File Discovery.

Story #38: Create SCIPQueryService with Unified SCIP File Discovery

Provides centralized SCIP file discovery logic that can be shared between
MCP handlers and REST routes, eliminating code duplication.

Key features:
- Find SCIP index files (.scip.db) across golden repositories
- Optional access control filtering by username
- Optional repository alias filtering for specific repository queries
- Backward compatibility when no username/access control provided

SERVER-ONLY SCOPE:
    This service is designed exclusively for server-side usage (MCP handlers,
    REST routes). It does NOT include CLI mode fallback logic that exists in
    the legacy REST implementation. This is intentional:

    1. The CLI mode has its own SCIP file discovery via local file paths
    2. This service operates on the golden_repos_dir server configuration
    3. Access control filtering is server-specific (users, groups)
    4. Keeping server and CLI logic separate improves maintainability

    CLI users should continue using the CLI-specific SCIP commands directly,
    which operate on local repository paths rather than the golden repos.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union, TYPE_CHECKING
from code_indexer.server.logging_utils import format_error_log

if TYPE_CHECKING:
    from .access_filtering_service import AccessFilteringService
    from code_indexer.scip.query.primitives import QueryResult

logger = logging.getLogger(__name__)


class SCIPQueryService:
    """
    Centralized service for SCIP file discovery (SERVER-ONLY).

    Provides unified logic for finding SCIP index files across golden
    repositories with optional access control filtering.

    This is a server-side service that operates on the golden_repos_dir
    configuration. It does not include CLI mode fallback logic - CLI users
    should use the CLI-specific SCIP commands that operate on local paths.

    Usage:
        service = SCIPQueryService(
            golden_repos_dir="/data/golden-repos",
            access_filtering_service=access_service,  # Optional
        )
        scip_files = service.find_scip_files(
            username="developer",  # Optional, for access control
            repository_alias="my-repo",  # Optional, for filtering
        )
    """

    def __init__(
        self,
        golden_repos_dir: Union[str, Path],
        access_filtering_service: Optional["AccessFilteringService"] = None,
    ):
        """
        Initialize the SCIPQueryService.

        Args:
            golden_repos_dir: Path to the golden repositories directory
            access_filtering_service: Optional access filtering for user-based
                                     repository filtering
        """
        self._golden_repos_dir = Path(golden_repos_dir)
        self.access_filtering_service = access_filtering_service

    def get_golden_repos_dir(self) -> Path:
        """
        Get the golden repos directory.

        Returns:
            Path to the golden repos directory
        """
        return self._golden_repos_dir

    def find_scip_files(
        self,
        repository_alias: Optional[str] = None,
        username: Optional[str] = None,
    ) -> List[Path]:
        """
        Find all .scip.db files across golden repositories.

        Args:
            repository_alias: Optional repository name to filter results
            username: Optional username for access control filtering

        Returns:
            List of Path objects pointing to .scip.db files
        """
        golden_repos_path = self.get_golden_repos_dir()

        # Return empty list if golden repos directory doesn't exist
        if not golden_repos_path.exists():
            return []

        # Get accessible repos if username provided and access service exists
        accessible_repos: Optional[Set[str]] = None
        if username is not None and self.access_filtering_service is not None:
            accessible_repos = self.access_filtering_service.get_accessible_repos(
                username
            )

        scip_files: List[Path] = []

        for repo_dir in golden_repos_path.iterdir():
            # Skip non-directories
            if not repo_dir.is_dir():
                continue

            # Skip hidden directories except .versioned
            if repo_dir.name.startswith(".") and repo_dir.name != ".versioned":
                continue

            # Filter by repository_alias if provided
            if repository_alias and repo_dir.name != repository_alias:
                continue

            # Check access control if enabled
            if accessible_repos is not None and repo_dir.name not in accessible_repos:
                continue

            # Find .scip.db files in the repository's scip directory
            scip_dir = repo_dir / ".code-indexer" / "scip"
            if scip_dir.exists():
                scip_files.extend(scip_dir.glob("**/*.scip.db"))

        return scip_files

    def get_accessible_repos(self, username: str) -> Optional[Set[str]]:
        """
        Get set of repositories accessible by the given user.

        Args:
            username: The user's identifier

        Returns:
            Set of accessible repository names, or None if no access
            service is configured
        """
        if self.access_filtering_service is None:
            return None
        return self.access_filtering_service.get_accessible_repos(username)

    def _query_result_to_dict(self, result: "QueryResult") -> Dict[str, Any]:
        """
        Convert a QueryResult object to a serializable dictionary.

        Args:
            result: QueryResult object from SCIPQueryEngine

        Returns:
            Dictionary with all QueryResult fields
        """
        return {
            "symbol": result.symbol,
            "project": result.project,
            "file_path": str(result.file_path),
            "line": result.line,
            "column": result.column,
            "kind": result.kind,
            "relationship": result.relationship,
            "context": result.context,
        }

    def find_definition(
        self,
        symbol: str,
        exact: bool = False,
        repository_alias: Optional[str] = None,
        username: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Find definition locations for a symbol across all indexed repositories.

        Args:
            symbol: Symbol name to search for
            exact: If True, match exact symbol name; if False, match substring
            repository_alias: Optional repository name to filter SCIP indexes
            username: Optional username for access control filtering

        Returns:
            List of dictionaries with definition results
        """
        from code_indexer.scip.query.primitives import SCIPQueryEngine

        scip_files = self.find_scip_files(
            repository_alias=repository_alias, username=username
        )

        if not scip_files:
            return []

        all_results: List[Dict[str, Any]] = []

        for scip_file in scip_files:
            try:
                engine = SCIPQueryEngine(scip_file)
                results = engine.find_definition(symbol, exact=exact)
                all_results.extend(self._query_result_to_dict(r) for r in results)
            except Exception as e:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-138", f"Failed to query SCIP file {scip_file}: {e}"
                    )
                )
                continue

        return all_results

    def find_references(
        self,
        symbol: str,
        limit: int = 100,
        exact: bool = False,
        repository_alias: Optional[str] = None,
        username: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Find all references to a symbol across all indexed repositories.

        Args:
            symbol: Symbol name to search for
            limit: Maximum number of results to return (default 100)
            exact: If True, match exact symbol name; if False, match substring
            repository_alias: Optional repository name to filter SCIP indexes
            username: Optional username for access control filtering

        Returns:
            List of dictionaries with reference results
        """
        from code_indexer.scip.query.primitives import SCIPQueryEngine

        scip_files = self.find_scip_files(
            repository_alias=repository_alias, username=username
        )

        if not scip_files:
            return []

        all_results: List[Dict[str, Any]] = []

        for scip_file in scip_files:
            try:
                engine = SCIPQueryEngine(scip_file)
                results = engine.find_references(symbol, limit=limit, exact=exact)
                all_results.extend(self._query_result_to_dict(r) for r in results)
            except Exception as e:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-139", f"Failed to query SCIP file {scip_file}: {e}"
                    )
                )
                continue

        return all_results

    def get_dependencies(
        self,
        symbol: str,
        depth: int = 1,
        exact: bool = False,
        repository_alias: Optional[str] = None,
        username: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get symbols that this symbol depends on.

        Args:
            symbol: Symbol name to analyze
            depth: Depth of transitive dependencies (1 = direct only)
            exact: If True, match exact symbol name; if False, match substring
            repository_alias: Optional repository name to filter SCIP indexes
            username: Optional username for access control filtering

        Returns:
            List of dictionaries with dependency results
        """
        from code_indexer.scip.query.primitives import SCIPQueryEngine

        scip_files = self.find_scip_files(
            repository_alias=repository_alias, username=username
        )

        if not scip_files:
            return []

        all_results: List[Dict[str, Any]] = []

        for scip_file in scip_files:
            try:
                engine = SCIPQueryEngine(scip_file)
                results = engine.get_dependencies(symbol, depth=depth, exact=exact)
                all_results.extend(self._query_result_to_dict(r) for r in results)
            except Exception as e:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-140", f"Failed to query SCIP file {scip_file}: {e}"
                    )
                )
                continue

        return all_results

    def get_dependents(
        self,
        symbol: str,
        depth: int = 1,
        exact: bool = False,
        repository_alias: Optional[str] = None,
        username: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get symbols that depend on this symbol.

        Args:
            symbol: Symbol name to analyze
            depth: Depth of transitive dependents (1 = direct only)
            exact: If True, match exact symbol name; if False, match substring
            repository_alias: Optional repository name to filter SCIP indexes
            username: Optional username for access control filtering

        Returns:
            List of dictionaries with dependent results
        """
        from code_indexer.scip.query.primitives import SCIPQueryEngine

        scip_files = self.find_scip_files(
            repository_alias=repository_alias, username=username
        )

        if not scip_files:
            return []

        all_results: List[Dict[str, Any]] = []

        for scip_file in scip_files:
            try:
                engine = SCIPQueryEngine(scip_file)
                results = engine.get_dependents(symbol, depth=depth, exact=exact)
                all_results.extend(self._query_result_to_dict(r) for r in results)
            except Exception as e:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-141", f"Failed to query SCIP file {scip_file}: {e}"
                    )
                )
                continue

        return all_results

    def analyze_impact(
        self,
        symbol: str,
        depth: int = 3,
        repository_alias: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Analyze impact of changes to a symbol.

        Args:
            symbol: Symbol name to analyze
            depth: Maximum traversal depth (default 3). Clamped by underlying
                   function to MAX_TRAVERSAL_DEPTH (10).
            repository_alias: Reserved for future filtering support
            username: Reserved for future filtering support

        Returns:
            Dictionary with impact analysis results
        """
        from code_indexer.scip.query.composites import analyze_impact

        # Get the scip directory from golden_repos_dir
        scip_dir = self.get_golden_repos_dir()

        # Note: repository_alias and username reserved for future filtering
        _ = repository_alias, username

        result = analyze_impact(symbol, scip_dir, depth=depth)

        return {
            "target_symbol": result.target_symbol,
            "depth_analyzed": result.depth_analyzed,
            "total_affected": result.total_affected,
            "truncated": result.truncated,
            "affected_symbols": [
                {
                    "symbol": s.symbol,
                    "file_path": str(s.file_path),
                    "line": s.line,
                    "column": s.column,
                    "depth": s.depth,
                    "relationship": s.relationship,
                    "chain": s.chain,
                }
                for s in result.affected_symbols
            ],
            "affected_files": [
                {
                    "path": str(f.path),
                    "project": f.project,
                    "affected_symbol_count": f.affected_symbol_count,
                    "min_depth": f.min_depth,
                    "max_depth": f.max_depth,
                }
                for f in result.affected_files
            ],
        }

    def trace_callchain(
        self,
        from_symbol: str,
        to_symbol: str,
        max_depth: int = 10,
        limit: int = 100,
        repository_alias: Optional[str] = None,
        username: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Trace call chains between two symbols.

        Args:
            from_symbol: Entry point symbol name
            to_symbol: Target symbol name
            max_depth: Maximum path length in hops (default 10)
            limit: Maximum number of paths to return (default 100)
            repository_alias: Optional repository name to filter SCIP indexes
            username: Optional username for access control filtering

        Returns:
            List of dictionaries with call chain information (path, length, has_cycle)
        """
        from code_indexer.scip.query.primitives import SCIPQueryEngine

        scip_files = self.find_scip_files(
            repository_alias=repository_alias, username=username
        )

        if not scip_files:
            return []

        all_results: List[Dict[str, Any]] = []

        for scip_file in scip_files:
            try:
                engine = SCIPQueryEngine(scip_file)
                chains = engine.trace_call_chain(
                    from_symbol, to_symbol, max_depth=max_depth, limit=limit
                )
                all_results.extend(
                    {
                        "path": chain.path,
                        "length": chain.length,
                        "has_cycle": chain.has_cycle,
                    }
                    for chain in chains
                )
            except Exception as e:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-142",
                        f"Failed to trace call chain in {scip_file}: {e}",
                    )
                )
                continue

        return all_results

    def get_context(
        self,
        symbol: str,
        limit: int = 20,
        min_score: float = 0.0,
        repository_alias: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get smart context for a symbol - curated file list with relevance scoring.

        Args:
            symbol: Target symbol name
            limit: Maximum files to return (default 20)
            min_score: Minimum relevance score (0.0-1.0)
            repository_alias: Reserved for future filtering support
            username: Reserved for future filtering support

        Returns:
            Dictionary with smart context results
        """
        from code_indexer.scip.query.composites import get_smart_context

        # Get the scip directory from golden_repos_dir
        scip_dir = self.get_golden_repos_dir()

        # Note: repository_alias and username reserved for future filtering
        _ = repository_alias, username

        result = get_smart_context(symbol, scip_dir, limit=limit, min_score=min_score)

        return {
            "target_symbol": result.target_symbol,
            "summary": result.summary,
            "files": [
                {
                    "path": str(f.path),
                    "project": f.project,
                    "relevance_score": f.relevance_score,
                    "symbols": [
                        {
                            "name": s.name,
                            "kind": s.kind,
                            "relationship": s.relationship,
                            "line": s.line,
                            "column": s.column,
                            "relevance": s.relevance,
                        }
                        for s in f.symbols
                    ],
                    "read_priority": f.read_priority,
                }
                for f in result.files
            ],
            "total_files": result.total_files,
            "total_symbols": result.total_symbols,
            "avg_relevance": result.avg_relevance,
        }
