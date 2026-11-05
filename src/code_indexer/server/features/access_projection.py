"""``access_projection``: the catalog access projection, as a capability.

The internal catalog (cidx-meta) is filtered per caller. A feature that
serves repository files as documents (the wiki) never decides that itself:
it asks this capability for the caller's view and serves through it, so the
projection is applied in exactly one place, above the content seam (end
state, sections 7 and 15.6). The dependency map's MCP tools and pages ask
:meth:`CoreAccessProjectionCapability.dependency_map_access`. Memory files (``memories/<id>.md``) are
decided by the access-filtering service's memory scope rule
(:meth:`CoreAccessProjectionCapability.visible_memory_ids`). The view is
``services/cidx_meta_access/catalog_wiki.py`` -- the same path policy,
description/dependency-map projection and canonical-path rule the file
doors (``FileListingService``) apply; this module only adapts its
missing-page answer to the SDK's ``None``.

A request with no access-filtering service is restricted (the catalog
visibility resolver treats an unknown caller as restricted).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence

from cidx_sdk.capability_types import CatalogArticleView, DependencyMapView

from code_indexer.server.services.cidx_meta_access import (
    is_catalog_request,
    resolve_meta_visibility,
)
from code_indexer.server.services.cidx_meta_access.catalog_articles import (
    catalog_decider,
    visible_articles,
)
from code_indexer.server.services.cidx_meta_access.depmap_source import (
    source_for_visibility,
)
from code_indexer.server.services.cidx_meta_access.universe_provider import (
    registry_of,
)
from code_indexer.server.services.cidx_meta_access.visibility import (
    MetaVisibility,
)
from code_indexer.server.services.cidx_meta_read_mirror import catalog_read_path
from code_indexer.server.services.cidx_meta_access.catalog_wiki import (
    CatalogPathNotServed,
    CatalogWikiView,
    filter_articles,
    filter_sidebar,
    projected_source,
    restricted_catalog_view,
    served_relative_path,
    served_seam_path,
)
from code_indexer.server.services.constants import is_internal_meta_repo


class CoreCatalogArticleView:
    """:class:`cidx_sdk.capability_types.CatalogArticleView` over one
    caller's :class:`CatalogWikiView`."""

    def __init__(self, view: CatalogWikiView) -> None:
        self._view = view

    def served_path(
        self, requested: str, *, md_fallback: bool = False
    ) -> Optional[str]:
        try:
            return served_relative_path(self._view, requested, md_fallback=md_fallback)[
                1
            ]
        except CatalogPathNotServed:
            return None

    def served_seam_path(self, requested: str, served: str) -> Optional[str]:
        try:
            return served_seam_path(self._view, requested, served)
        except CatalogPathNotServed:
            return None

    def project_article(self, raw: str, relative_path: str) -> Optional[str]:
        try:
            return projected_source(self._view, raw, relative_path)
        except CatalogPathNotServed:
            return None

    def filter_articles(
        self, articles: Sequence[Mapping[str, Any]]
    ) -> Sequence[Mapping[str, Any]]:
        return filter_articles([dict(a) for a in articles], self._view)

    def filter_sidebar(
        self, sidebar: Sequence[Mapping[str, Any]], uncategorized_label: str
    ) -> Sequence[Mapping[str, Any]]:
        return filter_sidebar(
            [dict(g) for g in sidebar], self._view, uncategorized_label
        )


class CoreDependencyMapAccess:
    """:class:`cidx_sdk.capability_types.DependencyMapAccess` over one
    caller's resolved dependency-map source."""

    def __init__(self, source: Any, vis: MetaVisibility) -> None:
        self._source = source
        self._vis = vis

    @property
    def restricted(self) -> bool:
        return not self._vis.unrestricted

    @property
    def visible_repositories(self) -> Optional[FrozenSet[str]]:
        return None if self._vis.unrestricted else frozenset(self._vis.visible)

    @property
    def view(self) -> DependencyMapView:
        view: DependencyMapView = self._source
        return view


class CoreAccessProjectionCapability:
    """``access_projection`` over the app's access-filtering service."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def _is_catalog(self, alias: str, owner: Optional[str]) -> bool:
        if owner is None:
            return bool(is_internal_meta_repo(alias))
        manager = getattr(self._app.state, "activated_repo_manager", None)
        return bool(is_catalog_request(alias, owner, manager))

    def catalog_view(
        self, viewer: str, repo_dir: str, *, alias: str, owner: Optional[str] = None
    ) -> Optional[CatalogArticleView]:
        view = restricted_catalog_view(
            self._app.state, viewer, Path(repo_dir), self._is_catalog(alias, owner)
        )
        return None if view is None else CoreCatalogArticleView(view)

    def visible_article_views(
        self, viewer: str, alias: str, rows: Sequence[Mapping[str, Any]]
    ) -> Sequence[Mapping[str, Any]]:
        listed: List[Dict[str, Any]] = [dict(row) for row in rows]
        if not is_internal_meta_repo(alias):
            return listed
        state = self._app.state
        access_service = getattr(state, "access_filtering_service", None)
        vis = resolve_meta_visibility(viewer, access_service)
        if vis.unrestricted:
            return listed
        catalog_root = catalog_read_path(state.golden_repo_manager)
        decide = catalog_decider(catalog_root, vis, access_service)
        return visible_articles(listed, lambda v: str(v["article_path"]), decide)

    def visible_memory_ids(
        self, viewer: str, memory_ids: Sequence[str]
    ) -> Sequence[str]:
        """The memory files' scope rule (``AccessFilteringService
        .filter_visible_memory_ids``, frontmatter read fresh); without an
        access-filtering service nothing is visible."""
        access_service = getattr(self._app.state, "access_filtering_service", None)
        if access_service is None:
            return []
        visible = set(
            access_service.filter_visible_memory_ids(list(memory_ids), viewer)
        )
        return [memory_id for memory_id in memory_ids if memory_id in visible]

    def dependency_map_access(
        self, viewer: str, map_dir: str
    ) -> CoreDependencyMapAccess:
        """The catalog's dependency-map projection for *viewer*
        (``cidx_meta_access.depmap_source.source_for_visibility``: the same
        path policy and projection the catalog's file doors apply), for the
        visibility resolved fresh now."""
        state = self._app.state
        access_service = getattr(state, "access_filtering_service", None)
        vis = resolve_meta_visibility(viewer, access_service)
        source = source_for_visibility(vis, Path(map_dir), registry_of(state))
        return CoreDependencyMapAccess(source, vis)
