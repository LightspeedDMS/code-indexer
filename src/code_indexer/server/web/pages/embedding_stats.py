"""
Admin embedding statistics page.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ._shared import (
    _create_login_redirect,
    _require_admin_session,
    templates,
)

web_router = APIRouter()


def _load_embedding_stats_dashboard_data(
    backend: Optional[Any],
    provider: Optional[str] = None,
    purpose: Optional[str] = None,
    golden_repo_alias: Optional[str] = None,
    job_id: Optional[str] = None,
    limit: int = 200,
) -> List[Dict]:
    """Load embedding_call_stats records for the Web UI dashboard page
    (Story #1418 Phase 3 Component 8), as plain dicts ready for Jinja
    rendering. Returns [] when backend is unavailable (fail-soft --
    consistent with this project's read-only reporting convention rather
    than raising into the page render)."""
    from dataclasses import asdict

    if backend is None:
        return []
    records = backend.query(
        provider=provider,
        purpose=purpose,
        golden_repo_alias=golden_repo_alias,
        job_id=job_id,
        limit=limit,
    )
    return [asdict(r) for r in records]


@web_router.get("/embedding-stats", response_class=HTMLResponse)
def embedding_stats_dashboard_page(
    request: Request,
    provider: Optional[str] = None,
    purpose: Optional[str] = None,
    golden_repo_alias: Optional[str] = None,
    job_id: Optional[str] = None,
):
    """Embedding & reranker call tracking dashboard (Story #1418 Phase 3
    Component 8). Filterable volume-over-time view backed by
    backend_registry.embedding_call_stats -- requires authenticated admin
    session."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    backend_registry = getattr(request.app.state, "backend_registry", None)
    backend = (
        getattr(backend_registry, "embedding_call_stats", None)
        if backend_registry is not None
        else None
    )
    records = _load_embedding_stats_dashboard_data(
        backend,
        provider=provider or None,
        purpose=purpose or None,
        golden_repo_alias=golden_repo_alias or None,
        job_id=job_id or None,
    )

    return templates.TemplateResponse(
        request,
        "embedding_stats_dashboard.html",
        {
            "records": records,
            "current_page": "embedding-stats",
            "show_nav": True,
            "filters": {
                "provider": provider or "",
                "purpose": purpose or "",
                "golden_repo_alias": golden_repo_alias or "",
                "job_id": job_id or "",
            },
        },
    )
