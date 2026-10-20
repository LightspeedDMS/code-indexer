"""Catalog surfaces that cannot be projected line by line: admin-only.

Git history (earlier revisions, diffs, blame, commit messages, branch
metadata, moving a clone's HEAD), temporal search and X-Ray over the
internal catalog are refused for every restricted caller with a fixed text
that names the surface and no repository content. Admins (``admins``
group, no acting scope) keep them and are answered before anything else is
looked at.

Detection is by the golden identity of the repository the door serves:
doors that may serve the caller's activation use ``catalog_surface_refusal``
(an activation of the catalog under any alias, or an activation whose
source cannot be resolved, counts as the catalog); doors that only serve
golden repositories use ``golden_catalog_surface_refusal``. Either way the
directory actually served is also checked against the catalog's locations
(``served_identity``), so a name or record that disagrees with the served
path still counts as the catalog.

For a restricted caller the gate fails closed on any request it cannot
judge as concrete repositories: a list or tuple is refused when it is empty
or any element is refused, a glob pattern (``*``, ``?``, ``[``) is refused
because it could expand to the catalog (a door that expands patterns asks
with the expanded list), and any other non-string or empty value is
refused.

All functions read stores synchronously; async callers run them through
``anyio.to_thread.run_sync``.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Optional

from ..constants import is_internal_meta_repo
from .catalog_requests import is_catalog_request
from .served_identity import alias_serves_catalog_path, golden_repos_dir_of
from .visibility import unrestricted_caller

if TYPE_CHECKING:
    from ..access_filtering_service import AccessFilteringService

_WILDCARD_CHARS = ("*", "?", "[")
_MAX_NESTING = 4


class CatalogSurface(Enum):
    """A catalog surface refused outright for restricted callers; the
    member's value is the refusal text."""

    GIT_HISTORY = "Git history is not available for this repository."
    TEMPORAL = "Temporal search is not available for this repository."
    XRAY = "X-Ray is not available for this repository."

    @property
    def refusal(self) -> str:
        return str(self.value)


def _may_serve_catalog(
    alias: Any, is_catalog: Callable[[str], bool], depth: int = 0
) -> bool:
    """True unless *alias* is concrete repository names none of which
    *is_catalog* judges to be the catalog."""
    if isinstance(alias, (list, tuple)):
        if not alias or depth >= _MAX_NESTING:
            return True
        return any(_may_serve_catalog(a, is_catalog, depth + 1) for a in alias)
    if not isinstance(alias, str) or not alias:
        return True
    if any(ch in alias for ch in _WILDCARD_CHARS):
        return True
    return is_catalog(alias)


def catalog_surface_refusal(
    surface: CatalogSurface,
    alias: Any,
    username: str,
    access_service: Optional["AccessFilteringService"],
    activated_repo_manager: Optional[Any],
) -> Optional[str]:
    """The refusal when *alias* (a golden alias, the caller's activation, or
    a collection of them) may serve the catalog and the caller is
    restricted, else None. Without the access service the caller is
    restricted."""
    if unrestricted_caller(username, access_service):
        return None
    golden_repos_dir = golden_repos_dir_of(activated_repo_manager)

    def is_catalog(name: str) -> bool:
        return is_catalog_request(
            name, username, activated_repo_manager
        ) or alias_serves_catalog_path(
            name, username, activated_repo_manager, golden_repos_dir
        )

    if _may_serve_catalog(alias, is_catalog):
        return surface.refusal
    return None


def golden_alias_is_catalog(alias: Any) -> bool:
    """True when a golden-only door would resolve *alias* to the catalog
    (it reads ``alias`` and then ``alias-global`` from the alias registry)."""
    if not isinstance(alias, str) or not alias:
        return False
    return is_internal_meta_repo(alias) or is_internal_meta_repo(f"{alias}-global")


def golden_request_may_serve_catalog(alias: Any) -> bool:
    """Pure (no store reads): True when a golden-only door's *alias* (one
    alias or a collection) may serve the catalog by name, including every
    shape the gate cannot judge."""
    return _may_serve_catalog(alias, golden_alias_is_catalog)


def golden_catalog_surface_refusal(
    surface: CatalogSurface,
    alias: Any,
    username: str,
    access_service: Optional["AccessFilteringService"],
    golden_repos_dir: Optional[str] = None,
) -> Optional[str]:
    """The refusal for a door that serves golden repositories only; with
    *golden_repos_dir* the alias pointer targets are checked as well."""
    if unrestricted_caller(username, access_service):
        return None

    def is_catalog(name: str) -> bool:
        return golden_alias_is_catalog(name) or alias_serves_catalog_path(
            name, username, None, golden_repos_dir
        )

    if _may_serve_catalog(alias, is_catalog):
        return surface.refusal
    return None


def refuse_catalog_temporal(
    alias: Any,
    username: str,
    access_service: Optional["AccessFilteringService"],
    activated_repo_manager: Optional[Any],
) -> None:
    """Reject a temporal search of the catalog before any job is submitted.

    Raises:
        SearchRequestError: the caller is restricted and *alias* may serve
            the catalog (a request rejection: no search event is recorded).
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
