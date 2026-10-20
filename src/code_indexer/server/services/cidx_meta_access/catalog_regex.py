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
from .catalog_search import CatalogProjectionUnavailable
from .hit_projection import open_catalog_scope, project_regex_matches
from .history_policy import _may_serve_catalog
from .served_identity import golden_repos_dir_of, path_is_catalog_location
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
    registry: Optional[Any],
) -> Optional[List[Dict[str, Any]]]:
    """The caller's projected matches, or None when no restriction applies.
    The services and *registry* come from the app serving the request.

    Raises:
        CatalogProjectionUnavailable: a restricted caller's catalog search
            cannot be projected (no registry or scope).
    """
    if unrestricted_caller(username, access_service):
        return None
    if not _serves_catalog(alias, Path(repo_path), username, activated_repo_manager):
        return None
    if registry is None:
        raise CatalogProjectionUnavailable("catalog regex needs the alias registry")
    scope = open_catalog_scope(username, access_service, Path(repo_path), registry)
    if scope is None:
        raise CatalogProjectionUnavailable("catalog regex scope unavailable")
    return project_regex_matches(matches, scope)


def caller_visible_flag(
    raw: bool, projected_count: Optional[int], max_results: int
) -> bool:
    """The engine's ``truncated`` / ``read_capped`` flag as the caller may
    see it: unchanged when no projection ran (*projected_count* None);
    after projection, kept only while the projected page is full, so the
    flag never signals matches the caller cannot see."""
    if projected_count is None:
        return bool(raw)
    return bool(raw) and projected_count >= max_results
