"""Wiki route endpoints (Stories #280, #281, #282, #283, #286, #290, #291).

Contributed through the ``wiki`` feature's ``routers()`` (``cidx_wiki.feature``):
the core mounts the routes under ``/wiki`` and authenticates every request
before a handler runs -- a Web UI session first, else a bearer token or API
key (``ROUTER_AUTH_HYBRID``) -- and puts the caller on the request state
(``cidx_sdk.web.PRINCIPAL_STATE_ATTR``). Page and asset routes send an
unauthenticated browser to the login page; the two ``_search`` routes answer
401 (they are fetched by ``wiki.js``). The core contributes the search routes
first: FastAPI matches in order, and the catch-all article routes would
otherwise take ``_search`` as an article path.

:data:`wiki_router` holds every route in that order, and the per-route
dependencies :func:`get_wiki_user_hybrid` / :func:`get_current_user_hybrid`
return the caller; a test that mounts the router directly overrides them.

The catalog access projection is applied through the core's
``access_projection`` capability (``cidx_wiki.server.catalog_view``).
"""

import logging
from contextlib import contextmanager
from pathlib import Path, PurePath, PurePosixPath
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union, cast

from cidx_sdk import PRINCIPAL_STATE_ATTR, Principal
from cidx_sdk.capability_types import CatalogArticleView
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from starlette import status

from cidx_wiki._platform import (
    is_readable_within_root,
    server_platform,
)
from cidx_wiki.config import WikiConfig
from cidx_wiki.server import runtime
from cidx_wiki.server.catalog_view import (
    filter_articles,
    filter_sidebar,
    home_allowed,
    projected_text,
    restricted_catalog_view,
    served_relative_path,
    served_seam_path,
)
from cidx_wiki.server.wiki_cache import FileStamp, WikiCache
from cidx_wiki.server.wiki_content_seam import (
    SeamWikiTree,
    WikiContentUnavailable,
    content_backend_wiki_tree,
    pod_directory_of,
)
from cidx_wiki.server.wiki_service import WikiService

logger = logging.getLogger(__name__)
wiki_router = APIRouter()

#: Where the core mounts the wiki's routers.
ROUTER_PREFIX = "/wiki"
#: The tool name and door the wiki's searches are recorded under.
SEARCH_EVENT_TOOL = "web:wiki_search"
SEARCH_EVENT_DOOR = "web"


class WikiCaller:
    """The authenticated caller of a wiki route (``username``, ``role``,
    ``has_permission``), as the core authenticated it for this request.

    Any permission is asked of the ``permissions`` capability against the
    live account (False when the feature has not started).
    """

    def __init__(self, principal: Principal) -> None:
        self.username = principal.username
        self.role = principal.role

    def has_permission(self, permission: Optional[str]) -> bool:
        if permission is None:
            return True
        permissions = runtime.permissions()
        return permissions is not None and permissions.has_permission(
            self.username, permission
        )


def _caller(request: Request) -> WikiCaller:
    principal = getattr(request.state, PRINCIPAL_STATE_ATTR, None)
    if not isinstance(principal, Principal):
        # Never reached through the core (it authenticates first): a router
        # mounted some other way is refused rather than served.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return WikiCaller(principal)


def get_wiki_user_hybrid(request: Request) -> WikiCaller:
    """The caller of a wiki page or asset route (the core redirected an
    unauthenticated browser to ``/login?redirect_to=...`` already)."""
    return _caller(request)


def get_current_user_hybrid(request: Request) -> WikiCaller:
    """The caller of a wiki search route (401 JSON when unauthenticated)."""
    return _caller(request)


WIKI_TEMPLATES_DIR = Path(__file__).parent / "templates"
#: The wiki's stylesheets and scripts, served by the core without
#: authentication at ``/wiki/_static/`` (``RouterContribution.static_dir``).
WIKI_STATIC_DIR = Path(__file__).parent / "static"
wiki_templates = Jinja2Templates(directory=str(WIKI_TEMPLATES_DIR))
WIKI_ALLOWED_EXTENSIONS = {".md", ".markdown", ".txt"}
#: The article a golden wiki's root serves when the repository has one.
_HOME_MD = "home.md"
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

# Module-level singletons: avoids re-instantiation on every request. Process
# wiring only (the cache's rows live in the shared store), never request state.
_wiki_service = WikiService()
_wiki_cache: Optional[WikiCache] = None


def _get_wiki_cache(request: Request) -> WikiCache:
    """Lazily initialise the module-level WikiCache singleton.

    Bug #1665: resolves the shared PostgreSQL wiki_cache backend in
    cluster mode (the core's backend registry) so a page cached by
    one node is visible to a request routed to a different node. Safe to
    resolve at first-use time: app.state.backend_registry is guaranteed
    populated before the server can serve its first HTTP request (Bug
    #1515).
    """
    global _wiki_cache
    if _wiki_cache is None:
        manager = request.app.state.golden_repo_manager
        wiki_backend = server_platform().backend_registry_attr(
            "wiki_cache", "wiki/routes._get_wiki_cache"
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


def _get_wiki_config(request: Request) -> WikiConfig:
    """The ``wiki`` config section (Story #323), read when the page renders
    so the Web UI toggles apply at once; the defaults when it cannot be read
    (the feature not started, a config read failure)."""
    try:
        return runtime.wiki_config()
    except Exception as exc:  # a page never fails on a config read
        logger.warning(
            "wiki config unreadable, rendering with defaults (%s)",
            type(exc).__name__,
        )
        return WikiConfig()


def _check_user_wiki_access(
    request: Request, username: str, alias: str, current_user: WikiCaller
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
        server_platform().enforce_activated_repo_access(
            access_svc, username, alias, not_found
        )
    elif not server_platform().is_access_admin(access_svc, current_user.username):
        raise not_found

    activated_repo_manager = request.app.state.activated_repo_manager
    if not activated_repo_manager.get_wiki_enabled(username, alias):
        raise HTTPException(status_code=404, detail="Not found")

    repo_path = activated_repo_manager.get_activated_repo_path(username, alias)
    if not _user_copy_present(activated_repo_manager, username, alias, repo_path):
        raise HTTPException(status_code=404, detail="Not found")

    return cast(str, repo_path)


def _user_copy_present(
    activated_repo_manager: Any, username: str, alias: str, repo_path: str
) -> bool:
    """Whether the user's activated copy is there: its directory, when the
    content seam's implementation reads this pod's disk -- else (the copy
    lives on the content service and this pod holds its index state at most) the
    registry's record of the activation for THIS user."""
    directory = pod_directory_of(repo_path)
    if directory is not None:
        return directory.exists()
    return bool(activated_repo_manager.user_has_activated_repo(username, alias))


def _check_wiki_access(
    request: Request, repo_alias: str, current_user: WikiCaller
) -> str:
    """Validate wiki access; return resolved filesystem path. 404 on any failure (invisible repo)."""
    manager = request.app.state.golden_repo_manager
    access_svc = request.app.state.access_filtering_service
    if not access_svc.is_admin_user(current_user.username):
        accessible = access_svc.get_accessible_repos(current_user.username)
        if repo_alias not in accessible:
            raise HTTPException(status_code=404, detail="Not found")
    if not manager.get_wiki_enabled(repo_alias):
        raise HTTPException(status_code=404, detail="Not found")
    actual_path: Optional[str] = server_platform().read_alias_target(
        str(manager.golden_repos_dir), f"{repo_alias}-global"
    )
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
    file_path: Union[Path, FileStamp],
    render: Optional[Callable[[], Dict[str, Any]]] = None,
) -> dict:
    """Final page ('html', 'title', 'metadata') of one article file.

    Rendered, link-rewritten and sanitised once per file version and cached in
    that final form, so a cached view serves it without sanitising again.

    ``file_path`` is the local file (stat()ed to validate the cache, read to
    render it), or -- for a file read through the content backend -- its
    manifest ``FileStamp``, with ``render`` producing the article from the
    bytes the backend fetched.
    """
    page = cache.get_page(cache_key, article_rel_path, file_path)
    if page is not None:
        return page
    if render is None:
        if not isinstance(file_path, Path):
            raise TypeError("a FileStamp article needs its render callable")
        local_file = file_path

        def render_local() -> Dict[str, Any]:
            return _wiki_service.render_article(
                local_file, url_prefix, wiki_config=wiki_config
            )

        render = render_local
    try:
        result = render()
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
    catalog: Optional[CatalogArticleView],
):
    """The shared (unfiltered) sidebar; a catalog view gets a filtered copy
    and the shared cache only ever holds the unfiltered tree."""
    sidebar = cache.get_sidebar(cache_alias, repo_dir)
    if sidebar is None:
        sidebar = _wiki_service.build_sidebar_tree(repo_dir, url_prefix)
        cache.put_sidebar(cache_alias, sidebar, repo_dir)
    return sidebar if catalog is None else filter_sidebar(sidebar, catalog)


def _index_articles(repo_dir: Path, catalog: Optional[CatalogArticleView]) -> list:
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
    catalog: Optional[CatalogArticleView],
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
    try:
        raw = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        raise HTTPException(status_code=404, detail="Not found")
    return _render_projected(
        catalog, raw, relative_file, url_prefix, current_dir, wiki_config
    )


def _render_projected(
    catalog: CatalogArticleView,
    raw: str,
    relative_file: str,
    url_prefix: str,
    current_dir: str,
    wiki_config: Any,
) -> Dict[str, Any]:
    """The final page of one catalog article for a restricted caller,
    rendered from its projection (never cached: it is the caller's own)."""
    text = projected_text(catalog, raw, relative_file)
    result = _wiki_service.render_article_source(
        text, PurePosixPath(relative_file), url_prefix, wiki_config=wiki_config
    )
    html = _wiki_service.prepare_article_content(
        result["html"], url_prefix, current_dir
    )
    return {"html": html, "title": result["title"], "metadata": result["metadata"]}


def _home_allowed(catalog: Optional[CatalogArticleView]) -> bool:
    """True when the caller may see the wiki home page (``home.md``)."""
    return home_allowed(catalog, _HOME_MD)


def _user_catalog_view(
    username: str, alias: str, viewer: str, repo_dir: Path
) -> Optional[CatalogArticleView]:
    """Catalog view of a user-wiki activation whose source is the catalog
    (the core decides that from *username*'s activation of *alias*)."""
    return restricted_catalog_view(viewer, repo_dir, alias=alias, owner=username)


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
    current_user: WikiCaller = Depends(get_wiki_user_hybrid),
):
    """Serve image/asset files from user's activated repo wiki (Story #291)."""
    repo_path = _check_user_wiki_access(request, username, alias, current_user)
    catalog = _user_catalog_view(
        username, alias, current_user.username, Path(repo_path)
    )
    tree = _content_backend_tree(repo_path)
    if tree is not None:
        with _content_unavailable_as_503():
            return _serve_wiki_asset_via_seam(tree, asset_path, catalog)
    if catalog is not None:
        # Raises 404 unless the request is the canonical, allowed path.
        asset_path = served_relative_path(catalog, asset_path)
    return _asset_response(Path(repo_path), asset_path)


@wiki_router.get("/u/{username}/{alias}/_search")
def user_wiki_search(
    username: str,
    alias: str,
    request: Request,
    q: str = "",
    mode: str = "semantic",
    current_user: WikiCaller = Depends(get_current_user_hybrid),
) -> JSONResponse:
    """Search user's activated repo wiki (Story #291)."""
    repo_dir = Path(_check_user_wiki_access(request, username, alias, current_user))
    catalog = _user_catalog_view(username, alias, current_user.username, repo_dir)

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
        with server_platform().activated_repo_query(
            getattr(request.app.state, "query_tracker", None),
            getattr(request.app.state, "activated_repo_manager", None),
            username,
            alias,
        ):
            # Story #2115: a completed search is recorded as the caller.
            result = server_platform().run_recorded_search(
                request.app,
                lambda: semantic_query_manager.query_user_repositories(
                    username=username,
                    query_text=q.strip(),
                    repository_alias=alias,
                    search_mode=mode,
                    file_extensions=[".md"],
                    limit=50,
                ),
                tool=SEARCH_EVENT_TOOL,
                door=SEARCH_EVENT_DOOR,
                username=current_user.username,
                repo_alias=alias,
                search_type=mode,
                query_text=q.strip(),
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
    current_user: WikiCaller = Depends(get_wiki_user_hybrid),
):
    """Serve user wiki root page — home.md or article index (Story #291)."""
    repo_path = _check_user_wiki_access(request, username, alias, current_user)
    repo_dir = Path(repo_path)
    # Cache key isolates user wiki from golden repo cache (AC5)
    cache_repo_alias = f"u:{username}:{alias}"
    # URL prefix for link rewriting: "u/{username}/{alias}"
    url_prefix = f"u/{username}/{alias}"
    catalog = _user_catalog_view(username, alias, current_user.username, repo_dir)
    tree = _content_backend_tree(repo_path)
    if tree is not None:
        with _content_unavailable_as_503():
            return _serve_user_wiki_root_via_seam(
                request, cache_repo_alias, url_prefix, alias, tree, catalog
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
    current_user: WikiCaller = Depends(get_wiki_user_hybrid),
):
    """Serve rendered user wiki article (Story #291)."""
    repo_path = _check_user_wiki_access(request, username, alias, current_user)
    repo_dir = Path(repo_path)
    cache_repo_alias = f"u:{username}:{alias}"
    url_prefix = f"u/{username}/{alias}"
    catalog = _user_catalog_view(username, alias, current_user.username, repo_dir)
    tree = _content_backend_tree(repo_path)
    if tree is not None:
        with _content_unavailable_as_503():
            return _serve_wiki_article_via_seam(
                request,
                cache_repo_alias,
                tree,
                path,
                url_prefix=url_prefix,
                catalog=catalog,
            )

    if catalog is not None:
        path = served_relative_path(catalog, path, md_fallback=True)

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


def _serve_user_wiki_root_via_seam(
    request: Request,
    cache_repo_alias: str,
    url_prefix: str,
    alias: str,
    tree: SeamWikiTree,
    catalog: Optional[CatalogArticleView],
) -> Any:
    """:func:`serve_user_wiki_root` for an activated copy behind the content
    backend: the same home page or ``index.html`` article index as the local
    walk, from the manifest and fetched markdown, judged by *catalog* (a
    restricted caller's view of a catalog activation) exactly as locally."""
    cache = _get_wiki_cache(request)
    wiki_config = _get_wiki_config(request)
    sidebar = _seam_sidebar(cache, cache_repo_alias, tree, catalog)
    if _home_allowed(catalog) and tree.has_file(_HOME_MD):
        cache.increment_view(cache_repo_alias, "")
        page = _seam_page(
            cache,
            wiki_config,
            cache_repo_alias,
            url_prefix,
            "",
            "",
            tree,
            _HOME_MD,
            catalog,
        )
        return _article_response(
            request, cache, wiki_config, page, url_prefix, "", sidebar
        )
    articles = _seam_index_articles(tree, catalog)
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


# ---------------------------------------------------------------------------
# Golden repo wiki routes
#
# With a delegated content backend the golden repo is not on this pod: each
# route lists and reads it through the backend (wiki_content_seam) instead of
# the disk, and a backend failure is a 503 with its message -- never an empty
# wiki or a 404 for a file that exists. Otherwise the local code runs as before.
# ---------------------------------------------------------------------------


@contextmanager
def _content_unavailable_as_503() -> Iterator[None]:
    """Turn the content backend's failure into a 503 carrying its message."""
    try:
        yield
    except WikiContentUnavailable as exc:
        logger.error("%s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


def _content_backend_tree(actual_path: str) -> Optional[SeamWikiTree]:
    """The repository (a golden repo or a user's activated copy) through the
    content backend, or None to read the disk."""
    with _content_unavailable_as_503():
        return content_backend_wiki_tree(actual_path)


def _index_entry(rel: PurePath) -> Optional[Dict[str, str]]:
    """The article-index entry for one repo-relative ``*.md``; None if hidden."""
    if any(part.startswith(".") for part in rel.parts):
        return None
    stem = rel.stem.replace("-", " ").replace("_", " ").title()
    return {"path": str(rel.with_suffix("")), "title": stem}


def _index_page(
    request: Request, repo_alias: str, articles: List[Dict[str, str]], sidebar: list
) -> Any:
    """The golden wiki root when there is no home.md: an index of articles.

    The template renders ``index_articles`` itself (escaped); no HTML is
    built from repository file names here."""
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


def _seam_sidebar(
    cache: WikiCache,
    repo_alias: str,
    tree: SeamWikiTree,
    catalog: Optional[CatalogArticleView] = None,
) -> list:
    """The cached sidebar, or one built from markdown read through the
    backend; a catalog view gets a filtered copy (as :func:`_sidebar`: the
    shared cache only ever holds the unfiltered tree)."""
    repo_dir = Path(tree.root)
    sidebar = cache.get_sidebar(repo_alias, repo_dir)
    if sidebar is None:
        sidebar = _wiki_service.build_sidebar_tree_from_texts(
            tree.read_visible_md_texts()
        )
        cache.put_sidebar(repo_alias, sidebar, repo_dir, max_mtime=tree.max_md_mtime())
    return sidebar if catalog is None else filter_sidebar(sidebar, catalog)


def _seam_index_articles(
    tree: SeamWikiTree, catalog: Optional[CatalogArticleView]
) -> List[Dict[str, str]]:
    """:func:`_index_articles` from the manifest, filtered for a catalog view."""
    articles = [
        entry
        for entry in (_index_entry(PurePosixPath(p)) for p in tree.md_paths())
        if entry is not None
    ]
    return articles if catalog is None else filter_articles(articles, catalog)


def _seam_page(
    cache: WikiCache,
    wiki_config: Any,
    cache_key: str,
    url_prefix: str,
    article_rel_path: str,
    current_dir: str,
    tree: SeamWikiTree,
    rel: str,
    catalog: Optional[CatalogArticleView],
) -> Dict[str, Any]:
    """:func:`_page` for an article read through the content backend: the
    shared cache for everyone but a restricted catalog caller, whose
    projected page is rendered per request and never cached."""

    def text() -> str:
        try:
            return tree.fetch_text(rel)
        except UnicodeDecodeError:
            raise HTTPException(status_code=404, detail="Not found")

    if catalog is not None:
        return _render_projected(
            catalog, text(), rel, url_prefix, current_dir, wiki_config
        )
    return _cached_page(
        cache,
        wiki_config,
        cache_key,
        url_prefix,
        article_rel_path,
        current_dir,
        tree.stamp(rel),
        render=lambda: _wiki_service.render_article_source(
            text(), PurePosixPath(rel), url_prefix, wiki_config=wiki_config
        ),
    )


def _serve_wiki_asset_via_seam(
    tree: SeamWikiTree, asset_path: str, catalog: Optional[CatalogArticleView]
) -> Response:
    """:func:`serve_wiki_asset` for a golden repo behind the content backend:
    the same passive types, fixed content types and headers as
    :func:`_asset_response`, and the same catalog judgement (a denied or
    non-canonical path is the missing-asset 404)."""
    rel = tree.confine(asset_path)
    if rel is None or not tree.has_file(rel):
        raise HTTPException(status_code=404, detail="Not found")
    if catalog is not None:
        rel = served_seam_path(catalog, asset_path, rel)
    media_type = WIKI_ASSET_MEDIA_TYPES.get(PurePosixPath(rel).suffix.lower())
    if media_type is None:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(
        content=tree.fetch_bytes(rel),
        media_type=media_type,
        headers=WIKI_ASSET_HEADERS,
    )


@wiki_router.get("/{repo_alias}/_assets/{asset_path:path}")
def serve_wiki_asset(
    repo_alias: str,
    asset_path: str,
    request: Request,
    current_user: WikiCaller = Depends(get_wiki_user_hybrid),
):
    """Serve image/asset files from wiki repo."""
    actual_path = _check_wiki_access(request, repo_alias, current_user)
    catalog = restricted_catalog_view(
        current_user.username, Path(actual_path), alias=repo_alias
    )
    tree = _content_backend_tree(actual_path)
    if tree is not None:
        with _content_unavailable_as_503():
            return _serve_wiki_asset_via_seam(tree, asset_path, catalog)
    if catalog is not None:
        # Raises 404 unless the request is the canonical, allowed path.
        asset_path = served_relative_path(catalog, asset_path)
    return _asset_response(Path(actual_path), asset_path)


def _serve_wiki_root_via_seam(
    request: Request,
    repo_alias: str,
    tree: SeamWikiTree,
    catalog: Optional[CatalogArticleView],
) -> Any:
    """:func:`serve_wiki_root` for a golden repo behind the content backend,
    judged by *catalog* exactly as locally."""
    cache = _get_wiki_cache(request)
    wiki_config = _get_wiki_config(request)
    sidebar = _seam_sidebar(cache, repo_alias, tree, catalog)
    if _home_allowed(catalog) and tree.has_file(_HOME_MD):
        cache.increment_view(repo_alias, "")
        page = _seam_page(
            cache,
            wiki_config,
            repo_alias,
            repo_alias,
            "",
            "",
            tree,
            _HOME_MD,
            catalog,
        )
        return _article_response(
            request, cache, wiki_config, page, repo_alias, "", sidebar
        )
    return _index_page(
        request, repo_alias, _seam_index_articles(tree, catalog), sidebar
    )


@wiki_router.get("/{repo_alias}/", response_class=HTMLResponse)
def serve_wiki_root(
    repo_alias: str,
    request: Request,
    current_user: WikiCaller = Depends(get_wiki_user_hybrid),
):
    """Serve wiki root page - home.md or article index."""
    actual_path = _check_wiki_access(request, repo_alias, current_user)
    repo_dir = Path(actual_path)
    catalog = restricted_catalog_view(current_user.username, repo_dir, alias=repo_alias)
    tree = _content_backend_tree(actual_path)
    if tree is not None:
        with _content_unavailable_as_503():
            return _serve_wiki_root_via_seam(request, repo_alias, tree, catalog)
    home_md = repo_dir / _HOME_MD
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
    return _index_page(request, repo_alias, _index_articles(repo_dir, catalog), sidebar)


@wiki_router.get("/{repo_alias}/_search")
def wiki_search(
    repo_alias: str,
    request: Request,
    q: str = "",
    mode: str = "semantic",
    current_user: WikiCaller = Depends(get_current_user_hybrid),
) -> JSONResponse:
    """Search wiki articles via SemanticQueryManager (Story #290)."""
    repo_dir = Path(_check_wiki_access(request, repo_alias, current_user))
    catalog = restricted_catalog_view(current_user.username, repo_dir, alias=repo_alias)

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
        result = server_platform().run_recorded_search(
            request.app,
            lambda: semantic_query_manager.query_user_repositories(
                username=current_user.username,
                query_text=q.strip(),
                repository_alias=f"{repo_alias}-global",
                search_mode=mode,
                file_extensions=[".md"],
                limit=50,
            ),
            tool=SEARCH_EVENT_TOOL,
            door=SEARCH_EVENT_DOOR,
            username=current_user.username,
            repo_alias=f"{repo_alias}-global",
            search_type=mode,
            query_text=q.strip(),
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


def _article_dirs(rel_path: PurePath) -> Tuple[str, str]:
    """(current_dir, article_rel_path) of a repo-relative article path."""
    current_dir = str(rel_path.parent)
    if current_dir == ".":
        current_dir = ""
    return current_dir, str(rel_path.with_suffix(""))


def _serve_wiki_article_via_seam(
    request: Request,
    repo_alias: str,
    tree: SeamWikiTree,
    path: str,
    *,
    url_prefix: Optional[str] = None,
    catalog: Optional[CatalogArticleView] = None,
) -> Any:
    """:func:`serve_wiki_article` for a repository behind the content backend.

    *repo_alias* keys the wiki cache; *url_prefix* (default: *repo_alias*, the
    golden wiki) is what links and images are rewritten under -- a user wiki
    passes ``u/<user>/<alias>`` with its own ``u:<user>:<alias>`` cache key.
    """
    prefix = repo_alias if url_prefix is None else url_prefix
    rel = tree.confine(path)
    if rel is None:
        raise HTTPException(status_code=404, detail="Not found")
    if not tree.has_path(rel) and not PurePosixPath(rel).suffix:
        md_rel = f"{rel}.md"
        if tree.has_path(md_rel):
            rel = md_rel
    if PurePosixPath(rel).suffix.lower() not in WIKI_ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Not found")
    if not tree.has_file(rel):
        raise HTTPException(status_code=404, detail="Not found")
    if catalog is not None:
        # Raises 404 unless the request is the canonical, allowed file.
        rel = served_seam_path(catalog, path, rel)

    current_dir, article_rel_path = _article_dirs(PurePosixPath(rel))
    cache = _get_wiki_cache(request)
    wiki_config = _get_wiki_config(request)
    cache.increment_view(repo_alias, article_rel_path)
    sidebar = _seam_sidebar(cache, repo_alias, tree, catalog)
    page = _seam_page(
        cache,
        wiki_config,
        repo_alias,
        prefix,
        article_rel_path,
        current_dir,
        tree,
        rel,
        catalog,
    )
    return _article_response(
        request, cache, wiki_config, page, prefix, article_rel_path, sidebar
    )


@wiki_router.get("/{repo_alias}/{path:path}", response_class=HTMLResponse)
def serve_wiki_article(
    repo_alias: str,
    path: str,
    request: Request,
    current_user: WikiCaller = Depends(get_wiki_user_hybrid),
):
    """Serve rendered wiki article."""
    actual_path = _check_wiki_access(request, repo_alias, current_user)
    repo_dir = Path(actual_path)
    catalog = restricted_catalog_view(current_user.username, repo_dir, alias=repo_alias)
    tree = _content_backend_tree(actual_path)
    if tree is not None:
        with _content_unavailable_as_503():
            return _serve_wiki_article_via_seam(
                request, repo_alias, tree, path, catalog=catalog
            )
    if catalog is not None:
        # Raises 404 unless the request is the canonical, allowed file.
        path = served_relative_path(catalog, path, md_fallback=True)
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

    current_dir, article_rel_path = _article_dirs(article_path.relative_to(repo_dir))
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
        article_path.relative_to(repo_dir).as_posix(),
    )
    return _article_response(
        request, cache, wiki_config, page, repo_alias, article_rel_path, sidebar
    )


# ---------------------------------------------------------------------------
# What the feature contributes: the same routes, split by how an
# unauthenticated request is answered. The search routes come first (see the
# module docstring).
# ---------------------------------------------------------------------------

#: The routes ``wiki.js`` fetches: an unauthenticated call is a 401.
SEARCH_ENDPOINTS = frozenset({"wiki_search", "user_wiki_search"})


def _split_router(search: bool) -> APIRouter:
    router = APIRouter(tags=["wiki"])
    for route in wiki_router.routes:
        name = getattr(route, "name", "")
        if (name in SEARCH_ENDPOINTS) == search:
            router.routes.append(route)
    return router


#: The two ``_search`` routes.
search_router = _split_router(search=True)
#: Every page and asset route, in :data:`wiki_router` order.
page_router = _split_router(search=False)
