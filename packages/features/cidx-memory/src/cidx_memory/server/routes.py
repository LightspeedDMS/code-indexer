"""
Memories REST API Router — Story #877.

Provides REST endpoints for shared technical memory CRUD:
  POST   /api/v1/memories          — create a new memory
  PUT    /api/v1/memories/{id}     — full replacement edit (If-Match required)
  DELETE /api/v1/memories/{id}     — delete (If-Match required)

Contributed to the core as a router (``routers()``, ``auth="api"``): the core
authenticates every request (bearer, API key or session JWT) and puts the
caller on ``request.state`` (:data:`cidx_sdk.PRINCIPAL_STATE_ATTR`); the
routes read it through :func:`current_principal`.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from cidx_sdk import PRINCIPAL_STATE_ATTR, Principal

from cidx_memory._platform import server_platform
from cidx_memory.server import runtime
from cidx_memory.server.memory_schema import MemorySchemaValidationError
from cidx_memory.server.memory_store_service import (
    ConflictError,
    NotFoundError,
    RateLimitError,
    StaleContentError,
)

logger = logging.getLogger(__name__)

#: Where the core mounts :data:`router`.
ROUTER_PREFIX = "/api/v1/memories"

router = APIRouter(tags=["memories"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class MemoryPayloadBase(BaseModel):
    """Shared fields for create and edit memory requests."""

    type: str = Field(
        ...,
        description=(
            "Memory type: architectural-fact | gotcha | config-behavior | "
            "api-contract | performance-note"
        ),
    )
    scope: str = Field(..., description="Scope: global | repo | file")
    scope_target: Optional[str] = Field(
        None, description="Scope target (null when scope is global)"
    )
    referenced_repo: Optional[str] = Field(
        None, description="Referenced repo alias (null when scope is global)"
    )
    summary: str = Field(..., description="Short summary of the memory")
    evidence: List[Dict[str, Any]] = Field(
        ..., description="Evidence entries: [{file, lines}] or [{commit}]"
    )
    body: str = Field("", description="Optional markdown body")


class CreateMemoryRequest(MemoryPayloadBase):
    """Request body for POST /api/v1/memories."""


class EditMemoryRequest(MemoryPayloadBase):
    """Request body for PUT /api/v1/memories/{memory_id} — full replacement."""


class MemoryWriteResponse(BaseModel):
    """Response body for create and edit operations."""

    id: str = Field(..., description="Memory identifier (UUID hex)")
    content_hash: str = Field(..., description="SHA-256 hash of the written content")
    path: str = Field(..., description="Filesystem path of the memory file")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def current_principal(request: Request) -> Principal:
    """The caller the core authenticated for this request (401 without one:
    the router is mounted outside the core's authentication)."""
    principal = getattr(request.state, PRINCIPAL_STATE_ATTR, None)
    if not isinstance(principal, Principal):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return principal


def format_error_log(error_code: str, message: str, **context: Any) -> str:
    """The core's structured error-log line."""
    return server_platform().format_error_log(error_code, message, **context)


def _get_service(request: Request):
    """Return the running feature's MemoryStoreService or raise 503."""
    svc = runtime.memory_store()
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="memory store service not available",
        )
    return svc


def _stale_content_response(exc: StaleContentError) -> JSONResponse:
    """Build 409 response with top-level current_content_hash field."""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc), "current_content_hash": exc.current_hash},
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=MemoryWriteResponse,
    summary="Create a new memory",
    responses={
        201: {"description": "Memory created"},
        400: {"description": "Invalid input"},
        422: {"description": "Schema validation error"},
        423: {"description": "Memory locked by another writer"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Service unavailable"},
    },
)
def create_memory(
    request: Request,
    body: CreateMemoryRequest,
    user: Principal = Depends(current_principal),
) -> MemoryWriteResponse:
    """Create a new shared technical memory."""
    service = _get_service(request)
    payload = body.model_dump()
    try:
        result = service.create_memory(payload, username=user.username)
        return MemoryWriteResponse(**result)
    except MemorySchemaValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except RateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        )
    except ConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=str(exc),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except Exception as exc:
        logger.error(
            format_error_log(
                "MEM-GENERAL-001",
                f"create_memory failed: {exc}",
            ),
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {exc}",
        )


@router.put(
    "/{memory_id}",
    status_code=status.HTTP_200_OK,
    response_model=MemoryWriteResponse,
    summary="Edit an existing memory (full replacement)",
    responses={
        200: {"description": "Memory updated"},
        400: {"description": "Invalid input"},
        404: {"description": "Memory not found"},
        409: {"description": "Stale content — re-read and retry"},
        422: {"description": "Schema validation error"},
        423: {"description": "Memory locked by another writer"},
        428: {"description": "If-Match header required"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Service unavailable"},
    },
)
def edit_memory(
    memory_id: str,
    request: Request,
    body: EditMemoryRequest,
    if_match: Optional[str] = Header(None, alias="If-Match"),
    user: Principal = Depends(current_principal),
):
    """Full-replacement edit of a shared technical memory (PUT semantics).

    Requires the ``If-Match`` header containing the current content hash.
    Returns 428 if the header is absent.
    """
    if if_match is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match header required",
        )
    service = _get_service(request)
    payload = body.model_dump()
    try:
        result = service.edit_memory(
            memory_id,
            payload,
            expected_content_hash=if_match,
            username=user.username,
        )
        return MemoryWriteResponse(**result)
    except StaleContentError as exc:
        return _stale_content_response(exc)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except ConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=str(exc),
        )
    except MemorySchemaValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except RateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except Exception as exc:
        logger.error(
            format_error_log(
                "MEM-GENERAL-002",
                f"edit_memory failed for {memory_id!r}: {exc}",
            ),
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {exc}",
        )


@router.delete(
    "/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a memory",
    responses={
        204: {"description": "Memory deleted"},
        400: {"description": "Invalid input"},
        404: {"description": "Memory not found"},
        409: {"description": "Stale content — re-read and retry"},
        423: {"description": "Memory locked by another writer"},
        428: {"description": "If-Match header required"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Service unavailable"},
    },
)
def delete_memory(
    memory_id: str,
    request: Request,
    if_match: Optional[str] = Header(None, alias="If-Match"),
    user: Principal = Depends(current_principal),
):
    """Delete a shared technical memory.

    Requires the ``If-Match`` header containing the current content hash.
    Returns 428 if the header is absent, 204 on success (empty body).
    """
    if if_match is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match header required",
        )
    service = _get_service(request)
    try:
        service.delete_memory(
            memory_id,
            expected_content_hash=if_match,
            username=user.username,
        )
    except StaleContentError as exc:
        return _stale_content_response(exc)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except ConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=str(exc),
        )
    except RateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except Exception as exc:
        logger.error(
            format_error_log(
                "MEM-GENERAL-003",
                f"delete_memory failed for {memory_id!r}: {exc}",
            ),
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {exc}",
        )
