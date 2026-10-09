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

Routes that serve the caller's own ACTIVATED repository by its alias call
enforce_activated_repo_access(), which applies the MCP dispatcher's rule
for such an alias (AccessFilteringService.alias_granted over the caller's
activation sources) and refuses exactly as the route refuses an alias the
caller never activated.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Dict, Iterable, List, Optional, Set, Union

import anyio.to_thread
from fastapi import HTTPException, status

from ..services.access_filtering_service import AccessFilteringService
from ..services.repo_access_guard import (
    AccessFilteringServiceUnavailableError,
    RepoAccessDeniedError,
    require_repo_access,
)

logger = logging.getLogger(__name__)

# The only text a client sees when an activated-repo access check cannot be
# completed; the cause is logged server-side.
ACCESS_NOT_VERIFIED_DETAIL = "Repository access could not be verified"


def module_app_access_filtering_service() -> Optional[AccessFilteringService]:
    """The access service wired on the server app (as the MCP dispatcher
    reads it); None when it is not wired, which every guard fails closed on."""
    from code_indexer.server import app as app_module

    server_app = getattr(app_module, "app")
    service: Optional[AccessFilteringService] = getattr(
        server_app.state, "access_filtering_service", None
    )
    return service


def activated_repo_not_found(user_alias: str) -> HTTPException:
    """The 404 for an alias that names none of the caller's activations."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Repository '{user_alias}' not found or not activated",
    )


class ActivationAccess:
    """One caller's activation-access view, loaded ONCE per request.

    ``admin`` is admins-group membership (the definition MCP uses); for an
    admin nothing else is loaded and every alias is allowed.
    """

    def __init__(
        self,
        admin: bool,
        accessible: Set[str],
        activations: Dict[str, Any],
    ) -> None:
        self.admin = admin
        self._accessible = accessible
        self._activations = activations

    def allows(self, user_alias: str, activation_required: bool = True) -> bool:
        """True when the caller may use their own activation *user_alias*.

        Anyone but an admin needs *user_alias* to be one of their OWN
        activations whose every source golden repository is granted (a
        colliding alias needs its golden name granted too). Another user's
        alias is never among the caller's activations, so it is refused
        like an unknown alias -- or, when not *activation_required*, left
        to the route's own (golden) handling.
        """
        if self.admin:
            return True
        if user_alias not in self._activations:
            return not activation_required
        return AccessFilteringService.alias_granted(
            user_alias, self._accessible, self._activations
        )


def _available(
    access_filtering_service: Optional[AccessFilteringService],
) -> AccessFilteringService:
    if access_filtering_service is None:
        raise AccessFilteringServiceUnavailableError(
            "access_filtering_service unavailable -- repository access "
            "cannot be verified; failing closed"
        )
    return access_filtering_service


def _load_activation_access(
    access_filtering_service: Optional[AccessFilteringService], username: str
) -> ActivationAccess:
    service = _available(access_filtering_service)
    if service.is_admin_user(username):
        return ActivationAccess(True, set(), {})
    return ActivationAccess(
        False,
        service.get_accessible_repos(username),
        service.caller_activation_sources(username),
    )


def _access_not_verified(username: str, what: str) -> HTTPException:
    """Log the failed lookup (with its cause) and build the generic 500."""
    logger.error(
        "Access check for %s of user '%s' failed; refusing the request",
        what,
        username,
        exc_info=True,
    )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "error_code": "access_control_unavailable",
            "detail": ACCESS_NOT_VERIFIED_DETAIL,
        },
    )


def activation_access(
    access_filtering_service: Optional[AccessFilteringService], username: str
) -> ActivationAccess:
    """Load *username*'s activation-access view once (sync I/O: call it
    from a sync route or a worker thread).

    Raises:
        HTTPException 500: service missing or a lookup raised; generic
            detail, cause logged (fails closed).
    """
    try:
        return _load_activation_access(access_filtering_service, username)
    except Exception:
        raise _access_not_verified(username, "activated repositories")


def is_access_admin(
    access_filtering_service: Optional[AccessFilteringService], username: str
) -> bool:
    """Admins-group membership, the access model's (and MCP's) admin.

    Raises:
        HTTPException 500: service missing or the lookup raised; generic
            detail, cause logged (fails closed).
    """
    try:
        return bool(_available(access_filtering_service).is_admin_user(username))
    except Exception:
        raise _access_not_verified(username, "admin membership")


def enforce_activated_repo_access(
    access_filtering_service: Optional[AccessFilteringService],
    username: str,
    user_alias: str,
    refusal: Optional[HTTPException] = None,
    *,
    activation_required: bool = True,
) -> None:
    """Require the caller's grants on their activation *user_alias*'s sources.

    Synchronous (database and filesystem I/O): call it from a sync route,
    or through enforce_activated_repo_access_async() from an async one.

    Args:
        refusal: the route's own response for an alias the caller never
            activated; defaults to activated_repo_not_found(user_alias).
        activation_required: False for a route that serves a golden
            repository when *user_alias* is none of the caller's
            activations (and checks that golden access itself): such an
            alias then passes here.

    Raises:
        HTTPException: *refusal* when access is not granted.
        HTTPException 500: the access check itself failed (service missing
            or a lookup raised); generic detail, cause logged.
    """
    try:
        allowed = _load_activation_access(access_filtering_service, username).allows(
            user_alias, activation_required
        )
    except Exception:
        raise _access_not_verified(username, f"activated repository '{user_alias}'")
    if not allowed:
        raise refusal if refusal is not None else activated_repo_not_found(user_alias)


async def enforce_activated_repo_access_async(
    access_filtering_service: Optional[AccessFilteringService],
    username: str,
    user_alias: str,
    refusal: Optional[HTTPException] = None,
) -> None:
    """enforce_activated_repo_access() on a worker thread, never on the
    event loop."""
    await anyio.to_thread.run_sync(
        functools.partial(
            enforce_activated_repo_access,
            access_filtering_service,
            username,
            user_alias,
            refusal,
        )
    )


async def is_access_admin_async(
    access_filtering_service: Optional[AccessFilteringService], username: str
) -> bool:
    """is_access_admin() on a worker thread, never on the event loop."""
    return await anyio.to_thread.run_sync(
        functools.partial(is_access_admin, access_filtering_service, username)
    )


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
        raise access_denied_error(e.alias, e.username)
    except AccessFilteringServiceUnavailableError as e:
        raise access_control_unavailable_error(e)


def access_denied_error(alias: str, username: str) -> HTTPException:
    """The REST refusal for a repository the caller has no access to."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error_code": "access_denied",
            "detail": str(RepoAccessDeniedError(alias, username)),
        },
    )


def access_control_unavailable_error(
    exc: AccessFilteringServiceUnavailableError,
) -> HTTPException:
    """The REST refusal for an unavailable access service (fails closed)."""
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error_code": "access_control_unavailable", "detail": str(exc)},
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
