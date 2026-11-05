"""
Admin query analytics export page.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

from code_indexer.server.services import search_event_recording
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ._shared import (
    _create_login_redirect,
    _elevation_page_redirect,
    _require_admin_session,
    generate_csrf_token,
    get_csrf_token_from_cookie,
    set_csrf_cookie,
    templates,
)

web_router = APIRouter()


@web_router.get("/analytics-export", response_class=HTMLResponse)
def analytics_export_page(request: Request):
    """Query Analytics Export page — filter and export search event data to Excel.

    Every user's search history is user data: like the elevation-gated
    ``/api/admin/search-events*`` routes the page calls, it requires the
    caller's own elevation window.
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)
    elevate = _elevation_page_redirect(request, session.username)
    if elevate is not None:
        return elevate

    # Bug #2110: the Search Type options are the one list writers record.
    from code_indexer.server.services.search_event_health import (
        search_event_health_info,
        search_event_log_window,
    )

    csrf_token = get_csrf_token_from_cookie(request) or generate_csrf_token()
    event_writer = search_event_recording.writer_from_app(request.app)
    response = templates.TemplateResponse(
        request,
        "analytics_export.html",
        {
            "request": request,
            "current_page": "analytics-export",
            "show_nav": True,
            "csrf_token": csrf_token,
            "search_types": search_event_recording.SEARCH_TYPES,
            # Bug #2114: events this server process dropped (an export can
            # be short).
            "search_event_drops": search_event_health_info(event_writer),
            # Story #2117: retention (live config) and the oldest stored row
            # (one indexed read; this route is sync, run in the threadpool).
            "search_event_window": search_event_log_window(event_writer),
            "query_text_max_chars": search_event_recording.QUERY_TEXT_MAX_CHARS,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response
