"""Catalog surfaces that cannot be projected line by line: admin-only.

Git history (old and deleted documents, diffs, blame, commit messages),
temporal search and X-Ray over the internal catalog are refused for every
restricted caller with a fixed text that names the surface and no
repository content. Admins (``admins`` group, no acting scope) keep them.

Detection is by the golden identity of the repository the door serves:
doors that may serve the caller's activation use ``catalog_surface_refusal``
(an activation of the catalog under any alias, or an activation whose
source cannot be resolved, counts as the catalog); doors that only serve
golden repositories use ``golden_catalog_surface_refusal``.

All functions read stores synchronously; async callers run them through
``anyio.to_thread.run_sync``.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from ..constants import is_internal_meta_repo
from .catalog_requests import is_catalog_request
from .visibility import resolve_meta_visibility

if TYPE_CHECKING:
    from ..access_filtering_service import AccessFilteringService


class CatalogSurface(Enum):
    """A catalog surface refused outright for restricted callers; the
    member's value is the refusal text."""

    GIT_HISTORY = "Git history is not available for this repository."
    TEMPORAL = "Temporal search is not available for this repository."
    XRAY = "X-Ray is not available for this repository."

    @property
    def refusal(self) -> str:
        return str(self.value)


def catalog_surface_refusal(
    surface: CatalogSurface,
    alias: Any,
    username: str,
    access_service: Optional["AccessFilteringService"],
    activated_repo_manager: Optional[Any],
) -> Optional[str]:
    """The refusal when *alias* (a golden alias or the caller's activation)
    serves the catalog and the caller is restricted, else None. Visibility
    is resolved only for a catalog request; without the access service the
    caller is restricted."""
    if not isinstance(alias, str) or not alias:
        return None
    if not is_catalog_request(alias, username, activated_repo_manager):
        return None
    if resolve_meta_visibility(username, access_service).unrestricted:
        return None
    return str(surface.value)


def golden_alias_is_catalog(alias: Any) -> bool:
    """True when a golden-only door would resolve *alias* to the catalog
    (it reads ``alias`` and then ``alias-global`` from the alias registry)."""
    if not isinstance(alias, str) or not alias:
        return False
    return is_internal_meta_repo(alias) or is_internal_meta_repo(f"{alias}-global")


def golden_catalog_surface_refusal(
    surface: CatalogSurface,
    alias: Any,
    username: str,
    access_service: Optional["AccessFilteringService"],
) -> Optional[str]:
    """The refusal for a door that serves golden repositories only."""
    if not golden_alias_is_catalog(alias):
        return None
    if resolve_meta_visibility(username, access_service).unrestricted:
        return None
    return str(surface.value)


def refuse_catalog_temporal(
    alias: Any,
    username: str,
    access_service: Optional["AccessFilteringService"],
    activated_repo_manager: Optional[Any],
) -> None:
    """Reject a temporal search of the catalog before any job is submitted.

    Raises:
        SearchRequestError: the caller is restricted and *alias* serves the
            catalog (a request rejection: no search event is recorded).
    """
    refusal = catalog_surface_refusal(
        CatalogSurface.TEMPORAL,
        alias,
        username,
        access_service,
        activated_repo_manager,
    )
    if refusal is not None:
        from ...query.semantic_query_manager import SearchRequestError

        raise SearchRequestError(refusal)
