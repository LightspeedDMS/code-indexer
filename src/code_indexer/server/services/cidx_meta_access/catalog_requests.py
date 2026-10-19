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
from typing import TYPE_CHECKING, Any, List, Optional, Sequence

from ..constants import is_internal_meta_repo
from .visibility import (
    is_internal_meta_identity,
    request_alias_identities,
    resolve_meta_visibility,
)

if TYPE_CHECKING:
    from ..access_filtering_service import AccessFilteringService

logger = logging.getLogger(__name__)


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
    activation lookup."""
    if not isinstance(alias, str) or not alias:
        return False
    if (
        access_service is not None
        and resolve_meta_visibility(username, access_service).unrestricted
    ):
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
