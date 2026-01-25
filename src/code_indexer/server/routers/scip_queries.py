"""
SCIP Query REST API Router.

Provides endpoints for SCIP call graph queries (definition, references, dependencies, dependents).

Story #704: All SCIP endpoints require authentication and apply group-based access filtering.
Users can only see SCIP results from repositories their group has access to.

Story #41: All routes delegate to SCIPQueryService for unified query execution and access control.
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import logging
from fastapi import APIRouter, Query, Depends, Request
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.scip_query_service import SCIPQueryService


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scip", tags=["SCIP Queries"])


# Response Models


class ScipResultItem(BaseModel):
    """Model for a single SCIP query result."""

    symbol: str = Field(..., description="Full SCIP symbol identifier")
    project: str = Field(..., description="Project path")
    file_path: str = Field(..., description="File path relative to project root")
    line: int = Field(..., description="Line number (1-indexed)")
    column: int = Field(..., description="Column number (0-indexed)")
    kind: str = Field(
        ..., description="Symbol kind (class, function, method, reference, etc.)"
    )
    relationship: Optional[str] = Field(
        None, description="Relationship type (import, call, etc.)"
    )
    context: Optional[str] = Field(
        None, description="Code context or additional information"
    )


class ScipDefinitionResponse(BaseModel):
    """Response model for SCIP definition query."""

    success: bool = Field(..., description="Whether the operation succeeded")
    symbol: str = Field(..., description="Symbol name that was searched for")
    total_results: int = Field(..., description="Total number of definitions found")
    results: List[ScipResultItem] = Field(
        ..., description="List of definition locations"
    )
    error: Optional[str] = Field(None, description="Error message if operation failed")


class ScipReferencesResponse(BaseModel):
    """Response model for SCIP references query."""

    success: bool = Field(..., description="Whether the operation succeeded")
    symbol: str = Field(..., description="Symbol name that was searched for")
    total_results: int = Field(..., description="Total number of references found")
    results: List[ScipResultItem] = Field(
        ..., description="List of reference locations"
    )
    error: Optional[str] = Field(None, description="Error message if operation failed")


def _get_scip_query_service(request: Request) -> SCIPQueryService:
    """Get SCIPQueryService instance for SCIP route handlers.

    Creates a SCIPQueryService configured with:
    - golden_repos_dir: From request.app.state (server configuration)
    - access_filtering_service: From request.app.state (for user-based filtering)

    Args:
        request: FastAPI request to access app.state

    Returns:
        SCIPQueryService instance ready for use by route handlers
    """
    golden_repos_dir = getattr(request.app.state, "golden_repos_dir", "")
    access_filtering_service = getattr(
        request.app.state, "access_filtering_service", None
    )

    return SCIPQueryService(
        golden_repos_dir=golden_repos_dir,
        access_filtering_service=access_filtering_service,
    )


async def _apply_scip_payload_truncation(
    results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Apply payload truncation to SCIP results.

    Delegates to the MCP handlers truncation function for consistency.

    Args:
        results: List of SCIP result dictionaries

    Returns:
        Results with truncated context fields if applicable
    """
    from code_indexer.server.mcp.handlers import _apply_scip_payload_truncation as mcp_truncate

    return await mcp_truncate(results)


@router.get("/definition")
async def get_definition(
    request: Request,
    symbol: str = Query(..., description="Symbol name to search for"),
    exact: bool = Query(False, description="If True, match exact symbol name"),
    project: Optional[str] = Query(None, description="Filter by specific project"),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Find definition locations for a symbol across all indexed projects.

    Args:
        symbol: Symbol name to search for (e.g., "UserService", "authenticate")
        exact: If True, match exact symbol name; if False, match substring
        project: Optional project filter (repository alias)
        current_user: Authenticated user (injected by dependency)

    Returns:
        JSON response with success status, symbol, total_results, and results list
    """
    try:
        service = _get_scip_query_service(request)
        results = service.find_definition(
            symbol=symbol,
            exact=exact,
            repository_alias=project,
            username=current_user.username,
        )

        # Apply SCIP payload truncation for consistency
        results = await _apply_scip_payload_truncation(results)

        return {
            "success": True,
            "symbol": symbol,
            "total_results": len(results),
            "results": results,
        }
    except Exception as e:
        logger.warning(
            f"Definition query failed: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return {"success": False, "error": str(e)}


@router.get("/references")
async def get_references(
    request: Request,
    symbol: str = Query(..., description="Symbol name to search for"),
    limit: int = Query(100, description="Maximum number of results to return"),
    exact: bool = Query(False, description="If True, match exact symbol name"),
    project: Optional[str] = Query(None, description="Filter by specific project"),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Find all references to a symbol across all indexed projects.

    Args:
        symbol: Symbol name to search for
        limit: Maximum number of results to return
        exact: If True, match exact symbol name; if False, match substring
        project: Optional project filter (repository alias)
        current_user: Authenticated user (injected by dependency)

    Returns:
        JSON response with success status, symbol, total_results, and results list
    """
    try:
        service = _get_scip_query_service(request)
        results = service.find_references(
            symbol=symbol,
            limit=limit,
            exact=exact,
            repository_alias=project,
            username=current_user.username,
        )

        # Apply SCIP payload truncation for consistency
        results = await _apply_scip_payload_truncation(results)

        return {
            "success": True,
            "symbol": symbol,
            "total_results": len(results),
            "results": results,
        }
    except Exception as e:
        logger.warning(
            f"References query failed: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return {"success": False, "error": str(e)}


@router.get("/dependencies")
async def get_dependencies(
    request: Request,
    symbol: str = Query(..., description="Symbol name to analyze"),
    depth: int = Query(1, description="Depth of transitive dependencies"),
    exact: bool = Query(False, description="If True, match exact symbol name"),
    project: Optional[str] = Query(None, description="Filter by specific project"),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Get symbols that the target symbol depends on.

    Args:
        symbol: Symbol name to analyze
        depth: Depth of transitive dependencies (1 = direct only)
        exact: If True, match exact symbol name; if False, match substring
        project: Optional project filter (repository alias)
        current_user: Authenticated user (injected by dependency)

    Returns:
        JSON response with success status, symbol, total_results, and results list
    """
    try:
        service = _get_scip_query_service(request)
        results = service.get_dependencies(
            symbol=symbol,
            depth=depth,
            exact=exact,
            repository_alias=project,
            username=current_user.username,
        )

        # Apply SCIP payload truncation for consistency
        results = await _apply_scip_payload_truncation(results)

        return {
            "success": True,
            "symbol": symbol,
            "total_results": len(results),
            "results": results,
        }
    except Exception as e:
        logger.warning(
            f"Dependencies query failed: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return {"success": False, "error": str(e)}


@router.get("/dependents")
async def get_dependents(
    request: Request,
    symbol: str = Query(..., description="Symbol name to analyze"),
    depth: int = Query(1, description="Depth of transitive dependents"),
    exact: bool = Query(False, description="If True, match exact symbol name"),
    project: Optional[str] = Query(None, description="Filter by specific project"),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Get symbols that depend on the target symbol.

    Args:
        symbol: Symbol name to analyze
        depth: Depth of transitive dependents (1 = direct only)
        exact: If True, match exact symbol name; if False, match substring
        project: Optional project filter (repository alias)
        current_user: Authenticated user (injected by dependency)

    Returns:
        JSON response with success status, symbol, total_results, and results list
    """
    try:
        service = _get_scip_query_service(request)
        results = service.get_dependents(
            symbol=symbol,
            depth=depth,
            exact=exact,
            repository_alias=project,
            username=current_user.username,
        )

        # Apply SCIP payload truncation for consistency
        results = await _apply_scip_payload_truncation(results)

        return {
            "success": True,
            "symbol": symbol,
            "total_results": len(results),
            "results": results,
        }
    except Exception as e:
        logger.warning(
            f"Dependents query failed: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return {"success": False, "error": str(e)}


@router.get("/impact")
async def get_impact(
    request: Request,
    symbol: str = Query(..., description="Symbol name to analyze"),
    depth: int = Query(
        3, ge=1, le=10, description="Maximum traversal depth (default 3, max 10)"
    ),
    project: Optional[str] = Query(None, description="Filter by specific project"),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Analyze impact of changes to a symbol.

    Args:
        symbol: Target symbol to analyze
        depth: Maximum traversal depth (default 3, max 10)
        project: Optional project filter (repository alias)
        current_user: Authenticated user (injected by dependency)

    Returns:
        JSON response with impact analysis results
    """
    try:
        service = _get_scip_query_service(request)
        result = service.analyze_impact(
            symbol=symbol,
            depth=depth,
            repository_alias=project,
            username=current_user.username,
        )

        return {
            "success": True,
            "target_symbol": result["target_symbol"],
            "depth_analyzed": result["depth_analyzed"],
            "total_affected": result["total_affected"],
            "truncated": result["truncated"],
            "affected_symbols": result["affected_symbols"],
            "affected_files": result["affected_files"],
        }
    except Exception as e:
        logger.warning(
            f"Impact analysis failed: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return {"success": False, "error": str(e)}


@router.get("/callchain")
async def get_callchain(
    request: Request,
    from_symbol: str = Query(..., description="Starting symbol"),
    to_symbol: str = Query(..., description="Target symbol"),
    max_depth: int = Query(
        10, ge=1, le=20, description="Maximum chain length (default 10, max 20)"
    ),
    project: Optional[str] = Query(None, description="Filter by specific project"),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Find call chains between two symbols.

    Args:
        from_symbol: Starting symbol
        to_symbol: Target symbol
        max_depth: Maximum chain length (default 10, max 20)
        project: Optional project filter (repository alias)
        current_user: Authenticated user (injected by dependency)

    Returns:
        JSON response with call chain results
    """
    try:
        service = _get_scip_query_service(request)
        chains = service.trace_callchain(
            from_symbol=from_symbol,
            to_symbol=to_symbol,
            max_depth=max_depth,
            repository_alias=project,
            username=current_user.username,
        )

        return {
            "success": True,
            "from_symbol": from_symbol,
            "to_symbol": to_symbol,
            "total_chains_found": len(chains),
            "chains": chains,
        }
    except Exception as e:
        logger.warning(
            f"Call chain tracing failed: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return {"success": False, "error": str(e)}


@router.get("/context")
async def get_context(
    request: Request,
    symbol: str = Query(..., description="Symbol name to analyze"),
    limit: int = Query(
        20, ge=1, le=100, description="Maximum files to return (default 20, max 100)"
    ),
    min_score: float = Query(
        0.0,
        ge=0.0,
        le=1.0,
        description="Minimum relevance score (default 0.0, range 0.0-1.0)",
    ),
    project: Optional[str] = Query(None, description="Filter by specific project"),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Get smart context for a symbol.

    Args:
        symbol: Target symbol
        limit: Maximum files to return (default 20, max 100)
        min_score: Minimum relevance score (0.0-1.0)
        project: Optional project filter (repository alias)
        current_user: Authenticated user (injected by dependency)

    Returns:
        JSON response with smart context results
    """
    try:
        service = _get_scip_query_service(request)
        result = service.get_context(
            symbol=symbol,
            limit=limit,
            min_score=min_score,
            repository_alias=project,
            username=current_user.username,
        )

        return {
            "success": True,
            "target_symbol": result["target_symbol"],
            "summary": result["summary"],
            "total_files": result["total_files"],
            "total_symbols": result["total_symbols"],
            "avg_relevance": result["avg_relevance"],
            "files": result["files"],
        }
    except Exception as e:
        logger.warning(
            f"Smart context query failed: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return {"success": False, "error": str(e)}
