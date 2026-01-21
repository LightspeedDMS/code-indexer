"""SCIP Code Intelligence API Client for CLI Remote Mode (Story #736).

Provides API client for SCIP operations via REST endpoints.
All operations support repository_alias for multi-repository queries.
"""

import logging
from typing import Dict, Any, Optional
from pathlib import Path

from .base_client import CIDXRemoteAPIClient, APIClientError

logger = logging.getLogger(__name__)


class SCIPQueryError(APIClientError):
    """Exception raised when SCIP query execution fails."""

    pass


class SCIPNotFoundError(APIClientError):
    """Exception raised when SCIP resource is not found."""

    pass


class SCIPAPIClient(CIDXRemoteAPIClient):
    """API client for SCIP code intelligence operations."""

    def __init__(
        self,
        server_url: str,
        credentials: Dict[str, Any],
        project_root: Optional[Path] = None,
    ):
        """Initialize SCIP API client.

        Args:
            server_url: Base URL of the CIDX server
            credentials: Authentication credentials dictionary
            project_root: Optional project root for persistent token storage
        """
        super().__init__(server_url, credentials, project_root)

    def _build_request_payload(
        self,
        symbol: str,
        repositories: list,
        limit: Optional[int] = None,
        max_depth: Optional[int] = None,
        project: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build request payload for SCIP multi-repository endpoints."""
        payload: Dict[str, Any] = {
            "repositories": repositories,
            "symbol": symbol,
        }
        if limit is not None:
            payload["limit"] = limit
        if max_depth is not None:
            payload["max_depth"] = max_depth
        if project:
            payload["project"] = project
        return payload

    async def _execute_scip_request(
        self, endpoint: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute SCIP request and handle responses."""
        try:
            response = await self._authenticated_request("POST", endpoint, json=payload)

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                error_detail = self._extract_error_detail(
                    response, "Resource not found"
                )
                raise SCIPNotFoundError(error_detail, status_code=404)
            elif response.status_code == 422:
                error_detail = self._extract_error_detail(response, "Invalid request")
                raise ValueError(f"Request validation failed: {error_detail}")
            else:
                error_detail = self._extract_error_detail(
                    response, f"HTTP {response.status_code}"
                )
                raise SCIPQueryError(
                    f"SCIP query failed: {error_detail}",
                    status_code=response.status_code,
                )
        except (SCIPQueryError, SCIPNotFoundError, ValueError):
            raise
        except APIClientError as e:
            raise SCIPQueryError(f"SCIP query failed: {e}")
        except Exception as e:
            raise SCIPQueryError(f"Unexpected error in SCIP query: {e}")

    def _extract_error_detail(self, response, default: str) -> str:
        """Extract error detail from response JSON."""
        try:
            detail = response.json().get("detail", default)
            return str(detail) if detail is not None else default
        except Exception:
            return default

    async def definition(
        self,
        symbol: str,
        repository_alias: Optional[str] = None,
        project: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Find symbol definition via SCIP multi-repository endpoint.

        Args:
            symbol: Symbol name to search for
            repository_alias: Repository alias to query (required for remote)
            project: Optional project path filter

        Returns:
            Dictionary with results, metadata, and errors from server
        """
        if not symbol or not isinstance(symbol, str):
            raise ValueError("Symbol cannot be empty")
        if not repository_alias:
            raise ValueError("repository_alias is required for remote SCIP queries")

        payload = self._build_request_payload(
            symbol=symbol, repositories=[repository_alias], project=project
        )
        return await self._execute_scip_request("/api/scip/multi/definition", payload)

    async def references(
        self,
        symbol: str,
        repository_alias: Optional[str] = None,
        limit: int = 100,
        project: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Find all references to a symbol via SCIP multi-repository endpoint.

        Args:
            symbol: Symbol name to search for
            repository_alias: Repository alias to query (required for remote)
            limit: Maximum number of results per repository
            project: Optional project path filter

        Returns:
            Dictionary with results, metadata, and errors from server
        """
        if not symbol or not isinstance(symbol, str):
            raise ValueError("Symbol cannot be empty")
        if not repository_alias:
            raise ValueError("repository_alias is required for remote SCIP queries")

        payload = self._build_request_payload(
            symbol=symbol, repositories=[repository_alias], limit=limit, project=project
        )
        return await self._execute_scip_request("/api/scip/multi/references", payload)

    async def dependencies(
        self,
        symbol: str,
        repository_alias: Optional[str] = None,
        depth: int = 1,
        project: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get symbols that this symbol depends on.

        Args:
            symbol: Symbol name to analyze
            repository_alias: Repository alias to query (required for remote)
            depth: Depth of transitive dependencies (default: 1 = direct only)
            project: Optional project path filter

        Returns:
            Dictionary with results, metadata, and errors from server
        """
        if not symbol or not isinstance(symbol, str):
            raise ValueError("Symbol cannot be empty")
        if not repository_alias:
            raise ValueError("repository_alias is required for remote SCIP queries")

        payload = self._build_request_payload(
            symbol=symbol,
            repositories=[repository_alias],
            max_depth=depth,
            project=project,
        )
        return await self._execute_scip_request("/api/scip/multi/dependencies", payload)

    async def dependents(
        self,
        symbol: str,
        repository_alias: Optional[str] = None,
        depth: int = 1,
        project: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get symbols that depend on this symbol.

        Args:
            symbol: Symbol name to analyze
            repository_alias: Repository alias to query (required for remote)
            depth: Depth of transitive dependents (default: 1 = direct only)
            project: Optional project path filter

        Returns:
            Dictionary with results, metadata, and errors from server
        """
        if not symbol or not isinstance(symbol, str):
            raise ValueError("Symbol cannot be empty")
        if not repository_alias:
            raise ValueError("repository_alias is required for remote SCIP queries")

        payload = self._build_request_payload(
            symbol=symbol,
            repositories=[repository_alias],
            max_depth=depth,
            project=project,
        )
        return await self._execute_scip_request("/api/scip/multi/dependents", payload)

    async def impact(
        self,
        symbol: str,
        repository_alias: Optional[str] = None,
        depth: int = 3,
        project: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Analyze impact of changes to a symbol (shows what depends on it).

        Args:
            symbol: Symbol name to analyze
            repository_alias: Repository alias to query (required for remote)
            depth: Analysis depth (default: 3, max: 10)
            project: Optional project path filter

        Returns:
            Dictionary with results, metadata, and errors from server
        """
        if not symbol or not isinstance(symbol, str):
            raise ValueError("Symbol cannot be empty")
        if not repository_alias:
            raise ValueError("repository_alias is required for remote SCIP queries")

        payload = self._build_request_payload(
            symbol=symbol,
            repositories=[repository_alias],
            max_depth=min(depth, 10),
            project=project,
        )
        return await self._execute_scip_request("/api/scip/multi/dependents", payload)

    async def callchain(
        self,
        from_symbol: str,
        to_symbol: str,
        repository_alias: Optional[str] = None,
        max_depth: int = 10,
        project: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Trace call chains between two symbols.

        Args:
            from_symbol: Starting symbol for chain tracing
            to_symbol: Target symbol for chain tracing
            repository_alias: Repository alias to query (required for remote)
            max_depth: Maximum chain length (default: 10, max: 20)
            project: Optional project path filter

        Returns:
            Dictionary with results, metadata, and errors from server
        """
        if not from_symbol or not isinstance(from_symbol, str):
            raise ValueError("from_symbol cannot be empty")
        if not to_symbol or not isinstance(to_symbol, str):
            raise ValueError("to_symbol cannot be empty")
        if not repository_alias:
            raise ValueError("repository_alias is required for remote SCIP queries")

        payload = {
            "repositories": [repository_alias],
            "symbol": from_symbol,
            "from_symbol": from_symbol,
            "to_symbol": to_symbol,
            "max_depth": min(max_depth, 20),
        }
        if project:
            payload["project"] = project

        return await self._execute_scip_request("/api/scip/multi/callchain", payload)

    def _combine_context_results(
        self, def_result: Dict[str, Any], ref_result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Combine definition and references results into context response."""
        combined: Dict[str, Any] = {
            "results": {},
            "metadata": {
                "total_results": 0,
                "repos_searched": 1,
                "execution_time_ms": 0,
            },
            "errors": {},
        }
        for repo_id, results in def_result.get("results", {}).items():
            combined["results"].setdefault(repo_id, [])
            for r in results:
                item = dict(r)  # Copy to avoid mutating input
                item["role"] = "definition"
                combined["results"][repo_id].append(item)
        for repo_id, results in ref_result.get("results", {}).items():
            combined["results"].setdefault(repo_id, [])
            for r in results:
                item = dict(r)  # Copy to avoid mutating input
                item["role"] = "reference"
                combined["results"][repo_id].append(item)
        combined["metadata"]["total_results"] = sum(
            len(r) for r in combined["results"].values()
        )
        for src in [def_result, ref_result]:
            for repo_id, error in src.get("errors", {}).items():
                if repo_id not in combined["errors"]:
                    combined["errors"][repo_id] = error
        return combined

    async def context(
        self,
        symbol: str,
        repository_alias: Optional[str] = None,
        limit: int = 20,
        project: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get smart context for a symbol (combines definition and references).

        Args:
            symbol: Symbol name to get context for
            repository_alias: Repository alias to query (required for remote)
            limit: Maximum number of reference results (default: 20, min: 1)
            project: Optional project path filter

        Returns:
            Dictionary with results, metadata, and errors from server
        """
        if not symbol or not isinstance(symbol, str):
            raise ValueError("Symbol cannot be empty")
        if not repository_alias:
            raise ValueError("repository_alias is required for remote SCIP queries")
        if limit < 1:
            raise ValueError("limit must be at least 1")

        def_payload = self._build_request_payload(
            symbol=symbol, repositories=[repository_alias], project=project
        )
        def_result = await self._execute_scip_request(
            "/api/scip/multi/definition", def_payload
        )
        ref_payload = self._build_request_payload(
            symbol=symbol, repositories=[repository_alias], limit=limit, project=project
        )
        ref_result = await self._execute_scip_request(
            "/api/scip/multi/references", ref_payload
        )
        return self._combine_context_results(def_result, ref_result)
