"""Wiki route endpoints for CIDX Server (Stories #280, #281, #282, #283)."""
import logging
import mimetypes
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from ..auth.dependencies import get_current_user_hybrid
from ..auth.user_manager import User
from ..repositories.golden_repo_manager import GoldenRepoNotFoundError
from .wiki_cache import WikiCache
from .wiki_service import WikiService

logger = logging.getLogger(__name__)
wiki_router = APIRouter()
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
    try:
        actual_path = manager.get_actual_repo_path(repo_alias)
    except (GoldenRepoNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="Not found")
    return actual_path


@wiki_router.get("/{repo_alias}/_assets/{asset_path:path}")
def serve_wiki_asset(repo_alias: str, asset_path: str, request: Request,
                     current_user: User = Depends(get_current_user_hybrid)):
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
                    current_user: User = Depends(get_current_user_hybrid)):
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
        cached_article = cache.get_article(repo_alias, "", home_md)
        if cached_article is not None:
            html = _wiki_service.rewrite_links(cached_article["html"], repo_alias, "")
            breadcrumbs = _wiki_service.build_breadcrumbs("", repo_alias)
            return wiki_templates.TemplateResponse("article.html", {
                "request": request, "title": cached_article["title"],
                "content": html, "repo_alias": repo_alias,
                "sidebar": sidebar, "breadcrumbs": breadcrumbs,
                "current_path": "",
            })
        result = _wiki_service.render_article(home_md, repo_alias)
        cache.put_article(repo_alias, "", result["html"], result["title"], home_md)
        html = _wiki_service.rewrite_links(result["html"], repo_alias, "")
        breadcrumbs = _wiki_service.build_breadcrumbs("", repo_alias)
        return wiki_templates.TemplateResponse("article.html", {
            "request": request, "title": result["title"],
            "content": html, "repo_alias": repo_alias,
            "sidebar": sidebar, "breadcrumbs": breadcrumbs,
            "current_path": "",
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


@wiki_router.get("/{repo_alias}/{path:path}", response_class=HTMLResponse)
def serve_wiki_article(repo_alias: str, path: str, request: Request,
                       current_user: User = Depends(get_current_user_hybrid)):
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

    sidebar = cache.get_sidebar(repo_alias, repo_dir)
    if sidebar is None:
        sidebar = _wiki_service.build_sidebar_tree(repo_dir, repo_alias)
        cache.put_sidebar(repo_alias, sidebar, repo_dir)

    cached_article = cache.get_article(repo_alias, article_rel_path, article_path)
    if cached_article is not None:
        html = _wiki_service.rewrite_links(cached_article["html"], repo_alias, current_dir)
        breadcrumbs = _wiki_service.build_breadcrumbs(article_rel_path, repo_alias)
        return wiki_templates.TemplateResponse("article.html", {
            "request": request, "title": cached_article["title"],
            "content": html, "repo_alias": repo_alias,
            "sidebar": sidebar, "breadcrumbs": breadcrumbs,
            "current_path": article_rel_path,
        })

    try:
        result = _wiki_service.render_article(article_path, repo_alias)
    except (UnicodeDecodeError, OSError):
        raise HTTPException(status_code=404, detail="Not found")

    cache.put_article(repo_alias, article_rel_path, result["html"], result["title"], article_path)

    html = _wiki_service.rewrite_links(result["html"], repo_alias, current_dir)
    breadcrumbs = _wiki_service.build_breadcrumbs(article_rel_path, repo_alias)

    return wiki_templates.TemplateResponse("article.html", {
        "request": request, "title": result["title"],
        "content": html, "repo_alias": repo_alias,
        "sidebar": sidebar, "breadcrumbs": breadcrumbs,
        "current_path": article_rel_path,
    })
