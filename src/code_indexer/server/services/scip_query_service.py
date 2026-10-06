"""
SCIP Query Service - Unified SCIP File Discovery.

Story #38: Create SCIPQueryService with Unified SCIP File Discovery

Provides centralized SCIP file discovery logic that can be shared between
MCP handlers and REST routes, eliminating code duplication.

Key features:
- Find SCIP index files (.scip.db) across golden repositories
- Queries search only repositories the caller may access: the access
  filtering service and the caller's username are required, and a query
  without them is refused with AccessFilteringServiceUnavailableError
- Optional repository alias filtering for specific repository queries

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
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union, TYPE_CHECKING
from code_indexer.server.logging_utils import format_error_log

if TYPE_CHECKING:
    from .access_filtering_service import AccessFilteringService
    from code_indexer.scip.query.primitives import QueryResult

logger = logging.getLogger(__name__)


def _call_chain_to_dict(chain: Any) -> Dict[str, Any]:
    """Convert a backend CallChain object into its serializable dict form.

    Extracted from SCIPQueryService.trace_callchain to keep that method
    focused/short.
    """
    return {
        "path": chain.path,
        "length": chain.length,
        "has_cycle": chain.has_cycle,
    }


def _trace_callchain_in_file(
    scip_file: Path,
    from_symbol: str,
    to_symbol: str,
    max_depth: int,
    limit: int,
    timeout_errors: List[str],
) -> List[Dict[str, Any]]:
    """Trace a call chain within a single SCIP file.

    Converts results to dicts and appends any timeout message (Bug #1603
    code review Priority 1) to `timeout_errors` (mutated in place). Logs
    and swallows any other exception (e.g. corrupt/incompatible index) so
    one bad file doesn't abort the whole cross-repo scan. Extracted from
    SCIPQueryService.trace_callchain to keep that method focused/short.
    """
    from code_indexer.scip.query.primitives import SCIPQueryEngine

    try:
        engine = SCIPQueryEngine(scip_file)
        chains = engine.trace_call_chain(
            from_symbol,
            to_symbol,
            max_depth=max_depth,
            limit=limit,
            timeout_errors=timeout_errors,
        )
        return [_call_chain_to_dict(c) for c in chains]
    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-142",
                f"Failed to trace call chain in {scip_file}: {e}",
            )
        )
        return []


class SCIPQueryService:
    """
    Centralized service for SCIP file discovery (SERVER-ONLY).

    Provides unified logic for finding SCIP index files across golden
    repositories, scoped to the repositories the caller may access.

    This is a server-side service that operates on the golden_repos_dir
    configuration. It does not include CLI mode fallback logic - CLI users
    should use the CLI-specific SCIP commands that operate on local paths.

    Usage:
        service = SCIPQueryService(
            golden_repos_dir="/data/golden-repos",
            access_filtering_service=access_service,  # Required for queries
        )
        scip_files = service.find_scip_files(
            username="developer",  # Required: the caller's grants scope it
            repository_alias="my-repo",  # Optional, for filtering
        )

    Every query without an access filtering service or a username is refused
    with AccessFilteringServiceUnavailableError.
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

        SCIP queries search only repositories the caller may access: every
        query operation starts here, and without an access filtering service
        or a caller identity there is nothing to scope by, so the call fails
        closed before touching the filesystem.

        Args:
            repository_alias: Optional repository name to filter results
            username: The caller whose repository grants scope the search
                (required)

        Returns:
            List of Path objects pointing to .scip.db files

        Raises:
            AccessFilteringServiceUnavailableError: no access filtering
                service is configured, or no username was given.
        """
        from code_indexer.server.services.repo_access_guard import (
            AccessFilteringServiceUnavailableError,
        )

        if not username or self.access_filtering_service is None:
            raise AccessFilteringServiceUnavailableError(
                "Access control unavailable: SCIP queries require the access "
                "filtering service and the caller's username"
            )
        accessible_repos: Set[str] = self.access_filtering_service.get_accessible_repos(
            username
        )

        golden_repos_path = self.get_golden_repos_dir()

        # Return empty list if golden repos directory doesn't exist
        if not golden_repos_path.exists():
            return []

        scip_files: List[Path] = []

        # Normalize repository_alias once before the loop (loop-invariant).
        # Converts MCP alias form (e.g. "flask-large-global") or full path
        # (e.g. "/data/golden-repos/flask-large") to a bare directory name
        # (e.g. "flask-large") for comparison with repo_dir.name.
        normalized_alias: Optional[str] = None
        if repository_alias is not None:
            normalized_alias = repository_alias
            if os.sep in normalized_alias or "/" in normalized_alias:
                normalized_alias = Path(normalized_alias).name
            normalized_alias = normalized_alias.removesuffix("-global")

        # Bug #1084 B2: a specific-alias query resolves its target_path DIRECTLY
        # (the authority semantic uses), independent of whether a matching base
        # clone dir appears in the iteration below. This guarantees SCIP reads the
        # alias-pointed version even when the mutable base clone is absent (AC #9).
        if normalized_alias is not None:
            if normalized_alias not in accessible_repos:
                return []
            alias_root = self._resolve_alias_scip_root(normalized_alias)
            if alias_root is not None:
                scip_dir = alias_root / ".code-indexer" / "scip"
                if scip_dir.exists():
                    return self._contained_scip_files(alias_root, scip_dir)

        for repo_dir in golden_repos_path.iterdir():
            # Skip non-directories
            if not repo_dir.is_dir():
                continue

            # Skip hidden directories (.versioned snapshots are reached
            # through alias target resolution below, never scanned directly)
            if repo_dir.name.startswith("."):
                continue

            # Filter by repository_alias if provided
            if normalized_alias is not None and repo_dir.name != normalized_alias:
                continue

            # Only repositories the caller may access
            if repo_dir.name not in accessible_repos:
                continue

            # Bug #1084 B2: resolve the alias target_path (the SAME authority
            # semantic search uses) so SCIP reads the version the alias points
            # to, not the mutable base clone. On cow-daemon the alias target is a
            # snapshot under the daemon mount while the base clone holds an older
            # index -- using the base clone produced cross-index version skew
            # (AC #9). When no alias resolves (e.g. local repos with no alias
            # JSON), fall back to the filesystem repo dir (local behavior
            # unchanged).
            scip_root = self._resolve_alias_scip_root(repo_dir.name) or repo_dir

            # Find .scip.db files in the repository's scip directory
            scip_dir = scip_root / ".code-indexer" / "scip"
            if scip_dir.exists():
                scip_files.extend(self._contained_scip_files(scip_root, scip_dir))

        return scip_files

    @staticmethod
    def _contained_scip_files(scip_root: Path, scip_dir: Path) -> List[Path]:
        """Return the ``.scip.db`` files under ``scip_dir`` that resolve
        inside the selected repository ``scip_root``.

        SCIP index files are read only from within the repository they were
        found in: a file (or directory) that resolves elsewhere is skipped,
        and the count of skipped files is logged once at WARNING.
        """
        from code_indexer.utils.path_confinement import resolve_if_within_root

        try:
            resolved_root = scip_root.resolve()
        except (OSError, RuntimeError):
            logger.warning(
                "Skipping SCIP indexes of %s: repository root cannot be resolved",
                scip_root,
            )
            return []
        contained: List[Path] = []
        skipped = 0
        for db_file in scip_dir.glob("**/*.scip.db"):
            if resolve_if_within_root(db_file, resolved_root) is None:
                skipped += 1
                continue
            contained.append(db_file)
        if skipped:
            logger.warning(
                "Skipped %d SCIP index file(s) under %s that do not resolve "
                "inside repository %s",
                skipped,
                scip_dir,
                scip_root,
            )
        return contained

    @staticmethod
    def _scip_dir_for_files(scip_files: List[Path]) -> Path:
        """Return the ``.code-indexer/scip`` directory containing ``scip_files``.

        For an alias-scoped query every file lives under a single
        ``<root>/.code-indexer/scip`` directory (see :meth:`find_scip_files`).
        Return that directory so a recursive ``**/*.scip.db`` glob crosses only
        this repo's indexes. Falls back to the first file's parent if the
        expected layout is not found.
        """
        first = scip_files[0]
        for parent in first.parents:
            if parent.name == "scip" and parent.parent.name == ".code-indexer":
                return parent
        return first.parent

    def _resolve_alias_scip_root(self, repo_name: str) -> Optional[Path]:
        """Resolve a repo's alias ``target_path`` to its SCIP-bearing root.

        Bug #1084 B2: mirrors the semantic-search resolution (alias is
        authoritative). Returns the alias target as a :class:`Path` when the
        ``{repo_name}-global`` (or bare ``{repo_name}``) alias resolves to an
        existing directory; otherwise ``None`` so the caller falls back to the
        filesystem layout. Never raises -- any resolution failure degrades to
        ``None`` (filesystem scan).
        """
        try:
            from code_indexer.global_repos.alias_manager import AliasManager

            aliases_dir = self._golden_repos_dir / "aliases"
            if not aliases_dir.is_dir():
                return None
            alias_manager = AliasManager(str(aliases_dir))

            # Prefer the -global alias (the form semantic resolves), then bare.
            target = alias_manager.read_alias(f"{repo_name}-global")
            if not target:
                target = alias_manager.read_alias(repo_name)
            if target and Path(target).is_dir():
                return Path(target)
        except Exception as exc:  # pragma: no cover - defensive, never fatal
            logger.debug(
                "find_scip_files: alias resolution for '%s' failed (%s); "
                "falling back to filesystem scan",
                repo_name,
                exc,
            )
        return None

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
            username: The caller whose grants scope the query (required; without
                it or an access filtering service the query is refused with
                AccessFilteringServiceUnavailableError)

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
            username: The caller whose grants scope the query (required; without
                it or an access filtering service the query is refused with
                AccessFilteringServiceUnavailableError)

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
            username: The caller whose grants scope the query (required; without
                it or an access filtering service the query is refused with
                AccessFilteringServiceUnavailableError)

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
            username: The caller whose grants scope the query (required; without
                it or an access filtering service the query is refused with
                AccessFilteringServiceUnavailableError)

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
            depth: Maximum traversal depth (default 3). Must be at least 1
                   (raises ValueError otherwise -- Bug #1672/#1639); values
                   above MAX_TRAVERSAL_DEPTH (10) are clamped by the
                   underlying `composites.analyze_impact` as a safety net.
            repository_alias: Repository to scope the analysis to. When given,
                   only that repo's SCIP indexes are traversed (mirrors the
                   other SCIP endpoints); when None, all golden repos are.
            username: The caller whose grants scope the query (required; without
                it or an access filtering service the query is refused with
                AccessFilteringServiceUnavailableError)

        Returns:
            Dictionary with impact analysis results

        Raises:
            ValueError: if depth is not a positive integer.
        """
        from code_indexer.scip.query.composites import analyze_impact

        # Bug #1672: assert the precondition explicitly instead of letting a
        # hypothetical depth < 1 flow into the no-index early return below,
        # where `min(depth, 10)` would otherwise silently report a
        # nonsensical `depth_analyzed: 0`. Currently unreachable in practice
        # (both real callers -- the MCP scip_impact handler and the REST
        # GET /scip/impact route -- already reject depth < 1 before calling
        # this method), but this mirrors composites.analyze_impact's own
        # guard (Bug #1639) so the invariant holds regardless of caller.
        if depth < 1:
            raise ValueError(f"depth must be at least 1, got {depth}")

        # Scope to the requested repo's SCIP indexes and early-out when there is
        # no index for it. Without this, impact analysis ignored the alias and
        # scanned EVERY golden repo's .scip.db twice with a depth-N CTE -- tens
        # of seconds even for a repo with no SCIP index at all. find_scip_files
        # also enforces access control for the caller.
        scip_files = self.find_scip_files(
            repository_alias=repository_alias, username=username
        )
        if not scip_files:
            return {
                "target_symbol": symbol,
                "depth_analyzed": min(depth, 10),
                "total_affected": 0,
                "truncated": False,
                "affected_symbols": [],
                "affected_files": [],
            }

        # SCIP impact queries search only repositories the caller may access:
        # the composite searches exactly the access-filtered scip_files above
        # and never globs a directory. scip_dir only labels log messages.
        scip_dir = (
            self._scip_dir_for_files(scip_files)
            if repository_alias is not None
            else self.get_golden_repos_dir()
        )

        result = analyze_impact(symbol, scip_dir, depth=depth, scip_files=scip_files)

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
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """
        Trace call chains between two symbols.

        Returns:
            (chains, timeout_errors); non-empty timeout_errors means a
            query was cut off and chains may be incomplete (Bug #1603).
        """
        if not from_symbol or not to_symbol or max_depth < 1 or limit < 0:
            return [], []

        scip_files = self.find_scip_files(
            repository_alias=repository_alias, username=username
        )
        if not scip_files:
            return [], []

        all_results: List[Dict[str, Any]] = []
        timeout_errors: List[str] = []
        for scip_file in scip_files:
            all_results.extend(
                _trace_callchain_in_file(
                    scip_file,
                    from_symbol,
                    to_symbol,
                    max_depth,
                    limit,
                    timeout_errors,
                )
            )

        return all_results, timeout_errors

    def get_context(
        self,
        symbol: str,
        limit: int = 20,
        min_score: float = 0.0,
        repository_alias: Optional[str] = None,
        username: Optional[str] = None,
        timeout_seconds: int = 30,
    ) -> Dict[str, Any]:
        """
        Get smart context for a symbol - curated file list with relevance scoring.

        Args:
            symbol: Target symbol name
            limit: Maximum files to return (default 20)
            min_score: Minimum relevance score (0.0-1.0)
            repository_alias: Repository to scope the query to; when None,
                every repository the caller may access is searched.
            username: The caller whose grants scope the query (required; without
                it or an access filtering service the query is refused with
                AccessFilteringServiceUnavailableError)
            timeout_seconds: Maximum seconds for the query (default 30).
                Raises QueryTimeoutError if exceeded.

        Returns:
            Dictionary with smart context results

        Raises:
            QueryTimeoutError: If the query exceeds timeout_seconds.
        """
        from code_indexer.scip.query.composites import get_smart_context

        scip_files = self.find_scip_files(
            repository_alias=repository_alias, username=username
        )
        if not scip_files:
            return {
                "target_symbol": symbol,
                "summary": "",
                "files": [],
                "total_files": 0,
                "total_symbols": 0,
                "avg_relevance": 0.0,
            }

        # SCIP context queries search only repositories the caller may access:
        # the composite searches exactly the access-filtered scip_files above
        # and never globs a directory. scip_dir only labels log messages.
        scip_dir = (
            self._scip_dir_for_files(scip_files)
            if repository_alias is not None
            else self.get_golden_repos_dir()
        )

        result = get_smart_context(
            symbol,
            scip_dir,
            limit=limit,
            min_score=min_score,
            timeout_seconds=timeout_seconds,
            scip_files=scip_files,
        )

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
