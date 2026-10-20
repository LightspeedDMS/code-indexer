"""The internal catalog's wiki for a restricted caller.

For the catalog repository and a caller outside the admins group, every
article, asset, sidebar entry and search result path goes through the
catalog path policy; a denied path answers exactly like a missing page.
The policy judges the file that is served: a request is resolved first
(confined to the repository, never into ``.git``) and only its canonical
spelling is served, so ``./``, ``//``, ``..``, backslashes and in-repo
symlink aliases answer not found. Description and dependency-map documents
are rendered from their projection. Nothing caller-specific is ever written
to the shared wiki cache: pages are rendered per request, and the shared
sidebar is filtered after it is read. Wiki routes are sync ``def``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request

from ...utils.path_confinement import resolve_confined_path
from ..services.cidx_meta_access import (
    Decision,
    DecisionKind,
    MetaProjector,
    MetaVisibility,
    render_for_display,
    resolve_meta_visibility,
)
from ..services.cidx_meta_access.catalog_articles import (
    article_relative_path,
    catalog_decider,
)
from ..services.cidx_meta_access.path_policy import DEP_MAP_DIR
from ..services.cidx_meta_access.universe_provider import (
    process_alias_universe,
    registry_of,
)

_PROJECTED = frozenset({DecisionKind.DESCRIPTION, DecisionKind.TRANSFORM})


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Not found")


@dataclass(frozen=True)
class CatalogWikiView:
    vis: MetaVisibility
    decide: Callable[[str], Decision]
    projector: MetaProjector
    repo_dir: Path


def restricted_catalog_view(
    request: Request, viewer: str, repo_dir: Path, is_catalog: bool
) -> Optional[CatalogWikiView]:
    """The viewer's restricted view of the catalog served from *repo_dir*;
    None when the repository is not the catalog or the viewer is
    unrestricted."""
    if not is_catalog:
        return None
    state = request.app.state
    access_service = getattr(state, "access_filtering_service", None)
    vis = resolve_meta_visibility(viewer, access_service)
    if vis.unrestricted:
        return None
    projector = MetaProjector(
        vis,
        dep_map_dir=repo_dir / DEP_MAP_DIR,
        universe=process_alias_universe(registry_of(state)),
    )
    return CatalogWikiView(
        vis, catalog_decider(repo_dir, vis, access_service), projector, repo_dir
    )


def _canonical(view: CatalogWikiView, requested: str) -> Optional[Tuple[Path, str]]:
    """(resolved file, catalog-relative path) when *requested* is the
    canonical spelling of a file inside the repository; else None."""
    try:
        resolved = resolve_confined_path(view.repo_dir, requested)
    except PermissionError:
        return None
    relative = resolved.relative_to(view.repo_dir.resolve()).as_posix()
    if relative != requested:
        return None
    return resolved, relative


def served_relative_path(
    view: CatalogWikiView, requested: str, *, md_fallback: bool = False
) -> Tuple[Path, str]:
    """The file *requested* names and its catalog-relative path; raises
    the missing-page 404 for a non-canonical spelling, an escape, a symlink
    alias or a denied path. With *md_fallback* an extension-less article
    name is its ``.md`` file when that exists."""
    found = _canonical(view, requested)
    if md_fallback and (found is None or not found[0].exists()):
        found = _canonical(view, f"{requested}.md") or found
    if found is None:
        raise _not_found()
    refuse_denied(view, found[1])
    return found


def _decision_or_404(view: CatalogWikiView, relative_path: str) -> Decision:
    decision = view.decide(relative_path)
    if decision.kind is DecisionKind.DENY:
        raise _not_found()
    return decision


def refuse_denied(view: CatalogWikiView, relative_path: str) -> None:
    """Raise the missing-page 404 when *relative_path* is denied."""
    _decision_or_404(view, relative_path)


def render_projected_page(
    view: CatalogWikiView,
    wiki_service: Any,
    article_path: Path,
    relative_path: str,
    url_prefix: str,
    current_dir: str,
    wiki_config: Any,
) -> Dict[str, Any]:
    """The final page of one catalog article for the restricted caller,
    rendered from its projection (never cached)."""
    decision = _decision_or_404(view, relative_path)
    try:
        raw = article_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        raise _not_found()
    if decision.kind in _PROJECTED:
        raw = render_for_display(
            view.projector.project(raw.splitlines(), relative_path)
        )
    result = wiki_service.render_article_text(
        raw, article_path, url_prefix, wiki_config=wiki_config
    )
    html = wiki_service.prepare_article_content(result["html"], url_prefix, current_dir)
    return {"html": html, "title": result["title"], "metadata": result["metadata"]}


def _article_visible(view: CatalogWikiView, article_path: str) -> bool:
    found = _canonical(view, article_relative_path(article_path))
    return found is not None and view.decide(found[1]).kind is not DecisionKind.DENY


def filter_articles(
    articles: List[Dict[str, Any]], view: CatalogWikiView
) -> List[Dict[str, Any]]:
    """Article entries (``path`` without extension) that resolve to
    themselves and that the caller may see."""
    return [a for a in articles if _article_visible(view, str(a["path"]))]


def filter_sidebar(sidebar: List[Dict[str, Any]], view: CatalogWikiView) -> List[Any]:
    """A filtered copy of the shared sidebar; empty categories and groups
    are dropped. The shared copy is never modified."""
    groups: List[Any] = []
    for group in sidebar:
        categories = {}
        for name, entries in (group.get("categories") or {}).items():
            kept = filter_articles(list(entries), view)
            if kept:
                categories[name] = kept
        articles = filter_articles(list(group.get("articles") or []), view)
        if categories or articles:
            groups.append({**group, "categories": categories, "articles": articles})
    return groups
