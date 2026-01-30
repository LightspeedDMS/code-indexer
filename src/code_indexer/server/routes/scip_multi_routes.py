"""
SCIP Multi-Repository Intelligence REST API Routes (Story #677).

Provides endpoints for SCIP operations across multiple repositories:
- /api/scip/multi/definition - Symbol definition lookup (AC1)
- /api/scip/multi/references - Symbol references lookup (AC2)
- /api/scip/multi/dependencies - Dependency analysis (AC3)
- /api/scip/multi/dependents - Dependents analysis (AC4)
- /api/scip/multi/callchain - Call chain tracing (AC5)

All endpoints require JWT authentication and support:
- Parallel execution across repositories
- Timeout enforcement (30s default per repo) - AC7
- Partial failure handling - AC8
- Result aggregation with repository attribution - AC6
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional, Dict, Any

from code_indexer.server.logging_utils import format_error_log, get_log_extra

from ..auth.dependencies import get_current_user
from ..auth.user_manager import User
from ..multi.scip_models import SCIPMultiRequest, SCIPMultiResponse
from ..multi.scip_multi_service import SCIPMultiService

logger = logging.getLogger(__name__)

# Create router with /api/scip/multi prefix
router = APIRouter(prefix="/api/scip/multi", tags=["scip-multi"])

# Initialize SCIP multi-service
_scip_multi_service: Optional[SCIPMultiService] = None


def get_scip_multi_service() -> SCIPMultiService:
    """
    Get or create SCIPMultiService instance.

    Uses ConfigService for configuration (Story #25) instead of defaults.
    Reads scip_multi_max_workers and scip_multi_timeout_seconds from
    the Web UI Configuration system.

    Returns:
        SCIPMultiService instance
    """
    global _scip_multi_service
    if _scip_multi_service is None:
        from ..services.config_service import get_config_service

        config_service = get_config_service()
        server_config = config_service.get_config()
        multi_search_limits = server_config.multi_search_limits_config
        assert multi_search_limits is not None  # Guaranteed by ServerConfig.__post_init__
        # Bug #83-3 Fix: Pass SCIP query limits from config
        scip_config = server_config.scip_config
        assert scip_config is not None  # Guaranteed by ServerConfig.__post_init__

        _scip_multi_service = SCIPMultiService(
            max_workers=multi_search_limits.scip_multi_max_workers,
            query_timeout_seconds=multi_search_limits.scip_multi_timeout_seconds,
            reference_limit=scip_config.scip_reference_limit,
            dependency_depth=scip_config.scip_dependency_depth,
            callchain_max_depth=scip_config.scip_callchain_max_depth,
            callchain_limit=scip_config.scip_callchain_limit,
        )
    return _scip_multi_service


def _apply_multi_scip_truncation(
    response: SCIPMultiResponse,
) -> Dict[str, Any]:
    """Apply SCIP payload truncation to multi-repo response results (Story #685).

    Story #50: Converted from async to sync because _apply_scip_payload_truncation
    in handlers.py is a sync function. FastAPI can run sync handlers in threadpool.

    Converts SCIPMultiResponse to dict and applies truncation to each
    repository's results list, handling the context field truncation.

    Args:
        response: SCIPMultiResponse with results grouped by repository

    Returns:
        Dict representation with truncated context fields
    """
    # Lazy import to avoid circular dependency with handlers.py
    from ..mcp.handlers import _apply_scip_payload_truncation

    # Convert response to dict for modification
    response_dict: Dict[str, Any] = response.model_dump()

    # Apply truncation to each repository's results
    for repo_id, results_list in response_dict["results"].items():
        if results_list:
            # Apply truncation (sync call - _apply_scip_payload_truncation is sync)
            truncated_results = _apply_scip_payload_truncation(results_list)
            response_dict["results"][repo_id] = truncated_results

    return response_dict


@router.post("/definition", response_model=SCIPMultiResponse)
def multi_repository_definition(
    request: SCIPMultiRequest,
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Find symbol definition across multiple repositories (AC1: Multi-Repository Definition Lookup).

    Story #50: Converted from async to sync. FastAPI runs sync handlers in threadpool.

    Performs parallel definition lookup across specified repositories with:
    - Authentication enforcement (JWT token required)
    - Request validation (Pydantic models)
    - Timeout handling (30s default per repo)
    - Partial failure support (some repos succeed, others fail)
    - Result aggregation with repository attribution

    **Authentication**: Requires valid JWT token in Authorization header.

    **Request Body**:
    ```json
    {
        "repositories": ["repo1", "repo2"],
        "symbol": "com.example.User",
        "timeout_seconds": 30
    }
    ```

    **Response Structure**:
    ```json
    {
        "results": {
            "repo1": [
                {
                    "symbol": "com.example.User",
                    "file_path": "src/User.java",
                    "line": 10,
                    "repository": "repo1"
                }
            ],
            "repo2": []
        },
        "metadata": {
            "total_results": 1,
            "repos_searched": 2,
            "execution_time_ms": 150
        },
        "errors": {}
    }
    ```

    **Timeout Behavior** (AC7):
    - Each repository has 30s timeout (configurable via timeout_seconds)
    - Timed out repos return error in `errors` field
    - Successful repos return results even if others time out

    **SCIP Index Availability** (AC8):
    - Repos without SCIP index return error in `errors` field
    - Repos with SCIP index return results
    - Empty results when symbol not found (no error)

    **Error Handling**:
    - Repository not found → error in `errors` field, other repos succeed
    - No SCIP index → error in `errors` field, other repos succeed
    - Invalid symbol → 422 Unprocessable Entity
    - Authentication failure → 401 Unauthorized
    - Unexpected error → 500 Internal Server Error

    Args:
        request: SCIP multi-request with repositories and symbol
        user: Authenticated user (injected by dependency)

    Returns:
        SCIPMultiResponse with results grouped by repository, metadata, and errors

    Raises:
        HTTPException: 401 if authentication fails
        HTTPException: 422 if request validation fails
        HTTPException: 500 if unexpected error occurs
    """
    try:
        # Log request
        logger.info(
            f"SCIP multi-definition request from user {user.username}: "
            f"{len(request.repositories)} repos, symbol={request.symbol}"
        )

        # Get service instance
        service = get_scip_multi_service()

        # Execute definition lookup
        response = service.definition(request)

        # Log response summary
        logger.info(
            f"SCIP multi-definition completed: {response.metadata.total_results} results "
            f"from {response.metadata.repos_searched} repos "
            f"in {response.metadata.execution_time_ms}ms"
        )

        # Story #685: Apply SCIP payload truncation to context fields (sync call)
        return _apply_multi_scip_truncation(response)

    except ValueError as e:
        # Validation error from service
        logger.error(format_error_log(
            "WEB-GENERAL-031",
            "SCIP multi-definition validation error",
            error=str(e)),
            extra=get_log_extra("WEB-GENERAL-031")
        )
        raise HTTPException(status_code=422, detail=str(e))

    except Exception as e:
        # Unexpected error
        logger.error(format_error_log(
            "WEB-GENERAL-032",
            "SCIP multi-definition failed",
            error=str(e)),
            extra=get_log_extra("WEB-GENERAL-032"),
            exc_info=True
        )
        raise HTTPException(
            status_code=500, detail=f"SCIP multi-definition failed: {str(e)}"
        )


@router.post("/references", response_model=SCIPMultiResponse)
def multi_repository_references(
    request: SCIPMultiRequest,
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Find symbol references across multiple repositories (AC2: Multi-Repository Reference Lookup).

    Story #50: Converted from async to sync. FastAPI runs sync handlers in threadpool.

    Performs parallel references lookup across specified repositories with:
    - Authentication enforcement (JWT token required)
    - Request validation (Pydantic models)
    - Timeout handling (30s default per repo)
    - Partial failure support (some repos succeed, others fail)
    - Result aggregation with repository attribution
    - Limit parameter to control results per repository

    **Authentication**: Requires valid JWT token in Authorization header.

    **Request Body**:
    ```json
    {
        "repositories": ["repo1", "repo2"],
        "symbol": "com.example.User",
        "limit": 100,
        "timeout_seconds": 30
    }
    ```

    Args:
        request: SCIP multi-request with repositories, symbol, and optional limit
        user: Authenticated user (injected by dependency)

    Returns:
        SCIPMultiResponse with references grouped by repository, metadata, and errors

    Raises:
        HTTPException: 401 if authentication fails
        HTTPException: 422 if request validation fails
        HTTPException: 500 if unexpected error occurs
    """
    try:
        # Log request
        logger.info(
            f"SCIP multi-references request from user {user.username}: "
            f"{len(request.repositories)} repos, symbol={request.symbol}, limit={request.limit}"
        )

        # Get service instance
        service = get_scip_multi_service()

        # Execute references lookup
        response = service.references(request)

        # Log response summary
        logger.info(
            f"SCIP multi-references completed: {response.metadata.total_results} results "
            f"from {response.metadata.repos_searched} repos "
            f"in {response.metadata.execution_time_ms}ms"
        )

        # Story #685: Apply SCIP payload truncation to context fields (sync call)
        return _apply_multi_scip_truncation(response)

    except ValueError as e:
        # Validation error from service
        logger.error(format_error_log(
            "WEB-GENERAL-033",
            "SCIP multi-references validation error",
            error=str(e)),
            extra=get_log_extra("WEB-GENERAL-033")
        )
        raise HTTPException(status_code=422, detail=str(e))

    except Exception as e:
        # Unexpected error
        logger.error(format_error_log(
            "WEB-GENERAL-034",
            "SCIP multi-references failed",
            error=str(e)),
            extra=get_log_extra("WEB-GENERAL-034"),
            exc_info=True
        )
        raise HTTPException(
            status_code=500, detail=f"SCIP multi-references failed: {str(e)}"
        )


@router.post("/dependencies", response_model=SCIPMultiResponse)
def multi_repository_dependencies(
    request: SCIPMultiRequest,
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Analyze symbol dependencies across multiple repositories (AC3: Multi-Repository Dependency Analysis).

    Story #50: Converted from async to sync. FastAPI runs sync handlers in threadpool.

    Performs parallel dependency analysis across specified repositories with:
    - Authentication enforcement (JWT token required)
    - Request validation (Pydantic models)
    - Timeout handling (30s default per repo)
    - Partial failure support (some repos succeed, others fail)
    - Result aggregation with repository attribution
    - Max depth parameter to control traversal depth

    **Authentication**: Requires valid JWT token in Authorization header.

    **Request Body**:
    ```json
    {
        "repositories": ["repo1", "repo2"],
        "symbol": "com.example.Service",
        "max_depth": 3,
        "timeout_seconds": 30
    }
    ```

    Args:
        request: SCIP multi-request with repositories, symbol, and optional max_depth
        user: Authenticated user (injected by dependency)

    Returns:
        SCIPMultiResponse with dependencies grouped by repository, metadata, and errors

    Raises:
        HTTPException: 401 if authentication fails
        HTTPException: 422 if request validation fails
        HTTPException: 500 if unexpected error occurs
    """
    try:
        # Log request
        logger.info(
            f"SCIP multi-dependencies request from user {user.username}: "
            f"{len(request.repositories)} repos, symbol={request.symbol}, max_depth={request.max_depth}"
        )

        # Get service instance
        service = get_scip_multi_service()

        # Execute dependencies analysis
        response = service.dependencies(request)

        # Log response summary
        logger.info(
            f"SCIP multi-dependencies completed: {response.metadata.total_results} results "
            f"from {response.metadata.repos_searched} repos "
            f"in {response.metadata.execution_time_ms}ms"
        )

        # Story #685: Apply SCIP payload truncation to context fields (sync call)
        return _apply_multi_scip_truncation(response)

    except ValueError as e:
        # Validation error from service
        logger.error(format_error_log(
            "WEB-GENERAL-035",
            "SCIP multi-dependencies validation error",
            error=str(e)),
            extra=get_log_extra("WEB-GENERAL-035")
        )
        raise HTTPException(status_code=422, detail=str(e))

    except Exception as e:
        # Unexpected error
        logger.error(format_error_log(
            "WEB-GENERAL-036",
            "SCIP multi-dependencies failed",
            error=str(e)),
            extra=get_log_extra("WEB-GENERAL-036"),
            exc_info=True
        )
        raise HTTPException(
            status_code=500, detail=f"SCIP multi-dependencies failed: {str(e)}"
        )


@router.post("/dependents", response_model=SCIPMultiResponse)
def multi_repository_dependents(
    request: SCIPMultiRequest,
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Analyze symbol dependents across multiple repositories (AC4: Multi-Repository Dependents Analysis).

    Story #50: Converted from async to sync. FastAPI runs sync handlers in threadpool.

    Performs parallel dependents analysis across specified repositories with:
    - Authentication enforcement (JWT token required)
    - Request validation (Pydantic models)
    - Timeout handling (30s default per repo)
    - Partial failure support (some repos succeed, others fail)
    - Result aggregation with repository attribution
    - Max depth parameter to control traversal depth

    **Authentication**: Requires valid JWT token in Authorization header.

    **Request Body**:
    ```json
    {
        "repositories": ["repo1", "repo2"],
        "symbol": "com.example.Database",
        "max_depth": 3,
        "timeout_seconds": 30
    }
    ```

    Args:
        request: SCIP multi-request with repositories, symbol, and optional max_depth
        user: Authenticated user (injected by dependency)

    Returns:
        SCIPMultiResponse with dependents grouped by repository, metadata, and errors

    Raises:
        HTTPException: 401 if authentication fails
        HTTPException: 422 if request validation fails
        HTTPException: 500 if unexpected error occurs
    """
    try:
        # Log request
        logger.info(
            f"SCIP multi-dependents request from user {user.username}: "
            f"{len(request.repositories)} repos, symbol={request.symbol}, max_depth={request.max_depth}"
        )

        # Get service instance
        service = get_scip_multi_service()

        # Execute dependents analysis
        response = service.dependents(request)

        # Log response summary
        logger.info(
            f"SCIP multi-dependents completed: {response.metadata.total_results} results "
            f"from {response.metadata.repos_searched} repos "
            f"in {response.metadata.execution_time_ms}ms"
        )

        # Story #685: Apply SCIP payload truncation to context fields (sync call)
        return _apply_multi_scip_truncation(response)

    except ValueError as e:
        # Validation error from service
        logger.error(format_error_log(
            "WEB-GENERAL-037",
            "SCIP multi-dependents validation error",
            error=str(e)),
            extra=get_log_extra("WEB-GENERAL-037")
        )
        raise HTTPException(status_code=422, detail=str(e))

    except Exception as e:
        # Unexpected error
        logger.error(format_error_log(
            "WEB-GENERAL-038",
            "SCIP multi-dependents failed",
            error=str(e)),
            extra=get_log_extra("WEB-GENERAL-038"),
            exc_info=True
        )
        raise HTTPException(
            status_code=500, detail=f"SCIP multi-dependents failed: {str(e)}"
        )


@router.post("/callchain", response_model=SCIPMultiResponse)
def multi_repository_callchain(
    request: SCIPMultiRequest,
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Trace call chains across multiple repositories (AC5: Per-Repository Call Chain Tracing).

    Story #50: Converted from async to sync. FastAPI runs sync handlers in threadpool.

    Performs parallel call chain tracing across specified repositories with:
    - Authentication enforcement (JWT token required)
    - Request validation (Pydantic models)
    - Timeout handling (30s default per repo)
    - Partial failure support (some repos succeed, others fail)
    - Result aggregation with repository attribution
    - Per-repository call chain tracing (no cross-repo stitching)

    **Authentication**: Requires valid JWT token in Authorization header.

    **Request Body**:
    ```json
    {
        "repositories": ["repo1", "repo2"],
        "from_symbol": "com.example.main",
        "to_symbol": "com.example.saveData",
        "timeout_seconds": 30
    }
    ```

    **Note**: Call chains are traced within each repository independently.
    No cross-repository call chain stitching is performed (AC5).

    Args:
        request: SCIP multi-request with repositories, from_symbol, and to_symbol
        user: Authenticated user (injected by dependency)

    Returns:
        SCIPMultiResponse with call chains grouped by repository, metadata, and errors

    Raises:
        HTTPException: 401 if authentication fails
        HTTPException: 422 if request validation fails
        HTTPException: 500 if unexpected error occurs
    """
    try:
        # Log request
        logger.info(
            f"SCIP multi-callchain request from user {user.username}: "
            f"{len(request.repositories)} repos, from={request.from_symbol}, to={request.to_symbol}"
        )

        # Get service instance
        service = get_scip_multi_service()

        # Execute callchain tracing
        response = service.callchain(request)

        # Log response summary
        logger.info(
            f"SCIP multi-callchain completed: {response.metadata.total_results} results "
            f"from {response.metadata.repos_searched} repos "
            f"in {response.metadata.execution_time_ms}ms"
        )

        # Story #685: Apply SCIP payload truncation to context fields (sync call)
        return _apply_multi_scip_truncation(response)

    except ValueError as e:
        # Validation error from service
        logger.error(format_error_log(
            "WEB-GENERAL-039",
            "SCIP multi-callchain validation error",
            error=str(e)),
            extra=get_log_extra("WEB-GENERAL-039")
        )
        raise HTTPException(status_code=422, detail=str(e))

    except Exception as e:
        # Unexpected error
        logger.error(format_error_log(
            "WEB-GENERAL-040",
            "SCIP multi-callchain failed",
            error=str(e)),
            extra=get_log_extra("WEB-GENERAL-040"),
            exc_info=True
        )
        raise HTTPException(
            status_code=500, detail=f"SCIP multi-callchain failed: {str(e)}"
        )
