"""Regex matches on the catalog, as one restricted caller may see them.

Shared by the MCP ``regex_search`` tool and REST ``/api/regex/search``. For
a restricted caller a request is the catalog unless it is proven not to be:
its directory is not a catalog location AND its alias is a concrete name
that neither names nor resolves to the catalog. An unknown golden
directory or an alias shape that cannot be judged counts as the catalog.
Unrestricted callers and proven non-catalog repositories are untouched.

A restricted catalog request's result depends only on what the caller can
see: advanced regex mode (PCRE2) is unavailable on repositories the caller
sees through a filtered view, and the engine runs at the door's maximum
result count with the caller's page cut after projection
(``catalog_page``), so only visible matches fill the page.
Synchronous file and store reads: async doors call these functions
through ``anyio.to_thread``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .catalog_requests import is_catalog_request
from .catalog_search import CatalogProjectionUnavailable
from .hit_projection import open_catalog_scope, project_regex_matches
from .history_policy import _may_serve_catalog
from .served_identity import (
    alias_serves_catalog_path,
    golden_repos_dir_of,
    path_is_catalog_location,
)
from .visibility import unrestricted_caller

ADVANCED_REGEX_REFUSAL = "advanced regex mode is not available for this repository"


def _serves_catalog(
    alias: Any,
    repo_path: Optional[Path],
    username: str,
    activated_repo_manager: Optional[Any],
) -> bool:
    golden_repos_dir = golden_repos_dir_of(activated_repo_manager)
    if not golden_repos_dir:
        return True
    if repo_path is not None and path_is_catalog_location(
        str(repo_path), str(golden_repos_dir)
    ):
        return True

    def is_catalog(name: str) -> bool:
        if is_catalog_request(name, username, activated_repo_manager):
            return True
        return repo_path is None and alias_serves_catalog_path(
            name, username, activated_repo_manager, golden_repos_dir
        )

    return _may_serve_catalog(alias, is_catalog)


def catalog_regex_applies(
    alias: Any,
    repo_path: Optional[Path],
    username: str,
    access_service: Optional[Any],
    activated_repo_manager: Optional[Any],
) -> bool:
    """True when a restricted caller's request may be the catalog: judged by
    the directory served (*repo_path*) and *alias*, or, before resolution
    (*repo_path* None), by *alias* (a name or a list) and every directory
    each name may serve. Unrestricted callers: always False."""
    if unrestricted_caller(username, access_service):
        return False
    path = None if repo_path is None else Path(repo_path)
    return _serves_catalog(alias, path, username, activated_repo_manager)


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
    if not catalog_regex_applies(
        alias, repo_path, username, access_service, activated_repo_manager
    ):
        return None
    if registry is None:
        raise CatalogProjectionUnavailable("catalog regex needs the alias registry")
    scope = open_catalog_scope(username, access_service, Path(repo_path), registry)
    if scope is None:
        raise CatalogProjectionUnavailable("catalog regex scope unavailable")
    return project_regex_matches(matches, scope)


def catalog_page(
    matches: Sequence[Dict[str, Any]], max_results: int
) -> List[Dict[str, Any]]:
    """The caller's page of projected *matches*: the first *max_results*."""
    return list(matches[: max(0, max_results)])


def caller_visible_flag(
    raw: bool, projected_count: Optional[int], max_results: int
) -> bool:
    """The engine's ``truncated`` / ``read_capped`` flag as the caller may
    see it: unchanged when no projection ran (*projected_count* None);
    after projection, True when more projected matches exist than the page
    shows, else kept only while the projected page is full, so the flag
    never signals matches the caller cannot see."""
    if projected_count is None:
        return bool(raw)
    if projected_count > max_results:
        return True
    return bool(raw) and projected_count >= max_results
