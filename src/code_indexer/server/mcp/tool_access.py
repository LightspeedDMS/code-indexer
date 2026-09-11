"""Request-scoped MCP tool access decisions (Story #1593)."""

from __future__ import annotations

from typing import Any, Dict, Optional, Set


# Security boundary: this is the complete and intentionally exact exception
# set. Keep the set pinned by test; adding another member changes the public
# recovery surface and requires an explicit security review.
_ALWAYS_AVAILABLE_TOOLS = frozenset({"authenticate"})


def resolve_effective_user(user: Any, session_state: Any = None) -> Any:
    """Resolve the post-impersonation principal used for every MCP check."""
    if session_state is not None and getattr(session_state, "is_impersonating", False):
        effective_user = getattr(session_state, "effective_user", None)
        if effective_user is not None:
            return effective_user
    return user


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
