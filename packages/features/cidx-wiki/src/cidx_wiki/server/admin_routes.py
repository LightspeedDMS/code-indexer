"""Wiki enable/refresh actions on golden and activated repositories.

Contributed through the ``wiki`` feature's ``routers()`` under ``/admin``
(the paths the golden and activated repository pages post to): the core
authenticates the caller (Web UI session, else bearer token or API key) and
requires ``manage_users`` before a handler runs. Each action then requires an
active elevation window, the Web UI admin session and its CSRF token, as when
the actions lived in the core's ``web/routes.py`` (Stories #280, #283, #287,
#291). The repository pages show these controls only while the feature is
loaded (``feature_offered("wiki")`` in their templates).

``wiki_enabled`` itself is repository metadata the core keeps (golden and
activated repository records); what the wiki does on a toggle -- seeding view
counts, dropping render cache rows -- happens here.
"""

import logging
import threading
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from cidx_wiki._platform import server_platform
from cidx_wiki.server import runtime
from cidx_wiki.server.wiki_cache import WikiCache
from cidx_wiki.server.wiki_service import WikiService

logger = logging.getLogger(__name__)

#: Where the core mounts :data:`web_router`.
ROUTER_PREFIX = "/admin"
#: The permission the core checks before any action here runs.
ROUTE_PERMISSION = "manage_users"


def require_admin_elevation(request: Request) -> None:
    """An administrator with an active elevation window (when enforcement
    is on), as every admin repository action requires."""
    server_platform().require_admin_elevation(request)


web_router = APIRouter(tags=["wiki"])


def _golden_repo_manager(request: Request):
    manager = getattr(request.app.state, "golden_repo_manager", None)
    if manager is None:
        raise RuntimeError(
            "golden_repo_manager not initialized. "
            "Server must set app.state.golden_repo_manager during startup."
        )
    return manager


def _activated_repo_manager(request: Request):
    # Bug #1670: the app's wired singleton, never a fresh pool-less instance.
    manager = getattr(request.app.state, "activated_repo_manager", None)
    if manager is None:
        raise RuntimeError(
            "activated_repo_manager not initialized. "
            "Server must set app.state.activated_repo_manager during startup."
        )
    return manager


def _wiki_cache(db_path: str, caller: str) -> WikiCache:
    # Bug #1665: the shared PostgreSQL wiki_cache backend in cluster mode, so
    # this action is visible to every node.
    backend = server_platform().backend_registry_attr("wiki_cache", caller)
    return WikiCache(db_path, storage_backend=backend)


@web_router.post(
    "/golden-repos/{alias}/wiki-toggle",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_elevation)],
)
def toggle_wiki_enabled(
    request: Request,
    alias: str,
    wiki_enabled: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Toggle wiki_enabled for a golden repo (Story #280)."""
    platform = server_platform()
    session = platform.admin_web_session(request)
    if not session:
        return platform.login_redirect_response(request)
    if not platform.validate_login_csrf(request, csrf_token):
        return platform.web_error_partial(request, "Invalid CSRF token", 400)
    manager = _golden_repo_manager(request)
    enabling = wiki_enabled == "1"
    manager.set_wiki_enabled(alias, enabling)
    if enabling:
        # Lifecycle hook: Populate initial view counts from front matter (Story #287, AC2)
        try:
            cache = _wiki_cache(manager.db_path, "web.routes.toggle_wiki_enabled")
            cache.ensure_tables()
            # Bug #1315: fall back to registry index_path when alias pointer
            # is missing/unreadable.
            wiki_repo_entry = None
            wiki_backend_registry = getattr(request.app.state, "backend_registry", None)
            if wiki_backend_registry:
                try:
                    wiki_repo_entry = wiki_backend_registry.global_repos.get_repo(
                        f"{alias}-global"
                    )
                except Exception:
                    pass
            actual_path = platform.resolve_alias_or_index_path(
                str(manager.golden_repos_dir), f"{alias}-global", wiki_repo_entry
            )
            if actual_path:
                svc = WikiService()
                # The wiki config, so enable_views_seeding is respected (Story #323)
                wiki_config = None
                try:
                    wiki_config = runtime.wiki_config()
                except Exception as exc:
                    logger.warning(
                        "Failed to fetch wiki_config for view seeding of %s, "
                        "proceeding with defaults: %s",
                        alias,
                        exc,
                    )
                threading.Thread(
                    target=svc.populate_views_from_front_matter,
                    kwargs={
                        "repo_alias": alias,
                        "repo_path": Path(actual_path),
                        "wiki_cache": cache,
                        "wiki_config": wiki_config,
                    },
                    daemon=True,
                ).start()
        except Exception as exc:
            logger.warning("Failed to trigger view population for %s: %s", alias, exc)
    return platform.golden_repos_page_response(
        request, session, "Wiki setting updated successfully"
    )


@web_router.post(
    "/golden-repos/{alias}/wiki-refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_elevation)],
)
def refresh_wiki_cache(
    request: Request,
    alias: str,
    csrf_token: Optional[str] = Form(None),
):
    """Clear wiki render cache for a golden repo (Story #283)."""
    platform = server_platform()
    session = platform.admin_web_session(request)
    if not session:
        return platform.login_redirect_response(request)
    if not platform.validate_login_csrf(request, csrf_token):
        return platform.web_error_partial(request, "Invalid CSRF token", 400)
    manager = _golden_repo_manager(request)
    cache = _wiki_cache(manager.db_path, "web.routes.refresh_wiki_cache")
    cache.invalidate_repo(alias)
    return platform.golden_repos_page_response(request, session, "Wiki cache cleared")


@web_router.post(
    "/activated-repos/{username}/{alias}/wiki-toggle",
    response_class=JSONResponse,
    dependencies=[Depends(require_admin_elevation)],
)
def toggle_user_wiki_enabled(
    request: Request,
    username: str,
    alias: str,
    wiki_enabled: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Toggle wiki_enabled for an activated repo (Story #291, AC1).

    Accepts wiki_enabled="1" to enable or "0" to disable.
    Returns JSON {"success": true} or {"error": "..."}.
    Requires admin session.
    """
    platform = server_platform()
    session = platform.admin_web_session(request)
    if not session:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not platform.validate_login_csrf(request, csrf_token):
        return JSONResponse({"error": "Invalid CSRF token"}, status_code=400)

    try:
        manager = _activated_repo_manager(request)
        enabling = wiki_enabled == "1"
        manager.set_wiki_enabled(username, alias, enabling)
        if not enabling:
            try:
                golden_manager = _golden_repo_manager(request)
                cache = _wiki_cache(
                    golden_manager.db_path, "web.routes.toggle_user_wiki_enabled"
                )
                cache.invalidate_user_wiki(username, alias)
            except Exception as cache_exc:
                logger.warning(
                    "Failed to invalidate wiki cache for %s/%s: %s",
                    username,
                    alias,
                    cache_exc,
                )
        return JSONResponse({"success": True})
    except Exception as exc:
        logger.warning("Failed to toggle wiki for %s/%s: %s", username, alias, exc)
        return JSONResponse({"error": str(exc)}, status_code=400)
