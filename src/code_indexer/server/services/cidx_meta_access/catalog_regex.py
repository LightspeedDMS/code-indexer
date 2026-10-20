"""Regex matches on the catalog, as one restricted caller may see them.

Shared by the MCP ``regex_search`` tool and REST ``/api/regex/search``. For
a restricted caller a request is the catalog unless it is proven not to be:
its directory is not a catalog location AND its alias is a concrete name
that neither names nor resolves to the catalog. An unknown golden
directory or an alias shape that cannot be judged counts as the catalog.
Unrestricted callers and proven non-catalog repositories are untouched.
Synchronous file and store reads: async doors call it through
``anyio.to_thread``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .catalog_requests import is_catalog_request
from .catalog_search import CatalogProjectionUnavailable, server_app_state
from .hit_projection import open_catalog_scope, project_regex_matches
from .history_policy import _may_serve_catalog
from .served_identity import golden_repos_dir_of, path_is_catalog_location
from .universe_provider import registry_of
from .visibility import unrestricted_caller


def _serves_catalog(
    alias: Any, repo_path: Path, username: str, activated_repo_manager: Optional[Any]
) -> bool:
    golden_repos_dir = golden_repos_dir_of(activated_repo_manager)
    if not golden_repos_dir:
        return True
    if path_is_catalog_location(str(repo_path), str(golden_repos_dir)):
        return True
    return _may_serve_catalog(
        alias, lambda name: is_catalog_request(name, username, activated_repo_manager)
    )


def restrict_regex_matches(
    matches: Sequence[Dict[str, Any]],
    *,
    alias: Any,
    repo_path: Path,
    username: str,
    access_service: Optional[Any],
    activated_repo_manager: Optional[Any],
    pattern: str,
    case_sensitive: bool,
    multiline: bool,
) -> Optional[List[Dict[str, Any]]]:
    """The caller's projected matches, or None when no restriction applies.

    Raises:
        CatalogProjectionUnavailable: a restricted caller's catalog search
            cannot be projected (no server state, registry or scope).
    """
    if unrestricted_caller(username, access_service):
        return None
    if not _serves_catalog(alias, Path(repo_path), username, activated_repo_manager):
        return None
    state = server_app_state()
    registry = None if state is None else registry_of(state)
    if registry is None:
        raise CatalogProjectionUnavailable("catalog regex needs the alias registry")
    scope = open_catalog_scope(username, access_service, Path(repo_path), registry)
    if scope is None:
        raise CatalogProjectionUnavailable("catalog regex scope unavailable")
    return project_regex_matches(
        matches,
        scope,
        pattern=pattern,
        case_sensitive=case_sensitive,
        multiline=multiline,
    )
