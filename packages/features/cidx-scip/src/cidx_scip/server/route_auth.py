"""The caller of a SCIP REST route, as the core authenticated it.

The core authenticates every request to a feature router (bearer token, API
key or Web UI session) and checks the router's ``required_permission``
before a handler runs, then puts the caller on the request state
(``cidx_sdk.web.PRINCIPAL_STATE_ATTR``). :func:`current_caller` is the
FastAPI dependency the SCIP routes take their user from (it replaced the
core's ``get_current_user``), in the shape the SCIP code uses: ``username``,
``role`` and ``has_permission``.
"""

from __future__ import annotations

from typing import Optional

from cidx_sdk import PRINCIPAL_STATE_ATTR, Principal
from fastapi import HTTPException, Request, status

from cidx_scip.server import runtime


class RouteCaller:
    """The authenticated caller of a SCIP route.

    Any permission is asked of the ``permissions`` capability against the
    live account (False when the feature has not started).
    """

    def __init__(self, principal: Principal) -> None:
        self.username = principal.username
        self.role = principal.role

    def has_permission(self, permission: Optional[str]) -> bool:
        if permission is None:
            return True
        permissions = runtime.permissions()
        return permissions is not None and permissions.has_permission(
            self.username, permission
        )


def current_caller(request: Request) -> RouteCaller:
    """The caller the core authenticated for *request*."""
    principal = getattr(request.state, PRINCIPAL_STATE_ATTR, None)
    if not isinstance(principal, Principal):
        # Never reached through the core (it sets the principal first): a
        # router mounted some other way is refused rather than served.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials",
        )
    return RouteCaller(principal)
