"""Request-scoped MCP tool access decisions (Story #1593)."""

from __future__ import annotations

from typing import Any, Dict, Optional, Set


# Security boundary: this is the complete and intentionally exact exception
# set. Keep the set pinned by test; adding another member changes the public
# recovery surface and requires an explicit security review.
_ALWAYS_AVAILABLE_TOOLS = frozenset({"authenticate"})


# Tools that manage the session's impersonation itself.  They are authorized
# for, and performed by, the AUTHENTICATED principal, so an administrator can
# always clear or change an impersonation whatever the impersonated user may
# call.
AUTHENTICATED_PRINCIPAL_TOOLS = frozenset({"set_session_impersonation"})


def resolve_effective_user(
    user: Any, session_state: Any = None, tool_name: Optional[str] = None
) -> Any:
    """Resolve the principal used for every MCP check of *tool_name*.

    The impersonated user while the session impersonates one, except for
    :data:`AUTHENTICATED_PRINCIPAL_TOOLS`, which always use *user* (the
    authenticated principal).  Without *tool_name*, the impersonated user.
    """
    if tool_name in AUTHENTICATED_PRINCIPAL_TOOLS:
        return user
    if session_state is not None and getattr(session_state, "is_impersonating", False):
        effective_user = getattr(session_state, "effective_user", None)
        if effective_user is not None:
            return effective_user
    return user


def principal_for_tool(user: Any, session_state: Any, tool_name: str) -> Any:
    """:func:`resolve_effective_user` for a HANDLER, which receives the
    effective user: starts from the session's authenticated user (refreshed
    with the current one on every request by the session registry), so
    listings agree with ``tools/list``."""
    authenticated = user if session_state is None else session_state.authenticated_user
    return resolve_effective_user(authenticated, session_state, tool_name)


class ToolAccessMemo:
    """Per-request memo for group tool grants.

    Instances must be constructed by the top-level request handler and passed
    explicitly through dispatch. This deliberately has no module/class cache,
    so executor threads and concurrent requests cannot share authorization
    state.
    """

    def __init__(self, group_manager: Any = None):
        self._group_manager = group_manager
        self._tools_by_user: Dict[str, Set[str]] = {}
        self._ready: Optional[bool] = None

    def is_allowed(self, tool_name: str, user: Any) -> Optional[bool]:
        """Return True/False for group enforcement, or None during fallback."""
        if tool_name in _ALWAYS_AVAILABLE_TOOLS:
            return True

        if self._group_manager is None:
            return None

        if self._ready is None:
            self._ready = bool(self._group_manager.is_tool_access_enforcement_ready())
        if not self._ready:
            # AC9: before Story 2's readiness marker, callers retain the
            # legacy role/permission decision rather than being denied.
            # Bug #2076: nothing writes the marker in this version, so grants
            # are not enforced (the REST grant routes report enforced=false).
            # Invariant: a per-group tool grant may only restrict the
            # caller's role permissions, never extend them.
            return None

        username = str(user.username)
        if username not in self._tools_by_user:
            membership = self._group_manager.get_user_group(username)
            if membership is None:
                self._tools_by_user[username] = set()
            else:
                self._tools_by_user[username] = set(
                    self._group_manager.get_group_tools(membership.id)
                )
        return tool_name in self._tools_by_user[username]
