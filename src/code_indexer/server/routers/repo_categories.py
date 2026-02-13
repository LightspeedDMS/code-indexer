"""
Repository Categories API Router for CIDX Server (Story #182).

Provides REST API endpoints for category management:
- GET /api/v1/repo-categories - List all categories (any authenticated user)
- POST /api/v1/repo-categories - Create category (admin only)
- PUT /api/v1/repo-categories/{id} - Update category (admin only)
- DELETE /api/v1/repo-categories/{id} - Delete category (admin only)
- POST /api/v1/repo-categories/reorder - Reorder categories (admin only)
- POST /api/v1/repo-categories/re-evaluate - Bulk re-evaluate (admin only)
"""

import logging
import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..auth.dependencies import get_current_user, get_current_admin_user
from ..auth.user_manager import User
from ..services.repo_category_service import RepoCategoryService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/repo-categories", tags=["repo-categories"])

# Global reference to category service (set during app initialization)
_category_service: Optional[RepoCategoryService] = None


def get_category_service() -> RepoCategoryService:
    """Get the RepoCategoryService instance."""
    if _category_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Category service not initialized",
        )
    return _category_service


def set_category_service(service: RepoCategoryService) -> None:
    """Set the RepoCategoryService instance (called during app startup)."""
    global _category_service
    _category_service = service


# Request models
class CategoryCreateRequest(BaseModel):
    """Request model for creating a category."""

    name: str = Field(..., min_length=1, max_length=100)
    pattern: str = Field(..., min_length=1, max_length=500)


class CategoryUpdateRequest(BaseModel):
    """Request model for updating a category."""

    name: str = Field(..., min_length=1, max_length=100)
    pattern: str = Field(..., min_length=1, max_length=500)


class CategoryReorderRequest(BaseModel):
    """Request model for reordering categories."""

    ordered_ids: List[int]


# Response models
class CategoryResponse(BaseModel):
    """Response model for a category."""

    id: int
    name: str
    pattern: str
    priority: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ReEvaluateResponse(BaseModel):
    """Response model for re-evaluate operation."""

    updated: int
    errors: List[str] = []


def _category_to_response(category: dict) -> CategoryResponse:
    """Convert a category dict to API response."""
    return CategoryResponse(
        id=category["id"],
        name=category["name"],
        pattern=category["pattern"],
        priority=category["priority"],
        created_at=category.get("created_at"),
        updated_at=category.get("updated_at"),
    )


@router.get("", response_model=List[CategoryResponse])
def list_categories(
    current_user: User = Depends(get_current_user),
    category_service: RepoCategoryService = Depends(get_category_service),
) -> List[CategoryResponse]:
    """
    List all repository categories ordered by priority.

    Accessible by all authenticated users (AC6).
    Returns categories in priority order (ascending).
    """
    categories = category_service.list_categories()
    return [_category_to_response(cat) for cat in categories]


@router.post(
    "",
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Category created successfully"},
        409: {"description": "Category name already exists"},
        422: {"description": "Invalid regex pattern"},
    },
)
def create_category(
    request: CategoryCreateRequest,
    current_user: User = Depends(get_current_admin_user),
    category_service: RepoCategoryService = Depends(get_category_service),
) -> CategoryResponse:
    """
    Create a new repository category (AC6).

    Requires admin role. Validates regex pattern and name uniqueness.
    """
    try:
        category_id = category_service.create_category(
            name=request.name,
            pattern=request.pattern,
        )

        # Fetch the created category to return full details
        category = category_service.get_category(category_id)
        if category is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Category created but not found",
            )

        return _category_to_response(category)

    except ValueError as e:
        # Invalid regex pattern
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )
    except sqlite3.IntegrityError:
        # Duplicate name
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Category name '{request.name}' already exists",
        )


@router.put(
    "/{category_id}",
    response_model=CategoryResponse,
    responses={
        200: {"description": "Category updated successfully"},
        404: {"description": "Category not found"},
        409: {"description": "Category name already exists"},
        422: {"description": "Invalid regex pattern"},
    },
)
def update_category(
    category_id: int,
    request: CategoryUpdateRequest,
    current_user: User = Depends(get_current_admin_user),
    category_service: RepoCategoryService = Depends(get_category_service),
) -> CategoryResponse:
    """
    Update a repository category (AC6).

    Requires admin role. Updates name and pattern.
    """
    try:
        category_service.update_category(
            category_id=category_id,
            name=request.name,
            pattern=request.pattern,
        )

        # Fetch updated category
        category = category_service.get_category(category_id)
        if category is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Category with ID {category_id} not found",
            )

        return _category_to_response(category)

    except ValueError as e:
        # Invalid regex pattern or category not found
        if "not found" in str(e):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e),
            )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )
    except sqlite3.IntegrityError:
        # Duplicate name
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Category name '{request.name}' already exists",
        )


@router.delete(
    "/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Category deleted successfully"},
        404: {"description": "Category not found"},
    },
)
def delete_category(
    category_id: int,
    current_user: User = Depends(get_current_admin_user),
    category_service: RepoCategoryService = Depends(get_category_service),
):
    """
    Delete a repository category (AC6).

    Requires admin role. Associated repos move to Unassigned (ON DELETE SET NULL).
    """
    try:
        category_service.delete_category(category_id)
        return None

    except ValueError as e:
        # Category not found
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


@router.post(
    "/reorder",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Categories reordered successfully"},
        400: {"description": "Invalid reorder list"},
    },
)
def reorder_categories(
    request: CategoryReorderRequest,
    current_user: User = Depends(get_current_admin_user),
    category_service: RepoCategoryService = Depends(get_category_service),
):
    """
    Reorder repository categories (AC6).

    Requires admin role. Reassigns priorities based on ordered list.
    """
    try:
        category_service.reorder_categories(request.ordered_ids)
        return {"message": "Categories reordered successfully"}

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post(
    "/re-evaluate",
    response_model=ReEvaluateResponse,
    responses={
        200: {"description": "Re-evaluation completed"},
    },
)
def re_evaluate_categories(
    current_user: User = Depends(get_current_admin_user),
    category_service: RepoCategoryService = Depends(get_category_service),
) -> ReEvaluateResponse:
    """
    Bulk re-evaluate repository category assignments (AC6).

    Requires admin role. Re-runs pattern matching on all eligible repos.
    Respects manual overrides (category_auto_assigned = False).
    """
    result = category_service.bulk_re_evaluate()
    return ReEvaluateResponse(
        updated=result["updated"],
        errors=result.get("errors", []),
    )
