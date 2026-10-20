"""Git history of the internal catalog is admin-only on every MCP tool that
exposes branch or commit metadata or moves a clone's HEAD.

``_legacy._resolve_git_repo_path`` asks ``catalog_history_refusal`` first;
tools that resolve their repository elsewhere (git tools going through
``git_operations_service`` by alias, and the branch tools get_branches and
switch_branch) are registered through ``guard_catalog_history`` so they ask
the same question before running.
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable, Dict, Optional

from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.cidx_meta_access.history_policy import (
    CatalogSurface,
    catalog_surface_refusal,
)

from . import _utils


def catalog_history_refusal(repository_alias: Any, username: str) -> Optional[str]:
    """The git-history refusal when *repository_alias* (a golden alias or
    the caller's activation, judged by golden identity) serves the catalog
    and the caller is restricted; None otherwise."""
    return catalog_surface_refusal(
        CatalogSurface.GIT_HISTORY,
        repository_alias,
        username,
        _utils._get_access_filtering_service(),
        _utils._get_activated_repo_manager(),
    )


def guard_catalog_history(
    handler: Callable[..., Dict[str, Any]],
    alias_param: str = "repository_alias",
) -> Callable[..., Dict[str, Any]]:
    """Refuse the catalog's history before *handler* runs; the repository
    is read from the argument named *alias_param*."""

    @wraps(handler)
    def guarded(args: Dict[str, Any], user: User, *extra: Any, **kw: Any) -> Any:
        refusal = catalog_history_refusal(args.get(alias_param), user.username)
        if refusal is not None:
            return _utils._mcp_response({"success": False, "error": refusal})
        return handler(args, user, *extra, **kw)

    return guarded


def guard_branch_tools(registry: Dict[str, Any]) -> None:
    """Wrap the registered branch tools (switch_branch names the caller's
    activation in ``user_alias``) with ``guard_catalog_history``."""
    registry["switch_branch"] = guard_catalog_history(
        registry["switch_branch"], alias_param="user_alias"
    )
    registry["get_branches"] = guard_catalog_history(registry["get_branches"])
