"""
File CRUD REST API Router.

Provides REST endpoints for file create, edit, and delete operations
with OAuth authentication and service layer integration.
"""

import logging
from typing import Callable, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from code_indexer.server.auth.dependencies import require_permission
from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.cidx_meta_access.file_doors import (
    CatalogFileAccess,
    judge_app_request,
)
from code_indexer.server.services.file_crud_service import (
    HashMismatchError,
    WriteModeLockUnavailableError,
    file_crud_service,
)
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.routers import repo_access_http

logger = logging.getLogger(__name__)

# Create router with prefix and tags
router = APIRouter(prefix="/api/v1/repos/{alias}", tags=["files"])


def _require_activation_access(alias: str, user: User) -> None:
    """An alias that is not a registered global write exception names the
    caller's own activation: require the caller's grants on its source
    golden repositories (the MCP rule), refusing like an alias never
    activated. Called first in each (sync) route body, outside its broad
    except."""
    if file_crud_service.is_write_exception(alias):
        return
    repo_access_http.enforce_activated_repo_access(
        repo_access_http.module_app_access_filtering_service(), user.username, alias
    )


def _lock_store_unavailable(alias: str, exc: Exception) -> HTTPException:
    """503 for a transient write-mode lock store error: retry, nothing written."""
    logger.warning(f"Write-mode lock store unavailable for {alias}")
    unavailable = status.HTTP_503_SERVICE_UNAVAILABLE
    return HTTPException(status_code=unavailable, detail=str(exc))


def _catalog_judge(
    request: Request, alias: str, user: User
) -> Callable[[str], CatalogFileAccess]:
    """The request's catalog judgement of the directory a write lands in."""
    state = request.app.state
    return lambda served: judge_app_request(
        state,
        alias,
        served,
        user.username,
        getattr(state, "activated_repo_manager", None),
        may_write=user.has_permission("repository:write"),
    )


# Request/Response Models


class CreateFileRequest(BaseModel):
    """Request model for creating a file."""

    file_path: str = Field(..., description="Path to the file within repository")
    content: str = Field(..., description="File content")


class CreateFileResponse(BaseModel):
    """Response model for file creation."""

    success: bool = Field(..., description="Operation success status")
    file_path: str = Field(..., description="Created file path")
    content_hash: str = Field(..., description="SHA-256 hash of file content")
    size_bytes: int = Field(..., description="File size in bytes")
    created_at: str = Field(..., description="Creation timestamp (ISO 8601)")
    error: Optional[str] = Field(
        None, description="Error message (present when success=false)"
    )


class EditFileRequest(BaseModel):
    """Request model for editing a file."""

    old_string: str = Field(..., description="String to search for in file")
    new_string: str = Field(..., description="String to replace with")
    content_hash: str = Field(..., description="Expected content hash for verification")
    replace_all: bool = Field(
        False, description="Replace all occurrences (default: first only)"
    )


class EditFileResponse(BaseModel):
    """Response model for file edit."""

    success: bool = Field(..., description="Operation success status")
    file_path: str = Field(..., description="Edited file path")
    content_hash: str = Field(..., description="SHA-256 hash of new content")
    modified_at: str = Field(..., description="Modification timestamp (ISO 8601)")
    changes_made: int = Field(..., description="Number of replacements made")
    error: Optional[str] = Field(
        None, description="Error message (present when success=false)"
    )


class DeleteFileResponse(BaseModel):
    """Response model for file deletion."""

    success: bool = Field(..., description="Operation success status")
    file_path: str = Field(..., description="Deleted file path")
    deleted_at: str = Field(..., description="Deletion timestamp (ISO 8601)")


# Endpoints


@router.post(
    "/files",
    status_code=status.HTTP_201_CREATED,
    response_model=CreateFileResponse,
    responses={
        201: {"description": "File created successfully"},
        400: {"description": "Invalid parameters"},
        401: {"description": "Missing or invalid authentication"},
        403: {"description": "Permission denied (.git/ access blocked)"},
        404: {"description": "Repository not found"},
        409: {"description": "File already exists"},
    },
    summary="Create a new file",
    description="Create a new file in the activated repository with specified content",
)
def create_file(
    alias: str,
    request: CreateFileRequest,
    http_request: Request,
    user: User = Depends(require_permission("repository:write")),
) -> CreateFileResponse:
    """
    Create a new file in the repository.

    Args:
        alias: Repository alias
        request: File creation request with path and content
        user: Authenticated user (from OAuth/JWT)

    Returns:
        CreateFileResponse with file metadata

    Raises:
        HTTPException: On various error conditions
    """
    _require_activation_access(alias, user)
    try:
        service = file_crud_service
        result = service.create_file(
            repo_alias=alias,
            file_path=request.file_path,
            content=request.content,
            username=user.username,
            access_for=_catalog_judge(http_request, alias, user),
        )
        return CreateFileResponse(**result)

    except FileExistsError as e:
        logger.warning(
            format_error_log(
                "STORE-GENERAL-018",
                f"File already exists: {alias}/{request.file_path}",
            )
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except WriteModeLockUnavailableError as e:
        raise _lock_store_unavailable(alias, e)
    except PermissionError as e:
        logger.warning(
            format_error_log(
                "STORE-GENERAL-019",
                f"Permission denied for {alias}/{request.file_path}: {e}",
            )
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except FileNotFoundError as e:
        logger.warning(
            format_error_log(
                "STORE-GENERAL-020",
                f"Repository not found: {alias}",
            )
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        logger.warning(
            format_error_log(
                "STORE-GENERAL-021",
                f"Invalid request for {alias}/{request.file_path}: {e}",
            )
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-022",
                f"Create file failed for {alias}/{request.file_path}: {e}",
            ),
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {str(e)}",
        )


@router.patch(
    "/files/{file_path:path}",
    status_code=status.HTTP_200_OK,
    response_model=EditFileResponse,
    responses={
        200: {"description": "File edited successfully"},
        400: {"description": "Invalid parameters"},
        401: {"description": "Missing or invalid authentication"},
        403: {"description": "Permission denied (.git/ access blocked)"},
        404: {"description": "Repository or file not found"},
        409: {"description": "Hash mismatch - file was modified"},
    },
    summary="Edit an existing file",
    description="Edit an existing file in the activated repository using string replacement",
)
def edit_file(
    alias: str,
    file_path: str,
    request: EditFileRequest,
    http_request: Request,
    user: User = Depends(require_permission("repository:write")),
) -> EditFileResponse:
    """
    Edit an existing file in the repository.

    Args:
        alias: Repository alias
        file_path: Path to the file within repository (can contain slashes)
        request: File edit request with old_string, new_string, content_hash, replace_all
        user: Authenticated user (from OAuth/JWT)

    Returns:
        EditFileResponse with updated file metadata and changes_made count

    Raises:
        HTTPException: On various error conditions
    """
    _require_activation_access(alias, user)
    try:
        service = file_crud_service
        result = service.edit_file(
            repo_alias=alias,
            file_path=file_path,
            old_string=request.old_string,
            new_string=request.new_string,
            content_hash=request.content_hash,
            replace_all=request.replace_all,
            username=user.username,
            access_for=_catalog_judge(http_request, alias, user),
        )
        return EditFileResponse(**result)

    except HashMismatchError as e:
        logger.warning(
            format_error_log(
                "STORE-GENERAL-023",
                f"Hash mismatch for {alias}/{file_path}: {e}",
            )
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except WriteModeLockUnavailableError as e:
        raise _lock_store_unavailable(alias, e)
    except PermissionError as e:
        logger.warning(
            format_error_log(
                "STORE-GENERAL-024",
                f"Permission denied for {alias}/{file_path}: {e}",
            )
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except FileNotFoundError as e:
        logger.warning(
            format_error_log(
                "STORE-GENERAL-025",
                f"File or repository not found: {alias}/{file_path}",
            )
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        logger.warning(
            format_error_log(
                "SVC-GENERAL-001",
                f"Invalid request for {alias}/{file_path}: {e}",
            )
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(
            format_error_log(
                "SVC-GENERAL-002",
                f"Edit file failed for {alias}/{file_path}: {e}",
            ),
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {str(e)}",
        )


@router.delete(
    "/files/{file_path:path}",
    status_code=status.HTTP_200_OK,
    response_model=DeleteFileResponse,
    responses={
        200: {"description": "File deleted successfully"},
        401: {"description": "Missing or invalid authentication"},
        403: {"description": "Permission denied (.git/ access blocked)"},
        404: {"description": "Repository or file not found"},
    },
    summary="Delete a file",
    description="Delete a file from the activated repository",
)
def delete_file(
    alias: str,
    file_path: str,
    http_request: Request,
    content_hash: Optional[str] = Query(
        None, description="Expected content hash for verification"
    ),
    user: User = Depends(require_permission("repository:write")),
) -> DeleteFileResponse:
    """
    Delete a file from the repository.

    Args:
        alias: Repository alias
        file_path: Path to the file within repository (can contain slashes)
        content_hash: Optional content hash for verification
        user: Authenticated user (from OAuth/JWT)

    Returns:
        DeleteFileResponse with deletion confirmation

    Raises:
        HTTPException: On various error conditions
    """
    _require_activation_access(alias, user)
    try:
        service = file_crud_service
        result = service.delete_file(
            repo_alias=alias,
            file_path=file_path,
            content_hash=content_hash,
            username=user.username,
            access_for=_catalog_judge(http_request, alias, user),
        )
        return DeleteFileResponse(**result)

    except WriteModeLockUnavailableError as e:
        raise _lock_store_unavailable(alias, e)
    except PermissionError as e:
        logger.warning(
            format_error_log(
                "SVC-GENERAL-003",
                f"Permission denied for {alias}/{file_path}: {e}",
            )
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except FileNotFoundError as e:
        logger.warning(
            format_error_log(
                "SVC-GENERAL-004",
                f"File or repository not found: {alias}/{file_path}",
            )
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(
            format_error_log(
                "SVC-GENERAL-005",
                f"Delete file failed for {alias}/{file_path}: {e}",
            ),
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {str(e)}",
        )
