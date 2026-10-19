"""Door-side decisions for a request naming a repository by alias.

A request alias serves the catalog when it is spelled like the catalog
(the alias registry resolves such a name to the catalog before any
activation) or when its golden identity is the catalog. Unrestricted
callers need no decision, so nothing is looked up for them.

All functions read stores synchronously; async callers run them through
``anyio.to_thread.run_sync``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Sequence, TypeVar

from ..constants import is_internal_meta_repo
from .visibility import (
    is_internal_meta_identity,
    request_alias_identities,
    unrestricted_caller,
)

if TYPE_CHECKING:
    from ..access_filtering_service import AccessFilteringService

logger = logging.getLogger(__name__)

T = TypeVar("T")


def served_repo_path(
    alias: str,
    username: str,
    activated_repo_manager: Optional[Any],
    resolve_golden: Callable[[str], Optional[str]],
) -> Optional[str]:
    """The directory a request for *alias* serves: the caller's activation
    with that ``user_alias`` first (as the file listing, file content and
    regex doors serve it, and as the dispatcher authorises it), else
    *resolve_golden*. None when the activation's directory is missing or
    nothing resolves."""
    if (
        activated_repo_manager is not None
        and not alias.endswith("-global")
        and activated_repo_manager.user_has_activated_repo(username, alias)
    ):
        path = activated_repo_manager.get_activated_repo_path(username, alias)
        return str(path) if path and Path(path).is_dir() else None
    return resolve_golden(alias)


def is_catalog_request(
    alias: str, username: str, activated_repo_manager: Optional[Any]
) -> bool:
    """True when *alias* may serve the internal catalog repository."""
    if is_internal_meta_repo(alias) or is_internal_meta_repo(f"{alias}-global"):
        return True
    return is_internal_meta_identity(
        request_alias_identities(alias, username, activated_repo_manager)
    )


def catalog_restriction_applies(
    alias: Any,
    username: str,
    access_service: Optional["AccessFilteringService"],
    activated_repo_manager: Optional[Any],
) -> bool:
    """True when a request for *alias* must be restricted to the caller's
    view of the catalog. Unrestricted callers are answered before any
    activation lookup and without reading their grants; a missing access
    service always restricts."""
    if not isinstance(alias, str) or not alias:
        return False
    if unrestricted_caller(username, access_service):
        return False
    return is_catalog_request(alias, username, activated_repo_manager)


def restrict_catalog_paths(
    paths: Sequence[str],
    alias: Any,
    username: str,
    access_service: Optional["AccessFilteringService"],
    activated_repo_manager: Optional[Any],
) -> Optional[List[str]]:
    """The catalog-relative *paths* the caller may see, or None when no
    restriction applies (not the catalog, or an unrestricted caller).
    Without the access service nothing is allowed."""
    if not catalog_restriction_applies(
        alias, username, access_service, activated_repo_manager
    ):
        return None
    if access_service is None:
        logger.warning(
            "Access filtering service unavailable for a catalog request; "
            "returning no entries"
        )
        return []
    return access_service.filter_cidx_meta_files(list(paths), username)


def restrict_catalog_items(
    items: Sequence[T],
    key: Callable[[T], str],
    alias: Any,
    username: str,
    access_service: Optional["AccessFilteringService"],
    activated_repo_manager: Optional[Any],
) -> Optional[List[T]]:
    """The *items* whose catalog-relative path (``key(item)``) the caller
    may see, in input order, or None when no restriction applies."""
    allowed = restrict_catalog_paths(
        [key(item) for item in items],
        alias,
        username,
        access_service,
        activated_repo_manager,
    )
    if allowed is None:
        return None
    kept = set(allowed)
    return [item for item in items if key(item) in kept]
