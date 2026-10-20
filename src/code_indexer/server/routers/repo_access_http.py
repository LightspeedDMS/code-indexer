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
    module_app_access_filtering_service as module_app_access_filtering_service,
    require_activation_allowed,
    require_repo_access,
)

logger = logging.getLogger(__name__)

# The only text a client sees when an activated-repo access check cannot be
# completed; the cause is logged server-side.
ACCESS_NOT_VERIFIED_DETAIL = "Repository access could not be verified"


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


def enforce_activated_history_access(
    activated_repo_manager: Optional[Any], username: str, user_alias: str
) -> None:
    """enforce_activated_repo_access() for a git route (a copy of the
    internal catalog is refused there like an unknown alias), then refuse
    the catalog's history (by the golden identity of *user_alias*) to every
    restricted caller: 403 with a fixed text naming no content. The second
    check stands on its own, whatever the activation rule allows.
    Synchronous: call it from a sync route."""
    service = module_app_access_filtering_service()
    enforce_activated_repo_access(service, username, user_alias)
    refuse_catalog_history(service, activated_repo_manager, username, user_alias)


def _history_refused(refusal: Optional[str]) -> None:
    if refusal is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)


def refuse_catalog_history(
    access_filtering_service: Optional[AccessFilteringService],
    activated_repo_manager: Optional[Any],
    username: str,
    alias: Any,
) -> None:
    """Refuse the catalog's history (commit and branch metadata, moving a
    clone's HEAD) when *alias* -- a golden alias or the caller's activation,
    judged by golden identity -- serves the catalog and the caller is
    restricted. Synchronous: call it from a sync route.

    Raises:
        HTTPException 403: fixed text naming no repository content.
    """
    from ..services.cidx_meta_access.history_policy import (
        CatalogSurface,
        catalog_surface_refusal,
    )

    _history_refused(
        catalog_surface_refusal(
            CatalogSurface.GIT_HISTORY,
            alias,
            username,
            access_filtering_service,
            activated_repo_manager,
        )
    )


async def refuse_catalog_history_async(
    access_filtering_service: Optional[AccessFilteringService],
    activated_repo_manager: Optional[Any],
    username: str,
    alias: Any,
) -> None:
    """refuse_catalog_history() on a worker thread, never on the event loop."""
    await anyio.to_thread.run_sync(
        functools.partial(
            refuse_catalog_history,
            access_filtering_service,
            activated_repo_manager,
            username,
            alias,
        )
    )


async def enforce_own_activation_async(
    username: str,
    user_alias: str,
    activated_repo_manager: Optional[Any],
    not_found_detail: str,
) -> None:
    """enforce_activated_repo_access_async() with the module app's access
    service, answering 404 *not_found_detail*; with *activated_repo_manager*
    the catalog's history is then refused too (403, fixed text)."""
    service = module_app_access_filtering_service()
    await enforce_activated_repo_access_async(
        service,
        username,
        user_alias,
        refusal=HTTPException(status_code=404, detail=not_found_detail),
    )
    if activated_repo_manager is not None:
        await refuse_catalog_history_async(
            service, activated_repo_manager, username, user_alias
        )


def refuse_golden_catalog_history(state: Any, username: str, alias: Any) -> None:
    """refuse_catalog_history() for a door that serves golden repositories
    only, with the access service and golden directory of the app *state*
    (alias pointer targets are checked too). Synchronous: call it from a
    sync route.

    Raises:
        HTTPException 403: fixed text naming no repository content.
    """
    from ..services.cidx_meta_access.history_policy import (
        CatalogSurface,
        golden_catalog_surface_refusal,
    )
    from ..services.cidx_meta_access.served_identity import state_golden_repos_dir

    _history_refused(
        golden_catalog_surface_refusal(
            CatalogSurface.GIT_HISTORY,
            alias,
            username,
            getattr(state, "access_filtering_service", None),
            state_golden_repos_dir(state),
        )
    )


def refuse_served_catalog_history(
    state: Any,
    activated_repo_manager: Optional[Any],
    username: str,
    alias: str,
    served_path: Any,
) -> None:
    """refuse_catalog_history() for the repository a door actually selected
    (*alias*), then for the directory it serves. Synchronous.

    Raises:
        HTTPException 403: fixed text naming no repository content.
    """
    from ..services.cidx_meta_access.history_policy import (
        CatalogSurface,
        served_catalog_surface_refusal,
    )
    from ..services.cidx_meta_access.served_identity import state_golden_repos_dir

    service = getattr(state, "access_filtering_service", None)
    refuse_catalog_history(service, activated_repo_manager, username, alias)
    _history_refused(
        served_catalog_surface_refusal(
            CatalogSurface.GIT_HISTORY,
            str(served_path),
            username,
            service,
            state_golden_repos_dir(state),
        )
    )


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
    return bool(
        await anyio.to_thread.run_sync(
            functools.partial(is_access_admin, access_filtering_service, username)
        )
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


def enforce_activation_allowed(
    access_filtering_service: Optional["AccessFilteringService"],
    username: str,
    aliases: Union[str, Iterable[str], None],
) -> None:
    """Refuse activating the internal catalog for *username* outside the
    admins group (services/repo_access_guard.require_activation_allowed).

    Raises:
        HTTPException 403: the same refusal as for an ungranted repository.
        HTTPException 500: access service unavailable (fails closed).
    """
    try:
        require_activation_allowed(access_filtering_service, username, aliases)
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
