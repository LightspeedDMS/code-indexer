"""REST shaping of the shared repository access guard.

Routes that name or list golden repositories call these helpers so every
REST door gives the same decision as the MCP dispatcher. The decision
itself lives in services/repo_access_guard.require_repo_access() and
AccessFilteringService.filter_repo_listing(); this module only turns it
into the REST response convention:

- 403 ``{"error_code": "access_denied", ...}`` when the caller lacks group
  access to a named repository;
- 500 ``{"error_code": "access_control_unavailable", ...}`` when the access
  service is unavailable (fails closed, for every caller including admins).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, List, Optional, Union

from fastapi import HTTPException, status

from ..services.repo_access_guard import (
    AccessFilteringServiceUnavailableError,
    RepoAccessDeniedError,
    require_repo_access,
)

if TYPE_CHECKING:
    from ..services.access_filtering_service import AccessFilteringService


def enforce_repo_access(
    access_filtering_service: Optional["AccessFilteringService"],
    username: str,
    aliases: Union[str, Iterable[str], None],
) -> None:
    """Require group access to every repository in *aliases*.

    With ``aliases=None`` it only requires the access service to be
    available. Admins bypass the group check, as on every door.

    Raises:
        HTTPException 403: caller lacks access to one of *aliases*.
        HTTPException 500: access service unavailable (fails closed).
    """
    try:
        require_repo_access(access_filtering_service, username, aliases)
    except RepoAccessDeniedError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error_code": "access_denied", "detail": str(e)},
        )
    except AccessFilteringServiceUnavailableError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error_code": "access_control_unavailable", "detail": str(e)},
        )


def accessible_repo_names(
    access_filtering_service: Optional["AccessFilteringService"],
    username: str,
    names: List[str],
) -> List[str]:
    """Return the subset of repository *names* the caller has access to.

    Admins see every name. A listing never falls back to the unfiltered
    names: an unavailable access service raises 500 instead.

    Raises:
        HTTPException 500: access service unavailable (fails closed).
    """
    enforce_repo_access(access_filtering_service, username, None)
    # Invariant: enforce_repo_access() above raised if the service is None.
    assert access_filtering_service is not None
    return access_filtering_service.filter_repo_listing(names, username)
