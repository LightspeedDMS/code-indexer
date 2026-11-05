"""The Web dependency map's view for one caller.

Visibility is the core's catalog access projection, asked through the
``access_projection`` capability (database-backed access filtering, so every
cluster node agrees; admin means admins-group membership, never the session
role). A restricted caller reads the projected dependency map. Resolved fresh
per request; routes are sync ``def`` (threadpool), so the reads never block
the event loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, FrozenSet, Optional, Set

from cidx_sdk.capability_types import DependencyMapView
from fastapi import Request

from cidx_depmap.server import runtime

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DepMapCallerView:
    """``source`` is None when the dependency map is unavailable.
    ``visible`` is None for an unrestricted caller."""

    visible: Optional[FrozenSet[str]]
    dep_map_service: Any
    source: Optional[DependencyMapView]

    @property
    def is_admin(self) -> bool:
        return self.visible is None

    @property
    def accessible_repos(self) -> Optional[Set[str]]:
        """None for an unrestricted caller, else the visible repositories."""
        return None if self.visible is None else set(self.visible)


#: What a caller whose visibility could not be decided sees: nothing.
_NOTHING: FrozenSet[str] = frozenset()


def resolve_caller_view(request: Request, username: str) -> DepMapCallerView:
    state = request.app.state
    dep_map_service = runtime.dependency_map_service()
    projection = runtime.access_projection()
    if projection is None:
        logger.warning("Dependency map access projection unavailable; showing nothing")
        return DepMapCallerView(_NOTHING, dep_map_service, None)
    if dep_map_service is not None:
        map_dir: Optional[Path] = (
            Path(dep_map_service.cidx_meta_read_path) / "dependency-map"
        )
    else:
        # No running service: the caller's visibility still decides what the
        # page shows; there is no map to read.
        golden = getattr(state, "golden_repos_dir", None)
        map_dir = Path(golden) / "cidx-meta" / "dependency-map" if golden else None
    if map_dir is None:
        return DepMapCallerView(_NOTHING, None, None)
    try:
        access = projection.dependency_map_access(username, str(map_dir))
    except Exception as exc:
        logger.warning(
            "Dependency map visibility unavailable (%s); showing nothing",
            type(exc).__name__,
        )
        return DepMapCallerView(_NOTHING, dep_map_service, None)
    visible = access.visible_repositories
    source = access.view if dep_map_service is not None else None
    return DepMapCallerView(visible, dep_map_service, source)
