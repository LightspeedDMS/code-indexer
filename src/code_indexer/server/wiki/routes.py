"""Wiki route endpoints for CIDX Server (Stories #280, #281, #282, #283, #286)."""

import logging
from pathlib import Path
from typing import Optional, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette import status

from ...global_repos.alias_manager import AliasManager
from ...utils.path_confinement import is_readable_within_root
from ..auth.dependencies import get_current_user_hybrid
from ..auth.user_manager import User
from ..middleware.correlation import get_correlation_id
from ..routers import repo_access_http
from ..services import search_event_recording
from ..services.cidx_meta_access import is_catalog_request
from ..services.constants import is_internal_meta_repo
from ..services.deactivation_query_drain import track_activated_repo_query
from ..utils.registry_factory import resolve_backend_registry_attr
from .catalog_wiki import (
    CatalogWikiView,
    filter_articles,
    filter_sidebar,
    render_projected_page,
    restricted_catalog_view,
    served_relative_path,
)
from .wiki_cache import WikiCache
from .wiki_service import WikiService

logger = logging.getLogger(__name__)
wiki_router = APIRouter()


def get_wiki_user_hybrid(request: Request) -> User:
    """Wiki auth dependency — redirects to login instead of returning JSON 401.

    Calls _hybrid_auth_impl directly (not get_current_user_hybrid) to avoid
    the Depends(security) default parameter issue.  Manually extracts Bearer
    credentials from the Authorization header so both session-cookie and
    token-based auth work.  On 401, redirects to /login?redirect_to=...
    """
    from urllib.parse import quote
    from fastapi.security import HTTPAuthorizationCredentials
    from code_indexer.server.auth.dependencies import _hybrid_auth_impl

    # Manually extract Bearer credentials (Depends(security) only works via DI)
    credentials = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=token
            )

    try:
        return cast(User, _hybrid_auth_impl(request, credentials, require_admin=False))
    except HTTPException as exc:
        if exc.status_code == 401:
            current_path = str(request.url.path)
            if request.url.query:
                current_path += f"?{request.url.query}"
            redirect_url = f"/login?redirect_to={quote(current_path)}"
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": redirect_url},
            )
        raise


WIKI_TEMPLATES_DIR = Path(__file__).parent / "templates"
wiki_templates = Jinja2Templates(directory=str(WIKI_TEMPLATES_DIR))
WIKI_ALLOWED_EXTENSIONS = {".md", ".markdown", ".txt"}
# Repository files served from the CIDX origin: passive types only. No script
# or document type (.js, .mjs, .html, .htm, .xhtml, .xml) is ever served; an
# SVG or PDF opened directly is neutralised by WIKI_ASSET_CSP's sandbox.
# Content types are fixed here, never taken from the host's MIME tables.
WIKI_ASSET_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".css": "text/css",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".eot": "application/vnd.ms-fontobject",
    ".pdf": "application/pdf",
}
WIKI_ASSET_EXTENSIONS = frozenset(WIKI_ASSET_MEDIA_TYPES)
# Set by the route, so the global security-headers middleware (which never
# overwrites a header a route set) keeps it for asset responses.
WIKI_ASSET_CSP = (
    "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"
)
WIKI_ASSET_HEADERS = {
    "Content-Security-Policy": WIKI_ASSET_CSP,
    "X-Content-Type-Options": "nosniff",
}

# Module-level singletons: avoids re-instantiation on every request
_wiki_service = WikiService()
_wiki_cache: Optional[WikiCache] = None


def _get_wiki_cache(request: Request) -> WikiCache:
    """Lazily initialise the module-level WikiCache singleton.

    Bug #1665: resolves the shared PostgreSQL wiki_cache backend in
    cluster mode via resolve_backend_registry_attr() so a page cached by
    one node is visible to a request routed to a different node. Safe to
    resolve at first-use time: app.state.backend_registry is guaranteed
    populated before the server can serve its first HTTP request (Bug
    #1515).
    """
    global _wiki_cache
    if _wiki_cache is None:
        manager = request.app.state.golden_repo_manager
        wiki_backend, _ = resolve_backend_registry_attr(
            "wiki_cache", caller_name="wiki/routes._get_wiki_cache"
        )
        _wiki_cache = WikiCache(manager.db_path, storage_backend=wiki_backend)
        _wiki_cache.ensure_tables()
    return _wiki_cache


def _reset_wiki_cache():
    """Reset module-level cache singleton (for testing)."""
    global _wiki_cache
    _wiki_cache = None


def _refuse_unreadable(repo_dir: Path, file_path: Path) -> None:
    """404 (the same answer as a missing page) unless *file_path* resolves
    inside *repo_dir* and not into the repository's .git -- whatever the
    file's own name (e.g. a committed ``link.md -> .git/config``)."""
    if not is_readable_within_root(file_path, repo_dir.resolve()):
        raise HTTPException(status_code=404, detail="Not found")


def _asset_response(repo_dir: Path, asset_path: str) -> FileResponse:
    """Serve one repository asset as passive content (404 otherwise)."""
    file_path = repo_dir / asset_path
    _refuse_unreadable(repo_dir, file_path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    media_type = WIKI_ASSET_MEDIA_TYPES.get(file_path.suffix.lower())
    if media_type is None:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(
        path=str(file_path), media_type=media_type, headers=WIKI_ASSET_HEADERS
    )


def _get_wiki_config(request: Request):
    """Return the WikiConfig from ConfigService, or a default WikiConfig.

    Follows the same pattern as _get_wiki_cache(). Retrieves wiki_config
    from the ConfigService stored on app.state.config_service so that all
    four metadata toggles configured in the Web UI are honoured at render time.
    Falls back to a default all-True WikiConfig when config_service is not
    available (e.g. in tests that don't configure it).
    """
    from ..utils.config_manager import WikiConfig

    try:
        config_service = request.app.state.config_service
        server_config = config_service.get_config()
        wiki_config = server_config.wiki_config
        if wiki_config is None:
            return WikiConfig()
        return wiki_config
    except AttributeError:
        return WikiConfig()


def _check_user_wiki_access(
    request: Request, username: str, alias: str, current_user: User
) -> str:
    """Validate user wiki access; return resolved filesystem path. 404 on any failure.

    Only the repo owner and admin users may access a user wiki (Story #291, AC4).
    The owner must still hold grants on the activation's source golden
    repositories (the MCP rule); another user's wiki is readable only by an
    admin -- a member of the admins group, never by role. Every route using
    this is a sync def (threadpool), so the lookups never block the loop.
    """
    access_svc = getattr(request.app.state, "access_filtering_service", None)
    not_found = HTTPException(status_code=404, detail="Not found")
    if current_user.username == username:
        repo_access_http.enforce_activated_repo_access(
            access_svc, username, alias, refusal=not_found
        )
    elif not repo_access_http.is_access_admin(access_svc, current_user.username):
        raise not_found

    activated_repo_manager = request.app.state.activated_repo_manager
    if not activated_repo_manager.get_wiki_enabled(username, alias):
        raise HTTPException(status_code=404, detail="Not found")

    repo_path = activated_repo_manager.get_activated_repo_path(username, alias)
    if not Path(repo_path).exists():
        raise HTTPException(status_code=404, detail="Not found")

    return cast(str, repo_path)


def _check_wiki_access(request: Request, repo_alias: str, current_user: User) -> str:
    """Validate wiki access; return resolved filesystem path. 404 on any failure (invisible repo)."""
    manager = request.app.state.golden_repo_manager
    access_svc = request.app.state.access_filtering_service
    if not access_svc.is_admin_user(current_user.username):
        accessible = access_svc.get_accessible_repos(current_user.username)
        if repo_alias not in accessible:
            raise HTTPException(status_code=404, detail="Not found")
    if not manager.get_wiki_enabled(repo_alias):
        raise HTTPException(status_code=404, detail="Not found")
    aliases_dir = Path(manager.golden_repos_dir) / "aliases"
    alias_manager = AliasManager(str(aliases_dir))
    actual_path = alias_manager.read_alias(f"{repo_alias}-global")
    if actual_path is None:
        raise HTTPException(status_code=404, detail="Not found")
    return actual_path


def _cached_page(
    cache: WikiCache,
    wiki_config,
    cache_key: str,
    url_prefix: str,
    article_rel_path: str,
    current_dir: str,
    file_path: Path,
) -> dict:
    """Final page ('html', 'title', 'metadata') of one article file.

    Rendered, link-rewritten and sanitised once per file version and cached in
    that final form, so a cached view serves it without sanitising again.
    """
    page = cache.get_page(cache_key, article_rel_path, file_path)
    if page is not None:
        return page
    try:
        result = _wiki_service.render_article(
            file_path, url_prefix, wiki_config=wiki_config
        )
    except (UnicodeDecodeError, OSError):
        raise HTTPException(status_code=404, detail="Not found")
    html = _wiki_service.prepare_article_content(
        result["html"], url_prefix, current_dir
    )
    cache.put_page(
        cache_key,
        article_rel_path,
        html,
        result["title"],
        file_path,
        metadata=result["metadata"],
    )
    return {"html": html, "title": result["title"], "metadata": result["metadata"]}


def _article_response(
    request: Request,
    cache: WikiCache,
    wiki_config,
    page: dict,
    url_prefix: str,
    article_rel_path: str,
    sidebar,
):
    """``article.html`` response for a page returned by ``_cached_page``."""
    metadata_panel = (
        _wiki_service.prepare_metadata_context(
            page.get("metadata") or {},
            url_prefix,
            article_rel_path,
            cache,
            wiki_config=wiki_config,
        )
        or None
    )
    return wiki_templates.TemplateResponse(
        request,
        "article.html",
        {
            "request": request,
            "title": page["title"],
            "content": page["html"],
            "repo_alias": url_prefix,
            "sidebar": sidebar,
            "breadcrumbs": _wiki_service.build_breadcrumbs(
                article_rel_path, url_prefix
            ),
            "current_path": article_rel_path,
            "metadata_panel": metadata_panel,
        },
    )


def _sidebar(
    cache: WikiCache,
    cache_alias: str,
    repo_dir: Path,
    url_prefix: str,
    catalog: Optional[CatalogWikiView],
):
    """The shared (unfiltered) sidebar; a catalog view gets a filtered copy
    and the shared cache only ever holds the unfiltered tree."""
    sidebar = cache.get_sidebar(cache_alias, repo_dir)
    if sidebar is None:
        sidebar = _wiki_service.build_sidebar_tree(repo_dir, url_prefix)
        cache.put_sidebar(cache_alias, sidebar, repo_dir)
    return sidebar if catalog is None else filter_sidebar(sidebar, catalog)


def _index_articles(repo_dir: Path, catalog: Optional[CatalogWikiView]) -> list:
    """Root article index (no home page), filtered for a catalog view."""
    articles = []
    for md_file in sorted(repo_dir.rglob("*.md")):
        rel = md_file.relative_to(repo_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if not is_readable_within_root(md_file, repo_dir.resolve()):
            continue
        stem = md_file.stem.replace("-", " ").replace("_", " ").title()
        articles.append({"path": str(rel.with_suffix("")), "title": stem})
    return articles if catalog is None else filter_articles(articles, catalog)


def _page(
    cache: WikiCache,
    wiki_config,
    cache_key: str,
    url_prefix: str,
    article_rel_path: str,
    current_dir: str,
    file_path: Path,
    catalog: Optional[CatalogWikiView],
    relative_file: str,
) -> dict:
    """The article page: shared cache for everyone but a restricted catalog
    caller, whose projected page is rendered per request and never cached."""
    if catalog is None:
        return _cached_page(
            cache,
            wiki_config,
            cache_key,
            url_prefix,
            article_rel_path,
            current_dir,
            file_path,
        )
    return render_projected_page(
        catalog,
        _wiki_service,
        file_path,
        relative_file,
        url_prefix,
        current_dir,
        wiki_config,
    )


def _home_allowed(catalog: Optional[CatalogWikiView]) -> bool:
    """True when the caller may see the wiki home page (``home.md``)."""
    if catalog is None:
        return True
    try:
        served_relative_path(catalog, "home.md")
    except HTTPException:
        return False
    return True


def _user_catalog_view(
    request: Request, username: str, alias: str, viewer: str, repo_dir: Path
) -> Optional[CatalogWikiView]:
    """Catalog view of a user-wiki activation whose source is the catalog."""
    arm = getattr(request.app.state, "activated_repo_manager", None)
    return restricted_catalog_view(
        request, viewer, repo_dir, is_catalog_request(alias, username, arm)
    )


# ---------------------------------------------------------------------------
# User wiki routes (Story #291) — MUST be defined before golden repo routes
# to avoid FastAPI matching "u" as a repo_alias in the catch-all routes.
# ---------------------------------------------------------------------------


@wiki_router.get("/u/{username}/{alias}/_assets/{asset_path:path}")
def serve_user_wiki_asset(
    username: str,
    alias: str,
    asset_path: str,
    request: Request,
    current_user: User = Depends(get_wiki_user_hybrid),
):
    """Serve image/asset files from user's activated repo wiki (Story #291)."""
    repo_dir = Path(_check_user_wiki_access(request, username, alias, current_user))
    catalog = _user_catalog_view(
        request, username, alias, current_user.username, repo_dir
    )
    if catalog is not None:
        # Raises 404 unless the request is the canonical, allowed path.
        asset_path = served_relative_path(catalog, asset_path)[1]
    return _asset_response(repo_dir, asset_path)


@wiki_router.get("/u/{username}/{alias}/_search")
def user_wiki_search(
    username: str,
    alias: str,
    request: Request,
    q: str = "",
    mode: str = "semantic",
    current_user: User = Depends(get_current_user_hybrid),
) -> JSONResponse:
    """Search user's activated repo wiki (Story #291)."""
    repo_dir = Path(_check_user_wiki_access(request, username, alias, current_user))
    catalog = _user_catalog_view(
        request, username, alias, current_user.username, repo_dir
    )

    if len(q.strip()) < 2:
        return JSONResponse(content=[])

    if mode not in ("semantic", "fts"):
        mode = "semantic"

    if not hasattr(request.app.state, "semantic_query_manager"):
        return JSONResponse(content={"error": "Search unavailable"})

    semantic_query_manager = request.app.state.semantic_query_manager
    try:
        # Codex Finding #7: wire the SAME activated-repo QueryTracker
        # refcount protection MCP search.py already has -- this route
        # always targets an activated (non-global) repo.
        with track_activated_repo_query(
            getattr(request.app.state, "query_tracker", None),
            getattr(request.app.state, "activated_repo_manager", None),
            username,
            alias,
        ):
            # Story #2115: a completed search is recorded as the caller.
            result = search_event_recording.run_recorded_search(
                search_event_recording.writer_from_app(request.app),
                lambda: semantic_query_manager.query_user_repositories(
                    username=username,
                    query_text=q.strip(),
                    repository_alias=alias,
                    search_mode=mode,
                    file_extensions=[".md"],
                    limit=50,
                ),
                origin=search_event_recording.SearchOrigin(
                    tool="web:wiki_search", door="web"
                ),
                username=current_user.username,
                repo_alias=alias,
                search_type=mode,
                query_text=q.strip(),
                correlation_id=get_correlation_id(),
            )
    except Exception:
        logger.warning(
            "User wiki search failed for %s/%s query %r",
            username,
            alias,
            q,
            exc_info=True,
        )
        return JSONResponse(content={"error": "Search unavailable"})

    raw_results = result.get("results", []) if isinstance(result, dict) else []
    mapped = []
    for item in raw_results:
        file_path = item.get("file_path", "")
        clean_path = file_path[:-3] if file_path.endswith(".md") else file_path
        stem = Path(clean_path).name
        title = stem.replace("-", " ").replace("_", " ").title()
        mapped.append(
            {
                "path": clean_path,
                "score": item.get("similarity_score", 0.0),
                "title": title,
            }
        )
    if catalog is not None:
        mapped = filter_articles(mapped, catalog)
    return JSONResponse(content=mapped)


@wiki_router.get("/u/{username}/{alias}/", response_class=HTMLResponse)
def serve_user_wiki_root(
    username: str,
    alias: str,
    request: Request,
    current_user: User = Depends(get_wiki_user_hybrid),
):
    """Serve user wiki root page — home.md or article index (Story #291)."""
    repo_path = _check_user_wiki_access(request, username, alias, current_user)
    repo_dir = Path(repo_path)
    # Cache key isolates user wiki from golden repo cache (AC5)
    cache_repo_alias = f"u:{username}:{alias}"
    # URL prefix for link rewriting: "u/{username}/{alias}"
    url_prefix = f"u/{username}/{alias}"

    catalog = _user_catalog_view(
        request, username, alias, current_user.username, repo_dir
    )
    home_md = repo_dir / "home.md"
    cache = _get_wiki_cache(request)
    wiki_config = _get_wiki_config(request)
    sidebar = _sidebar(cache, cache_repo_alias, repo_dir, url_prefix, catalog)

    if _home_allowed(catalog) and home_md.exists() and home_md.is_file():
        cache.increment_view(cache_repo_alias, "")
        page = _page(
            cache,
            wiki_config,
            cache_repo_alias,
            url_prefix,
            "",
            "",
            home_md,
            catalog,
            "home.md",
        )
        return _article_response(
            request, cache, wiki_config, page, url_prefix, "", sidebar
        )

    # No home.md — render article index
    articles = _index_articles(repo_dir, catalog)
    return wiki_templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "title": f"Wiki: {alias}",
            "repo_alias": url_prefix,
            "articles": articles,
        },
    )


@wiki_router.get("/u/{username}/{alias}/{path:path}", response_class=HTMLResponse)
def serve_user_wiki_article(
    username: str,
    alias: str,
    path: str,
    request: Request,
    current_user: User = Depends(get_wiki_user_hybrid),
):
    """Serve rendered user wiki article (Story #291)."""
    repo_path = _check_user_wiki_access(request, username, alias, current_user)
    repo_dir = Path(repo_path)
    cache_repo_alias = f"u:{username}:{alias}"
    url_prefix = f"u/{username}/{alias}"
    catalog = _user_catalog_view(
        request, username, alias, current_user.username, repo_dir
    )
    if catalog is not None:
        # Raises 404 unless the request is the canonical, allowed file.
        path = served_relative_path(catalog, path, md_fallback=True)[1]

    article_path = repo_dir / path
    _refuse_unreadable(repo_dir, article_path)
    if not article_path.exists() and not article_path.suffix:
        md_path = article_path.with_suffix(".md")
        if md_path.exists():
            article_path = md_path
    if article_path.suffix.lower() not in WIKI_ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Not found")
    # The final file (after the .md fallback) is checked before any cache
    # lookup, so a page cached for such a path is never served either.
    _refuse_unreadable(repo_dir, article_path)
    if not article_path.exists() or not article_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    rel_path = article_path.relative_to(repo_dir)
    current_dir = str(rel_path.parent)
    if current_dir == ".":
        current_dir = ""
    article_rel_path = str(rel_path.with_suffix(""))

    cache = _get_wiki_cache(request)
    wiki_config = _get_wiki_config(request)
    cache.increment_view(cache_repo_alias, article_rel_path)
    sidebar = _sidebar(cache, cache_repo_alias, repo_dir, url_prefix, catalog)
    page = _page(
        cache,
        wiki_config,
        cache_repo_alias,
        url_prefix,
        article_rel_path,
        current_dir,
        article_path,
        catalog,
        rel_path.as_posix(),
    )
    return _article_response(
        request, cache, wiki_config, page, url_prefix, article_rel_path, sidebar
    )


# ---------------------------------------------------------------------------
# Golden repo wiki routes (existing — unchanged)
# ---------------------------------------------------------------------------


@wiki_router.get("/{repo_alias}/_assets/{asset_path:path}")
def serve_wiki_asset(
    repo_alias: str,
    asset_path: str,
    request: Request,
    current_user: User = Depends(get_wiki_user_hybrid),
):
    """Serve image/asset files from wiki repo."""
    repo_dir = Path(_check_wiki_access(request, repo_alias, current_user))
    catalog = restricted_catalog_view(
        request, current_user.username, repo_dir, is_internal_meta_repo(repo_alias)
    )
    if catalog is not None:
        # Raises 404 unless the request is the canonical, allowed path.
        asset_path = served_relative_path(catalog, asset_path)[1]
    return _asset_response(repo_dir, asset_path)


@wiki_router.get("/{repo_alias}/", response_class=HTMLResponse)
def serve_wiki_root(
    repo_alias: str,
    request: Request,
    current_user: User = Depends(get_wiki_user_hybrid),
):
    """Serve wiki root page - home.md or article index."""
    actual_path = _check_wiki_access(request, repo_alias, current_user)
    repo_dir = Path(actual_path)
    catalog = restricted_catalog_view(
        request, current_user.username, repo_dir, is_internal_meta_repo(repo_alias)
    )
    home_md = repo_dir / "home.md"
    cache = _get_wiki_cache(request)
    wiki_config = _get_wiki_config(request)
    sidebar = _sidebar(cache, repo_alias, repo_dir, repo_alias, catalog)

    if _home_allowed(catalog) and home_md.exists() and home_md.is_file():
        cache.increment_view(repo_alias, "")
        page = _page(
            cache,
            wiki_config,
            repo_alias,
            repo_alias,
            "",
            "",
            home_md,
            catalog,
            "home.md",
        )
        return _article_response(
            request, cache, wiki_config, page, repo_alias, "", sidebar
        )
    articles = _index_articles(repo_dir, catalog)
    breadcrumbs = _wiki_service.build_breadcrumbs("", repo_alias)
    return wiki_templates.TemplateResponse(
        request,
        "article.html",
        {
            "request": request,
            "title": f"Wiki: {repo_alias}",
            "index_articles": articles,
            "repo_alias": repo_alias,
            "sidebar": sidebar,
            "breadcrumbs": breadcrumbs,
            "current_path": "",
            "metadata_panel": None,
        },
    )


@wiki_router.get("/{repo_alias}/_search")
def wiki_search(
    repo_alias: str,
    request: Request,
    q: str = "",
    mode: str = "semantic",
    current_user: User = Depends(get_current_user_hybrid),
) -> JSONResponse:
    """Search wiki articles via SemanticQueryManager (Story #290)."""
    repo_dir = Path(_check_wiki_access(request, repo_alias, current_user))
    catalog = restricted_catalog_view(
        request, current_user.username, repo_dir, is_internal_meta_repo(repo_alias)
    )

    # Validate query — return empty for short/empty queries
    if len(q.strip()) < 2:
        return JSONResponse(content=[])

    # Validate mode
    if mode not in ("semantic", "fts"):
        mode = "semantic"

    # Graceful degradation: search unavailable if manager not present
    if not hasattr(request.app.state, "semantic_query_manager"):
        return JSONResponse(content={"error": "Search unavailable"})

    semantic_query_manager = request.app.state.semantic_query_manager

    try:
        # Story #2115: a completed search is recorded as the caller.
        result = search_event_recording.run_recorded_search(
            search_event_recording.writer_from_app(request.app),
            lambda: semantic_query_manager.query_user_repositories(
                username=current_user.username,
                query_text=q.strip(),
                repository_alias=f"{repo_alias}-global",
                search_mode=mode,
                file_extensions=[".md"],
                limit=50,
            ),
            origin=search_event_recording.SearchOrigin(
                tool="web:wiki_search", door="web"
            ),
            username=current_user.username,
            repo_alias=f"{repo_alias}-global",
            search_type=mode,
            query_text=q.strip(),
            correlation_id=get_correlation_id(),
        )
    except Exception as exc:
        logger.warning(
            "Wiki search failed for repo %s query %r", repo_alias, q, exc_info=True
        )
        error_msg = str(exc)
        # Surface specific provider errors to the user
        if "API key" in error_msg or "Unauthorized" in error_msg:
            return JSONResponse(
                content={"error": f"Semantic search error: {error_msg}"}
            )
        return JSONResponse(
            content={"error": "Search failed — check server logs for details"}
        )

    raw_results = result.get("results", []) if isinstance(result, dict) else []

    mapped = []
    for item in raw_results:
        file_path = item.get("file_path", "")
        # Strip .md extension so path matches wiki URL routing
        if file_path.endswith(".md"):
            clean_path = file_path[:-3]
        else:
            clean_path = file_path
        # Derive title from the final path component
        stem = Path(clean_path).name
        title = stem.replace("-", " ").replace("_", " ").title()
        mapped.append(
            {
                "path": clean_path,
                "score": item.get("similarity_score", 0.0),
                "title": title,
            }
        )

    if catalog is not None:
        mapped = filter_articles(mapped, catalog)
    return JSONResponse(content=mapped)


@wiki_router.get("/{repo_alias}/{path:path}", response_class=HTMLResponse)
def serve_wiki_article(
    repo_alias: str,
    path: str,
    request: Request,
    current_user: User = Depends(get_wiki_user_hybrid),
):
    """Serve rendered wiki article."""
    actual_path = _check_wiki_access(request, repo_alias, current_user)
    repo_dir = Path(actual_path)
    catalog = restricted_catalog_view(
        request, current_user.username, repo_dir, is_internal_meta_repo(repo_alias)
    )
    if catalog is not None:
        # Raises 404 unless the request is the canonical, allowed file.
        path = served_relative_path(catalog, path, md_fallback=True)[1]
    article_path = repo_dir / path
    _refuse_unreadable(repo_dir, article_path)
    if not article_path.exists() and not article_path.suffix:
        md_path = article_path.with_suffix(".md")
        if md_path.exists():
            article_path = md_path
    if article_path.suffix.lower() not in WIKI_ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Not found")
    # The final file (after the .md fallback) is checked before any cache
    # lookup, so a page cached for such a path is never served either.
    _refuse_unreadable(repo_dir, article_path)
    if not article_path.exists() or not article_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    rel_path = article_path.relative_to(repo_dir)
    current_dir = str(rel_path.parent)
    if current_dir == ".":
        current_dir = ""
    article_rel_path = str(rel_path.with_suffix(""))

    cache = _get_wiki_cache(request)
    wiki_config = _get_wiki_config(request)
    cache.increment_view(repo_alias, article_rel_path)
    sidebar = _sidebar(cache, repo_alias, repo_dir, repo_alias, catalog)
    page = _page(
        cache,
        wiki_config,
        repo_alias,  # cache key
        repo_alias,  # url prefix
        article_rel_path,
        current_dir,
        article_path,
        catalog,
        rel_path.as_posix(),
    )
    return _article_response(
        request, cache, wiki_config, page, repo_alias, article_rel_path, sidebar
    )
