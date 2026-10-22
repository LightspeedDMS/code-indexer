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
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    List,
    Mapping,
    Optional,
    Sequence,
    TypeVar,
)

from ..constants import is_internal_meta_repo
from ..repo_access_guard import normalize_repo_alias
from .visibility import (
    UNRESOLVED_IDENTITY,
    MetaVisibility,
    activation_identities,
    is_internal_meta_identity,
    names_activation,
    request_alias_identities,
    resolve_meta_visibility,
    unrestricted_caller,
)

if TYPE_CHECKING:
    from ..access_filtering_service import AccessFilteringService

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class ServedRepository:
    """One request's resolution, read once and used for both serving and
    filtering: the directory served (None = not found), the alias it is
    served under, the caller's visibility when the catalog restriction
    applies (None = unrestricted), and whether the activation record says
    it is a composite."""

    path: Optional[str]
    alias: str
    visibility: Optional[MetaVisibility]
    composite: bool = False


def _spelled_like_catalog(alias: str) -> bool:
    return is_internal_meta_repo(alias) or is_internal_meta_repo(f"{alias}-global")


def resolve_served_repository(
    alias: str,
    username: str,
    access_service: Optional["AccessFilteringService"],
    activated_repo_manager: Optional[Any],
    resolve_golden: Callable[[str], Optional[str]],
    promote: Callable[[str], Optional[str]],
) -> ServedRepository:
    """Resolve *alias* once for one request.

    The caller's activation with that ``user_alias`` is read once; when one
    exists it is the only repository served (its directory, or not found),
    never a same-named registry entry. Only when the caller has no such
    activation is the alias promoted (*promote*) and served from the
    registry (*resolve_golden*), and then, for a restricted caller, only a
    repository visible to them or the catalog. An activation that cannot be
    read answers not found.
    """
    unrestricted = unrestricted_caller(username, access_service)
    record: Optional[Mapping[str, Any]] = None
    if activated_repo_manager is not None and names_activation(alias):
        try:
            found = activated_repo_manager.get_repository(username, alias, touch=False)
        except Exception as exc:
            logger.warning(
                "Activation lookup failed (%s); answering not found",
                type(exc).__name__,
            )
            return ServedRepository(None, alias, None)
        record = found if isinstance(found, Mapping) else None

    if record is not None:
        path = activated_repo_manager.get_activated_repo_path(username, alias)  # type: ignore[union-attr]
        served = str(path) if path and Path(path).is_dir() else None
        catalog = _spelled_like_catalog(alias) or is_internal_meta_identity(
            activation_identities(record)
        )
        visibility = (
            None
            if unrestricted or not catalog
            else resolve_meta_visibility(username, access_service)
        )
        return ServedRepository(
            served, alias, visibility, bool(record.get("is_composite"))
        )

    served_alias = (promote(alias) if names_activation(alias) else None) or alias
    if served_alias != alias:
        logger.info(
            "bare-alias fallback: %r -> %r for user %r", alias, served_alias, username
        )
    unresolvable = activated_repo_manager is None and names_activation(alias)
    catalog = _spelled_like_catalog(alias) or is_internal_meta_identity(
        (UNRESOLVED_IDENTITY,) if unresolvable else (served_alias,)
    )
    bare = names_activation(alias)
    if unrestricted or not (catalog or bare):
        return ServedRepository(resolve_golden(served_alias), served_alias, None)
    visibility = resolve_meta_visibility(username, access_service)
    if not catalog and normalize_repo_alias(served_alias) not in visibility.visible:
        return ServedRepository(None, served_alias, None)
    return ServedRepository(
        resolve_golden(served_alias), served_alias, visibility if catalog else None
    )


def is_catalog_request(
    alias: str, username: str, activated_repo_manager: Optional[Any]
) -> bool:
    """True when *alias* may serve the internal catalog repository."""
    if _spelled_like_catalog(alias):
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
