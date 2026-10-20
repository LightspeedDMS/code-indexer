"""Cached catalog content is served only while the caller's view is unchanged.

A door that may serve the internal catalog binds the handles minted during
the request to the caller's visibility (``bind_catalog_handles``; the
payload cache writes it into the handle key). Every generic read door asks
``require_catalog_handle_visible`` before serving a handle: a bound handle
whose recorded visibility differs from the caller's CURRENT visibility (a
grant revoked or added, admin membership changed) answers exactly like an
expired handle. Visibility is resolved fresh on every read and never cached.

Functions read the group and activation stores synchronously; async callers
run them through ``anyio.to_thread.run_sync``.
"""

from __future__ import annotations

from contextvars import Token
from typing import TYPE_CHECKING, Any, Optional

from ...cache.handle_binding import bind_handles, handle_binding
from ...cache.payload_cache import CacheNotFoundError
from .catalog_requests import is_catalog_request
from .visibility import resolve_meta_visibility

if TYPE_CHECKING:
    from ..access_filtering_service import AccessFilteringService

_WILDCARD_CHARS = ("*", "?", "[")


def _may_serve_catalog(
    aliases: Any, username: str, activated_repo_manager: Optional[Any]
) -> bool:
    if isinstance(aliases, list):
        return any(
            _may_serve_catalog(a, username, activated_repo_manager) for a in aliases
        )
    if not isinstance(aliases, str) or not aliases:
        return False
    if any(ch in aliases for ch in _WILDCARD_CHARS):
        return True
    return is_catalog_request(aliases, username, activated_repo_manager)


def bind_catalog_handles(
    aliases: Any,
    username: str,
    access_service: Optional["AccessFilteringService"],
    activated_repo_manager: Optional[Any],
) -> "Token[Optional[str]]":
    """Bind the handles this request mints to the caller's visibility when
    *aliases* (one alias, a list, or a wildcard) may serve the catalog.
    Pass the returned token to ``reset_handle_binding`` when done."""
    if not _may_serve_catalog(aliases, username, activated_repo_manager):
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
