"""
Web routes for repository category management (Story #180).

Provides admin UI for CRUD operations on repository categories.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .routes import (
    _require_admin_session,
    _create_login_redirect,
    validate_login_csrf_token,
    generate_csrf_token,
    set_csrf_cookie,
)
from ..services.repo_category_service import RepoCategoryService
from ..services.config_service import get_config_service

logger = logging.getLogger(__name__)

# Get templates directory path
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _get_server_time_for_template() -> str:
    """Get current server time in ISO format for template."""
    return datetime.now(timezone.utc).isoformat()


# Register template global functions
templates.env.globals["get_server_time"] = _get_server_time_for_template

# Create router for repo category management
repo_category_web_router = APIRouter(tags=["repo-categories-web"])


def _get_repo_category_service() -> RepoCategoryService:
    """Get RepoCategoryService instance with proper db_path."""
    config_service = get_config_service()
    server_dir = config_service.config_manager.server_dir
    db_path = str(server_dir / "data" / "cidx_server.db")
    return RepoCategoryService(db_path)


@repo_category_web_router.get("/repo-categories", response_class=HTMLResponse)
def repo_categories_page(request: Request):
    """
    Repository categories management page.

    Displays table of all categories with CRUD operations.
    Requires authenticated admin session.
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    # Get all categories
    service = _get_repo_category_service()
    categories = service.list_categories()

    # Generate CSRF token for forms
    csrf_token = generate_csrf_token()

    response = templates.TemplateResponse(
        "repo_categories.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "repo-categories",
            "show_nav": True,
            "categories": categories,
            "csrf_token": csrf_token,
        },
    )

    # Set CSRF cookie
    set_csrf_cookie(response, csrf_token, path="/")

    return response


@repo_category_web_router.post("/repo-categories/create", response_class=HTMLResponse)
def create_category(
    request: Request,
    name: str = Form(...),
    pattern: str = Form(...),
    apply_now: Optional[str] = Form(None),
    csrf_token: Optional[str] = Form(None),
):
    """
    Create a new repository category.

    Returns HTMX partial refresh or full page with success/error message.
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _render_categories_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Validate inputs
    if not name or not name.strip():
        return _render_categories_response(
            request, session, error_message="Category name is required"
        )

    if not pattern or not pattern.strip():
        return _render_categories_response(
            request, session, error_message="Pattern is required"
        )

    # Create category
    try:
        service = _get_repo_category_service()
        category_id = service.create_category(name.strip(), pattern.strip())
        logger.info(f"Created category '{name}' (id={category_id}) by {session.username}")

        # Apply to existing repos if checkbox was checked
        apply_msg = ""
        if apply_now == "1":
            try:
                result = service.bulk_re_evaluate()
                updated = result.get("updated", 0)
                if updated > 0:
                    apply_msg = f" ({updated} repo(s) re-assigned)"
                logger.info(f"Auto-applied categories after create: {updated} updated")
            except Exception as e:
                logger.warning(f"Failed to auto-apply categories after create: {e}")
                apply_msg = " (auto-apply failed)"

        return _render_categories_response(
            request,
            session,
            success_message=f"Category '{name}' created successfully{apply_msg}",
        )
    except ValueError as e:
        # Validation error (invalid regex, pattern too long)
        return _render_categories_response(
            request, session, error_message=str(e)
        )
    except Exception as e:
        # Database error (duplicate name, etc.)
        logger.warning("Failed to create category '%s': %s", name, e)
        error_msg = str(e)
        if "UNIQUE constraint failed" in error_msg:
            error_msg = f"Category name '{name}' already exists"
        return _render_categories_response(
            request, session, error_message=error_msg
        )


@repo_category_web_router.post("/repo-categories/{category_id}/update", response_class=HTMLResponse)
def update_category(
    request: Request,
    category_id: int,
    name: str = Form(...),
    pattern: str = Form(...),
    apply_now: Optional[str] = Form(None),
    csrf_token: Optional[str] = Form(None),
):
    """
    Update an existing repository category.

    Returns HTMX partial refresh or full page with success/error message.
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _render_categories_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Validate inputs
    if not name or not name.strip():
        return _render_categories_response(
            request, session, error_message="Category name is required"
        )

    if not pattern or not pattern.strip():
        return _render_categories_response(
            request, session, error_message="Pattern is required"
        )

    # Update category
    try:
        service = _get_repo_category_service()
        service.update_category(category_id, name.strip(), pattern.strip())
        logger.info(f"Updated category id={category_id} to '{name}' by {session.username}")

        # Apply to existing repos if checkbox was checked
        apply_msg = ""
        if apply_now == "1":
            try:
                result = service.bulk_re_evaluate()
                updated = result.get("updated", 0)
                if updated > 0:
                    apply_msg = f" ({updated} repo(s) re-assigned)"
                logger.info(f"Auto-applied categories after update: {updated} updated")
            except Exception as e:
                logger.warning(f"Failed to auto-apply categories after update: {e}")
                apply_msg = " (auto-apply failed)"

        return _render_categories_response(
            request,
            session,
            success_message=f"Category '{name}' updated successfully{apply_msg}",
        )
    except ValueError as e:
        # Validation error (invalid regex, pattern too long)
        return _render_categories_response(
            request, session, error_message=str(e)
        )
    except Exception as e:
        # Database error (duplicate name, etc.)
        logger.warning("Failed to update category id=%s to '%s': %s", category_id, name, e)
        error_msg = str(e)
        if "UNIQUE constraint failed" in error_msg:
            error_msg = f"Category name '{name}' already exists"
        return _render_categories_response(
            request, session, error_message=error_msg
        )


@repo_category_web_router.post("/repo-categories/{category_id}/delete", response_class=HTMLResponse)
def delete_category(
    request: Request,
    category_id: int,
    csrf_token: Optional[str] = Form(None),
):
    """
    Delete a repository category.

    All repos with this category will have category_id set to NULL (Unassigned).
    Returns HTMX partial refresh or full page with success message.
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _render_categories_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Delete category
    try:
        service = _get_repo_category_service()
        service.delete_category(category_id)
        logger.info(f"Deleted category id={category_id} by {session.username}")

        return _render_categories_response(
            request,
            session,
            success_message="Category deleted successfully (repositories moved to Unassigned)",
        )
    except Exception as e:
        logger.warning("Failed to delete category id=%s: %s", category_id, e)
        return _render_categories_response(
            request, session, error_message=f"Failed to delete category: {e}"
        )


@repo_category_web_router.post("/repo-categories/reorder", response_class=HTMLResponse)
def reorder_categories(
    request: Request,
    ordered_ids: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """
    Reorder repository categories by priority.

    Expects ordered_ids as comma-separated list of category IDs.
    Returns HTMX partial refresh or full page with success message.
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _render_categories_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Parse ordered IDs
    try:
        id_list = [int(id_str.strip()) for id_str in ordered_ids.split(",") if id_str.strip()]
    except ValueError:
        return _render_categories_response(
            request, session, error_message="Invalid category IDs format"
        )

    if not id_list:
        return _render_categories_response(
            request, session, error_message="No categories to reorder"
        )

    # Reorder categories
    try:
        service = _get_repo_category_service()
        service.reorder_categories(id_list)
        logger.info(f"Reordered {len(id_list)} categories by {session.username}")

        return _render_categories_response(
            request,
            session,
            success_message="Categories reordered successfully",
        )
    except Exception as e:
        logger.warning("Failed to reorder categories: %s", e)
        return _render_categories_response(
            request, session, error_message=f"Failed to reorder categories: {e}"
        )


@repo_category_web_router.post("/repo-categories/re-evaluate", response_class=HTMLResponse)
def re_evaluate_categories(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """
    Re-evaluate all repository category assignments (Story #181 AC3).

    Re-runs auto-assignment logic on all repositories.
    Respects manual overrides (only re-assigns auto-assigned repos and Unassigned repos).
    Returns HTMX partial refresh or full page with summary message.
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _render_categories_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Re-evaluate all repositories
    try:
        service = _get_repo_category_service()
        result = service.bulk_re_evaluate()

        updated = result.get("updated", 0)
        errors = result.get("errors", [])

        logger.info(f"Re-evaluated repository categories: {updated} updated by {session.username}")

        # Build success message
        if errors:
            success_msg = f"Re-evaluated {updated} repositories with {len(errors)} errors"
        else:
            success_msg = f"Re-evaluated all repositories: {updated} assignments updated"

        return _render_categories_response(
            request,
            session,
            success_message=success_msg,
        )
    except Exception as e:
        logger.warning("Failed to re-evaluate categories: %s", e)
        return _render_categories_response(
            request, session, error_message=f"Failed to re-evaluate categories: {e}"
        )


def _render_categories_response(
    request: Request,
    session,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
):
    """
    Helper to render categories page with success/error messages.

    Checks for HTMX request header and returns partial or full page accordingly.
    """
    # Get fresh category list
    service = _get_repo_category_service()
    categories = service.list_categories()

    # Generate new CSRF token
    csrf_token = generate_csrf_token()

    # Check if HTMX partial request
    is_htmx = request.headers.get("HX-Request") == "true"

    if is_htmx:
        # Return partial list only
        response = templates.TemplateResponse(
            "partials/repo_categories_list.html",
            {
                "request": request,
                "categories": categories,
                "csrf_token": csrf_token,
                "success_message": success_message,
                "error_message": error_message,
            },
        )
    else:
        # Return full page
        response = templates.TemplateResponse(
            "repo_categories.html",
            {
                "request": request,
                "username": session.username,
                "current_page": "repo-categories",
                "show_nav": True,
                "categories": categories,
                "csrf_token": csrf_token,
                "success_message": success_message,
                "error_message": error_message,
            },
        )

    # Set CSRF cookie
    set_csrf_cookie(response, csrf_token, path="/")

    return response


@repo_category_web_router.post("/golden-repos/{alias}/category", response_class=HTMLResponse)
def update_repo_category_route(
    request: Request,
    alias: str,
    category_id: Optional[str] = Form(None),
    csrf_token: Optional[str] = Form(None),
):
    """
    Update category assignment for a golden repo (Story #183 AC6).

    This route handles manual category override from the Web UI.
    Sets category_auto_assigned=False to mark as manual override.

    Args:
        request: FastAPI request object.
        alias: Golden repository alias.
        category_id: Category ID as string, or empty/None for Unassigned.
        csrf_token: CSRF token for validation.

    Returns:
        JSON response with success/error status.
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return templates.TemplateResponse(
            "partials/error_message.html",
            {"request": request, "error": "Invalid CSRF token"},
            status_code=400,
        )

    # Parse category_id (empty string or None means Unassigned)
    parsed_category_id = None
    if category_id and category_id.strip():
        try:
            parsed_category_id = int(category_id.strip())
        except ValueError:
            return templates.TemplateResponse(
                "partials/error_message.html",
                {"request": request, "error": "Invalid category ID format"},
                status_code=400,
            )

    # Update category via service
    try:
        service = _get_repo_category_service()
        service.update_repo_category(alias, parsed_category_id, auto_assigned=False)

        logger.info(
            f"Repo '{alias}' category updated to {parsed_category_id} by {session.username}"
        )

        # Return success response
        # For HTMX, return a simple success message or trigger page refresh
        return templates.TemplateResponse(
            "partials/success_message.html",
            {"request": request, "message": "Category updated successfully"},
            status_code=200,
        )

    except ValueError as e:
        # Repository not found or other validation error
        logger.warning(f"Failed to update category for '{alias}': {e}")
        return templates.TemplateResponse(
            "partials/error_message.html",
            {"request": request, "error": str(e)},
            status_code=404,
        )
    except Exception as e:
        # Database error (foreign key constraint, etc.)
        logger.error(f"Failed to update category for '{alias}': {e}")
        error_msg = str(e)
        if "FOREIGN KEY constraint failed" in error_msg:
            error_msg = "Invalid category ID"
        return templates.TemplateResponse(
            "partials/error_message.html",
            {"request": request, "error": error_msg},
            status_code=400,
        )
