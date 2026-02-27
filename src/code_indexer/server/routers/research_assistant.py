"""
Research Assistant Router for CIDX Server.

Story #141: Research Assistant - Basic Chatbot Working

Provides admin endpoints for the Research Assistant chatbot interface.
"""

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from code_indexer.server.web.auth import require_admin_session, SessionData
from code_indexer.server.services.research_assistant_service import (
    ResearchAssistantService,
)
from code_indexer.server.web.jinja_filters import relative_time

logger = logging.getLogger(__name__)

# Module-level cache for GitHub token (Story #202 optimization)
_github_token_cache: Optional[str] = None
_github_token_cache_time: float = 0
_GITHUB_TOKEN_CACHE_TTL = 300  # 5 minutes


# Helper function to retrieve GitHub token for RA sessions (Story #202)
def _get_github_token() -> Optional[str]:
    """
    Retrieve GitHub token from CITokenManager for RA sessions.

    Uses module-level cache with 5-minute TTL to avoid recreating
    CITokenManager on every request.
    """
    global _github_token_cache, _github_token_cache_time

    # Check cache validity
    current_time = time.time()
    if _github_token_cache is not None and (current_time - _github_token_cache_time) < _GITHUB_TOKEN_CACHE_TTL:
        return _github_token_cache

    # Cache miss or expired - fetch from CITokenManager
    try:
        from code_indexer.server.services.ci_token_manager import CITokenManager
        server_data_dir = os.environ.get(
            "CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server")
        )
        db_path = str(Path(server_data_dir) / "data" / "cidx_server.db")
        token_manager = CITokenManager(
            server_dir_path=server_data_dir,
            use_sqlite=True,
            db_path=db_path,
        )
        token_data = token_manager.get_token("github")
        token = token_data.token if token_data else None

        # Update cache
        _github_token_cache = token
        _github_token_cache_time = current_time

        return token
    except Exception as e:
        logger.debug("Failed to retrieve GitHub token: %s", e)
        return None


def _get_job_tracker():
    """Get JobTracker from app module for dashboard visibility."""
    try:
        from code_indexer.server.app import job_tracker
        return job_tracker
    except (ImportError, AttributeError):
        return None


# Helper function for server time in templates (Story #89)
def _get_server_time_for_template() -> str:
    """Get current server time for Jinja2 templates (Story #89)."""
    current_time = datetime.now(timezone.utc)
    return current_time.isoformat().replace("+00:00", "Z")


# Templates
TEMPLATES_DIR = Path(__file__).parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Add server time function to Jinja2 globals for server clock (Story #89)
templates.env.globals["get_server_time"] = _get_server_time_for_template

# Register relative_time filter for message timestamps (Story #142 AC3)
templates.env.filters["relative_time"] = relative_time

# Router
router = APIRouter(prefix="/admin/research", tags=["research-assistant"])


@router.get("", response_class=HTMLResponse)
async def get_research_assistant_page(
    request: Request,
    session_data: SessionData = Depends(require_admin_session),
) -> HTMLResponse:
    """
    Render the Research Assistant page (AC1, AC2, Story #143).

    Returns HTML page with:
    - Two-column layout: sidebar (left) and chat area (right)
    - Sidebar with session list or empty state
    - Chat area for conversation history and input
    - Messages for most recent session loaded from database

    Args:
        request: FastAPI request object
        session_data: Authenticated admin session

    Returns:
        HTML response with research assistant page
    """
    service = ResearchAssistantService(github_token=_get_github_token(), job_tracker=_get_job_tracker())

    # Get all sessions (Story #143 AC1)
    sessions = service.get_all_sessions()

    # Get messages for most recent session (or empty if no sessions)
    messages = []
    active_session_id = None
    if sessions:
        active_session_id = sessions[0]["id"]
        messages = service.get_messages(active_session_id)

        # Render markdown for all messages
        for message in messages:
            message["content"] = service.render_markdown(message["content"])

    return templates.TemplateResponse(
        request=request,
        name="research_assistant.html",
        context={
            "current_page": "research",
            "show_nav": True,
            "sessions": sessions,
            "active_session_id": active_session_id,
            "messages": messages,
        },
    )


@router.post("/send", response_class=HTMLResponse)
async def send_message(
    request: Request,
    user_prompt: str = Form(...),
    session_id: str = Form(None),
    session_data: SessionData = Depends(require_admin_session),
) -> HTMLResponse:
    """
    Send user message and start Claude execution (AC2, AC4).

    Args:
        request: FastAPI request object
        user_prompt: User's question/prompt from form
        session_id: Optional session ID from form (falls back to default)
        session_data: Authenticated admin session

    Returns:
        Partial HTML with new user message and polling trigger
    """
    service = ResearchAssistantService(github_token=_get_github_token(), job_tracker=_get_job_tracker())

    # Use provided session_id or fall back to default
    if session_id:
        session = service.get_session(session_id)
        if not session:
            session = service.get_default_session()
    else:
        session = service.get_default_session()

    # Execute prompt (stores user message and starts background job)
    job_id = service.execute_prompt(session["id"], user_prompt)

    # Get updated messages
    messages = service.get_messages(session["id"])

    # Render markdown for all messages
    for message in messages:
        message["content"] = service.render_markdown(message["content"])

    # Get all sessions for sidebar OOB update
    sessions = service.get_all_sessions()

    # Return partial HTML with messages, polling trigger, and sidebar OOB update
    return templates.TemplateResponse(
        request=request,
        name="partials/research_send_response.html",
        context={
            "messages": messages,
            "job_id": job_id,
            "polling": True,
            "sessions": sessions,
            "active_session_id": session["id"],
        },
    )


@router.get("/poll/{job_id}", response_class=HTMLResponse)
async def poll_job(
    request: Request,
    job_id: str,
    session_id: Optional[str] = None,
    session_data: SessionData = Depends(require_admin_session),
) -> HTMLResponse:
    """
    Poll job status for Claude execution (AC2, AC4).

    Bug #151 Fix: Accepts optional session_id query parameter for database
    fallback when job is lost from memory.

    Args:
        request: FastAPI request object
        job_id: Job ID to poll
        session_id: Optional session ID for database fallback
        session_data: Authenticated admin session

    Returns:
        Partial HTML with status or final messages when complete
    """
    service = ResearchAssistantService(github_token=_get_github_token(), job_tracker=_get_job_tracker())
    status = service.poll_job(job_id, session_id=session_id)

    # Get session_id from job status (falls back to param if not in response)
    session_id = status.get("session_id") or session_id
    messages = service.get_messages(session_id) if session_id else []

    # Render markdown for all messages
    for message in messages:
        message["content"] = service.render_markdown(message["content"])

    if status["status"] == "complete":
        # Job complete - return updated messages without polling
        return templates.TemplateResponse(
            request=request,
            name="partials/research_messages.html",
            context={
                "messages": messages,
                "polling": False,
            },
        )
    elif status["status"] == "error":
        # Job failed - return messages with error (preserve chat history)
        return templates.TemplateResponse(
            request=request,
            name="partials/research_messages.html",
            context={
                "messages": messages,
                "error": status.get("error", "Unknown error"),
                "polling": False,
            },
        )
    else:
        # Still running - return messages with polling indicator
        return templates.TemplateResponse(
            request=request,
            name="partials/research_messages.html",
            context={
                "messages": messages,
                "job_id": job_id,
                "polling": True,
                "active_session_id": session_id,  # Bug #151: For database fallback
            },
        )


# Story #143: Session Management CRUD Endpoints


@router.post("/sessions", response_class=HTMLResponse)
async def create_session(
    request: Request,
    session_data: SessionData = Depends(require_admin_session),
) -> HTMLResponse:
    """
    Create a new research session (AC2 - Story #143).

    Args:
        request: FastAPI request object
        session_data: Authenticated admin session

    Returns:
        Partial HTML with updated session list
    """
    service = ResearchAssistantService(github_token=_get_github_token(), job_tracker=_get_job_tracker())
    new_session = service.create_session()

    # Get all sessions for rendering
    sessions = service.get_all_sessions()

    return templates.TemplateResponse(
        request=request,
        name="partials/research_sessions_list.html",
        context={
            "sessions": sessions,
            "active_session_id": new_session["id"],
        },
    )


@router.put("/sessions/{session_id}", response_class=HTMLResponse)
async def rename_session(
    request: Request,
    session_id: str,
    new_name: str = Form(...),
    session_data: SessionData = Depends(require_admin_session),
) -> HTMLResponse:
    """
    Rename a research session (AC4 - Story #143).

    Args:
        request: FastAPI request object
        session_id: Session ID to rename
        new_name: New name for session
        session_data: Authenticated admin session

    Returns:
        Partial HTML with updated session list or error
    """
    service = ResearchAssistantService(github_token=_get_github_token(), job_tracker=_get_job_tracker())
    success = service.rename_session(session_id, new_name)

    if not success:
        # Return error response
        return templates.TemplateResponse(
            request=request,
            name="partials/research_error.html",
            context={
                "error": "Failed to rename session. Name must be 1-100 characters with only letters, numbers, spaces, and hyphens.",
            },
            status_code=400,
        )

    # Get all sessions for rendering
    sessions = service.get_all_sessions()

    return templates.TemplateResponse(
        request=request,
        name="partials/research_sessions_list.html",
        context={
            "sessions": sessions,
            "active_session_id": session_id,
        },
    )


@router.delete("/sessions/{session_id}", response_class=HTMLResponse)
async def delete_session(
    request: Request,
    session_id: str,
    active_session_id: Optional[str] = None,
    session_data: SessionData = Depends(require_admin_session),
) -> HTMLResponse:
    """
    Delete a research session (AC5 - Story #143).

    Bug Fix: Now updates chat area when deleting active session.
    - If deleted session was active and sessions remain: shows topmost session messages
    - If deleted session was active and no sessions remain: clears chat
    - If deleted session was not active: only updates sidebar (no chat update)

    Args:
        request: FastAPI request object
        session_id: Session ID to delete
        active_session_id: Optional query param indicating which session is currently active
        session_data: Authenticated admin session

    Returns:
        Partial HTML with updated session list (and OOB swap for chat if active session deleted)
    """
    service = ResearchAssistantService(github_token=_get_github_token(), job_tracker=_get_job_tracker())
    success = service.delete_session(session_id)

    if not success:
        # Return error response
        return templates.TemplateResponse(
            request=request,
            name="partials/research_error.html",
            context={
                "error": "Failed to delete session. Session not found.",
            },
            status_code=404,
        )

    # Determine if deleted session was the active one
    deleted_was_active = (active_session_id == session_id) or (active_session_id is None)

    # Get remaining sessions
    sessions = service.get_all_sessions()

    # If deleted session was active, we need to update chat area too
    if deleted_was_active:
        # Get messages for new active session (or empty if no sessions)
        messages = []
        new_active_session_id = None
        if sessions:
            new_active_session_id = sessions[0]["id"]
            messages = service.get_messages(new_active_session_id)

            # Render markdown for all messages
            for message in messages:
                message["content"] = service.render_markdown(message["content"])

        # Return response with OOB swap for both sidebar and chat
        return templates.TemplateResponse(
            request=request,
            name="partials/research_delete_response.html",
            context={
                "sessions": sessions,
                "active_session_id": new_active_session_id,
                "messages": messages,
            },
        )
    else:
        # Deleted session was not active - only update sidebar
        if len(sessions) == 0:
            # No sessions left - return empty state
            return templates.TemplateResponse(
                request=request,
                name="partials/research_empty_state.html",
                context={},
            )

        # Return updated session list (keep existing active session)
        return templates.TemplateResponse(
            request=request,
            name="partials/research_sessions_list.html",
            context={
                "sessions": sessions,
                "active_session_id": active_session_id,  # Keep existing active session
            },
        )


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
async def load_session(
    request: Request,
    session_id: str,
    session_data: SessionData = Depends(require_admin_session),
) -> HTMLResponse:
    """
    Load a research session's conversation (AC3 - Story #143).

    Args:
        request: FastAPI request object
        session_id: Session ID to load
        session_data: Authenticated admin session

    Returns:
        Partial HTML with session's messages
    """
    service = ResearchAssistantService(github_token=_get_github_token(), job_tracker=_get_job_tracker())
    session = service.get_session(session_id)

    if not session:
        # Return error response
        return templates.TemplateResponse(
            request=request,
            name="partials/research_error.html",
            context={
                "error": "Session not found.",
            },
            status_code=404,
        )

    # Get messages for this session
    messages = service.get_messages(session_id)

    # Render markdown for all messages
    for message in messages:
        message["content"] = service.render_markdown(message["content"])

    return templates.TemplateResponse(
        request=request,
        name="partials/research_messages.html",
        context={
            "messages": messages,
            "polling": False,
        },
    )


# Story #144: File Upload Endpoints


@router.post("/sessions/{session_id}/upload")
async def upload_file(
    session_id: str,
    file: UploadFile = File(...),
    session_data: SessionData = Depends(require_admin_session),
) -> JSONResponse:
    """
    Upload file to session (AC2 - Story #144).

    Args:
        session_id: Session ID
        file: Uploaded file
        session_data: Authenticated admin session

    Returns:
        JSON with success/error/filename/size/uploaded_at
    """
    service = ResearchAssistantService(github_token=_get_github_token(), job_tracker=_get_job_tracker())
    result = service.upload_file(session_id, file)

    if result["success"]:
        return JSONResponse(content=result, status_code=200)
    else:
        return JSONResponse(content=result, status_code=400)


@router.get("/sessions/{session_id}/files")
async def list_files(
    session_id: str,
    session_data: SessionData = Depends(require_admin_session),
) -> JSONResponse:
    """
    List uploaded files for session (AC4 - Story #144).

    Args:
        session_id: Session ID
        session_data: Authenticated admin session

    Returns:
        JSON array of file metadata
    """
    service = ResearchAssistantService(github_token=_get_github_token(), job_tracker=_get_job_tracker())
    files = service.list_files(session_id)

    return JSONResponse(content={"files": files}, status_code=200)


@router.delete("/sessions/{session_id}/files/{filename}")
async def delete_file(
    session_id: str,
    filename: str,
    session_data: SessionData = Depends(require_admin_session),
) -> JSONResponse:
    """
    Delete uploaded file from session (AC4 - Story #144).

    Args:
        session_id: Session ID
        filename: Filename to delete
        session_data: Authenticated admin session

    Returns:
        JSON with success status
    """
    # Defense in depth: reject filenames with path separators
    if "/" in filename or "\\" in filename:
        return JSONResponse(
            content={"success": False, "error": "Invalid filename"}, status_code=400
        )

    service = ResearchAssistantService(github_token=_get_github_token(), job_tracker=_get_job_tracker())
    success = service.delete_file(session_id, filename)

    if success:
        return JSONResponse(content={"success": True}, status_code=200)
    else:
        return JSONResponse(
            content={"success": False, "error": "File not found"}, status_code=404
        )


@router.get("/sessions/{session_id}/files/{filename}")
async def download_file(
    session_id: str,
    filename: str,
    session_data: SessionData = Depends(require_admin_session),
) -> Response:
    """
    Download uploaded file from session (AC4 - Story #144).

    Args:
        session_id: Session ID
        filename: Filename to download
        session_data: Authenticated admin session

    Returns:
        File download response or 404 error
    """
    # Defense in depth: reject filenames with path separators
    if "/" in filename or "\\" in filename:
        return JSONResponse(
            content={"success": False, "error": "Invalid filename"}, status_code=400
        )

    service = ResearchAssistantService(github_token=_get_github_token(), job_tracker=_get_job_tracker())
    file_path = service.get_file_path(session_id, filename)

    if file_path is None:
        return JSONResponse(content={"error": "File not found"}, status_code=404)

    return FileResponse(
        path=str(file_path), filename=filename, media_type="application/octet-stream"
    )
