"""Wiki route endpoints for CIDX Server (Stories #280, #281, #282, #283, #286)."""
import logging
import mimetypes
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette import status

from ...global_repos.alias_manager import AliasManager
from ..auth.dependencies import get_current_user_hybrid
from ..auth.user_manager import User
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
            credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    try:
        return _hybrid_auth_impl(request, credentials, require_admin=False)
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
WIKI_ASSET_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot", ".pdf",
}

# Module-level singletons: avoids re-instantiation on every request
_wiki_service = WikiService()
_wiki_cache: Optional[WikiCache] = None


def _get_wiki_cache(request: Request) -> WikiCache:
    """Lazily initialise the module-level WikiCache singleton."""
    global _wiki_cache
    if _wiki_cache is None:
        manager = request.app.state.golden_repo_manager
        _wiki_cache = WikiCache(manager.db_path)
        _wiki_cache.ensure_tables()
    return _wiki_cache


def _reset_wiki_cache():
    """Reset module-level cache singleton (for testing)."""
    global _wiki_cache
    _wiki_cache = None


def _check_user_wiki_access(
    request: Request, username: str, alias: str, current_user: User
) -> str:
    """Validate user wiki access; return resolved filesystem path. 404 on any failure.

    Only the repo owner and admin users may access a user wiki (Story #291, AC4).
    """
    access_svc = request.app.state.access_filtering_service
    if current_user.username != username and not access_svc.is_admin_user(
        current_user.username
    ):
        raise HTTPException(status_code=404, detail="Not found")

    activated_repo_manager = request.app.state.activated_repo_manager
    if not activated_repo_manager.get_wiki_enabled(username, alias):
        raise HTTPException(status_code=404, detail="Not found")

    repo_path = activated_repo_manager.get_activated_repo_path(username, alias)
    if not Path(repo_path).exists():
        raise HTTPException(status_code=404, detail="Not found")

    return repo_path


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
    repo_path = _check_user_wiki_access(request, username, alias, current_user)
    repo_dir = Path(repo_path)
    file_path = repo_dir / asset_path
    try:
        file_path.resolve().relative_to(repo_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    if file_path.suffix.lower() not in WIKI_ASSET_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Not found")
    media_type, _ = mimetypes.guess_type(str(file_path))
    return FileResponse(
        path=str(file_path), media_type=media_type or "application/octet-stream"
    )


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
    _check_user_wiki_access(request, username, alias, current_user)

    if len(q.strip()) < 2:
        return JSONResponse(content=[])

    if mode not in ("semantic", "fts"):
        mode = "semantic"

    if not hasattr(request.app.state, "semantic_query_manager"):
        return JSONResponse(content={"error": "Search unavailable"})

    semantic_query_manager = request.app.state.semantic_query_manager
    try:
        result = semantic_query_manager.query_user_repositories(
            username=username,
            query_text=q.strip(),
            repository_alias=alias,
            search_mode=mode,
            file_extensions=[".md"],
            limit=50,
        )
    except Exception:
        logger.warning(
            "User wiki search failed for %s/%s query %r", username, alias, q,
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
        mapped.append({
            "path": clean_path,
            "score": item.get("similarity_score", 0.0),
            "title": title,
        })
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

    home_md = repo_dir / "home.md"
    cache = _get_wiki_cache(request)

    sidebar = cache.get_sidebar(cache_repo_alias, repo_dir)
    if sidebar is None:
        sidebar = _wiki_service.build_sidebar_tree(repo_dir, url_prefix)
        cache.put_sidebar(cache_repo_alias, sidebar, repo_dir)

    if home_md.exists() and home_md.is_file():
        cache.increment_view(cache_repo_alias, "")
        cached_article = cache.get_article(cache_repo_alias, "", home_md)
        if cached_article is not None:
            html = _wiki_service.rewrite_links(cached_article["html"], url_prefix, "")
            breadcrumbs = _wiki_service.build_breadcrumbs("", url_prefix)
            cached_meta = cached_article.get("metadata") or {}
            metadata_panel = (
                _wiki_service.prepare_metadata_context(
                    cached_meta, url_prefix, "", cache
                )
                or None
            )
            return wiki_templates.TemplateResponse(
                "article.html",
                {
                    "request": request,
                    "title": cached_article["title"],
                    "content": html,
                    "repo_alias": url_prefix,
                    "sidebar": sidebar,
                    "breadcrumbs": breadcrumbs,
                    "current_path": "",
                    "metadata_panel": metadata_panel,
                },
            )
        result = _wiki_service.render_article(home_md, url_prefix)
        cache.put_article(
            cache_repo_alias, "", result["html"], result["title"], home_md,
            metadata=result.get("metadata"),
        )
        html = _wiki_service.rewrite_links(result["html"], url_prefix, "")
        breadcrumbs = _wiki_service.build_breadcrumbs("", url_prefix)
        metadata_panel = (
            _wiki_service.prepare_metadata_context(
                result.get("metadata") or {}, url_prefix, "", cache
            )
            or None
        )
        return wiki_templates.TemplateResponse(
            "article.html",
            {
                "request": request,
                "title": result["title"],
                "content": html,
                "repo_alias": url_prefix,
                "sidebar": sidebar,
                "breadcrumbs": breadcrumbs,
                "current_path": "",
                "metadata_panel": metadata_panel,
            },
        )

    # No home.md — render article index
    articles = []
    for md_file in sorted(repo_dir.rglob("*.md")):
        rel = md_file.relative_to(repo_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        stem = md_file.stem.replace("-", " ").replace("_", " ").title()
        articles.append({"path": str(rel.with_suffix("")), "title": stem})
    return wiki_templates.TemplateResponse(
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

    article_path = repo_dir / path
    try:
        article_path.resolve().relative_to(repo_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if not article_path.exists() and not article_path.suffix:
        md_path = article_path.with_suffix(".md")
        if md_path.exists():
            article_path = md_path
    if article_path.suffix.lower() not in WIKI_ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Not found")
    if not article_path.exists() or not article_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    rel_path = article_path.relative_to(repo_dir)
    current_dir = str(rel_path.parent)
    if current_dir == ".":
        current_dir = ""
    article_rel_path = str(rel_path.with_suffix(""))

    cache = _get_wiki_cache(request)
    cache.increment_view(cache_repo_alias, article_rel_path)

    sidebar = cache.get_sidebar(cache_repo_alias, repo_dir)
    if sidebar is None:
        sidebar = _wiki_service.build_sidebar_tree(repo_dir, url_prefix)
        cache.put_sidebar(cache_repo_alias, sidebar, repo_dir)

    cached_article = cache.get_article(cache_repo_alias, article_rel_path, article_path)
    if cached_article is not None:
        html = _wiki_service.rewrite_links(
            cached_article["html"], url_prefix, current_dir
        )
        breadcrumbs = _wiki_service.build_breadcrumbs(article_rel_path, url_prefix)
        cached_meta = cached_article.get("metadata") or {}
        metadata_panel = (
            _wiki_service.prepare_metadata_context(
                cached_meta, url_prefix, article_rel_path, cache
            )
            or None
        )
        return wiki_templates.TemplateResponse(
            "article.html",
            {
                "request": request,
                "title": cached_article["title"],
                "content": html,
                "repo_alias": url_prefix,
                "sidebar": sidebar,
                "breadcrumbs": breadcrumbs,
                "current_path": article_rel_path,
                "metadata_panel": metadata_panel,
            },
        )

    try:
        result = _wiki_service.render_article(article_path, url_prefix)
    except (UnicodeDecodeError, OSError):
        raise HTTPException(status_code=404, detail="Not found")

    cache.put_article(
        cache_repo_alias,
        article_rel_path,
        result["html"],
        result["title"],
        article_path,
        metadata=result.get("metadata"),
    )

    html = _wiki_service.rewrite_links(result["html"], url_prefix, current_dir)
    breadcrumbs = _wiki_service.build_breadcrumbs(article_rel_path, url_prefix)
    metadata_panel = (
        _wiki_service.prepare_metadata_context(
            result.get("metadata") or {}, url_prefix, article_rel_path, cache
        )
        or None
    )

    return wiki_templates.TemplateResponse(
        "article.html",
        {
            "request": request,
            "title": result["title"],
            "content": html,
            "repo_alias": url_prefix,
            "sidebar": sidebar,
            "breadcrumbs": breadcrumbs,
            "current_path": article_rel_path,
            "metadata_panel": metadata_panel,
        },
    )


# ---------------------------------------------------------------------------
# Golden repo wiki routes (existing — unchanged)
# ---------------------------------------------------------------------------

@wiki_router.get("/{repo_alias}/_assets/{asset_path:path}")
def serve_wiki_asset(repo_alias: str, asset_path: str, request: Request,
                     current_user: User = Depends(get_wiki_user_hybrid)):
    """Serve image/asset files from wiki repo."""
    actual_path = _check_wiki_access(request, repo_alias, current_user)
    repo_dir = Path(actual_path)
    file_path = repo_dir / asset_path
    try:
        file_path.resolve().relative_to(repo_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    if file_path.suffix.lower() not in WIKI_ASSET_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Not found")
    media_type, _ = mimetypes.guess_type(str(file_path))
    return FileResponse(path=str(file_path), media_type=media_type or "application/octet-stream")


@wiki_router.get("/{repo_alias}/", response_class=HTMLResponse)
def serve_wiki_root(repo_alias: str, request: Request,
                    current_user: User = Depends(get_wiki_user_hybrid)):
    """Serve wiki root page - home.md or article index."""
    actual_path = _check_wiki_access(request, repo_alias, current_user)
    repo_dir = Path(actual_path)
    home_md = repo_dir / "home.md"
    cache = _get_wiki_cache(request)

    sidebar = cache.get_sidebar(repo_alias, repo_dir)
    if sidebar is None:
        sidebar = _wiki_service.build_sidebar_tree(repo_dir, repo_alias)
        cache.put_sidebar(repo_alias, sidebar, repo_dir)

    if home_md.exists() and home_md.is_file():
        cache.increment_view(repo_alias, "")
        cached_article = cache.get_article(repo_alias, "", home_md)
        if cached_article is not None:
            html = _wiki_service.rewrite_links(cached_article["html"], repo_alias, "")
            breadcrumbs = _wiki_service.build_breadcrumbs("", repo_alias)
            cached_meta = cached_article.get("metadata") or {}
            metadata_panel = _wiki_service.prepare_metadata_context(cached_meta, repo_alias, "", cache) or None
            return wiki_templates.TemplateResponse("article.html", {
                "request": request, "title": cached_article["title"],
                "content": html, "repo_alias": repo_alias,
                "sidebar": sidebar, "breadcrumbs": breadcrumbs,
                "current_path": "", "metadata_panel": metadata_panel,
            })
        result = _wiki_service.render_article(home_md, repo_alias)
        cache.put_article(repo_alias, "", result["html"], result["title"], home_md, metadata=result.get("metadata"))
        html = _wiki_service.rewrite_links(result["html"], repo_alias, "")
        breadcrumbs = _wiki_service.build_breadcrumbs("", repo_alias)
        metadata_panel = _wiki_service.prepare_metadata_context(result.get("metadata") or {}, repo_alias, "", cache) or None
        return wiki_templates.TemplateResponse("article.html", {
            "request": request, "title": result["title"],
            "content": html, "repo_alias": repo_alias,
            "sidebar": sidebar, "breadcrumbs": breadcrumbs,
            "current_path": "", "metadata_panel": metadata_panel,
        })
    articles = []
    for md_file in sorted(repo_dir.rglob("*.md")):
        rel = md_file.relative_to(repo_dir)
        if any(part.startswith('.') for part in rel.parts):
            continue
        stem = md_file.stem.replace("-", " ").replace("_", " ").title()
        articles.append({"path": str(rel.with_suffix("")), "title": stem})
    return wiki_templates.TemplateResponse("index.html", {
        "request": request, "title": f"Wiki: {repo_alias}",
        "repo_alias": repo_alias, "articles": articles,
    })


@wiki_router.get("/{repo_alias}/_search")
def wiki_search(
    repo_alias: str,
    request: Request,
    q: str = "",
    mode: str = "semantic",
    current_user: User = Depends(get_current_user_hybrid),
) -> JSONResponse:
    """Search wiki articles via SemanticQueryManager (Story #290)."""
    _check_wiki_access(request, repo_alias, current_user)

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
        result = semantic_query_manager.query_user_repositories(
            username=current_user.username,
            query_text=q.strip(),
            repository_alias=f"{repo_alias}-global",
            search_mode=mode,
            file_extensions=[".md"],
            limit=50,
        )
    except Exception as exc:
        logger.warning("Wiki search failed for repo %s query %r", repo_alias, q, exc_info=True)
        error_msg = str(exc)
        # Surface specific provider errors to the user
        if "API key" in error_msg or "Unauthorized" in error_msg:
            return JSONResponse(content={"error": f"Semantic search error: {error_msg}"})
        return JSONResponse(content={"error": "Search failed — check server logs for details"})

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
        mapped.append({
            "path": clean_path,
            "score": item.get("similarity_score", 0.0),
            "title": title,
        })

    return JSONResponse(content=mapped)


@wiki_router.get("/{repo_alias}/{path:path}", response_class=HTMLResponse)
def serve_wiki_article(repo_alias: str, path: str, request: Request,
                       current_user: User = Depends(get_wiki_user_hybrid)):
    """Serve rendered wiki article."""
    actual_path = _check_wiki_access(request, repo_alias, current_user)
    repo_dir = Path(actual_path)
    article_path = repo_dir / path
    try:
        article_path.resolve().relative_to(repo_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if not article_path.exists() and not article_path.suffix:
        md_path = article_path.with_suffix(".md")
        if md_path.exists():
            article_path = md_path
    if article_path.suffix.lower() not in WIKI_ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Not found")
    if not article_path.exists() or not article_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    rel_path = article_path.relative_to(repo_dir)
    current_dir = str(rel_path.parent)
    if current_dir == ".":
        current_dir = ""
    article_rel_path = str(rel_path.with_suffix(""))

    cache = _get_wiki_cache(request)
    cache.increment_view(repo_alias, article_rel_path)

    sidebar = cache.get_sidebar(repo_alias, repo_dir)
    if sidebar is None:
        sidebar = _wiki_service.build_sidebar_tree(repo_dir, repo_alias)
        cache.put_sidebar(repo_alias, sidebar, repo_dir)

    cached_article = cache.get_article(repo_alias, article_rel_path, article_path)
    if cached_article is not None:
        html = _wiki_service.rewrite_links(cached_article["html"], repo_alias, current_dir)
        breadcrumbs = _wiki_service.build_breadcrumbs(article_rel_path, repo_alias)
        cached_meta = cached_article.get("metadata") or {}
        metadata_panel = _wiki_service.prepare_metadata_context(cached_meta, repo_alias, article_rel_path, cache) or None
        return wiki_templates.TemplateResponse("article.html", {
            "request": request, "title": cached_article["title"],
            "content": html, "repo_alias": repo_alias,
            "sidebar": sidebar, "breadcrumbs": breadcrumbs,
            "current_path": article_rel_path, "metadata_panel": metadata_panel,
        })

    try:
        result = _wiki_service.render_article(article_path, repo_alias)
    except (UnicodeDecodeError, OSError):
        raise HTTPException(status_code=404, detail="Not found")

    cache.put_article(
        repo_alias, article_rel_path, result["html"], result["title"], article_path,
        metadata=result.get("metadata"),
    )

    html = _wiki_service.rewrite_links(result["html"], repo_alias, current_dir)
    breadcrumbs = _wiki_service.build_breadcrumbs(article_rel_path, repo_alias)
    metadata_panel = _wiki_service.prepare_metadata_context(
        result.get("metadata") or {}, repo_alias, article_rel_path, cache
    ) or None

    return wiki_templates.TemplateResponse("article.html", {
        "request": request, "title": result["title"],
        "content": html, "repo_alias": repo_alias,
        "sidebar": sidebar, "breadcrumbs": breadcrumbs,
        "current_path": article_rel_path, "metadata_panel": metadata_panel,
    })
