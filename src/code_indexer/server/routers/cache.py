"""Cache retrieval REST API Router.

Story #679: S1 - Semantic Search with Payload Control (Foundation)
AC4: REST Cache Retrieval API

Provides GET /cache/{handle} endpoint for retrieving cached content with pagination.
"""

import logging
from typing import Union
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from code_indexer.server.cache.payload_cache import CacheNotFoundError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cache", tags=["cache"])


class CacheRetrievalResponse(BaseModel):
    """Response model for cache retrieval."""

    content: str = Field(..., description="Retrieved content for requested page")
    page: int = Field(..., description="Current page number (0-indexed)")
    total_pages: int = Field(..., description="Total number of pages available")
    has_more: bool = Field(..., description="Whether more pages are available")


class CacheErrorResponse(BaseModel):
    """Error response for cache retrieval failures."""

    error: str = Field(
        ...,
        description=(
            "Error type: cache_expired (handle not found/expired), or "
            "wrong_tool_for_handle (Bug #1928: an xray-pv1-* handle was "
            "passed to this REST endpoint -- fetch it via the "
            "cidx_fetch_cached_payload MCP tool instead)."
        ),
    )
    message: str = Field(..., description="Human-readable error message")
    handle: str = Field(..., description="The requested cache handle")


@router.get(
    "/{handle}",
    response_model=CacheRetrievalResponse,
    responses={
        200: {"description": "Cache content retrieved successfully"},
        400: {
            "description": (
                "Wrong tool for this handle -- an xray-pv1-* (Bug #1928) "
                "truncated-result manifest handle was passed to this "
                "REST endpoint; fetch it via the cidx_fetch_cached_payload "
                "MCP tool instead"
            ),
            "model": CacheErrorResponse,
        },
        404: {
            "description": "Cache handle not found or expired",
            "model": CacheErrorResponse,
        },
    },
    summary="Retrieve cached content",
    description="Retrieve cached content by handle with pagination support",
)
def get_cached_content(
    request: Request,
    handle: str,
    page: int = Query(default=0, ge=0, description="Page number (0-indexed)"),
) -> Union[CacheRetrievalResponse, JSONResponse]:
    """Retrieve cached content by handle with pagination.

    Args:
        request: FastAPI request object
        handle: UUID4 cache handle
        page: Page number (0-indexed, default 0)

    Returns:
        CacheRetrievalResponse with content and pagination info on
        success, or a JSONResponse whose body matches CacheErrorResponse
        EXACTLY (flat {error, message, handle}, no "detail" wrapper --
        Bug #1928, Codex: an HTTPException(detail={...}) body would NOT
        match the schema this route declares for 400/404 in `responses`)
        on a 400 or 404 failure.
    """
    # Bug #1928 round 3 (P3, Opus): a handle carrying the pages-v1
    # discriminator prefix is an xray truncation manifest, not ordinary
    # paginated content -- this route's 0-indexed page convention differs
    # from cidx_fetch_cached_payload's 1-indexed contract, so silently
    # routing it through payload_cache.retrieve() here would either leak
    # the manifest internals or corrupt pagination. Reject it loudly.
    from code_indexer.server.mcp.handlers import xray_truncation

    if handle.startswith(xray_truncation._PAGES_V1_HANDLE_PREFIX):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=CacheErrorResponse(
                error="wrong_tool_for_handle",
                message=(
                    "This handle is an xray truncated-result manifest -- "
                    "fetch it via the `cidx_fetch_cached_payload` MCP tool "
                    "(1-indexed page parameter), not this REST endpoint."
                ),
                handle=handle,
            ).model_dump(),
        )

    payload_cache = getattr(request.app.state, "payload_cache", None)

    if payload_cache is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cache service not available",
        )

    try:
        result = payload_cache.retrieve(handle, page=page)
        return CacheRetrievalResponse(
            content=result.content,
            page=result.page,
            total_pages=result.total_pages,
            has_more=result.has_more,
        )
    except CacheNotFoundError:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=CacheErrorResponse(
                error="cache_expired",
                message="Cache handle has expired or does not exist",
                handle=handle,
            ).model_dump(),
        )
