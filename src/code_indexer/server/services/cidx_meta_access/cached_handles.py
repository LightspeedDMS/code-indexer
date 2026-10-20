"""Cached catalog content is served only while the caller's view is unchanged.

A door that may serve the internal catalog binds the handles minted during
the request to the caller's visibility (``bind_catalog_handles``; the
payload cache writes it into the handle key). Aliases are judged with the
history policy's alias-shape rule, so a shape it cannot judge (a tuple, an
empty collection, a non-string, a wildcard, an over-nested list) binds.
Every generic read door asks ``require_catalog_handle_visible`` before
serving a handle: a bound handle whose recorded visibility differs from the
caller's CURRENT visibility (a grant revoked or added, admin membership
changed) answers exactly like an expired handle. Visibility is resolved
fresh on every read and never cached.

Functions read the group and activation stores synchronously; async callers
run them through ``anyio.to_thread.run_sync``.
"""

from __future__ import annotations

from contextvars import Token
from typing import TYPE_CHECKING, Any, Optional

from ...cache.handle_binding import bind_handles, handle_binding
from ...cache.payload_cache import CacheNotFoundError
from .catalog_requests import is_catalog_request
from .history_policy import _may_serve_catalog
from .served_identity import (
    alias_serves_catalog_path,
    golden_repos_dir_of,
    request_catalog_locations,
)
from .visibility import resolve_meta_visibility, unrestricted_caller

if TYPE_CHECKING:
    from ..access_filtering_service import AccessFilteringService


def bind_catalog_handles(
    aliases: Any,
    username: str,
    access_service: Optional["AccessFilteringService"],
    activated_repo_manager: Optional[Any],
) -> "Token[Optional[str]]":
    """Bind the handles this request mints to the caller's visibility when
    *aliases* (one alias, a collection, or a wildcard) may serve the
    catalog; for a restricted caller the directories served are checked
    too. Pass the returned token to ``reset_handle_binding`` when done."""
    restricted = not unrestricted_caller(username, access_service)
    golden_repos_dir = golden_repos_dir_of(activated_repo_manager)
    locations = (
        request_catalog_locations(golden_repos_dir) if golden_repos_dir else None
    )

    def is_catalog(name: str) -> bool:
        if is_catalog_request(name, username, activated_repo_manager):
            return True
        return restricted and alias_serves_catalog_path(
            name, username, activated_repo_manager, golden_repos_dir, locations
        )

    if not _may_serve_catalog(aliases, is_catalog):
        return bind_handles(None)
    return bind_handles(resolve_meta_visibility(username, access_service).vis_hash)


def require_catalog_handle_visible(
    handle: Any,
    username: str,
    access_service: Optional["AccessFilteringService"],
) -> None:
    """Refuse a bound handle unless the caller still sees what it saw.

    Raises:
        CacheNotFoundError: the handle is bound and the access service is
            missing, or the caller's current visibility differs.
    """
    bound = handle_binding(handle) if isinstance(handle, str) else None
    if bound is None:
        return
    if access_service is None:
        raise CacheNotFoundError(f"Cache handle not found: {handle}")
    if resolve_meta_visibility(username, access_service).vis_hash != bound:
        raise CacheNotFoundError(f"Cache handle not found: {handle}")
