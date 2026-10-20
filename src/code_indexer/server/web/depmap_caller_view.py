"""The Web dependency map's view for one caller.

Visibility comes from the server's access filtering service (database
backed, so every cluster node agrees) and admin means admins-group
membership, never the session role. A restricted caller reads the projected
dependency-map source. Resolved fresh per request; routes are sync ``def``
(threadpool), so the reads never block the event loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Set

from fastapi import Request

from ..services.cidx_meta_access import MetaVisibility, resolve_meta_visibility
from ..services.cidx_meta_access.depmap_source import source_for_visibility
from ..services.cidx_meta_access.universe_provider import registry_of
from ..services.dep_map_source import DepMapSource

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DepMapCallerView:
    """``source`` is None when the dependency map is unavailable."""

    vis: MetaVisibility
    dep_map_service: Any
    source: Optional[DepMapSource]

    @property
    def is_admin(self) -> bool:
        return self.vis.unrestricted

    @property
    def accessible_repos(self) -> Optional[Set[str]]:
        """None for an unrestricted caller, else the visible repositories."""
        return None if self.vis.unrestricted else set(self.vis.visible)


def resolve_caller_view(request: Request, username: str) -> DepMapCallerView:
    state = request.app.state
    try:
        vis = resolve_meta_visibility(
            username, getattr(state, "access_filtering_service", None)
        )
    except Exception as exc:
        logger.warning(
            "Dependency map visibility unavailable (%s); showing nothing",
            type(exc).__name__,
        )
        vis = MetaVisibility.restricted(())
    dep_map_service = getattr(state, "dependency_map_service", None)
    source: Optional[DepMapSource] = None
    if dep_map_service is not None:
        try:
            output_dir = Path(dep_map_service.cidx_meta_read_path) / "dependency-map"
            source = source_for_visibility(vis, output_dir, registry_of(state))
        except Exception as exc:
            logger.warning(
                "Dependency map source unavailable (%s); showing nothing",
                type(exc).__name__,
            )
    return DepMapCallerView(vis, dep_map_service, source)
