"""The catalog access projection, as the wiki routes apply it.

For the internal catalog repository and a restricted caller, every article,
asset, sidebar entry and search result goes through the caller's view; a
denied path answers exactly like a missing page. The wiki never decides what
is restricted: :func:`restricted_catalog_view` asks the core's
``access_projection`` capability (the projection lives in exactly one place,
above the content seam), and the helpers below apply the view it returns.
None means "nothing restricted" (not the catalog, or an unrestricted caller).

Nothing caller-specific is ever written to the shared wiki cache: projected
pages are rendered per request, and the shared sidebar is filtered after it
is read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from cidx_sdk.capability_types import CatalogArticleView
from fastapi import HTTPException

from cidx_wiki.server import runtime

#: The sidebar's bucket for articles without a category.
UNCATEGORIZED_LABEL = "Uncategorized"


def not_found() -> HTTPException:
    """The missing-page answer, also for a denied path."""
    return HTTPException(status_code=404, detail="Not found")


def restricted_catalog_view(
    viewer: str, repo_dir: Path, *, alias: str, owner: Optional[str] = None
) -> Optional[CatalogArticleView]:
    """*viewer*'s restricted view of the repository served from *repo_dir*
    (golden *alias*, or *owner*'s activated *alias*), or None."""
    projection = runtime.access_projection()
    if projection is None:
        return None
    return projection.catalog_view(viewer, str(repo_dir), alias=alias, owner=owner)


def served_relative_path(
    view: CatalogArticleView, requested: str, *, md_fallback: bool = False
) -> str:
    """The catalog-relative path to serve for *requested*; the missing-page
    404 when it is not the canonical, allowed spelling."""
    served = view.served_path(requested, md_fallback=md_fallback)
    if served is None:
        raise not_found()
    return served


def served_seam_path(view: CatalogArticleView, requested: str, served: str) -> str:
    """:func:`served_relative_path` for a file read through the content seam."""
    allowed = view.served_seam_path(requested, served)
    if allowed is None:
        raise not_found()
    return allowed


def home_allowed(view: Optional[CatalogArticleView], home: str) -> bool:
    """Whether the caller may see the wiki home page *home*."""
    return view is None or view.served_path(home) is not None


def projected_text(view: CatalogArticleView, raw: str, relative_path: str) -> str:
    """The article text the caller may see; the missing-page 404 if denied."""
    projected = view.project_article(raw, relative_path)
    if projected is None:
        raise not_found()
    return projected


def filter_articles(
    articles: List[Dict[str, Any]], view: Optional[CatalogArticleView]
) -> List[Dict[str, Any]]:
    """Article entries (``path`` without extension) the caller may see."""
    if view is None:
        return articles
    return [dict(entry) for entry in view.filter_articles(articles)]


def filter_sidebar(
    sidebar: Sequence[Dict[str, Any]], view: Optional[CatalogArticleView]
) -> List[Any]:
    """The shared sidebar, filtered for the caller (a copy; the shared one is
    never modified)."""
    if view is None:
        return list(sidebar)
    return [dict(group) for group in view.filter_sidebar(sidebar, UNCATEGORIZED_LABEL)]
