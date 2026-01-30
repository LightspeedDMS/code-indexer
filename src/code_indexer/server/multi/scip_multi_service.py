"""
Multi-Repository SCIP Intelligence Service.

Orchestrates parallel SCIP operations across multiple repositories with
timeout enforcement, partial failure handling, and SCIP index availability detection.

Implements:
- AC1: Multi-Repository Definition Lookup
- AC2: Multi-Repository Reference Lookup
- AC3: Multi-Repository Dependency Analysis
- AC4: Multi-Repository Dependents Analysis
- AC5: Per-Repository Call Chain Tracing (no cross-repo stitching)
- AC6: Result Aggregation with Repository Attribution
- AC7: Timeout Enforcement (30s default timeout)
- AC8: SCIP Index Availability Handling
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from pathlib import Path
from typing import Dict, List, Optional

from .scip_models import (
    SCIPMultiRequest,
    SCIPMultiResponse,
    SCIPResult,
    SCIPMultiMetadata,
)
from ...scip.query.primitives import SCIPQueryEngine, QueryResult

logger = logging.getLogger(__name__)


def _get_golden_repos_dir() -> str:
    """Get golden_repos_dir from app.state.

    Raises:
        RuntimeError: If golden_repos_dir is not configured in app.state
    """
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


# Constants for SCIP query parameters
DEFAULT_REFERENCE_LIMIT = 100
DEFAULT_DEPENDENCY_DEPTH = 1
DEFAULT_CALLCHAIN_MAX_DEPTH = 5
DEFAULT_CALLCHAIN_LIMIT = 100


class SCIPMultiService:
    """
    Orchestrates parallel SCIP operations across multiple repositories.

    Threading Strategy:
    - ThreadPoolExecutor for parallel SCIP queries (I/O bound operations)
    - Each repository query runs in separate thread
    - Default: 2 workers, 30s timeout (configurable via Web UI, Story #25)
    """

    def __init__(
        self,
        max_workers: int = 2,
        query_timeout_seconds: int = 30,
        reference_limit: int = DEFAULT_REFERENCE_LIMIT,
        dependency_depth: int = DEFAULT_DEPENDENCY_DEPTH,
        callchain_max_depth: int = DEFAULT_CALLCHAIN_MAX_DEPTH,
        callchain_limit: int = DEFAULT_CALLCHAIN_LIMIT,
    ):
        """
        Initialize SCIP multi-repository service.

        Args:
            max_workers: Maximum number of concurrent threads (default: 2 per resource audit)
            query_timeout_seconds: Timeout for each repository query (default: 30)
            reference_limit: Maximum references to return (default: 100, Bug #83-3 fix)
            dependency_depth: Dependency traversal depth (default: 1, Bug #83-3 fix)
            callchain_max_depth: Maximum callchain depth (default: 5, Bug #83-3 fix)
            callchain_limit: Maximum callchain results (default: 100, Bug #83-3 fix)
        """
        self.max_workers = max_workers
        self.query_timeout_seconds = query_timeout_seconds
        # Bug #83-3 Fix: Store config limits as instance variables
        self.reference_limit = reference_limit
        self.dependency_depth = dependency_depth
        self.callchain_max_depth = callchain_max_depth
        self.callchain_limit = callchain_limit
        self.thread_executor = ThreadPoolExecutor(max_workers=max_workers)

    def definition(self, request: SCIPMultiRequest) -> SCIPMultiResponse:
        """
        Find definitions across multiple repositories (AC1).

        Args:
            request: SCIP multi-repository request with symbol to search

        Returns:
            SCIPMultiResponse with definitions grouped by repository
        """
        return self._execute_parallel_operation(
            request, self._find_definition_in_repo, "definition"
        )

    def _find_definition_in_repo(
        self, repo_id: str, request: SCIPMultiRequest
    ) -> Optional[List[QueryResult]]:
        """
        Find definitions in a single repository.

        Args:
            repo_id: Repository identifier
            request: SCIP multi-repository request

        Returns:
            List of QueryResult objects, or None if no SCIP index available
        """
        scip_file = self._get_scip_file_for_repo(repo_id)
        if scip_file is None:
            return None

        try:
            engine = SCIPQueryEngine(scip_file)
            return engine.find_definition(request.symbol, exact=False)
        except Exception as e:
            logger.error(f"Definition query failed for repo {repo_id}: {e}")
            raise

    def references(self, request: SCIPMultiRequest) -> SCIPMultiResponse:
        """
        Find references across multiple repositories (AC2).

        Args:
            request: SCIP multi-repository request with symbol to search

        Returns:
            SCIPMultiResponse with references grouped by repository
        """
        return self._execute_parallel_operation(
            request, self._find_references_in_repo, "references"
        )

    def _find_references_in_repo(
        self, repo_id: str, request: SCIPMultiRequest
    ) -> Optional[List[QueryResult]]:
        """
        Find references in a single repository.

        Args:
            repo_id: Repository identifier
            request: SCIP multi-repository request

        Returns:
            List of QueryResult objects, or None if no SCIP index available
        """
        scip_file = self._get_scip_file_for_repo(repo_id)
        if scip_file is None:
            return None

        try:
            engine = SCIPQueryEngine(scip_file)
            # Bug #83-3 Fix: Use instance variable instead of hardcoded constant
            limit = (
                request.limit if request.limit is not None else self.reference_limit
            )
            return engine.find_references(request.symbol, limit=limit, exact=False)
        except Exception as e:
            logger.error(f"References query failed for repo {repo_id}: {e}")
            raise

    def dependencies(self, request: SCIPMultiRequest) -> SCIPMultiResponse:
        """
        Find dependencies across multiple repositories (AC3).

        Args:
            request: SCIP multi-repository request with symbol to analyze

        Returns:
            SCIPMultiResponse with dependencies grouped by repository
        """
        return self._execute_parallel_operation(
            request, self._get_dependencies_in_repo, "dependencies"
        )

    def _get_dependencies_in_repo(
        self, repo_id: str, request: SCIPMultiRequest
    ) -> Optional[List[QueryResult]]:
        """
        Get dependencies in a single repository.

        Args:
            repo_id: Repository identifier
            request: SCIP multi-repository request

        Returns:
            List of QueryResult objects, or None if no SCIP index available
        """
        scip_file = self._get_scip_file_for_repo(repo_id)
        if scip_file is None:
            return None

        try:
            engine = SCIPQueryEngine(scip_file)
            # Bug #83-3 Fix: Use instance variable instead of hardcoded constant
            depth = (
                request.max_depth
                if request.max_depth is not None
                else self.dependency_depth
            )
            return engine.get_dependencies(request.symbol, depth=depth, exact=False)
        except Exception as e:
            logger.error(f"Dependencies query failed for repo {repo_id}: {e}")
            raise

    def dependents(self, request: SCIPMultiRequest) -> SCIPMultiResponse:
        """
        Find dependents across multiple repositories (AC4).

        Args:
            request: SCIP multi-repository request with symbol to analyze

        Returns:
            SCIPMultiResponse with dependents grouped by repository
        """
        return self._execute_parallel_operation(
            request, self._get_dependents_in_repo, "dependents"
        )

    def _get_dependents_in_repo(
        self, repo_id: str, request: SCIPMultiRequest
    ) -> Optional[List[QueryResult]]:
        """
        Get dependents in a single repository.

        Args:
            repo_id: Repository identifier
            request: SCIP multi-repository request

        Returns:
            List of QueryResult objects, or None if no SCIP index available
        """
        scip_file = self._get_scip_file_for_repo(repo_id)
        if scip_file is None:
            return None

        try:
            engine = SCIPQueryEngine(scip_file)
            # Bug #83-3 Fix: Use instance variable instead of hardcoded constant
            depth = (
                request.max_depth
                if request.max_depth is not None
                else self.dependency_depth
            )
            return engine.get_dependents(request.symbol, depth=depth, exact=False)
        except Exception as e:
            logger.error(f"Dependents query failed for repo {repo_id}: {e}")
            raise

    def callchain(self, request: SCIPMultiRequest) -> SCIPMultiResponse:
        """
        Trace call chains per repository (AC5).

        NO cross-repository stitching. Each repository's call chains are independent.

        Args:
            request: SCIP multi-repository request with from_symbol and to_symbol

        Returns:
            SCIPMultiResponse with call chains grouped by repository
        """
        return self._execute_parallel_operation(
            request, self._trace_callchain_in_repo, "callchain"
        )

    def _trace_callchain_in_repo(
        self, repo_id: str, request: SCIPMultiRequest
    ) -> Optional[List[QueryResult]]:
        """
        Trace call chains in a single repository (AC5: no cross-repo stitching).

        Args:
            repo_id: Repository identifier
            request: SCIP multi-repository request with from_symbol and to_symbol

        Returns:
            List of QueryResult objects representing call chains, or None if no SCIP index
        """
        scip_file = self._get_scip_file_for_repo(repo_id)
        if scip_file is None:
            return None

        if not request.from_symbol or not request.to_symbol:
            raise ValueError(
                "from_symbol and to_symbol required for callchain operation"
            )

        try:
            engine = SCIPQueryEngine(scip_file)
            # Bug #83-3 Fix: Use instance variables instead of hardcoded constants
            max_depth = (
                request.max_depth
                if request.max_depth is not None
                else self.callchain_max_depth
            )
            limit = (
                request.limit if request.limit is not None else self.callchain_limit
            )
            call_chains = engine.trace_call_chain(
                request.from_symbol, request.to_symbol, max_depth=max_depth, limit=limit
            )

            # Convert CallChain objects to QueryResult objects
            results = []
            for chain in call_chains:
                # Create a single QueryResult representing the chain
                # chain.path is List[str] - symbol names in execution order
                chain_str = " -> ".join(chain.path)
                results.append(
                    QueryResult(
                        symbol=chain_str,
                        project=repo_id,
                        file_path="",
                        line=0,
                        column=0,
                        kind="callchain",
                        context=chain_str,
                    )
                )
            return results
        except Exception as e:
            logger.error(f"Callchain tracing failed for repo {repo_id}: {e}")
            raise

    def _execute_parallel_operation(
        self, request: SCIPMultiRequest, operation_func, operation_name: str
    ) -> SCIPMultiResponse:
        """
        Execute SCIP operation in parallel across repositories.

        Implements:
        - AC6: Result Aggregation with Repository Attribution
        - AC7: Timeout Enforcement
        - AC8: SCIP Index Availability Handling

        Args:
            request: SCIP multi-repository request
            operation_func: Function to execute for each repository
            operation_name: Name of operation for logging

        Returns:
            SCIPMultiResponse with aggregated results
        """
        start_time = time.time()

        repo_results: Dict[str, List[SCIPResult]] = {}
        errors: Dict[str, str] = {}
        skipped: Dict[str, str] = {}

        # Use timeout from request if provided, otherwise use instance default
        timeout_seconds = (
            request.timeout_seconds
            if request.timeout_seconds is not None
            else self.query_timeout_seconds
        )

        # Submit all tasks
        future_to_repo = {
            self.thread_executor.submit(operation_func, repo_id, request): repo_id
            for repo_id in request.repositories
        }

        # Collect results as they complete
        for future in as_completed(future_to_repo):
            repo_id = future_to_repo[future]

            try:
                result = future.result(timeout=timeout_seconds)

                if result is None:
                    # No SCIP index available (AC8)
                    skipped[repo_id] = "No SCIP index available"
                elif isinstance(result, list):
                    # Convert QueryResult to SCIPResult (including empty lists)
                    if len(result) > 0:
                        scip_results = [
                            self._query_result_to_scip_result(qr, repo_id)
                            for qr in result
                        ]
                        repo_results[repo_id] = scip_results
                    else:
                        # Empty list - repo was searched successfully but found nothing
                        repo_results[repo_id] = []

            except TimeoutError:
                error_msg = (
                    f"Query timed out after {timeout_seconds}s. "
                    f"Consider reducing the number of repositories or increasing timeout."
                )
                errors[repo_id] = error_msg
                logger.warning(
                    f"SCIP {operation_name} timeout for repo {repo_id} after {timeout_seconds}s"
                )
            except Exception as e:
                errors[repo_id] = f"SCIP {operation_name} failed: {str(e)}"
                logger.error(f"SCIP {operation_name} error for repo {repo_id}: {e}")

        # Calculate metadata
        total_results = sum(len(results) for results in repo_results.values())
        repos_searched = len(
            repo_results
        )  # All repos that returned results (including empty lists)
        repos_with_results = sum(
            1 for results in repo_results.values() if len(results) > 0
        )
        execution_time_ms = int((time.time() - start_time) * 1000)

        metadata = SCIPMultiMetadata(
            total_results=total_results,
            repos_searched=repos_searched,
            repos_with_results=repos_with_results,
            execution_time_ms=execution_time_ms,
        )

        return SCIPMultiResponse(
            results=repo_results,
            metadata=metadata,
            skipped=skipped,
            errors=errors if errors else None,
        )

    def _get_scip_file_for_repo(self, repo_id: str) -> Optional[Path]:
        """
        Get SCIP index file path for repository (AC8: SCIP availability check).

        Args:
            repo_id: Repository identifier

        Returns:
            Path to SCIP index file, or None if not available
        """
        try:
            repo_path = self._get_repository_path(repo_id)
            scip_db_path = Path(repo_path) / ".code-indexer" / "scip" / "index.scip.db"

            if scip_db_path.exists():
                return scip_db_path
            else:
                logger.info(f"No SCIP index found for repo {repo_id} at {scip_db_path}")
                return None
        except FileNotFoundError:
            # Let FileNotFoundError propagate so the proper error message
            # ("not found in global repositories") reaches the user
            raise
        except Exception as e:
            logger.warning(f"Failed to get SCIP path for repo {repo_id}: {e}")
            return None

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

        # Get golden_repos_dir from server configuration
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

    def _query_result_to_scip_result(self, qr: QueryResult, repo_id: str) -> SCIPResult:
        """
        Convert QueryResult to SCIPResult with repository attribution.

        Args:
            qr: QueryResult from SCIP engine
            repo_id: Repository identifier

        Returns:
            SCIPResult with repository attribution
        """
        return SCIPResult(
            repository=repo_id,
            file_path=qr.file_path,
            line=qr.line,
            column=qr.column,
            symbol=qr.symbol,
            kind=qr.kind,
            context=qr.context,
        )
