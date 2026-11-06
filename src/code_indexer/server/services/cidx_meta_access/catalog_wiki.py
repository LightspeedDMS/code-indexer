"""The internal catalog served as wiki articles, for a restricted caller.

For the catalog repository and a caller outside the admins group, every
article, asset, sidebar entry and search result path goes through the
catalog path policy; a denied path answers exactly like a missing page.
The policy judges the file that is served: a request is resolved first
(confined to the repository, never into ``.git``) and only its canonical
spelling is served, so ``./``, ``//``, ``..``, backslashes and in-repo
symlink aliases answer not found. Description and dependency-map documents
are served as their projection. Sidebar titles and category names come
from raw article content, so they are projected like prose too.

This is the projection only; the cidx-wiki feature renders. It reaches it
through the ``access_projection`` capability
(``server/features/access_projection.py``), which turns the missing-page
answer of these functions into ``None``. Nothing caller-specific may be
written to a shared cache: the wiki renders a projected page per request
and filters the shared sidebar after reading it. Synchronous file I/O:
the wiki routes are sync ``def``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ....utils.path_confinement import resolve_confined_path
from . import (
    Decision,
    DecisionKind,
    MetaProjector,
    MetaVisibility,
    render_for_display,
    resolve_meta_visibility,
)
from .catalog_articles import article_relative_path, catalog_decider
from .path_policy import catalog_dep_map_dir
from .universe_provider import process_alias_universe, registry_of

_PROJECTED = frozenset({DecisionKind.DESCRIPTION, DecisionKind.TRANSFORM})


class CatalogPathNotServed(LookupError):
    """The requested catalog path is denied, missing or not canonical: the
    caller is answered exactly as for a missing page."""


@dataclass(frozen=True)
class CatalogWikiView:
    vis: MetaVisibility
    decide: Callable[[str], Decision]
    projector: MetaProjector
    repo_dir: Path


def restricted_catalog_view(
    app_state: Any, viewer: str, repo_dir: Path, is_catalog: bool
) -> Optional[CatalogWikiView]:
    """The viewer's restricted view of the catalog served from *repo_dir*;
    None when the repository is not the catalog or the viewer is
    unrestricted."""
    if not is_catalog:
        return None
    access_service = getattr(app_state, "access_filtering_service", None)
    vis = resolve_meta_visibility(viewer, access_service)
    if vis.unrestricted:
        return None
    projector = MetaProjector(
        vis,
        dep_map_dir=catalog_dep_map_dir(repo_dir),
        universe=process_alias_universe(registry_of(app_state)),
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


def _decision_or_refuse(view: CatalogWikiView, relative_path: str) -> Decision:
    decision = view.decide(relative_path)
    if decision.kind is DecisionKind.DENY:
        raise CatalogPathNotServed(relative_path)
    return decision


def refuse_denied(view: CatalogWikiView, relative_path: str) -> None:
    """Raise :class:`CatalogPathNotServed` when *relative_path* is denied."""
    _decision_or_refuse(view, relative_path)


def served_relative_path(
    view: CatalogWikiView, requested: str, *, md_fallback: bool = False
) -> Tuple[Path, str]:
    """The file *requested* names and its catalog-relative path; raises
    :class:`CatalogPathNotServed` for a non-canonical spelling, an escape, a
    symlink alias or a denied path. With *md_fallback* an extension-less
    article name is its ``.md`` file when that exists."""
    found = _canonical(view, requested)
    if md_fallback and (found is None or not found[0].exists()):
        found = _canonical(view, f"{requested}.md") or found
    if found is None:
        raise CatalogPathNotServed(requested)
    refuse_denied(view, found[1])
    return found


def served_seam_path(view: CatalogWikiView, requested: str, served: str) -> str:
    """:func:`served_relative_path` for a file read through the content
    backend (a mount-less pod: nothing to ``resolve()`` or ``exists()``
    here). *served* is the backend-confined path the route chose for
    *requested* -- itself, or its ``.md`` file."""
    if served not in (requested, f"{requested}.md"):
        raise CatalogPathNotServed(requested)
    if _canonical(view, served) is None:
        raise CatalogPathNotServed(requested)
    refuse_denied(view, served)
    return served


def projected_source(view: CatalogWikiView, raw: str, relative_path: str) -> str:
    """The article text the restricted caller may see: projected for a
    description or dependency-map document, as is otherwise. Raises
    :class:`CatalogPathNotServed` when denied. Never cached."""
    decision = _decision_or_refuse(view, relative_path)
    if decision.kind in _PROJECTED:
        return render_for_display(
            view.projector.project(raw.splitlines(), relative_path)
        )
    return raw


def _article_visible(view: CatalogWikiView, article_path: str) -> bool:
    # _canonical only succeeds when the served path equals the requested
    # one, so judging the request first is the same decision without I/O.
    requested = article_relative_path(article_path)
    if view.decide(requested).kind is DecisionKind.DENY:
        return False
    return _canonical(view, requested) is not None


def filter_articles(
    articles: Sequence[Dict[str, Any]], view: CatalogWikiView
) -> List[Dict[str, Any]]:
    """Article entries (``path`` without extension) that resolve to
    themselves and that the caller may see."""
    return [a for a in articles if _article_visible(view, str(a["path"]))]


def _projected_label(view: CatalogWikiView, text: str, fallback: str) -> str:
    """*text* (a title or category taken from an article's raw content)
    through the caller's view (never the shared projection cache);
    *fallback* when it is removed."""
    projected = view.projector.redact_label(text)
    return projected if projected else fallback


def _projected_entries(
    entries: List[Dict[str, Any]], view: CatalogWikiView
) -> List[Dict[str, Any]]:
    kept = []
    for entry in filter_articles(entries, view):
        path = str(entry["path"])
        derived = PurePosixPath(path).name.replace("-", " ").replace("_", " ").title()
        title = _projected_label(view, str(entry.get("title", "")), derived)
        kept.append({**entry, "title": title})
    return kept


def filter_sidebar(
    sidebar: Sequence[Dict[str, Any]], view: CatalogWikiView, uncategorized_label: str
) -> List[Dict[str, Any]]:
    """A filtered copy of the shared sidebar: entries the caller may see,
    their titles and category names projected like prose (categories that
    project to one name are merged); empty categories and groups are
    dropped. The shared copy is never modified."""
    groups: List[Dict[str, Any]] = []
    for group in sidebar:
        categories: Dict[str, List[Dict[str, Any]]] = {}
        for name, entries in (group.get("categories") or {}).items():
            kept = _projected_entries(list(entries), view)
            if not kept:
                continue
            label = _projected_label(view, str(name), uncategorized_label)
            categories.setdefault(label, []).extend(kept)
        for merged in categories.values():
            merged.sort(key=lambda a: str(a["title"]).lower())
        articles = _projected_entries(list(group.get("articles") or []), view)
        if categories or articles:
            groups.append({**group, "categories": categories, "articles": articles})
    return groups
